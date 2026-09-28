"""真实公司工具链的四批次适配器。

此模块只把已确认的公司预处理/预测协议和本地 GDSX 读取能力转换为现有 Workflow
Contract；它不复用 Mock 结果，也不让 Agent 自行决定底层调用顺序。
"""

import asyncio
import math
from pathlib import Path
from typing import Any

from cnlc_agent.application.company_results import project_prediction
from cnlc_agent.domain.enums import StepId, StepStatus, ValidationStatus
from cnlc_agent.domain.errors import ToolError
from cnlc_agent.domain.models import (
    DataRequirements,
    JsonObject,
    LogCurve,
    RawData,
    StageResult,
    ValidationResult,
    Well,
    WellData,
)
from cnlc_agent.infrastructure.company_api import CompanyApiClient, CompanyApiSettings
from cnlc_agent.tools.contracts import ToolInput
from cnlc_agent.wplm.data_analysis_utils.reader import load_well


def _json_value(value: Any) -> Any:
    """将 numpy/pandas 读取结果降为严格 JSON，避免 NaN 进入状态或持久化。"""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        return _json_value(item())
    return str(value)


def _well_data(path: Path, well_id: str) -> WellData:
    """读取 GDSX 并显式标注未知单位，不能把缺失元数据伪造成测量单位。"""

    parsed = load_well(path)
    frame = parsed.df
    if frame.empty or "DEPTH" not in frame:
        raise ToolError("COMPANY_GDSX_DATA_EMPTY", "GDSX 中没有可用深度和曲线数据")
    depths = [_json_value(value) for value in frame["DEPTH"].tolist()]
    if not all(isinstance(value, (int, float)) for value in depths):
        raise ToolError("COMPANY_GDSX_DEPTH_INVALID", "GDSX 深度轴包含不可用数据")
    curves = {
        str(name): LogCurve(
            unit="unknown",
            values=[_json_value(value) for value in frame[name].tolist()],
        )
        for name in frame.columns
        if name != "DEPTH"
    }
    if not curves:
        raise ToolError("COMPANY_GDSX_CURVES_EMPTY", "GDSX 中没有可用曲线")
    info = _json_value(parsed.well_info)
    # 公司现有 yuce_api.py 使用 GDSX wellinfo.WELLNAME 作为预测 wellName。
    # 上传落盘文件名仅是会话临时标识，不能替代真实井名。
    name = (
        (info.get("WELLNAME") or info.get("wellName") or info.get("LEGALNAME"))
        if isinstance(info, dict)
        else None
    )
    if not isinstance(name, str) or not name.strip():
        name = well_id
    # 当前没有经确认的正式必需曲线表。仅要求本次真实文件实际读取到的曲线，
    # 避免在代码中补造区块专业阈值；后续由配置/业务字典替换。
    requirements = DataRequirements(required_curves=sorted(curves))
    return WellData(
        well=Well(well_id=well_id, name=name, extensions={"gdsx_well_info": info}),
        raw_data=RawData(depths=depths, curves=curves),
        requirements=requirements,
    )


