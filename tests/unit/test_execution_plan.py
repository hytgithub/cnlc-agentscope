"""四阶段计划是纯判定结果，不应创建 Execution 或改变现有十步执行。"""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.planning import (
    DEPENDENCY_IMPACT,
    STAGE_ORDER,
    STAGE_STEPS,
    DependencyResolver,
    ExecutionStage,
    PlanAction,
)
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import DataError, WorkflowError
from cnlc_agent.domain.execution import Execution, ExecutionStatus
from cnlc_agent.domain.inputs import InterpretationInputVersion, fixture_digest
from cnlc_agent.domain.models import MockFixture, TaskRequest, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository


def input_version(
    task_id: str, fixture: MockFixture, sequence: int = 1
) -> InterpretationInputVersion:
    return InterpretationInputVersion(
        task_id=task_id,
        well_id=fixture.well.well_id,
        sequence=sequence,
        source_type="UPLOAD",
        content_sha256=fixture_digest(fixture),
        payload=fixture,
    )


def execution(
    request: TaskRequest,
    input_id: str | None,
    override: InterpretationOverride | None = None,
    status: StepStatus = StepStatus.SUCCESS,
    sequence: int = 1,
) -> Execution:
    state = InterpretationState(task=request, status=status)
    return Execution(
        execution_id=state.workflow_execution_id,
        task_id=request.task_id,
        sequence=sequence,
        status=status,
        finished_at=utc_now(),
        state_snapshot=state,
        markdown="report" if status in {StepStatus.SUCCESS, StepStatus.WARNING} else "",
        trigger_type="INITIAL" if sequence == 1 else "RERUN",
        input_version_id=input_id,
        override_snapshot=override or InterpretationOverride(),
    )


def actions(plan) -> tuple[PlanAction, ...]:
    assert [item.stage for item in plan.stage_plans] == list(STAGE_ORDER)
    assert [item.steps for item in plan.stage_plans] == [
        STAGE_STEPS[stage] for stage in STAGE_ORDER
    ]
    assert plan.action_for(ExecutionStage.REPORT) == PlanAction.RUN
    return tuple(item.action for item in plan.stage_plans)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            InterpretationOverride(por=0.16),
            (PlanAction.REUSE, PlanAction.REUSE, PlanAction.RUN, PlanAction.RUN),
        ),
        (
            InterpretationOverride(perm=0.16),
            (PlanAction.REUSE, PlanAction.REUSE, PlanAction.RUN, PlanAction.RUN),
        ),
        (
            InterpretationOverride(prediction_model="prediction-v2"),
            (PlanAction.REUSE, PlanAction.REUSE, PlanAction.RUN, PlanAction.RUN),
        ),
        (
            InterpretationOverride(sampling_interval=0.1),
            (PlanAction.REUSE, PlanAction.RUN, PlanAction.RUN, PlanAction.RUN),
        ),
        (
            InterpretationOverride(sampling_interval=0.1, por=0.16),
            (PlanAction.REUSE, PlanAction.RUN, PlanAction.RUN, PlanAction.RUN),
        ),
    ],
)
def test_field_impacts_start_at_earliest_stage(fixture_data, changes, expected):
    fixture = MockFixture.model_validate(fixture_data)
    request = TaskRequest(well_id=fixture.well.well_id)
    source_input = input_version(request.task_id, fixture)
    source = execution(request, source_input.input_version_id)
    plan = DependencyResolver().plan(
        task_id=request.task_id,
        selected_input=source_input,
        source_execution=source,
        source_input=source_input,
        current_execution=source,
        requested_changes=changes,
    )
    assert actions(plan) == expected
    assert plan.source_execution_id == source.execution_id
    assert plan.effective_override == changes
    assert plan.changed_fields == tuple(changes.model_dump(exclude_none=True))
    assert STAGE_STEPS[ExecutionStage.DATA_DECODE] == (StepId.W01,)
    assert STAGE_STEPS[ExecutionStage.PREPROCESS] == (StepId.W02, StepId.W03)
    assert STAGE_STEPS[ExecutionStage.INTERPRET][-1] == StepId.W10
    assert STAGE_STEPS[ExecutionStage.REPORT] == ()


