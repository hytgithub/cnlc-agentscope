"""Opt-in real servers. Use dedicated test services; never substitutes SQLite/fake Redis."""

import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.dataset_revision_service import DatasetRevisionService
from cnlc_agent.application.execution_dispatcher import InProcessExecutionDispatcher
from cnlc_agent.application.runtime import application_runtime
from cnlc_agent.application.stage_orchestrator import StageOrchestrator
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.domain.dataset_revision import (
    DatasetChangeSet,
    DatasetCurveChange,
    DatasetCurvePatchRequest,
    DatasetPatchRequest,
    DatasetPatchSampleRequest,
    DatasetRevision,
    DatasetSampleChange,
    change_set_digest,
    child_lineage,
)
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.execution import ExecutionRunMode, ExecutionStatus
from cnlc_agent.domain.models import MockFixture, TaskRequest, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.stages import STAGE_ORDER, StageRunStatus
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.domain.tool_run import ToolExecutionMode, ToolRun, ToolRunStatus
from cnlc_agent.infrastructure.database import (
    DatasetRevisionRow,
    ExecutionRow,
    InputVersionRow,
    PostgreSQLTaskRepository,
    TaskRow,
    ToolRunRow,
)
from cnlc_agent.infrastructure.redis_store import RedisInterpretationStateStore

ROOT = Path(__file__).resolve().parents[2]
DB_URL = os.environ.get("CNLC_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("CNLC_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not (DB_URL and REDIS_URL),
    reason="Set CNLC_TEST_DATABASE_URL and CNLC_TEST_REDIS_URL for real services",
)


async def finish_execution(repository, state, status):
    """真实数据库测试按 claim/finish 状态机结束执行。"""

    worker = f"test-{uuid4().hex}"
    assert await repository.claim_execution(
        state.workflow_execution_id, worker, utc_now() + timedelta(minutes=1)
    )
    await repository.finish_execution(state.workflow_execution_id, worker, status)


async def test_real_tool_run_and_report_survive_new_repository(migrated):
    """真实 PostgreSQL 上验证 ToolRun 外键、终态、历史报告和新会话读回。"""

    request = TaskRequest(well_id="WELL_MOCK_001")
    state = InterpretationState(task=request)
    engine = create_async_engine(DB_URL)
    new_engine = create_async_engine(DB_URL)
    try:
        repository = PostgreSQLTaskRepository(engine)
        await repository.create(state)
        record = await repository.create_tool_run(ToolRun(
            task_id=request.task_id, execution_id=state.workflow_execution_id,
            step_id="W01", tool_code="get_well_data", execution_mode=ToolExecutionMode.MOCK,
            source="mock:fixture", input_snapshot={"well_id": request.well_id},
        ))
        await repository.finish_tool_run(
            record.tool_run_id, status=ToolRunStatus.SUCCESS, source="fixture:test",
            output_snapshot={"status": "SUCCESS", "data_keys": ["well", "raw_data"]},
        )
        state.status = StepStatus.SUCCESS
        await repository.save(state, "first report")
        await finish_execution(repository, state, ExecutionStatus.SUCCESS)
        second = InterpretationState(task=request)
        await repository.create_execution(second)
        second.status = StepStatus.SUCCESS
        await repository.save(second, "second report")
        await finish_execution(repository, second, ExecutionStatus.SUCCESS)
        reopened = PostgreSQLTaskRepository(new_engine)
        runs = await reopened.list_tool_runs(state.workflow_execution_id)
        assert len(runs) == 1
        assert runs[0].tool_run_id == record.tool_run_id
        assert runs[0].status == ToolRunStatus.SUCCESS
        assert await reopened.get_tool_run(record.tool_run_id) == runs[0]
        assert await reopened.list_tool_runs(second.workflow_execution_id) == []
        assert await reopened.get_execution_report(state.workflow_execution_id) == "first report"
        assert await reopened.get_report(request.task_id) == "second report"
        assert {fk.column.table.name for fk in ToolRunRow.__table__.foreign_keys} == {
            "interpretation_task", "interpretation_execution"
        }
        with pytest.raises(InfrastructureError) as caught:
            await reopened.create_tool_run(ToolRun(
                task_id="other-task", execution_id=state.workflow_execution_id,
                step_id="W01", tool_code="bad", execution_mode=ToolExecutionMode.MOCK,
                source="mock:test",
            ))
        assert caught.value.code == "TOOL_RUN_EXECUTION_MISMATCH"
    finally:
        async with engine.begin() as connection:
            await connection.execute(delete(TaskRow).where(TaskRow.task_id == request.task_id))
        await engine.dispose()
        await new_engine.dispose()


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


