"""Durable checkpoint first, runtime cache second; no distributed transaction."""

from cnlc_agent.application.ports import InterpretationStateStore, TaskRepository
from cnlc_agent.domain.state import InterpretationState


class CheckpointStore:
    def __init__(self, repository: TaskRepository, cache: InterpretationStateStore) -> None:
        self.repository = repository
        self.cache = cache

    async def save(self, state: InterpretationState) -> None:
        await self.repository.save(state, "")
        await self.cache.save(state)

    async def get(self, task_id: str) -> InterpretationState | None:
        return await self.cache.get(task_id)
