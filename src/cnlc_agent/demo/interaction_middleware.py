"""在 AgentScope 请求生命周期管理短期澄清，不另行调用意图模型。"""

import json
import re
from collections.abc import AsyncGenerator, Callable
from typing import Any
from uuid import uuid4

from agentscope.agent import Agent
from agentscope.event import (
    AgentEvent,
    ModelCallStartEvent,
    ReplyEndEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse
from pydantic import ValidationError

from cnlc_agent.demo.interaction_state import (
    InteractionPolicy,
    InteractionSnapshot,
    render_interaction_result,
)
from cnlc_agent.demo.response_renderer import render_response_evidence
from cnlc_agent.demo.task_tools import TaskCommandRunner, interaction_chunk
from cnlc_agent.domain.response_evidence import (
    ResponseEvidenceEnvelope,
    response_envelope_from_task_result,
)

_NUMBER_PATTERN = re.compile(r"(?<![\d.])([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(%)?")


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
    """验证关键数值来自当前轮；不判断数字对应的业务意图。"""

    from cnlc_agent.demo.operation_interaction import ClarificationReplyRequest, PlanRequest

    numbers = extract_grounded_numbers(user_text)
    values: list[float] = []
    ordinals: list[int] = []
    if isinstance(request, PlanRequest):
        ordinals.extend(_scope_ordinals(request.plan.shared_context.scope))
        for operation in request.plan.operations:
            value = operation.parameters.value
            if value is not None and value.mode == "ABSOLUTE":
                values.append(value.value)
            ordinals.extend(_scope_ordinals(operation.scope))
    elif isinstance(request, ClarificationReplyRequest):
        value = request.patch.value
        if value is not None and value.mode == "ABSOLUTE":
            values.append(value.value)
        ordinals.extend(_scope_ordinals(request.patch.scope))
    if any(value not in numbers for value in values) or any(
        float(ordinal) not in numbers for ordinal in ordinals
    ):
        raise ValueError("operation numbers are not grounded in this turn")


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

        self.runner.attach_session_runtime_context(agent.state.middle_context)
        self.runner.begin_interaction_turn()
        source = next_handler(**input_kwargs)
        intercepted = None
        response_evidence_seen = False
        model_called = False
        buffered_text_events: list[Any] = []
        try:
            try:
                async for event in source:
                    if isinstance(
                        event,
                        (TextBlockStartEvent, TextBlockDeltaEvent, TextBlockEndEvent),
                    ):
                        buffered_text_events.append(event)
                        continue
                    if isinstance(event, ReplyEndEvent):
                        if not response_evidence_seen and model_called:
                            # 无 Tool/Application evidence 的自由文本不能报告业务成功。
                            block_id = uuid4().hex
                            reply_id = agent.state.reply_id
                            safe_text = render_response_evidence(None)
                            safe_events = [
                                TextBlockStartEvent(reply_id=reply_id, block_id=block_id),
                                TextBlockDeltaEvent(
                                    reply_id=reply_id, block_id=block_id, delta=safe_text
                                ),
                                TextBlockEndEvent(reply_id=reply_id, block_id=block_id),
                            ]
                            context_message = (
                                agent.state.context[-1]
                                if agent.state.context and agent.state.context[-1].id == reply_id
                                else None
                            )
                            if context_message is not None:
                                # AgentScope 每轮文本存入同一 AssistantMsg；保留 Tool 审计块。
                                context_message.content = [
                                    block
                                    for block in context_message.content
                                    if not isinstance(block, TextBlock)
                                ] + [TextBlock(text=safe_text)]
                            for safe_event in safe_events:
                                yield safe_event
                        else:
                            for buffered_event in buffered_text_events:
                                yield buffered_event
                        buffered_text_events.clear()
                        yield event
                        continue
                    if isinstance(event, ModelCallStartEvent):
                        model_called = True
                    if (
                        isinstance(event, Msg)
                        and event.id == agent.state.reply_id
                        and not response_evidence_seen
                        and model_called
                    ):
                        safe_text = render_response_evidence(None)
                        event.content = [
                            block for block in event.content if not isinstance(block, TextBlock)
                        ] + [TextBlock(text=safe_text)]
                        if (
                            agent.state.context
                            and agent.state.context[-1].id == agent.state.reply_id
                        ):
                            agent.state.context[-1].content = list(event.content)
                    yield event
                    if isinstance(event, ToolResultEndEvent):
                        operation = event.metadata.get("operation")
                        payload = event.metadata.get("result", event.metadata)
                        response_data = operation or payload
                        if response_data.get("response_evidence"):
                            response_evidence_seen = True
                            result_command = response_data.get("command")
                            result_status = response_data.get("execution_status")
                            envelopes = response_data["response_evidence"]
                            operation_active = bool(
                                operation and operation.get("created_execution_ids")
                            ) and any(
                                item.get("status") in {"QUEUED", "RUNNING"} for item in envelopes
                            )
                            result_active = result_command in {
                                "START",
                                "MODIFY",
                                "FULL_RERUN",
                                "CONFIRM",
                            } and result_status in {"QUEUED", "RUNNING"}
                            active = operation_active or result_active
                            if not active:
                                intercepted = response_data
                                break
                        elif isinstance(payload, dict) and payload.get("command") in {
                            "START",
                            "MODIFY",
                            "FULL_RERUN",
                            "STATUS",
                            "GET_REPORT",
                            "CONFIRM",
                        }:
                            response_evidence_seen = True
                            if payload.get("execution_status") not in {"QUEUED", "RUNNING"}:
                                try:
                                    from cnlc_agent.application.commands import TaskCommandResult

                                    task_result = TaskCommandResult.model_validate(payload)
                                    intercepted = {
                                        "response_evidence": [
                                            response_envelope_from_task_result(
                                                task_result
                                            ).model_dump(mode="json")
                                        ]
                                    }
                                    break
                                except (TypeError, ValueError):
                                    pass
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
            self.runner.end_interaction_turn()

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
                    user_text = next(
                        (
                            msg.get_text_content() or ""
                            for msg in reversed(agent.state.context)
                            if msg.role == "user"
                        ),
                        "",
                    )
                    validate_grounded_operation_values(request, user_text)
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
    envelopes = payload.get("response_evidence")
    if isinstance(envelopes, list) and envelopes:
        return "\n\n".join(
            render_response_evidence(ResponseEvidenceEnvelope.model_validate(item))
            for item in envelopes
        )
    if payload.get("command") in {
        "START",
        "MODIFY",
        "FULL_RERUN",
        "STATUS",
        "GET_REPORT",
        "CONFIRM",
    }:
        from cnlc_agent.application.commands import TaskCommandResult

        try:
            task_result = TaskCommandResult.model_validate(payload)
        except (TypeError, ValueError):
            return "业务工具结果结构无效，本轮不能声明操作成功。"
        return render_response_evidence(response_envelope_from_task_result(task_result))
    if "task_results" not in payload:
        return render_interaction_result(payload)
    if payload["task_results"]:
        return "\n\n".join(render_interaction_result(item) for item in payload["task_results"])
    return payload.get("message") or "本次请求没有产生新的执行。"
