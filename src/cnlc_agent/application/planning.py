"""确定性执行规划；只描述四阶段 RUN/REUSE，不驱动 Workflow。"""

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import DataError, WorkflowError
from cnlc_agent.domain.execution import Execution
from cnlc_agent.domain.inputs import InterpretationInputVersion
from cnlc_agent.domain.models import Contract
from cnlc_agent.domain.override import InterpretationOverride


class ExecutionStage(StrEnum):
    """Execution 视图的四个规划阶段，不取代 W01～W10。"""

    DATA_DECODE = "DATA_DECODE"
    PREPROCESS = "PREPROCESS"
    INTERPRET = "INTERPRET"
    REPORT = "REPORT"


class PlanAction(StrEnum):
    """计划动作，与现有 StepStatus 生命周期状态分离。"""

    RUN = "RUN"
    REUSE = "REUSE"


STAGE_ORDER = tuple(ExecutionStage)
STAGE_STEPS: dict[ExecutionStage, tuple[StepId, ...]] = {
    ExecutionStage.DATA_DECODE: (StepId.W01,),
    ExecutionStage.PREPROCESS: (StepId.W02, StepId.W03),
    ExecutionStage.INTERPRET: (
        StepId.W04,
        StepId.W05,
        StepId.W06,
        StepId.W07,
        StepId.W08,
        StepId.W09,
        StepId.W10,
    ),
    # REPORT 由现有 ReportAssembler / ReportGenerator 在十步完成后承担。
    ExecutionStage.REPORT: (),
}

# 唯一的参数依赖事实表；新 Override 字段缺少映射时必须显式报错。
DEPENDENCY_IMPACT: dict[str, ExecutionStage] = {
    "sampling_interval": ExecutionStage.PREPROCESS,
    "por": ExecutionStage.INTERPRET,
    "perm": ExecutionStage.INTERPRET,
    "prediction_model": ExecutionStage.INTERPRET,
}


class StagePlan(Contract):
    """一个上层阶段的计划动作及其现有业务步骤映射。"""

    stage: ExecutionStage
    action: PlanAction
    steps: tuple[StepId, ...]


class ExecutionPlan(Contract):
    """可审查的只读规划结果；执行边界重新校验后创建独立 Execution。"""

    task_id: str = Field(min_length=1)
    source_execution_id: str | None = None
    # 在实际执行边界核对来源绑定，避免读取到与计划不一致的输入版本。
    source_input_version_id: str | None = None
    selected_input_version_id: str | None = None
    effective_override: InterpretationOverride
    changed_fields: tuple[str, ...]
    stage_plans: tuple[StagePlan, ...]
    planning_reason: Literal[
        "FULL_RERUN", "NO_REUSABLE_SOURCE", "INPUT_CHANGED", "OVERRIDE_CHANGED", "REPORT_ONLY"
    ]

    @model_validator(mode="after")
    def validate_stages(self) -> "ExecutionPlan":
        """防止计划遗漏阶段，或错误复用完整报告。"""

        if tuple(item.stage for item in self.stage_plans) != STAGE_ORDER:
            raise ValueError("stage plans must contain four stages in business order")
        if any(item.steps != STAGE_STEPS[item.stage] for item in self.stage_plans):
            raise ValueError("stage-to-step mapping must remain fixed")
        if self.stage_plans[-1].action != PlanAction.RUN:
            raise ValueError("report must run for every new execution")
        actions = [item.action for item in self.stage_plans]
        if PlanAction.REUSE in actions[actions.index(PlanAction.RUN) :]:
            raise ValueError("reused stages must form a contiguous prefix")
        return self

    def action_for(self, stage: ExecutionStage) -> PlanAction:
        """按四阶段枚举读取动作，便于调用方避免依赖列表下标。"""

        return next(item.action for item in self.stage_plans if item.stage == stage)


