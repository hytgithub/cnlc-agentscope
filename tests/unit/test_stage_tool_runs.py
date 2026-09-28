"""四阶段 ToolRun 展示、batch 关联和失败诊断测试。"""

import json

import pytest
from pydantic import ValidationError

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.stage_orchestrator import StageOrchestrator
from cnlc_agent.application.stage_tool_runs import StageToolRunView
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.execution import ExecutionRunMode, ExecutionStatus
from cnlc_agent.domain.models import TaskRequest, utc_now
from cnlc_agent.domain.stages import STAGE_ORDER, InterpretationStage, StageRunStatus
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.domain.tool_run import ToolExecutionMode, ToolRunStatus


def test_stage_tool_run_view_rejects_bulk_and_sensitive_fields():
    """DTO 自身也封住完整曲线、报告、Fixture 和凭据字段。"""

    base = {
        "tool_run_id": "run",
        "execution_id": "execution",
        "tool_code": "get_well_data",
        "step_id": StepId.W01,
        "business_stage": InterpretationStage.DECODE,
        "status": ToolRunStatus.SUCCESS,
        "execution_mode": ToolExecutionMode.MOCK,
        "source": "mock:fixture",
        "started_at": utc_now(),
        "finished_at": utc_now(),
    }
    for field in (
        "raw_data",
        "processed_data",
        "depths",
        "curves",
        "values",
        "curve_values",
        "payload",
        "MockFixture",
        "markdown",
        "candidate_markdown",
        "token",
        "authorization",
    ):
        with pytest.raises(ValidationError, match="forbids field"):
            StageToolRunView(**base, output_summary={field: "forbidden"})


async def _run_stages(data_dir, provider: str):
    app = build_application(
        AppSettings(
            mode="demo",
            model_provider="mock",
            professional_provider=provider,
            mock_data_dir=data_dir,
            _env_file=None,
        )
    )
    request = TaskRequest(well_id="WELL_MOCK_001")
    state = InterpretationState(task=request, mode="demo")
    await app.repository.create_task(state)
    execution = await app.repository.create_execution(
        state, "INITIAL", run_mode=ExecutionRunMode.STAGED_CONFIRMATION
    )
    orchestrator = StageOrchestrator(app)
    views = {}
    for index, stage in enumerate(STAGE_ORDER):
        waiting = await orchestrator.execute_next_stage(
            execution.execution_id, worker_id=f"{provider}-{index}"
        )
        run = waiting.state_snapshot.stage_runs[-1]
        views[stage] = await orchestrator.get_stage_result(
            request.task_id, execution.execution_id, run.id
        )
        await orchestrator.confirm_stage(
            request.task_id, execution.execution_id, stage, run.id, actor="tester"
        )
    return app, request, execution, views


