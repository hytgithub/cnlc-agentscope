"""分阶段确认应用编排的核心生命周期与并发边界。"""

import asyncio

import pytest

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.planning import ExecutionPlan, ExecutionStage, PlanAction
from cnlc_agent.application.stage_orchestrator import StageOrchestrator
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.errors import DataError, InfrastructureError
from cnlc_agent.domain.execution import ExecutionRunMode, ExecutionStatus
from cnlc_agent.domain.models import MockFixture, TaskRequest, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.stage_runtime import begin_stage
from cnlc_agent.domain.stages import STAGE_ORDER, InterpretationStage, StageRunStatus
from cnlc_agent.domain.state import InterpretationState


async def staged_app(data_dir):
    """创建不带上传 InputVersion 的分阶段任务，W01 仍走正式 Fixture Tool。"""

    app = build_application(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None)
    )
    request = TaskRequest(well_id="WELL_MOCK_001")
    state = InterpretationState(task=request, mode="demo")
    await app.repository.create_task(state)
    execution = await app.repository.create_execution(
        state, "INITIAL", run_mode=ExecutionRunMode.STAGED_CONFIRMATION
    )
    return app, request, execution, StageOrchestrator(app)


async def test_four_stages_pause_confirm_and_finish_same_execution(data_dir):
    app, request, initial, orchestrator = await staged_app(data_dir)
    execution_id = initial.execution_id
    first_started_at = None

    for index, stage in enumerate(STAGE_ORDER):
        waiting = await orchestrator.execute_next_stage(
            execution_id, worker_id=f"worker-{index}"
        )
        assert waiting.execution_id == execution_id
        assert waiting.sequence == initial.sequence
        assert waiting.status == ExecutionStatus.WAITING_CONFIRMATION
        assert waiting.finished_at is None
        assert waiting.lease_owner is None
        assert waiting.lease_expires_at is None
        first_started_at = first_started_at or waiting.started_at
        assert waiting.started_at == first_started_at
        current_run = [
            run for run in waiting.state_snapshot.stage_runs
            if run.execution_id == execution_id
        ][-1]
        assert current_run.stage == stage
        assert current_run.status == StageRunStatus.WAITING_CONFIRM
        assert len(waiting.state_snapshot.executions) == (1, 3, 10, 10)[index]
        assert len({item.step_id for item in waiting.state_snapshot.executions}) == len(
            waiting.state_snapshot.executions
        )

        progress = await orchestrator.get_progress(request.task_id, execution_id)
        assert progress.waiting_confirmation_stage == stage
        assert progress.current_stage_run_id == current_run.id
        if stage == InterpretationStage.REPORT:
            assert progress.candidate_report
            with pytest.raises(DataError, match="尚未完成"):
                await app.get_execution_report(request.task_id, execution_id)

        confirmed = await orchestrator.confirm_stage(
            request.task_id,
            execution_id,
            stage,
            current_run.id,
            actor="tester",
        )
        if stage == InterpretationStage.REPORT:
            assert confirmed.status == ExecutionStatus.SUCCESS
            assert confirmed.finished_at is not None
        else:
            assert confirmed.status == ExecutionStatus.QUEUED
            assert confirmed.finished_at is None
            assert confirmed.started_at == first_started_at

    final = await app.repository.get_execution(execution_id)
    assert final is not None
    assert [
        run.stage for run in final.state_snapshot.stage_runs
        if run.execution_id == execution_id
    ] == list(STAGE_ORDER)
    assert all(
        run.status == StageRunStatus.CONFIRMED
        for run in final.state_snapshot.stage_runs
        if run.execution_id == execution_id
    )
    assert await app.get_execution_report(request.task_id, execution_id)
    task = await app.repository.get_task(request.task_id)
    assert task is not None and task.latest_successful_execution_id == execution_id