def test_changed_input_digest_forces_all_stages(fixture_data):
    fixture = MockFixture.model_validate(fixture_data)
    request = TaskRequest(well_id=fixture.well.well_id)
    original = input_version(request.task_id, fixture)
    changed_fixture = fixture.model_copy(deep=True)
    changed_fixture.well.name = "另一份输入内容"
    selected = input_version(request.task_id, changed_fixture, 2)
    source = execution(request, original.input_version_id)
    plan = DependencyResolver().plan(
        task_id=request.task_id,
        selected_input=selected,
        source_execution=source,
        source_input=original,
        current_execution=source,
        requested_changes=InterpretationOverride(por=0.16),
    )
    assert actions(plan) == (PlanAction.RUN,) * 4
    assert plan.changed_fields == ("input_content", "por")
    assert plan.selected_input_version_id == selected.input_version_id


def test_equal_digest_with_different_version_id_does_not_force_decode(fixture_data):
    fixture = MockFixture.model_validate(fixture_data)
    request = TaskRequest(well_id=fixture.well.well_id)
    original = input_version(request.task_id, fixture)
    selected = input_version(request.task_id, fixture.model_copy(deep=True), 2)
    assert original.input_version_id != selected.input_version_id
    assert original.content_sha256 == selected.content_sha256
    source = execution(request, original.input_version_id)
    resolver = DependencyResolver()
    plan = resolver.plan(
        task_id=request.task_id,
        selected_input=selected,
        source_execution=source,
        source_input=original,
        requested_changes=InterpretationOverride(por=0.16),
    )
    assert actions(plan) == (
        PlanAction.REUSE,
        PlanAction.REUSE,
        PlanAction.RUN,
        PlanAction.RUN,
    )
    assert plan.selected_input_version_id == selected.input_version_id
    with pytest.raises(DataError) as caught:
        resolver.plan(
            task_id=request.task_id,
            selected_input=selected,
            source_execution=source,
            source_input=original,
        )
    assert caught.value.code == "NO_EFFECTIVE_CHANGE"


def test_full_rerun_preserves_effective_override(fixture_data):
    fixture = MockFixture.model_validate(fixture_data)
    request = TaskRequest(well_id=fixture.well.well_id)
    selected = input_version(request.task_id, fixture)
    previous = InterpretationOverride(por=0.16, prediction_model="prediction-v2")
    source = execution(request, selected.input_version_id, previous)
    plan = DependencyResolver().plan(
        task_id=request.task_id,
        selected_input=selected,
        source_execution=source,
        source_input=selected,
        current_execution=source,
        force_full_rerun=True,
    )
    assert actions(plan) == (PlanAction.RUN,) * 4
    assert plan.effective_override == previous
    assert plan.changed_fields == ()
    assert plan.planning_reason == "FULL_RERUN"


def test_multiround_override_merge_and_same_value_rejection(fixture_data):
    fixture = MockFixture.model_validate(fixture_data)
    request = TaskRequest(well_id=fixture.well.well_id)
    selected = input_version(request.task_id, fixture)
    previous = InterpretationOverride(por=0.16, perm=0.16)
    source = execution(request, selected.input_version_id, previous, sequence=2)
    resolver = DependencyResolver()
    plan = resolver.plan(
        task_id=request.task_id,
        selected_input=selected,
        source_execution=source,
        source_input=selected,
        current_execution=source,
        requested_changes=InterpretationOverride(prediction_model="prediction-v2"),
    )
    assert plan.effective_override == InterpretationOverride(
        por=0.16, perm=0.16, prediction_model="prediction-v2"
    )
    assert plan.changed_fields == ("prediction_model",)
    assert source.override_snapshot == previous
    with pytest.raises(DataError) as caught:
        resolver.plan(
            task_id=request.task_id,
            selected_input=selected,
            source_execution=source,
            source_input=selected,
            current_execution=source,
            requested_changes=InterpretationOverride(por=0.16),
        )
    assert caught.value.code == "NO_EFFECTIVE_CHANGE"


