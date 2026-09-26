"""基于 Fixture 的 Tool 仍遵循正式契约；此处不包含任何地质计算公式。"""

from cnlc_agent.application.ports import WellRepository
from cnlc_agent.application.prediction import MockPredictionProvider, PredictionProvider
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import ToolError
from cnlc_agent.domain.models import JsonObject
from cnlc_agent.domain.override import ExecutionContext
from cnlc_agent.domain.tool_run import ToolExecutionMode
from cnlc_agent.infrastructure.mock import FixtureRepository
from cnlc_agent.tools.contracts import ToolInput, ToolOutput


class GetWellDataTool:
    """W01 井资料加载 Tool 的 Mock 实现。"""

    name = "get_well_data"
    execution_mode = ToolExecutionMode.MOCK
    source = "mock:fixture"

    def __init__(self, repository: WellRepository) -> None:
        self.repository = repository

    async def execute(self, request: ToolInput) -> ToolOutput:
        """读取 Fixture，并只返回 W01 需要的井资料字段。"""

        fixture = await self.repository.load(request.well_id)
        return ToolOutput(
            status=StepStatus.SUCCESS,
            data={
                "well": fixture.well.model_dump(mode="json"),
                "raw_data": fixture.raw_data.model_dump(mode="json"),
                "requirements": fixture.requirements.model_dump(mode="json"),
            },
            metadata={"is_mock": True, "source": f"fixture:{request.well_id}"},
        )


class MockResultTool:
    """一个实例对应一个命名能力和一个 Fixture 结果键。"""

    execution_mode = ToolExecutionMode.MOCK
    source = "mock:fixture"

    def __init__(
        self,
        name: str,
        result_key: str,
        repository: FixtureRepository,
        prediction: PredictionProvider | None = None,
    ) -> None:
        self.name = name
        self.result_key = result_key
        self.repository = repository
        self.prediction = prediction if prediction is not None else MockPredictionProvider()

    async def execute(self, request: ToolInput) -> ToolOutput:
        """回放预设结果；W05 可投影用户指定值，但不冒充专业复算。"""

        fixture = await self.repository.load(request.well_id)
        result = fixture.outputs.get(self.result_key)
        if result is None:
            raise ToolError("MOCK_TOOL_RESULT_MISSING", f"缺少演示工具结果：{self.result_key}")
        context = ExecutionContext.model_validate(request.parameters)
        metadata = await self.prediction.describe(context)
        # 专业数值实际来自 Fixture，预测上下文不能覆盖结果来源。
        metadata["source"] = self.source
        metadata["fixture_source"] = result.source
        # 只修改返回副本，输入 Fixture 和历史 Execution 始终保持不可变。
        result = result.model_copy(deep=True)
        result.source = f"mock:parameter-aware:{context.execution_id or 'unbound'}"
        if self.result_key == "petrophysics":
            projected = self._project_petrophysics_override(result.result, context)
            if projected:
                markers = {
                    "por": ("por", "孔隙度"),
                    "perm": ("perm", "渗透率"),
                }
                result.evidence = [
                    item
                    for item in result.evidence
                    if not any(
                        marker in item.casefold()
                        for field in projected
                        for marker in markers[field]
                    )
                ]
                result.evidence.extend(
                    self._override_evidence(field, value) for field, value in projected.items()
                )
                retained_warnings = [
                    item
                    for item in result.warnings
                    if not any(
                        marker in item.casefold()
                        for field in projected
                        for marker in markers[field]
                    )
                ]
                if len(retained_warnings) != len(result.warnings):
                    retained_warnings.append(
                        "当前物性值包含用户指定的 Demo Override 投影；"
                        "其他物性仍来自 Fixture，均未经过专业算法复算"
                    )
                result.warnings = retained_warnings
                projected_metadata: JsonObject = {
                    field: value for field, value in projected.items()
                }
                metadata["projected_parameters"] = projected_metadata
        if self.result_key == "qc":
            # 插值、离散曲线和缺失值规则尚未确认，只证明参数到达 W03。
            preprocess: JsonObject = {
                "requested_sampling_interval": context.effective_override.sampling_interval,
                "resampling_applied": False,
                "reason": "professional resampling rules pending",
            }
            result.result["preprocessing"] = preprocess
            metadata["preprocessing"] = preprocess.copy()
        return ToolOutput(
            status=result.status,
            data=result.model_dump(mode="json"),
            warnings=result.warnings,
            metadata=metadata,
        )

    @staticmethod
    def _project_petrophysics_override(
        payload: JsonObject, context: ExecutionContext
    ) -> dict[str, float]:
        """按原结果单位替换显式 POR/PERM；不推导其他物性或饱和度。"""

        projected: dict[str, float] = {}
        for field, result_key in (("por", "porosity"), ("perm", "permeability")):
            value = getattr(context.effective_override, field)
            existing = payload.get(result_key)
            if value is None or not isinstance(existing, dict):
                continue
            measurement = existing.copy()
            measurement["value"] = value
            payload[result_key] = measurement
            projected[field] = value
        return projected

    @staticmethod
    def _override_evidence(field: str, value: float) -> str:
        """记录用户输入的演示投影事实，避免与 Fixture 原值混在同一证据中。"""

        if field == "por":
            return f"用户指定 Demo Override：孔隙度={value:.2%}（未执行专业复算）"
        return f"用户指定 Demo Override：渗透率={value:g}（沿用 Fixture 单位，未执行专业复算）"