async def test_wrong_or_double_confirmation_cannot_confirm_next_stage(data_dir):
    _, request, initial, orchestrator = await staged_app(data_dir)
    waiting = await orchestrator.execute_next_stage(initial.execution_id, worker_id="worker")
    run = waiting.state_snapshot.stage_runs[-1]
    with pytest.raises(InfrastructureError) as wrong:
        await orchestrator.confirm_stage(
            request.task_id,
            initial.execution_id,
            InterpretationStage.DECODE,
            "stale-button-id",
            actor="tester",
        )
    assert wrong.value.code == "STAGE_CONFIRMATION_CONFLICT"

    results = await asyncio.gather(
        *[
            orchestrator.confirm_stage(
                request.task_id,
                initial.execution_id,
                InterpretationStage.DECODE,
                run.id,
                actor="tester",
            )
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in results) == 1
    failure = next(item for item in results if isinstance(item, InfrastructureError))
    assert failure.code == "STAGE_CONFIRMATION_CONFLICT"
    current = await orchestrator.repository.get_execution(initial.execution_id)
    assert current is not None and current.status == ExecutionStatus.QUEUED
    assert len(current.state_snapshot.stage_runs) == 1


async def test_superseded_waiting_execution_cannot_be_confirmed(data_dir):
    app, request, first, orchestrator = await staged_app(data_dir)
    waiting = await orchestrator.execute_next_stage(first.execution_id, worker_id="worker")
    old_run = waiting.state_snapshot.stage_runs[-1]
    replacement_state = InterpretationState(task=request, mode="demo")
    replacement = await app.repository.create_execution(
        replacement_state,
        "RERUN",
        expected_current_execution_id=first.execution_id,
        run_mode=ExecutionRunMode.STAGED_CONFIRMATION,
    )
    assert replacement.execution_id != first.execution_id
    with pytest.raises(InfrastructureError) as caught:
        await orchestrator.confirm_stage(
            request.task_id,
            first.execution_id,
            InterpretationStage.DECODE,
            old_run.id,
            actor="tester",
        )
    assert caught.value.code == "EXECUTION_NOT_CURRENT"
    unchanged = await app.repository.get_execution(first.execution_id)
    assert unchanged is not None
    assert unchanged.status == ExecutionStatus.WAITING_CONFIRMATION
    assert unchanged.state_snapshot.stage_runs[-1].status == StageRunStatus.WAITING_CONFIRM


async def test_continuous_mode_still_runs_all_stages(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    state, _ = await app.run(TaskRequest(well_id="WELL_MOCK_001"))
    execution = await app.repository.get_execution(state.workflow_execution_id)
    assert execution is not None
    assert execution.run_mode == ExecutionRunMode.CONTINUOUS
    assert execution.status == ExecutionStatus.SUCCESS
    assert [run.stage for run in state.stage_runs] == list(STAGE_ORDER)
    assert all(run.status == StageRunStatus.CONFIRMED for run in state.stage_runs)


async def test_waiting_is_not_expired_and_cannot_run_next_stage(data_dir):
    app, _, initial, orchestrator = await staged_app(data_dir)
    waiting = await orchestrator.execute_next_stage(initial.execution_id, worker_id="worker")
    with pytest.raises(Exception) as caught:
        await orchestrator.execute_next_stage(initial.execution_id, worker_id="other")
    assert getattr(caught.value, "code", None) == "STAGE_DEPENDENCY_UNAVAILABLE"
    assert await app.repository.recover_expired_executions(
        utc_now().replace(year=utc_now().year + 1)
    ) == []
    unchanged = await app.repository.get_execution(initial.execution_id)
    assert unchanged == waiting


async def test_two_wells_are_independent(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    orchestrator = StageOrchestrator(app)
    items = []
    for well_id in ("WELL_MOCK_001", "WELL_MOCK_002"):
        request = TaskRequest(well_id=well_id)
        state = InterpretationState(task=request)
        await app.repository.create_task(state)
        execution = await app.repository.create_execution(
            state, "INITIAL", run_mode=ExecutionRunMode.STAGED_CONFIRMATION
        )
        items.append((request, execution))
    # 第二口井沿用同一份可控 Fixture，只调整文件内井号。
    source = (data_dir / "WELL_MOCK_001.json").read_text()
    (data_dir / "WELL_MOCK_002.json").write_text(
        source.replace("WELL_MOCK_001", "WELL_MOCK_002")
    )
    first_waiting, second_waiting = await asyncio.gather(*[
        orchestrator.execute_next_stage(item.execution_id, worker_id=f"worker-{index}")
        for index, (_, item) in enumerate(items)
    ])
    first_run = first_waiting.state_snapshot.stage_runs[-1]
    await orchestrator.confirm_stage(
        items[0][0].task_id,
        items[0][1].execution_id,
        InterpretationStage.DECODE,
        first_run.id,
        actor="tester",
    )
    assert await app.repository.get_execution(items[1][1].execution_id) == second_waiting


async def test_staged_local_w04_and_report_only_start_at_declared_boundary(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    request = TaskRequest(well_id="WELL_MOCK_001")
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())

    async def materialize(version):
        (data_dir / f"{version.well_id}.json").write_text(version.payload.model_dump_json())

    source_state, _ = await app.run_with_input(request, fixture, materialize)
    plan = await app.plan_rerun(request, changes=InterpretationOverride(por=0.16))
    local = await app.prepare_rerun_plan(
        request, plan, run_mode=ExecutionRunMode.STAGED_CONFIRMATION
    )
    orchestrator = StageOrchestrator(app)
    waiting = await orchestrator.execute_next_stage(
        local.execution_id, worker_id="local-worker", materialize=materialize
    )
    actual_steps = [item.step_id for item in waiting.state_snapshot.executions]
    assert waiting.start_step == StepId.W04
    assert actual_steps == list(StepId)[3:]
    assert waiting.state_snapshot.stage_runs[-1].stage == InterpretationStage.INTERPRET
    assert waiting.state_snapshot.stage_runs[-1].status == StageRunStatus.WAITING_CONFIRM

    # 独立构造既有测试使用的 REPORT_ONLY 计划，确认不会重新进入 W01～W10。
    await orchestrator.confirm_stage(
        request.task_id,
        local.execution_id,
        InterpretationStage.INTERPRET,
        waiting.state_snapshot.stage_runs[-1].id,
        actor="tester",
    )
    report_waiting = await orchestrator.execute_next_stage(
        local.execution_id, worker_id="report-before-source", materialize=materialize
    )
    await orchestrator.confirm_stage(
        request.task_id,
        local.execution_id,
        InterpretationStage.REPORT,
        report_waiting.state_snapshot.stage_runs[-1].id,
        actor="tester",
    )
    source = await app.repository.get_execution(local.execution_id)
    assert source is not None
    base = await app.plan_rerun(request, force_full_rerun=True)
    payload = base.model_dump(mode="python")
    payload["planning_reason"] = "REPORT_ONLY"
    for stage_plan in payload["stage_plans"]:
        if stage_plan["stage"] != ExecutionStage.REPORT:
            stage_plan["action"] = PlanAction.REUSE
    report_plan = ExecutionPlan.model_validate(payload)
    report_only = await app.prepare_rerun_plan(
        request, report_plan, run_mode=ExecutionRunMode.STAGED_CONFIRMATION
    )
    assert report_only.start_step is None
    report_candidate = await orchestrator.execute_next_stage(
        report_only.execution_id, worker_id="report-only", materialize=materialize
    )
    assert report_candidate.state_snapshot.executions == []
    assert report_candidate.state_snapshot.stage_runs[-1].stage == InterpretationStage.REPORT
    assert report_candidate.status == ExecutionStatus.WAITING_CONFIRMATION
    assert source_state.workflow_execution_id != report_only.execution_id


async def test_stage_worker_failure_never_becomes_waiting(data_dir, monkeypatch):
    app, _, initial, orchestrator = await staged_app(data_dir)

    async def fail_after_stage_started(state, stage):
        begin_stage(state, stage)
        await app.repository.save_execution_state(state)
        raise RuntimeError("simulated worker failure")

    monkeypatch.setattr(app.main_agent.workflow, "run_stage", fail_after_stage_started)
    with pytest.raises(RuntimeError, match="simulated"):
        await orchestrator.execute_next_stage(initial.execution_id, worker_id="worker")
    failed = await app.repository.get_execution(initial.execution_id)
    assert failed is not None
    assert failed.status == ExecutionStatus.FAILED
    assert failed.state_snapshot.stage_runs[-1].status == StageRunStatus.FAILED
    assert failed.state_snapshot.stage_runs[-1].errors[-1].code == (
        "BACKGROUND_EXECUTION_FAILED"
    )
