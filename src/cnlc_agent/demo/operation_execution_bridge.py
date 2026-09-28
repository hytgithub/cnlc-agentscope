"""结构化计划到现有任务命令的确定性执行桥；不接自然语言、Tool 或专业 Workflow。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import Field, ValidationError

from cnlc_agent.application.commands import (
    FullRerunCommand,
    GetReportCommand,
    ModifyInterpretationCommand,
    TaskCommandResult,
)
from cnlc_agent.demo.interaction_context import InteractionContext
from cnlc_agent.demo.interaction_state import InteractionPolicy
from cnlc_agent.demo.operation_command_adapter import adapt_commands
from cnlc_agent.demo.operation_context_resolver import (
    READ_ACTIONS,
    TASK_CREATION_ACTIONS,
    OperationContextResolver,
)
from cnlc_agent.demo.operation_models import (
    ActionType,
    ExecutionReference,
    ExecutionReferenceKind,
    InputClassification,
    OperationPlan,
    ResolutionOutcome,
    WholeWellScope,
)
from cnlc_agent.demo.operation_parser import (
    ClarificationIssue,
    ClarificationSlot,
    PartialOperationPlan,
)
from cnlc_agent.demo.plan_validator import PlanIssue, PlanValidationResult, PlanValidator
from cnlc_agent.demo.reference_resolver import (
    OperationReferenceResolver,
    ReferenceAccessMode,
    ResolvedExecutionReference,
    ResolvedTaskReference,
)
from cnlc_agent.demo.scope_resolver import ResolvedScope, ScopeResolver
from cnlc_agent.demo.task_context import TaskReference
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.domain.errors import ApplicationError, DataError
from cnlc_agent.domain.models import Contract


class OperationBridgeResult(Contract):
    """SUCCESS 仅表示命令已提交或读取成功，不代表后台专业解释完成。"""

    outcome: Literal["SUCCESS", "READ_ONLY", "REJECTED", "NEED_CLARIFICATION", "KNOWN_UNSUPPORTED"]
    validation: PlanValidationResult | None = None
    results: list[TaskCommandResult] = Field(default_factory=list)
    error_code: str | None = None
    message: str = ""
    created_execution_ids: list[str] = Field(default_factory=list)
    read_only: bool = False
    resolved_tasks: dict[str, ResolvedTaskReference] = Field(default_factory=dict)
    resolved_executions: dict[str, ResolvedExecutionReference] = Field(default_factory=dict)
    resolved_scopes: dict[str, ResolvedScope] = Field(default_factory=dict)


class OperationExecutionBridge:
    """每次调用独立解析，授权仅来自 runner 的服务端 Session Identity 和 Repository。"""

    def __init__(self, runner: TaskCommandRunner) -> None:
        self.runner = runner
        self.validator = PlanValidator()

    async def execute(self, source: PartialOperationPlan | OperationPlan) -> OperationBridgeResult:
        """先完整解析、校验与适配，再执行；预检失败不产生任何业务写副作用。"""

        result = OperationBridgeResult(outcome="REJECTED")
        if self.runner.session_identity is None:
            return self._reject(result, "OPERATION_SESSION_IDENTITY_REQUIRED", "执行桥需要会话身份")
        try:
            context = self.runner.interaction_context
            resolved = OperationContextResolver().resolve(source, context)
        except ValidationError:
            return self._reject(result, "INVALID_OPERATION_PLAN", "操作计划结构无效")
        plan = resolved.plan
        # operation_id 必须先唯一，才能构造 B 的按节点结果表；否则覆盖键会伪造 D2 矛盾。
        graph_issues = self.validator.validate_graph(plan)
        if graph_issues:
            result.validation = PlanValidationResult(
                outcome=ResolutionOutcome.REJECTED, issues=graph_issues, graph_valid=False
            )
            return self._validation_result(result)
        # 非执行分类只走纯 Validator；能力询问不能因为携带动作而查询或提交命令。
        if plan.input_classification not in {
            InputClassification.EXECUTION_REQUEST,
            InputClassification.READ_REQUEST,
        }:
            result.validation = self.validator.validate(resolved)
            return self._validation_result(result)
        initial_operations = [
            op for op in plan.operations if op.action == ActionType.FULL_INTERPRET
        ]
        if initial_operations:
            # 首次解释也必须先经过完整计划裁决，不能因其中一个节点提前短路其余语义。
            result.validation = self.validator.validate(resolved)
            if result.validation.plan is None:
                return self._validation_result(result)
            if len(plan.operations) != 1:
                return self._reject(
                    result,
                    "COMPOUND_EXECUTION_UNSUPPORTED",
                    "首次解释当前不能与其他操作组成复合执行",
                )
            return self._reject(
                result, "INITIAL_INPUT_ROUTE_REQUIRED", "首次解释必须通过已校验输入资料入口"
            )
        try:
            # 使用 runner.context，真实持久模式不能误读 runner 的内存测试仓库。
            with TemporaryDirectory(prefix="cnlc-operation-") as directory:
                async with self.runner.context(Path(directory)) as service:
                    references = OperationReferenceResolver(
                        service.repository, self.runner.session_identity
                    )
                    scopes = ScopeResolver(service.repository, self.runner.session_identity)
                    for op in plan.operations:
                        if (
                            op.task_reference is None
                            and context.active
                            and any(
                                issue.operation_id == op.operation_id
                                and issue.error_code == "VIEW_ACTIVE_CONTEXT_CONFLICT"
                                and issue.slot == ClarificationSlot.EXECUTION
                                for issue in resolved.issues
                            )
                        ):
                            # 同任务的版本冲突可先核验 current；跨任务冲突绝不填默认目标。
                            op.task_reference = TaskReference(
                                kind="TASK_ID", value=context.active.task_id
                            )
                        if op.action in TASK_CREATION_ACTIONS or op.task_reference is None:
                            continue
                        task = await references.resolve_task(
                            op.task_reference,
                            ReferenceAccessMode.READ_ONLY
                            if op.action in READ_ACTIONS
                            else ReferenceAccessMode.WRITE,
                            active_task_id=context.active.task_id if context.active else None,
                        )
                        result.resolved_tasks[op.operation_id] = task
                        if op.execution_reference is None:
                            # 符号 Task 只有经过 B 后才知道是否与 Active 同井，不能跨井拼接基线。
                            active = context.active
                            if (
                                op.action not in READ_ACTIONS
                                and active is not None
                                and active.task_id == task.task_id
                                and active.base_execution_id
                            ):
                                op.execution_reference = ExecutionReference(
                                    kind=ExecutionReferenceKind.ACTIVE_BASE
                                )
                                if op.scope is None:
                                    op.scope = active.scope
                            else:
                                op.execution_reference = ExecutionReference(
                                    kind=ExecutionReferenceKind.TASK_CURRENT
                                )
                        if (
                            op.action == ActionType.STATUS
                            and op.execution_reference.kind != ExecutionReferenceKind.TASK_CURRENT
                        ):
                            return self._reject(
                                result,
                                "STATUS_EXECUTION_SELECTION_UNSUPPORTED",
                                "状态查询暂不支持指定历史版本",
                            )
                        execution = await references.resolve_execution(
                            task.task_id,
                            op.execution_reference,
                            active_base_execution_id=self._base(context, task.task_id),
                            previous_anchor_execution_id=self._previous_anchor(
                                context, task.task_id, op.action, op.execution_reference.kind
                            ),
                        )
                        result.resolved_executions[op.operation_id] = execution
                        op.scope = op.scope or WholeWellScope()
                        result.resolved_scopes[op.operation_id] = await scopes.resolve_reference(
                            task.task_id, execution.execution_id, op.scope
                        )
                        op.scope = result.resolved_scopes[op.operation_id].scope
                    # C 的纯上下文阶段不知道 View 是否仍是 current；仅用 B 的真实事实消除误报。
                    view = context.view
                    if view is not None:
                        resolved.issues = [
                            issue
                            for issue in resolved.issues
                            if not (
                                issue.error_code == "VIEW_ACTIVE_CONTEXT_CONFLICT"
                                and issue.slot == ClarificationSlot.EXECUTION
                                and issue.operation_id in result.resolved_tasks
                                and result.resolved_tasks[issue.operation_id].task_id
                                == view.task_id
                                and result.resolved_tasks[issue.operation_id].current_execution_id
                                == view.execution_id
                            )
                        ]
                    result.validation = self.validator.validate(
                        resolved, resolved_task_references=result.resolved_tasks
                    )
                    if result.validation.plan is None:
                        return self._validation_result(result)
                    validated = result.validation.plan
                    result.read_only = result.validation.read_only
                    commands = adapt_commands(
                        validated, result.resolved_tasks, result.resolved_executions
                    )
                    if not result.read_only:
                        await self._revalidate_write(references, validated, result, context)
                    # 所有命令先通过产品策略，再允许第一个命令进入 runner。
                    for adapted in commands:
                        command = adapted.command
                        action: Literal["MODIFY", "FULL_RERUN", "GET_REPORT", "STATUS"] = (
                            "MODIFY"
                            if isinstance(command, ModifyInterpretationCommand)
                            else "FULL_RERUN"
                            if isinstance(command, FullRerunCommand)
                            else "GET_REPORT"
                            if isinstance(command, GetReportCommand)
                            else "STATUS"
                        )
                        decision = InteractionPolicy.decide(
                            await self.runner.interaction_snapshot(command.task_id),
                            action,
                            selector="EXPLICIT"
                            if isinstance(command, GetReportCommand)
                            else "CURRENT",
                        )
                        if decision.error_code:
                            return self._reject(result, decision.error_code, decision.message)
                    for adapted in commands:
                        if result.read_only:
                            command_result = await self.runner.execute(adapted.command)
                        else:

                            async def before_write() -> None:
                                # 锁前预检用于整计划拒绝，锁内重检保护排队期间的撤权和漂移。
                                await self._revalidate_write(references, validated, result, context)

                            command_result = await self.runner.execute(
                                adapted.command,
                                expected_current_execution_id=result.resolved_executions[
                                    adapted.operation_ids[0]
                                ].execution_id,
                                before_write=before_write,
                            )
                        result.results.append(command_result)
                        if result.read_only:
                            scope = result.resolved_scopes[adapted.operation_ids[0]].scope
                            self.runner.set_view_context(
                                command_result.task_id, command_result.execution_id, scope
                            )
                        else:
                            result.created_execution_ids.append(command_result.execution_id)
                            self.runner.set_active_task(command_result.task_id)
                            self.runner.clear_active_base()
        except ValidationError:
            return self._reject(result, "INVALID_OPERATION_PLAN", "参数不符合现有命令契约")
        except ApplicationError as exc:
            # 只传播稳定代码；底层异常原文可能含连接或输入细节，不能进入桥接结果。
            return self._reject(
                result, exc.code, "操作未能完成，请按错误码检查引用、参数或当前状态"
            )
        result.outcome = "SUCCESS"
        return result

    async def _revalidate_write(
        self,
        references: OperationReferenceResolver,
        plan: OperationPlan,
        result: OperationBridgeResult,
        context: InteractionContext,
    ) -> None:
        """解析事实不是永久授权；重新解析原选择器并拒绝任务或版本漂移。"""

        for op in plan.operations:
            assert op.task_reference is not None and op.execution_reference is not None
            before = result.resolved_tasks[op.operation_id]
            selected = result.resolved_executions[op.operation_id]
            current = await references.resolve_task(
                op.task_reference,
                ReferenceAccessMode.WRITE,
                active_task_id=self.runner.active_task_id,
            )
            execution = await references.resolve_execution(
                current.task_id,
                op.execution_reference,
                active_base_execution_id=self._base(context, current.task_id),
            )
            if (
                current.task_id != before.task_id
                or current.current_execution_id != before.current_execution_id
                or execution.execution_id != selected.execution_id
                or execution.execution_id != current.current_execution_id
            ):
                raise DataError("STALE_CONTEXT_REFERENCE", "计划解析后任务或版本已变化")

    @staticmethod
    def _base(context: InteractionContext, task_id: str) -> str | None:
        return (
            context.active.base_execution_id
            if context.active is not None and context.active.task_id == task_id
            else None
        )

    @staticmethod
    def _previous_anchor(
        context: InteractionContext,
        task_id: str,
        action: ActionType,
        kind: ExecutionReferenceKind,
    ) -> str | None:
        """只读 PREVIOUS 优先沿用同任务 View；跨任务或写操作均不能借用该锚点。"""

        view = context.view
        if (
            action in READ_ACTIONS
            and kind == ExecutionReferenceKind.PREVIOUS
            and view is not None
            and view.task_id == task_id
        ):
            return view.execution_id
        return None

    @staticmethod
    def _reject(result: OperationBridgeResult, code: str, message: str) -> OperationBridgeResult:
        result.outcome = (
            "NEED_CLARIFICATION"
            if code in {"MULTI_OPERATION_CONFLICT", "AMBIGUOUS_TASK_REFERENCE"}
            else "KNOWN_UNSUPPORTED"
            if code.endswith("UNSUPPORTED")
            or code in {"UNSUPPORTED_VALUE_MODE", "UNSUPPORTED_OPERATION", "UNSUPPORTED_PARAMETER"}
            else "REJECTED"
        )
        result.error_code, result.message = code, message
        return result

    @staticmethod
    def _validation_result(result: OperationBridgeResult) -> OperationBridgeResult:
        validation = result.validation
        assert validation is not None
        result.outcome = (
            "READ_ONLY"
            if validation.outcome == ResolutionOutcome.READ_ONLY
            else "NEED_CLARIFICATION"
            if validation.outcome == ResolutionOutcome.NEED_CLARIFICATION
            else "KNOWN_UNSUPPORTED"
            if validation.outcome == ResolutionOutcome.KNOWN_UNSUPPORTED
            else "REJECTED"
        )
        result.read_only = validation.read_only
        issues: list[PlanIssue | ClarificationIssue] = [
            *validation.issues,
            *validation.conflicts,
            *validation.missing_slots,
        ]
        if issues:
            result.error_code, result.message = issues[0].error_code, issues[0].message
        elif validation.capability_issues:
            result.error_code = "UNSUPPORTED_OPERATION"
            result.message = "计划包含当前未开放的能力、范围或参数"
        return result
