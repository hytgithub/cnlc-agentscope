"""公司真实测井能力适配器；独立调用底层服务，不依赖 qwen-agent-main。"""

import asyncio
import json
import logging
import math
import shutil
from copy import deepcopy
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
from cnlc_agent.infrastructure.company_api import (
    CompanyApiClient,
    CompanyApiSettings,
    CompanyCallResult,
)
from cnlc_agent.infrastructure.gdsx_artifacts import InputArtifactResolver
from cnlc_agent.pygdsx.config import STANDARD_CURVE_DICT
from cnlc_agent.pygdsx.curve import get_gdx_curve_info, get_gdx_curve_name_list
from cnlc_agent.tools.contracts import ToolInput
from cnlc_agent.wplm.data_analysis_utils.gdsx_service import run_standardize
from cnlc_agent.wplm.data_analysis_utils.reader import load_well
from cnlc_agent.wplm.tools import GdsxPostProcessTool, PostProcessTool


logger = logging.getLogger(__name__)


PREDICTION_CURVES = [
    "AC", "CAL", "CNL", "DEN", "GR", "PE", "RT", "RXO", "SP",
    "UPOSX", "UPOSY", "UPOSZ",
]
PREDICTION_TASK_CONFIG: JsonObject = {
    "CLS": ["DZFC", "CCHF", "JSJL"],
    "NUM": ["POR", "SW", "PERM", "SH", "SAND", "LIME", "DOLO", "CARB", "ANHY"],
}


def _json_value(value: Any) -> Any:
    """把 numpy/pandas 值转换为严格 JSON，并把非有限浮点值改为 null。"""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    item = getattr(value, "item", None)
    return _json_value(item()) if callable(item) else str(value)


def _well_data(path: Path, well_id: str) -> WellData:
    """读取本次真实 GDSX；这里不生成任何预测输入结构。"""

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
    name = (
        info.get("WELLNAME") or info.get("wellName") or info.get("LEGALNAME")
        if isinstance(info, dict)
        else None
    )
    if not isinstance(name, str) or not name.strip():
        name = well_id
    return WellData(
        well=Well(well_id=well_id, name=name, extensions={"gdsx_well_info": info}),
        raw_data=RawData(depths=depths, curves=curves),
        requirements=DataRequirements(required_curves=sorted(curves)),
    )


def _reference_log_req_json(value: Any) -> JsonObject:
    """只解析预处理服务返回的 logReqJson，禁止从 GDSX 自行构造。"""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise ToolError(
                "COMPANY_PREPROCESS_DATA_INVALID", "预处理返回的 logReqJson 不是有效 JSON"
            ) from None
    if not isinstance(value, dict) or not value:
        raise ToolError(
            "COMPANY_PREPROCESS_DATA_INVALID", "预处理返回的 logReqJson 必须是非空对象"
        )
    return value


def _operations_with_depth_range(path: Path, configured: JsonObject) -> JsonObject:
    """按已跑通业务规则计算全部可识别曲线的公共深度区间。"""

    operations = deepcopy(configured)
    if isinstance(operations.get("startDepth"), (int, float)) and isinstance(
        operations.get("endDepth"), (int, float)
    ):
        return operations
    aliases = {alias.upper() for values in STANDARD_CURVE_DICT.values() for alias in values}
    start, end = 0.0, 9999999.0
    for name in get_gdx_curve_name_list(str(path)) or []:
        if name.upper() not in aliases:
            continue
        curve = get_gdx_curve_info(str(path), name)
        if curve is None:
            continue
        curve_start = getattr(curve, "dimension1Start", 0)
        curve_end = getattr(curve, "dimension1End", 0)
        if isinstance(curve_start, (int, float)) and curve_start > 0:
            start = max(start, float(curve_start))
        if isinstance(curve_end, (int, float)) and curve_end > 0:
            end = min(end, float(curve_end))
    start, end = round(start, 2), round(end, 2)
    if end <= 0 or end < start or end == 9999999.0:
        raise ToolError("COMPANY_PREPROCESS_DEPTH_INVALID", "GDSX 不存在有效的公共深度区间")
    operations.update({"startDepth": start, "endDepth": end})
    return operations


