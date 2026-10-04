"""最终回复中间件必须用证据门控模型自由文本。"""

from types import SimpleNamespace

import pytest
from agentscope.event import (
    ModelCallStartEvent,
    ReplyEndEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
)
from agentscope.message import AssistantMsg

from cnlc_agent.demo.interaction_middleware import InteractionStateMiddleware


class _RunnerStub:
    def attach_session_runtime_context(self, _context):
        pass

    def begin_interaction_turn(self):
        pass

    def end_interaction_turn(self):
        pass


@pytest.mark.asyncio
async def test_no_tool_result_replaces_freeform_success_claim_before_reply_end():
    reply_id = "reply-no-evidence"
    assistant = AssistantMsg(id=reply_id, name="agent", content=[])
    agent = SimpleNamespace(
        state=SimpleNamespace(
            middle_context={},
            reply_id=reply_id,
            session_id="session-1",
            context=[assistant],
        )
    )

    async def next_handler(**_kwargs):
        yield ModelCallStartEvent(reply_id=reply_id, model_name="offline-model")
        yield TextBlockStartEvent(reply_id=reply_id, block_id="model-text")
        yield TextBlockDeltaEvent(
            reply_id=reply_id, block_id="model-text", delta="已经查询到该层孔隙度为 25%。"
        )
        yield TextBlockEndEvent(reply_id=reply_id, block_id="model-text")
        yield ReplyEndEvent(session_id="session-1", reply_id=reply_id)

    middleware = InteractionStateMiddleware(_RunnerStub())
    events = [event async for event in middleware.on_reply(agent, {}, next_handler)]
    visible = "".join(event.delta for event in events if isinstance(event, TextBlockDeltaEvent))
    assert "已经查询到" not in visible
    assert "25%" not in visible
    assert "没有查询或修改业务数据" in visible
    assert [event for event in events if isinstance(event, ReplyEndEvent)]
