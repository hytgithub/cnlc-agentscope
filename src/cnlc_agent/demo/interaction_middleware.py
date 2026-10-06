"""在 AgentScope 请求生命周期管理短期澄清，不另行调用意图模型。"""

import json
import re
from collections.abc import AsyncGenerator, Callable
from contextvars import ContextVar
from copy import deepcopy
from typing import Any
from uuid import uuid4

from agentscope.agent import Agent
from agentscope.event import (
    AgentEvent,
    ReplyEndEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import Msg, ToolCallBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse
from pydantic import ValidationError

from cnlc_agent.demo.interaction_state import (
    InteractionPolicy,
    InteractionSnapshot,
    render_interaction_result,
)
from cnlc_agent.demo.operation_models import ActionType, WholeWellScope
from cnlc_agent.demo.task_tools import TaskCommandRunner, interaction_chunk

_NUMBER_PATTERN = re.compile(
    r"(?<![\d.])([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(%)?"
)
_WHOLE_WELL_WORDING = re.compile(r"全井|整井|整口井|全口井|整个井")
_PREVIOUS_TASK_WORDING = re.compile(r"上一口井|前一口井|之前那口井")
_PREVIOUS_VERSION_WORDING = re.compile(r"上一版|前一版|上一版本|前一版本")
_LATEST_SUCCESSFUL_WORDING = re.compile(r"最近成功|最新成功|最近一次成功")
_CURRENT_TURN_MESSAGES: ContextVar[tuple[Msg, ...] | None] = ContextVar(
    "cnlc_current_turn_messages",
    default=None,
)


class UngroundedWriteScope(ValueError):
    """模型候选整井范围没有本轮用户措辞支持，不能进入执行桥。"""


class UngroundedAuthorityReference(ValueError):
    """摘要单独支持的显式 Task / Execution ID 不得进入 Resolver。"""


class UngroundedScopeReference(ValueError):
    """摘要单独支持的层号或深度不能充当本轮范围授权。"""


def current_turn_messages(inputs: Any) -> tuple[Msg, ...] | None:
    """仅从本次 AgentScope reply 的原始 inputs 取得用户消息。"""

    if inputs is None:
        return None
    messages = inputs if isinstance(inputs, list) else [inputs]
    return tuple(
        deepcopy(message)
        for message in messages
        if isinstance(message, Msg) and message.role == "user"
    )


def current_turn_text() -> str:
    """返回当前 reply 的原始用户文字；历史摘要不参与本轮证据判断。"""

    messages = _CURRENT_TURN_MESSAGES.get()
    if messages is None:
        return ""
    return "\n".join(message.get_text_content() or "" for message in messages)


def extract_grounded_numbers(user_text: str) -> set[float]:
    """提取当前用户消息中的阿拉伯数字；百分数按绝对小数折算。"""

    return {
        float(match.group(1)) / (100 if match.group(2) else 1)
        for match in _NUMBER_PATTERN.finditer(user_text)
    }


def _scope_ordinals(scope: Any) -> list[int]:
    """只读取模型已结构化的层号，不解释自然语言。"""

    from cnlc_agent.demo.operation_parser import (
        IntervalOrdinalReference,
        MultiIntervalOrdinalReference,
    )

    if isinstance(scope, IntervalOrdinalReference):
        return [scope.ordinal]
    if isinstance(scope, MultiIntervalOrdinalReference):
        return scope.ordinals
    return []


def validate_grounded_operation_values(request: Any, user_text: str) -> None:
    """验证关键数值和整井范围来自当前轮；不判断数字对应的业务意图。"""

    from cnlc_agent.demo.operation_interaction import ClarificationReplyRequest, PlanRequest

    numbers = extract_grounded_numbers(user_text)
    values: list[float] = []
    ordinals: list[int] = []
    write_scopes: list[Any] = []
    if isinstance(request, PlanRequest):
        ordinals.extend(_scope_ordinals(request.plan.shared_context.scope))
        if any(op.action == ActionType.MODIFY_PARAMETER for op in request.plan.operations):
            write_scopes.append(request.plan.shared_context.scope)
        for operation in request.plan.operations:
            value = operation.parameters.value
            if value is not None and value.mode == "ABSOLUTE":
                values.append(value.value)
            ordinals.extend(_scope_ordinals(operation.scope))
            if operation.action == ActionType.MODIFY_PARAMETER:
                write_scopes.append(operation.scope)
    elif isinstance(request, ClarificationReplyRequest):
        value = request.patch.value
        if value is not None and value.mode == "ABSOLUTE":
            values.append(value.value)
        ordinals.extend(_scope_ordinals(request.patch.scope))
        write_scopes.append(request.patch.scope)
    if any(isinstance(scope, WholeWellScope) for scope in write_scopes) and not (
        _WHOLE_WELL_WORDING.search(user_text)
    ):
        raise UngroundedWriteScope("whole-well scope is not stated in the current user turn")
    if any(float(ordinal) not in numbers for ordinal in ordinals):
        raise UngroundedScopeReference("scope ordinal is not stated in this turn")
    if any(value not in numbers for value in values):
        raise ValueError("operation numbers are not grounded in this turn")


def validate_grounded_authority_references(request: Any, user_text: str) -> None:
    """只允许本轮明确表达的显式 ID / 序号进入业务引用解析。"""

    from cnlc_agent.demo.operation_interaction import PlanRequest, SetActiveContextRequest

    task_references = []
    execution_references = []
    if isinstance(request, PlanRequest):
        task_references.append(request.plan.shared_context.task_reference)
        execution_references.append(request.plan.shared_context.execution_reference)
        for operation in request.plan.operations:
            task_references.append(operation.task_reference)
            execution_references.append(operation.execution_reference)
    elif isinstance(request, SetActiveContextRequest):
        task_references.append(request.task_reference)
        execution_references.append(request.execution_reference)

    previous_task_requested = any(
        reference is not None and reference.kind == "PREVIOUS_TASK"
        for reference in task_references
    )
    for reference in task_references:
        if (
            reference is not None
            and reference.kind == "PREVIOUS_TASK"
            and not _PREVIOUS_TASK_WORDING.search(user_text)
        ):
            raise UngroundedAuthorityReference("previous task is not requested in this turn")
        if (
            reference is not None
            and reference.kind in {"WELL_ID", "TASK_ID"}
            and reference.value not in user_text
        ):
            raise UngroundedAuthorityReference("task reference is not stated in this turn")
    for reference in execution_references:
        if reference is None:
            continue
        if reference.execution_id is not None and reference.execution_id not in user_text:
            raise UngroundedAuthorityReference("execution id is not stated in this turn")
        if reference.sequence is not None and str(reference.sequence) not in user_text:
            raise UngroundedAuthorityReference("execution sequence is not stated in this turn")
        if reference.kind == "PREVIOUS" and not _PREVIOUS_VERSION_WORDING.search(user_text):
            raise UngroundedAuthorityReference("previous version is not requested in this turn")
        if (
            reference.kind == "LATEST_SUCCESSFUL"
            and not previous_task_requested
            and not _LATEST_SUCCESSFUL_WORDING.search(user_text)
        ):
            raise UngroundedAuthorityReference("latest successful version is not requested")


def raw_operation_mode(raw: str) -> str | None:
    """仅识别生命周期 mode，供无效 Schema 时决定是否保留既有 Pending。"""

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("request"), dict):
        return None
    mode = payload["request"].get("mode")
    return mode if isinstance(mode, str) else None


