"""会话短期交互焦点；引用只是 hint，业务执行仍须经 Resolver 与 Binding 校验。"""

from typing import Any, Self

from pydantic import Field, ValidationError, model_validator

from cnlc_agent.demo.operation_models import NonBlank, OperationScope
from cnlc_agent.domain.models import Contract

INTERACTION_CONTEXT_KEY = "cnlc_interaction_context"
LEGACY_ACTIVE_TASK_KEY = "cnlc_active_task_id"


class ActiveContext(Contract):
    """当前操作上下文；工作基线是交互意图，不等于 Task 的当前版本。"""

    task_id: NonBlank
    base_execution_id: NonBlank | None = None
    scope: OperationScope | None = None

    @model_validator(mode="after")
    def require_scope_base(self) -> Self:
        """范围必须绑定具体版本，避免把旧层段用于另一版本。"""

        if self.scope is not None and self.base_execution_id is None:
            raise ValueError("active scope requires base_execution_id")
        return self


class ViewContext(Contract):
    """当前查看上下文；查看其他任务或历史版本不代表切换写操作焦点。"""

    task_id: NonBlank
    execution_id: NonBlank | None = None
    scope: OperationScope | None = None

    @model_validator(mode="after")
    def require_scope_execution(self) -> Self:
        """局部查看范围同样必须具有版本语境。"""

        if self.scope is not None and self.execution_id is None:
            raise ValueError("view scope requires execution_id")
        return self


class ContextExecutionReference(Contract):
    """近期交互中的具体任务版本引用；不证明版本存在或归属合法。"""

    task_id: NonBlank
    execution_id: NonBlank
    scope: OperationScope | None = None


class CompareContext(Contract):
    """最近比较的左右引用；不执行比较或保存比较结果。"""

    left: ContextExecutionReference
    right: ContextExecutionReference


class SelectedIntervalReference(Contract):
    """选中层段的版本限定引用；禁止仅用层号跨版本复用。"""

    task_id: NonBlank
    execution_id: NonBlank
    interval_id: NonBlank


class RecentContext(Contract):
    """近期交互上下文，只辅助未来指代解析，不承担授权或业务事实存储。"""

    last_operation_id: NonBlank | None = None
    last_compare: CompareContext | None = None
    last_scenario_id: NonBlank | None = None
    selected_intervals: list[SelectedIntervalReference] = Field(default_factory=list)


class InteractionContext(Contract):
    """完整短期交互上下文；仅随活跃 Session cache 保存。"""

    active: ActiveContext | None = None
    view: ViewContext | None = None
    recent: RecentContext = Field(default_factory=RecentContext)


class InteractionContextStore:
    """在 middle_context 中维护 JSON 表示与兼容 key，不访问 Repository。"""

    def __init__(self, context: dict[str, Any]) -> None:
        self._context = context
        self._state = InteractionContext()
        self.attach(context)

    @property
    def snapshot(self) -> InteractionContext:
        """返回隔离副本，调用方必须通过明确 API 更新运行态。"""

        return self._state.model_copy(deep=True)

    @property
    def active_task_id(self) -> str | None:
        """旧 runner 的兼容焦点，始终来自同一份 ActiveContext。"""

        return self._state.active.task_id if self._state.active is not None else None

    def attach(self, context: dict[str, Any]) -> None:
        """新格式优先；损坏或缺失时只恢复合法 legacy hint，绝不沿用旧短期状态。"""

        self._context = context
        try:
            state = InteractionContext.model_validate(context.get(INTERACTION_CONTEXT_KEY))
        except ValidationError:
            state = InteractionContext()
            try:
                state.active = ActiveContext.model_validate(
                    {"task_id": context.get(LEGACY_ACTIVE_TASK_KEY)}
                )
            except ValidationError:
                # 外部 Session 状态损坏不应阻断初始化，也不能据此恢复写范围。
                state.active = None
        self._save(state)

    def _save(self, state: InteractionContext) -> None:
        # 重新验证并隔离嵌套模型，防止调用方持有的可变对象绕过状态边界。
        validated = InteractionContext.model_validate(state.model_dump(mode="json"))
        self._context[INTERACTION_CONTEXT_KEY] = validated.model_dump(mode="json")
        if validated.active is None:
            self._context.pop(LEGACY_ACTIVE_TASK_KEY, None)
        else:
            self._context[LEGACY_ACTIVE_TASK_KEY] = validated.active.task_id
        self._state = validated

    def set_active_task(self, task_id: str) -> None:
        """同井保留工作基线；切井时丢弃旧版本和范围，保留独立的查看与近期引用。"""

        state = self.snapshot
        if state.active is None or state.active.task_id != task_id:
            state.active = ActiveContext(task_id=task_id)
        self._save(state)

    def set_active_base(
        self, task_id: str, execution_id: str, scope: OperationScope | None = None
    ) -> None:
        """仅为当前操作任务记录工作基线；版本归属留给执行前的 Resolver 验证。"""

        if self.active_task_id != task_id:
            raise ValueError("active base task must match current active task")
        state = self.snapshot
        state.active = ActiveContext(task_id=task_id, base_execution_id=execution_id, scope=scope)
        self._save(state)

    def set_view_context(
        self, task_id: str, execution_id: str | None = None, scope: OperationScope | None = None
    ) -> None:
        """只更新查看焦点，不能借查看操作切换 Active。"""

        state = self.snapshot
        state.view = ViewContext(task_id=task_id, execution_id=execution_id, scope=scope)
        self._save(state)

    def clear_view_context(self) -> None:
        """清除查看焦点，保留操作与近期上下文。"""

        state = self.snapshot
        state.view = None
        self._save(state)

    def set_recent_context(self, recent: RecentContext) -> None:
        """整体替换近期引用；未来 Operation 路由负责决定何时更新。"""

        state = self.snapshot
        state.recent = recent
        self._save(state)
