import asyncio
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
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
from cnlc_agent.domain.errors import DataError, InfrastructureError
from cnlc_agent.domain.execution import ExecutionStatus
from cnlc_agent.domain.inputs import fixture_digest
from cnlc_agent.domain.models import MockFixture, TaskRequest, utc_now
from cnlc_agent.domain.override import InterpretationOverride
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
    repository.save_execution_state.side_effect = InfrastructureError(
        "DATABASE_WRITE_FAILED", "failed"
    )
    with pytest.raises(InfrastructureError):
        await CheckpointStore(repository, cache).save(
            InterpretationState(task=TaskRequest(well_id="WELL_1"))
        )
    cache.save.assert_not_awaited()


async def finish_execution(repository, state, status):
    """测试仓库时按正式生命周期结束执行，避免跳过 active 保护。"""

    worker = "test-worker"
    assert await repository.claim_execution(
        state.workflow_execution_id, worker, utc_now() + timedelta(minutes=1)
    )
    await repository.finish_execution(state.workflow_execution_id, worker, status)


async def test_duplicate_task_is_rejected_without_overwriting(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    request = TaskRequest(well_id="WELL_MOCK_001")
    state, report = await app.run(request)
    with pytest.raises(InfrastructureError) as caught:
        await app.run(request)
    assert caught.value.code == "TASK_EXISTS"
    assert await app.repository.get(request.task_id) == state
    assert await app.repository.get_report(request.task_id) == report


async def test_execution_versions_keep_independent_state_and_report():
    repository = InMemoryTaskRepository()
    request = TaskRequest(well_id="WELL_1")
    first = InterpretationState(task=request)
    await repository.create(first)
    first.status = StepStatus.SUCCESS
    first.warnings.append("first version")
    await repository.save(first, "report one")
    await finish_execution(repository, first, ExecutionStatus.SUCCESS)
    first_saved = await repository.get_execution(first.workflow_execution_id)
    assert first_saved is not None

    second = InterpretationState(task=request)
    created = await repository.create_execution(second, "RERUN")
    assert created.sequence == 2
    task = await repository.get_task(request.task_id)
    assert task is not None
    assert task.current_execution_id == second.workflow_execution_id
    assert task.latest_successful_execution_id == first.workflow_execution_id
    second.status = StepStatus.FAILED
    second.warnings.append("second version")
    await repository.save(second, "report two")
    await finish_execution(repository, second, ExecutionStatus.FAILED)

    versions = await repository.list_executions(request.task_id)
    assert [item.sequence for item in versions] == [1, 2]
    assert [item.trigger_type for item in versions] == ["INITIAL", "RERUN"]
    assert versions[0] == first_saved
    assert versions[0].state_snapshot.warnings == ["first version"]
    assert versions[0].markdown == "report one"
    assert versions[1].state_snapshot.warnings == ["second version"]
    assert versions[1].markdown == "report two"
    with pytest.raises(InfrastructureError) as caught:
        await repository.save(first, "overwrite")
    assert caught.value.code == "EXECUTION_NOT_CURRENT"
    with pytest.raises(InfrastructureError) as caught:
        await repository.set_current_execution(request.task_id, first.workflow_execution_id)
    assert caught.value.code == "EXECUTION_NOT_LATEST"
    assert (await repository.get_execution(first.workflow_execution_id)) == first_saved
    assert await repository.get(request.task_id) == second
    assert await repository.get_report(request.task_id) == "report two"
    task = await repository.get_task(request.task_id)
    assert task is not None
    assert task.latest_successful_execution_id == first.workflow_execution_id


async def test_execution_creation_conflicts_and_missing_task():
    repository = InMemoryTaskRepository()
    request = TaskRequest(well_id="WELL_1")
    first = InterpretationState(task=request)
    with pytest.raises(InfrastructureError) as caught:
        await repository.create_execution(first)
    assert caught.value.code == "TASK_NOT_FOUND"

    await repository.create(first)
    with pytest.raises(InfrastructureError) as caught:
        await repository.create_execution(first)
    assert caught.value.code == "EXECUTION_EXISTS"

    second = InterpretationState(task=request)
    with pytest.raises(InfrastructureError) as caught:
        await repository.create_execution(second, sequence=1)
    assert caught.value.code == "TASK_EXECUTION_ACTIVE"
    await finish_execution(repository, first, ExecutionStatus.FAILED)
    with pytest.raises(InfrastructureError) as caught:
        await repository.create_execution(second, sequence=1)
    assert caught.value.code == "EXECUTION_SEQUENCE_EXISTS"
    task = await repository.get_task(request.task_id)
    assert task is not None
    assert task.current_execution_id == first.workflow_execution_id
    assert len(await repository.list_executions(request.task_id)) == 1


async def test_concurrent_execution_creation_allows_only_one_active():
    repository = InMemoryTaskRepository()
    request = TaskRequest(well_id="WELL_1")
    first = InterpretationState(task=request)
    await repository.create(first)
    states = [InterpretationState(task=request) for _ in range(10)]
    await finish_execution(repository, first, ExecutionStatus.SUCCESS)
    results = await asyncio.gather(
        *(repository.create_execution(state) for state in states), return_exceptions=True
    )
    created = [item for item in results if not isinstance(item, Exception)]
    rejected = [item for item in results if isinstance(item, InfrastructureError)]
    assert len(created) == 1 and created[0].sequence == 2
    assert len(rejected) == 9
    assert all(item.code == "TASK_EXECUTION_ACTIVE" for item in rejected)


async def test_service_rerun_creates_new_complete_execution(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    request = TaskRequest(well_id="WELL_MOCK_001")
    first_state, first_report = await app.run(request)
    second_state, second_report = await app.rerun(request)
    assert first_state.workflow_execution_id != second_state.workflow_execution_id
    assert len(first_state.completed_steps) == len(second_state.completed_steps) == 10
    versions = await app.repository.list_executions(request.task_id)
    assert [item.sequence for item in versions] == [1, 2]
    assert versions[0].state_snapshot == first_state
    assert versions[0].markdown == first_report
    assert versions[1].state_snapshot == second_state
    assert versions[1].markdown == second_report
    task = await app.repository.get_task(request.task_id)
    assert task is not None
    assert task.current_execution_id == second_state.workflow_execution_id
    assert task.latest_successful_execution_id == second_state.workflow_execution_id


async def test_uploaded_input_survives_temporary_directory_and_reruns(data_dir):
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    original_raw_data = fixture.raw_data.model_copy(deep=True)
    repository = InMemoryTaskRepository()
    request = TaskRequest(well_id=fixture.well.well_id)

    with TemporaryDirectory(prefix="cnlc-test-input-") as directory:
        root = Path(directory)
        app = build_application(
            AppSettings(mock_data_dir=root, _env_file=None), task_repository=repository
        )

        async def materialize(version):
            (root / f"{version.well_id}.json").write_text(version.payload.model_dump_json())

        first_state, first_report = await app.run_with_input(request, fixture, materialize)
        assert (root / f"{fixture.well.well_id}.json").exists()
    assert not await asyncio.to_thread(root.exists)

    inputs = await repository.list_input_versions(request.task_id)
    assert len(inputs) == 1
    first_input = inputs[0]
    assert first_input.task_id == request.task_id
    assert first_input.well_id == fixture.well.well_id
    assert first_input.sequence == 1
    assert first_input.source_type == "UPLOAD"
    assert first_input.payload == fixture
    assert first_input.content_sha256 == fixture_digest(fixture)
    assert (await repository.get_input_version(first_input.input_version_id)).payload == fixture
    first_execution = await repository.get_execution(first_state.workflow_execution_id)
    assert first_execution is not None
    assert first_execution.input_version_id == first_input.input_version_id
    assert first_execution.override_snapshot == InterpretationOverride()
    assert first_execution.markdown == first_report

    changed = fixture.model_copy(deep=True)
    changed.well.name = "第二份规范化输入"
    second_input = await repository.create_input_version(request.task_id, changed)
    assert second_input.sequence == 2
    assert second_input.input_version_id != first_input.input_version_id
    assert second_input.content_sha256 != first_input.content_sha256
    assert (await repository.get_input_version(first_input.input_version_id)) == first_input
    task = await repository.get_task(request.task_id)
    assert task is not None
    assert task.current_input_version_id == second_input.input_version_id

    with TemporaryDirectory(prefix="cnlc-test-rerun-") as directory:
        root = Path(directory)
        app = build_application(
            AppSettings(mock_data_dir=root, _env_file=None), task_repository=repository
        )

        async def materialize_old(version):
            (root / f"{version.well_id}.json").write_text(version.payload.model_dump_json())

        second_state, _ = await app.rerun(
            request,
            input_version_id=first_input.input_version_id,
            override=InterpretationOverride(por=0.16, perm=0.16),
            materialize=materialize_old,
        )
        third_state, _ = await app.rerun(
            request,
            input_version_id=first_input.input_version_id,
            override=InterpretationOverride(
                sampling_interval=0.1, por=0.18, prediction_model="prediction-v2"
            ),
            materialize=materialize_old,
        )

    executions = await repository.list_executions(request.task_id)
    assert [item.sequence for item in executions] == [1, 2, 3]
    assert all(item.input_version_id == first_input.input_version_id for item in executions)
    assert [item.override_snapshot.por for item in executions] == [None, 0.16, 0.18]
    assert executions[1].override_snapshot.perm == 0.16
    assert executions[2].override_snapshot.sampling_interval == 0.1
    assert executions[2].override_snapshot.prediction_model == "prediction-v2"
    assert executions[0].markdown == first_report
    assert executions[0].state_snapshot == first_state
    assert executions[1].state_snapshot == second_state
    assert executions[2].state_snapshot == third_state
    assert fixture.raw_data == original_raw_data
    assert (await repository.get_input_version(first_input.input_version_id)) == first_input
    assert all(item.state_snapshot.raw_data == original_raw_data for item in executions)
    assert all(len(item.state_snapshot.completed_steps) == 10 for item in executions)


async def test_input_version_cannot_cross_task_or_fake_empty_override(data_dir):
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    repository = InMemoryTaskRepository()
    first_request = TaskRequest(well_id=fixture.well.well_id)
    second_request = TaskRequest(well_id=fixture.well.well_id)
    await repository.create(InterpretationState(task=first_request))
    await repository.create(InterpretationState(task=second_request))
    first_input = await repository.create_input_version(first_request.task_id, fixture)
    with pytest.raises(InfrastructureError) as caught:
        await repository.create_execution(
            InterpretationState(task=second_request), input_version_id=first_input.input_version_id
        )
    assert caught.value.code == "INPUT_VERSION_TASK_MISMATCH"
    assert len(await repository.list_executions(second_request.task_id)) == 1

    app = build_application(
        AppSettings(mock_data_dir=data_dir, _env_file=None), task_repository=repository
    )
    with pytest.raises(DataError) as caught:
        await app.rerun(first_request, override=InterpretationOverride())
    assert caught.value.code == "EMPTY_OVERRIDE"


async def test_rejected_input_does_not_move_task_pointer(data_dir):
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    repository = InMemoryTaskRepository()
    request = TaskRequest(well_id="OTHER_WELL")
    await repository.create_task(InterpretationState(task=request))
    with pytest.raises(InfrastructureError) as caught:
        await repository.create_input_version(request.task_id, fixture)
    assert caught.value.code == "TASK_WELL_MISMATCH"
    task = await repository.get_task(request.task_id)
    assert task is not None and task.current_input_version_id is None
    assert await repository.list_input_versions(request.task_id) == []


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
