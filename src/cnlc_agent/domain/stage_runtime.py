"""把现有执行边界记录为 StageRun；不重新编排 W01～W10 或报告。"""

from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import WorkflowError
from cnlc_agent.domain.models import ErrorDetail
from cnlc_agent.domain.stages import (
    DEPENDENCIES,
    OUTPUT_KEY,
    STAGE_STEPS,
    InterpretationStage,
    StageRun,
    StageRunStatus,
    StageValidity,
    latest_runs,
    validate_dependencies,
)
from cnlc_agent.domain.state import InterpretationState


def _dependency_refs(
    state: InterpretationState, stage: InterpretationStage, report_config_ref: str | None
) -> dict[str, str]:
    """旧快照没有 StageRun 时，只从已校验复用步骤生成逻辑字段引用。"""

    current = latest_runs(state.stage_runs, state.task.task_id)
    refs = {}
    legacy_dependencies = []
    for dependency in DEPENDENCIES[stage]:
        run = current.get(dependency)
        if run is not None:
            if run.status != StageRunStatus.CONFIRMED or run.validity != StageValidity.CURRENT:
                raise WorkflowError("STAGE_DEPENDENCY_UNAVAILABLE", "前置阶段未确认或已失效")
            refs[OUTPUT_KEY[dependency]] = run.output_refs[OUTPUT_KEY[dependency]]
        else:
            steps = STAGE_STEPS[dependency]
            reused = [item for item in state.reused_steps if item.step_id in steps]
            if len(reused) != len(steps) or any(
                item.source_status not in {StepStatus.SUCCESS, StepStatus.WARNING}
                for item in reused
            ):
                raise WorkflowError("STAGE_DEPENDENCY_UNAVAILABLE", "旧快照缺少有效复用前缀")
            refs[OUTPUT_KEY[dependency]] = (
                f"execution:{reused[-1].source_execution_id}:{dependency.value}"
            )
            legacy_dependencies.append(dependency)
    if stage == InterpretationStage.PREPROCESS:
        refs["preprocess_parameter_revision_id"] = (
            f"execution:{state.workflow_execution_id}:parameters"
        )
    elif stage == InterpretationStage.INTERPRET:
        refs["parameter_revision_id"] = f"execution:{state.workflow_execution_id}:parameters"
    elif stage == InterpretationStage.REPORT:
        if not report_config_ref:
            raise WorkflowError("STAGE_DEPENDENCY_UNAVAILABLE", "报告缺少配置引用")
        refs["report_config_revision_id"] = report_config_ref
    elif stage == InterpretationStage.DECODE and state.input_version_id is not None:
        refs["input_version_id"] = state.input_version_id
    # 无历史契约的迁移分支仍逐项校验有记录的依赖；不伪造历史运行。
    if not legacy_dependencies:
        validate_dependencies(
            state.stage_runs, task_id=state.task.task_id, stage=stage, input_refs=refs
        )
    return refs


def begin_stage(
    state: InterpretationState, stage: InterpretationStage, *, report_config_ref: str | None = None
) -> None:
    """真实执行开始时追加独立运行，复用结果不产生新的运行记录。"""

    if any(
        run.execution_id == state.workflow_execution_id and run.stage == stage
        for run in state.stage_runs
    ):
        raise WorkflowError("INVALID_STAGE_TRANSITION", "同一次执行不能重复启动同一阶段")
    run = StageRun(
        task_id=state.task.task_id,
        execution_id=state.workflow_execution_id,
        stage=stage,
        input_refs=_dependency_refs(state, stage, report_config_ref),
    )
    state.stage_runs.append(
        run.transition(StageRunStatus.RUNNING, actor="system", reason="现有执行链进入阶段边界")
    )


def finish_stage(
    state: InterpretationState,
    stage: InterpretationStage,
    *,
    error: ErrorDetail | None = None,
) -> None:
    """Demo 自动确认完成阶段；阻断和复核保留原步骤状态，并记录阶段未成功。"""

    index = next(
        i
        for i, run in enumerate(state.stage_runs)
        if run.execution_id == state.workflow_execution_id and run.stage == stage
    )
    run = state.stage_runs[index]
    steps = [item for item in state.executions if item.step_id in STAGE_STEPS[stage]]
    warnings = [warning for item in steps for warning in item.warnings]
    errors = [item for step in steps for item in step.errors]
    if error is not None:
        errors.append(error)
    if (
        error is not None
        or tuple(item.step_id for item in steps) != STAGE_STEPS[stage]
        or any(item.status not in {StepStatus.SUCCESS, StepStatus.WARNING} for item in steps)
    ):
        errors = errors or [
            ErrorDetail(
                code="STAGE_NOT_COMPLETED",
                message="阶段未完整完成或需要复核",
                step_id=state.current_step,
            )
        ]
        updated = run.transition(
            StageRunStatus.FAILED,
            actor="system",
            reason="阶段未成功完成",
            warnings=warnings,
            errors=errors,
        )
    else:
        # 尚未建设 Revision 实体；引用指向包含结果的不可变 Execution 快照字段。
        output = {OUTPUT_KEY[stage]: f"execution:{state.workflow_execution_id}:{stage.value}"}
        updated = run.transition(
            StageRunStatus.CONFIRMED,
            actor="system",
            reason="自动确认阶段结果",
            output_refs=output,
            warnings=warnings,
            summary=f"{stage.value} 阶段完成",
        )
    state.stage_runs[index] = updated


def fail_running_stages(state: InterpretationState, error: ErrorDetail) -> None:
    """基础设施或报告异常终止时，同步结束本次仍在运行的阶段。"""

    for run in list(state.stage_runs):
        if run.execution_id == state.workflow_execution_id and run.status == StageRunStatus.RUNNING:
            finish_stage(state, run.stage, error=error)
