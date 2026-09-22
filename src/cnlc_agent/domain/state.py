"""由 Workflow 独占修改的核心状态，与 AgentScope、SQLAlchemy 和 Redis 解耦。"""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field

from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.models import (
    Contract,
    DataRequirements,
    ErrorDetail,
    JsonObject,
    MissingData,
    RawData,
    StageResult,
    TaskRequest,
    ValidationResult,
    Well,
    utc_now,
)


class StatePatch(Contract):
    """Workflow 节点只能通过这些字段返回状态变更，不能原地修改全局状态。"""

    well: Well | None = None
    raw_data: RawData | None = None
    data_requirements: DataRequirements | None = None
    processed_data: RawData | None = None
    qc_result: StageResult | None = None
    lithology_result: StageResult | None = None
    petrophysics_result: StageResult | None = None
    fluid_result: StageResult | None = None
    layer_classification: StageResult | None = None
    interval_result: StageResult | None = None
    validation_result: ValidationResult | None = None
    final_check: StageResult | None = None


class StepOutcome(Contract):
    """单个节点执行后的终态、状态补丁及诊断信息。"""

    status: StepStatus = StepStatus.SUCCESS
    patch: StatePatch = Field(default_factory=StatePatch)
    missing_data: list[MissingData] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[ErrorDetail] = Field(default_factory=list)
    reason: str


class StateChange(Contract):
    """一次可审计状态变更，记录修改者、原因及前后差异。"""

    actor: str
    step_id: StepId
    timestamp: datetime = Field(default_factory=utc_now)
    reason: str
    before: JsonObject
    after: JsonObject


class StepExecution(Contract):
    """节点单次执行记录，为后续 Retry/Rollback 保留独立标识。"""

    step_execution_id: str = Field(default_factory=lambda: str(uuid4()))
    step_id: StepId
    status: StepStatus = StepStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[ErrorDetail] = Field(default_factory=list)
    retry_count: int = 0


class InterpretationState(StatePatch):
    """单井解释任务唯一可信状态，汇总 W01-W10 的全部阶段结果。"""

    schema_version: Literal["0.1-skeleton"] = "0.1-skeleton"
    mode: Literal["mock", "demo"] = "mock"
    task: TaskRequest
    workflow_execution_id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str = Field(default_factory=lambda: uuid4().hex)
    status: StepStatus = StepStatus.PENDING
    current_step: StepId | None = None
    completed_steps: list[StepId] = Field(default_factory=list)
    executions: list[StepExecution] = Field(default_factory=list)
    changes: list[StateChange] = Field(default_factory=list)
    missing_data: list[MissingData] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[ErrorDetail] = Field(default_factory=list)
    review_required: bool = False
    rollback_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
