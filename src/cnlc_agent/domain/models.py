"""当前阶段最小业务契约；正式井资料 Schema 尚待最终确认。

Mock 输出只能来自显式 Fixture，不得根据自行补造的阈值推断。
"""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from cnlc_agent.domain.enums import StepId, StepStatus, ValidationStatus

WellId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
JsonObject = dict[str, JsonValue]


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，供状态和追踪记录统一使用。"""

    return datetime.now(UTC)


class Contract(BaseModel):
    """所有业务契约的严格基类，禁止额外字段、无穷值和赋值后失校验。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class TaskRequest(Contract):
    """启动一次单井解释任务所需的最小请求。"""

    task_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    well_id: WellId
    instruction: str = Field(default="执行单井测井解释骨架演示", min_length=1)


class Well(Contract):
    """井基础信息；未冻结字段放入 extensions，避免提前扩张 Schema。"""

    well_id: WellId
    name: str
    extensions: JsonObject = Field(default_factory=dict)


class LogCurve(Contract):
    """一条测井曲线及其单位和逐深度采样值。"""

    unit: str = Field(min_length=1)
    values: list[float | None]


class RawData(Contract):
    """统一深度轴上的原始测井曲线和辅助资料。"""

    depth_unit: Literal["m"] = "m"
    depth_reference: Literal["MD"] = "MD"
    depths: list[float]
    curves: dict[str, LogCurve]
    auxiliary: JsonObject = Field(default_factory=dict)
    extensions: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_samples(self) -> Self:
        """保证深度严格递增，且所有曲线与统一深度轴对齐。"""

        if any(b <= a for a, b in zip(self.depths, self.depths[1:], strict=False)):
            raise ValueError("depths must be strictly increasing")
        if any(len(curve.values) != len(self.depths) for curve in self.curves.values()):
            raise ValueError("curve samples must align with depths")
        return self


class ErrorDetail(Contract):
    """可持久化、可展示的结构化错误。"""

    code: str
    message: str
    retryable: bool = False
    step_id: StepId | None = None


class MissingData(Contract):
    """缺失资料及其重要级别和影响步骤。"""

    field: str
    importance: Literal["Required", "Recommended", "Optional"]
    affected_step: StepId


class StageResult(Contract):
    """各专业阶段共享的结构化结果外壳。"""

    status: Literal[
        StepStatus.SUCCESS,
        StepStatus.WARNING,
        StepStatus.BLOCKED,
        StepStatus.FAILED,
        StepStatus.REVIEW_REQUIRED,
    ] = StepStatus.SUCCESS
    result: JsonObject = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    recommended_action: str = "continue"
    is_mock: bool
    source: str = Field(min_length=1)


class ValidationResult(StageResult):
    """W09 多源验证结果，可提出回退目标但不能直接改写解释结论。"""

    validation_status: ValidationStatus
    rollback_target: Literal[StepId.W06, StepId.W07] | None = None


class DataRequirements(Contract):
    """临时输入资料契约，不代表正式专业解释标准。"""

    required_curves: list[str] = Field(min_length=1)
    recommended_sources: list[str] = Field(default_factory=list)


class WellData(Contract):
    """井信息、原始数据和资料要求的统一输入对象。"""

    well: Well
    raw_data: RawData
    requirements: DataRequirements


class MockFixture(WellData):
    """Demo Fixture，要求所有预设专业结果明确标记 ``is_mock=true``。"""

    schema_version: Literal["0.1-skeleton"] = "0.1-skeleton"
    is_mock: Literal[True] = True
    outputs: dict[str, StageResult]
    validation: ValidationResult

    @model_validator(mode="after")
    def only_mock_results(self) -> Self:
        """阻止未标记 Mock 的专业结论混入演示资料。"""

        if not self.validation.is_mock or any(not item.is_mock for item in self.outputs.values()):
            raise ValueError("fixture outputs must be explicitly marked is_mock=true")
        return self