def _preprocess_operations(path: Path, resample_interval: float) -> JsonObject:
    """使用 yuce 已验证的固定预处理参数，不让 Agent 动态生成协议结构。"""

    return _operations_with_depth_range(
        path,
        {
            "curves": list(PREDICTION_CURVES),
            "curveNameStandard": {"enable": True, "index": 1, "data": {}},
            "curveUnitStandard": {
                "enable": True,
                "index": 2,
                "data": {"CAL": "cm", "GR": "API"},
            },
            "resample": {
                "enable": True,
                "index": 3,
                "data": {"defaultInterval": resample_interval},
            },
            "wellCoordinateGenerate": {"enable": True, "index": 4, "data": True},
        },
    )


def _standardize_source(path: Path, directory: Path) -> tuple[Path, list[JsonObject]]:
    """在 AgentScope 内生成标准曲线名副本，保持原文件不变。"""

    result = run_standardize(
        read_gdsx_file_path=str(path),
        write_gdsx_file_folder=str(directory),
        write_gdsx_file_name="standardized.gdsx",
        standard_curve_dict=STANDARD_CURVE_DICT,
    )
    output = result.get("gdsx_file_path")
    renamed = result.get("renamed", [])
    if (
        result.get("success") is not True
        or not isinstance(output, str)
        or not Path(output).is_file()
        or not isinstance(renamed, list)
    ):
        raise ToolError("COMPANY_GDSX_STANDARDIZE_FAILED", "GDSX 曲线标准化失败")
    return Path(output), [item for item in renamed if isinstance(item, dict)]


def _postprocess_prediction(
    processed_path: Path, final_path: Path, prediction: CompanyCallResult
) -> tuple[JsonObject, list[Any]]:
    """调用本项目确定性后处理工具，将真实预测结果写回最终 GDSX。"""

    if not isinstance(prediction.data, dict):
        raise ToolError("COMPANY_PREDICTION_DATA_INVALID", "预测结果结构不合法")
    shutil.copy2(processed_path, final_path)
    raw = PostProcessTool().run(
        {
            "predictions": {"resultData": prediction.data.get("resultData")},
            "wplm_postprocessed_gdsx_file_path": str(final_path),
        }
    )
    try:
        postprocess = json.loads(raw)
    except json.JSONDecodeError:
        raise ToolError("COMPANY_POSTPROCESS_FAILED", "预测后处理返回无效 JSON") from None
    if not isinstance(postprocess, dict) or postprocess.get("success") is not True:
        raise ToolError("COMPANY_POSTPROCESS_FAILED", "预测成功，但结果写入 GDSX 失败")
    try:
        chart_raw = GdsxPostProcessTool().run(
            {"gdsx_path": str(final_path), "language": "CN", "save_files": False}
        )
        chart_result = json.loads(chart_raw)
    except Exception as exc:  # 图表不是预测结果落盘的前置条件，但失败必须留日志。
        logger.warning("最终 GDSX 图表生成失败: %s", type(exc).__name__)
        chart_result = {}
    charts = chart_result.get("option_files", []) if isinstance(chart_result, dict) else []
    return postprocess, charts if isinstance(charts, list) else []


