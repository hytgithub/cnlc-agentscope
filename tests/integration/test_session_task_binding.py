"""真实 PostgreSQL/Redis 上验证 Session ↔ Task 绑定跨实例恢复。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import create_async_engine

from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.demo.agentscope_app import SessionTaskToolFactory, create_demo_app
from cnlc_agent.domain.models import MockFixture, TaskRequest
from cnlc_agent.domain.session_binding import SessionTaskBinding, TaskSessionIdentity
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.database import (
    PostgreSQLTaskRepository,
    SessionTaskBindingRow,
    TaskRow,
)

ROOT = Path(__file__).resolve().parents[2]
DB_URL = os.environ.get("CNLC_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("CNLC_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not (DB_URL and REDIS_URL),
    reason="Set CNLC_TEST_DATABASE_URL and CNLC_TEST_REDIS_URL for real services",
)


@pytest.fixture(scope="module")
def migrated_session_binding():
    """真实库升级到包含 0006 的 head；重复执行必须幂等。"""

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": DB_URL},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, "Real PostgreSQL migration failed"


async def test_postgresql_binding_repository_idempotence_isolation_and_cascade(
    migrated_session_binding,
):
    """真实表支持幂等、多任务、四元组隔离和 Task 删除级联。"""

    del migrated_session_binding
    engine = create_async_engine(DB_URL)
    repository = PostgreSQLTaskRepository(engine)
    first = TaskRequest(well_id="WELL_BIND_1")
    second = TaskRequest(well_id="WELL_BIND_2")
    identity = TaskSessionIdentity(user_id="alice", agent_id="agent-a", session_id="session-a")
    try:
        await repository.create_task(InterpretationState(task=first))
        await repository.create_task(InterpretationState(task=second))
        binding = SessionTaskBinding(
            **identity.model_dump(mode="python"), task_id=first.task_id
        )
        await repository.bind_task_to_session(binding)
        await repository.bind_task_to_session(binding)
        await repository.bind_task_to_session(SessionTaskBinding(
            **identity.model_dump(mode="python"), task_id=second.task_id
        ))

        reopened_engine = create_async_engine(DB_URL)
        reopened = PostgreSQLTaskRepository(reopened_engine)
        try:
            assert await reopened.task_belongs_to_session(identity, first.task_id)
            assert set(await reopened.list_session_task_ids(identity)) == {
                first.task_id,
                second.task_id,
            }
            for other in [
                TaskSessionIdentity(user_id="bob", agent_id="agent-a", session_id="session-a"),
                TaskSessionIdentity(user_id="alice", agent_id="agent-b", session_id="session-a"),
                TaskSessionIdentity(user_id="alice", agent_id="agent-a", session_id="session-b"),
            ]:
                assert not await reopened.task_belongs_to_session(other, first.task_id)
        finally:
            await reopened_engine.dispose()

        async with engine.begin() as connection:
            count = await connection.scalar(
                select(func.count())
                .select_from(SessionTaskBindingRow)
                .where(
                    SessionTaskBindingRow.user_id == identity.user_id,
                    SessionTaskBindingRow.agent_id == identity.agent_id,
                    SessionTaskBindingRow.session_id == identity.session_id,
                )
            )
            assert count == 2
            await connection.execute(delete(TaskRow).where(TaskRow.task_id == first.task_id))
        assert not await repository.task_belongs_to_session(identity, first.task_id)
        assert await repository.task_belongs_to_session(identity, second.task_id)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(TaskRow).where(TaskRow.task_id.in_([first.task_id, second.task_id]))
            )
        await engine.dispose()


async def test_factory_task_tool_and_read_api_restore_after_restart(
    migrated_session_binding, tmp_path, monkeypatch
):
    """丢弃全部 runner 后，Read API 和 Task Tool 都从 PostgreSQL binding 恢复。"""

    del migrated_session_binding
    prefix = f"cnlc:test:binding:{uuid4().hex}"
    settings = AppSettings(
        mode="demo", model_provider="mock", mock_data_dir=ROOT / "mock_data", _env_file=None
    )
    persistence = PersistenceSettings(
        persistence="postgres-redis", redis_prefix=prefix, _env_file=None
    )
    connections = ConnectionSettings(
        database_url=DB_URL, redis_url=REDIS_URL, _env_file=None
    )
    engine = create_async_engine(DB_URL)
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    task_id = ""
    first_factory = SessionTaskToolFactory(
        settings=settings, persistence=persistence, connections=connections
    )
    try:
        first_tools = {
            tool.name: tool for tool in await first_factory("alice", "agent-a", "session-a")
        }
        started = await first_tools["run_well_interpretation"].call(
            well_id="WELL_MOCK_001"
        )
        first = started.metadata["result"]
        task_id = first["task_id"]
        await first_tools["get_interpretation_status"].runner.wait_for_completion(
            task_id, first["execution_id"]
        )
        await first_factory.shutdown()

        restored_factory = SessionTaskToolFactory(
            settings=settings, persistence=persistence, connections=connections
        )
        assert restored_factory.runners == {}
        restored_tools = {
            tool.name: tool
            for tool in await restored_factory("alice", "agent-a", "session-a")
        }
        restored_runner = restored_tools["get_interpretation_status"].runner
        assert task_id in restored_runner.observed_task_ids
        status = await restored_tools["get_interpretation_status"].call(task_id=task_id)
        assert status.state == "success"
        modified = await restored_tools["modify_well_interpretation"].call(
            task_id=task_id, por=0.17
        )
        assert modified.state == "success"
        await restored_runner.wait_for_completion(
            task_id, modified.metadata["result"]["execution_id"]
        )
        await restored_factory.shutdown()

        monkeypatch.setenv("CNLC_MODEL_PROVIDER", "mock")
        monkeypatch.setenv("CNLC_PERSISTENCE", "postgres-redis")
        monkeypatch.setenv("CNLC_REDIS_PREFIX", prefix)
        monkeypatch.setenv("DATABASE_URL", DB_URL)
        monkeypatch.setenv("REDIS_URL", REDIS_URL)
        app = create_demo_app(connections=connections, workspace_dir=tmp_path / "workspaces")
        assert app.state.cnlc_task_tools.runners == {}
        path = (
            "/cnlc/interpretation/agents/agent-a/sessions/session-a/tasks/"
            f"{task_id}"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(path, headers={"X-User-ID": "alice"})
            assert response.status_code == 200
            assert response.json()["current_execution"]["sequence"] == 2
            for denied_path, user_id in [
                (path, "bob"),
                (path.replace("agent-a", "agent-b"), "alice"),
                (path.replace("session-a", "session-b"), "alice"),
            ]:
                denied = await client.get(denied_path, headers={"X-User-ID": user_id})
                assert denied.status_code == 404
        await app.state.cnlc_task_tools.shutdown()
    finally:
        await first_factory.shutdown()
        if task_id:
            async with engine.begin() as connection:
                await connection.execute(delete(TaskRow).where(TaskRow.task_id == task_id))
        keys = await redis.keys(f"{prefix}*")
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
        await engine.dispose()


async def test_multiple_wells_resolve_after_real_backend_restart(
    migrated_session_binding, fixture_data
):
    """真实 PostgreSQL Binding 在 Factory 重建后仍恢复当前井和上一口井。"""

    del migrated_session_binding
    prefix = f"cnlc:test:multi-well:{uuid4().hex}"
    session_id = f"session-{uuid4().hex}"
    identity = ("alice", "agent-a", session_id)
    settings = AppSettings(
        mode="demo", model_provider="mock", mock_data_dir=ROOT / "mock_data", _env_file=None
    )
    persistence = PersistenceSettings(
        persistence="postgres-redis", redis_prefix=prefix, _env_file=None
    )
    connections = ConnectionSettings(
        database_url=DB_URL, redis_url=REDIS_URL, _env_file=None
    )
    task_ids: list[str] = []
    first_factory = SessionTaskToolFactory(
        settings=settings, persistence=persistence, connections=connections
    )
    try:
        tools = {tool.name: tool for tool in await first_factory(*identity)}
        started_a = await tools["run_well_interpretation"].call(well_id="WELL_MOCK_001")
        task_a = started_a.metadata["result"]
        task_ids.append(task_a["task_id"])
        runner = tools["get_interpretation_status"].runner
        await runner.wait_for_completion(task_a["task_id"], task_a["execution_id"])

        fixture_b_data = json.loads(json.dumps(fixture_data))
        fixture_b_data["well"]["well_id"] = "WELL_MOCK_002"
        fixture_b_data["well"]["name"] = "虚构演示井 002"
        start_tool = tools["run_well_interpretation"]
        start_tool.upload = MockFixture.model_validate(fixture_b_data), "解释第二口井"
        started_b = await start_tool.call(well_id="WELL_MOCK_002")
        start_tool.upload = None
        task_b = started_b.metadata["result"]
        task_ids.append(task_b["task_id"])
        await runner.wait_for_completion(task_b["task_id"], task_b["execution_id"])
        await first_factory.shutdown()

        restored_factory = SessionTaskToolFactory(
            settings=settings, persistence=persistence, connections=connections
        )
        restored = {tool.name: tool for tool in await restored_factory(*identity)}
        restored_runner = restored["get_interpretation_status"].runner
        assert restored_runner.active_task_id is None
        current = await restored["get_interpretation_status"].call(
            task_reference={"kind": "CURRENT"}
        )
        assert current.metadata["result"]["task_id"] == task_b["task_id"]
        previous = await restored["get_interpretation_report"].call(
            task_reference={"kind": "PREVIOUS_TASK"},
            selector="LATEST_SUCCESSFUL",
        )
        assert previous.metadata["result"]["task_id"] == task_a["task_id"]
        await restored_factory.shutdown()
    finally:
        await first_factory.shutdown()
        engine = create_async_engine(DB_URL)
        redis = Redis.from_url(REDIS_URL, decode_responses=True)
        try:
            if task_ids:
                async with engine.begin() as connection:
                    await connection.execute(
                        delete(TaskRow).where(TaskRow.task_id.in_(task_ids))
                    )
            keys = [key async for key in redis.scan_iter(f"{prefix}*")]
            if keys:
                await redis.delete(*keys)
        finally:
            await redis.aclose()
            await engine.dispose()
