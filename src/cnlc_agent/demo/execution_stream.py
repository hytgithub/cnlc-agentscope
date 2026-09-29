"""将一个已创建的 Execution 投影为统一的 AgentScope 流式回复。"""

import asyncio
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, TypeGuard
from uuid import uuid4

from agentscope.agent import Agent
from agentscope.event import (
    AgentEvent,
    ReplyEndEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ThinkingBlockDeltaEvent,
    ThinkingBlockEndEvent,
    ThinkingBlockStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import Msg, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.types import ReplyFinishedReason

from cnlc_agent.application.commands import TaskCommandResult
from cnlc_agent.application.stage_orchestrator import StageProgress
from cnlc_agent.demo.progress import ExecutionProgressProjector
from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.models import JsonObject
from cnlc_agent.infrastructure.telemetry import event_observer
from cnlc_agent.workflows.interpretation_workflow import step_observability

_REPORT_SECTION_PATTERN = re.compile(r"(?=^#{1,3} )", re.MULTILINE)
_EXECUTION_COMMANDS = frozenset({"START", "MODIFY", "FULL_RERUN", "CONFIRM"})
_ACTIVE_STATUSES = frozenset({"QUEUED", "RUNNING"})
_PARAMETER_LABELS = {
    "sampling_interval": "采样间隔",
    "por": "孔隙度",
    "perm": "渗透率",
    "prediction_model": "预测模型",
}


def is_streaming_execution(payload: object) -> TypeGuard[dict[str, Any]]:
    """只接受任务 Tool 返回的创建型 Execution 事实。"""

    return (
        isinstance(payload, dict)
        and payload.get("command") in _EXECUTION_COMMANDS
        and payload.get("execution_status") in _ACTIVE_STATUSES
        and isinstance(payload.get("task_id"), str)
        and isinstance(payload.get("execution_id"), str)
    )


def _report_chunks(markdown: str) -> list[str]:
    """按 Markdown 标题切分确定性报告，只改善流式体验而不改写文本。"""

    chunks = [chunk for chunk in _REPORT_SECTION_PATTERN.split(markdown) if chunk]
    return chunks or [markdown]


@dataclass
class ExecutionEventBuffer:
    """串行保存本轮 Worker 的真实 Telemetry，供回复流消费。"""

    queue: asyncio.Queue[tuple[str, str | None]] = field(default_factory=asyncio.Queue)
    projector: ExecutionProgressProjector = field(default_factory=ExecutionProgressProjector)

    def observe(self, name: str, attributes: JsonObject) -> None:
        """投影有限的业务事件；自由文本、异常详情和模型输出不会进入 UI。"""

        ended_steps_before = len(self.projector.ended_steps)
        for message in self.projector.project(name, attributes):
            self.queue.put_nowait(("progress", message))
        if len(self.projector.ended_steps) > ended_steps_before:
            self.queue.put_nowait(("step_completed", None))


class ExecutionReplyStreamer:
    """等待指定 Execution，展示事实进度，并读取该 execution_id 的报告。"""

    def __init__(
        self,
        wait_for_completion: Callable[[str, str], Awaitable[TaskCommandResult]],
        get_report: Callable[[str, str], Awaitable[TaskCommandResult]],
        get_stage_progress: Callable[[str, str], Awaitable[StageProgress]] | None = None,
        *,
        step_delay_seconds: float = 1.0,
        report_chunk_delay_seconds: float = 0.12,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if step_delay_seconds < 0 or report_chunk_delay_seconds < 0:
            raise ValueError("流式展示间隔不能为负数")
        self._wait_for_completion = wait_for_completion
        self._get_report = get_report
        self._get_stage_progress = get_stage_progress
        self.step_delay_seconds = step_delay_seconds
        self.report_chunk_delay_seconds = report_chunk_delay_seconds
        self._sleep = sleep

    def new_buffer(self) -> ExecutionEventBuffer:
        """每次回复使用独立投影器，避免多个 Execution 互相去重。"""

        return ExecutionEventBuffer()

    async def _pace_step(self) -> None:
        if self.step_delay_seconds > 0:
            await self._sleep(self.step_delay_seconds)

    async def _pace_report_chunk(self) -> None:
        if self.report_chunk_delay_seconds > 0:
            await self._sleep(self.report_chunk_delay_seconds)

    async def _waiting_confirmation_text(self, task_id: str, execution_id: str) -> str:
        """从既有阶段结果投影生成聊天正文和可识别的确认动作。"""

        if self._get_stage_progress is None:
            return "当前阶段已完成，请确认并继续。"
        progress = await self._get_stage_progress(task_id, execution_id)
        result = progress.stage_result
        stage_names = {
            "DATA_DECODE": "数据解编",
            "PREPROCESS": "数据预处理",
            "INTERPRET": "智能处理",
            "REPORT": "报告生成",
        }
        stage = stage_names.get(
            progress.waiting_confirmation_stage.value
            if progress.waiting_confirmation_stage is not None
            else "",
            "当前阶段",
        )
        lines = [f"\n### {stage}阶段结果", f"井名：{progress.well_name or progress.well_id}"]
        if result is not None:
            lines.extend([f"**{result.headline}**", result.summary])
            if result.metrics:
                lines.append(
                    "指标："
                    + "；".join(f"{key}={value}" for key, value in result.metrics.items())
                )
            if result.items:
                lines.append("结果明细：" + "；".join(str(item) for item in result.items[:6]))
            if result.warnings:
                lines.append("告警：" + "；".join(result.warnings))
            if result.conflicts:
                lines.append("冲突：" + "；".join(result.conflicts))
            if result.missing_items:
                lines.append("缺失：" + "；".join(result.missing_items))
        action = "确认并生成报告" if stage == "报告生成" else "确认并继续"
        lines.append(f"\n[{action}]")
        lines.append(f"<!-- cnlc-stage-confirm:{'REPORT' if stage == '报告生成' else 'STAGE'} -->")
        return "\n".join(lines)

    @staticmethod
    def _intro(payload: dict[str, Any]) -> str:
        """仅展示 ToolResult 中已经存在的命令、参数和复用事实。"""

        command = payload.get("command")
        sequence = payload.get("execution_sequence")
        if command == "MODIFY":
            lines = [f"\n开始重新解释：Execution #{sequence}\n"]
        elif command == "FULL_RERUN":
            lines = [f"\n开始全量重新解释：Execution #{sequence}\n"]
        else:
            lines = []

        override = payload.get("effective_override")
        if command == "MODIFY" and isinstance(override, dict):
            parameters = [
                f"  {_PARAMETER_LABELS[key]}：{value}"
                for key, value in override.items()
                if key in _PARAMETER_LABELS and value is not None
            ]
            if parameters:
                lines.extend(["\n本次有效参数：", *parameters, ""])

        reused = payload.get("reused_steps")
        if command == "MODIFY" and isinstance(reused, list) and reused:
            lines.append("\n复用已有结果：")
            for raw_step in reused:
                try:
                    step = StepId(raw_step)
                except (TypeError, ValueError):
                    continue
                name = step_observability(step).get("step_name", step.value)
                lines.append(f"  ✓ {step.value} {name}已复用")
            lines.append("")
        return "\n".join(lines)

    async def _drain_item(
        self, reply_id: str, block_id: str, item: tuple[str, str | None]
    ) -> AsyncGenerator[AgentEvent, None]:
        kind, value = item
        if kind == "progress" and value is not None:
            yield ThinkingBlockDeltaEvent(reply_id=reply_id, block_id=block_id, delta=value)
        elif kind == "step_completed":
            await self._pace_step()

    async def stream(
        self,
        *,
        session_id: str,
        reply_id: str,
        payload: dict[str, Any],
        buffer: ExecutionEventBuffer,
        thinking_block_id: str | None = None,
        thinking_started: bool = False,
    ) -> AsyncGenerator[AgentEvent, None]:
        """从 QUEUED/RUNNING 持续输出，直到同一 Execution 的报告或安全终态。"""

        block_id = thinking_block_id or uuid4().hex
        if not thinking_started:
            yield ThinkingBlockStartEvent(reply_id=reply_id, block_id=block_id)
        intro = self._intro(payload)
        if intro:
            yield ThinkingBlockDeltaEvent(reply_id=reply_id, block_id=block_id, delta=intro)

        task_id = str(payload["task_id"])
        execution_id = str(payload["execution_id"])
        completion_task: asyncio.Future[Any] = asyncio.ensure_future(
            self._wait_for_completion(task_id, execution_id)
        )
        queue_task: asyncio.Task[tuple[str, str | None]] | None = None
        completed: TaskCommandResult | None = None
        completion_error = False
        try:
            while True:
                if completion_task.done():
                    break
                queue_task = asyncio.create_task(buffer.queue.get())
                done, _ = await asyncio.wait(
                    {completion_task, queue_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if queue_task in done:
                    async for event in self._drain_item(reply_id, block_id, queue_task.result()):
                        yield event
                    queue_task = None
                else:
                    queue_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await queue_task
                    queue_task = None
            try:
                completed = await completion_task
            except asyncio.CancelledError:
                raise
            except Exception:
                completion_error = True

            # Worker 在 completion future 完成前同步发布最终 Telemetry，排空后再收尾。
            while not buffer.queue.empty():
                async for event in self._drain_item(reply_id, block_id, buffer.queue.get_nowait()):
                    yield event

            report: str | None = None
            final_text = "解释任务失败，请检查井资料或服务配置后重试。"
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
                        report_result = await self._get_report(task_id, execution_id)
                        report = report_result.report_markdown
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        completion_error = True
                    final_text = "解释报告暂不可用，请查看聊天中的当前执行状态。"
                elif status == "BLOCKED":
                    yield ThinkingBlockDeltaEvent(
                        reply_id=reply_id,
                        block_id=block_id,
                        delta="\n解释流程已停止：缺少后续处理所需资料\n",
                    )
                    final_text = "解释流程已停止：缺少后续处理所需资料。"
                elif status == "WAITING_CONFIRMATION":
                    yield ThinkingBlockDeltaEvent(
                        reply_id=reply_id,
                        block_id=block_id,
                        delta="\n⏸ 当前阶段已完成，结果已发送到聊天中，等待确认继续\n",
                    )
                    try:
                        final_text = await self._waiting_confirmation_text(task_id, execution_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        completion_error = True
                        final_text = "当前阶段已完成，请确认并继续。"
                elif status == "REVIEW_REQUIRED":
                    yield ThinkingBlockDeltaEvent(
                        reply_id=reply_id,
                        block_id=block_id,
                        delta="\n解释流程已进入人工复核\n",
                    )
                    final_text = "解释流程已进入人工复核，请在任务面板查看执行事实。"
                    # 复核终态已有诊断报告时展示本版报告，不能伪装成验证通过的正式结论。
                    if completed.report_ready:
                        try:
                            report_result = await self._get_report(task_id, execution_id)
                            if report_result.report_markdown:
                                report = (
                                    "# 人工复核诊断报告\n\n"
                                    "> 本次解释需要人工复核；以下为诊断结果，"
                                    "不是已通过验证的正式解释结论。\n\n"
                                    + report_result.report_markdown
                                )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            completion_error = True
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
            chunks = _report_chunks(report or final_text)
            for index, chunk in enumerate(chunks):
                yield TextBlockDeltaEvent(reply_id=reply_id, block_id=report_id, delta=chunk)
                if index < len(chunks) - 1:
                    await self._pace_report_chunk()
            yield TextBlockEndEvent(reply_id=reply_id, block_id=report_id)
            yield ReplyEndEvent(
                session_id=session_id,
                reply_id=reply_id,
                finished_reason=ReplyFinishedReason.COMPLETED,
            )
        finally:
            # 取消的只是请求侧等待；dispatcher.wait 的 shield 保证 Worker 继续执行。
            for task in (queue_task, completion_task):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


class ExecutionStreamingMiddleware(MiddlewareBase):
    """拦截创建型任务 Tool 的结果，并用同一回复持续展示该 Execution。"""

    def __init__(self, streamer: ExecutionReplyStreamer) -> None:
        self.streamer = streamer

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[AgentEvent | Msg, None]:
        """只接管 START/MODIFY/FULL_RERUN；只读 Tool 保持官方 ReAct 流程。"""

        buffer = self.streamer.new_buffer()
        source = next_handler(**input_kwargs)
        intercepted: dict[str, Any] | None = None
        try:
            while True:
                # 框架可能在另一 Context 关闭生成器；Token 不得跨 yield 保存。
                # 每次推进源流时绑定观察器，执行期间创建的 Worker 仍继承它。
                token = event_observer.set(buffer.observe)
                try:
                    event = await anext(source)
                except StopAsyncIteration:
                    break
                finally:
                    event_observer.reset(token)
                yield event
                if (
                    isinstance(event, ToolResultEndEvent)
                    and event.state == ToolResultState.SUCCESS
                    and is_streaming_execution(event.metadata.get("result"))
                ):
                    intercepted = event.metadata["result"]
                    break
        finally:
            await source.aclose()

        if intercepted is None:
            return

        async for event in self.streamer.stream(
            session_id=agent.state.session_id,
            reply_id=agent.state.reply_id,
            payload=intercepted,
            buffer=buffer,
        ):
            # ToolCall/ToolResult 已由 AgentScope 写入；这里只补齐同一 AssistantMsg 的过程与报告。
            if agent.state.context and agent.state.context[-1].id == agent.state.reply_id:
                agent.state.context[-1].append_event(event)
            yield event
        if agent.state.context and agent.state.context[-1].id == agent.state.reply_id:
            yield agent.state.context[-1]
