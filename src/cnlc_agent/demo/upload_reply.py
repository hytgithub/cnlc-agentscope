"""使用官方 AgentScope Tool 与事件协议生成边界清晰的 Demo 流式回复。"""

import asyncio
import json
import re
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

from cnlc_agent.demo.tools import RUN_TOOL_NAME, RunWellInterpretationTool
from cnlc_agent.demo.uploads import UploadError, has_attachment, parse_upload
from cnlc_agent.domain.models import JsonObject
from cnlc_agent.infrastructure.telemetry import event_observer

_REPORT_SECTION_PATTERN = re.compile(r"(?=^#{1,3} )", re.MULTILINE)


def _progress(name: str, attributes: JsonObject) -> str | None:
    """只转发受控状态字段，不把模型错误原文或完整 Trace 发到前端。"""
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


def _report_chunks(markdown: str) -> list[str]:
    """按 Markdown 标题切分确定性报告，只改善流式体验而不改写文本。"""
    chunks = [chunk for chunk in _REPORT_SECTION_PATTERN.split(markdown) if chunk]
    return chunks or [markdown]


class UploadInterpretationReply(MiddlewareBase):
    """把本轮上传映射为一次业务 Tool 调用，不再交给外层 LLM 改写报告。"""

    def __init__(self, tool: RunWellInterpretationTool) -> None:
        self.tool = tool

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
        """按 Reply、Thinking、Tool、Report 的顺序产生官方流式事件。"""

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
        # Tool 与进度观察器并发运行，通过队列统一串行化为 SSE 事件。
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def observe(name: str, attributes: JsonObject) -> None:
            """把当前请求的 Workflow 事件转换为有限的思考区进度。"""

            text = _progress(name, attributes)
            if text:
                queue.put_nowait(("progress", text))

        async def execute() -> None:
            """在请求级 ContextVar 中安装观察器并执行 Tool。"""

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
                    yield ThinkingBlockDeltaEvent(
                        reply_id=reply_id,
                        block_id=block_id,
                        delta=value,
                    )
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
            # 无论成功、失败还是取消，都必须清理后台任务和本轮上传引用。
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
        yield ThinkingBlockEndEvent(reply_id=reply_id, block_id=block_id)
        report_id = uuid4().hex
        payload = result.metadata.get("result", {}) if result else {}
        # 报告来自业务 Tool 的确定性结果；缺失时只返回稳定诊断文案。
        report = payload.get("report_markdown") or (
            "解释已中断。" if interrupted else "解释任务失败，请检查井资料或服务配置后重试。"
        )
        yield TextBlockStartEvent(reply_id=reply_id, block_id=report_id)
        for chunk in _report_chunks(report):
            yield TextBlockDeltaEvent(reply_id=reply_id, block_id=report_id, delta=chunk)
        yield TextBlockEndEvent(reply_id=reply_id, block_id=report_id)
        yield ReplyEndEvent(
            session_id=session_id,
            reply_id=reply_id,
            finished_reason=(
                ReplyFinishedReason.INTERRUPTED if interrupted else ReplyFinishedReason.COMPLETED
            ),
        )
