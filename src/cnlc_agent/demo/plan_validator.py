"""全计划纯校验；只给出规划资格，不创建 Execution，不调用 Handler 或 dispatcher。"""

from collections import deque
from collections.abc import Mapping

from pydantic import Field

from cnlc_agent.demo.operation_capabilities import (
    OperationCapabilityStatus,
    OperationCatalog,
)
from cnlc_agent.demo.operation_context_resolver import (
    READ_ACTIONS,
    TASK_CREATION_ACTIONS,
    ContextResolutionResult,
)
from cnlc_agent.demo.operation_models import (
    ActionType,
    DepthPointScope,
    DepthRangeScope,
    FilterSetScope,
    InputClassification,
    IntervalScope,
    MultiIntervalScope,
    NonBlank,
    OperationConstraintType,
    OperationPlan,
    OperationScope,
    PersistMode,
    ResolutionOutcome,
    WholeWellScope,
)
from cnlc_agent.demo.operation_parser import (
    ClarificationIssue,
    ClarificationSlot,
    PartialOperationPlan,
    PartialScope,
    finalize_partial_plan,
)
from cnlc_agent.demo.reference_resolver import ResolvedTaskReference
from cnlc_agent.domain.models import Contract


class PlanIssue(Contract):
    """全局或节点级稳定规划错误，不包含业务执行结果。"""

    error_code: NonBlank
    message: NonBlank
    operation_id: NonBlank | None = None


class CapabilityFact(Contract):
    """保留 Catalog 原始能力状态；缺少声明用 None，绝不隐式启用。"""

    operation_id: NonBlank
    action: ActionType
    capability_status: OperationCapabilityStatus | None
    description: str
    error_code: str | None = None


class PlanValidationResult(Contract):
    """裁决针对整个计划；失败时不返回可执行子计划，EXECUTABLE 仍非业务授权。"""

    outcome: ResolutionOutcome
    plan: OperationPlan | None = None
    issues: list[PlanIssue] = Field(default_factory=list)
    capability_facts: list[CapabilityFact] = Field(default_factory=list)
    capability_issues: list[CapabilityFact] = Field(default_factory=list)
    missing_slots: list[ClarificationIssue] = Field(default_factory=list)
    conflicts: list[PlanIssue] = Field(default_factory=list)
    read_only: bool = False
    graph_valid: bool = True


def _interval_ids(scope: OperationScope) -> set[str] | None:
    if isinstance(scope, IntervalScope):
        return {scope.interval_id}
    if isinstance(scope, MultiIntervalScope):
        return set(scope.interval_ids)
    if isinstance(scope, FilterSetScope) and scope.resolved_ids is not None:
        return set(scope.resolved_ids)
    return None


def _fully_excluded(scope: PartialScope, exclusions: list[OperationScope]) -> bool | None:
    """仅处理能由显式集合/同基准几何证明的覆盖，无法判定时返回 None。"""

    # 未解析层号没有稳定层段身份，不能在此宣称完全覆盖。
    if not isinstance(
        scope,
        (
            WholeWellScope,
            IntervalScope,
            MultiIntervalScope,
            DepthRangeScope,
            DepthPointScope,
            FilterSetScope,
        ),
    ):
        return None
    if any(isinstance(item, WholeWellScope) or item == scope for item in exclusions):
        return True
    ids = _interval_ids(scope)
    if ids is not None:
        sets = [_interval_ids(item) for item in exclusions]
        known = set().union(*(item for item in sets if item is not None))
        if ids <= known:
            return True
        return None if any(item is None for item in sets) else False
    if isinstance(scope, (DepthRangeScope, DepthPointScope)):
        ranges = [
            item
            for item in exclusions
            if isinstance(item, DepthRangeScope) and item.depth_reference == scope.depth_reference
        ]
        unknown = len(ranges) != len(exclusions)
        if isinstance(scope, DepthPointScope):
            if any(item.top <= scope.depth <= item.bottom for item in ranges):
                return True
        else:
            end = scope.top
            for item in sorted(ranges, key=lambda item: item.top):
                if item.top > end:
                    break
                end = max(end, item.bottom)
            if end >= scope.bottom:
                return True
        return None if unknown else False
    return None