class DependencyResolver:
    """按可信输入摘要和字段影响范围生成确定性四阶段计划。"""

    def plan(
        self,
        *,
        task_id: str,
        selected_input: InterpretationInputVersion | None,
        source_execution: Execution | None,
        source_input: InterpretationInputVersion | None,
        current_execution: Execution | None = None,
        requested_changes: InterpretationOverride | None = None,
        force_full_rerun: bool = False,
    ) -> ExecutionPlan:
        """本轮变化相对当前配置；可复用性相对最近成功执行重新判定。"""

        if selected_input is not None and selected_input.task_id != task_id:
            raise DataError("INPUT_VERSION_TASK_MISMATCH", "选中输入不属于此任务")
        if current_execution is not None and current_execution.task_id != task_id:
            raise DataError("EXECUTION_TASK_MISMATCH", "当前执行不属于此任务")
        if source_execution is not None and source_execution.task_id != task_id:
            raise DataError("EXECUTION_TASK_MISMATCH", "复用来源不属于此任务")

        baseline = (
            current_execution.override_snapshot
            if current_execution is not None
            else source_execution.override_snapshot
            if source_execution is not None
            else InterpretationOverride()
        )
        changes = requested_changes or InterpretationOverride()
        merged = baseline.model_dump(mode="python")
        merged.update(changes.model_dump(exclude_none=True, mode="python"))
        effective = InterpretationOverride.model_validate(merged)
        for snapshot in (baseline, changes, effective):
            unknown = set(snapshot.model_dump(exclude_none=True)) - DEPENDENCY_IMPACT.keys()
            if unknown:
                raise WorkflowError(
                    "UNSUPPORTED_DEPENDENCY_CHANGE", "解释参数缺少明确的阶段依赖规则"
                )

        changed = [
            field
            for field in DEPENDENCY_IMPACT
            if getattr(baseline, field) != getattr(effective, field)
        ]
        reliable_source = (
            source_execution is not None
            and source_execution.status in {StepStatus.SUCCESS, StepStatus.WARNING}
            and bool(source_execution.markdown)
            and source_input is not None
            and source_input.task_id == task_id
            and source_input.input_version_id == source_execution.input_version_id
        )
        source = source_execution if reliable_source else None
        if source is not None:
            unknown_source = (
                set(source.override_snapshot.model_dump(exclude_none=True))
                - DEPENDENCY_IMPACT.keys()
            )
            if unknown_source:
                raise WorkflowError(
                    "UNSUPPORTED_DEPENDENCY_CHANGE", "复用来源参数缺少明确的阶段依赖规则"
                )
        input_changed = (
            source is not None
            and selected_input is not None
            and source_input is not None
            and source_input.content_sha256 != selected_input.content_sha256
        )
        if input_changed:
            changed.insert(0, "input_content")

        if force_full_rerun:
            first_run = 0
            reason = "FULL_RERUN"
        elif source is None or selected_input is None:
            first_run = 0
            reason = "NO_REUSABLE_SOURCE"
        elif input_changed:
            first_run = 0
            reason = "INPUT_CHANGED"
        else:
            reuse_fields = [
                field
                for field in DEPENDENCY_IMPACT
                if getattr(source.override_snapshot, field) != getattr(effective, field)
            ]
            if reuse_fields:
                first_run = min(
                    STAGE_ORDER.index(DEPENDENCY_IMPACT[field]) for field in reuse_fields
                )
                reason = "OVERRIDE_CHANGED"
            elif changed:
                # 当前失败版本的配置被改回成功来源配置时，只需形成新报告版本。
                first_run = STAGE_ORDER.index(ExecutionStage.REPORT)
                reason = "REPORT_ONLY"
            else:
                raise DataError("NO_EFFECTIVE_CHANGE", "输入与解释参数均无有效变化")

        stage_plans = tuple(
            StagePlan(
                stage=stage,
                action=PlanAction.RUN if index >= first_run else PlanAction.REUSE,
                steps=STAGE_STEPS[stage],
            )
            for index, stage in enumerate(STAGE_ORDER)
        )
        return ExecutionPlan(
            task_id=task_id,
            source_execution_id=source.execution_id if source is not None else None,
            source_input_version_id=source.input_version_id if source is not None else None,
            selected_input_version_id=(
                selected_input.input_version_id if selected_input is not None else None
            ),
            effective_override=effective,
            changed_fields=tuple(changed),
            stage_plans=stage_plans,
            planning_reason=reason,  # type: ignore[arg-type]
        )
