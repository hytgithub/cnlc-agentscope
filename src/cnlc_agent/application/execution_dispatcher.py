"""进程内后台执行调度器；生命周期事实始终写入 TaskRepository。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from cnlc_agent.application.ports import TaskRepository
from cnlc_agent.domain.models import utc_now


class InProcessExecutionDispatcher:
    """管理当前进程提交的协程，不把 asyncio Task 当作业务状态来源。"""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._worker_prefix = uuid4().hex

    async def submit(
        self,
        execution_id: str,
        execute: Callable[[str], Awaitable[None]],
    ) -> None:
        """登记并立即返回；同一进程内重复提交同一 Execution 时保持幂等。"""

        existing = self._tasks.get(execution_id)
        if existing is not None and not existing.done():
            return
        worker_id = f"{self._worker_prefix}:{execution_id}"
        async def run() -> None:
            await execute(worker_id)

        task: asyncio.Task[None] = asyncio.create_task(run(), name=f"execution:{execution_id}")
        self._tasks[execution_id] = task

        def consume(completed: asyncio.Task[None]) -> None:
            self._consume(execution_id, completed)

        task.add_done_callback(consume)

    async def wait(self, execution_id: str) -> None:
        """测试和同步兼容入口可等待指定任务；交互 Tool 不调用此方法。"""

        task = self._tasks.get(execution_id)
        if task is not None:
            await asyncio.shield(task)

    async def recover(self, repository: TaskRepository) -> int:
        """启动时仅终止过期租约，避免未知副作用的自动重跑。"""

        recovered = await repository.recover_expired_executions(utc_now())
        return len(recovered)

    async def shutdown(self) -> None:
        """应用退出时取消并等待本进程任务，让 Worker 写入明确终态。"""

        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def _consume(self, execution_id: str, task: asyncio.Task[None]) -> None:
        """取走异常，防止事件循环泄漏堆栈；详情由 Service 写入安全错误码。"""

        self._tasks.pop(execution_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logging.getLogger(__name__).warning(
                "Background execution failed: execution=%s type=%s",
                execution_id,
                type(error).__name__,
            )