async def test_staged_confirmation_survives_postgresql_restart(migrated):
    """WAITING 快照、确认和同一 Execution 续跑只依赖 PostgreSQL 事实。"""

    request = TaskRequest(well_id="WELL_MOCK_001")
    first_engine = create_async_engine(DB_URL)
    second_engine = create_async_engine(DB_URL)
    try:
        first_repository = PostgreSQLTaskRepository(first_engine)
        first_app = build_application(
            AppSettings(mock_data_dir=ROOT / "mock_data", _env_file=None),
            task_repository=first_repository,
        )
        state = InterpretationState(task=request)
        await first_repository.create_task(state)
        execution = await first_repository.create_execution(
            state, "INITIAL", run_mode=ExecutionRunMode.STAGED_CONFIRMATION
        )
        first_orchestrator = StageOrchestrator(first_app)
        waiting = await first_orchestrator.execute_next_stage(
            execution.execution_id, worker_id="postgres-stage-1"
        )
        assert waiting.status == ExecutionStatus.WAITING_CONFIRMATION
        stage_run = waiting.state_snapshot.stage_runs[-1]
        async with first_engine.connect() as connection:
            task_snapshot = await connection.scalar(
                select(TaskRow.snapshot).where(TaskRow.task_id == request.task_id)
            )
        assert InterpretationState.model_validate(task_snapshot) == waiting.state_snapshot

        # 新连接和新应用对象模拟进程重启；不读取旧对象中的任何活动上下文。
        second_repository = PostgreSQLTaskRepository(second_engine)
        second_app = build_application(
            AppSettings(mock_data_dir=ROOT / "mock_data", _env_file=None),
            task_repository=second_repository,
        )
        second_orchestrator = StageOrchestrator(second_app)
        confirmations = await asyncio.gather(
            *[
                second_orchestrator.confirm_stage(
                    request.task_id,
                    execution.execution_id,
                    STAGE_ORDER[0],
                    stage_run.id,
                    actor="postgres-test",
                )
                for _ in range(2)
            ],
            return_exceptions=True,
        )
        assert sum(not isinstance(item, Exception) for item in confirmations) == 1
        queued = next(item for item in confirmations if not isinstance(item, Exception))
        conflict = next(item for item in confirmations if isinstance(item, InfrastructureError))
        assert conflict.code == "STAGE_CONFIRMATION_CONFLICT"
        assert queued.status == ExecutionStatus.QUEUED
        assert queued.run_mode == ExecutionRunMode.STAGED_CONFIRMATION
        assert queued.execution_id == execution.execution_id
        # 模拟确认事务已经提交、原 dispatcher 尚未 submit 就崩溃。新进程只按
        # 持久化 execution_id 重新投递，execute_prepared 根据 run_mode 恢复下一阶段。
        dispatcher = InProcessExecutionDispatcher()

        async def resume(worker_id: str) -> None:
            await second_app.execute_prepared(execution.execution_id, worker_id=worker_id)

        try:
            await dispatcher.submit(execution.execution_id, resume)
            await dispatcher.submit(execution.execution_id, resume)
            await dispatcher.wait(execution.execution_id)
        finally:
            await dispatcher.shutdown()
        second_waiting = await second_repository.get_execution(execution.execution_id)
        assert second_waiting is not None
        assert second_waiting.status == ExecutionStatus.WAITING_CONFIRMATION
        assert second_waiting.state_snapshot.stage_runs[-1].stage == STAGE_ORDER[1]
        assert second_waiting.started_at == waiting.started_at
    finally:
        async with second_engine.begin() as connection:
            await connection.execute(delete(TaskRow).where(TaskRow.task_id == request.task_id))
        await first_engine.dispose()
        await second_engine.dispose()