def test_failed_or_missing_source_never_reuses_stages(fixture_data):
    fixture = MockFixture.model_validate(fixture_data)
    request = TaskRequest(well_id=fixture.well.well_id)
    selected = input_version(request.task_id, fixture)
    failed = execution(request, selected.input_version_id, status=StepStatus.FAILED)
    resolver = DependencyResolver()
    for source, source_input in ((failed, selected), (None, None)):
        plan = resolver.plan(
            task_id=request.task_id,
            selected_input=selected,
            source_execution=source,
            source_input=source_input,
            current_execution=failed,
        )
        assert actions(plan) == (PlanAction.RUN,) * 4
        assert plan.source_execution_id is None
        assert plan.planning_reason == "NO_REUSABLE_SOURCE"


def test_unknown_dependency_is_rejected(fixture_data, monkeypatch):
    fixture = MockFixture.model_validate(fixture_data)
    request = TaskRequest(well_id=fixture.well.well_id)
    selected = input_version(request.task_id, fixture)
    source = execution(request, selected.input_version_id)
    monkeypatch.delitem(DEPENDENCY_IMPACT, "por")
    with pytest.raises(WorkflowError) as caught:
        DependencyResolver().plan(
            task_id=request.task_id,
            selected_input=selected,
            source_execution=source,
            source_input=selected,
            requested_changes=InterpretationOverride(por=0.16),
        )
    assert caught.value.code == "UNSUPPORTED_DEPENDENCY_CHANGE"
    with pytest.raises(ValidationError):
        InterpretationOverride.model_validate({"unmapped_parameter": 1})


async def test_service_uses_latest_successful_not_failed_current(data_dir):
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    request = TaskRequest(well_id=fixture.well.well_id)
    repository = InMemoryTaskRepository()
    first = InterpretationState(task=request)
    await repository.create_task(first)
    selected = await repository.create_input_version(request.task_id, fixture)
    await repository.create_execution(first, "INITIAL", input_version_id=selected.input_version_id)
    first.status = StepStatus.SUCCESS
    await repository.save(first, "successful report")
    assert await repository.claim_execution(
        first.workflow_execution_id, "test-worker", utc_now() + timedelta(minutes=1)
    )
    await repository.finish_execution(
        first.workflow_execution_id, "test-worker", ExecutionStatus.SUCCESS
    )
    failed = InterpretationState(task=request)
    await repository.create_execution(
        failed,
        input_version_id=selected.input_version_id,
        override_snapshot=InterpretationOverride(por=0.16, perm=0.16),
    )
    failed.status = StepStatus.FAILED
    await repository.save(failed, "failed report")
    assert await repository.claim_execution(
        failed.workflow_execution_id, "test-worker", utc_now() + timedelta(minutes=1)
    )
    await repository.finish_execution(
        failed.workflow_execution_id, "test-worker", ExecutionStatus.FAILED
    )
    app = build_application(
        AppSettings(mock_data_dir=data_dir, _env_file=None), task_repository=repository
    )
    plan = await app.plan_rerun(
        request, changes=InterpretationOverride(prediction_model="prediction-v2")
    )
    assert plan.source_execution_id == first.workflow_execution_id
    assert plan.effective_override == InterpretationOverride(
        por=0.16, perm=0.16, prediction_model="prediction-v2"
    )
    assert plan.changed_fields == ("prediction_model",)
    assert actions(plan) == (
        PlanAction.REUSE,
        PlanAction.REUSE,
        PlanAction.RUN,
        PlanAction.RUN,
    )
    assert len(await repository.list_executions(request.task_id)) == 2
    task = await repository.get_task(request.task_id)
    assert task is not None
    assert task.current_execution_id == failed.workflow_execution_id


async def test_service_without_successful_execution_plans_all_run(data_dir):
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    request = TaskRequest(well_id=fixture.well.well_id)
    repository = InMemoryTaskRepository()
    state = InterpretationState(task=request)
    await repository.create_task(state)
    selected = await repository.create_input_version(request.task_id, fixture)
    await repository.create_execution(state, "INITIAL", input_version_id=selected.input_version_id)
    state.status = StepStatus.FAILED
    await repository.save(state, "failed report")
    app = build_application(
        AppSettings(mock_data_dir=data_dir, _env_file=None), task_repository=repository
    )
    plan = await app.plan_rerun(request)
    assert actions(plan) == (PlanAction.RUN,) * 4
    assert plan.source_execution_id is None
    assert len(await repository.list_executions(request.task_id)) == 1
