"""将上传资料映射为首次解释，并复用统一 Execution 流式投影。"""

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
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
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
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

from cnlc_agent.demo.execution_stream import ExecutionReplyStreamer, is_streaming_execution
from cnlc_agent.demo.tools import RUN_TOOL_NAME, RunWellInterpretationTool
from cnlc_agent.demo.uploads import UploadError, has_attachment, parse_upload
from cnlc_agent.infrastructure.telemetry import event_observer


class UploadInterpretationReply(MiddlewareBase):
    """上传层只解析附件并创建 START，后续执行统一交给 Streamer。"""

    def __init__(
        self,
        tool: RunWellInterpretationTool,
        *,
        streamer: ExecutionReplyStreamer | None = None,
        step_delay_seconds: float = 1.0,
        report_chunk_delay_seconds: float = 0.12,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.tool = tool
        self.streamer = streamer or ExecutionReplyStreamer(
            tool.wait_for_execution_completion,
            tool.get_execution_report,
            step_delay_seconds=step_delay_seconds,
            report_chunk_delay_seconds=report_chunk_delay_seconds,
            sleep=sleep,
        )

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[AgentEvent | Msg, None]:
        """构造并持久化完整 AssistantMsg，同时逐事件推送给前端。"""

        inputs = input_kwargs.get("inputs")
        messages = (
            [inputs] if isinstance(inputs, Msg) else inputs if isinstance(inputs, list) else []
        )
        if not has_attachment(messages):
            async for event in next_handler(**input_kwargs):
                yield event
            return
        reply_id, block_id = uuid4().hex, uuid4().hex
        agent.state.reply_id = reply_id
        reply = AssistantMsg(id=reply_id, name=agent.name, content=[])
        async for event in self._events(agent, messages, reply_id, block_id):
            reply.append_event(event)
            yield event
        # 二进制附件留在官方消息存储中，不进入模型上下文，避免上下文膨胀和数据泄漏。
        agent.state.context.append(reply)
        yield reply

    async def _events(
        self, agent: Agent, messages: list[Msg], reply_id: str, block_id: str
    ) -> AsyncGenerator[AgentEvent, None]:
        """提交 START 后把 task_id/execution_id 交给公共 Streamer。"""

        session_id = agent.state.session_id
        yield ReplyStartEvent(session_id=session_id, reply_id=reply_id, name=agent.name)
        try:
            fixture, instruction = parse_upload(messages)
        except UploadError as exc:
            yield TextBlockStartEvent(reply_id=reply_id, block_id=block_id)
            yield TextBlockDeltaEvent(reply_id=reply_id, block_id=block_id, delta=str(exc))
            yield TextBlockEndEvent(reply_id=reply_id, block_id=block_id)
            yield ReplyEndEvent(session_id=session_id, reply_id=reply_id)
            return

        yield ThinkingBlockStartEvent(reply_id=reply_id, block_id=block_id)
        yield ThinkingBlockDeltaEvent(
            reply_id=reply_id,
            block_id=block_id,
            delta=f"开始解释井 {fixture.well.well_id}\n\n✓ 上传资料读取与校验完成\n\n"
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

        buffer = self.streamer.new_buffer()
        result: ToolResponse | None = None
        token = event_observer.set(buffer.observe)
        try:
            async for chunk in agent.toolkit.call_tool(call, agent.state):
                if isinstance(chunk, ToolResponse):
                    result = chunk
                else:
                    for content in chunk.content:
                        if isinstance(content, TextBlock):
                            yield ToolResultTextDeltaEvent(
                                reply_id=reply_id,
                                tool_call_id=call.id,
                                delta=content.text,
                            )
        finally:
            event_observer.reset(token)
            self.tool.upload = None

        yield ToolResultEndEvent(
            reply_id=reply_id,
            tool_call_id=call.id,
            state=result.state if result else ToolResultState.ERROR,
            metadata=result.metadata if result else {},
        )
        payload = result.metadata.get("result") if result else None
        if (
            result is not None
            and result.state == ToolResultState.SUCCESS
            and is_streaming_execution(payload)
        ):
            async for event in self.streamer.stream(
                session_id=session_id,
                reply_id=reply_id,
                payload=payload,
                buffer=buffer,
                thinking_block_id=block_id,
                thinking_started=True,
            ):
                yield event
            return

        yield ThinkingBlockDeltaEvent(
            reply_id=reply_id,
            block_id=block_id,
            delta="\n✗ 解释任务提交失败\n",
        )
        yield ThinkingBlockEndEvent(reply_id=reply_id, block_id=block_id)
        error_id = uuid4().hex
        yield TextBlockStartEvent(reply_id=reply_id, block_id=error_id)
        yield TextBlockDeltaEvent(
            reply_id=reply_id,
            block_id=error_id,
            delta="解释任务失败，请检查井资料或服务配置后重试。",
        )
        yield TextBlockEndEvent(reply_id=reply_id, block_id=error_id)
        yield ReplyEndEvent(
            session_id=session_id,
            reply_id=reply_id,
            finished_reason=ReplyFinishedReason.COMPLETED,
        )