async def test_company_mock_stage_tool_runs_and_external_call_correlation(data_dir):
    app, _, _, views = await _run_stages(data_dir, "company_mock")
    try:
        decode = views[InterpretationStage.DECODE]
        assert {item.tool_code for item in decode.tool_runs} == {
            "company_analysis",
            "get_well_data",
        }
        preprocess = views[InterpretationStage.PREPROCESS]
        assert {item.tool_code for item in preprocess.tool_runs} == {
            "company_preprocessing",
            "check_curve_quality",
        }
        interpret = views[InterpretationStage.INTERPRET]
        assert {item.tool_code for item in interpret.tool_runs} == {
            "company_interpretation",
            "identify_lithology",
            "evaluate_petrophysics",
            "calculate_sw",
            "identify_fluid",
            "classify_layer",
            "merge_intervals",
            "validate_interpretation",
            "company_report",
            "prepare_report",
        }
        assert views[InterpretationStage.REPORT].tool_runs == []

        interpretation_batch = next(
            item for item in interpret.tool_runs if item.tool_code == "company_interpretation"
        )
        interpretation_derived = [
            item
            for item in interpret.tool_runs
            if item.tool_code
            in {
                "identify_lithology",
                "evaluate_petrophysics",
                "calculate_sw",
                "identify_fluid",
                "classify_layer",
                "merge_intervals",
                "validate_interpretation",
            }
        ]
        assert interpretation_batch.execution_mode == ToolExecutionMode.MOCK
        assert interpretation_batch.external_call_id
        assert len(interpretation_derived) == 7
        assert all(
            item.execution_mode == ToolExecutionMode.DERIVED
            and item.external_call_id == interpretation_batch.external_call_id
            for item in interpretation_derived
        )
        report_batch = next(
            item for item in interpret.tool_runs if item.tool_code == "company_report"
        )
        prepare = next(item for item in interpret.tool_runs if item.tool_code == "prepare_report")
        assert report_batch.business_stage == InterpretationStage.INTERPRET
        assert prepare.business_stage == InterpretationStage.INTERPRET
        assert report_batch.external_call_id == prepare.external_call_id

        serialized = json.dumps(interpret.model_dump(mode="json"), ensure_ascii=False).casefold()
        for forbidden in (
            '"raw_data"',
            '"processed_data"',
            '"depths"',
            '"curves"',
            '"values"',
            '"payload"',
            '"markdown"',
            '"token"',
            '"authorization"',
        ):
            assert forbidden not in serialized
    finally:
        await app.close()


async def test_fixture_profile_has_only_physically_used_business_tools(data_dir):
    app, _, _, views = await _run_stages(data_dir, "fixture")
    try:
        assert [item.tool_code for item in views[InterpretationStage.DECODE].tool_runs] == [
            "get_well_data"
        ]
        assert [
            item.tool_code for item in views[InterpretationStage.PREPROCESS].tool_runs
        ] == ["check_curve_quality"]
        assert [item.tool_code for item in views[InterpretationStage.INTERPRET].tool_runs] == [
            "identify_lithology",
            "evaluate_petrophysics",
            "calculate_sw",
            "merge_intervals",
        ]
        assert views[InterpretationStage.REPORT].tool_runs == []
    finally:
        await app.close()


async def test_failed_preprocess_projects_safe_failed_tool(data_dir, fixture_data):
    fixture_data["outputs"].pop("qc")
    (data_dir / "WELL_MOCK_001.json").write_text(
        json.dumps(fixture_data), encoding="utf-8"
    )
    app = build_application(
        AppSettings(
            mode="demo",
            model_provider="mock",
            professional_provider="fixture",
            mock_data_dir=data_dir,
            _env_file=None,
        )
    )
    request = TaskRequest(well_id="WELL_MOCK_001")
    state = InterpretationState(task=request, mode="demo")
    await app.repository.create_task(state)
    execution = await app.repository.create_execution(
        state, "INITIAL", run_mode=ExecutionRunMode.STAGED_CONFIRMATION
    )
    orchestrator = StageOrchestrator(app)
    try:
        decoded = await orchestrator.execute_next_stage(
            execution.execution_id, worker_id="decode"
        )
        await orchestrator.confirm_stage(
            request.task_id,
            execution.execution_id,
            InterpretationStage.DECODE,
            decoded.state_snapshot.stage_runs[-1].id,
            actor="tester",
        )
        failed = await orchestrator.execute_next_stage(
            execution.execution_id, worker_id="preprocess"
        )
        assert failed.status == ExecutionStatus.FAILED
        stage_run = failed.state_snapshot.stage_runs[-1]
        assert stage_run.status == StageRunStatus.FAILED
        view = await orchestrator.get_stage_result(
            request.task_id, execution.execution_id, stage_run.id
        )
        failed_tool = next(
            item for item in view.tool_runs if item.tool_code == "check_curve_quality"
        )
        assert failed_tool.status == ToolRunStatus.FAILED
        assert failed_tool.error_code == "MOCK_TOOL_RESULT_MISSING"
        assert failed_tool.error_message == "工具调用失败；请按错误代码排查"
        assert view.can_confirm is False
    finally:
        await app.close()
