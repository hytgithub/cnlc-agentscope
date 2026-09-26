"""使用官方 AgentScope Tool 与事件协议生成边界清晰的 Demo 流式回复。"""

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
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

from cnlc_agent.demo.progress import ExecutionProgressProjector
from cnlc_agent.demo.tools import RUN_TOOL_NAME, RunWellInterpretationTool
from cnlc_agent.demo.uploads import UploadError, has_attachment, parse_upload
from cnlc_agent.domain.models import JsonObject
from cnlc_agent.infrastructure.telemetry import event_observer

_REPORT_SECTION_PATTERN = re.compile(r"(?=^#{1,3} )", re.MULTILINE)


def _report_chunks(markdown: str) -> list[str]:
    """按 Markdown 标题切分确定性报告，只改善流式体验而不改写文本。"""
    chunks = [chunk for chunk in _REPORT_SECTION_PATTERN.split(markdown) if chunk]
    return chunks or [markdown]


class UploadInterpretationReply(MiddlewareBase):
    """把本轮上传映射为一次业务 Tool 调用，不再交给外层 LLM 改写报告。"""

    def __init__(
        self,
        tool: RunWellInterpretationTool,
        *,
        step_delay_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if step_delay_seconds < 0:
            raise ValueError("步骤展示间隔不能为负数")
        self.tool = tool
        self.step_delay_seconds = step_delay_seconds
        self._sleep = sleep

    async def _pace_step(self) -> None:
        """仅控制 SSE 展示节奏，不阻塞后台 Workflow 或修改业务状态。"""

        if self.step_delay_seconds > 0:
            await self._sleep(self.step_delay_seconds)

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
        """提交后台任务后继续消费真实事件，直到本轮 Execution 报告已持久化。"""

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
            delta=f"开始解释井 {fixture.well.well_id}\n\n正在读取并校验上传资料……\n\n"
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
        # Tool 提交与后台事件共用当前 ContextVar；队列仅串行化本轮 SSE 展示。
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        projector = ExecutionProgressProjector()

        def observe(name: str, attributes: JsonObject) -> None:
            """把当前 Worker 的既有 Telemetry 转成有限的安全文本。"""

            ended_steps_before = len(projector.ended_steps)
            for text in projector.project(name, attributes):
                queue.put_nowait(("progress", text))
            # 每个真实步骤终态后插入展示节拍；队列顺序保证完成文案先于等待出现。
            if len(projector.ended_steps) > ended_steps_before:
                queue.put_nowait(("step_completed", None))

        async def execute() -> None:
            """在请求级 ContextVar 中安装观察器并执行 Tool。"""

            token = event_observer.set(observe)
            try:
                async for chunk in agent.toolkit.call_tool(call, agent.state):
                    await queue.put(("tool", chunk))
            finally:
                event_observer.reset(token)
                await queue.put(("submission_done", None))

        submission_task = asyncio.create_task(execute())
        completion_task: asyncio.Task[Any] | None = None
        queue_task: asyncio.Task[tuple[str, Any]] | None = None
        result: ToolResponse | None = None
        buffered_progress: list[tuple[str, Any]] = []
        try:
            # 先完整取得 QUEUED Tool Result，让 Panel 尽早获得 task/execution 标识。
            while True:
                kind, value = await queue.get()
                if kind == "submission_done":
                    await submission_task
                    break
                if kind in {"progress", "step_completed"}:
                    buffered_progress.append((kind, value))
                elif isinstance(value, ToolResponse):
                    result = value
                else:
                    for block in value.content:
                        if isinstance(block, TextBlock):
                            yield ToolResultTextDeltaEvent(
                                reply_id=reply_id, tool_call_id=call.id, delta=block.text
                            )
            yield ToolResultEndEvent(
                reply_id=reply_id,
                tool_call_id=call.id,
                state=result.state if result else ToolResultState.ERROR,
                metadata=result.metadata if result else {},
            )
            for kind, value in buffered_progress:
                if kind == "progress":
                    yield ThinkingBlockDeltaEvent(
                        reply_id=reply_id, block_id=block_id, delta=value
                    )
                else:
                    await self._pace_step()

            payload = result.metadata.get("result", {}) if result else {}
            task_id = payload.get("task_id")
            execution_id = payload.get("execution_id")
            completed = None
            completion_error = False
            if (
                result is not None
                and result.state == ToolResultState.SUCCESS
                and isinstance(task_id, str)
                and isinstance(execution_id, str)
            ):
                completion_task = asyncio.create_task(
                    self.tool.wait_for_execution_completion(task_id, execution_id)
                )
                try:
                    while True:
                        if completion_task.done():
                            break
                        queue_task = asyncio.create_task(queue.get())
                        done, _ = await asyncio.wait(
                            {completion_task, queue_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if queue_task in done:
                            kind, value = queue_task.result()
                            queue_task = None
                            if kind == "progress":
                                yield ThinkingBlockDeltaEvent(
                                    reply_id=reply_id, block_id=block_id, delta=value
                                )
                            elif kind == "step_completed":
                                await self._pace_step()
                        else:
                            queue_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await queue_task
                            queue_task = None
                    completed = await completion_task
                except asyncio.CancelledError:
                    raise
                except Exception:
                    completion_error = True
                # Worker 结束后观察器已经同步放入所有最终事件；必须先排空再收尾。
                while not queue.empty():
                    kind, value = queue.get_nowait()
                    if kind == "progress":
                        yield ThinkingBlockDeltaEvent(
                            reply_id=reply_id, block_id=block_id, delta=value
                        )
                    elif kind == "step_completed":
                        await self._pace_step()

            final_text = "解释任务失败，请检查井资料或服务配置后重试。"
            report: str | None = None
            if completed is not None:
                status = completed.execution_status.value
                if status in {"SUCCESS", "WARNING"}:
                    if status == "WARNING":
                        yield ThinkingBlockDeltaEvent(
                            reply_id=reply_id,
                            block_id=block_id,
                            delta="\n⚠ 本次解释完成，但存在告警\n",
                        )
                    try:
                        report_result = await self.tool.get_execution_report(
                            completed.task_id, completed.execution_id
                        )
                        report = report_result.report_markdown
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        completion_error = True
                    final_text = "解释报告暂不可用，请在右侧任务面板查看当前执行状态。"
                elif status == "BLOCKED":
                    yield ThinkingBlockDeltaEvent(
                        reply_id=reply_id,
                        block_id=block_id,
                        delta="\n解释流程已停止：缺少后续处理所需资料\n",
                    )
                    final_text = "解释流程已停止：缺少后续处理所需资料。"
                elif status == "REVIEW_REQUIRED":
                    yield ThinkingBlockDeltaEvent(
                        reply_id=reply_id,
                        block_id=block_id,
                        delta="\n解释流程已进入人工复核\n",
                    )
                    final_text = "解释流程已进入人工复核，请在任务面板查看执行事实。"
                else:
                    code = completed.error_code or "INTERPRETATION_FAILED"
                    yield ThinkingBlockDeltaEvent(
                        reply_id=reply_id,
                        block_id=block_id,
                        delta=f"\n✗ 解释执行失败（错误代码：{code}）\n",
                    )
                    final_text = f"解释执行失败（错误代码：{code}），请查看任务面板。"
            elif completion_error:
                yield ThinkingBlockDeltaEvent(
                    reply_id=reply_id,
                    block_id=block_id,
                    delta="\n✗ 无法读取解释执行终态\n",
                )

            yield ThinkingBlockEndEvent(reply_id=reply_id, block_id=block_id)
            report_id = uuid4().hex
            yield TextBlockStartEvent(reply_id=reply_id, block_id=report_id)
            for chunk in _report_chunks(report or final_text):
                yield TextBlockDeltaEvent(reply_id=reply_id, block_id=report_id, delta=chunk)
            yield TextBlockEndEvent(reply_id=reply_id, block_id=report_id)
            yield ReplyEndEvent(
                session_id=session_id,
                reply_id=reply_id,
                finished_reason=ReplyFinishedReason.COMPLETED,
            )
        finally:
            # 这里只取消 SSE 观察者自己的等待；dispatcher.wait 的 shield 保证 Worker 继续。
            for local_task in (queue_task, completion_task, submission_task):
                if local_task is not None and not local_task.done():
                    local_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await local_task
            self.tool.upload = None
