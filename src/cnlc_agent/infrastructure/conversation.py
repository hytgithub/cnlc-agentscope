"""AgentScope 对话归档：PostgreSQL 保存长期事实，Redis 仅保存活跃缓存。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, Self

from agentscope.app.storage import (
    ChannelOrigin,
    RedisStorage,
    ScheduleOrigin,
    SessionConfig,
    SessionOrigin,
    SessionRecord,
    TeamOrigin,
    UserOrigin,
)
from agentscope.message import Msg
from agentscope.state import AgentState
from pydantic import ValidationError
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cnlc_agent.domain.errors import InfrastructureError

logger = logging.getLogger(__name__)


class ConversationBase(DeclarativeBase):
    """Conversation 专用映射基类；建表只允许通过 Alembic 0007。"""

    pass


class ConversationSessionRow(ConversationBase):
    """长期会话元数据和可恢复 AgentScope SessionRecord。"""

    __tablename__ = "conversation_session"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "session_id",
            name="uq_conversation_session_user_session",
        ),
        Index(
            "ix_conversation_session_owner",
            "user_id",
            "agent_id",
            "session_id",
        ),
        Index("ix_conversation_session_last_active", "last_active_at"),
    )

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationMessageRow(ConversationBase):
    """结构化 AgentScope 消息副本；业务 Tool/Report 仍以既有业务表为准。"""

    __tablename__ = "conversation_message"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "agent_id", "session_id"],
            [
                "conversation_session.user_id",
                "conversation_session.agent_id",
                "conversation_session.session_id",
            ],
            name="fk_conversation_message_session",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "user_id",
            "agent_id",
            "session_id",
            "sequence",
            name="uq_conversation_message_session_sequence",
        ),
        Index(
            "ix_conversation_message_session_sequence",
            "user_id",
            "agent_id",
            "session_id",
            "sequence",
        ),
    )

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    """PostgreSQL timestamptz 统一使用 UTC，兼容 AgentScope 的 naive 时间。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _message_created_at(msg: Msg) -> datetime:
    """把 AgentScope ISO 字符串转成持久时间；无效时间不污染消息结构。"""

    try:
        return _aware(datetime.fromisoformat(msg.created_at.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _durable_session(record: SessionRecord) -> SessionRecord:
    """归档副本移除短期澄清，避免 Redis 丢失后重新激活旧参数。"""

    durable = record.model_copy(deep=True)
    durable.state.middle_context.pop("cnlc_pending_clarification", None)
    return durable


class ConversationRepository(Protocol):
    """对话归档端口，便于独立验证 Redis 缓存行为。"""

    async def upsert_session(self, record: SessionRecord) -> SessionRecord: ...

    async def get_session(
        self, user_id: str, agent_id: str, session_id: str
    ) -> SessionRecord | None: ...

    async def get_session_by_id(self, user_id: str, session_id: str) -> SessionRecord | None: ...

    async def list_sessions(self, user_id: str, agent_id: str) -> list[SessionRecord]: ...

    async def list_sessions_by_origin(
        self, user_id: str, origin_type: str, origin_id: str
    ) -> list[SessionRecord]: ...

    async def delete_session(self, user_id: str, agent_id: str, session_id: str) -> bool: ...

    async def upsert_message(self, user_id: str, session_id: str, msg: Msg) -> None: ...

    async def get_message(self, user_id: str, session_id: str, message_id: str) -> Msg | None: ...

    async def list_messages(
        self, user_id: str, session_id: str, limit: int, before: str | None
    ) -> tuple[list[Msg], bool]: ...

    async def message_count(self, user_id: str, session_id: str) -> int: ...

    async def purge_inactive_before(self, cutoff: datetime) -> int: ...


class PostgreSQLConversationRepository:
    """PostgreSQL 对话仓库；父会话行锁负责消息顺序串行分配。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    @staticmethod
    def _record(row: ConversationSessionRow) -> SessionRecord:
        return SessionRecord.model_validate(row.record_json)

    async def upsert_session(self, record: SessionRecord) -> SessionRecord:
        """按完整 ownership 幂等更新，created_at 保持首次值。"""

        durable = _durable_session(record)
        values = {
            "user_id": durable.user_id,
            "agent_id": durable.agent_id,
            "session_id": durable.id,
            "title": durable.config.name,
            "status": "ACTIVE",
            "record_json": durable.model_dump(mode="json"),
            "created_at": _aware(durable.created_at),
            "updated_at": _aware(durable.updated_at),
            "last_active_at": datetime.now(UTC),
        }
        statement = postgresql_insert(ConversationSessionRow).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "agent_id", "session_id"],
            set_={
                "title": statement.excluded.title,
                "status": statement.excluded.status,
                "record_json": statement.excluded.record_json,
                "updated_at": statement.excluded.updated_at,
                "last_active_at": statement.excluded.last_active_at,
            },
        )
        try:
            async with self.sessions.begin() as session:
                await session.execute(statement)
            return durable
        except IntegrityError:
            raise InfrastructureError(
                "CONVERSATION_OWNERSHIP_CONFLICT",
                "会话标识已属于其他 Agent",
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "CONVERSATION_WRITE_FAILED",
                "长期会话保存失败",
            ) from None

    async def get_session(
        self, user_id: str, agent_id: str, session_id: str
    ) -> SessionRecord | None:
        try:
            async with self.sessions() as session:
                row = await session.get(
                    ConversationSessionRow,
                    (user_id, agent_id, session_id),
                )
                return self._record(row) if row is not None else None
        except (ValidationError, ValueError):
            raise InfrastructureError(
                "INVALID_STORED_CONVERSATION",
                "长期会话结构无效",
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "CONVERSATION_READ_FAILED",
                "长期会话读取失败",
            ) from None

    async def get_session_by_id(self, user_id: str, session_id: str) -> SessionRecord | None:
        try:
            async with self.sessions() as session:
                row = await session.scalar(
                    select(ConversationSessionRow).where(
                        ConversationSessionRow.user_id == user_id,
                        ConversationSessionRow.session_id == session_id,
                    )
                )
                return self._record(row) if row is not None else None
        except (ValidationError, ValueError):
            raise InfrastructureError(
                "INVALID_STORED_CONVERSATION",
                "长期会话结构无效",
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "CONVERSATION_READ_FAILED",
                "长期会话读取失败",
            ) from None

    async def list_sessions(self, user_id: str, agent_id: str) -> list[SessionRecord]:
        try:
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(ConversationSessionRow)
                        .where(
                            ConversationSessionRow.user_id == user_id,
                            ConversationSessionRow.agent_id == agent_id,
                        )
                        .order_by(ConversationSessionRow.created_at.desc())
                    )
                ).all()
                return [self._record(row) for row in rows]
        except (ValidationError, ValueError):
            raise InfrastructureError(
                "INVALID_STORED_CONVERSATION",
                "长期会话结构无效",
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "CONVERSATION_READ_FAILED",
                "长期会话列表读取失败",
            ) from None

    async def list_sessions_by_origin(
        self, user_id: str, origin_type: str, origin_id: str
    ) -> list[SessionRecord]:
        """低频来源查询从 durable JSON 过滤，避免为禁用能力增加冗余列。"""

        records: list[SessionRecord] = []
        try:
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(ConversationSessionRow)
                        .where(ConversationSessionRow.user_id == user_id)
                        .order_by(ConversationSessionRow.created_at.desc())
                    )
                ).all()
            for row in rows:
                record = self._record(row)
                origin = record.origin
                if origin.type != origin_type:
                    continue
                if getattr(origin, f"{origin_type}_id", None) == origin_id:
                    records.append(record)
            return records
        except (ValidationError, ValueError):
            raise InfrastructureError(
                "INVALID_STORED_CONVERSATION",
                "长期会话结构无效",
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "CONVERSATION_READ_FAILED",
                "长期会话来源索引读取失败",
            ) from None

    async def delete_session(self, user_id: str, agent_id: str, session_id: str) -> bool:
        """硬删除 Conversation 与其消息；业务 Task/Binding 没有外键级联。"""

        try:
            async with self.sessions.begin() as session:
                result = await session.execute(
                    delete(ConversationSessionRow).where(
                        ConversationSessionRow.user_id == user_id,
                        ConversationSessionRow.agent_id == agent_id,
                        ConversationSessionRow.session_id == session_id,
                    )
                )
            return bool(getattr(result, "rowcount", 0))
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "CONVERSATION_DELETE_FAILED",
                "长期会话删除失败",
            ) from None

    async def _owner_locked(
        self, session: AsyncSession, user_id: str, session_id: str
    ) -> ConversationSessionRow:
        row = await session.scalar(
            select(ConversationSessionRow)
            .where(
                ConversationSessionRow.user_id == user_id,
                ConversationSessionRow.session_id == session_id,
            )
            .with_for_update()
        )
        if row is None:
            raise InfrastructureError(
                "CONVERSATION_NOT_FOUND",
                "消息所属会话不存在",
            )
        return row

    async def upsert_message(self, user_id: str, session_id: str, msg: Msg) -> None:
        """message_id 幂等；首次插入在父会话行锁内分配稳定 sequence。"""

        try:
            async with self.sessions.begin() as session:
                owner = await self._owner_locked(session, user_id, session_id)
                existing = await session.get(ConversationMessageRow, msg.id)
                if existing is not None:
                    if (
                        existing.user_id != user_id
                        or existing.agent_id != owner.agent_id
                        or existing.session_id != session_id
                    ):
                        raise InfrastructureError(
                            "CONVERSATION_MESSAGE_ID_CONFLICT",
                            "消息标识已属于其他会话",
                        )
                    existing.role = msg.role
                    existing.content_json = msg.model_dump(mode="json")
                    existing.created_at = _message_created_at(msg)
                else:
                    sequence = await session.scalar(
                        select(func.coalesce(func.max(ConversationMessageRow.sequence), 0)).where(
                            ConversationMessageRow.user_id == user_id,
                            ConversationMessageRow.agent_id == owner.agent_id,
                            ConversationMessageRow.session_id == session_id,
                        )
                    )
                    session.add(
                        ConversationMessageRow(
                            message_id=msg.id,
                            user_id=user_id,
                            agent_id=owner.agent_id,
                            session_id=session_id,
                            sequence=int(sequence or 0) + 1,
                            role=msg.role,
                            content_json=msg.model_dump(mode="json"),
                            created_at=_message_created_at(msg),
                        )
                    )
                owner.last_active_at = datetime.now(UTC)
        except InfrastructureError:
            raise
        except IntegrityError:
            raise InfrastructureError(
                "CONVERSATION_MESSAGE_CONFLICT",
                "消息顺序写入冲突，请重试",
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "CONVERSATION_MESSAGE_WRITE_FAILED",
                "长期消息保存失败",
            ) from None

    async def get_message(self, user_id: str, session_id: str, message_id: str) -> Msg | None:
        try:
            async with self.sessions() as session:
                row = await session.get(ConversationMessageRow, message_id)
                if row is None or row.user_id != user_id or row.session_id != session_id:
                    return None
                return Msg.model_validate(row.content_json)
        except (ValidationError, ValueError):
            raise InfrastructureError(
                "INVALID_STORED_CONVERSATION_MESSAGE",
                "长期消息结构无效",
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "CONVERSATION_MESSAGE_READ_FAILED",
                "长期消息读取失败",
            ) from None

    async def list_messages(
        self,
        user_id: str,
        session_id: str,
        limit: int = 50,
        before: str | None = None,
    ) -> tuple[list[Msg], bool]:
        """按 sequence 做游标分页，不把长期全量历史注入运行时 Context。"""

        if limit <= 0:
            return [], False
        try:
            async with self.sessions() as session:
                owner = await session.scalar(
                    select(ConversationSessionRow).where(
                        ConversationSessionRow.user_id == user_id,
                        ConversationSessionRow.session_id == session_id,
                    )
                )
                if owner is None:
                    return [], False
                end_sequence: int | None = None
                if before is not None:
                    end_sequence = await session.scalar(
                        select(ConversationMessageRow.sequence).where(
                            ConversationMessageRow.message_id == before,
                            ConversationMessageRow.user_id == user_id,
                            ConversationMessageRow.agent_id == owner.agent_id,
                            ConversationMessageRow.session_id == session_id,
                        )
                    )
                    if end_sequence is None:
                        return [], False
                conditions = [
                    ConversationMessageRow.user_id == user_id,
                    ConversationMessageRow.agent_id == owner.agent_id,
                    ConversationMessageRow.session_id == session_id,
                ]
                if end_sequence is not None:
                    conditions.append(ConversationMessageRow.sequence < end_sequence)
                rows: Sequence[ConversationMessageRow] = (
                    await session.scalars(
                        select(ConversationMessageRow)
                        .where(*conditions)
                        .order_by(ConversationMessageRow.sequence.desc())
                        .limit(limit + 1)
                    )
                ).all()
                has_more = len(rows) > limit
                selected = list(reversed(rows[:limit]))
                return [Msg.model_validate(row.content_json) for row in selected], has_more
        except (ValidationError, ValueError):
            raise InfrastructureError(
                "INVALID_STORED_CONVERSATION_MESSAGE",
                "长期消息结构无效",
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "CONVERSATION_MESSAGE_READ_FAILED",
                "长期消息列表读取失败",
            ) from None

    async def message_count(self, user_id: str, session_id: str) -> int:
        try:
            async with self.sessions() as session:
                count = await session.scalar(
                    select(func.count(ConversationMessageRow.message_id)).where(
                        ConversationMessageRow.user_id == user_id,
                        ConversationMessageRow.session_id == session_id,
                    )
                )
                return int(count or 0)
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "CONVERSATION_MESSAGE_READ_FAILED",
                "长期消息计数失败",
            ) from None

    async def purge_inactive_before(self, cutoff: datetime) -> int:
        """可选保留策略只删 Conversation；数据库级联仅覆盖 ConversationMessage。"""

        try:
            async with self.sessions.begin() as session:
                result = await session.execute(
                    delete(ConversationSessionRow).where(
                        ConversationSessionRow.last_active_at < _aware(cutoff)
                    )
                )
            return int(getattr(result, "rowcount", 0) or 0)
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "CONVERSATION_RETENTION_FAILED",
                "长期会话保留策略执行失败",
            ) from None


class DurableConversationStorage(RedisStorage):
    """在官方 RedisStorage Contract 上增加 durable archive 和选择性滑动 TTL。"""

    def __init__(
        self,
        database_url: str,
        *,
        session_cache_ttl_seconds: int,
        conversation_retention_days: int | None = None,
        repository: ConversationRepository | None = None,
        engine: AsyncEngine | None = None,
        **redis_kwargs: Any,
    ) -> None:
        # 父类 key_ttl 必须保持 None，否则 Credential/Agent 等资源会被误设会话 TTL。
        redis_kwargs.pop("key_ttl", None)
        super().__init__(key_ttl=None, **redis_kwargs)
        self._database_url = database_url
        self.session_cache_ttl_seconds = session_cache_ttl_seconds
        self.conversation_retention_days = conversation_retention_days
        self._repository = repository
        self._external_engine = engine
        self._conversation_engine: AsyncEngine | None = None

    @property
    def repository(self) -> ConversationRepository:
        """生命周期开始后暴露归档仓库，便于可观测测试。"""

        if self._repository is None:
            raise RuntimeError("Conversation storage 尚未启动")
        return self._repository

    async def __aenter__(self) -> Self:
        """先连接 Redis，再连接已迁移 PostgreSQL；不在应用启动时建表。"""

        await super().__aenter__()
        try:
            if self._repository is None:
                self._conversation_engine = self._external_engine or create_async_engine(
                    self._database_url
                )
                self._repository = PostgreSQLConversationRepository(self._conversation_engine)
            if self.conversation_retention_days is not None:
                await self.repository.purge_inactive_before(
                    datetime.now(UTC) - timedelta(days=self.conversation_retention_days)
                )
            return self
        except BaseException:
            await super().aclose()
            raise

    async def aclose(self) -> None:
        """关闭自建数据库引擎与 Redis；外部注入引擎由调用者负责。"""

        try:
            if self._conversation_engine is not None and self._external_engine is None:
                await self._conversation_engine.dispose()
        finally:
            self._conversation_engine = None
            await super().aclose()

    @staticmethod
    def _origin(
        origin: SessionOrigin | None,
        source: str | None,
        schedule_id: str | None,
        channel_id: str | None,
        chat_id: str | None,
        chat_name: str | None,
    ) -> SessionOrigin:
        """兼容 AgentScope 的旧扁平来源参数，不制造缺失的来源标识。"""

        if origin is not None:
            return origin
        if source == "schedule" and schedule_id:
            return ScheduleOrigin(schedule_id=schedule_id)
        if source == "channel" and channel_id and chat_id:
            return ChannelOrigin(channel_id=channel_id, chat_id=chat_id, chat_name=chat_name)
        if source == "team":
            return TeamOrigin()
        return UserOrigin()

    async def _expire(self, *keys: str) -> None:
        """仅刷新 Conversation keys；其他 AgentScope 资源明确不设置 TTL。"""

        assert self._client is not None
        for key in keys:
            if await self._client.exists(key):
                await self._client.expire(key, self.session_cache_ttl_seconds)

    def _session_key(self, user_id: str, session_id: str) -> str:
        return self._key(self.key_config.session, user_id=user_id, session_id=session_id)

    def _session_index_key(self, user_id: str, agent_id: str) -> str:
        return self._key(self.key_config.session_index, user_id=user_id, agent_id=agent_id)

    async def _cache_session(self, record: SessionRecord) -> None:
        """精确缓存 durable record，保留原始 id 和时间戳。"""

        assert self._client is not None
        session_key = self._session_key(record.user_id, record.id)
        index_key = self._session_index_key(record.user_id, record.agent_id)
        await self._client.set(session_key, record.model_dump_json())
        await self._client.sadd(index_key, record.id)  # type: ignore[misc]
        keys = [session_key, index_key]
        if isinstance(record.origin, ScheduleOrigin):
            key = self._key(
                self.key_config.schedule_session_index,
                user_id=record.user_id,
                schedule_id=record.origin.schedule_id,
            )
            await self._client.sadd(key, record.id)  # type: ignore[misc]
            keys.append(key)
        if isinstance(record.origin, ChannelOrigin):
            key = self._key(
                self.key_config.channel_session_index,
                user_id=record.user_id,
                channel_id=record.origin.channel_id,
            )
            await self._client.sadd(key, record.id)  # type: ignore[misc]
            keys.append(key)
        await self._expire(*keys)

    async def _cache_best_effort(self, record: SessionRecord) -> None:
        """durable 成功后缓存失败不回滚事实；后续读取会再次回填。"""

        try:
            await self._cache_session(record)
        except Exception as exc:
            logger.warning("Conversation cache write failed: type=%s", type(exc).__name__)

    async def upsert_session(
        self,
        user_id: str,
        agent_id: str,
        config: SessionConfig,
        state: AgentState | None = None,
        session_id: str | None = None,
        origin: SessionOrigin | None = None,
        source: str | None = None,
        source_schedule_id: str | None = None,
        source_chat_id: str | None = None,
        source_chat_name: str | None = None,
        source_channel_id: str | None = None,
    ) -> SessionRecord:
        """先写 PostgreSQL；缓存失败不造成已确认消息丢失。"""

        existing = (
            await self.repository.get_session(user_id, agent_id, session_id)
            if session_id
            else None
        )
        if existing is not None:
            existing.config = config
            if state is not None:
                existing.state = state
            existing.updated_at = datetime.now()
            record = existing
        else:
            record_origin = self._origin(
                origin,
                source,
                source_schedule_id,
                source_channel_id,
                source_chat_id,
                source_chat_name,
            )
            if session_id is None:
                record = SessionRecord(
                    user_id=user_id,
                    agent_id=agent_id,
                    config=config,
                    state=state if state is not None else AgentState(),
                    origin=record_origin,
                )
            else:
                record = SessionRecord(
                    id=session_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    config=config,
                    state=state if state is not None else AgentState(),
                    origin=record_origin,
                )
        await self.repository.upsert_session(record)
        await self._cache_best_effort(record)
        return record

    async def set_session_team_id(
        self, user_id: str, session_id: str, team_id: str | None
    ) -> None:
        record = await self.repository.get_session_by_id(user_id, session_id)
        if record is None or record.team_id == team_id:
            return
        record.team_id = team_id
        record.updated_at = datetime.now()
        await self.repository.upsert_session(record)
        await self._cache_best_effort(record)

    async def update_session_state(
        self, user_id: str, agent_id: str, session_id: str, state: AgentState
    ) -> None:
        record = await self.repository.get_session(user_id, agent_id, session_id)
        if record is None:
            # 升级时允许把尚未归档的旧 Redis Session 收编为 durable fact。
            record = await super().get_session(user_id, agent_id, session_id)
        if record is None:
            raise KeyError(f"Session {session_id!r} not found.")
        record.state = state
        record.updated_at = datetime.now()
        await self.repository.upsert_session(record)
        await self._cache_best_effort(record)

    async def list_sessions(self, user_id: str, agent_id: str) -> list[SessionRecord]:
        # 首次部署 10.4 时把旧 Redis 会话安全收编，之后以 PostgreSQL 列表为准。
        cached = await super().list_sessions(user_id, agent_id)
        cached_ids = {record.id for record in cached}
        durable_by_id = {
            record.id: record for record in await self.repository.list_sessions(user_id, agent_id)
        }
        for record in cached:
            if record.id not in durable_by_id:
                durable_by_id[record.id] = await self.repository.upsert_session(record)
        records = sorted(durable_by_id.values(), key=lambda item: item.created_at, reverse=True)
        for record in records:
            # 已命中缓存的记录可能带有效的短期 PendingClarification；列表读取不能覆盖它。
            if record.id not in cached_ids:
                await self._cache_best_effort(record)
        return records

    async def get_session(
        self, user_id: str, agent_id: str, session_id: str
    ) -> SessionRecord | None:
        cached = await super().get_session(user_id, agent_id, session_id)
        if cached is not None:
            durable = await self.repository.get_session(user_id, agent_id, session_id)
            if durable is None:
                await self.repository.upsert_session(cached)
            await self._expire(
                self._session_key(user_id, session_id),
                self._session_index_key(user_id, agent_id),
                self._message_key(user_id, session_id),
            )
            return cached
        durable = await self.repository.get_session(user_id, agent_id, session_id)
        if durable is not None:
            await self._cache_best_effort(durable)
        return durable

    async def delete_session(self, user_id: str, agent_id: str, session_id: str) -> bool:
        """删除 durable Conversation 和 cache；不触碰 SessionTaskBinding/Task。"""

        durable = await self.repository.delete_session(user_id, agent_id, session_id)
        cached = await super().delete_session(user_id, agent_id, session_id)
        return durable or cached

    async def list_sessions_by_schedule(
        self, user_id: str, schedule_id: str
    ) -> list[SessionRecord]:
        records = await self.repository.list_sessions_by_origin(user_id, "schedule", schedule_id)
        for record in records:
            await self._cache_best_effort(record)
        return records

    async def list_sessions_by_channel(
        self, user_id: str, channel_id: str
    ) -> list[SessionRecord]:
        records = await self.repository.list_sessions_by_origin(user_id, "channel", channel_id)
        for record in records:
            await self._cache_best_effort(record)
        return records

    async def _archive_legacy_messages(self, user_id: str, session_id: str) -> None:
        """按原 Redis 顺序一次性收编升级前历史，避免分页导入打乱 sequence。"""

        if await self.repository.message_count(user_id, session_id):
            return
        assert self._client is not None
        raw_messages = await self._client.lrange(  # type: ignore[misc]
            self._message_key(user_id, session_id), 0, -1
        )
        for raw in raw_messages:
            await self.repository.upsert_message(user_id, session_id, Msg.model_validate_json(raw))

    async def upsert_message(self, user_id: str, session_id: str, msg: Msg) -> None:
        session = await self.repository.get_session_by_id(user_id, session_id)
        if session is None:
            # 兼容升级前已存在 Redis、尚未触发 Session 读取的会话。
            assert self._client is not None
            raw = await self._client.get(self._session_key(user_id, session_id))
            if raw:
                session = SessionRecord.model_validate_json(raw)
                await self.repository.upsert_session(session)
        await self._archive_legacy_messages(user_id, session_id)
        await self.repository.upsert_message(user_id, session_id, msg)
        try:
            await super().upsert_message(user_id, session_id, msg)
            if session is not None:
                await self._expire(
                    self._message_key(user_id, session_id),
                    self._session_key(user_id, session_id),
                    self._session_index_key(user_id, session.agent_id),
                )
        except Exception as exc:
            logger.warning("Conversation message cache failed: type=%s", type(exc).__name__)

    async def get_message(
        self, user_id: str, session_id: str, message_id: str
    ) -> Msg | None:
        await self._archive_legacy_messages(user_id, session_id)
        return await self.repository.get_message(user_id, session_id, message_id)

    async def list_messages(
        self,
        user_id: str,
        session_id: str,
        limit: int = 50,
        before: str | None = None,
        **kwargs: Any,
    ) -> tuple[list[Msg], bool]:
        """历史分页始终读 PostgreSQL；AgentState Context 仍由框架压缩控制。"""

        del kwargs
        await self._archive_legacy_messages(user_id, session_id)
        return await self.repository.list_messages(user_id, session_id, limit, before)
