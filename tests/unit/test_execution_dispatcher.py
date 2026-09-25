"""后台调度器的并发、续租和关闭行为。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from cnlc_agent.application.commands import GetStatusCommand
from cnlc_agent.application.execution_dispatcher import InProcessExecutionDispatcher
from cnlc_agent.application.service import InterpretationTaskService
from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.agentscope_app import SessionTaskToolFactory
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.execution import ExecutionStatus
from cnlc_agent.infrastructure.mock import MockModelGateway


async def test_dispatcher_deduplicates_and_reaps_tasks():
    dispatcher = InProcessExecutionDispatcher()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def execute(worker_id):
        calls.append(worker_id)
        entered.set()
        await release.wait()

    await dispatcher.submit("execution-1", execute)
    await dispatcher.submit("execution-1", execute)
    await asyncio.wait_for(entered.wait(), 5)
    assert len(calls) == 1
    release.set()
    await dispatcher.wait("execution-1")
    await asyncio.sleep(0)
    assert dispatcher._tasks == {}
    await dispatcher.shutdown()


async def test_different_tasks_can_run_and_renew_lease(data_dir, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0
    original = MockModelGateway.generate

    async def paused_fluid(self, request):
        nonlocal count
        if request.purpose == "fluid":
            count += 1
            if count == 2:
                entered.set()
            await release.wait()
        return await original(self, request)

    monkeypatch.setattr(MockModelGateway, "generate", paused_fluid)
    session = TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(
            persistence="memory", execution_lease_seconds=3.3, _env_file=None
        ),
    )
    first = await session.run("WELL_MOCK_001")
    second = await session.run("WELL_MOCK_001")
    assert first.task_id != second.task_id
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for submitted in (first, second):
            status = await session.execute(GetStatusCommand(task_id=submitted.task_id))
            assert status.execution_status == ExecutionStatus.RUNNING
            assert status.current_step == StepId.W06
        before = await session.repository.get_execution(first.execution_id)
        assert before.lease_expires_at is not None
        await asyncio.sleep(1.25)
        after = await session.repository.get_execution(first.execution_id)
        assert after.lease_expires_at > before.lease_expires_at
        assert await session.repository.recover_expired_executions(after.updated_at) == []
    finally:
        release.set()
        await session.wait_for_completion(first.task_id, first.execution_id)
        await session.wait_for_completion(second.task_id, second.execution_id)
        await session.dispatcher.shutdown()


async def test_shutdown_cancels_running_worker_and_marks_failed(data_dir, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = MockModelGateway.generate

    async def paused_fluid(self, request):
        if request.purpose == "fluid":
            entered.set()
            await release.wait()
        return await original(self, request)

    monkeypatch.setattr(MockModelGateway, "generate", paused_fluid)
    session = TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
    )
    submitted = await session.run("WELL_MOCK_001")
    await asyncio.wait_for(entered.wait(), 5)
    await session.dispatcher.shutdown()
    execution = await session.repository.get_execution(submitted.execution_id)
    assert execution.status == ExecutionStatus.FAILED
    assert execution.error_code == "BACKGROUND_EXECUTION_CANCELLED"
    assert session.dispatcher._tasks == {}


async def test_unhandled_worker_exception_marks_failed(data_dir, monkeypatch):
    """Worker 异常不能让 Execution 永久留在 RUNNING。"""

    async def broken(*_args, **_kwargs):
        raise RuntimeError("private-error-text")

    monkeypatch.setattr(InterpretationTaskService, "_run_execution", broken)
    session = TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
    )
    submitted = await session.run("WELL_MOCK_001")
    try:
        with pytest.raises(RuntimeError):
            await session.dispatcher.wait(submitted.execution_id)
        execution = await session.repository.get_execution(submitted.execution_id)
        assert execution.status == ExecutionStatus.FAILED
        assert execution.error_code == "BACKGROUND_EXECUTION_FAILED"
        assert "private-error-text" not in execution.model_dump_json()
    finally:
        await session.dispatcher.shutdown()


async def test_postgres_factory_polls_recovery_and_closes_context(monkeypatch):
    """应用级 Worker 定期查过期租约，并在关闭时回收持有的连接。"""

    calls = []
    closed = asyncio.Event()
    repository = object()

    @asynccontextmanager
    async def runtime(*_args):
        try:
            yield SimpleNamespace(repository=repository)
        finally:
            closed.set()

    async def recover(self, observed):
        assert observed is repository
        calls.append(1)
        return 0

    monkeypatch.setattr("cnlc_agent.demo.agentscope_app.application_runtime", runtime)
    monkeypatch.setattr(InProcessExecutionDispatcher, "recover", recover)
    factory = SessionTaskToolFactory(
        persistence=PersistenceSettings(
            persistence="postgres-redis", execution_poll_seconds=0.01, _env_file=None
        )
    )
    await factory.start()
    await asyncio.sleep(0.035)
    await factory.shutdown()
    assert len(calls) >= 2
    assert closed.is_set()
    assert factory._recovery_task is None
