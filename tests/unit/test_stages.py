"""四阶段契约：状态转换、依赖版本和修改后的历史保留。"""

import pytest
from pydantic import ValidationError

from cnlc_agent.application.planning import ExecutionStage
from cnlc_agent.domain.errors import WorkflowError
from cnlc_agent.domain.models import ErrorDetail
from cnlc_agent.domain.stages import (
    OUTPUT_KEY,
    STAGE_ORDER,
    InterpretationStage,
    StageImpact,
    StageRun,
    StageRunStatus,
    StageValidity,
    confirm_stage,
    invalidate_from,
    validate_dependencies,
)


def running(stage=InterpretationStage.DECODE, task_id="task"):
    return StageRun(task_id=task_id, execution_id="execution-1", stage=stage).transition(
        StageRunStatus.RUNNING, actor="system", reason="开始执行"
    )


def success(
    stage=InterpretationStage.DECODE,
    status=StageRunStatus.CONFIRMED,
    task_id="task",
):
    run = running(stage, task_id)
    if stage == InterpretationStage.PREPROCESS:
        run.input_refs["dataset_revision_id"] = "result:DATA_DECODE"
    return run.transition(
        status,
        actor="system",
        reason="阶段完成",
        output_refs={OUTPUT_KEY[stage]: f"result:{stage.value}"},
        summary="结果摘要",
        warnings=["保留诊断告警"],
    )


def test_shared_stage_enum_preserves_demo_wire_contract():
    assert InterpretationStage is ExecutionStage
    assert InterpretationStage.DECODE == ExecutionStage.DATA_DECODE
    assert InterpretationStage.DECODE.value == "DATA_DECODE"
    assert len(STAGE_ORDER) == 4


def test_stage_run_requires_execution_and_keeps_only_logical_references():
    with pytest.raises(ValidationError):
        StageRun(task_id="task", stage=InterpretationStage.DECODE)
    run = StageRun(
        task_id="task",
        execution_id="execution-1",
        stage=InterpretationStage.DECODE,
        input_refs={"input_version_id": "input-1"},
        applied_change_ids=["change-1"],
    )
    assert run.execution_id == "execution-1"
    assert run.input_refs == {"input_version_id": "input-1"}
    assert run.applied_change_ids == ["change-1"]
    with pytest.raises(ValidationError):
        StageRun(
            task_id="task",
            execution_id="execution-1",
            stage=InterpretationStage.DECODE,
            input_refs={"dataset_revision_id": "/tmp/curve.json"},
        )


def test_manual_confirmation_and_audit():
    pending = StageRun(task_id="task", execution_id="execution-1", stage=InterpretationStage.DECODE)
    started = pending.transition(StageRunStatus.RUNNING, actor="system", reason="开始执行")
    waiting = started.transition(
        StageRunStatus.WAITING_CONFIRM,
        actor="system",
        reason="等待审核",
        output_refs={"dataset_revision_id": "dataset:1"},
        summary="成功",
    )
    confirmed = confirm_stage(waiting, actor="user")
    assert confirmed.status == StageRunStatus.CONFIRMED
    assert confirmed.confirmed_by == "user"
    assert confirmed.started_at <= confirmed.finished_at <= confirmed.confirmed_at
    assert [event.after for event in confirmed.transitions] == [
        StageRunStatus.RUNNING,
        StageRunStatus.WAITING_CONFIRM,
        StageRunStatus.CONFIRMED,
    ]
    assert waiting.status == StageRunStatus.WAITING_CONFIRM
    assert pending.started_at is None
    assert confirmed.output_refs == waiting.output_refs
    assert StageRun.model_validate_json(confirmed.model_dump_json()) == confirmed


def test_auto_confirmation():
    run = success()
    assert run.confirmed_by == "system"
    assert [event.after for event in run.transitions] == [
        StageRunStatus.RUNNING,
        StageRunStatus.CONFIRMED,
    ]


