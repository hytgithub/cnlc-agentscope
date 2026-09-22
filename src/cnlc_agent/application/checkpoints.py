"""检查点先写长期存储、再写运行缓存；两者之间不提供分布式事务。"""

from cnlc_agent.application.ports import InterpretationStateStore, TaskRepository
from cnlc_agent.domain.state import InterpretationState


class CheckpointStore:
    """把 PostgreSQL 任务仓库与 Redis 运行时快照组合成统一状态存储。"""

    def __init__(self, repository: TaskRepository, cache: InterpretationStateStore) -> None:
        self.repository = repository
        self.cache = cache

    async def save(self, state: InterpretationState) -> None:
        """先保存可恢复快照，再刷新缓存；缓存失败由上层终止流程。"""

        await self.repository.save(state, "")
        await self.cache.save(state)

    async def get(self, task_id: str) -> InterpretationState | None:
        """读取当前运行快照；历史事实仍以任务仓库为准。"""

        return await self.cache.get(task_id)
