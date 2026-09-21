from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.checkpoints import CheckpointStore
from cnlc_agent.application.runtime import application_runtime, database_url
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.database import PostgreSQLTaskRepository
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository
from cnlc_agent.infrastructure.redis_store import RedisInterpretationStateStore


@pytest.mark.parametrize("url", [None, "", "bad-secret", "sqlite:///file.db"])
def test_database_config_fails_safely(url):
    with pytest.raises(InfrastructureError) as caught:
        database_url(ConnectionSettings(database_url=url, _env_file=None))
    assert "bad-secret" not in str(caught.value)


async def test_real_mode_never_falls_back_to_memory():
    with pytest.raises(InfrastructureError, match="DATABASE_URL"):
        async with application_runtime(
            AppSettings(_env_file=None),
            PersistenceSettings(persistence="postgres-redis", _env_file=None),
            ConnectionSettings(_env_file=None),
        ):
            pytest.fail("must not start without configuration")


async def test_redis_roundtrip_key_and_expiry_arguments():
    client = AsyncMock()
    state = InterpretationState(task=TaskRequest(task_id="a:/b", well_id="WELL_1"))
    store = RedisInterpretationStateStore(client, "test:v1", 60)
    await store.save(state)
    key, payload = client.set.call_args.args
    assert key.startswith("test:v1:state:")
    assert "a:/b" not in key
    assert client.set.call_args.kwargs == {"ex": 60}
    client.get.return_value = payload
    assert await store.get("a:/b") == state
    client.get.return_value = None
    assert await store.get("a:/b") is None


async def test_redis_corruption_and_connection_errors():
    client = AsyncMock()
    store = RedisInterpretationStateStore(client, "test", 60)
    client.get.return_value = "{invalid"
    with pytest.raises(InfrastructureError) as caught:
        await store.get("task")
    assert caught.value.code == "INVALID_CACHED_STATE"
    client.get.side_effect = RedisConnectionError("secret URL")
    with pytest.raises(InfrastructureError) as caught:
        await store.get("task")
    assert caught.value.code == "REDIS_READ_FAILED"
    assert "secret" not in str(caught.value)
    client.set.side_effect = RedisConnectionError("secret URL")
    with pytest.raises(InfrastructureError) as caught:
        await store.save(InterpretationState(task=TaskRequest(well_id="WELL_1")))
    assert caught.value.code == "REDIS_WRITE_FAILED"


async def test_cache_failure_preserves_durable_failed_task(data_dir, caplog):
    repository = InMemoryTaskRepository()
    cache = AsyncMock()
    cache.save.side_effect = InfrastructureError("REDIS_WRITE_FAILED", "Redis 状态保存失败")
    app = build_application(
        AppSettings(mock_data_dir=data_dir, _env_file=None),
        task_repository=repository,
        state_store=CheckpointStore(repository, cache),
    )
    state, markdown = await app.run(TaskRequest(well_id="WELL_MOCK_001"))
    assert state.status == StepStatus.FAILED
    assert not state.completed_steps
    assert state.errors[-1].code == "REDIS_WRITE_FAILED"
    assert state.executions[-1].status == StepStatus.FAILED
    assert (await repository.get(state.task.task_id)) == state
    assert await repository.get_report(state.task.task_id) == markdown
    assert "诊断摘要" in markdown


async def test_database_checkpoint_failure_does_not_write_cache():
    repository, cache = AsyncMock(), AsyncMock()
    repository.save.side_effect = InfrastructureError("DATABASE_WRITE_FAILED", "failed")
    with pytest.raises(InfrastructureError):
        await CheckpointStore(repository, cache).save(
            InterpretationState(task=TaskRequest(well_id="WELL_1"))
        )
    cache.save.assert_not_awaited()


async def test_duplicate_task_is_rejected_without_overwriting(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    request = TaskRequest(well_id="WELL_MOCK_001")
    state, report = await app.run(request)
    with pytest.raises(InfrastructureError) as caught:
        await app.run(request)
    assert caught.value.code == "TASK_EXISTS"
    assert await app.repository.get(request.task_id) == state
    assert await app.repository.get_report(request.task_id) == report


async def test_sql_errors_are_sanitized():
    engine = create_async_engine("postgresql+asyncpg://localhost/test")
    repository = PostgreSQLTaskRepository(engine)
    sessions = AsyncMock()
    sessions.__aenter__.side_effect = OperationalError("secret SQL", {}, Exception("password"))
    repository.sessions = lambda: sessions
    try:
        with pytest.raises(InfrastructureError) as caught:
            await repository.get("x")
        assert caught.value.code == "DATABASE_READ_FAILED"
        assert "secret" not in str(caught.value)
        assert "password" not in str(caught.value)
    finally:
        await engine.dispose()


async def test_runtime_closes_resources_on_application_error(monkeypatch):
    import cnlc_agent.application.runtime as runtime

    client, engine = AsyncMock(), AsyncMock()
    monkeypatch.setattr(runtime.Redis, "from_url", lambda *args, **kwargs: client)
    monkeypatch.setattr(runtime, "create_async_engine", lambda *args, **kwargs: engine)
    with pytest.raises(RuntimeError):
        async with application_runtime(
            AppSettings(_env_file=None),
            PersistenceSettings(persistence="postgres-redis", _env_file=None),
            ConnectionSettings(
                database_url="postgresql+asyncpg://localhost/test",
                redis_url="redis://localhost",
                _env_file=None,
            ),
        ):
            raise RuntimeError("test")
    client.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()


async def test_closed_database_socket_is_classified():
    import socket

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        engine = create_async_engine(
            f"postgresql+asyncpg://test:test@127.0.0.1:{port}/test",
            connect_args={"timeout": 0.2},
        )
        try:
            with pytest.raises(InfrastructureError) as caught:
                await PostgreSQLTaskRepository(engine).create(
                    InterpretationState(task=TaskRequest(well_id="WELL_1"))
                )
            assert caught.value.code == "DATABASE_WRITE_FAILED"
        finally:
            await engine.dispose()


async def test_closed_redis_socket_is_classified():
    import socket

    from redis.asyncio import Redis

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        client = Redis(host="127.0.0.1", port=reserved.getsockname()[1], socket_connect_timeout=0.2)
        try:
            with pytest.raises(InfrastructureError) as caught:
                await RedisInterpretationStateStore(client, "test", 60).get("missing")
            assert caught.value.code == "REDIS_READ_FAILED"
        finally:
            await client.aclose()
