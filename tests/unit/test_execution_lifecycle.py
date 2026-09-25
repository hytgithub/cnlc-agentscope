"""后台 Execution 生命周期、仓库事实和任务级并发边界。"""

import asyncio
from datetime import timedelta

import pytest

from cnlc_agent.application.commands import (
    FullRerunCommand,
    GetReportCommand,
    GetStatusCommand,
    ModifyInterpretationCommand,
)
from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import DataError, InfrastructureError
from cnlc_agent.domain.execution import ExecutionStatus
from cnlc_agent.domain.models import TaskRequest, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository, MockModelGateway


def runner(data_dir, *, lease_seconds=90):
    """每个测试使用独立会话仓库和正式后台调度器。"""

    return TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(
            persistence="memory", execution_lease_seconds=lease_seconds, _env_file=None
        ),
    )


async def test_execution_status_is_separate_from_step_status():
    repository = InMemoryTaskRepository()
    state = InterpretationState(task=TaskRequest(well_id="WELL_1"))
    await repository.create(state)
    queued = await repository.get_execution(state.workflow_execution_id)
    assert queued.status == ExecutionStatus.QUEUED
    assert queued.state_snapshot.status == StepStatus.PENDING
    assert queued.started_at is None
    lease_until = utc_now() + timedelta(minutes=1)
    assert await repository.claim_execution(queued.execution_id, "worker-a", lease_until)
    assert not await repository.claim_execution(queued.execution_id, "worker-b", lease_until)
    running = await repository.get_execution(queued.execution_id)
    assert running.status == ExecutionStatus.RUNNING
    assert running.state_snapshot.status == StepStatus.PENDING
    with pytest.raises(InfrastructureError) as caught:
        await repository.finish_execution(queued.execution_id, "worker-b", ExecutionStatus.SUCCESS)
    assert caught.value.code == "EXECUTION_LEASE_MISMATCH"
    finished = await repository.finish_execution(
        queued.execution_id, "worker-a", ExecutionStatus.FAILED,
        error_code="BACKGROUND_EXECUTION_FAILED",
    )
    assert finished.status == ExecutionStatus.FAILED
    assert finished.finished_at is not None and finished.lease_owner is None
    with pytest.raises(InfrastructureError):
        await repository.finish_execution(queued.execution_id, "worker-a", ExecutionStatus.SUCCESS)


async def test_stale_plan_is_rejected_atomically(data_dir):
    session = runner(data_dir)
    initial = await session.run("WELL_MOCK_001")
    await session.wait_for_completion(initial.task_id, initial.execution_id)
    try:
        async with session.context(data_dir) as service:
            request = TaskRequest(task_id=initial.task_id, well_id=initial.well_id)
            plan_a = await service.plan_rerun(
                request, changes=InterpretationOverride(por=0.16)
            )
            plan_b = await service.plan_rerun(
                request, changes=InterpretationOverride(perm=0.16)
            )
            created = await service.prepare_rerun_plan(request, plan_b)
            assert created.status == ExecutionStatus.QUEUED
            with pytest.raises(InfrastructureError) as caught:
                await service.prepare_rerun_plan(request, plan_a)
            assert caught.value.code == "STALE_EXECUTION_PLAN"
            assert len(await service.repository.list_executions(initial.task_id)) == 2
    finally:
        await session.dispatcher.shutdown()


async def test_running_status_reports_checkpoint_and_rejects_same_task(data_dir, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original = MockModelGateway.generate

    async def paused_fluid(self, request):
        if request.purpose == "fluid":
            entered.set()
            await release.wait()
        return await original(self, request)

    monkeypatch.setattr(MockModelGateway, "generate", paused_fluid)
    session = runner(data_dir)
    initial = await session.run("WELL_MOCK_001")
    assert initial.execution_status == ExecutionStatus.QUEUED
    assert initial.current_step is None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        status = await session.execute(GetStatusCommand(task_id=initial.task_id))
        assert status.execution_status == ExecutionStatus.RUNNING
        assert status.workflow_status == StepStatus.RUNNING
        assert status.current_step == StepId.W06
        assert status.completed_steps == list(StepId)[:5]
        assert status.tool_run_summary["count"] >= 4
        assert not status.report_ready
        with pytest.raises(DataError) as caught:
            await session.execute(GetReportCommand(task_id=initial.task_id))
        assert caught.value.code == "REPORT_NOT_READY"
        with pytest.raises(InfrastructureError) as caught:
            await session.execute(ModifyInterpretationCommand(
                task_id=initial.task_id, changes=InterpretationOverride(por=0.16)
            ))
        assert caught.value.code == "TASK_EXECUTION_ACTIVE"
        with pytest.raises(InfrastructureError) as caught:
            await session.execute(FullRerunCommand(task_id=initial.task_id))
        assert caught.value.code == "TASK_EXECUTION_ACTIVE"
        assert len(await session.repository.list_executions(initial.task_id)) == 1
    finally:
        release.set()
        completed = await session.wait_for_completion(initial.task_id, initial.execution_id)
        await session.dispatcher.shutdown()
    assert completed.execution_status == ExecutionStatus.SUCCESS
    assert completed.current_step is None
    assert completed.completed_steps == list(StepId)
    assert completed.report_ready
    runs = await session.repository.list_tool_runs(initial.execution_id)
    assert len(runs) == 6
    assert all(item.execution_id == initial.execution_id for item in runs)


async def test_modify_full_rerun_and_previous_report_are_versioned(data_dir):
    session = runner(data_dir)
    try:
        first = await session.run("WELL_MOCK_001")
        await session.wait_for_completion(first.task_id, first.execution_id)
        first_report = await session.repository.get_execution_report(first.execution_id)
        second = await session.execute(ModifyInterpretationCommand(
            task_id=first.task_id, changes=InterpretationOverride(por=0.16, perm=0.16)
        ))
        assert second.execution_status == ExecutionStatus.QUEUED
        assert second.reused_steps == [StepId.W01, StepId.W02, StepId.W03]
        with pytest.raises(DataError) as caught:
            await session.execute(GetReportCommand(task_id=first.task_id, selector="CURRENT"))
        assert caught.value.code == "REPORT_NOT_READY"
        previous = await session.execute(GetReportCommand(
            task_id=first.task_id, selector="PREVIOUS"
        ))
        assert previous.report_markdown == first_report
        second_done = await session.wait_for_completion(first.task_id, second.execution_id)
        assert second_done.completed_steps == list(StepId)
        full = await session.execute(FullRerunCommand(task_id=first.task_id))
        assert full.execution_status == ExecutionStatus.QUEUED
        assert full.reused_steps == []
        assert full.effective_override == second_done.effective_override
        final = await session.wait_for_completion(first.task_id, full.execution_id)
        assert final.execution_status == ExecutionStatus.SUCCESS
        assert final.tool_run_summary["count"] == 6
    finally:
        await session.dispatcher.shutdown()


async def test_expired_lease_fails_without_replaying_workflow():
    repository = InMemoryTaskRepository()
    state = InterpretationState(task=TaskRequest(well_id="WELL_1"))
    await repository.create(state)
    assert await repository.claim_execution(
        state.workflow_execution_id, "worker-a", utc_now() - timedelta(seconds=1)
    )
    expired = await repository.recover_expired_executions(utc_now())
    assert expired == [state.workflow_execution_id]
    result = await repository.get_execution(state.workflow_execution_id)
    assert result.status == ExecutionStatus.FAILED
    assert result.error_code == "WORKER_LEASE_EXPIRED"
    assert not await repository.claim_execution(
        state.workflow_execution_id, "worker-b", utc_now() + timedelta(minutes=1)
    )
