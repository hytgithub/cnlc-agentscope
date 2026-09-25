"""把可信来源的业务字段装配到全新状态；不复制历史运行快照。"""

from typing import Literal

from cnlc_agent.application.planning import (
    DEPENDENCY_IMPACT,
    STAGE_ORDER,
    ExecutionPlan,
    ExecutionStage,
    PlanAction,
)
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import WorkflowError
from cnlc_agent.domain.execution import Execution
from cnlc_agent.domain.inputs import InterpretationInputVersion
from cnlc_agent.domain.models import StageResult, TaskRequest
from cnlc_agent.domain.state import InterpretationState, ReusedStep, StateChange

REUSE_FIELDS = {
    ExecutionStage.DATA_DECODE: ("well", "raw_data", "data_requirements"),
    ExecutionStage.PREPROCESS: ("processed_data", "qc_result"),
    ExecutionStage.INTERPRET: (
        "lithology_result",
        "petrophysics_result",
        "fluid_result",
        "layer_classification",
        "interval_result",
        "validation_result",
        "final_check",
    ),
}
VALID_SOURCE_STATUSES = {StepStatus.SUCCESS, StepStatus.WARNING}


def planned_start_step(plan: ExecutionPlan) -> StepId | None:
    """固定阶段映射中的第一个 RUN 步骤；None 表示仅生成报告。"""

    return next(
        (
            item.steps[0]
            for item in plan.stage_plans
            if item.action == PlanAction.RUN and item.steps
        ),
        None,
    )


class StateReuseAssembler:
    """执行边界校验来源、输入及复用范围，只装配白名单业务字段。"""

    def assemble(
        self,
        plan: ExecutionPlan,
        source: Execution | None,
        request: TaskRequest,
        *,
        selected_input: InterpretationInputVersion | None,
        source_input: InterpretationInputVersion | None,
        mode: Literal["mock", "demo"] = "mock",
    ) -> InterpretationState:
        """新 ID、时间与运行记录由默认工厂产生；失败时不自动扩大执行范围。"""

        # Pydantic 对容器内修改不触发赋值验证，进入执行边界时重新校验契约。
        plan = ExecutionPlan.model_validate(plan.model_dump(mode="python"))
        if plan.task_id != request.task_id:
            raise WorkflowError("PLAN_TASK_MISMATCH", "执行计划不属于当前任务")
        if plan.selected_input_version_id != (
            selected_input.input_version_id if selected_input is not None else None
        ):
            raise WorkflowError("PLAN_INPUT_MISMATCH", "选中输入与计划不一致")
        if selected_input is not None and (
            selected_input.task_id != request.task_id or selected_input.well_id != request.well_id
        ):
            raise WorkflowError("INPUT_VERSION_TASK_MISMATCH", "输入版本不属于当前任务或井")

        state = InterpretationState(
            task=request.model_copy(deep=True),
            mode=mode,
            input_version_id=plan.selected_input_version_id,
            effective_override=plan.effective_override.model_copy(deep=True),
        )
        reused = [item for item in plan.stage_plans if item.action == PlanAction.REUSE]
        if not reused:
            return state
        if source is None or source.execution_id != plan.source_execution_id:
            raise WorkflowError("REUSE_SOURCE_NOT_FOUND", "计划指定的复用来源不存在")
        if (
            source.task_id != request.task_id
            or source.state_snapshot.task.well_id != request.well_id
        ):
            raise WorkflowError("REUSE_SOURCE_TASK_MISMATCH", "复用来源不属于当前任务或井")
        if (
            source.status not in VALID_SOURCE_STATUSES
            or source.state_snapshot.status not in VALID_SOURCE_STATUSES
        ):
            raise WorkflowError("REUSE_SOURCE_INVALID_STATUS", "只能复用成功或告警执行")
        if source.state_snapshot.mode != mode:
            raise WorkflowError("REUSE_SOURCE_MODE_MISMATCH", "不同执行模式的业务校验不可混用")
        if (
            source.input_version_id != plan.source_input_version_id
            or source_input is None
            or source_input.input_version_id != source.input_version_id
            or source_input.task_id != request.task_id
            or selected_input is None
            or source_input.content_sha256 != selected_input.content_sha256
        ):
            raise WorkflowError("REUSE_SOURCE_INPUT_MISMATCH", "来源输入绑定或内容摘要不匹配")

        # 防止外部调用方篡改计划，将已经失效的阶段标成 REUSE。
        for field, stage in DEPENDENCY_IMPACT.items():
            if getattr(source.override_snapshot, field) != getattr(plan.effective_override, field):
                if any(
                    STAGE_ORDER.index(item.stage) >= STAGE_ORDER.index(stage) for item in reused
                ):
                    raise WorkflowError("REUSE_PLAN_CONFLICT", "计划复用了参数变化已影响的阶段")

        source_state = source.state_snapshot
        prefix = [step for item in reused for step in item.steps]
        if source_state.completed_steps[: len(prefix)] != prefix:
            raise WorkflowError("REUSE_SOURCE_INCOMPLETE", "来源缺少连续有效业务前缀")
        for item in reused:
            for field in REUSE_FIELDS[item.stage]:
                value = getattr(source_state, field)
                if value is None or (
                    isinstance(value, StageResult) and value.status not in VALID_SOURCE_STATUSES
                ):
                    raise WorkflowError("REUSE_SOURCE_INCOMPLETE", f"来源业务字段无效：{field}")
                setattr(state, field, value.model_copy(deep=True))
        if state.final_check is not None and (
            state.final_check.result.get("structural_check_passed") is not True
        ):
            raise WorkflowError("REUSE_SOURCE_INCOMPLETE", "来源最终结构检查未通过")

        for step in prefix:
            # 来源也可能是局部重跑；按其有效链记录逐步追溯，不能只查 executions。
            actual = [item for item in source_state.executions if item.step_id == step]
            inherited = [item for item in source_state.reused_steps if item.step_id == step]
            if len(actual) + len(inherited) != 1:
                raise WorkflowError("REUSE_SOURCE_INCOMPLETE", "来源步骤缺少唯一执行或复用记录")
            status = actual[0].status if actual else inherited[0].source_status
            warnings = actual[0].warnings if actual else inherited[0].warnings
            if status not in VALID_SOURCE_STATUSES:
                raise WorkflowError("REUSE_SOURCE_INCOMPLETE", "来源步骤不是有效完成状态")
            state.reused_steps.append(
                ReusedStep(
                    step_id=step,
                    source_execution_id=source.execution_id,
                    source_status=status,  # type: ignore[arg-type]
                    warnings=list(warnings),
                )
            )
            state.completed_steps.append(step)
            state.changes.append(
                StateChange(
                    actor="StateReuseAssembler",
                    step_id=step,
                    reason="按执行计划复用已校验的业务结果",
                    before={},
                    after={
                        "source_execution_id": source.execution_id,
                        "source_status": status.value,
                    },
                )
            )
        return state
