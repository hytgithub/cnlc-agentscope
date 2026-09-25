import json
import logging

import pytest

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.planning import ExecutionPlan, ExecutionStage, PlanAction
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import WorkflowError
from cnlc_agent.domain.models import MockFixture, TaskRequest
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.mock import MockModelGateway
from cnlc_agent.workflows.node import WorkflowNode


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


async def seed_versioned_task(data_dir):
    """使用真实应用链生成可复用来源，避免手工伪造成功业务字段。"""

    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    request = TaskRequest(well_id=fixture.well.well_id)

    async def materialize(version):
        (data_dir / f"{version.well_id}.json").write_text(version.payload.model_dump_json())

    state, _ = await app.run_with_input(request, fixture, materialize)
    source = await app.repository.get_execution(state.workflow_execution_id)
    return app, request, source, materialize


@pytest.mark.parametrize(
    "changes,start",
    [
        (InterpretationOverride(sampling_interval=0.1), StepId.W02),
        (InterpretationOverride(por=0.16), StepId.W04),
        (InterpretationOverride(perm=0.16), StepId.W04),
        (InterpretationOverride(prediction_model="prediction-v2"), StepId.W04),
    ],
)
async def test_planned_rerun_executes_only_expected_suffix(data_dir, monkeypatch, changes, start):
    app, request, source, materialize = await seed_versioned_task(data_dir)
    before = source.model_dump_json()
    called = []
    original = WorkflowNode.execute

    async def recording_execute(node, state):
        called.append(node.step_id)
        if node.step_id == start:
            assert state.well is not None and state.raw_data is not None
            assert state.data_requirements is not None
            assert state.lithology_result is None and state.petrophysics_result is None
            if start == StepId.W04:
                assert state.processed_data is not None and state.qc_result is not None
            else:
                assert state.processed_data is None and state.qc_result is None
        return await original(node, state)

    monkeypatch.setattr(WorkflowNode, "execute", recording_execute)
    state, report = await app.rerun_planned(request, changes=changes, materialize=materialize)
    index = list(StepId).index(start)
    assert called == list(StepId)[index:]
    assert [item.step_id for item in state.executions] == called
    assert [item.step_id for item in state.reused_steps] == list(StepId)[:index]
    assert all(item.source_execution_id == source.execution_id for item in state.reused_steps)
    assert state.completed_steps == list(StepId)
    assert state.final_check.result["structural_check_passed"] is True
    assert state.status == StepStatus.SUCCESS
    assert state.workflow_execution_id != source.execution_id
    assert state.trace_id != source.state_snapshot.trace_id
    saved = await app.repository.get_execution(state.workflow_execution_id)
    assert saved.override_snapshot == changes
    assert saved.input_version_id == source.input_version_id
    assert saved.markdown == report
    assert saved.state_snapshot == state
    assert InterpretationState.model_validate_json(state.model_dump_json()) == state
    assert (await app.repository.get_execution(source.execution_id)).model_dump_json() == before


async def test_planned_full_rerun_keeps_override_and_executes_all_nodes(data_dir, monkeypatch):
    app, request, source, materialize = await seed_versioned_task(data_dir)
    previous, _ = await app.rerun_planned(
        request,
        changes=InterpretationOverride(por=0.16, prediction_model="prediction-v2"),
        materialize=materialize,
    )
    called = []
    original = WorkflowNode.execute

    async def recording_execute(node, state):
        called.append(node.step_id)
        return await original(node, state)

    monkeypatch.setattr(WorkflowNode, "execute", recording_execute)
    state, _ = await app.rerun_planned(request, force_full_rerun=True, materialize=materialize)
    assert called == list(StepId)
    assert [item.step_id for item in state.executions] == list(StepId)
    assert state.reused_steps == []
    saved = await app.repository.get_execution(state.workflow_execution_id)
    assert saved.override_snapshot == InterpretationOverride(
        por=0.16, prediction_model="prediction-v2"
    )
    assert saved.input_version_id == source.input_version_id
    assert saved.execution_id != previous.workflow_execution_id