async def test_postgresql_waiting_execution_can_be_superseded_and_old_confirm_is_rejected(
    migrated,
):
    """WAITING_CONFIRMATION 不阻止 V2；Task 指针移动后 V1 只能读取。"""

    request = TaskRequest(well_id="WELL_MOCK_001")
    engine = create_async_engine(DB_URL)
    repository = PostgreSQLTaskRepository(engine)
    app = build_application(
        AppSettings(mock_data_dir=ROOT / "mock_data", _env_file=None),
        task_repository=repository,
    )
    orchestrator = StageOrchestrator(app)
    try:
        first_state = InterpretationState(task=request)
        await repository.create_task(first_state)
        first = await repository.create_execution(
            first_state, "INITIAL", run_mode=ExecutionRunMode.STAGED_CONFIRMATION
        )
        waiting = await orchestrator.execute_next_stage(
            first.execution_id, worker_id="postgres-v1"
        )
        first_run = waiting.state_snapshot.stage_runs[-1]

        second = await repository.create_execution(
            InterpretationState(task=request),
            "RERUN",
            expected_current_execution_id=first.execution_id,
            run_mode=ExecutionRunMode.STAGED_CONFIRMATION,
        )
        task = await repository.get_task(request.task_id)
        assert task is not None and task.current_execution_id == second.execution_id

        with pytest.raises(InfrastructureError) as caught:
            await orchestrator.confirm_stage(
                request.task_id,
                first.execution_id,
                STAGE_ORDER[0],
                first_run.id,
                actor="postgres-test",
            )
        assert caught.value.code == "EXECUTION_NOT_CURRENT"
        unchanged = await repository.get_execution(first.execution_id)
        assert unchanged == waiting
    finally:
        async with engine.begin() as connection:
            await connection.execute(delete(TaskRow).where(TaskRow.task_id == request.task_id))
        await engine.dispose()


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
            assert [run.stage for run in state.stage_runs] == list(STAGE_ORDER)
            assert all(run.status == StageRunStatus.CONFIRMED for run in state.stage_runs)
            # Redis 是可丢弃运行检查点，可能停在 Workflow 最后一步；报告后的
            # current_step 清理与最终 updated_at 以 PostgreSQL Execution 为准。
            cached = await store.get(request.task_id)
            assert cached is not None
            assert cached.task == state.task
            assert cached.workflow_execution_id == state.workflow_execution_id
            assert cached.completed_steps == state.completed_steps
            assert cached.status == state.status
            assert cached.stage_runs == state.stage_runs[:3]
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
        await finish_execution(repository, first, ExecutionStatus.SUCCESS)
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
        await finish_execution(repository, second, ExecutionStatus.FAILED)
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


async def test_postgresql_claim_is_atomic_and_expired_lease_is_failed(migrated):
    """两个独立仓库并发 claim 只能有一个成功；过期不触发业务重跑。"""

    request = TaskRequest(well_id="WELL_1")
    state = InterpretationState(task=request)
    first_engine = create_async_engine(DB_URL)
    second_engine = create_async_engine(DB_URL)
    first = PostgreSQLTaskRepository(first_engine)
    second = PostgreSQLTaskRepository(second_engine)
    try:
        await first.create(state)
        expiry = utc_now() + timedelta(minutes=1)
        claims = await asyncio.gather(
            first.claim_execution(state.workflow_execution_id, "worker-a", expiry),
            second.claim_execution(state.workflow_execution_id, "worker-b", expiry),
        )
        assert sorted(claims) == [False, True]
        claimed = await first.get_execution(state.workflow_execution_id)
        assert claimed.status == ExecutionStatus.RUNNING
        assert claimed.lease_owner in {"worker-a", "worker-b"}
        other = "worker-b" if claimed.lease_owner == "worker-a" else "worker-a"
        assert not await second.renew_execution_lease(
            state.workflow_execution_id, other, utc_now() + timedelta(minutes=2)
        )
        assert await first.renew_execution_lease(
            state.workflow_execution_id, claimed.lease_owner,
            utc_now() + timedelta(seconds=1),
        )
        expired = await second.recover_expired_executions(utc_now() + timedelta(seconds=2))
        assert state.workflow_execution_id in expired
        failed = await first.get_execution(state.workflow_execution_id)
        assert failed.status == ExecutionStatus.FAILED
        assert failed.error_code == "WORKER_LEASE_EXPIRED"
        assert not await first.claim_execution(
            state.workflow_execution_id, "worker-c", utc_now() + timedelta(minutes=1)
        )
    finally:
        async with first_engine.begin() as connection:
            await connection.execute(delete(TaskRow).where(TaskRow.task_id == request.task_id))
        await first_engine.dispose()
        await second_engine.dispose()


async def test_postgresql_stale_plan_rejected_under_task_lock(migrated):
    """两份基于同一当前版本的计划只能创建一个新 Execution。"""

    request = TaskRequest(well_id="WELL_1")
    first_state = InterpretationState(task=request)
    engine = create_async_engine(DB_URL)
    repository = PostgreSQLTaskRepository(engine)
    try:
        await repository.create(first_state)
        first_state.status = StepStatus.SUCCESS
        await repository.save(first_state, "first report")
        await finish_execution(repository, first_state, ExecutionStatus.SUCCESS)
        expected = first_state.workflow_execution_id
        second = await repository.create_execution(
            InterpretationState(task=request), expected_current_execution_id=expected
        )
        assert second.status == ExecutionStatus.QUEUED
        with pytest.raises(InfrastructureError) as caught:
            await repository.create_execution(
                InterpretationState(task=request), expected_current_execution_id=expected
            )
        assert caught.value.code == "STALE_EXECUTION_PLAN"
        assert len(await repository.list_executions(request.task_id)) == 2
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
        await finish_execution(repository, first, ExecutionStatus.SUCCESS)
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
        await finish_execution(repository, second, ExecutionStatus.FAILED)

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


