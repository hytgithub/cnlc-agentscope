"""W01–W10 handlers. Professional outputs come through injected tools/agents."""

from cnlc_agent.agents.interpretation_agent import InterpretationAgent
from cnlc_agent.agents.validation_agent import ValidationAgent
from cnlc_agent.application.ports import ModelRequest
from cnlc_agent.domain.enums import StepId, StepStatus, ValidationStatus
from cnlc_agent.domain.models import MissingData, StageResult, ValidationResult, WellData
from cnlc_agent.domain.state import InterpretationState, StatePatch, StepOutcome
from cnlc_agent.tools.contracts import Tool, ToolCaller, ToolInput
from cnlc_agent.workflows.node import WorkflowNode


def tool_request(state: InterpretationState, step: StepId) -> ToolInput:
    return ToolInput(
        task_id=state.task.task_id,
        trace_id=state.trace_id,
        well_id=state.task.well_id,
        step_id=step,
    )


def model_request(state: InterpretationState, purpose: str) -> ModelRequest:
    # Deliberately exclude execution history, task instructions and unrelated state.
    fields = {
        "well",
        "qc_result",
        "lithology_result",
        "petrophysics_result",
        "processed_data",
        "fluid_result",
        "layer_classification",
        "interval_result",
    }
    context = state.model_dump(mode="json", include=fields)
    context["response_contract"] = {
        "status": "SUCCESS or WARNING",
        "result": "JSON object containing the requested interpretation result",
        "evidence": "array of strings",
        "conflicts": "array of strings",
        "missing_evidence": "array of strings",
        "warnings": "array of strings",
        "recommended_action": "string",
        "is_mock": False,
        "source": "model:qwen-plus",
    }
    context["response_instruction"] = (
        "Return only one JSON object matching response_contract. Do not return markdown. "
        "Use only the supplied context and do not invent missing measurements."
    )
    context["execution_mode"] = state.mode
    return ModelRequest(
        task_id=state.task.task_id,
        trace_id=state.trace_id,
        well_id=state.task.well_id,
        purpose=purpose,
        context=context,
    )


def outcome_for(result: StageResult, patch: StatePatch) -> StepOutcome:
    status = result.status
    if status == StepStatus.SUCCESS and result.warnings:
        status = StepStatus.WARNING
    return StepOutcome(
        status=status,
        patch=patch,
        warnings=result.warnings,
        reason=result.source,
    )


