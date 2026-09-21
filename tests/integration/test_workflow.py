import json
import logging

import pytest

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.mock import MockModelGateway


def write_fixture(data_dir, data):
    (data_dir / "WELL_MOCK_001.json").write_text(json.dumps(data), encoding="utf-8")


async def run(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    return await app.run(TaskRequest(well_id="WELL_MOCK_001"))


async def test_complete_chain_roundtrip_and_trace(data_dir, caplog):
    caplog.set_level(logging.INFO, logger="cnlc_agent.trace")
    state, report = await run(data_dir)
    assert state.status == StepStatus.SUCCESS
    assert state.completed_steps == list(StepId)
    assert [item.step_id for item in state.executions] == list(StepId)
    assert len(state.changes) == 10
    assert all(item.started_at <= item.ended_at for item in state.executions)
    assert all(item.actor == "InterpretationWorkflow" for item in state.changes)
    restored = InterpretationState.model_validate_json(state.model_dump_json())
    assert restored == state
    assert state.layer_classification.result["layer_type"] in report
    assert state.fluid_result.result["sw_result"]["is_mock"] is True
    events = [json.loads(record.message) for record in caplog.records]
    assert {item["agent"] for item in events if item["event"] == "agent.start"} == {
        "MainAgent",
        "InterpretationAgent",
        "ValidationAgent",
    }
    assert len([item for item in events if item["event"] == "tool.result"]) == 6
    assert all(item["trace_id"] == state.trace_id for item in events)
    assert "Mock" in report


async def test_repeat_runs_have_distinct_ids_and_same_results(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    first, _ = await app.run(TaskRequest(well_id="WELL_MOCK_001"))
    second, _ = await app.run(TaskRequest(well_id="WELL_MOCK_001"))
    assert first.task.task_id != second.task.task_id
    assert first.trace_id != second.trace_id
    assert first.layer_classification == second.layer_classification
    assert len(second.completed_steps) == 10


async def test_demo_mode_runs_all_steps_with_missing_input_and_skips_validation(
    data_dir, fixture_data, monkeypatch
):
    del fixture_data["raw_data"]["curves"]["GR"]
    del fixture_data["raw_data"]["auxiliary"]["core"]
    write_fixture(data_dir, fixture_data)
    purposes = []
    original_generate = MockModelGateway.generate

    async def recording_generate(self, request):
        purposes.append(request.purpose)
        return await original_generate(self, request)

    monkeypatch.setattr(MockModelGateway, "generate", recording_generate)
    app = build_application(AppSettings(mode="demo", mock_data_dir=data_dir, _env_file=None))
    state, _ = await app.run(TaskRequest(well_id="WELL_MOCK_001"))

    assert state.mode == "demo"
    assert state.status == StepStatus.SUCCESS
    assert state.completed_steps == list(StepId)
    assert [execution.step_id for execution in state.executions] == list(StepId)
    assert len(state.executions) == 10
    assert state.missing_data == []
    assert state.review_required is False
    assert state.validation_result is not None
    assert state.validation_result.result["demo_skipped"] is True
    assert purposes == ["fluid", "classification"]


async def test_missing_required_data_blocks_without_fake_results(data_dir, fixture_data):
    del fixture_data["raw_data"]["curves"]["GR"]
    write_fixture(data_dir, fixture_data)
    state, report = await run(data_dir)
    assert state.status == StepStatus.BLOCKED
    assert state.current_step == StepId.W02
    assert state.completed_steps == [StepId.W01]
    assert state.lithology_result is None
    assert state.final_check is None
    assert state.missing_data[0].affected_step == StepId.W03
    assert "任务未完成" in report


async def test_missing_recommended_data_warns_and_continues(data_dir, fixture_data):
    del fixture_data["raw_data"]["auxiliary"]["core"]
    write_fixture(data_dir, fixture_data)
    state, report = await run(data_dir)
    assert state.status == StepStatus.WARNING
    assert len(state.completed_steps) == 10
    assert "raw_data.auxiliary.core" in report


@pytest.mark.parametrize(
    "key,step,field,code",
    [
        ("petrophysics", StepId.W05, "petrophysics_result", "MOCK_TOOL_RESULT_MISSING"),
        ("fluid", StepId.W06, "fluid_result", "MOCK_RESPONSE_MISSING"),
    ],
)
async def test_tool_and_model_failure(data_dir, fixture_data, key, step, field, code):
    del fixture_data["outputs"][key]
    write_fixture(data_dir, fixture_data)
    state, report = await run(data_dir)
    assert state.status == StepStatus.FAILED
    assert state.current_step == step
    assert getattr(state, field) is None
    assert state.errors[-1].code == code
    assert state.errors[-1].step_id == step
    assert state.final_check is None
    assert "诊断摘要" in report


@pytest.mark.parametrize(
    "validation_status,expected",
    [
        ("PARTIAL_CONFLICT", StepStatus.WARNING),
        ("SERIOUS_CONFLICT", StepStatus.REVIEW_REQUIRED),
        ("INSUFFICIENT_EVIDENCE", StepStatus.REVIEW_REQUIRED),
    ],
)
async def test_validation_branches_never_overwrite_interpretation(
    data_dir,
    fixture_data,
    validation_status,
    expected,
):
    fixture_data["validation"]["validation_status"] = validation_status
    fixture_data["validation"]["conflicts"] = ["演示验证证据冲突"]
    write_fixture(data_dir, fixture_data)
    state, _ = await run(data_dir)
    assert state.status == expected
    assert state.layer_classification.result == fixture_data["outputs"]["classification"]["result"]
    if expected == StepStatus.REVIEW_REQUIRED:
        assert state.current_step == StepId.W09
        assert state.review_required is True
        assert state.final_check is None
        assert state.rollback_count == 0


async def test_invalid_model_result_is_visible_failure(data_dir, monkeypatch):
    async def invalid_response(self, request):
        return {"unexpected": "not a structured result"}

    monkeypatch.setattr(MockModelGateway, "generate", invalid_response)
    state, _ = await run(data_dir)
    assert state.status == StepStatus.FAILED
    assert state.errors[-1].code == "INVALID_RESULT"
    assert state.fluid_result is None


async def test_unknown_well_produces_failed_task(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    state, _ = await app.run(TaskRequest(well_id="MISSING"))
    assert state.status == StepStatus.FAILED
    assert state.current_step == StepId.W01
    assert state.errors[-1].code == "WELL_NOT_FOUND"


async def test_malformed_tool_output_stops_workflow(data_dir, monkeypatch):
    from cnlc_agent.tools.mock import MockResultTool

    async def invalid_output(self, request):
        return None

    monkeypatch.setattr(MockResultTool, "execute", invalid_output)
    state, report = await run(data_dir)
    assert state.status == StepStatus.FAILED
    assert state.current_step == StepId.W03
    assert state.errors[-1].code == "INVALID_TOOL_OUTPUT"
    assert state.qc_result is None
    assert state.final_check is None
    assert "诊断摘要" in report