async def test_postgresql_dataset_revision_survives_repository_restart(migrated):
    """真实 PostgreSQL 原子保存稀疏 ChangeSet，并能跨连接重新物化。"""

    fixture = MockFixture.model_validate_json((ROOT / "mock_data/WELL_MOCK_001.json").read_text())
    request = TaskRequest(well_id=fixture.well.well_id)
    engine = create_async_engine(DB_URL)
    reopened_engine = create_async_engine(DB_URL)
    try:
        repository = PostgreSQLTaskRepository(engine)
        await repository.create_task(InterpretationState(task=request))
        input_version = await repository.create_input_version(request.task_id, fixture)
        service = DatasetRevisionService(repository)
        root = await service.create_root_revision(
            request.task_id, input_version.input_version_id
        )
        child, change_set = await service.apply_patch(
            request.task_id,
            root.dataset_revision_id,
            DatasetPatchRequest(
                curves=[
                    DatasetCurvePatchRequest(
                        curve_code="GR",
                        unit="API",
                        samples=[
                            DatasetPatchSampleRequest(depth_m=2000.0, new_value=82.0)
                        ],
                    )
                ]
            ),
            actor="integration-test",
        )
        await engine.dispose()

        reopened = PostgreSQLTaskRepository(reopened_engine)
        restored = await DatasetRevisionService(reopened).materialize(
            request.task_id, child.dataset_revision_id
        )
        assert restored.curves["GR"].values == [82.0, 48.0, 46.0]
        assert (await reopened.get_dataset_revision(root.dataset_revision_id)) == root
        assert (await reopened.get_dataset_change_set(change_set.change_set_id)) == change_set
        assert len((await reopened.list_dataset_change_sets(request.task_id))[0].curve_changes) == 1
        assert len(
            (await reopened.list_dataset_change_sets(request.task_id))[0]
            .curve_changes[0]
            .samples
        ) == 1

        concurrent = await asyncio.gather(
            *(
                DatasetRevisionService(reopened).apply_patch(
                    request.task_id,
                    root.dataset_revision_id,
                    DatasetPatchRequest(
                        curves=[
                            DatasetCurvePatchRequest(
                                curve_code="GR",
                                unit="API",
                                samples=[
                                    DatasetPatchSampleRequest(
                                        depth_m=2000.5, new_value=value
                                    )
                                ],
                            )
                        ]
                    ),
                    actor="concurrency-test",
                )
                for value in (81.0, 82.0, 83.0)
            )
        )
        assert sorted(item[0].sequence for item in concurrent) == [3, 4, 5]

        # 数据库唯一约束独立保证同一 ChangeSet 不能被第二个 Child 引用。
        with pytest.raises(IntegrityError):
            async with reopened_engine.begin() as connection:
                await connection.execute(
                    insert(DatasetRevisionRow).values(
                        dataset_revision_id=str(uuid4()),
                        task_id=request.task_id,
                        well_id=request.well_id,
                        sequence=6,
                        root_input_version_id=root.root_input_version_id,
                        parent_revision_id=root.dataset_revision_id,
                        change_set_id=change_set.change_set_id,
                        lineage_sha256=child.lineage_sha256,
                        created_from_execution_id=None,
                        created_at=utc_now(),
                    )
                )

        # ChangeSet flush 后让 Revision 主键冲突，整个事务必须回滚。
        curve_change = DatasetCurveChange(
            curve_code="GR",
            unit="API",
            samples=[
                DatasetSampleChange(
                    sample_index=1,
                    depth_m=2000.5,
                    before_value=48.0,
                    after_value=90.0,
                )
            ],
        )
        digest = change_set_digest([curve_change])
        orphan_candidate = DatasetChangeSet(
            task_id=request.task_id,
            well_id=request.well_id,
            base_revision_id=child.dataset_revision_id,
            curve_changes=[curve_change],
            content_sha256=digest,
            created_by="integration-test",
        )
        conflicting_revision = DatasetRevision(
            dataset_revision_id=root.dataset_revision_id,
            task_id=request.task_id,
            well_id=request.well_id,
            sequence=3,
            root_input_version_id=root.root_input_version_id,
            parent_revision_id=child.dataset_revision_id,
            change_set_id=orphan_candidate.change_set_id,
            lineage_sha256=child_lineage(child.lineage_sha256, digest),
        )
        with pytest.raises(InfrastructureError) as caught:
            await reopened.create_revision_with_change_set(
                conflicting_revision, orphan_candidate
            )
        assert caught.value.code == "DATASET_REVISION_CONFLICT"
        assert await reopened.get_dataset_change_set(orphan_candidate.change_set_id) is None
    finally:
        async with reopened_engine.begin() as connection:
            await connection.execute(delete(TaskRow).where(TaskRow.task_id == request.task_id))
        await engine.dispose()
        await reopened_engine.dispose()
