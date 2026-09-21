"""A bounded Demo reply using official AgentScope Tool and streaming event protocols."""

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from typing import Any
from uuid import uuid4

from agentscope.agent import Agent
from agentscope.event import (
    AgentEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import AssistantMsg, Msg, TextBlock, ToolCallBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse
from agentscope.types import ReplyFinishedReason

from cnlc_agent.demo.tools import RUN_TOOL_NAME, RunWellInterpretationTool
from cnlc_agent.demo.uploads import UploadError, parse_upload
from cnlc_agent.domain.models import JsonObject
from cnlc_agent.infrastructure.telemetry import event_observer


def _progress(name: str, attributes: JsonObject) -> str | None:
    """Only forward bounded status fields, never raw model errors or trace payloads."""
    step = attributes.get("step_id")
    if step not in {f"W{i:02}" for i in range(1, 11)}:
        step = None
    if name == "workflow.step.start" and step:
        return f"- {step} {attributes.get('step_name', '')}：正在处理\n"
    if name == "state.change" and step:
        status = attributes.get("status")
        return f"- {step}：{status}\n"
    if name == "report.start":
        return "\n正在生成报告……\n"
    return None


class UploadInterpretationReply(MiddlewareBase):
    """Map one current-turn upload to one business Tool call, without an extra LLM rewrite."""

    def __init__(self, tool: RunWellInterpretationTool) -> None:
        self.tool = tool

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[AgentEvent | Msg, None]:
        del next_handler
        inputs = input_kwargs.get("inputs")
        messages = (
            [inputs] if isinstance(inputs, Msg) else inputs if isinstance(inputs, list) else []
        )
        reply_id, block_id = uuid4().hex, uuid4().hex
        agent.state.reply_id = reply_id
        reply = AssistantMsg(id=reply_id, name=agent.name, content=[])
        async for event in self._events(agent, messages, reply_id, block_id):
            reply.append_event(event)
            yield event
        # Binary files remain in official service message storage, outside model context.
        agent.state.context.append(reply)
        yield reply

    async def _events(
        self, agent: Agent, messages: list[Msg], reply_id: str, block_id: str
    ) -> AsyncGenerator[AgentEvent, None]:
        session_id = agent.state.session_id
        yield ReplyStartEvent(session_id=session_id, reply_id=reply_id, name=agent.name)
        yield TextBlockStartEvent(reply_id=reply_id, block_id=block_id)
        try:
            fixture, instruction = parse_upload(messages)
        except UploadError as exc:
            yield TextBlockDeltaEvent(reply_id=reply_id, block_id=block_id, delta=str(exc))
            yield TextBlockEndEvent(reply_id=reply_id, block_id=block_id)
            yield ReplyEndEvent(session_id=session_id, reply_id=reply_id)
            return
        yield TextBlockDeltaEvent(
            reply_id=reply_id,
            block_id=block_id,
            delta="正在读取井资料……\n\n正在检查数据……\n\n"
            "Demo / Mock：使用上传资料中的预设专业结果，不能作为真实测井解释结论。\n\n",
        )
        self.tool.upload = fixture, instruction
        call = ToolCallBlock(
            id=uuid4().hex,
            name=RUN_TOOL_NAME,
            input=json.dumps({"well_id": fixture.well.well_id}),
        )
        yield ToolCallStartEvent(reply_id=reply_id, tool_call_id=call.id, tool_call_name=call.name)
        yield ToolCallDeltaEvent(reply_id=reply_id, tool_call_id=call.id, delta=call.input)
        yield ToolCallEndEvent(reply_id=reply_id, tool_call_id=call.id)
        yield ToolResultStartEvent(
            reply_id=reply_id, tool_call_id=call.id, tool_call_name=call.name
        )
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def observe(name: str, attributes: JsonObject) -> None:
            text = _progress(name, attributes)
            if text:
                queue.put_nowait(("progress", text))

        async def execute() -> None:
            token = event_observer.set(observe)
            try:
                async for chunk in agent.toolkit.call_tool(call, agent.state):
                    await queue.put(("tool", chunk))
            finally:
                event_observer.reset(token)
                await queue.put(("done", None))

        task = asyncio.create_task(execute())
        result: ToolResponse | None = None
        interrupted = False
        try:
            while True:
                kind, value = await queue.get()
                if kind == "done":
                    await task
                    break
                if kind == "progress":
                    yield TextBlockDeltaEvent(reply_id=reply_id, block_id=block_id, delta=value)
                elif isinstance(value, ToolResponse):
                    result = value
                else:
                    for block in value.content:
                        if isinstance(block, TextBlock):
                            yield ToolResultTextDeltaEvent(
                                reply_id=reply_id, tool_call_id=call.id, delta=block.text
                            )
        except asyncio.CancelledError:
            interrupted = True
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.tool.upload = None

        yield ToolResultEndEvent(
            reply_id=reply_id,
            tool_call_id=call.id,
            state=(
                ToolResultState.INTERRUPTED
                if interrupted
                else result.state
                if result
                else ToolResultState.ERROR
            ),
            metadata=result.metadata if result else {},
        )
        yield TextBlockEndEvent(reply_id=reply_id, block_id=block_id)
        report_id = uuid4().hex
        payload = result.metadata.get("result", {}) if result else {}
        report = payload.get("report_markdown") or (
            "解释已中断。" if interrupted else "解释任务失败，请检查井资料或服务配置后重试。"
        )
        yield TextBlockStartEvent(reply_id=reply_id, block_id=report_id)
        yield TextBlockDeltaEvent(reply_id=reply_id, block_id=report_id, delta=report)
        yield TextBlockEndEvent(reply_id=reply_id, block_id=report_id)
        yield ReplyEndEvent(
            session_id=session_id,
            reply_id=reply_id,
            finished_reason=(
                ReplyFinishedReason.INTERRUPTED if interrupted else ReplyFinishedReason.COMPLETED
            ),
        )