class PlanValidator:
    """依赖注入 Catalog；所有节点先校验，任何阻断均阻断整个计划。"""

    def __init__(self, catalog: OperationCatalog | None = None) -> None:
        self.catalog = catalog if catalog is not None else OperationCatalog()

    @staticmethod
    def validate_graph(plan: PartialOperationPlan | OperationPlan) -> list[PlanIssue]:
        """检查显式边与输入依赖形成的同一图，不规划专业步骤或执行数量。"""

        ids = [op.operation_id for op in plan.operations]
        nodes = set(ids)
        errors: list[str] = []
        if len(nodes) != len(ids):
            errors.append("operation_id 必须唯一")
        edges = {(edge.from_operation_id, edge.to_operation_id) for edge in plan.edges}
        for op in plan.operations:
            edges.update(
                (ref.operation_id, op.operation_id)
                for ref in op.input_refs
                if ref.operation_id is not None
            )
        for source, target in edges:
            if source not in nodes or target not in nodes:
                errors.append("操作边或输入引用的端点不存在")
            elif source == target:
                errors.append("操作不能依赖自身")
        references = [op_id for condition in plan.conditions for op_id in condition.operation_ids]
        if plan.output_requirement is not None:
            references.extend(plan.output_requirement.operation_ids)
        if any(op_id not in nodes for op_id in references):
            errors.append("条件或输出引用的操作不存在")
        if not errors:
            successors: dict[str, set[str]] = {op_id: set() for op_id in nodes}
            degree = dict.fromkeys(nodes, 0)
            for source, target in edges:
                successors[source].add(target)
                degree[target] += 1
            ready = deque(op_id for op_id, count in degree.items() if count == 0)
            visited = 0
            while ready:
                source = ready.popleft()
                visited += 1
                for target in successors[source]:
                    degree[target] -= 1
                    if degree[target] == 0:
                        ready.append(target)
            if visited != len(nodes):
                errors.append("操作依赖图存在环")
        return [
            PlanIssue(error_code="INVALID_OPERATION_PLAN", message=message)
            for message in dict.fromkeys(errors)
        ]

    def validate(
        self,
        source: PartialOperationPlan | OperationPlan | ContextResolutionResult,
        *,
        resolved_task_references: Mapping[str, ResolvedTaskReference] | None = None,
    ) -> PlanValidationResult:
        """消费候选及其 issues；只接受 B 的 typed 任务解析读模型，不接受裸 task_id。"""

        resolved_task_references = resolved_task_references or {}
        if any(
            not isinstance(reference, ResolvedTaskReference)
            for reference in resolved_task_references.values()
        ):
            raise TypeError("resolved_task_references must contain ResolvedTaskReference values")

        context_issues = source.issues if isinstance(source, ContextResolutionResult) else []
        raw = source.plan if isinstance(source, ContextResolutionResult) else source
        plan = PartialOperationPlan.model_validate(raw.model_dump(mode="json"))
        operation_ids = {op.operation_id for op in plan.operations}
        unknown_resolution_ids = set(resolved_task_references) - operation_ids
        if unknown_resolution_ids:
            raise ValueError("resolved task reference provided for unknown operation_id")
        for op in plan.operations:
            resolved = resolved_task_references.get(op.operation_id)
            task = op.task_reference or plan.shared_context.task_reference
            if (
                resolved is not None
                and task is not None
                and task.kind == "TASK_ID"
                and resolved.task_id != task.value
            ):
                raise ValueError("resolved task does not match explicit TASK_ID reference")
        graph_issues = self.validate_graph(plan)
        result = PlanValidationResult(
            outcome=ResolutionOutcome.REJECTED, issues=graph_issues, graph_valid=not graph_issues
        )
        if graph_issues:
            return result
        if plan.input_classification == InputClassification.OUT_OF_DOMAIN:
            result.issues.append(
                PlanIssue(error_code="OUT_OF_DOMAIN", message="请求不属于测井领域")
            )
            return result
        for op in plan.operations:
            try:
                capability = self.catalog.get(op.action)
            except KeyError:
                fact = CapabilityFact(
                    operation_id=op.operation_id,
                    action=op.action,
                    capability_status=None,
                    description="Catalog 未声明此动作",
                    error_code="UNSUPPORTED_OPERATION",
                )
            else:
                unsupported = capability.status != OperationCapabilityStatus.ENABLED
                scope = op.scope or plan.shared_context.scope
                parameter = op.parameters.parameter_name
                if plan.input_classification != InputClassification.CAPABILITY_QUERY:
                    unsupported = (
                        unsupported
                        or (scope is not None and scope.kind not in capability.allowed_scopes)
                        or (
                            parameter is not None
                            and parameter not in capability.supported_parameters
                        )
                    )
                fact = CapabilityFact(
                    operation_id=op.operation_id,
                    action=op.action,
                    capability_status=capability.status,
                    description=capability.description,
                    error_code="UNSUPPORTED_OPERATION" if unsupported else None,
                )
            result.capability_facts.append(fact)
            if fact.error_code:
                result.capability_issues.append(fact)
        # 能力询问不要求执行槽位、保存模式或实际业务引用；无论开关如何都不产生写资格。
        if plan.input_classification == InputClassification.CAPABILITY_QUERY:
            result.outcome = ResolutionOutcome.READ_ONLY
            result.read_only = True
            return result
        invalid_creation_references = [
            op.operation_id
            for op in plan.operations
            if op.action in TASK_CREATION_ACTIONS
            and (op.task_reference is not None or plan.shared_context.task_reference is not None)
        ]
        if invalid_creation_references:
            result.issues.extend(
                PlanIssue(
                    operation_id=operation_id,
                    error_code="INVALID_OPERATION_PLAN",
                    message="任务创建动作不能引用已有 Task；已有任务全量重跑应使用稳定重跑链",
                )
                for operation_id in invalid_creation_references
            )
            return result
        if plan.input_classification in {
            InputClassification.META_REQUEST,
            InputClassification.CANCELLATION,
            InputClassification.CLARIFICATION_REPLY,
            InputClassification.CORRECTION,
            InputClassification.CONFIRMATION,
        }:
            result.issues.append(
                PlanIssue(
                    error_code="INVALID_OPERATION_PLAN",
                    message="交互回复必须先合并到待处理计划，不能直接执行回复载荷",
                )
            )
            return result
        if not plan.operations:
            result.missing_slots.append(
                ClarificationIssue(slot=ClarificationSlot.TARGET, message="请明确需要进行的操作")
            )
        finalized = finalize_partial_plan(plan)
        unsupported_ids = {
            fact.operation_id for fact in result.capability_issues if fact.operation_id is not None
        }
        result.missing_slots.extend(
            issue
            for issue in context_issues
            if not (
                issue.slot == ClarificationSlot.SCOPE
                and issue.operation_id in unsupported_ids
            )
        )
        result.missing_slots.extend(finalized.issues)
        writes = [op for op in plan.operations if op.action not in READ_ACTIONS]
        if writes and plan.input_classification == InputClassification.READ_REQUEST:
            result.conflicts.append(
                PlanIssue(
                    error_code="MULTI_OPERATION_CONFLICT", message="只读请求包含写操作，请明确意图"
                )
            )
        task_ids: set[str] = set()
        unresolved_writes = []
        for op in plan.operations:
            task = op.task_reference or plan.shared_context.task_reference
            resolved_reference = resolved_task_references.get(op.operation_id)
            resolved_id = resolved_reference.task_id if resolved_reference is not None else None
            if resolved_id is None and task is not None and task.kind == "TASK_ID":
                resolved_id = task.value
            creates_task = op.action in TASK_CREATION_ACTIONS
            if task is None and resolved_id is None and not op.input_refs and not creates_task:
                if not any(issue.operation_id == op.operation_id for issue in context_issues):
                    result.missing_slots.append(
                        ClarificationIssue(
                            operation_id=op.operation_id,
                            slot=ClarificationSlot.TASK,
                            message="请明确任务引用",
                        )
                    )
            if op in writes and not creates_task:
                if resolved_id is not None:
                    task_ids.add(resolved_id)
                else:
                    unresolved_writes.append(op.operation_id)
            types = {constraint.type for constraint in op.constraints}
            if (
                plan.persist_mode == PersistMode.CREATE_VERSION
                and OperationConstraintType.DO_NOT_PERSIST in types
            ):
                result.conflicts.append(
                    PlanIssue(
                        error_code="MULTI_OPERATION_CONFLICT",
                        operation_id=op.operation_id,
                        message="创建版本与不保存约束冲突",
                    )
                )
            if plan.persist_mode == PersistMode.PREVIEW and op.action == ActionType.COMMIT_SCENARIO:
                result.conflicts.append(
                    PlanIssue(
                        error_code="MULTI_OPERATION_CONFLICT",
                        operation_id=op.operation_id,
                        message="预览与提交试算冲突",
                    )
                )
            exclusions = [
                constraint.scope
                for constraint in op.constraints
                if constraint.type == OperationConstraintType.EXCLUDE_SCOPE
                and constraint.scope is not None
            ]
            if OperationConstraintType.ONLY_SCOPE in types and exclusions:
                scope = op.scope or plan.shared_context.scope
                excluded = _fully_excluded(scope, exclusions) if scope is not None else None
                if excluded:
                    result.conflicts.append(
                        PlanIssue(
                            error_code="MULTI_OPERATION_CONFLICT",
                            operation_id=op.operation_id,
                            message="排除范围覆盖了全部限定目标",
                        )
                    )
                elif excluded is None:
                    result.missing_slots.append(
                        ClarificationIssue(
                            operation_id=op.operation_id,
                            slot=ClarificationSlot.CONFLICT_RESOLUTION,
                            message="无法确定限定范围与排除范围关系，请明确实际范围",
                        )
                    )
        if any(
            op.action in {ActionType.FULL_INTERPRET, ActionType.FULL_RERUN}
            for op in plan.operations
        ) and any(
            c.type == OperationConstraintType.NO_FULL_RERUN
            for op in plan.operations
            for c in op.constraints
        ):
            result.conflicts.append(
                PlanIssue(
                    error_code="MULTI_OPERATION_CONFLICT", message="完整解释与禁止全量重跑冲突"
                )
            )
        if plan.conditions:
            result.issues.append(
                PlanIssue(
                    error_code="CONDITIONAL_EXECUTION_UNSUPPORTED",
                    message="当前不支持条件式自动执行",
                )
            )
        if len(task_ids) > 1:
            result.issues.append(
                PlanIssue(
                    error_code="CROSS_TASK_WRITE_UNSUPPORTED", message="当前不支持跨任务复合写"
                )
            )
        elif len(writes) > 1 and unresolved_writes:
            result.missing_slots.extend(
                ClarificationIssue(
                    operation_id=op_id,
                    slot=ClarificationSlot.TASK,
                    message="复合写需先明确各节点所属任务",
                )
                for op_id in unresolved_writes
            )
        if result.issues:
            result.outcome = ResolutionOutcome.KNOWN_UNSUPPORTED
        elif result.conflicts or result.missing_slots:
            result.outcome = ResolutionOutcome.NEED_CLARIFICATION
        elif result.capability_issues:
            result.outcome = ResolutionOutcome.KNOWN_UNSUPPORTED
        else:
            result.read_only = not writes
            result.outcome = (
                ResolutionOutcome.READ_ONLY if not writes else ResolutionOutcome.EXECUTABLE
            )
            result.plan = finalized.plan
        return result
