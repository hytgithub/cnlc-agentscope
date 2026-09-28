"""通用澄清的纯计划修补与 Session 生命周期，由统一操作交互层调用。"""

from collections.abc import Callable
from time import time
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import Field, ValidationError, model_validator

from cnlc_agent.demo.operation_models import (
    ExecutionReference,
    ExecutionReferenceKind,
    InputClassification,
    NonBlank,
    OperationInputReference,
    OperationScope,
    PersistMode,
    TargetType,
    ValueSpec,
)
from cnlc_agent.demo.operation_parser import (
    ClarificationIssue,
    ClarificationSlot,
    PartialOperationPlan,
    PartialScope,
    missing_plan_slots,
)
from cnlc_agent.demo.reference_resolver import ResolvedExecutionReference, ResolvedTaskReference
from cnlc_agent.demo.scope_resolver import ResolvedScope
from cnlc_agent.demo.task_context import TaskReference
from cnlc_agent.domain.models import Contract

PENDING_OPERATION_KEY = "cnlc_pending_operation_clarification"


class LockedOperationReference(Contract):
    """服务端固定的已解析引用；不是模型输入字段，执行时仍须重新授权。"""

    operation_id: NonBlank
    task_id: NonBlank
    execution_id: NonBlank | None = None
    scope: OperationScope | None = None

    @model_validator(mode="after")
    def require_version(self) -> Self:
        """固定层段必须连同版本固定。"""

        if self.scope is not None and self.execution_id is None:
            raise ValueError("locked scope requires execution_id")
        return self

    @classmethod
    def from_resolved(
        cls,
        operation_id: str,
        task: ResolvedTaskReference,
        execution: ResolvedExecutionReference | None = None,
        scope: ResolvedScope | None = None,
    ) -> Self:
        """供统一交互层在解析校验成功后使用；不接受用户 JSON 自称 trusted/locked。"""

        if execution is not None and execution.task_id != task.task_id:
            raise ValueError("resolved execution belongs to a different task")
        if scope is not None and (
            execution is None
            or scope.task_id != task.task_id
            or scope.execution_id != execution.execution_id
        ):
            raise ValueError("resolved scope does not match execution")
        return cls(
            operation_id=operation_id,
            task_id=task.task_id,
            execution_id=execution.execution_id if execution else None,
            scope=scope.scope if scope else None,
        )


