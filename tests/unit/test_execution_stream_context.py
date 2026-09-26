"""验证官方流消费者跨任务推进或关闭时，观察器不会泄漏或清理失败。"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from cnlc_agent.demo.execution_stream import ExecutionStreamingMiddleware
from cnlc_agent.infrastructure.telemetry import event_observer


async def test_observer_scope_survives_cross_context_close():
    """模拟 SSE 取消后由另一任务关闭流，不允许 ContextVar Token 跨 yield。"""
    observed = []
    buffer = SimpleNamespace(observe=lambda *args: observed.append(args))
    middleware = ExecutionStreamingMiddleware(cast(Any, SimpleNamespace(new_buffer=lambda: buffer)))
    closed = []

    async def source(**kwargs):
        try:
            assert event_observer.get() == buffer.observe
            yield "first"
            assert event_observer.get() == buffer.observe
            yield "second"
        finally:
            closed.append(True)

    stream = middleware.on_reply(cast(Any, SimpleNamespace()), {}, source)
    assert await asyncio.create_task(anext(stream)) == "first"
    assert event_observer.get() is None
    assert await asyncio.create_task(anext(stream)) == "second"
    await asyncio.create_task(stream.aclose())
    assert closed == [True]
    assert event_observer.get() is None