async def test_report_only_creates_new_report_without_entering_workflow(data_dir, monkeypatch):
    app, request, source, materialize = await seed_versioned_task(data_dir)
    # 来源本身是局部重跑，验证多轮复用引用仍可解析。
    partial, _ = await app.rerun_planned(
        request, changes=InterpretationOverride(por=0.16), materialize=materialize
    )
    source = await app.repository.get_execution(partial.workflow_execution_id)
    before = source.model_dump_json()
    base = await app.plan_rerun(request, force_full_rerun=True)
    payload = base.model_dump(mode="python")
    payload["planning_reason"] = "REPORT_ONLY"
    for stage in payload["stage_plans"]:
        if stage["stage"] != ExecutionStage.REPORT:
            stage["action"] = PlanAction.REUSE
    plan = ExecutionPlan.model_validate(payload)

    async def unexpected_workflow(*args, **kwargs):
        pytest.fail("REPORT_ONLY must not enter MainAgent / Workflow")

    monkeypatch.setattr(app.main_agent, "run", unexpected_workflow)
    state, report = await app.execute_rerun_plan(request, plan)
    assert state.executions == []
    assert state.current_step is None
    assert state.completed_steps == list(StepId)
    assert [item.step_id for item in state.reused_steps] == list(StepId)
    assert all(item.source_execution_id == source.execution_id for item in state.reused_steps)
    assert state.status == StepStatus.SUCCESS
    assert state.workflow_execution_id != source.execution_id
    saved = await app.repository.get_execution(state.workflow_execution_id)
    assert saved.markdown == report and report
    assert saved.state_snapshot == state
    assert saved.override_snapshot == source.override_snapshot
    assert (await app.repository.get_execution(source.execution_id)).model_dump_json() == before


async def test_reused_warning_survives_multiple_partial_executions(data_dir, fixture_data):
    del fixture_data["raw_data"]["auxiliary"]["core"]
    write_fixture(data_dir, fixture_data)
    app, request, source, materialize = await seed_versioned_task(data_dir)
    assert source.status == StepStatus.WARNING
    for changes in (InterpretationOverride(por=0.16), InterpretationOverride(perm=0.16)):
        state, _ = await app.rerun_planned(request, changes=changes, materialize=materialize)
        assert state.status == StepStatus.WARNING
        reused_w02 = next(item for item in state.reused_steps if item.step_id == StepId.W02)
        assert reused_w02.source_status == StepStatus.WARNING
        assert reused_w02.warnings
        assert state.warnings == []  # 全局旧告警未复制，告警来源保留在 reused_steps。
        assert all(item.status == StepStatus.SUCCESS for item in state.executions)


@pytest.mark.parametrize("prefix", [[], [StepId.W01], list(StepId)[:3]])
async def test_workflow_rejects_incomplete_or_unproven_prefix(data_dir, prefix):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    state = InterpretationState(task=TaskRequest(well_id="WELL_MOCK_001"), completed_steps=prefix)
    with pytest.raises(WorkflowError, match="完整连续") as caught:
        await app.main_agent.workflow.run(state, start_step=StepId.W04)
    assert caught.value.code == "INVALID_REUSE_PREFIX"
    assert state.executions == []


@pytest.mark.parametrize("same_content", [True, False])
async def test_planned_execution_binds_selected_input_and_honors_digest(data_dir, same_content):
    app, request, source, materialize = await seed_versioned_task(data_dir)
    original = await app.repository.get_input_version(source.input_version_id)
    payload = original.payload.model_copy(deep=True)
    if not same_content:
        payload.well.name = "新的输入版本井名"
    selected = await app.repository.create_input_version(request.task_id, payload)
    state, _ = await app.rerun_planned(
        request,
        input_version_id=selected.input_version_id,
        changes=InterpretationOverride(por=0.16),
        materialize=materialize,
    )
    expected = list(StepId)[3:] if same_content else list(StepId)
    assert [item.step_id for item in state.executions] == expected
    assert state.well.name == payload.well.name
    saved = await app.repository.get_execution(state.workflow_execution_id)
    assert saved.input_version_id == selected.input_version_id
    assert saved.input_version_id != source.input_version_id
    assert await app.repository.get_execution(source.execution_id) == source
