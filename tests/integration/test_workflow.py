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
    caplog.set_level(logging.DEBUG)
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
    events = [
        json.loads(record.message) for record in caplog.records if record.name == "cnlc_agent.trace"
    ]
    assert {item["agent"] for item in events if item["event"] == "agent.start"} == {
        "MainAgent",
        "InterpretationAgent",
        "ValidationAgent",
    }
    assert len([item for item in events if item["event"] == "tool.result"]) == 6
    assert all(item["trace_id"] == state.trace_id for item in events)
    step_events = [item for item in events if item["event"] == "workflow.step.start"]
    assert [item["step_name"] for item in step_events] == [
        "加载井段资料",
        "检查资料完整性",
        "检查曲线质量",
        "识别岩性",
        "评价储层物性",
        "识别流体性质",
        "进行油气水层分类",
        "整理层段解释结果",
        "综合验证解释结果",
        "执行最终一致性检查",
    ]
    assert all(item["step_description"] for item in step_events)
    assert all(item["processing_data"] for item in step_events)
    w03_input = next(item for item in step_events if item["step_id"] == "W03")["input_data"]
    assert w03_input["raw_data"]["curves"]["DEN"] == {
        "unit": "g/cm3",
        "values": [2.4, 2.42, 2.41],
        "value_count": 3,
    }
    state_events = [item for item in events if item["event"] == "state.change"]
    w01_output = next(item for item in state_events if item["step_id"] == "W01")["output_data"]
    assert w01_output["updated_fields"]["raw_data"]["curves"]["GR"]["values"] == [
        45.0,
        48.0,
        46.0,
    ]
    w04_output = next(item for item in state_events if item["step_id"] == "W04")["output_data"]
    assert w04_output["updated_fields"]["lithology_result"]["result"] == {
        "lithology": "砂岩（预设）"
    }
    assert w04_output["updated_fields"]["lithology_result"]["is_mock"] is True
    progress = "\n".join(
        record.message for record in caplog.records if record.name == "cnlc_agent.progress"
    )
    assert "本阶段处理内容：读取本次待解释井段的曲线和辅助资料" in progress
    assert "数据范围：井号、井段曲线、辅助资料" in progress
    assert "输入：depths；曲线[GR, DEN, CNL, AC, RT, RXO]" in progress
    assert "辅助资料[岩心资料(core), 录井油气显示(mud_logging), 试油资料(well_test)]" in progress
    assert "输出：lithology_result：lithology=砂岩（预设）；Mock" in progress
    assert "本层段最终解释结果：" in progress
    assert "层段：2000.0–2001.0 m，总厚度=1.0 m，有效厚度=0.8 m" in progress
    assert "层类型：油水同层（预设）" in progress
    assert "结果性质：Mock/历史解释演示结果，不是独立专业复算结论" in progress
    assert '"values"' not in progress
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


async def test_demo_mode_wraps_valid_json_model_payload(data_dir, monkeypatch):
    async def json_only_response(self, request):
        return {"model_payload": request.purpose}

    monkeypatch.setattr(MockModelGateway, "generate", json_only_response)
    app = build_application(AppSettings(mode="demo", mock_data_dir=data_dir, _env_file=None))
    state, _ = await app.run(TaskRequest(well_id="WELL_MOCK_001"))

    assert state.status == StepStatus.SUCCESS
    assert state.fluid_result is not None
    assert state.fluid_result.source == "model:demo-normalized"
    assert state.layer_classification is not None
    assert state.layer_classification.result["model_payload"] == "classification"


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
