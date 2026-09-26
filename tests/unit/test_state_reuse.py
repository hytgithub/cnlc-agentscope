"""复用装配边界：来源不完整、跨任务或计划失效时显式拒绝。"""

import pytest
from pydantic import ValidationError

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.planning import ExecutionPlan, PlanAction
from cnlc_agent.application.state_reuse import StateReuseAssembler
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import WorkflowError
from cnlc_agent.domain.models import ErrorDetail, MockFixture, TaskRequest
from cnlc_agent.domain.override import InterpretationOverride


@pytest.fixture
async def reuse_context(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    request = TaskRequest(well_id="WELL_MOCK_001")
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())

    async def materialize(version):
        (data_dir / f"{version.well_id}.json").write_text(version.payload.model_dump_json())

    first, _ = await app.run_with_input(request, fixture, materialize)
    source = await app.repository.get_execution(first.workflow_execution_id)
    selected = await app.repository.get_input_version(source.input_version_id)
    plan = await app.plan_rerun(request, changes=InterpretationOverride(por=0.16))
    return app, request, source, selected, plan


def assemble(context, *, source_override=None):
    _, request, source, selected, plan = context
    return StateReuseAssembler().assemble(
        plan,
        source if source_override is None else source_override,
        request,
        selected_input=selected,
        source_input=selected,
    )


async def test_assembler_copies_only_business_prefix_with_fresh_runtime(reuse_context):
    _, _, source, _, _ = reuse_context
    old = source.state_snapshot
    old.errors.append(ErrorDetail(code="OLD_DIAGNOSTIC", message="旧诊断"))
    old.warnings.append("旧全局警告")
    old.rollback_count = 2
    before = source.model_dump_json()
    state = assemble(reuse_context)
    for field in ("well", "raw_data", "data_requirements", "processed_data", "qc_result"):
        assert getattr(state, field) == getattr(old, field)
        assert getattr(state, field) is not getattr(old, field)
    for field in (
        "lithology_result", "petrophysics_result", "fluid_result", "layer_classification",
        "interval_result", "validation_result", "final_check",
    ):
        assert getattr(state, field) is None
    assert state.workflow_execution_id != old.workflow_execution_id
    assert state.trace_id != old.trace_id
    assert state.created_at > old.created_at
    assert state.updated_at > old.updated_at
    assert state.status == StepStatus.PENDING
    assert state.current_step is None
    assert state.executions == []
    assert state.errors == state.warnings == state.missing_data == []
    assert state.rollback_count == 0
    assert state.review_required is False
    assert state.completed_steps == list(StepId)[:3]
    assert len(state.changes) == 3
    assert all(item.actor == "StateReuseAssembler" for item in state.changes)
    assert all(item not in old.changes for item in state.changes)
    state.raw_data.depths[0] += 0.1
    assert source.model_dump_json() == before


@pytest.mark.parametrize(
    "field", ["well", "raw_data", "data_requirements", "processed_data", "qc_result"]
)
async def test_missing_business_field_is_rejected(reuse_context, field):
    source = reuse_context[2]
    setattr(source.state_snapshot, field, None)
    with pytest.raises(WorkflowError) as caught:
        assemble(reuse_context)
    assert caught.value.code == "REUSE_SOURCE_INCOMPLETE"


@pytest.mark.parametrize(
    "status", [StepStatus.FAILED, StepStatus.BLOCKED, StepStatus.REVIEW_REQUIRED]
)
async def test_failed_source_is_rejected_even_with_business_results(reuse_context, status):
    reuse_context[2].status = status
    with pytest.raises(WorkflowError) as caught:
        assemble(reuse_context)
    assert caught.value.code == "REUSE_SOURCE_INVALID_STATUS"


async def test_cross_task_source_is_rejected(reuse_context):
    source = reuse_context[2].model_copy(update={"task_id": "another-task"})
    with pytest.raises(WorkflowError) as caught:
        assemble(reuse_context, source_override=source)
    assert caught.value.code == "REUSE_SOURCE_TASK_MISMATCH"


async def test_source_input_binding_must_match_plan(reuse_context):
    reuse_context[2].input_version_id = "another-input-version"
    with pytest.raises(WorkflowError) as caught:
        assemble(reuse_context)
    assert caught.value.code == "REUSE_SOURCE_INPUT_MISMATCH"


async def test_changed_selected_input_cannot_reuse_old_results(reuse_context):
    app, request, source, original, plan = reuse_context
    changed = original.payload.model_copy(deep=True)
    changed.well.name = "新输入内容"
    selected = await app.repository.create_input_version(request.task_id, changed)
    plan.selected_input_version_id = selected.input_version_id
    with pytest.raises(WorkflowError) as caught:
        StateReuseAssembler().assemble(
            plan, source, request, selected_input=selected, source_input=original
        )
    assert caught.value.code == "REUSE_SOURCE_INPUT_MISMATCH"


@pytest.mark.parametrize("broken", ["completed", "record", "status"])
async def test_incomplete_step_provenance_is_rejected(reuse_context, broken):
    state = reuse_context[2].state_snapshot
    if broken == "completed":
        state.completed_steps.remove(StepId.W02)
    elif broken == "record":
        state.executions.pop(1)
    else:
        state.executions[1].status = StepStatus.FAILED
    with pytest.raises(WorkflowError) as caught:
        assemble(reuse_context)
    assert caught.value.code == "REUSE_SOURCE_INCOMPLETE"


async def test_missing_source_rejected_before_execution_creation(reuse_context, monkeypatch):
    app, request, _, _, plan = reuse_context
    before = await app.repository.list_executions(request.task_id)

    async def missing_source(execution_id):
        return None

    monkeypatch.setattr(app.repository, "get_execution", missing_source)
    with pytest.raises(WorkflowError) as caught:
        await app.execute_rerun_plan(request, plan)
    assert caught.value.code == "REUSE_SOURCE_NOT_FOUND"
    assert await app.repository.list_executions(request.task_id) == before


async def test_invalid_stage_order_and_invalid_reuse_impact_are_rejected(reuse_context):
    _, _, _, _, plan = reuse_context
    payload = plan.model_dump(mode="python")
    payload["stage_plans"][0]["action"] = PlanAction.RUN
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(payload)
    # PREPROCESS 已被新采样间隔影响，不能用手工计划绕过依赖判定。
    plan.effective_override = InterpretationOverride(sampling_interval=0.1)
    with pytest.raises(WorkflowError) as caught:
        assemble(reuse_context)
    assert caught.value.code == "REUSE_PLAN_CONFLICT"


async def test_partial_source_validation_runs_again_in_service(reuse_context, monkeypatch):
    app, request, source, _, plan = reuse_context
    source.state_snapshot.qc_result = None

    async def incomplete_source(execution_id):
        return source

    monkeypatch.setattr(app.repository, "get_execution", incomplete_source)
    with pytest.raises(WorkflowError) as caught:
        await app.execute_rerun_plan(request, plan)
    assert caught.value.code == "REUSE_SOURCE_INCOMPLETE"
    assert len(await app.repository.list_executions(request.task_id)) == 1
