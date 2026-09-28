"""Operation 交互控制：复用通用澄清、可信引用与执行桥，不解析自然语言。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

from pydantic import Field

from cnlc_agent.application.commands import TaskCommandResult
from cnlc_agent.demo.operation_clarification import ClarificationPatch, LockedOperationReference
from cnlc_agent.demo.operation_context_resolver import READ_ACTIONS
from cnlc_agent.demo.operation_execution_bridge import (
    OperationBridgeResult,
    OperationExecutionBridge,
)
from cnlc_agent.demo.operation_models import (
    ExecutionReference,
    ExecutionReferenceKind,
    InputClassification,
    PersistMode,
    WholeWellScope,
)
from cnlc_agent.demo.operation_parser import (
    ClarificationIssue,
    ClarificationSlot,
    PartialOperationPlan,
    PartialScope,
)
from cnlc_agent.demo.plan_validator import CapabilityFact
from cnlc_agent.demo.reference_resolver import OperationReferenceResolver, ReferenceAccessMode
from cnlc_agent.demo.scope_resolver import ScopeResolver
from cnlc_agent.demo.task_context import TaskReference
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.domain.models import Contract


class PlanRequest(Contract):
    """一次请求提交完整意图集合，缺槽允许进入受控澄清。"""

    mode: Literal["PLAN"]
    plan: PartialOperationPlan


class ClarificationReplyRequest(Contract):
    """只允许通过正式 Patch 补齐或纠正尚未执行的计划。"""

    mode: Literal["CLARIFICATION_REPLY"]
    patch: ClarificationPatch


class CancelRequest(Contract):
    """取消短期 Pending，不取消后台 Execution。"""

    mode: Literal["CANCEL"]


class SetActiveContextRequest(Contract):
    """显式切换后续写操作焦点，不是业务 Action。"""

    mode: Literal["SET_ACTIVE_CONTEXT"]
    task_reference: TaskReference
    execution_reference: ExecutionReference | None = None
    scope: PartialScope | None = None


OperationRequest = Annotated[
    PlanRequest | ClarificationReplyRequest | CancelRequest | SetActiveContextRequest,
    Field(discriminator="mode"),
]


class OperationToolResult(Contract):
    """只投影安全命令事实；不返回 Pending owner、完整内部计划或状态快照。"""

    outcome: str
    error_code: str | None = None
    message: str = ""
    task_results: list[TaskCommandResult] = Field(default_factory=list)
    created_execution_ids: list[str] = Field(default_factory=list)
    capability_facts: list[CapabilityFact] = Field(default_factory=list)


class OperationInteractionController:
    """一个 Runner 的交互请求串行处理，Pending 在执行前消费，失败不重放。"""

    def __init__(self, runner: TaskCommandRunner) -> None:
        self.runner = runner
        self.bridge = OperationExecutionBridge(runner)

    async def handle(self, request: OperationRequest) -> OperationToolResult:
        """模式分发只管理交互生命周期；所有业务操作进入同一 Bridge。"""
        async with self.runner.operation_lock:
            if isinstance(request, CancelRequest):
                self.runner.clear_operation_clarification()
                return OperationToolResult(
                    outcome="READ_ONLY", message="本次待澄清操作已取消，没有产生新的执行。"
                )
            if isinstance(request, SetActiveContextRequest):
                self.runner.clear_operation_clarification()
                return await self._set_active(request)
            replying = isinstance(request, ClarificationReplyRequest)
            if isinstance(request, ClarificationReplyRequest):
                pending = self.runner.apply_operation_clarification(request.patch)
                source = pending.partial_plan
                # 消费在任何命令之前；重试同一 Patch 不可能再次执行。
                self.runner.clear_operation_clarification()
            else:
                self.runner.clear_operation_clarification()
                source = request.plan.model_copy(deep=True)
                if (
                    source.input_classification == InputClassification.READ_REQUEST
                    and source.operations
                    and all(op.action in READ_ACTIONS for op in source.operations)
                    and source.persist_mode is None
                ):
                    # 只读请求不创建版本；为既有完整 Plan 契约填充无副作用的占位保存模式。
                    source.persist_mode = PersistMode.CREATE_VERSION
                context = self.runner.interaction_context
                if context.active is None and context.view is None:
                    for op in source.operations:
                        if (
                            op.action not in {"FULL_INTERPRET", "NEW_WELL"}
                            and op.task_reference is None
                            and source.shared_context.task_reference is None
                        ):
                            op.task_reference = TaskReference(kind="CURRENT")
            result = await self.bridge.execute(source)
            if result.outcome == "NEED_CLARIFICATION" and not replying:
                self._save_pending(source, result)
            output = self._project(result)
            if result.error_code == "NO_EFFECTIVE_CHANGE":
                labels = {"POROSITY": "孔隙度", "PERMEABILITY": "渗透率"}
                values = "、".join(
                    f"{labels.get(op.target or '', '参数')}已经是{op.parameters.value.value:g}"
                    for op in source.operations
                    if op.parameters.value is not None
                )
                output.message = f"当前{values}，本次没有产生新的执行。"
            return output

    def _save_pending(self, source: PartialOperationPlan, result: OperationBridgeResult) -> None:
        """仅使用 B Resolver 成功返回的引用锁，不能把模型 ID 标记为可信。"""
        issues = list(result.validation.missing_slots) if result.validation else []
        if not issues:
            issues = [
                ClarificationIssue(
                    slot=ClarificationSlot.CONFLICT_RESOLUTION,
                    error_code=result.error_code or "CLARIFICATION_REQUIRED",
                    message=result.message or "请明确本次操作",
                )
            ]
        locks = [
            LockedOperationReference.from_resolved(
                operation_id,
                task,
                result.resolved_executions.get(operation_id),
                result.resolved_scopes.get(operation_id),
            )
            for operation_id, task in result.resolved_tasks.items()
        ]
        self.runner.save_operation_clarification(source, issues, locks)

    async def _set_active(self, request: SetActiveContextRequest) -> OperationToolResult:
        """全部引用先以 WRITE 规则解析；成功后一次更新 Runtime，不创建 Execution。"""
        identity = self.runner.session_identity
        if identity is None:
            return OperationToolResult(
                outcome="REJECTED",
                error_code="OPERATION_SESSION_IDENTITY_REQUIRED",
                message="缺少会话身份",
            )
        with TemporaryDirectory(prefix="cnlc-focus-") as directory:
            async with self.runner.context(Path(directory)) as service:
                references = OperationReferenceResolver(service.repository, identity)
                task = await references.resolve_task(
                    request.task_reference,
                    ReferenceAccessMode.WRITE,
                    active_task_id=self.runner.active_task_id,
                )
                execution = None
                scope = None
                if request.execution_reference is not None or request.scope is not None:
                    execution = await references.resolve_execution(
                        task.task_id,
                        request.execution_reference
                        or ExecutionReference(kind=ExecutionReferenceKind.TASK_CURRENT),
                        active_base_execution_id=OperationExecutionBridge._base(
                            self.runner.interaction_context, task.task_id
                        ),
                    )
                    scope = (
                        await ScopeResolver(service.repository, identity).resolve_reference(
                            task.task_id, execution.execution_id, request.scope or WholeWellScope()
                        )
                    ).scope
                self.runner.set_active_task(task.task_id)
                if execution is not None:
                    self.runner.set_active_base(task.task_id, execution.execution_id, scope)
                # 用户显式选择后续写焦点时，旧查看锚点不再参与隐式写入裁决。
                # 同井历史 View 也必须清除，否则切回该井仍会被旧版本冲突挡住。
                self.runner.clear_view_context()
        return OperationToolResult(
            outcome="READ_ONLY", message=f"当前操作焦点已切换到 {task.well_id}，没有产生新的执行。"
        )

    @staticmethod
    def _project(result: OperationBridgeResult) -> OperationToolResult:
        """能力事实使用 Catalog 描述；读写命令结果保留原有持久读模型。"""
        facts = result.validation.capability_facts if result.validation else []
        message = result.message
        safe_messages = {
            "TASK_NOT_FOUND": "请先上传井资料或开始一次解释任务。",
            "EXECUTION_NOT_FOUND": "没有符合条件的历史报告或执行版本，请明确版本。",
            "UNSUPPORTED_OPERATION": "当前未开放该操作、参数或局部重算能力，不会自动全量重跑。",
            "INTERVAL_NOT_FOUND": "目标版本不存在所指定的层号，本次没有产生新的执行。",
        }
        message = safe_messages.get(result.error_code or "", message)
        if result.outcome == "READ_ONLY" and facts:
            labels = {
                "ENABLED": "已开放",
                "DISABLED": "已禁用",
                "UNVERIFIED": "待业务确认",
                "NOT_IMPLEMENTED": "尚未开放",
            }
            message = "；".join(
                f"{fact.description}：{labels.get(fact.capability_status or '', '尚未开放')}"
                for fact in facts
            )
        if result.error_code == "INITIAL_INPUT_ROUTE_REQUIRED":
            message = "请上传井资料，或提供明确的测试井号，再开始首次解释。"
        return OperationToolResult(
            outcome=result.outcome,
            error_code=result.error_code,
            message=message,
            task_results=result.results,
            created_execution_ids=result.created_execution_ids,
            capability_facts=facts,
        )
