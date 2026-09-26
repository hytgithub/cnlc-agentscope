"""Conversation durable/cache 边界的快速单元测试。"""

from datetime import datetime

from agentscope.app.storage import SessionConfig, SessionRecord
from agentscope.message import Msg, TextBlock
from agentscope.state import AgentState
from fakeredis.aioredis import FakeRedis

from cnlc_agent.config.settings import PersistenceSettings
from cnlc_agent.demo.demo_agent import MockTaskShellCredential
from cnlc_agent.infrastructure.conversation import DurableConversationStorage


class MemoryConversationRepository:
    """测试用端口实现，只模拟 durable 语义，不替代真实 PostgreSQL 集成测试。"""

    def __init__(self) -> None:
        self.sessions: dict[tuple[str, str, str], SessionRecord] = {}
        self.messages: dict[tuple[str, str], list[Msg]] = {}
        self.purge_cutoff: datetime | None = None

    async def upsert_session(self, record: SessionRecord) -> SessionRecord:
        durable = record.model_copy(deep=True)
        durable.state.middle_context.pop("cnlc_pending_clarification", None)
        self.sessions[(record.user_id, record.agent_id, record.id)] = durable
        return durable.model_copy(deep=True)

    async def get_session(
        self, user_id: str, agent_id: str, session_id: str
    ) -> SessionRecord | None:
        record = self.sessions.get((user_id, agent_id, session_id))
        return record.model_copy(deep=True) if record else None

    async def get_session_by_id(self, user_id: str, session_id: str) -> SessionRecord | None:
        return next(
            (
                record.model_copy(deep=True)
                for (owner, _, stored_id), record in self.sessions.items()
                if owner == user_id and stored_id == session_id
            ),
            None,
        )

    async def list_sessions(self, user_id: str, agent_id: str) -> list[SessionRecord]:
        return [
            record.model_copy(deep=True)
            for (owner, agent, _), record in self.sessions.items()
            if owner == user_id and agent == agent_id
        ]

    async def list_sessions_by_origin(
        self, user_id: str, origin_type: str, origin_id: str
    ) -> list[SessionRecord]:
        return [
            record.model_copy(deep=True)
            for (owner, _, _), record in self.sessions.items()
            if owner == user_id
            and record.origin.type == origin_type
            and getattr(record.origin, f"{origin_type}_id", None) == origin_id
        ]

    async def delete_session(self, user_id: str, agent_id: str, session_id: str) -> bool:
        existed = self.sessions.pop((user_id, agent_id, session_id), None) is not None
        self.messages.pop((user_id, session_id), None)
        return existed

    async def upsert_message(self, user_id: str, session_id: str, msg: Msg) -> None:
        messages = self.messages.setdefault((user_id, session_id), [])
        for index, existing in enumerate(messages):
            if existing.id == msg.id:
                messages[index] = msg.model_copy(deep=True)
                return
        messages.append(msg.model_copy(deep=True))

    async def get_message(self, user_id: str, session_id: str, message_id: str) -> Msg | None:
        return next(
            (
                msg.model_copy(deep=True)
                for msg in self.messages.get((user_id, session_id), [])
                if msg.id == message_id
            ),
            None,
        )

    async def list_messages(
        self, user_id: str, session_id: str, limit: int, before: str | None
    ) -> tuple[list[Msg], bool]:
        messages = self.messages.get((user_id, session_id), [])
        end = len(messages)
        if before is not None:
            end = next((i for i, msg in enumerate(messages) if msg.id == before), 0)
        start = max(end - limit, 0)
        return [msg.model_copy(deep=True) for msg in messages[start:end]], start > 0

    async def message_count(self, user_id: str, session_id: str) -> int:
        return len(self.messages.get((user_id, session_id), []))

    async def purge_inactive_before(self, cutoff: datetime) -> int:
        self.purge_cutoff = cutoff
        return 0


def _message(message_id: str, text: str = "hello") -> Msg:
    return Msg(
        id=message_id,
        name="user",
        role="user",
        content=[TextBlock(text=text)],
    )


