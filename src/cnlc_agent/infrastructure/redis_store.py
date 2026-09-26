"""Redis 仅保存可丢弃的运行时快照，不能作为最终业务事实的唯一存储。"""

from hashlib import sha256

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.state import InterpretationState


class RedisInterpretationStateStore:
    """InterpretationStateStore 的 Redis 实现，所有快照都设置滑动 TTL。"""

    def __init__(self, client: Redis, prefix: str, ttl_seconds: int) -> None:
        if not prefix or ttl_seconds <= 0:
            raise ValueError("Redis prefix and positive TTL are required")
        self.client = client
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds

    def key(self, task_id: str) -> str:
        """散列任务标识，避免原始 ID 直接出现在 Redis Key 中。"""

        return f"{self.prefix}:state:{sha256(task_id.encode()).hexdigest()}"

    async def save(self, state: InterpretationState) -> None:
        """原子写入 JSON 快照并刷新过期时间。"""

        try:
            await self.client.set(
                self.key(state.task.task_id), state.model_dump_json(), ex=self.ttl_seconds
            )
        except (RedisError, OSError, TimeoutError):
            raise InfrastructureError("REDIS_WRITE_FAILED", "Redis 状态保存失败") from None

    async def get(self, task_id: str) -> InterpretationState | None:
        """读取并校验快照；缓存缺失返回 None，损坏则显式失败。"""

        try:
            payload = await self.client.get(self.key(task_id))
            return InterpretationState.model_validate_json(payload) if payload is not None else None
        except (ValidationError, UnicodeError):
            raise InfrastructureError("INVALID_CACHED_STATE", "Redis 状态结构无效") from None
        except (RedisError, OSError, TimeoutError):
            raise InfrastructureError("REDIS_READ_FAILED", "Redis 状态读取失败") from None