class PendingOperationClarification(Contract):
    """完整待澄清计划与生命周期；仅保存短期 runtime，不是可执行队列。"""

    partial_plan: PartialOperationPlan
    issues: list[ClarificationIssue]
    owner_token: NonBlank
    created_turn: int = Field(ge=0, strict=True)
    expires_at: float
    original_instruction: NonBlank
    locked_references: list[LockedOperationReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_locked_references(self) -> Self:
        """损坏或与计划不一致的锁不能在跨轮次读取时继续使用。"""

        if self.original_instruction != self.partial_plan.original_instruction:
            raise ValueError("pending instruction differs from partial plan")
        nodes = {op.operation_id: op for op in self.partial_plan.operations}
        if len(nodes) != len(self.partial_plan.operations):
            raise ValueError("pending operations must have unique IDs")
        seen: set[str] = set()
        for locked in self.locked_references:
            node = nodes.get(locked.operation_id)
            if node is None or locked.operation_id in seen:
                raise ValueError("locked reference must identify one operation")
            seen.add(locked.operation_id)
            if node.task_reference != TaskReference(kind="TASK_ID", value=locked.task_id):
                raise ValueError("locked task differs from pending plan")
            if locked.execution_id is not None and node.execution_reference != ExecutionReference(
                kind=ExecutionReferenceKind.EXECUTION_ID, execution_id=locked.execution_id
            ):
                raise ValueError("locked execution differs from pending plan")
            if locked.scope is not None and node.scope != locked.scope:
                raise ValueError("locked scope differs from pending plan")
        if any(
            issue.operation_id is not None and issue.operation_id not in nodes
            for issue in self.issues
        ):
            raise ValueError("clarification issue refers to unknown operation")
        return self


class ClarificationPatch(Contract):
    """显式结构化回复；只开放可修补槽位，不允许声明锁或改写任意 Plan 字段。"""

    input_classification: Literal[
        InputClassification.CLARIFICATION_REPLY, InputClassification.CORRECTION
    ] = InputClassification.CLARIFICATION_REPLY
    operation_id: NonBlank | None = None
    task_reference: TaskReference | None = None
    execution_reference: ExecutionReference | None = None
    scope: PartialScope | None = None
    target: TargetType | None = None
    value: ValueSpec | None = None
    method_id: NonBlank | None = None
    model_id: NonBlank | None = None
    input_refs: list[OperationInputReference] | None = None
    persist_mode: PersistMode | None = None

    @model_validator(mode="after")
    def require_changes(self) -> Self:
        """显式 null 不是补全；清空或取消应使用 Store 的独立生命周期 API。"""

        changes = self.model_fields_set - {"operation_id", "input_classification"}
        if not changes or any(getattr(self, field) is None for field in changes):
            raise ValueError("patch must contain non-null slot values")
        return self


class ClarificationPatchError(ValueError):
    """受控修补拒绝，不更改既有 Pending。"""

    code = "CLARIFICATION_SLOT_INVALID"


_PATCH_SLOTS = {
    "task_reference": ClarificationSlot.TASK,
    "execution_reference": ClarificationSlot.EXECUTION,
    "scope": ClarificationSlot.SCOPE,
    "target": ClarificationSlot.TARGET,
    "value": ClarificationSlot.VALUE,
    "method_id": ClarificationSlot.METHOD,
    "model_id": ClarificationSlot.METHOD,
    "input_refs": ClarificationSlot.COMPARE_TARGET,
    "persist_mode": ClarificationSlot.PERSIST_MODE,
}


def apply_clarification_patch(
    pending: PendingOperationClarification,
    patch: ClarificationPatch,
) -> PendingOperationClarification:
    """原子修补尚未执行的计划；普通回复仅补允许槽，Correction 显式替换并撤销相关锁。"""

    pending = PendingOperationClarification.model_validate(pending.model_dump(mode="json"))
    patch = ClarificationPatch.model_validate(patch.model_dump(mode="json", exclude_unset=True))
    data = pending.model_dump(mode="json")
    plan = data["partial_plan"]
    # 将共享显式引用展开为节点引用，避免修正某节点后又从共享旧版本继承回来。
    for operation in plan["operations"]:
        for field in ("task_reference", "execution_reference", "scope"):
            if operation[field] is None:
                operation[field] = plan["shared_context"][field]
    plan["shared_context"] = {}
    changes = patch.model_dump(mode="json", exclude_unset=True)
    changes.pop("input_classification", None)
    operation_id = changes.pop("operation_id", None)
    node_changes = set(changes) - {"persist_mode"}
    node = None
    if node_changes:
        nodes = plan["operations"]
        if operation_id is None and len(nodes) == 1:
            operation_id = nodes[0]["operation_id"]
        node = next((op for op in nodes if op["operation_id"] == operation_id), None)
        if node is None:
            raise ClarificationPatchError("多节点修补必须指定已有 operation_id")
    correction = patch.input_classification == InputClassification.CORRECTION
    locks = [ref for ref in pending.locked_references if ref.operation_id == operation_id]
    for field, value in changes.items():
        slot = _PATCH_SLOTS[field]
        issue_operation = None if field == "persist_mode" else operation_id
        allowed = any(
            issue.slot == slot and issue.operation_id == issue_operation for issue in pending.issues
        )
        if not correction and not allowed:
            raise ClarificationPatchError(f"不允许补写槽位 {slot}")
        locked_field = {
            "task_reference": "task_id",
            "execution_reference": "execution_id",
            "scope": "scope",
        }.get(field)
        if (
            not correction
            and locked_field is not None
            and any(getattr(lock, locked_field) is not None for lock in locks)
        ):
            raise ClarificationPatchError("普通澄清不能改变已锁定引用")
        if field == "persist_mode":
            if not correction and plan[field] is not None:
                raise ClarificationPatchError("保存模式已经固定")
            plan[field] = value
        elif node is not None:
            container = node["parameters"] if field in {"value", "method_id", "model_id"} else node
            if not correction and field in {"target", "value", "method_id", "model_id"}:
                if container[field] is not None:
                    raise ClarificationPatchError("普通澄清不能覆盖已知值")
            container[field] = value
    if correction and node is not None:
        # 纠正 Task/版本时不能继承此前解析出的下级引用；后续 E 必须重新调用 B。
        if "task_reference" in changes:
            node["execution_reference"] = changes.get("execution_reference")
            node["scope"] = changes.get("scope")
        elif "execution_reference" in changes:
            node["scope"] = changes.get("scope")
        if node_changes.intersection({"task_reference", "execution_reference", "scope"}):
            data["locked_references"] = [
                ref for ref in data["locked_references"] if ref["operation_id"] != operation_id
            ]
    updated = PartialOperationPlan.model_validate(plan)
    fixed = {
        (None if field == "persist_mode" else operation_id, _PATCH_SLOTS[field])
        for field in changes
    }
    remaining = [
        issue
        for issue in pending.issues
        if (issue.operation_id, issue.slot) not in fixed
        and not (
            correction
            and issue.operation_id in {None, operation_id}
            and issue.slot == ClarificationSlot.CONFLICT_RESOLUTION
        )
    ]
    for issue in missing_plan_slots(updated):
        if not any(
            old.operation_id == issue.operation_id and old.slot == issue.slot for old in remaining
        ):
            remaining.append(issue)
    data["partial_plan"] = updated.model_dump(mode="json")
    data["issues"] = [issue.model_dump(mode="json") for issue in remaining]
    return PendingOperationClarification.model_validate(data)


class OperationClarificationStore:
    """只写 middle_context；owner 由服务端生成，新实例不会继承旧 owner。"""

    def __init__(
        self,
        context: dict[str, Any],
        *,
        ttl_seconds: float,
        clock: Callable[[], float] = time,
    ) -> None:
        if not 0 < ttl_seconds <= 3600:
            raise ValueError("clarification TTL must be in (0, 3600]")
        self._context = context
        self._owner = uuid4().hex
        self._ttl = ttl_seconds
        self._clock = clock

    def attach(self, context: dict[str, Any]) -> None:
        """每轮更换 Session runtime 引用，保留同一 Runner 的 owner。"""

        self._context = context

    def save(
        self,
        partial_plan: PartialOperationPlan,
        issues: list[ClarificationIssue],
        *,
        turn: int,
        locked_references: list[LockedOperationReference] | None = None,
    ) -> PendingOperationClarification:
        """新计划替换旧 Pending；caller issue 优先，再稳定合并 Schema 缺口并去重。"""

        plan = PartialOperationPlan.model_validate(partial_plan.model_dump(mode="json"))
        for locked in locked_references or []:
            node = next(
                (op for op in plan.operations if op.operation_id == locked.operation_id), None
            )
            if node is None:
                raise ClarificationPatchError("锁定引用必须属于已有节点")
            node.task_reference = TaskReference(kind="TASK_ID", value=locked.task_id)
            if locked.execution_id is not None:
                node.execution_reference = ExecutionReference(
                    kind=ExecutionReferenceKind.EXECUTION_ID, execution_id=locked.execution_id
                )
            if locked.scope is not None:
                node.scope = locked.scope
        merged_issues: list[ClarificationIssue] = []
        issue_keys: set[tuple[str | None, ClarificationSlot, str]] = set()
        for issue in [*issues, *missing_plan_slots(plan)]:
            key = (issue.operation_id, issue.slot, issue.error_code)
            if key not in issue_keys:
                issue_keys.add(key)
                merged_issues.append(issue.model_copy(deep=True))
        if not merged_issues:
            raise ClarificationPatchError("完整且无问题的计划不能保存为待澄清计划")
        pending = PendingOperationClarification(
            partial_plan=plan,
            issues=merged_issues,
            owner_token=self._owner,
            created_turn=turn,
            expires_at=self._clock() + self._ttl,
            original_instruction=plan.original_instruction,
            locked_references=locked_references or [],
        )
        self._context[PENDING_OPERATION_KEY] = pending.model_dump(mode="json")
        return pending.model_copy(deep=True)

    def peek(self, *, turn: int) -> PendingOperationClarification | None:
        """损坏、owner 不符、过期或非本轮/紧邻下一轮时清除，不从消息恢复。"""

        raw = self._context.get(PENDING_OPERATION_KEY)
        if raw is None:
            self.clear()
            return None
        try:
            pending = PendingOperationClarification.model_validate(raw)
        except ValidationError:
            self.clear()
            return None
        if (
            pending.owner_token != self._owner
            or pending.expires_at <= self._clock()
            or not pending.created_turn <= turn <= pending.created_turn + 1
        ):
            self.clear()
            return None
        return pending

    def apply_patch(self, patch: ClarificationPatch, *, turn: int) -> PendingOperationClarification:
        """有效窗口内原子补齐；不续期，不更改创建轮次，不执行最终计划。"""

        pending = self.peek(turn=turn)
        if pending is None:
            raise ClarificationPatchError("没有有效待澄清计划；已执行或失效的操作应作为新请求")
        updated = apply_clarification_patch(pending, patch)
        self._context[PENDING_OPERATION_KEY] = updated.model_dump(mode="json")
        return updated.model_copy(deep=True)

    def retain(self, *, turn: int) -> PendingOperationClarification | None:
        """模型输出无效时把同一服务端 Pending 延续到本轮，保留计划与可信引用。"""

        pending = self.peek(turn=turn)
        if pending is None:
            return None
        return self.save(
            pending.partial_plan,
            pending.issues,
            turn=turn,
            locked_references=pending.locked_references,
        )

    def consume(self, *, turn: int) -> PendingOperationClarification | None:
        """取出后清除；取出不等于允许执行，仍需完整 Context/Plan/Reference 校验。"""

        pending = self.peek(turn=turn)
        self.clear()
        return pending

    def clear(self) -> None:
        """取消、无关轮次或新独立操作可显式清除；旧 Pending key 完全不受影响。"""

        self._context.pop(PENDING_OPERATION_KEY, None)

    def cancel(self) -> None:
        """显式取消尚未执行的计划，不修改任何历史 Execution。"""

        self.clear()