class InteractionStateMiddleware(MiddlewareBase):
    """每轮读取事实并发布受控上下文，旧澄清只允许紧邻下一轮使用。"""

    def __init__(self, runner: TaskCommandRunner) -> None:
        self.runner = runner

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """无关回复、断流和失败同样结束澄清有效窗口。"""

        current_turn_token = _CURRENT_TURN_MESSAGES.set(
            current_turn_messages(input_kwargs.get("inputs"))
        )
        intercepted = None
        turn_started = False
        try:
            self.runner.attach_session_runtime_context(agent.state.middle_context)
            self.runner.begin_interaction_turn()
            turn_started = True
            source = next_handler(**input_kwargs)
            try:
                async for event in source:
                    yield event
                    if isinstance(event, ToolResultEndEvent):
                        operation = event.metadata.get("operation")
                        payload = event.metadata.get("result", event.metadata)
                        if operation is not None and not operation.get("created_execution_ids"):
                            intercepted = operation
                            break
                        if (payload.get("error_code") and payload.get("message")) or payload.get(
                            "command"
                        ) in {
                            "STATUS",
                            "GET_REPORT",
                        }:
                            intercepted = payload
                            break
            finally:
                await source.aclose()
            if intercepted is not None:
                # 裁决后立即给出固定文案，不再由模型反复重试或改写澄清意图。
                block_id = uuid4().hex
                reply_id = agent.state.reply_id
                events: list[AgentEvent] = [
                    TextBlockStartEvent(reply_id=reply_id, block_id=block_id),
                    TextBlockDeltaEvent(
                        reply_id=reply_id,
                        block_id=block_id,
                        delta=render_operation_result(intercepted),
                    ),
                    TextBlockEndEvent(reply_id=reply_id, block_id=block_id),
                    ReplyEndEvent(session_id=agent.state.session_id, reply_id=reply_id),
                ]
                for event in events:
                    if agent.state.context and agent.state.context[-1].id == reply_id:
                        agent.state.context[-1].append_event(event)
                    yield event
                if agent.state.context and agent.state.context[-1].id == reply_id:
                    yield agent.state.context[-1]
        finally:
            try:
                if turn_started:
                    self.runner.end_interaction_turn()
            finally:
                _CURRENT_TURN_MESSAGES.reset(current_turn_token)

    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Any],
    ) -> Any:
        """若压缩丢弃或切分本轮输入，在请求内将原消息恢复到上下文尾部。"""

        current = _CURRENT_TURN_MESSAGES.get()
        if not current:
            return await next_handler(**input_kwargs)
        messages = list(input_kwargs["messages"])
        for source in current:
            context_index = next(
                (
                    index
                    for index, message in enumerate(agent.state.context)
                    if message.id == source.id
                ),
                None,
            )
            current_copy = deepcopy(source)
            if context_index is None:
                agent.state.context.append(current_copy)
            else:
                agent.state.context[context_index] = current_copy

            model_index = next(
                (index for index, message in enumerate(messages) if message.id == source.id),
                None,
            )
            model_copy = deepcopy(source)
            if model_index is None:
                messages.append(model_copy)
            else:
                messages[model_index] = model_copy

        return await next_handler(**{**input_kwargs, "messages": messages})

    async def on_acting(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """模型一次提出多个写动作时先拒绝整批，避免先写入后发现冲突。"""

        calls = [
            block
            for message in agent.state.context[-1:]
            for block in message.content
            if isinstance(block, ToolCallBlock)
        ]
        # AgentScope 的 Schema 参数清理可能删掉未知字段；先校验原始调用，
        # 防止局部范围或错误嵌套被静默丢弃后变成另一项合法操作。
        from cnlc_agent.demo.operation_tool import OPERATION_TOOL_NAME, OperationToolInput

        pending_before_validation = self.runner.pending_operation_clarification()
        invalid_operation_mode = None
        try:
            for call in calls:
                if call.name == OPERATION_TOOL_NAME:
                    invalid_operation_mode = raw_operation_mode(call.input)
                    request = OperationToolInput.model_validate_json(call.input).request
                    user_text = current_turn_text()
                    validate_grounded_authority_references(request, user_text)
                    validate_grounded_operation_values(request, user_text)
        except UngroundedWriteScope:
            if pending_before_validation is not None and invalid_operation_mode not in {
                "PLAN",
                "CANCEL",
                "SET_ACTIVE_CONTEXT",
            }:
                self.runner.retain_operation_clarification()
            else:
                self.runner.clear_operation_clarification()
            chunk = interaction_chunk(
                "CLARIFICATION_REQUIRED",
                "请明确本次修改范围（如全井或具体层段）；系统不会根据模型候选范围扩大写入。",
            )
            yield ToolResponse(
                id=input_kwargs["tool_call"].id,
                content=chunk.content,
                state=ToolResultState.ERROR,
                metadata=chunk.metadata,
            )
            return
        except UngroundedAuthorityReference:
            if pending_before_validation is not None and invalid_operation_mode not in {
                "PLAN",
                "CANCEL",
                "SET_ACTIVE_CONTEXT",
            }:
                self.runner.retain_operation_clarification()
            else:
                self.runner.clear_operation_clarification()
            chunk = interaction_chunk(
                "CLARIFICATION_REQUIRED",
                "请在本轮明确任务或版本引用；历史摘要不能单独指定操作对象。",
            )
            yield ToolResponse(
                id=input_kwargs["tool_call"].id,
                content=chunk.content,
                state=ToolResultState.ERROR,
                metadata=chunk.metadata,
            )
            return
        except UngroundedScopeReference:
            if pending_before_validation is not None and invalid_operation_mode not in {
                "PLAN",
                "CANCEL",
                "SET_ACTIVE_CONTEXT",
            }:
                self.runner.retain_operation_clarification()
            else:
                self.runner.clear_operation_clarification()
            chunk = interaction_chunk(
                "CLARIFICATION_REQUIRED",
                "请在本轮明确层号或深度范围；历史摘要不能单独恢复操作范围。",
            )
            yield ToolResponse(
                id=input_kwargs["tool_call"].id,
                content=chunk.content,
                state=ToolResultState.ERROR,
                metadata=chunk.metadata,
            )
            return
        except (ValidationError, ValueError, TypeError):
            if pending_before_validation is not None and invalid_operation_mode not in {
                "PLAN",
                "CANCEL",
                "SET_ACTIVE_CONTEXT",
            }:
                # 用户已经回应，但模型的 Patch 无效；续留服务端计划供下一轮重答。
                self.runner.retain_operation_clarification()
            else:
                self.runner.clear_operation_clarification()
            chunk = interaction_chunk(
                "INVALID_OPERATION_PLAN", "操作结构无效，请明确任务、目标、范围和修改值。"
            )
            yield ToolResponse(
                id=input_kwargs["tool_call"].id,
                content=chunk.content,
                state=ToolResultState.ERROR,
                metadata=chunk.metadata,
            )
            return
        writes = [
            call
            for call in calls
            if call.name
            in {
                "run_well_interpretation",
                "modify_well_interpretation",
                "rerun_well_interpretation",
                "interpret_interpretation_operation",
            }
        ]
        if (
            (writes and self.runner.operation_request_used)
            or len(writes) > 1
            or (
                writes
                and any(call.name == "request_interpretation_clarification" for call in calls)
            )
        ):
            decision = InteractionPolicy.decide(InteractionSnapshot(), "MODIFY", conflict=True)
            chunk = interaction_chunk(
                decision.error_code or "CLARIFICATION_REQUIRED", decision.message
            )
            self.runner.clear_pending()
            self.runner.clear_operation_clarification()
            yield ToolResponse(
                id=input_kwargs["tool_call"].id,
                content=chunk.content,
                state=ToolResultState.ERROR,
                metadata=chunk.metadata,
            )
            return
        if writes:
            self.runner.operation_request_used = True
        async for event in next_handler(**input_kwargs):
            yield event

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        """动态快照不是聊天摘要；每次推理仍以仓库状态为准。"""

        snapshot = await self.runner.interaction_snapshot()
        payload = snapshot.model_dump(mode="json", exclude={"pending_clarification"})
        payload["interaction_context"] = self.runner.interaction_context.model_dump(mode="json")
        pending = self.runner.pending_operation_clarification()
        payload["pending_operation_clarification"] = (
            {
                "operations": [
                    {"operation_id": op.operation_id, "action": op.action, "target": op.target}
                    for op in pending.partial_plan.operations
                ],
                "issues": [issue.model_dump(mode="json") for issue in pending.issues],
            }
            if pending
            else None
        )
        return (
            current_prompt
            + "\n当前可信交互快照（不得由聊天记忆覆盖）：\n"
            + json.dumps(payload, ensure_ascii=False)
        )


def render_operation_result(payload: dict[str, Any]) -> str:
    """业务读取沿用真实报告/状态 renderer；交互结果只显示服务器安全文案。"""
    if "task_results" not in payload:
        return render_interaction_result(payload)
    if payload["task_results"]:
        return "\n\n".join(render_interaction_result(item) for item in payload["task_results"])
    return payload.get("message") or "本次请求没有产生新的执行。"