def build_steps(
    tools: dict[str, Tool],
    caller: ToolCaller,
    interpretation: InterpretationAgent,
    validation: ValidationAgent,
    *,
    demo_mode: bool = False,
) -> list[WorkflowNode]:
    async def load(state: InterpretationState) -> StepOutcome:
        output = await caller.call(tools["get_well_data"], tool_request(state, StepId.W01))
        data = WellData.model_validate(output.data)
        return StepOutcome(
            patch=StatePatch(
                well=data.well, raw_data=data.raw_data, data_requirements=data.requirements
            ),
            reason="通过 get_well_data 加载 Mock 井资料",
        )

    async def completeness(state: InterpretationState) -> StepOutcome:
        assert state.raw_data is not None and state.data_requirements is not None
        if demo_mode:
            return StepOutcome(
                reason="Demo Mode：将演示井资料视为完整，继续执行 W03–W10",
            )
        raw = state.raw_data
        missing: list[MissingData] = []
        if not raw.depths:
            missing.append(
                MissingData(
                    field="raw_data.depths",
                    importance="Required",
                    affected_step=StepId.W03,
                )
            )
        for name in state.data_requirements.required_curves:
            curve = raw.curves.get(name)
            if curve is None or not any(value is not None for value in curve.values):
                missing.append(
                    MissingData(
                        field=f"raw_data.curves.{name}",
                        importance="Required",
                        affected_step=StepId.W03,
                    )
                )
        for name in state.data_requirements.recommended_sources:
            if not raw.auxiliary.get(name):
                missing.append(
                    MissingData(
                        field=f"raw_data.auxiliary.{name}",
                        importance="Recommended",
                        affected_step=StepId.W09,
                    )
                )
        warnings = [
            f"缺少推荐资料：{item.field}" for item in missing if item.importance == "Recommended"
        ]
        return StepOutcome(
            status=StepStatus.BLOCKED
            if any(item.importance == "Required" for item in missing)
            else StepStatus.WARNING
            if warnings
            else StepStatus.SUCCESS,
            missing_data=missing,
            warnings=warnings,
            reason="检查当前 Mock Fixture 声明的数据要求（正式业务字段表待确认）",
        )

    async def qc(state: InterpretationState) -> StepOutcome:
        output = await caller.call(tools["check_curve_quality"], tool_request(state, StepId.W03))
        result = StageResult.model_validate(output.data)
        return outcome_for(result, StatePatch(qc_result=result, processed_data=state.raw_data))

    async def lithology(state: InterpretationState) -> StepOutcome:
        output = await caller.call(tools["identify_lithology"], tool_request(state, StepId.W04))
        result = StageResult.model_validate(output.data)
        return outcome_for(result, StatePatch(lithology_result=result))

    async def petrophysics(state: InterpretationState) -> StepOutcome:
        output = await caller.call(tools["evaluate_petrophysics"], tool_request(state, StepId.W05))
        result = StageResult.model_validate(output.data)
        return outcome_for(result, StatePatch(petrophysics_result=result))

    async def fluid(state: InterpretationState) -> StepOutcome:
        result = await interpretation.run(model_request(state, "fluid"))
        return outcome_for(result, StatePatch(fluid_result=result))

    async def classification(state: InterpretationState) -> StepOutcome:
        result = await interpretation.run(model_request(state, "classification"))
        return outcome_for(result, StatePatch(layer_classification=result))

    async def intervals(state: InterpretationState) -> StepOutcome:
        output = await caller.call(tools["merge_intervals"], tool_request(state, StepId.W08))
        result = StageResult.model_validate(output.data)
        return outcome_for(result, StatePatch(interval_result=result))

    async def validate(state: InterpretationState) -> StepOutcome:
        if demo_mode:
            result = ValidationResult(
                status=StepStatus.SUCCESS,
                result={"demo_skipped": True, "summary": "Demo Mode 跳过真实综合验证"},
                evidence=["Demo Mode：保留结构化验证占位结果"],
                validation_status=ValidationStatus.CONSISTENT,
                is_mock=True,
                source="demo:w09-skipped-validation",
            )
            return outcome_for(result, StatePatch(validation_result=result))
        result = await validation.run(model_request(state, "validation"))
        outcome = outcome_for(result, StatePatch(validation_result=result))
        if outcome.status in {StepStatus.SUCCESS, StepStatus.WARNING}:
            if result.validation_status in {
                ValidationStatus.SERIOUS_CONFLICT,
                ValidationStatus.INSUFFICIENT_EVIDENCE,
            }:
                outcome.status = StepStatus.REVIEW_REQUIRED
                outcome.reason = "验证冲突或证据不足；自动回退尚未实现，停止并等待复核"
            elif result.validation_status == ValidationStatus.PARTIAL_CONFLICT:
                outcome.status = StepStatus.WARNING
                outcome.warnings.append("综合验证存在部分冲突，请查看结构化证据")
        return outcome

    async def final_check(state: InterpretationState) -> StepOutcome:
        # Structural consistency only, not professional interpretation validation.
        valid = (
            state.completed_steps == list(StepId)[:-1]
            and not state.errors
            and not state.review_required
            and not any(item.importance == "Required" for item in state.missing_data)
        )
        result = StageResult(
            status=StepStatus.SUCCESS if valid else StepStatus.REVIEW_REQUIRED,
            result={"structural_check_passed": valid},
            is_mock=True,
            source="skeleton:structural-final-check",
        )
        return outcome_for(result, StatePatch(final_check=result))

    return [
        WorkflowNode(StepId.W01, load),
        WorkflowNode(StepId.W02, completeness, ("well", "raw_data", "data_requirements")),
        WorkflowNode(StepId.W03, qc, ("raw_data",)),
        WorkflowNode(StepId.W04, lithology, ("processed_data", "qc_result")),
        WorkflowNode(StepId.W05, petrophysics, ("lithology_result", "qc_result")),
        WorkflowNode(StepId.W06, fluid, ("lithology_result", "petrophysics_result")),
        WorkflowNode(StepId.W07, classification, ("fluid_result", "petrophysics_result")),
        WorkflowNode(StepId.W08, intervals, ("layer_classification", "petrophysics_result")),
        WorkflowNode(StepId.W09, validate, ("layer_classification", "interval_result")),
        WorkflowNode(
            StepId.W10,
            final_check,
            (
                "qc_result",
                "lithology_result",
                "petrophysics_result",
                "fluid_result",
                "layer_classification",
                "interval_result",
                "validation_result",
            ),
        ),
    ]