class RealCompanyBatchProvider:
    """真实预处理、模型列表和预测的执行级编排，结果仍按四批次输出。"""

    def __init__(
        self, client: CompanyApiClient, settings: CompanyApiSettings, output_dir: Path
    ) -> None:
        self.client = client
        self.settings = settings
        self.output_dir = output_dir
        self._contexts: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _context(self, request: ToolInput) -> dict[str, Any]:
        execution_id = request.parameters.get("execution_id")
        input_version_id = request.parameters.get("input_version_id")
        if not isinstance(execution_id, str) or not isinstance(input_version_id, str):
            raise ToolError("COMPANY_CONTEXT_REQUIRED", "真实公司调用必须绑定执行和输入版本")
        key = (request.task_id, execution_id, input_version_id)
        return self._contexts.setdefault(
            key, {"execution_id": execution_id, "input_version_id": input_version_id}
        )

    def _source_path(self, request: ToolInput) -> Path:
        dynamic_path = request.parameters.get("source_path")
        configured_path = (
            Path(dynamic_path)
            if isinstance(dynamic_path, str) and dynamic_path.strip()
            else self.settings.gdsx_path
        )
        if configured_path is None:
            raise ToolError("COMPANY_GDSX_PATH_REQUIRED", "真实模式需要配置脱敏 GDSX 文件路径")
        path = configured_path.resolve()
        if not path.is_file() or path.suffix.lower() != ".gdsx":
            raise ToolError("COMPANY_FILE_INVALID", "真实模式配置的输入必须是存在的 GDSX 文件")
        return path

    def _real_settings(self) -> tuple[JsonObject, JsonObject, str, str]:
        operations = self.settings.preprocess_operations
        task_config = self.settings.task_config
        service_id = self.settings.service_id
        create_people = self.settings.create_people
        if not isinstance(operations, dict) or not operations:
            raise ToolError("COMPANY_OPERATIONS_REQUIRED", "真实模式需要配置预处理 operations")
        if not isinstance(task_config, dict) or not task_config:
            raise ToolError("COMPANY_TASK_CONFIG_REQUIRED", "真实模式需要配置预测 taskConfig")
        if not isinstance(service_id, str) or not service_id.strip():
            raise ToolError("COMPANY_SERVICE_ID_REQUIRED", "真实模式需要配置模型 serviceId")
        if not isinstance(create_people, str) or not create_people.strip():
            raise ToolError("COMPANY_CREATE_PEOPLE_REQUIRED", "真实模式需要配置预测创建人")
        return operations, task_config, service_id, create_people

    async def execute(self, stage: str, request: ToolInput) -> JsonObject:
        context = self._context(request)
        if stage == "analysis":
            data = await asyncio.to_thread(_well_data, self._source_path(request), request.well_id)
            context["source_data"] = data
            return {"well_data": data.model_dump(mode="json")}
        if stage == "preprocessing":
            operations, _, _, _ = self._real_settings()
            result = await self.client.preprocess(
                self._source_path(request),
                operations,
                context["execution_id"],
                context["input_version_id"],
            )
            directory = self.output_dir / "company" / context["execution_id"]
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
            processed_path = directory / "processed.gdsx"
            await asyncio.to_thread(processed_path.write_bytes, result.gdsx_content)
            processed_data = await asyncio.to_thread(_well_data, processed_path, request.well_id)
            context["processed_path"] = processed_path
            context["processed_data"] = processed_data.raw_data
            # 与 yuce_api.py 对齐：预测请求的 wellName 从预处理后的 GDSX
            # 读取，而不是沿用上传原文件或会话临时名。
            context["processed_well_name"] = processed_data.well.name
            context["log_req_json"] = result.call.data["logReqJson"]
            qc = StageResult(
                status=StepStatus.WARNING,
                result={
                    "operations": result.call.data["operations"],
                    "external_call_id": result.call.external_call_id,
                    "processed_curve_count": len(processed_data.raw_data.curves),
                },
                evidence=["已完成公司预处理并下载处理后 GDSX"],
                warnings=["真实 QC 图表尚未写入 InterpretationState"],
                is_mock=False,
                source=f"company:preprocessing:{result.call.external_call_id}",
            )
            return {
                "qc": qc.model_dump(mode="json"),
                "processed_data": processed_data.raw_data.model_dump(mode="json"),
                "operations_applied": True,
            }
        if stage == "interpretation":
            if "log_req_json" not in context:
                raise ToolError("COMPANY_PREPROCESS_REQUIRED", "预测前必须先完成真实预处理")
            _, task_config, service_id, create_people = self._real_settings()
            processed_well_name = context.get("processed_well_name")
            well_name = (
                processed_well_name
                if isinstance(processed_well_name, str) and processed_well_name.strip()
                else request.well_id
            )
            call = await self.client.predict(
                {
                    "encodingUrl": self.settings.encoding_url,
                    "wellName": well_name,
                    "serviceId": service_id,
                    "taskConfig": task_config,
                    "batchSize": self.settings.batch_size,
                    "createPeople": create_people,
                    "logReqJson": context["log_req_json"],
                },
                context["execution_id"],
                context["input_version_id"],
            )
            context["prediction"] = call
            projection = project_prediction(call)
            results: JsonObject = {}
            mapping = {
                "lithology": projection.steps.get(StepId.W04),
                "petrophysics": projection.steps.get(StepId.W05),
                "sw": projection.steps.get(StepId.W06),
                "classification": projection.steps.get(StepId.W07),
                "intervals": projection.steps.get(StepId.W08),
            }
            for key, result in mapping.items():
                if result is None or result.status == StepStatus.BLOCKED:
                    continue
                result.status = StepStatus.WARNING
                result.warnings.append("真实预测字段已接入；专业字段映射需结合脱敏样本复核")
                results[key] = result.model_dump(mode="json")
            if "sw" in results:
                results["fluid"] = dict(results["sw"])
            results["validation"] = real_validation_result().model_dump(mode="json")
            return results
        if stage == "report":
            return {
                "final_check": StageResult(
                    status=StepStatus.WARNING,
                    result={"company_report_api_connected": False},
                    warnings=["报告仍由本项目依据真实 Workflow 状态生成"],
                    is_mock=False,
                    source="company:report:local",
                ).model_dump(mode="json")
            }
        raise ToolError("COMPANY_STAGE_UNSUPPORTED", "未知公司处理步骤")

    async def close(self) -> None:
        """由应用关闭回调释放 HTTP 客户端。"""

        await self.client.aclose()


def real_validation_result() -> ValidationResult:
    """公司预测未携带独立验证资料时，明确终止于人工复核而不伪造验证结论。"""

    return ValidationResult(
        status=StepStatus.REVIEW_REQUIRED,
        result={"company_validation_api_connected": False},
        missing_evidence=["尚未接入岩心、录井、试油或邻井等独立验证资料"],
        recommended_action="provide_validation_evidence",
        is_mock=False,
        source="company:validation:missing-evidence",
        validation_status=ValidationStatus.INSUFFICIENT_EVIDENCE,
    )
