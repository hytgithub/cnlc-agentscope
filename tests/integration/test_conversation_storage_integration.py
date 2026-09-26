"""真实 PostgreSQL/Redis 上验证 Conversation durable archive 与缓存恢复。"""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from agentscope.app.storage import SessionConfig
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock
from agentscope.state import AgentState
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from cnlc_agent.demo.demo_agent import MockTaskShellCredential
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.session_binding import SessionTaskBinding, TaskSessionIdentity
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.conversation import (
    ConversationMessageRow,
    ConversationSessionRow,
    DurableConversationStorage,
    PostgreSQLConversationRepository,
)
from cnlc_agent.infrastructure.database import PostgreSQLTaskRepository, TaskRow
from cnlc_agent.infrastructure.redis_store import RedisInterpretationStateStore

ROOT = Path(__file__).resolve().parents[2]
DB_URL = os.environ.get("CNLC_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("CNLC_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not (DB_URL and REDIS_URL),
    reason="Set CNLC_TEST_DATABASE_URL and CNLC_TEST_REDIS_URL for real services",
)


@pytest.fixture(scope="module")
def migrated_conversation():
    """真实测试只运行显式 migration，不允许应用启动自动建表。"""

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": DB_URL or ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, "Real PostgreSQL conversation migration failed"


def _msg(message_id: str, text: str) -> Msg:
    return Msg(
        id=message_id,
        name="user",
        role="user",
        content=[TextBlock(text=text)],
    )


async def test_real_conversation_survives_cache_loss_and_restart(migrated_conversation):
    """覆盖 durable save、幂等、TTL、flush 恢复、短期状态与业务事实隔离。"""

    token = uuid4().hex
    user_id = f"conversation-user-{token}"
    agent_id = f"conversation-agent-{token}"
    session_id = f"conversation-session-{token}"
    task_id = f"conversation-task-{token}"
    engine = create_async_engine(DB_URL)
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    conversation = PostgreSQLConversationRepository(engine)
    tasks = PostgreSQLTaskRepository(engine)
    storage = DurableConversationStorage(
        DB_URL or "",
        session_cache_ttl_seconds=30,
        repository=conversation,
        connection_pool=redis.connection_pool,
    )
    state = InterpretationState(task=TaskRequest(task_id=task_id, well_id=f"WELL-{token[:8]}"))
    identity = TaskSessionIdentity(user_id=user_id, agent_id=agent_id, session_id=session_id)
    try:
        await tasks.create(state)
        await tasks.bind_task_to_session(
            SessionTaskBinding(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                task_id=task_id,
            )
        )
        session_state = AgentState(
            context=[_msg("context-1", "bounded runtime context")],
            middle_context={
                "cnlc_active_task_id": task_id,
                "cnlc_pending_clarification": {
                    "known_value": 0.16,
                    "task_id": task_id,
                    "owner_token": "expired-owner",
                },
            },
        )
        async with storage:
            await storage.upsert_session(
                user_id,
                agent_id,
                SessionConfig(workspace_id="workspace", name="持久化集成测试"),
                session_state,
                session_id=session_id,
            )
            user_message = _msg("message-user", "解释这口井")
            assistant_message = Msg(
                id="message-assistant",
                name="assistant",
                role="assistant",
                content=[
                    ThinkingBlock(thinking="解释过程展示副本"),
                    ToolCallBlock(
                        id="call-1",
                        name="get_interpretation_report",
                        input='{"selector":"CURRENT"}',
                    ),
                    ToolResultBlock(
                        id="call-1",
                        name="get_interpretation_report",
                        output="execution reference",
                        state="success",
                    ),
                    TextBlock(text="报告已生成"),
                ],
            )
            await storage.upsert_message(user_id, session_id, user_message)
            await storage.upsert_message(user_id, session_id, assistant_message)
            user_message.content = [TextBlock(text="解释这口井（重试）")]
            await storage.upsert_message(user_id, session_id, user_message)
            await storage.upsert_credential(
                "__backend__",
                MockTaskShellCredential(id=f"credential-{token}", name="Backend"),
            )

            session_key = storage._session_key(user_id, session_id)
            message_key = storage._message_key(user_id, session_id)
            index_key = storage._session_index_key(user_id, agent_id)
            credential_key = storage._key(
                storage.key_config.credential,
                user_id="__backend__",
                credential_id=f"credential-{token}",
            )
            for key in (session_key, message_key, index_key):
                assert 0 < await redis.ttl(key) <= 30
            assert await redis.ttl(credential_key) == -1
            assert await conversation.message_count(user_id, session_id) == 2

            archived = await conversation.get_session(user_id, agent_id, session_id)
            assert archived is not None
            assert archived.state.middle_context["cnlc_active_task_id"] == task_id
            assert "cnlc_pending_clarification" not in archived.state.middle_context

            # 同时删除 AgentScope chat cache 和业务运行快照，模拟 Redis flush 的关键部分。
            state_cache = RedisInterpretationStateStore(redis, f"cnlc:test:{token}", 60)
            await state_cache.save(state)
            await redis.delete(session_key, message_key, index_key, state_cache.key(task_id))
            assert await redis.exists(session_key, message_key, state_cache.key(task_id)) == 0

        # 新 Storage/Repository 模拟 Backend restart；历史、Task 与报告边界不依赖旧对象。
        reopened_repository = PostgreSQLConversationRepository(engine)
        reopened = DurableConversationStorage(
            DB_URL or "",
            session_cache_ttl_seconds=30,
            repository=reopened_repository,
            connection_pool=redis.connection_pool,
        )
        async with reopened:
            restored = await reopened.get_session(user_id, agent_id, session_id)
            messages, has_more = await reopened.list_messages(user_id, session_id)
            assert restored is not None
            assert restored.state.middle_context["cnlc_active_task_id"] == task_id
            assert "cnlc_pending_clarification" not in restored.state.middle_context
            assert [message.id for message in messages] == [
                "message-user",
                "message-assistant",
            ]
            assert messages[0].content[0].text == "解释这口井（重试）"  # type: ignore[union-attr]
            assert [block.type for block in messages[1].content] == [
                "thinking",
                "tool_call",
                "tool_result",
                "text",
            ]
            assert has_more is False
            assert 0 < await redis.ttl(reopened._session_key(user_id, session_id)) <= 30
            assert await tasks.list_session_task_ids(identity) == [task_id]
            assert await tasks.get_task(task_id) is not None

            assert await reopened.delete_session(user_id, agent_id, session_id)
            assert await reopened.get_session(user_id, agent_id, session_id) is None
            assert await tasks.get_task(task_id) is not None
            assert await tasks.list_session_task_ids(identity) == [task_id]
    finally:
        await redis.delete(
            f"agentscope:user:{user_id}:session:{session_id}",
            f"agentscope:user:{user_id}:session:{session_id}:messages",
            f"agentscope:user:{user_id}:agent:{agent_id}:sessions",
            f"agentscope:user:__backend__:credential:credential-{token}",
            "agentscope:user:__backend__:credentials",
        )
        async with engine.begin() as connection:
            await connection.execute(
                delete(ConversationSessionRow).where(
                    ConversationSessionRow.user_id == user_id
                )
            )
            await connection.execute(delete(TaskRow).where(TaskRow.task_id == task_id))
        await redis.aclose()
        await engine.dispose()


