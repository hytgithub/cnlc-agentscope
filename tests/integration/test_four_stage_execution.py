"""使用真实现有业务链验证阶段边界、局部重跑和快照兼容性。"""

import json

import pytest

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.models import MockFixture, TaskRequest
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.stages import (
    OUTPUT_KEY,
    STAGE_ORDER,
    InterpretationStage,
    StageRunStatus,
    StageValidity,
)
from cnlc_agent.domain.state import InterpretationState


async def seed(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    request = TaskRequest(well_id="WELL_MOCK_001")
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())

    async def materialize(version):
        (data_dir / f"{version.well_id}.json").write_text(version.payload.model_dump_json())

    state, _ = await app.run_with_input(request, fixture, materialize)
    return app, request, state, materialize


async def test_existing_chain_records_four_auto_confirmed_stages(data_dir):
    app, request, state, _ = await seed(data_dir)
    assert state.status == StepStatus.SUCCESS
    assert [run.stage for run in state.stage_runs] == list(STAGE_ORDER)
    assert all(
        run.task_id == request.task_id
        and run.status == StageRunStatus.CONFIRMED
        and run.confirmed_by == "system"
        for run in state.stage_runs
    )
    decode, preprocess, interpret, report = state.stage_runs
    assert decode.input_refs["input_version_id"] == state.input_version_id
    assert preprocess.input_refs["dataset_revision_id"] == decode.output_refs["dataset_revision_id"]
    assert (
        interpret.input_refs["preprocess_revision_id"]
        == preprocess.output_refs["preprocess_revision_id"]
    )
    assert (
        report.input_refs["interpretation_revision_id"]
        == interpret.output_refs["interpretation_revision_id"]
    )
    assert all(run.started_at <= run.finished_at <= run.confirmed_at for run in state.stage_runs)
    assert (await app.repository.get_execution(state.workflow_execution_id)).state_snapshot == state
    assert InterpretationState.model_validate_json(state.model_dump_json()) == state


@pytest.mark.parametrize(
    "changes,first",
    [
        (InterpretationOverride(sampling_interval=0.1), InterpretationStage.PREPROCESS),
        (InterpretationOverride(por=0.16), InterpretationStage.INTERPRET),
    ],
)
async def test_local_rerun_retains_stale_history_and_records_only_actual_stages(
    data_dir, changes, first
):
    app, request, original, materialize = await seed(data_dir)
    before = original.model_dump_json()
    state, _ = await app.rerun_planned(request, changes=changes, materialize=materialize)
    suffix = STAGE_ORDER[STAGE_ORDER.index(first) :]
    assert [run.stage for run in state.stage_runs[4:]] == list(suffix)
    assert all(run.execution_id == state.workflow_execution_id for run in state.stage_runs[4:])
    for old, copied in zip(original.stage_runs, state.stage_runs[:4], strict=True):
        assert copied.id == old.id
        assert copied.execution_id == original.workflow_execution_id
        assert copied.output_refs == old.output_refs
        assert copied.status == StageRunStatus.CONFIRMED
        assert copied.validity == (
            StageValidity.STALE if old.stage in suffix else StageValidity.CURRENT
        )
    assert (
        await app.repository.get_execution(original.workflow_execution_id)
    ).state_snapshot == original
    assert original.model_dump_json() == before
    assert all(run.validity == StageValidity.CURRENT for run in original.stage_runs)
    assert len({run.id for run in state.stage_runs}) == len(state.stage_runs)
    saved = (await app.repository.get_execution(state.workflow_execution_id)).state_snapshot
    assert saved == state


async def test_failed_workflow_retains_stage_error_and_diagnostic_report(data_dir, fixture_data):
    del fixture_data["outputs"]["fluid"]
    (data_dir / "WELL_MOCK_001.json").write_text(json.dumps(fixture_data))
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    state, report = await app.run(TaskRequest(well_id="WELL_MOCK_001"))
    assert state.status == StepStatus.FAILED
    assert [run.stage for run in state.stage_runs] == list(STAGE_ORDER[:3])
    assert state.stage_runs[-1].status == StageRunStatus.FAILED
    assert state.stage_runs[-1].errors[-1].code == "MOCK_RESPONSE_MISSING"
    assert state.current_step == StepId.W06
    assert "诊断摘要" in report
    assert not any(run.stage == InterpretationStage.REPORT for run in state.stage_runs)


async def test_old_snapshot_without_stage_history_still_supports_local_rerun(data_dir):
    app, request, original, materialize = await seed(data_dir)
    old_data = original.model_dump(mode="json")
    old_data.pop("stage_runs")
    legacy = InterpretationState.model_validate(old_data)
    assert legacy.stage_runs == []
    await app.repository.save(legacy, "旧版报告")
    state, _ = await app.rerun_planned(
        request, changes=InterpretationOverride(por=0.16), materialize=materialize
    )
    assert [run.stage for run in state.stage_runs] == list(STAGE_ORDER[2:])
    assert state.stage_runs[0].input_refs["preprocess_revision_id"] == (
        f"execution:{legacy.workflow_execution_id}:PREPROCESS"
    )
    assert OUTPUT_KEY[InterpretationStage.INTERPRET] in state.stage_runs[0].output_refs


async def test_report_failure_retains_failed_stage_snapshot(data_dir, monkeypatch):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    request = TaskRequest(well_id="WELL_MOCK_001")

    def fail_report(state):
        raise ValueError("测试报告异常")

    monkeypatch.setattr(app.reports, "to_markdown", fail_report)
    with pytest.raises(ValueError):
        await app.run(request)
    executions = await app.repository.list_executions(request.task_id)
    assert len(executions) == 1
    run = executions[0].state_snapshot.stage_runs[-1]
    assert run.stage == InterpretationStage.REPORT
    assert run.status == StageRunStatus.FAILED
    assert run.errors[-1].code == "REPORT_GENERATION_FAILED"


async def test_cancelled_execution_ends_running_stage(data_dir, monkeypatch):
    """执行取消后保留阶段失败原因，避免后台终态与阶段运行态冲突。"""
    import asyncio

    from cnlc_agent.domain.execution import ExecutionStatus
    from cnlc_agent.workflows.node import WorkflowNode

    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    request = TaskRequest(well_id="WELL_MOCK_001")
    entered = asyncio.Event()

    async def pending_node(node, state):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(WorkflowNode, "execute", pending_node)
    task = asyncio.create_task(app.run(request))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    execution = (await app.repository.list_executions(request.task_id))[0]
    assert execution.status == ExecutionStatus.FAILED
    assert execution.state_snapshot.stage_runs[0].status == StageRunStatus.FAILED
    assert (
        execution.state_snapshot.stage_runs[0].errors[-1].code == "BACKGROUND_EXECUTION_CANCELLED"
    )
    assert execution.state_snapshot.executions[0].status == StepStatus.FAILED


@pytest.mark.parametrize("style", ["standard", "compact"])
async def test_report_records_actual_config_reference(data_dir, style):
    app = build_application(AppSettings(mock_data_dir=data_dir, report_style=style, _env_file=None))
    state, _ = await app.run(TaskRequest(well_id="WELL_MOCK_001"))
    assert state.stage_runs[-1].input_refs["report_config_revision_id"] == (
        f"report-config:style:{style}"
    )
