"""四阶段确认视图只投影持久事实，并保持任务与大数据边界。"""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.stage_orchestrator import StageOrchestrator
from cnlc_agent.application.stage_results import StageResultView
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.execution import ExecutionRunMode, ExecutionStatus
from cnlc_agent.domain.models import MockFixture, TaskRequest
from cnlc_agent.domain.stages import (
    STAGE_ORDER,
    InterpretationStage,
    StageValidity,
    mark_downstream_stale,
)
from cnlc_agent.domain.state import InterpretationState


async def _staged_app(data_dir, well_id: str = "WELL_MOCK_001"):
    """创建带 InputVersion 的分阶段执行，materialize 由受控测试适配器完成。"""

    app = build_application(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None)
    )
    fixture = MockFixture.model_validate_json((data_dir / f"{well_id}.json").read_text())
    request = TaskRequest(well_id=well_id)
    execution = await app.prepare_initial_with_input(
        request, fixture, run_mode=ExecutionRunMode.STAGED_CONFIRMATION
    )

    async def materialize(_version) -> None:
        return None

    return app, request, execution, StageOrchestrator(app), materialize


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _all_keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _all_keys(child)}
    return set()


async def test_all_stage_projections_and_current_confirmation(data_dir):
    app, request, execution, orchestrator, materialize = await _staged_app(data_dir)
    views: dict[InterpretationStage, StageResultView] = {}
    waiting_execution = execution

    for index, stage in enumerate(STAGE_ORDER):
        waiting_execution = await orchestrator.execute_next_stage(
            execution.execution_id,
            worker_id=f"projector-worker-{index}",
            materialize=materialize,
        )
        run = waiting_execution.state_snapshot.stage_runs[-1]
        view = await orchestrator.get_stage_result(
            request.task_id, execution.execution_id, run.id
        )
        views[stage] = view
        assert view.stage == stage
        assert view.can_confirm is True
        assert view.confirmation_blockers == []
        progress = await orchestrator.get_progress(request.task_id, execution.execution_id)
        assert progress.stage_result == view
        await orchestrator.confirm_stage(
            request.task_id,
            execution.execution_id,
            stage,
            run.id,
            actor="tester",
        )

    decode = views[InterpretationStage.DECODE]
    assert decode.metrics["well_id"] == "WELL_MOCK_001"
    assert decode.metrics["curve_count"] == 6
    assert decode.metrics["sample_count"] == 3
    assert decode.metrics["input_version_id"] == execution.input_version_id
    assert decode.metrics["dataset_revision_id"] == decode.output_refs["dataset_revision_id"]
    assert {item["curve_code"] for item in decode.items} == {
        "GR",
        "DEN",
        "CNL",
        "AC",
        "RT",
        "RXO",
    }

    preprocess = views[InterpretationStage.PREPROCESS]
    assert preprocess.metrics["qc_status"] == "SUCCESS"
    assert preprocess.metrics["input_curve_count"] == 6
    assert preprocess.metrics["output_curve_count"] == 6
    assert preprocess.metrics["resampling_applied"] is False
    assert preprocess.metrics["anomaly_summary"] == "当前工具未提供该统计"
    assert preprocess.metrics["preprocess_result_ref"]

    interpret = views[InterpretationStage.INTERPRET]
    assert interpret.metrics["lithology"]["lithology"] == "砂岩（预设）"
    assert interpret.metrics["petrophysics"]["porosity"] == {
        "value": 0.16,
        "unit": "fraction",
    }
    assert interpret.metrics["fluid"]["fluid_type"] == "油水（预设）"
    assert interpret.metrics["fluid"]["water_saturation"] == {
        "value": 0.45,
        "unit": "fraction",
    }
    assert interpret.metrics["classification"]["layer_type"] == "油水同层（预设）"
    assert interpret.metrics["validation_status"] == "CONSISTENT"
    assert interpret.items == [
        {
            "top_depth": 2000.0,
            "bottom_depth": 2001.0,
            "thickness": 1.0,
            "effective_thickness": 0.8,
            "fluid_or_layer_class": "油水同层（预设）",
        }
    ]

    report = views[InterpretationStage.REPORT]
    assert report.metrics["report_state"] == "CANDIDATE"
    assert report.metrics["report_style"] == "standard"
    assert report.metrics["candidate_markdown_exists"] is True
    assert report.metrics["report_ref"]
    final_report = await orchestrator.get_stage_result(
        request.task_id, execution.execution_id, report.stage_run_id
    )
    assert final_report.metrics["report_state"] == "READY"
    assert final_report.can_confirm is False

    forbidden = {"raw_data", "processed_data", "depths", "values", "curve_values", "markdown"}
    for view in views.values():
        serialized = view.model_dump(mode="json")
        assert not (_all_keys(serialized) & forbidden)
        assert len(json.dumps(serialized, ensure_ascii=False).encode()) < 128 * 1024

    with pytest.raises(ValidationError, match="forbids field"):
        StageResultView.model_validate(
            {
                **decode.model_dump(mode="python"),
                "metrics": {"raw_data": {"depths": [1.0], "values": [2.0]}},
            }
        )

    # 缺少专业结果时只报告缺失，不借用其他结果补造岩性事实。
    task = await app.repository.get_task(request.task_id)
    stored = await app.repository.get_execution(execution.execution_id)
    assert task is not None and stored is not None
    stored.state_snapshot.lithology_result = None
    missing_view = orchestrator.results.project_loaded(
        task, stored, interpret.stage_run_id
    )
    assert "lithology" not in missing_view.metrics
    assert "lithology: 当前没有正式结果" in missing_view.missing_items