async def test_real_long_history_is_bounded_and_ordered(migrated_conversation):
    """100+ durable 消息保持顺序，恢复 Session Context 不会被全历史替换。"""

    token = uuid4().hex
    user_id, agent_id, session_id = f"u-{token}", f"a-{token}", f"s-{token}"
    engine = create_async_engine(DB_URL)
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    repository = PostgreSQLConversationRepository(engine)
    storage = DurableConversationStorage(
        DB_URL or "",
        session_cache_ttl_seconds=20,
        repository=repository,
        connection_pool=redis.connection_pool,
    )
    try:
        async with storage:
            await storage.upsert_session(
                user_id,
                agent_id,
                SessionConfig(workspace_id="workspace"),
                AgentState(context=[_msg("context-only", "compressed")]),
                session_id=session_id,
            )
            for index in range(105):
                await storage.upsert_message(
                    user_id,
                    session_id,
                    _msg(f"long-{index:03}", str(index)),
                )
            latest, has_more = await storage.list_messages(user_id, session_id, limit=10)
            older, older_has_more = await storage.list_messages(
                user_id, session_id, limit=10, before=latest[0].id
            )
            restored = await storage.get_session(user_id, agent_id, session_id)
            assert [message.id for message in latest] == [
                f"long-{index:03}" for index in range(95, 105)
            ]
            assert [message.id for message in older] == [
                f"long-{index:03}" for index in range(85, 95)
            ]
            assert has_more and older_has_more
            assert restored is not None
            assert [message.id for message in restored.state.context] == ["context-only"]
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(ConversationMessageRow).where(
                    ConversationMessageRow.user_id == user_id
                )
            )
            await connection.execute(
                delete(ConversationSessionRow).where(
                    ConversationSessionRow.user_id == user_id
                )
            )
        await redis.delete(
            f"agentscope:user:{user_id}:session:{session_id}",
            f"agentscope:user:{user_id}:session:{session_id}:messages",
            f"agentscope:user:{user_id}:agent:{agent_id}:sessions",
        )
        await redis.aclose()
        await engine.dispose()
