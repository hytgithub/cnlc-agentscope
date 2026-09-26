"""四大步骤的公司能力边界：当前返回既有 Fixture，细分步骤只消费批量结果。"""

import asyncio
import hashlib
import json
from typing import Protocol
from uuid import uuid4

from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import ToolError
from cnlc_agent.domain.models import JsonObject, StageResult
from cnlc_agent.domain.tool_run import ToolExecutionMode
from cnlc_agent.infrastructure.mock import FixtureRepository
from cnlc_agent.tools.contracts import ToolCaller, ToolInput, ToolOutput
from cnlc_agent.tools.mock import MockResultTool


class CompanyBatchProvider(Protocol):
    """将来公司 API 拆分时只替换批量提供者内部实现，保留规范化结果键。"""

    async def execute(self, stage: str, request: ToolInput) -> JsonObject: ...


class MockCompanyBatchProvider:
    """直接返回本项目现有 Mock 资料，不发网络请求、不伪造真实服务响应协议。"""

    def __init__(self, repository: FixtureRepository) -> None:
        self.repository = repository

    async def execute(self, stage: str, request: ToolInput) -> JsonObject:
        """四个分支分别对应数据分析、预处理、智能处理和报告数据准备。"""
        fixture = await self.repository.load(request.well_id)
        if stage == "analysis":
            return {
                "well_data": {
                    "well": fixture.well.model_dump(mode="json"),
                    "raw_data": fixture.raw_data.model_dump(mode="json"),
                    "requirements": fixture.requirements.model_dump(mode="json"),
                }
            }
        if stage == "preprocessing":
            if "qc" not in fixture.outputs:
                raise ToolError("COMPANY_MOCK_RESULT_MISSING", "Mock 资料缺少质量结果")
            return {
                "qc": fixture.outputs["qc"].model_dump(mode="json"),
                # 当前 Fixture 没有独立的处理后曲线：仅回传原数据并明确标记未转换。
                "processed_data": fixture.raw_data.model_dump(mode="json"),
                "operations_applied": False,
            }
        if stage == "interpretation":
            results: JsonObject = {}
            for key in ("lithology", "petrophysics", "sw", "fluid", "classification", "intervals"):
                if key not in fixture.outputs:
                    # 缺失某项不抹去其他项；相应细分步骤取结果时单独失败。
                    continue
                # 复用既有受控参数投影；仍然是 Mock，不宣称专业重算。
                output = await MockResultTool(key, key, self.repository).execute(request)
                results[key] = output.data
            results["validation"] = fixture.validation.model_dump(mode="json")
            return results
        if stage == "report":
            return {
                "final_check": StageResult(
                    is_mock=True,
                    source="mock:company:report",
                    result={"report_renderer": "local", "company_report_api_connected": False},
                    evidence=["报告继续由本项目模板依据当前状态生成；未调用公司报告 API"],
                ).model_dump(mode="json")
            }
        raise ToolError("COMPANY_STAGE_UNSUPPORTED", "未知公司处理步骤")


class _BatchTool:
    """一个实际的 Mock 大步骤调用，独立记录来源和共享调用 ID。"""

    execution_mode = ToolExecutionMode.MOCK

    def __init__(self, stage: str, provider: CompanyBatchProvider) -> None:
        self.stage = stage
        self.provider = provider
        self.name = f"company_{stage}"
        self.source = f"mock:company:{stage}"

    async def execute(self, request: ToolInput) -> ToolOutput:
        data = await self.provider.execute(self.stage, request)
        call_id = uuid4().hex
        return ToolOutput(
            status=StepStatus.SUCCESS,
            data=data,
            metadata={
                "is_mock": True,
                "source": f"{self.source}:{call_id}",
                "external_call_id": call_id,
                "stage": self.stage,
            },
        )


class CompanyBatchResults:
    """一次 Execution 内共享批量响应；不同输入或参数不得复用旧结果。"""

    def __init__(self, provider: CompanyBatchProvider, caller: ToolCaller) -> None:
        self.provider = provider
        self.caller = caller
        self._results: dict[tuple[str, str], ToolOutput] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def get(self, stage: str, request: ToolInput) -> ToolOutput:
        """仅缓存成功响应；取消或失败保留异常，由上层决定是否重试。"""
        if not request.parameters.get("execution_id"):
            raise ToolError("COMPANY_CONTEXT_REQUIRED", "批量结果必须绑定当前执行")
        identity = json.dumps(
            {
                "task_id": request.task_id,
                "well_id": request.well_id,
                "parameters": request.parameters,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        key = (stage, hashlib.sha256(identity.encode()).hexdigest())
        async with self._locks.setdefault(key, asyncio.Lock()):
            if key not in self._results:
                self._results[key] = await self.caller.call(
                    _BatchTool(stage, self.provider),
                    request,
                )
            return self._results[key].model_copy(deep=True)


class CompanyResultTool:
    """细分工具读取批量结果，审计标记 DERIVED，不冒充独立公司 API 调用。"""

    execution_mode = ToolExecutionMode.DERIVED
    source = "mock:company:projection"

    def __init__(self, name: str, stage: str, key: str, batches: CompanyBatchResults) -> None:
        self.name, self.stage, self.key, self.batches = name, stage, key, batches

    async def execute(self, request: ToolInput) -> ToolOutput:
        batch = await self.batches.get(self.stage, request)
        value = batch.data.get(self.key)
        if not isinstance(value, dict):
            raise ToolError("COMPANY_STEP_RESULT_MISSING", f"批量响应缺少细分结果：{self.key}")
        metadata = dict(batch.metadata)
        data = dict(value)
        if self.key == "well_data":
            well = data.get("well")
            if isinstance(well, dict):
                raw_extensions = well.get("extensions", {})
                if not isinstance(raw_extensions, dict):
                    raise ToolError("COMPANY_RESULT_INVALID", "井资料扩展信息必须为对象")
                extensions = dict(raw_extensions)
                extensions["company_batch_source"] = metadata
                data["well"] = {**well, "extensions": extensions}
        else:
            data["source"] = metadata["source"]
            data["is_mock"] = True
            raw_result = data.get("result", {})
            if not isinstance(raw_result, dict):
                raise ToolError("COMPANY_RESULT_INVALID", "细分结果必须为对象")
            result = dict(raw_result)
            result["company_batch"] = metadata
            if self.key == "qc":
                result["operations_applied"] = batch.data.get("operations_applied", False)
            data["result"] = result
        # 传输成功与子功能成功分开；由 Workflow 消费 StageResult 的业务状态。
        return ToolOutput(status=StepStatus.SUCCESS, data=data, metadata=metadata)


def build_company_mock_tools(
    repository: FixtureRepository, caller: ToolCaller
) -> dict[str, CompanyResultTool]:
    """集中装配现有内部功能到四个大步骤的映射。"""
    batches = CompanyBatchResults(MockCompanyBatchProvider(repository), caller)
    mapping = {
        "get_well_data": ("analysis", "well_data"),
        "check_curve_quality": ("preprocessing", "qc"),
        "identify_lithology": ("interpretation", "lithology"),
        "evaluate_petrophysics": ("interpretation", "petrophysics"),
        "calculate_sw": ("interpretation", "sw"),
        "identify_fluid": ("interpretation", "fluid"),
        "classify_layer": ("interpretation", "classification"),
        "merge_intervals": ("interpretation", "intervals"),
        "validate_interpretation": ("interpretation", "validation"),
        "prepare_report": ("report", "final_check"),
    }
    return {
        name: CompanyResultTool(name, stage, key, batches) for name, (stage, key) in mapping.items()
    }
