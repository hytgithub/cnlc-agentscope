"""Opt-in real servers. Use dedicated test services; never substitutes SQLite/fake Redis."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from cnlc_agent.application.runtime import application_runtime
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.models import MockFixture, TaskRequest
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.database import (
    ExecutionRow,
    InputVersionRow,
    PostgreSQLTaskRepository,
    TaskRow,
)
from cnlc_agent.infrastructure.redis_store import RedisInterpretationStateStore

ROOT = Path(__file__).resolve().parents[2]
DB_URL = os.environ.get("CNLC_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("CNLC_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not (DB_URL and REDIS_URL),
    reason="Set CNLC_TEST_DATABASE_URL and CNLC_TEST_REDIS_URL for real services",
)


@pytest.fixture(scope="module")
def migrated():
    env = {**os.environ, "DATABASE_URL": DB_URL}
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, "Real PostgreSQL migration failed; check local configuration"


async def test_real_workflow_restart_and_duplicate(migrated, tmp_path):
    prefix = "cnlc:test:" + uuid4().hex
    persistence = PersistenceSettings(
        persistence="postgres-redis",
        redis_prefix=prefix,
        redis_ttl_seconds=60,
        _env_file=None,
    )
    connections = ConnectionSettings(database_url=DB_URL, redis_url=REDIS_URL, _env_file=None)
    settings = AppSettings(mock_data_dir=ROOT / "mock_data", _env_file=None)
    request = TaskRequest(well_id="WELL_MOCK_001")
    engine = create_async_engine(DB_URL)
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    store = RedisInterpretationStateStore(client, prefix, 60)
    try:
        async with application_runtime(settings, persistence, connections) as app:
            state, report = await app.run(request)
            assert state.status.value == "SUCCESS"
            assert len(state.completed_steps) == 10
            assert await store.get(request.task_id) == state
            assert 0 < await client.ttl(store.key(request.task_id)) <= 60
            with pytest.raises(InfrastructureError) as caught:
                await app.run(request)
            assert caught.value.code == "TASK_EXISTS"
        # Fresh process/connection verifies history does not depend on local memory.
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "cnlc_agent.main",
                "--task-id",
                request.task_id,
                "--output-dir",
                str(tmp_path),
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "CNLC_PERSISTENCE": "postgres-redis",
                "DATABASE_URL": DB_URL,
                "REDIS_URL": REDIS_URL,
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, "Historical task query failed"
        assert (
            InterpretationState.model_validate_json(
                (tmp_path / request.task_id / "result.json").read_text()
            )
            == state
        )
        assert (tmp_path / request.task_id / "report.md").read_text() == report
    finally:
        await client.delete(store.key(request.task_id))
        async with engine.begin() as connection:
            await connection.execute(delete(TaskRow).where(TaskRow.task_id == request.task_id))
        await client.aclose()
        await engine.dispose()


async def test_real_cache_expiry_and_corruption(migrated):
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    store = RedisInterpretationStateStore(client, "cnlc:test:" + uuid4().hex, 1)
    state = InterpretationState(task=TaskRequest(well_id="WELL_1"))
    key = store.key(state.task.task_id)
    try:
        await store.save(state)
        await asyncio.sleep(1.1)
        assert await store.get(state.task.task_id) is None
        await client.set(key, "{corrupt", ex=30)
        with pytest.raises(InfrastructureError) as caught:
            await store.get(state.task.task_id)
        assert caught.value.code == "INVALID_CACHED_STATE"
    finally:
        await client.delete(key)
        await client.aclose()


async def test_real_failed_workflow_report_is_durable(migrated):
    request = TaskRequest(well_id="MISSING")
    prefix = "cnlc:test:" + uuid4().hex
    engine = create_async_engine(DB_URL)
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    store = RedisInterpretationStateStore(client, prefix, 60)
    try:
        async with application_runtime(
            AppSettings(mock_data_dir=ROOT / "mock_data", _env_file=None),
            PersistenceSettings(persistence="postgres-redis", redis_prefix=prefix, _env_file=None),
            ConnectionSettings(database_url=DB_URL, redis_url=REDIS_URL, _env_file=None),
        ) as app:
            state, markdown = await app.run(request)
            assert state.status.value == "FAILED"
            assert state.errors[-1].code == "WELL_NOT_FOUND"
        repository = PostgreSQLTaskRepository(engine)
        assert await repository.get(request.task_id) == state
        assert await repository.get_report(request.task_id) == markdown
    finally:
        await client.delete(store.key(request.task_id))
        async with engine.begin() as connection:
            await connection.execute(delete(TaskRow).where(TaskRow.task_id == request.task_id))
        await client.aclose()
        await engine.dispose()


async def test_postgresql_execution_versions_match_memory_semantics(migrated):
    """真实 PostgreSQL 验证内存仓库相同的多版本、指针及冲突契约。"""

    request = TaskRequest(well_id="WELL_1")
    engine = create_async_engine(DB_URL)
    repository = PostgreSQLTaskRepository(engine)
    first = InterpretationState(task=request)
    try:
        with pytest.raises(InfrastructureError) as caught:
            await repository.create_execution(first)
        assert caught.value.code == "TASK_NOT_FOUND"
        await repository.create(first)
        first.status = StepStatus.SUCCESS
        first.warnings.append("first")
        await repository.save(first, "report one")
        with pytest.raises(InfrastructureError) as caught:
            await repository.create_execution(first)
        assert caught.value.code == "EXECUTION_EXISTS"

        second = InterpretationState(task=request)
        with pytest.raises(InfrastructureError) as caught:
            await repository.create_execution(second, sequence=1)
        assert caught.value.code == "EXECUTION_SEQUENCE_EXISTS"
        created = await repository.create_execution(second)
        assert created.sequence == 2
        second.status = StepStatus.FAILED
        second.warnings.append("second")
        await repository.save(second, "report two")
        versions = await repository.list_executions(request.task_id)
        assert [item.sequence for item in versions] == [1, 2]
        assert versions[0].state_snapshot.warnings == ["first"]
        assert versions[0].markdown == "report one"
        assert versions[1].state_snapshot.warnings == ["second"]
        assert versions[1].markdown == "report two"
        task = await repository.get_task(request.task_id)
        assert task is not None
        assert task.current_execution_id == second.workflow_execution_id
        assert task.latest_successful_execution_id == first.workflow_execution_id
        with pytest.raises(InfrastructureError) as caught:
            await repository.set_current_execution(request.task_id, first.workflow_execution_id)
        assert caught.value.code == "EXECUTION_NOT_LATEST"
        assert await repository.get(request.task_id) == second
        assert await repository.get_report(request.task_id) == "report two"
    finally:
        async with engine.begin() as connection:
            await connection.execute(delete(TaskRow).where(TaskRow.task_id == request.task_id))
        await engine.dispose()


async def test_postgresql_input_version_and_override_survive_new_repository(migrated):
    """真实数据库跨仓库实例读回输入快照，且不允许跨任务绑定。"""

    fixture = MockFixture.model_validate_json((ROOT / "mock_data/WELL_MOCK_001.json").read_text())
    request = TaskRequest(well_id=fixture.well.well_id)
    other_request = TaskRequest(well_id=fixture.well.well_id)
    engine = create_async_engine(DB_URL)
    repository = PostgreSQLTaskRepository(engine)
    first = InterpretationState(task=request)
    try:
        await repository.create_task(first)
        input_one = await repository.create_input_version(request.task_id, fixture)
        await repository.create_execution(
            first, "INITIAL", input_version_id=input_one.input_version_id
        )
        first.status = StepStatus.SUCCESS
        await repository.save(first, "report one")
        changed = fixture.model_copy(deep=True)
        changed.well.name = "第二份上传输入"
        input_two = await repository.create_input_version(request.task_id, changed)
        assert input_two.sequence == 2

        second = InterpretationState(task=request)
        override = InterpretationOverride(
            sampling_interval=0.1, por=0.16, perm=0.16, prediction_model="prediction-v2"
        )
        await repository.create_execution(
            second, input_version_id=input_one.input_version_id, override_snapshot=override
        )
        second.status = StepStatus.FAILED
        await repository.save(second, "report two")

        reloaded = PostgreSQLTaskRepository(engine)
        assert (await reloaded.get_input_version(input_one.input_version_id)) == input_one
        assert [item.sequence for item in await reloaded.list_input_versions(request.task_id)] == [
            1,
            2,
        ]
        versions = await reloaded.list_executions(request.task_id)
        assert [item.input_version_id for item in versions] == [
            input_one.input_version_id,
            input_one.input_version_id,
        ]
        assert versions[0].override_snapshot == InterpretationOverride()
        assert versions[1].override_snapshot == override
        assert versions[0].markdown == "report one"
        assert versions[1].markdown == "report two"
        assert input_one.payload.raw_data == fixture.raw_data
        task = await reloaded.get_task(request.task_id)
        assert task is not None
        assert task.current_input_version_id == input_two.input_version_id
        assert task.current_execution_id == second.workflow_execution_id
        assert task.latest_successful_execution_id == first.workflow_execution_id

        await repository.create_task(InterpretationState(task=other_request))
        with pytest.raises(InfrastructureError) as caught:
            await repository.create_execution(
                InterpretationState(task=other_request),
                input_version_id=input_one.input_version_id,
            )
        assert caught.value.code == "INPUT_VERSION_TASK_MISMATCH"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(ExecutionRow).where(ExecutionRow.task_id == request.task_id)
            )
            await connection.execute(
                delete(InputVersionRow).where(InputVersionRow.task_id == request.task_id)
            )
            await connection.execute(
                delete(TaskRow).where(TaskRow.task_id.in_([request.task_id, other_request.task_id]))
            )
        await engine.dispose()
