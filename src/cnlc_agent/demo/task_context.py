"""同一 AgentScope Session 内的多井任务引用解析。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from cnlc_agent.application.ports import TaskRepository
from cnlc_agent.domain.errors import ApplicationError
from cnlc_agent.domain.models import Contract

TaskReferenceKind = Literal["CURRENT", "PREVIOUS_TASK", "WELL_ID", "TASK_ID"]


class TaskReference(Contract):
    """用户语义中的任务引用；不把 LLM 文本直接当作任务授权。"""

    kind: TaskReferenceKind = "CURRENT"
    value: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_value(self) -> "TaskReference":
        """只有按井号或任务号解析时允许携带值。"""

        needs_value = self.kind in {"WELL_ID", "TASK_ID"}
        if needs_value != (self.value is not None):
            raise ValueError(f"{self.kind} task reference has invalid value")
        return self


class SessionTaskSummary(Contract):
    """仅用于会话任务解析的读模型，不是新的数据库实体。"""

    task_id: str
    well_id: str
    created_at: datetime
    current_execution_id: str | None = None
    latest_successful_execution_id: str | None = None


class SessionTaskResolver:
    """基于 SessionTaskBinding 的稳定顺序解析当前、上一口井或指定井。"""

    def __init__(self, repository: TaskRepository, task_ids: list[str]) -> None:
        self.repository = repository
        # Repository 已按 binding created_at + task_id 稳定排序。
        self.task_ids = list(dict.fromkeys(task_ids))

    async def summaries(self) -> list[SessionTaskSummary]:
        """从已授权的 task_id 读取井号和执行指针。"""

        summaries: list[SessionTaskSummary] = []
        for task_id in self.task_ids:
            task = await self.repository.get_task(task_id)
            if task is None:
                continue
            summaries.append(
                SessionTaskSummary(
                    task_id=task.task_id,
                    well_id=task.well_id,
                    created_at=task.created_at,
                    current_execution_id=task.current_execution_id,
                    latest_successful_execution_id=task.latest_successful_execution_id,
                )
            )
        return summaries

    async def resolve(
        self,
        reference: TaskReference,
        active_task_id: str | None,
    ) -> SessionTaskSummary:
        """解析引用；任务必须先出现在当前 Session 的绑定列表中。"""

        items = await self.summaries()
        if not items:
            raise ApplicationError("TASK_NOT_FOUND", "当前会话尚无解释任务")
        by_id = {item.task_id: item for item in items}
        current = by_id.get(active_task_id or "") or items[-1]
        if reference.kind == "CURRENT":
            return current
        if reference.kind == "PREVIOUS_TASK":
            index = items.index(current)
            if index == 0:
                raise ApplicationError(
                    "PREVIOUS_TASK_NOT_FOUND", "当前会话没有上一口井的解释任务"
                )
            return items[index - 1]
        if reference.kind == "WELL_ID":
            matches = [item for item in items if item.well_id == reference.value]
            if not matches:
                raise ApplicationError(
                    "SESSION_WELL_NOT_FOUND",
                    f"当前会话中没有 {reference.value} 的解释任务。",
                )
            # 同井重复 Task 时选绑定顺序中最近创建的一个。
            return matches[-1]
        selected = by_id.get(reference.value or "")
        if selected is None:
            raise ApplicationError("TASK_NOT_FOUND", "任务不属于当前会话")
        return selected
