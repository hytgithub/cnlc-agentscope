"""Resource lifecycle and explicit persistence selection; model/data remain Mock."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import create_async_engine

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.checkpoints import CheckpointStore
from cnlc_agent.application.service import InterpretationTaskService
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.infrastructure.database import PostgreSQLTaskRepository
from cnlc_agent.infrastructure.redis_store import RedisInterpretationStateStore


def database_url(connections: ConnectionSettings) -> str:
    if connections.database_url is None:
        raise InfrastructureError("DATABASE_CONFIG_REQUIRED", "请配置 DATABASE_URL")
    value = connections.database_url.get_secret_value()
    try:
        parsed = make_url(value)
        if parsed.drivername != "postgresql+asyncpg" or not parsed.database:
            raise ValueError("unsupported database URL")
    except (ValueError, TypeError, ArgumentError):
        raise InfrastructureError(
            "INVALID_DATABASE_CONFIG", "需要 postgresql+asyncpg 数据库地址"
        ) from None
    return value


@asynccontextmanager
async def application_runtime(
    settings: AppSettings,
    persistence: PersistenceSettings | None = None,
    connections: ConnectionSettings | None = None,
) -> AsyncIterator[InterpretationTaskService]:
    persistence = persistence or PersistenceSettings()
    if persistence.persistence == "memory":
        yield build_application(settings)
        return
    connections = connections or ConnectionSettings()
    url = database_url(connections)
    if connections.redis_url is None:
        raise InfrastructureError("REDIS_CONFIG_REQUIRED", "请配置 REDIS_URL")
    timeout = persistence.infrastructure_timeout_seconds
    try:
        client = Redis.from_url(
            connections.redis_url.get_secret_value(),
            decode_responses=True,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
        )
    except (ValueError, TypeError):
        raise InfrastructureError("INVALID_REDIS_CONFIG", "Redis 地址配置无效") from None
    engine = create_async_engine(
        url,
        pool_pre_ping=True,
        pool_timeout=timeout,
        hide_parameters=True,
        connect_args={"timeout": timeout, "command_timeout": timeout},
    )
    try:
        repository = PostgreSQLTaskRepository(engine)
        cache = RedisInterpretationStateStore(
            client,
            persistence.redis_prefix,
            persistence.redis_ttl_seconds,
        )
        yield build_application(
            settings,
            task_repository=repository,
            state_store=CheckpointStore(repository, cache),
        )
    finally:
        try:
            await client.aclose()
        finally:
            await engine.dispose()
