"""持续解释任务与单次执行的最小版本化契约。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.models import Contract, WellId, utc_now
from cnlc_agent.domain.state import InterpretationState

ExecutionTrigger = Literal["INITIAL", "RERUN"]


class InterpretationTask(Contract):
    """一口井的持续任务；指针指向已创建的执行版本。"""

    task_id: str
    well_id: WellId
    current_execution_id: str | None = None
    latest_successful_execution_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Execution(Contract):
    """一次完整 Workflow 运行及其独立快照、报告。"""

    execution_id: str
    task_id: str
    sequence: int = Field(ge=1)
    status: StepStatus
    state_snapshot: InterpretationState
    markdown: str = ""
    trigger_type: ExecutionTrigger
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_identity(self) -> "Execution":
        """执行 ID 与状态快照中的 Workflow ID 始终一一对应。"""

        if self.execution_id != self.state_snapshot.workflow_execution_id:
            raise ValueError("execution_id must match workflow_execution_id")
        if self.task_id != self.state_snapshot.task.task_id:
            raise ValueError("task_id must match state task_id")
        return self
