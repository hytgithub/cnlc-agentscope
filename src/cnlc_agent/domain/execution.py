"""持续解释任务与单次执行的最小版本化契约。"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.models import Contract, WellId, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState

ExecutionTrigger = Literal["INITIAL", "RERUN"]
PlanningReason = Literal[
    "INITIAL", "FULL_RERUN", "NO_REUSABLE_SOURCE", "INPUT_CHANGED",
    "OVERRIDE_CHANGED", "REPORT_ONLY",
    "LEGACY_MIGRATED",
]


class ExecutionStatus(StrEnum):
    """后台执行生命周期，与 W01-W10 的 StepStatus 明确分离。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


TERMINAL_EXECUTION_STATUSES = {
    ExecutionStatus.SUCCESS,
    ExecutionStatus.WARNING,
    ExecutionStatus.FAILED,
    ExecutionStatus.BLOCKED,
    ExecutionStatus.REVIEW_REQUIRED,
}


def execution_status_from_state(status: StepStatus) -> ExecutionStatus:
    """在唯一边界把 Workflow 终态映射为 Execution 终态。"""

    mapping = {
        StepStatus.SUCCESS: ExecutionStatus.SUCCESS,
        StepStatus.WARNING: ExecutionStatus.WARNING,
        StepStatus.FAILED: ExecutionStatus.FAILED,
        StepStatus.BLOCKED: ExecutionStatus.BLOCKED,
        StepStatus.REVIEW_REQUIRED: ExecutionStatus.REVIEW_REQUIRED,
    }
    if status not in mapping:
        raise ValueError("workflow state is not terminal")
    return mapping[status]


class InterpretationTask(Contract):
    """一口井的持续任务；指针指向已创建的执行版本。"""

    task_id: str
    well_id: WellId
    current_execution_id: str | None = None
    current_input_version_id: str | None = Field(default=None, min_length=1)
    latest_successful_execution_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Execution(Contract):
    """一次完整或局部重跑及其独立状态快照、复用来源和报告。"""

    execution_id: str
    task_id: str
    sequence: int = Field(ge=1)
    status: ExecutionStatus
    state_snapshot: InterpretationState
    markdown: str = ""
    trigger_type: ExecutionTrigger
    input_version_id: str | None = Field(default=None, min_length=1)
    override_snapshot: InterpretationOverride = Field(default_factory=InterpretationOverride)
    start_step: StepId | None = StepId.W01
    source_execution_id: str | None = Field(default=None, min_length=1)
    planning_reason: PlanningReason = "INITIAL"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    lease_owner: str | None = Field(default=None, min_length=1, max_length=128)
    lease_expires_at: datetime | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_identity(self) -> "Execution":
        """执行 ID 与状态快照中的 Workflow ID 始终一一对应。"""

        if self.execution_id != self.state_snapshot.workflow_execution_id:
            raise ValueError("execution_id must match workflow_execution_id")
        if self.task_id != self.state_snapshot.task.task_id:
            raise ValueError("task_id must match state task_id")
        if self.start_step not in {StepId.W01, StepId.W02, StepId.W04, None}:
            raise ValueError("execution start_step must be a stage boundary")
        if self.status == ExecutionStatus.QUEUED and any(
            value is not None
            for value in (
                self.started_at, self.finished_at, self.lease_owner, self.lease_expires_at
            )
        ):
            raise ValueError("queued execution cannot have running fields")
        if self.status == ExecutionStatus.RUNNING and (
            self.started_at is None or self.lease_owner is None or self.lease_expires_at is None
        ):
            raise ValueError("running execution requires an active lease")
        if self.status in TERMINAL_EXECUTION_STATUSES and self.finished_at is None:
            raise ValueError("terminal execution requires finished_at")
        return self
