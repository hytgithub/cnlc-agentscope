"""一次真实专业 Tool 调用的可查询审计记录，不记录复用步骤或模型请求。"""

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import Field, model_validator

from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.models import Contract, JsonObject, utc_now


class ToolRunStatus(StrEnum):
    """工具调用生命周期；与 ExecutionPlan 的 RUN/REUSE 分开。"""

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    FAILED = "FAILED"


class ToolExecutionMode(StrEnum):
    """调用的物理来源类型，由具体 Tool 显式声明。"""

    MOCK = "MOCK"
    REAL = "REAL"
    VIRTUAL = "VIRTUAL"
    DERIVED = "DERIVED"


class ToolRun(Contract):
    """隶属于单次 Execution 的 Tool 调用；终态只允许在仓库中写入一次。"""

    tool_run_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    task_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    step_id: StepId
    tool_code: str = Field(min_length=1, max_length=128)
    status: ToolRunStatus = ToolRunStatus.RUNNING
    execution_mode: ToolExecutionMode
    source: str = Field(min_length=1, max_length=256)
    input_snapshot: JsonObject = Field(default_factory=dict)
    output_snapshot: JsonObject = Field(default_factory=dict)
    source_external_call_id: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ToolRun":
        """运行态不能伪装已完成，终态必须有完成时间。"""

        if self.status == ToolRunStatus.RUNNING:
            if self.finished_at is not None or self.error_code is not None:
                raise ValueError("running ToolRun cannot have terminal fields")
        elif self.finished_at is None:
            raise ValueError("terminal ToolRun requires finished_at")
        if self.status == ToolRunStatus.FAILED and not self.error_code:
            raise ValueError("failed ToolRun requires error_code")
        if self.status in {ToolRunStatus.SUCCESS, ToolRunStatus.WARNING} and self.error_code:
            raise ValueError("successful ToolRun cannot carry error_code")
        return self
