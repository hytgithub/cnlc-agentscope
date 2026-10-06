"""只补全交互引用候选；不访问业务仓库，不能替代 Task 10.5-B 的授权解析。"""

from pydantic import Field

from cnlc_agent.demo.interaction_context import (
    ActiveContext,
    CompareContext,
    InteractionContext,
    SelectedIntervalReference,
    ViewContext,
)
from cnlc_agent.demo.operation_models import (
    ActionType,
    ExecutionReference,
    ExecutionReferenceKind,
    InputClassification,
    OperationPlan,
)
from cnlc_agent.demo.operation_parser import (
    ClarificationIssue,
    ClarificationSlot,
    PartialOperationPlan,
)
from cnlc_agent.demo.task_context import TaskReference
from cnlc_agent.domain.models import Contract

# 未列入只读集合的动作一律采用写焦点边界，避免新动作意外继承 View。
READ_ACTIONS = frozenset(
    {
        ActionType.QUERY,
        ActionType.EXPLAIN,
        ActionType.COMPARE,
        ActionType.HISTORY,
        ActionType.REPORT,
        ActionType.STATUS,
    }
)
# 首次解释和新井语义在 Task 创建前成立；不能从 Active 继承旧任务。
TASK_CREATION_ACTIONS = frozenset({ActionType.FULL_INTERPRET, ActionType.NEW_WELL})


class ContextResolutionResult(Contract):
    """候选计划及阻断补全的问题；调用 Validator 时应整体传入，不能丢弃 issues。"""

    plan: PartialOperationPlan
    issues: list[ClarificationIssue] = Field(default_factory=list)


class OperationContextResolver:
    """显式引用优先，读使用 View/Active，写只能使用 Active，不自动使用 Recent。"""

    def resolve(
        self, plan: PartialOperationPlan | OperationPlan, context: InteractionContext
    ) -> ContextResolutionResult:
        """返回隔离副本；冲突节点不选择任何默认任务或历史写基线。"""

        result = ContextResolutionResult(
            plan=PartialOperationPlan.model_validate(plan.model_dump(mode="json"))
        )
        context = InteractionContext.model_validate(context.model_dump(mode="json"))
        if plan.input_classification in {
            InputClassification.CAPABILITY_QUERY,
            InputClassification.OUT_OF_DOMAIN,
            InputClassification.META_REQUEST,
            InputClassification.CANCELLATION,
        }:
            return result
        for op in result.plan.operations:
            shared = result.plan.shared_context
            task = op.task_reference or shared.task_reference
            execution = op.execution_reference or shared.execution_reference
            scope = op.scope or shared.scope
            active, view = context.active, context.view
            read = op.action in READ_ACTIONS
            creates_task = op.action in TASK_CREATION_ACTIONS
            candidate: ActiveContext | ViewContext | None = (
                None if creates_task else (view or active) if read else active
            )
            implicit_task = task is None
            conflict_slot: ClarificationSlot | None = None
            if not read and not creates_task:
                # 跨任务查看与操作焦点冲突时，即使提供了版本也不能猜 Task。
                if implicit_task and view and active and view.task_id != active.task_id:
                    conflict_slot = ClarificationSlot.TASK
                selected_task = (
                    task.value
                    if task and task.kind == "TASK_ID"
                    else (active.task_id if implicit_task and active else None)
                )
                if (
                    active
                    and view
                    and selected_task == active.task_id == view.task_id
                    and execution is None
                    and active.base_execution_id is None
                    and view.execution_id is not None
                ):
                    conflict_slot = ClarificationSlot.EXECUTION
            if conflict_slot is not None:
                result.issues.append(
                    ClarificationIssue(
                        operation_id=op.operation_id,
                        slot=conflict_slot,
                        error_code="VIEW_ACTIVE_CONTEXT_CONFLICT",
                        message=(
                            "查看对象与操作焦点属于不同任务，请明确要操作的任务或井"
                            if conflict_slot == ClarificationSlot.TASK
                            else "刚查看的历史版本与当前操作基线不一致，请明确工作版本"
                        ),
                    )
                )
                # 只保留用户给出的字段；未来澄清不能误把自动填入的 A 当显式选择。
                op.task_reference, op.execution_reference, op.scope = task, execution, scope
                continue
            if task is not None:
                # 纯内存只能确定 TASK_ID 是否匹配；WELL_ID/符号引用交给 B，不能跨井拼接版本。
                candidates = [view, active] if read else [active]
                candidate = next(
                    (
                        item
                        for item in candidates
                        if item is not None
                        and task.kind == "TASK_ID"
                        and task.value == item.task_id
                    ),
                    None,
                )
            elif candidate is not None:
                task = TaskReference(kind="TASK_ID", value=candidate.task_id)
            if task is None and not op.input_refs and not creates_task:
                result.issues.append(
                    ClarificationIssue(
                        operation_id=op.operation_id,
                        slot=ClarificationSlot.TASK,
                        message="请明确操作任务；查看焦点不能作为写操作任务",
                    )
                )
            candidate_execution = None
            if isinstance(candidate, ActiveContext):
                candidate_execution = candidate.base_execution_id
            elif isinstance(candidate, ViewContext):
                candidate_execution = candidate.execution_id
            if execution is None and candidate_execution is not None:
                execution = ExecutionReference(
                    kind=ExecutionReferenceKind.EXECUTION_ID, execution_id=candidate_execution
                )
            # 不把某一历史版本的局部 scope 拼到显式选择的另一版本或符号版本上。
            same_version = (
                execution is not None
                and execution.kind == ExecutionReferenceKind.EXECUTION_ID
                and execution.execution_id == candidate_execution
            )
            if scope is None and candidate is not None and same_version:
                scope = candidate.scope
            if (
                op.action == ActionType.MODIFY_PARAMETER
                and scope is None
                and result.plan.input_classification != InputClassification.CAPABILITY_QUERY
                and not any(issue.operation_id == op.operation_id for issue in result.issues)
            ):
                result.issues.append(
                    ClarificationIssue(
                        operation_id=op.operation_id,
                        slot=ClarificationSlot.SCOPE,
                        error_code="CLARIFICATION_REQUIRED",
                        message="请明确本次参数修改范围；查看范围不会自动成为写入范围",
                    )
                )
            op.task_reference, op.execution_reference, op.scope = task, execution, scope
        return result

    @staticmethod
    def resolve_recent_selection(context: InteractionContext) -> list[SelectedIntervalReference]:
        """仅供未来明确的近期选择语义调用，返回副本，不隐式注入节点。"""

        return context.recent.model_copy(deep=True).selected_intervals

    @staticmethod
    def resolve_last_compare(context: InteractionContext) -> CompareContext | None:
        """明确读取上次比较引用，仍需 B 重新校验。"""

        return context.recent.model_copy(deep=True).last_compare