async def test_durable_cache_ttl_restore_and_credential_isolation():
    redis = FakeRedis(decode_responses=True)
    repository = MemoryConversationRepository()
    storage = DurableConversationStorage(
        "postgresql+asyncpg://unused/unused",
        session_cache_ttl_seconds=60,
        repository=repository,
        connection_pool=redis.connection_pool,
    )
    state = AgentState(
        middle_context={
            "cnlc_active_task_id": "task-1",
            "cnlc_pending_clarification": {"unsafe": "old"},
        }
    )
    async with storage:
        record = await storage.upsert_session(
            "user-1",
            "agent-1",
            SessionConfig(workspace_id="workspace-1", name="井解释"),
            state,
            session_id="session-1",
        )
        await storage.upsert_message("user-1", record.id, _message("message-1"))
        await storage.upsert_message(
            "user-1", record.id, _message("message-1", "updated")
        )
        await storage.upsert_credential(
            "__backend__",
            MockTaskShellCredential(id="credential-1", name="Backend"),
        )

        session_key = storage._session_key("user-1", record.id)
        message_key = storage._message_key("user-1", record.id)
        credential_key = storage._key(
            storage.key_config.credential,
            user_id="__backend__",
            credential_id="credential-1",
        )
        assert 0 < await redis.ttl(session_key) <= 60
        assert 0 < await redis.ttl(message_key) <= 60
        assert await redis.ttl(credential_key) == -1
        assert len(repository.messages[("user-1", record.id)]) == 1
        archived = await repository.get_session("user-1", "agent-1", record.id)
        assert archived is not None
        assert archived.state.middle_context["cnlc_active_task_id"] == "task-1"
        assert "cnlc_pending_clarification" not in archived.state.middle_context
        await storage.list_sessions("user-1", "agent-1")
        cached = await storage.get_session("user-1", "agent-1", record.id)
        assert cached is not None
        assert "cnlc_pending_clarification" in cached.state.middle_context

        await redis.delete(session_key, message_key)
        restored = await storage.get_session("user-1", "agent-1", record.id)
        messages, has_more = await storage.list_messages("user-1", record.id)
        assert restored is not None
        assert "cnlc_pending_clarification" not in restored.state.middle_context
        assert [message.id for message in messages] == ["message-1"]
        assert has_more is False
        assert 0 < await redis.ttl(session_key) <= 60
    await redis.aclose()


async def test_long_history_is_paginated_and_retention_is_separate():
    redis = FakeRedis(decode_responses=True)
    repository = MemoryConversationRepository()
    storage = DurableConversationStorage(
        "postgresql+asyncpg://unused/unused",
        session_cache_ttl_seconds=15,
        conversation_retention_days=90,
        repository=repository,
        connection_pool=redis.connection_pool,
    )
    async with storage:
        record = await storage.upsert_session(
            "user-2",
            "agent-2",
            SessionConfig(workspace_id="workspace-2"),
            AgentState(context=[_message("context-only")]),
            session_id="session-2",
        )
        for index in range(100):
            await storage.upsert_message("user-2", record.id, _message(f"message-{index:03}"))
        page, has_more = await storage.list_messages("user-2", record.id, limit=10)
        restored = await storage.get_session("user-2", "agent-2", record.id)
        assert [msg.id for msg in page] == [f"message-{index:03}" for index in range(90, 100)]
        assert has_more is True
        assert restored is not None
        assert len(restored.state.context) == 1
        assert repository.purge_cutoff is not None

    settings = PersistenceSettings(
        redis_ttl_seconds=1,
        session_cache_ttl_seconds=7,
        conversation_retention_days=None,
        _env_file=None,
    )
    assert settings.redis_ttl_seconds == 1
    assert settings.session_cache_ttl_seconds == 7
    assert settings.conversation_retention_days is None
    await redis.aclose()


async def test_multiple_active_sessions_and_messages_have_real_ttl():
    """容量回归直接检查多会话、多消息产生的真实 Redis 过期时间。"""

    redis = FakeRedis(decode_responses=True)
    repository = MemoryConversationRepository()
    storage = DurableConversationStorage(
        "postgresql+asyncpg://unused/unused",
        session_cache_ttl_seconds=45,
        repository=repository,
        connection_pool=redis.connection_pool,
    )
    async with storage:
        for session_index in range(3):
            session_id = f"capacity-session-{session_index}"
            await storage.upsert_session(
                "capacity-user",
                "capacity-agent",
                SessionConfig(workspace_id="capacity-workspace"),
                session_id=session_id,
            )
            for message_index in range(4):
                await storage.upsert_message(
                    "capacity-user",
                    session_id,
                    _message(f"capacity-{session_index}-{message_index}"),
                )
            assert 0 < await redis.ttl(
                storage._session_key("capacity-user", session_id)
            ) <= 45
            assert 0 < await redis.ttl(
                storage._message_key("capacity-user", session_id)
            ) <= 45
        assert 0 < await redis.ttl(
            storage._session_index_key("capacity-user", "capacity-agent")
        ) <= 45
    await redis.aclose()
