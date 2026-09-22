"""把完整解释状态投影为字段受控、适合 Web 展示的 Demo DTO。"""

from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.models import JsonObject, StageResult
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.workflows.interpretation_workflow import (
    STEP_OBSERVABILITY,
    compact_result,
    step_input_summary,
    summarize_for_observability,
)

STEP_RESULT_FIELDS: dict[StepId, str | None] = {
    StepId.W01: None,
    StepId.W02: None,
    StepId.W03: "qc_result",
    StepId.W04: "lithology_result",
    StepId.W05: "petrophysics_result",
    StepId.W06: "fluid_result",
    StepId.W07: "layer_classification",
    StepId.W08: "interval_result",
    StepId.W09: "validation_result",
    StepId.W10: "final_check",
}


class DemoStep(BaseModel):
    """单个 Workflow 步骤的安全精简视图，避免前端接触完整 State。"""

    model_config = ConfigDict(extra="forbid")

    id: StepId
    name: str
    status: StepStatus = StepStatus.PENDING
    source: str = "workflow"
    input_summary: JsonObject = Field(default_factory=dict)
    output_summary: JsonObject = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _step_statuses(state: InterpretationState) -> dict[StepId, StepStatus]:
    statuses = {step_id: StepStatus.PENDING for step_id in StepId}
    statuses.update({execution.step_id: execution.status for execution in state.executions})
    return statuses


def _source(step_id: StepId, result: object) -> str:
    """根据执行边界标记结果来源，供前端区分 Tool、模型和 Demo Skip。"""

    if step_id == StepId.W09 and result is not None:
        result_data = getattr(result, "result", {})
        if isinstance(result_data, dict) and result_data.get("demo_skipped") is True:
            return "demo-skip"
    if step_id in {StepId.W06, StepId.W07}:
        return "qwen-plus"
    if step_id in {StepId.W01, StepId.W03, StepId.W04, StepId.W05, StepId.W08}:
        return "mock" if result is not None and getattr(result, "is_mock", False) else "tool"
    return "workflow"


def _output_summary(
    state: InterpretationState,
    step_id: StepId,
    result: StageResult | None,
) -> JsonObject:
    """提取有限的输出摘要，禁止把完整原始数据和内部状态发送到前端。"""

    if result is not None:
        result_data = getattr(result, "result", {})
        if isinstance(result_data, dict):
            return {
                "summary": compact_result(result),
                "result": summarize_for_observability(result_data),
            }
    if step_id == StepId.W01:
        raw_data = state.raw_data
        return {
            "well_id": state.well.well_id if state.well else state.task.well_id,
            "curve_names": list(raw_data.curves) if raw_data else [],
            "depth_count": len(raw_data.depths) if raw_data else 0,
            "auxiliary_sources": list(raw_data.auxiliary) if raw_data else [],
        }
    if step_id == StepId.W02:
        requirements = state.data_requirements
        missing = [item.field for item in state.missing_data if item.affected_step == step_id]
        return {
            "required_curves": summarize_for_observability(
                requirements.required_curves if requirements else []
            ),
            "recommended_sources": summarize_for_observability(
                requirements.recommended_sources if requirements else []
            ),
            "missing_data": summarize_for_observability(missing),
        }
    if step_id == StepId.W10 and state.final_check is not None:
        return {"result": summarize_for_observability(state.final_check.result)}
    return {}


def present_steps(state: InterpretationState) -> list[DemoStep]:
    """按 W01-W10 顺序构造展示对象，不暴露完整 InterpretationState。"""

    statuses = _step_statuses(state)
    executions = {execution.step_id: execution for execution in state.executions}
    steps: list[DemoStep] = []
    for step_id in StepId:
        field = STEP_RESULT_FIELDS[step_id]
        result = cast(StageResult | None, getattr(state, field)) if field else None
        execution = executions.get(step_id)
        warnings = list(getattr(result, "warnings", [])) if result is not None else []
        if execution is not None:
            warnings.extend(execution.warnings)
        evidence = list(getattr(result, "evidence", [])) if result is not None else []
        steps.append(
            DemoStep(
                id=step_id,
                name=STEP_OBSERVABILITY[step_id][0],
                status=statuses[step_id],
                source=_source(step_id, result),
                input_summary={"summary": step_input_summary(state, step_id)},
                output_summary=_output_summary(state, step_id, result),
                evidence=evidence,
                warnings=warnings,
            )
        )
    return steps
