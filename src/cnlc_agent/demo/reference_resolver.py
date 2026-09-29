"""Operation 的只读任务与版本解析；授权来自 Repository Binding，不信任语义中的 ID。"""

from enum import StrEnum

from pydantic import ConfigDict, Field

from cnlc_agent.application.ports import TaskRepository
from cnlc_agent.demo.operation_models import ExecutionReference, ExecutionReferenceKind
from cnlc_agent.demo.task_context import SessionTaskResolver, SessionTaskSummary, TaskReference
from cnlc_agent.domain.errors import DataError
from cnlc_agent.domain.execution import Execution, ExecutionStatus, InterpretationTask
from cnlc_agent.domain.models import Contract
from cnlc_agent.domain.session_binding import TaskSessionIdentity


class ReferenceAccessMode(StrEnum):
    """引用的使用目的；写入目标不能沿用只读查询的歧义回退。"""

    READ_ONLY = "READ_ONLY"
    WRITE = "WRITE"


class TaskResolutionSource(StrEnum):
    """说明任务选择依据，便于后续交互解释兼容回退。"""

    ACTIVE = "ACTIVE"
    ONLY_TASK = "ONLY_TASK"
    LATEST_BOUND = "LATEST_BOUND"
    EXPLICIT_WELL = "EXPLICIT_WELL"
    EXPLICIT_TASK = "EXPLICIT_TASK"
    PREVIOUS_BOUND = "PREVIOUS_BOUND"


class ResolvedTaskReference(Contract):
    """可信任务的最小读模型；不携带 State 或数据库实体。"""

    model_config = ConfigDict(frozen=True)

    task_id: str
    well_id: str
    well_name: str | None = None
    current_execution_id: str | None
    latest_successful_execution_id: str | None
    resolution_source: TaskResolutionSource
    match_count: int = Field(ge=1)
    anchor_task_id: str | None = None


class ResolvedExecutionReference(Contract):
    """版本解析的最小结果；明确上一版所相对的 anchor。"""

    model_config = ConfigDict(frozen=True)

    task_id: str
    execution_id: str
    sequence: int = Field(ge=1)
    status: ExecutionStatus
    source_execution_id: str | None
    resolution_source: ExecutionReferenceKind
    anchor_execution_id: str | None = None


