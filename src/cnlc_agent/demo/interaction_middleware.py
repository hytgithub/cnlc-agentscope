"""在 AgentScope 请求生命周期管理短期澄清，不另行调用意图模型。"""

import json
from collections.abc import AsyncGenerator, Callable
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
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from cnlc_agent.demo.interaction_state import (
    InteractionPolicy,
    InteractionSnapshot,
    render_interaction_result,
)
from cnlc_agent.demo.task_tools import TaskCommandRunner, interaction_chunk


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
        try:
            try:
                async for event in source:
                    yield event
                    if isinstance(event, ToolResultEndEvent):
                        payload = event.metadata.get("result", event.metadata)
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
                        delta=render_interaction_result(intercepted),
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
        writes = [
            call
            for call in calls
            if call.name
            in {
                "run_well_interpretation",
                "modify_well_interpretation",
                "rerun_well_interpretation",
            }
        ]
        if len(writes) > 1 or (
            writes and any(call.name == "request_interpretation_clarification" for call in calls)
        ):
            decision = InteractionPolicy.decide(InteractionSnapshot(), "MODIFY", conflict=True)
            chunk = interaction_chunk(
                decision.error_code or "CLARIFICATION_REQUIRED", decision.message
            )
            self.runner.clear_pending()
            yield ToolResponse(
                id=input_kwargs["tool_call"].id,
                content=chunk.content,
                state=ToolResultState.ERROR,
                metadata=chunk.metadata,
            )
            return
        async for event in next_handler(**input_kwargs):
            yield event

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        """动态快照不是聊天摘要；每次推理仍以仓库状态为准。"""

        snapshot = await self.runner.interaction_snapshot()
        return (
            current_prompt
            + "\n当前可信交互快照（不得由聊天记忆覆盖）：\n"
            + json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False)
        )