async def test_historical_and_stale_results_cannot_be_confirmed(data_dir):
    app, request, first, orchestrator, materialize = await _staged_app(data_dir)
    waiting = await orchestrator.execute_next_stage(
        first.execution_id, worker_id="first-worker", materialize=materialize
    )
    first_run = waiting.state_snapshot.stage_runs[-1]

    replacement_state = InterpretationState(task=request, mode="demo")
    replacement = await app.repository.create_execution(
        replacement_state,
        "RERUN",
        expected_current_execution_id=first.execution_id,
        run_mode=ExecutionRunMode.STAGED_CONFIRMATION,
    )
    historical = await orchestrator.get_stage_result(
        request.task_id, first.execution_id, first_run.id
    )
    assert historical.can_confirm is False
    assert "EXECUTION_NOT_CURRENT" in {
        blocker.code for blocker in historical.confirmation_blockers
    }

    stale_state = waiting.state_snapshot.model_copy(deep=True)
    stale_state.workflow_execution_id = str(uuid4())
    stale_state.stage_runs = mark_downstream_stale(
        stale_state.stage_runs,
        task_id=request.task_id,
        start_stage=InterpretationStage.DECODE,
        actor="test",
        reason="测试新工作版本失效投影",
    )
    # 先让当前 V2 终结，避免测试构造的 QUEUED 版本阻断下一版本创建。
    assert await app.repository.claim_execution(
        replacement.execution_id, "finish-worker", waiting.updated_at.replace(year=2099)
    )
    await app.repository.finish_execution(
        replacement.execution_id, "finish-worker", ExecutionStatus.FAILED
    )
    current = await app.repository.create_execution(
        stale_state,
        "RERUN",
        expected_current_execution_id=replacement.execution_id,
        run_mode=ExecutionRunMode.STAGED_CONFIRMATION,
    )
    stale = await orchestrator.get_stage_result(
        request.task_id, current.execution_id, first_run.id
    )
    assert stale.validity == StageValidity.STALE
    assert stale.can_confirm is False
    assert "STAGE_RESULT_STALE" in {blocker.code for blocker in stale.confirmation_blockers}


async def test_wrong_task_and_multiple_wells_are_isolated(data_dir):
    source = (data_dir / "WELL_MOCK_001.json").read_text()
    (data_dir / "WELL_MOCK_002.json").write_text(
        source.replace("WELL_MOCK_001", "WELL_MOCK_002")
    )
    app = build_application(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None)
    )
    records = []
    for index, well_id in enumerate(("WELL_MOCK_001", "WELL_MOCK_002")):
        fixture = MockFixture.model_validate_json((data_dir / f"{well_id}.json").read_text())
        request = TaskRequest(well_id=well_id)
        execution = await app.prepare_initial_with_input(
            request, fixture, run_mode=ExecutionRunMode.STAGED_CONFIRMATION
        )

        async def materialize(_version) -> None:
            return None

        waiting = await StageOrchestrator(app).execute_next_stage(
            execution.execution_id,
            worker_id=f"well-worker-{index}",
            materialize=materialize,
        )
        records.append((request, execution, waiting.state_snapshot.stage_runs[-1]))

    projector = StageOrchestrator(app)
    first_view = await projector.get_stage_result(
        records[0][0].task_id, records[0][1].execution_id, records[0][2].id
    )
    second_view = await projector.get_stage_result(
        records[1][0].task_id, records[1][1].execution_id, records[1][2].id
    )
    assert first_view.well_id == "WELL_MOCK_001"
    assert second_view.well_id == "WELL_MOCK_002"
    assert first_view.task_id != second_view.task_id
    with pytest.raises(InfrastructureError) as caught:
        await projector.get_stage_result(
            records[1][0].task_id,
            records[0][1].execution_id,
            records[0][2].id,
        )
    assert caught.value.code == "EXECUTION_NOT_FOUND"