def test_failure_retains_structured_error():
    error = ErrorDetail(code="TOOL_TIMEOUT", message="工具超时", retryable=True)
    run = running().transition(StageRunStatus.FAILED, actor="system", reason="失败", errors=[error])
    assert run.errors == [error]
    assert run.finished_at >= run.started_at
    assert run.confirmed_at is None
    with pytest.raises(WorkflowError):
        confirm_stage(run)
    with pytest.raises(WorkflowError):
        run.transition(StageRunStatus.RUNNING, actor="system", reason="非法复活")


@pytest.mark.parametrize(
    "impact,affected",
    [
        (StageImpact.DATASET_PATCH, STAGE_ORDER[1:]),
        (StageImpact.PREPROCESS_PARAMETER_CHANGE, STAGE_ORDER[1:]),
        (StageImpact.INTERPRETATION_PARAMETER_CHANGE, STAGE_ORDER[2:]),
        (StageImpact.INTERPRETATION_OVERRIDE, STAGE_ORDER[2:]),
        (StageImpact.REPORT_CONFIG_CHANGE, STAGE_ORDER[3:]),
    ],
)
def test_impact_matrix_preserves_history(impact, affected):
    history = [success(stage) for stage in STAGE_ORDER]
    original = [run.model_dump_json() for run in history]
    invalidated = invalidate_from(
        history,
        task_id="task",
        impact=impact,
        actor="user",
        reason="修改输入",
        change_id="change-1",
    )
    assert len(invalidated) == len(history)
    assert [run.model_dump_json() for run in history] == original
    for before, after in zip(history, invalidated, strict=True):
        assert before.id == after.id
        assert before.output_refs == after.output_refs
        assert before.input_refs == after.input_refs
        assert before.warnings == after.warnings
        assert before.confirmed_at == after.confirmed_at
        assert after.status == StageRunStatus.CONFIRMED
        assert after.validity == (
            StageValidity.STALE if before.stage in affected else StageValidity.CURRENT
        )
        if before.stage in affected:
            assert after.stale_at is not None
            assert after.stale_reason == "修改输入"
            assert after.invalidated_by_change_id == "change-1"
    assert (
        invalidate_from(invalidated, task_id="task", impact=impact, actor="user", reason="再次修改")
        == invalidated
    )


def test_waiting_result_can_be_invalidated_but_not_confirmed_afterwards():
    waiting = success(InterpretationStage.REPORT, StageRunStatus.WAITING_CONFIRM)
    stale = invalidate_from(
        [waiting],
        task_id="task",
        impact=StageImpact.REPORT_CONFIG_CHANGE,
        actor="user",
        reason="换模板",
    )[0]
    assert stale.status == StageRunStatus.WAITING_CONFIRM
    assert stale.validity == StageValidity.STALE
    assert stale.confirmed_at is None
    with pytest.raises(WorkflowError):
        confirm_stage(stale)


def test_only_latest_run_of_selected_task_is_invalidated():
    old = success(InterpretationStage.REPORT)
    new = success(InterpretationStage.REPORT)
    other = success(InterpretationStage.REPORT, task_id="other")
    result = invalidate_from(
        [old, new, other],
        task_id="task",
        impact=StageImpact.REPORT_CONFIG_CHANGE,
        actor="user",
        reason="修改",
    )
    assert [run.validity for run in result] == [
        StageValidity.CURRENT,
        StageValidity.STALE,
        StageValidity.CURRENT,
    ]
    assert len({run.id for run in result}) == 3


def test_changes_during_affected_run_are_rejected_atomically():
    history = [success(InterpretationStage.DECODE), running(InterpretationStage.PREPROCESS)]
    with pytest.raises(WorkflowError) as error:
        invalidate_from(
            history,
            task_id="task",
            impact=StageImpact.DATASET_PATCH,
            actor="user",
            reason="修改曲线",
        )
    assert error.value.code == "STAGE_CHANGE_DURING_RUN"
    assert history[1].status == StageRunStatus.RUNNING


@pytest.mark.parametrize("new_status", [StageRunStatus.FAILED, StageRunStatus.CONFIRMED])
def test_pending_cannot_skip_running(new_status):
    run = StageRun(task_id="task", execution_id="execution-1", stage=InterpretationStage.DECODE)
    with pytest.raises(WorkflowError):
        run.transition(new_status, actor="user", reason="非法转换")