class OperationReferenceResolver:
    """新的安全解析入口；不改变 SessionTaskResolver 的旧行为或保存会话焦点。

    session_identity 必须由服务端提供；每次查询重新核验持久绑定。
    返回值仅反映查询时事实，不替代未来写命令的并发和授权检查。
    """

    def __init__(self, repository: TaskRepository, session_identity: TaskSessionIdentity) -> None:
        self.repository = repository
        self.session_identity = session_identity.model_copy(deep=True)

    async def resolve_task(
        self,
        reference: TaskReference,
        access_mode: ReferenceAccessMode,
        *,
        active_task_id: str | None = None,
    ) -> ResolvedTaskReference:
        """按 Binding 稳定顺序选井；未知 active 与焦点丢失具有相同安全语义。"""

        reference = TaskReference.model_validate(reference.model_dump())
        access_mode = ReferenceAccessMode(access_mode)
        task_ids = await self.repository.list_session_task_ids(self.session_identity)
        items = await SessionTaskResolver(self.repository, task_ids).summaries()
        if not items:
            raise DataError("TASK_NOT_FOUND", "当前会话没有可用任务")
        active = next((item for item in items if item.task_id == active_task_id), None)
        source: TaskResolutionSource
        anchor_id = None
        match_count = 1
        if reference.kind == "TASK_ID":
            selected = next((item for item in items if item.task_id == reference.value), None)
            if selected is None:
                raise DataError("TASK_NOT_FOUND", "当前会话没有可用的指定任务")
            source = TaskResolutionSource.EXPLICIT_TASK
        elif reference.kind == "WELL_ID":
            matches = [item for item in items if item.well_id == reference.value]
            match_count = len(matches)
            if not matches:
                raise DataError("SESSION_WELL_NOT_FOUND", "当前会话没有指定井的任务")
            if len(matches) == 1:
                selected, source = matches[0], TaskResolutionSource.EXPLICIT_WELL
            elif access_mode == ReferenceAccessMode.READ_ONLY:
                selected, source = matches[-1], TaskResolutionSource.LATEST_BOUND
            elif active is not None and active in matches:
                selected, source = active, TaskResolutionSource.ACTIVE
            else:
                raise DataError("AMBIGUOUS_TASK_REFERENCE", "同井有多个任务，请明确写入目标")
        else:
            selected, source = self._current_anchor(items, active, access_mode)
            if reference.kind == "PREVIOUS_TASK":
                anchor_id = selected.task_id
                index = items.index(selected)
                if index == 0:
                    raise DataError("PREVIOUS_TASK_NOT_FOUND", "当前会话没有上一绑定任务")
                selected, source = items[index - 1], TaskResolutionSource.PREVIOUS_BOUND
        # Summary 是读模型，返回前再次核验归属并读取最新指针，避免把旧投影当授权。
        task = await self._authorized_task(selected.task_id)
        return ResolvedTaskReference(
            task_id=task.task_id,
            well_id=task.well_id,
            well_name=selected.well_name or task.well_id,
            current_execution_id=task.current_execution_id,
            latest_successful_execution_id=task.latest_successful_execution_id,
            resolution_source=source,
            match_count=match_count,
            anchor_task_id=anchor_id,
        )

    @staticmethod
    def _current_anchor(
        items: list[SessionTaskSummary],
        active: SessionTaskSummary | None,
        mode: ReferenceAccessMode,
    ) -> tuple[SessionTaskSummary, TaskResolutionSource]:
        if active is not None:
            return active, TaskResolutionSource.ACTIVE
        if len(items) == 1:
            return items[0], TaskResolutionSource.ONLY_TASK
        if mode == ReferenceAccessMode.WRITE:
            raise DataError("AMBIGUOUS_TASK_REFERENCE", "有多个任务且当前焦点失效，请明确目标")
        return items[-1], TaskResolutionSource.LATEST_BOUND

    async def _authorized_task(self, task_id: str) -> InterpretationTask:
        if not await self.repository.task_belongs_to_session(self.session_identity, task_id):
            raise DataError("TASK_NOT_FOUND", "当前会话没有可用的指定任务")
        task = await self.repository.get_task(task_id)
        if task is None or task.task_id != task_id:
            raise DataError("TASK_NOT_FOUND", "当前会话没有可用的指定任务")
        return task

    async def _execution(
        self, task_id: str, execution_id: str | None, *, stale: bool = False
    ) -> Execution:
        execution = await self.repository.get_execution(execution_id) if execution_id else None
        if (
            execution is None
            or execution.task_id != task_id
            or execution.execution_id != execution_id
        ):
            if stale:
                raise DataError("STALE_CONTEXT_REFERENCE", "工作基线失效或不属于当前任务")
            raise DataError("EXECUTION_NOT_FOUND", "当前任务没有可用的指定执行版本")
        return execution

    async def load_execution(self, task_id: str, execution_id: str) -> Execution:
        """供 ScopeResolver 内部读取完整快照；先验归属，不作为 ReAct 的输出模型。"""

        await self._authorized_task(task_id)
        return await self._execution(task_id, execution_id)

    async def resolve_execution(
        self,
        task_id: str,
        reference: ExecutionReference,
        *,
        active_base_execution_id: str | None = None,
        previous_anchor_execution_id: str | None = None,
    ) -> ResolvedExecutionReference:
        """重新读取任务指针；PREVIOUS 的查看锚点与写工作基线保持独立。"""

        reference = ExecutionReference.model_validate(reference.model_dump())
        task = await self._authorized_task(task_id)
        kind = reference.kind
        anchor_id = None
        if kind == ExecutionReferenceKind.TASK_CURRENT:
            selected = await self._execution(task_id, task.current_execution_id)
        elif kind == ExecutionReferenceKind.ACTIVE_BASE:
            selected = await self._execution(task_id, active_base_execution_id, stale=True)
        elif kind == ExecutionReferenceKind.LATEST_SUCCESSFUL:
            selected = await self._execution(task_id, task.latest_successful_execution_id)
        elif kind == ExecutionReferenceKind.EXECUTION_ID:
            selected = await self._execution(task_id, reference.execution_id)
        else:
            anchor = None
            if kind == ExecutionReferenceKind.PREVIOUS:
                anchor_id = (
                    previous_anchor_execution_id
                    if previous_anchor_execution_id is not None
                    else active_base_execution_id
                )
                has_context_anchor = anchor_id is not None
                anchor = await self._execution(
                    task_id,
                    anchor_id if has_context_anchor else task.current_execution_id,
                    stale=has_context_anchor,
                )
                anchor_id = anchor.execution_id
            executions = await self.repository.list_executions(task_id)
            # Repository 端口应按 task 过滤；即使适配器出错也不能返回跨任务版本。
            if any(item.task_id != task_id for item in executions):
                raise DataError("EXECUTION_NOT_FOUND", "执行列表包含不属于当前任务的版本")
            if kind == ExecutionReferenceKind.FIRST:
                sequence = min((item.sequence for item in executions), default=None)
            elif kind == ExecutionReferenceKind.PREVIOUS:
                assert anchor is not None
                sequence = max(
                    (item.sequence for item in executions if item.sequence < anchor.sequence),
                    default=None,
                )
            else:
                sequence = reference.sequence
            matches = [item for item in executions if item.sequence == sequence]
            if not matches:
                raise DataError("EXECUTION_NOT_FOUND", "当前任务没有符合条件的执行版本")
            if len(matches) != 1:
                raise DataError("AMBIGUOUS_EXECUTION_REFERENCE", "执行序号重复，无法唯一选择版本")
            selected = matches[0]
        return ResolvedExecutionReference(
            task_id=task_id,
            execution_id=selected.execution_id,
            sequence=selected.sequence,
            status=selected.status,
            source_execution_id=selected.source_execution_id,
            resolution_source=kind,
            anchor_execution_id=anchor_id,
        )