class RealCompanyBatchProvider:
    """按现有四批次边界执行真实公司链路，保持 W01-W10 顺序不变。"""

    is_mock = False

    def __init__(
        self, client: CompanyApiClient, settings: CompanyApiSettings, output_dir: Path,
        input_artifacts: InputArtifactResolver | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.output_dir = output_dir
        self.input_artifacts = input_artifacts
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

    async def _source_path(self, request: ToolInput) -> Path:
        """只解析任务绑定的上传工件；固定 .env 路径仅保留旧的本地联调兼容。"""

        input_version_id = request.parameters.get("input_version_id")
        if self.input_artifacts is not None and isinstance(input_version_id, str):
            path = await self.input_artifacts.resolve_gdsx(request.task_id, input_version_id)
        elif self.settings.gdsx_path is not None:
            path = self.settings.gdsx_path.resolve()
        else:
            raise ToolError("COMPANY_GDSX_PATH_REQUIRED", "真实模式需要任务绑定的 GDSX 输入文件")
        if not path.is_file() or path.suffix.lower() != ".gdsx":
            raise ToolError("COMPANY_FILE_INVALID", "真实模式输入必须是存在的 GDSX 文件")
        return path

    def _prediction_identity(self) -> tuple[str, str]:
        service_id, creator = self.settings.service_id, self.settings.create_people
        if not isinstance(service_id, str) or not service_id.strip():
            raise ToolError("COMPANY_SERVICE_ID_REQUIRED", "真实模式需要配置模型 serviceId")
        if not isinstance(creator, str) or not creator.strip():
            raise ToolError("COMPANY_CREATE_PEOPLE_REQUIRED", "真实模式需要配置预测创建人")
        return service_id, creator

    async def execute(self, stage: str, request: ToolInput) -> JsonObject:
        """执行一个批次；同一 Execution 的预处理和预测中间结果只在此上下文共享。"""

        context = self._context(request)
        if stage == "analysis":
            source_path = await self._source_path(request)
            data = await asyncio.to_thread(_well_data, source_path, request.well_id)
            context["source_data"] = data
            return {"well_data": data.model_dump(mode="json")}

        if stage == "preprocessing":
            directory = self.output_dir / "company" / context["execution_id"]
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
            source_path = await self._source_path(request)
            standardized_path, renamed = await asyncio.to_thread(_standardize_source, source_path, directory)
            operations = await asyncio.to_thread(
                _preprocess_operations, standardized_path, self.settings.resample_interval
            )
            result = await self.client.preprocess(
                standardized_path,
                operations,
                context["execution_id"],
                context["input_version_id"],
            )
            processed_path = directory / "processed.gdsx"
            await asyncio.to_thread(processed_path.write_bytes, result.gdsx_content)
            processed = await asyncio.to_thread(_well_data, processed_path, request.well_id)
            context.update(
                {
                    "processed_path": processed_path,
                    "processed_well_name": processed.well.name,
                    "log_req_json": _reference_log_req_json(result.call.data["logReqJson"]),
                }
            )
            qc = StageResult(
                status=StepStatus.WARNING,
                result={
                    "operations": result.call.data["operations"],
                    "standardized_curves": renamed,
                    "processed_curve_count": len(processed.raw_data.curves),
                    "external_call_id": result.call.external_call_id,
                },
                evidence=["已完成本地标准化和公司预处理，并取得原始 logReqJson"],
                warnings=["真实 QC 图表尚未写入 InterpretationState"],
                is_mock=False,
                source=f"company:preprocessing:{result.call.external_call_id}",
            )
            return {
                "qc": qc.model_dump(mode="json"),
                "processed_data": processed.raw_data.model_dump(mode="json"),
                "operations_applied": True,
            }

        if stage == "interpretation":
            if "log_req_json" not in context or "processed_path" not in context:
                raise ToolError("COMPANY_PREPROCESS_REQUIRED", "预测前必须先完成真实预处理")
            service_id, configured_creator = self._prediction_identity()
            models = await self.client.list_models(
                {"name": "", "pageNum": 1, "pageSize": 100, "status": "运行中"}
            )
            model_ids = {
                str(item.get("id") or item.get("service_id"))
                for item in models
                if isinstance(item, dict) and (item.get("id") or item.get("service_id"))
            }
            if service_id not in model_ids:
                raise ToolError("COMPANY_MODEL_NOT_RUNNING", "配置的预测模型当前不在运行列表")
            requested_creator = request.parameters.get("create_people")
            create_people = (
                requested_creator.strip()
                if isinstance(requested_creator, str) and requested_creator.strip()
                else configured_creator
            )
            call = await self.client.predict(
                {
                    "encodingUrl": "",
                    "wellName": context["processed_well_name"],
                    "serviceId": service_id,
                    "taskConfig": deepcopy(PREDICTION_TASK_CONFIG),
                    "batchSize": self.settings.batch_size,
                    "createPeople": create_people,
                    "logReqJson": context["log_req_json"],
                },
                context["execution_id"],
                context["input_version_id"],
            )
            final_path = self.output_dir / "company" / context["execution_id"] / "final.gdsx"
            postprocess, charts = await asyncio.to_thread(
                _postprocess_prediction, context["processed_path"], final_path, call
            )
            context.update(
                {"prediction": call, "final_path": final_path, "postprocess": postprocess, "charts": charts}
            )
            projection = project_prediction(call)
            results: JsonObject = {}
            mapping = {
                "lithology": StepId.W04,
                "petrophysics": StepId.W05,
                "sw": StepId.W06,
                "classification": StepId.W07,
                "intervals": StepId.W08,
            }
            for key, step in mapping.items():
                result = projection.steps.get(step)
                if result is None or result.status == StepStatus.BLOCKED:
                    continue
                result.status = StepStatus.WARNING
                result.evidence.append("预测结果已写入本次 Execution 的最终 GDSX")
                results[key] = result.model_dump(mode="json")
            if "sw" in results:
                results["fluid"] = deepcopy(results["sw"])
            results["validation"] = real_prediction_validation_result(
                projection.steps,
                final_path=final_path,
                external_call_id=call.external_call_id,
            ).model_dump(mode="json")
            return results

        if stage == "validation":
            final_path = context.get("final_path")
            if not isinstance(final_path, Path) or not final_path.is_file():
                raise ToolError("COMPANY_FINAL_GDSX_REQUIRED", "获取解释结论前必须生成最终 GDSX")
            uploaded = await self.client.upload_report_gdsx(final_path)
            conclusions = await self.client.get_gdsx_conclusions(str(uploaded["upload_id"]))
            context.update({"final_upload": uploaded, "conclusions": conclusions})
            return {
                "validation": ValidationResult(
                    status=StepStatus.SUCCESS,
                    result={
                        "interpretation_conclusions": conclusions,
                        "final_gdsx_upload": uploaded,
                    },
                    evidence=["已由最终 GDSX 调用正式解释结论接口"],
                    validation_status=ValidationStatus.CONSISTENT,
                    is_mock=False,
                    source="company:real:gdsx-interresult",
                ).model_dump(mode="json")
            }

        if stage == "report":
            uploaded = context.get("final_upload")
            conclusions = context.get("conclusions")
            if not isinstance(uploaded, dict) or not isinstance(conclusions, list):
                raise ToolError("COMPANY_INTERRESULT_REQUIRED", "生成报告前必须取得真实解释结论")
            if not self.settings.report_type or not self.settings.report_gdsx_type:
                raise ToolError("COMPANY_REPORT_CONFIG_MISSING", "未配置正式报告类型或 GDSX 类型")
            conclusion_ids: list[str] = []
            for index, item in enumerate(conclusions):
                if not isinstance(item, dict):
                    continue
                identifier = next(
                    (
                        str(item[key]).strip()
                        for key in ("id", "result_id", "value", "layer", "formation", "interval", "name")
                        if item.get(key) is not None and str(item[key]).strip()
                    ),
                    "",
                )
                if not identifier:
                    raise ToolError(
                        "COMPANY_INTERRESULT_ID_MISSING",
                        f"第 {index + 1} 条真实解释结论缺少报告选择标识",
                    )
                conclusion_ids.append(identifier)
            if not conclusion_ids:
                raise ToolError("COMPANY_INTERRESULT_EMPTY", "没有可用于正式报告的解释结论")
            report = await self.client.generate_report(
                {
                    "zone": self.settings.report_zone,
                    "report_type": self.settings.report_type,
                    "well_name": context.get("processed_well_name") or request.well_id,
                    "data_source": 2,
                    "msg": f"请生成【{context.get('processed_well_name') or request.well_id}】井的{self.settings.report_type}",
                    "author_info": {},
                    "extra_request_parameter": {
                        "well_id": request.well_id,
                        "logIAS_log_names": [],
                        "gdsx_config": [
                            {
                                "gdsx_name": uploaded.get("filename") or "final.gdsx",
                                "gdsx_upload_id": uploaded["upload_id"],
                                "gdsx_size": uploaded.get("size", 0),
                                "gdsx_types": [self.settings.report_gdsx_type],
                            }
                        ],
                        "interresult_type_list": conclusion_ids,
                        "appendix_files": [],
                        "draw": {"customer": 0, "templates": []},
                    },
                }
            )
            return {
                "final_check": StageResult(
                    status=StepStatus.SUCCESS,
                    result={
                        "company_report_api_connected": True,
                        "report_task": report,
                        "final_gdsx_upload": uploaded,
                        "chart_count": len(context.get("charts", [])),
                    },
                    evidence=["解释报告由正式异步报告接口生成"],
                    is_mock=False,
                    source="company:real:report-generation",
                ).model_dump(mode="json")
            }
        raise ToolError("COMPANY_STAGE_UNSUPPORTED", "未知公司处理步骤")

    async def close(self) -> None:
        """释放底层 HTTP 客户端。"""

        await self.client.aclose()


def real_prediction_validation_result(
    steps: dict[StepId, StageResult], *, final_path: Path, external_call_id: str
) -> ValidationResult:
    """校验真实预测链路的结果完整性；不冒充尚未提供的外部地质证据。"""

    expected = (StepId.W04, StepId.W05, StepId.W06, StepId.W07, StepId.W08)
    missing = [step.value for step in expected if step not in steps]
    invalid = [
        step.value
        for step in expected
        if step in steps
        and steps[step].status not in {StepStatus.SUCCESS, StepStatus.WARNING}
    ]
    final_available = final_path.is_file()
    if missing or invalid or not final_available:
        return ValidationResult(
            status=StepStatus.REVIEW_REQUIRED,
            result={
                "prediction_result_complete": False,
                "missing_prediction_steps": missing,
                "invalid_prediction_steps": invalid,
                "final_gdsx_available": final_available,
            },
            conflicts=["真实预测结果未通过完整性校验"],
            missing_evidence=[f"缺少预测步骤：{', '.join(missing)}"] if missing else [],
            recommended_action="inspect_prediction_result",
            is_mock=False,
            source=f"company:validation:{external_call_id}",
            validation_status=ValidationStatus.INSUFFICIENT_EVIDENCE,
        )
    return ValidationResult(
        status=StepStatus.WARNING,
        result={
            "prediction_result_complete": True,
            "validated_prediction_steps": [step.value for step in expected],
            "final_gdsx_available": True,
        },
        evidence=[
            "W04-W08 均来自本次真实公司预测响应",
            "真实预测结果已完成确定性后处理并写入本次 Execution 的最终 GDSX",
        ],
        missing_evidence=["本次未提供岩心、录井、试油或邻井等独立验证资料"],
        warnings=["当前仅完成预测结果完整性验证，未执行独立多源地质验证"],
        recommended_action="review_when_external_evidence_available",
        is_mock=False,
        source=f"company:validation:{external_call_id}",
        validation_status=ValidationStatus.CONSISTENT,
    )