def test_completion_requires_output_and_failure_requires_error():
    run = running()
    with pytest.raises(ValidationError):
        run.transition(StageRunStatus.CONFIRMED, actor="system", reason="缺少输出")
    with pytest.raises(ValidationError):
        run.transition(StageRunStatus.FAILED, actor="system", reason="缺少错误")
    with pytest.raises(ValidationError):
        run.transition(
            StageRunStatus.CONFIRMED,
            actor="",
            reason="无确认者",
            output_refs={"dataset_revision_id": "dataset:1"},
        )


def interpretation_refs():
    return {
        "dataset_revision_id": "result:DATA_DECODE",
        "preprocess_revision_id": "result:PREPROCESS",
        "parameter_revision_id": "params:1",
    }


def test_dependency_validation_allows_local_interpretation_run():
    history = [success(stage) for stage in STAGE_ORDER[:2]]
    validate_dependencies(
        history,
        task_id="task",
        stage=InterpretationStage.INTERPRET,
        input_refs=interpretation_refs(),
    )
    validate_dependencies(
        [success(InterpretationStage.INTERPRET)],
        task_id="task",
        stage=InterpretationStage.REPORT,
        input_refs={
            "interpretation_revision_id": "result:INTERPRET",
            "report_config_revision_id": "config:1",
        },
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "waiting",
        "stale",
        "wrong_task",
        "wrong_ref",
        "missing_parameters",
        "new_failed_run",
    ],
)
def test_dependency_validation_rejects_unavailable_or_mismatched_inputs(mutation):
    history = [success(stage) for stage in STAGE_ORDER[:2]]
    refs = interpretation_refs()
    if mutation == "missing":
        history.pop()
    elif mutation == "waiting":
        history[-1] = success(InterpretationStage.PREPROCESS, StageRunStatus.WAITING_CONFIRM)
    elif mutation == "stale":
        history = invalidate_from(
            history, task_id="task", impact=StageImpact.DATASET_PATCH, actor="user", reason="修改"
        )
    elif mutation == "wrong_task":
        history[-1] = success(InterpretationStage.PREPROCESS, task_id="other")
    elif mutation == "wrong_ref":
        refs["preprocess_revision_id"] = "old:preprocess"
    elif mutation == "missing_parameters":
        refs.pop("parameter_revision_id")
    else:
        history.append(
            running(InterpretationStage.PREPROCESS).transition(
                StageRunStatus.FAILED,
                actor="system",
                reason="失败",
                errors=[ErrorDetail(code="TOOL_ERROR", message="失败")],
            )
        )
    with pytest.raises(WorkflowError) as error:
        validate_dependencies(
            history, task_id="task", stage=InterpretationStage.INTERPRET, input_refs=refs
        )
    assert error.value.code == "STAGE_DEPENDENCY_UNAVAILABLE"


def test_patched_dataset_reference_does_not_require_decode_rerun():
    decode = success()
    validate_dependencies(
        [decode],
        task_id="task",
        stage=InterpretationStage.PREPROCESS,
        input_refs={"dataset_revision_id": "dataset:patched"},
        dataset_revision_id="dataset:patched",
    )
    assert decode.status == StageRunStatus.CONFIRMED


def test_patched_dataset_cannot_consume_preprocess_from_previous_revision():
    history = [success(stage) for stage in STAGE_ORDER[:2]]
    refs = interpretation_refs()
    refs["dataset_revision_id"] = "dataset:patched"
    with pytest.raises(WorkflowError):
        validate_dependencies(
            history,
            task_id="task",
            stage=InterpretationStage.INTERPRET,
            input_refs=refs,
            dataset_revision_id="dataset:patched",
        )
    patched = success(InterpretationStage.PREPROCESS)
    patched.input_refs["dataset_revision_id"] = "dataset:patched"
    history.append(patched)
    validate_dependencies(
        history,
        task_id="task",
        stage=InterpretationStage.INTERPRET,
        input_refs=refs,
        dataset_revision_id="dataset:patched",
    )
    assert history[0].status == StageRunStatus.CONFIRMED
