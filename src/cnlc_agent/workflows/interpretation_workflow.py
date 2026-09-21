"""Sequential skeleton with auditable state changes and explicit terminal branches.

Automatic retry/rollback is intentionally deferred until its policy is designed.
Serious validation conflicts stop for review in this task.
"""

from pydantic import JsonValue
from pydantic import ValidationError as SchemaError

from cnlc_agent.application.ports import InterpretationStateStore, Telemetry
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import ApplicationError, WorkflowError
from cnlc_agent.domain.models import ErrorDetail, JsonObject, RawData, StageResult, utc_now
from cnlc_agent.domain.state import (
    InterpretationState,
    StateChange,
    StepExecution,
    StepOutcome,
)
from cnlc_agent.workflows.node import WorkflowNode

COMPLETED = {StepStatus.SUCCESS, StepStatus.WARNING}
OBSERVABILITY_SAMPLE_LIMIT = 10

AUXILIARY_LABELS = {
    "interval": "层段信息",
    "borehole": "井眼状况",
    "surrounding_rock_comparison": "上下围岩对比",
    "mud_logging": "录井油气显示",
    "historical_interpretation": "历史解释成果",
    "core": "岩心资料",
    "well_test": "试油资料",
}

STEP_OBSERVABILITY = {
    StepId.W01: ("加载井段资料", "读取本次待解释井段的曲线和辅助资料", "井号、井段曲线、辅助资料"),
    StepId.W02: (
        "检查资料完整性",
        "确认当前井段满足后续解释所需的基础资料",
        "深度数据、必需曲线、推荐辅助资料",
    ),
    StepId.W03: ("检查曲线质量", "检查测井曲线质量并形成质量结论", "井段曲线和深度数据"),
    StepId.W04: ("识别岩性", "根据井段资料形成岩性识别结果", "质量检查结果、处理后的曲线数据"),
    StepId.W05: (
        "评价储层物性",
        "形成泥质含量、孔隙度、渗透率等物性评价结果",
        "岩性结果、质量检查结果",
    ),
    StepId.W06: (
        "识别流体性质",
        "结合物性和电性资料判断流体性质",
        "岩性结果、物性结果、含水饱和度",
    ),
    StepId.W07: (
        "进行油气水层分类",
        "综合岩性、物性和流体结果形成层类型结论",
        "岩性结果、物性结果、流体识别结果",
    ),
    StepId.W08: (
        "整理层段解释结果",
        "整理已知层段的顶底深度、厚度和解释结论",
        "层类型结果、物性评价结果",
    ),
    StepId.W09: (
        "综合验证解释结果",
        "使用辅助资料检查解释结论是否存在冲突",
        "层类型结果、层段结果、辅助资料",
    ),
    StepId.W10: (
        "执行最终一致性检查",
        "确认本次井段解释流程完整且结果可输出",
        "W03 至 W09 的阶段结果",
    ),
}


def step_observability(step_id: StepId) -> JsonObject:
    step_name, step_description, processing_data = STEP_OBSERVABILITY[step_id]
    return {
        "step_name": step_name,
        "step_description": step_description,
        "processing_data": processing_data,
    }


STEP_INPUT_FIELDS = {
    StepId.W01: (),
    StepId.W02: ("well", "raw_data", "data_requirements"),
    StepId.W03: ("raw_data",),
    StepId.W04: ("processed_data", "qc_result"),
    StepId.W05: ("lithology_result", "qc_result"),
    StepId.W06: ("well", "processed_data", "qc_result", "lithology_result", "petrophysics_result"),
    StepId.W07: (
        "well",
        "processed_data",
        "qc_result",
        "lithology_result",
        "petrophysics_result",
        "fluid_result",
    ),
    StepId.W08: ("layer_classification", "petrophysics_result"),
    StepId.W09: ("layer_classification", "interval_result"),
    StepId.W10: (
        "qc_result",
        "lithology_result",
        "petrophysics_result",
        "fluid_result",
        "layer_classification",
        "interval_result",
        "validation_result",
    ),
}


def summarize_for_observability(value: object) -> JsonValue:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        summarized = {key: summarize_for_observability(item) for key, item in value.items()}
        curve_values = value.get("values")
        if isinstance(curve_values, list):
            summarized["value_count"] = len(curve_values)
            summarized["values"] = curve_values[:OBSERVABILITY_SAMPLE_LIMIT]
            if len(curve_values) > OBSERVABILITY_SAMPLE_LIMIT:
                summarized["values_truncated"] = True
        depths = value.get("depths")
        if isinstance(depths, list):
            summarized["depth_count"] = len(depths)
            summarized["depths"] = depths[:OBSERVABILITY_SAMPLE_LIMIT]
            if len(depths) > OBSERVABILITY_SAMPLE_LIMIT:
                summarized["depths_truncated"] = True
        return summarized
    if isinstance(value, list):
        sample = [summarize_for_observability(item) for item in value[:OBSERVABILITY_SAMPLE_LIMIT]]
        if len(value) <= OBSERVABILITY_SAMPLE_LIMIT:
            return sample
        return {"total_count": len(value), "sample_values": sample, "truncated": True}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def step_input_data(state: InterpretationState, step_id: StepId) -> JsonObject:
    if step_id == StepId.W01:
        return {
            "well_id": state.task.well_id,
            "instruction": state.task.instruction,
        }
    return {
        field: summarize_for_observability(getattr(state, field))
        for field in STEP_INPUT_FIELDS[step_id]
        if getattr(state, field) is not None
    }


def step_output_data(outcome: StepOutcome) -> JsonObject:
    patch = outcome.patch.model_dump(mode="json", exclude_unset=True)
    output: JsonObject = {
        "status": outcome.status.value,
        "updated_fields": summarize_for_observability(patch),
    }
    if outcome.missing_data:
        output["missing_data"] = summarize_for_observability(outcome.missing_data)
    if outcome.warnings:
        output["warnings"] = summarize_for_observability(outcome.warnings)
    if outcome.errors:
        output["errors"] = summarize_for_observability(outcome.errors)
    return output


def compact_result(result: StageResult | None) -> str:
    if result is None:
        return "无"
    if hasattr(result, "result"):
        result_data = result.result
        parts = []
        for key, value in result_data.items():
            if value is None or isinstance(value, (str, int, float, bool)):
                parts.append(f"{key}={value}")
            elif isinstance(value, list):
                parts.append(f"{key}({len(value)}项)")
            else:
                parts.append(key)
        mock_label = "；Mock" if result.is_mock else ""
        return f"{', '.join(parts) or '无结果字段'}{mock_label}"
    return str(result)


def raw_data_summary(raw_data: RawData | None) -> str:
    if raw_data is None:
        return "无井段数据"
    curves = ", ".join(raw_data.curves)
    auxiliary = (
        ", ".join(
            f"{AUXILIARY_LABELS.get(name, '其他资料')}({name})" for name in raw_data.auxiliary
        )
        or "无"
    )
    return f"depths；曲线[{curves}]；辅助资料[{auxiliary}]"


def step_input_summary(state: InterpretationState, step_id: StepId) -> str:
    if step_id == StepId.W01:
        return f"well_id={state.task.well_id}"
    parts = []
    for field in STEP_INPUT_FIELDS[step_id]:
        value = getattr(state, field)
        if value is None:
            continue
        if field in {"raw_data", "processed_data"}:
            parts.append(raw_data_summary(value))
        elif field == "well":
            parts.append(f"well_id={value.well_id}")
        elif field == "data_requirements":
            parts.append(f"必需曲线[{', '.join(value.required_curves)}]")
        else:
            parts.append(f"{field}：{compact_result(value)}")
    return "；".join(parts) or "无"


def format_measurement(value: object) -> str | None:
    if not isinstance(value, dict) or "value" not in value:
        return None
    number = value["value"]
    unit = value.get("unit", "")
    if unit == "fraction" and isinstance(number, (int, float)):
        return f"{number * 100:.1f}%"
    return f"{number} {unit}".strip()


def final_interpretation_summary(state: InterpretationState) -> str:
    classification = state.layer_classification.result if state.layer_classification else {}
    lithology = state.lithology_result.result if state.lithology_result else {}
    petrophysics = state.petrophysics_result.result if state.petrophysics_result else {}
    fluid = state.fluid_result.result if state.fluid_result else {}
    intervals = state.interval_result.result.get("intervals", []) if state.interval_result else []
    interval = intervals[0] if isinstance(intervals, list) and intervals else {}

    top = interval.get("top_depth_m") if isinstance(interval, dict) else None
    bottom = interval.get("bottom_depth_m") if isinstance(interval, dict) else None
    gross = interval.get("gross_thickness_m") if isinstance(interval, dict) else None
    vertical = interval.get("vertical_thickness_m") if isinstance(interval, dict) else None
    effective = interval.get("effective_thickness_m") if isinstance(interval, dict) else None

    lines = ["本层段最终解释结果："]
    if top is not None and bottom is not None:
        thickness = f"，总厚度={gross} m" if gross is not None else ""
        if effective is not None:
            thickness += f"，有效厚度={effective} m"
        elif vertical is not None:
            thickness += f"，垂直厚度={vertical} m"
        lines.append(f"  层段：{top}–{bottom} m{thickness}")
    lines.append(f"  岩性：{lithology.get('lithology', classification.get('lithology', '未形成'))}")
    lines.append(f"  储层类别：{classification.get('reservoir_class', '未形成')}")
    lines.append(f"  层类型：{classification.get('layer_type', '未形成')}")

    measurement_fields = [
        ("泥质含量", petrophysics.get("vsh")),
        ("孔隙度", petrophysics.get("porosity")),
        ("渗透率", petrophysics.get("permeability")),
    ]
    measurements = [
        f"{label}={formatted}"
        for label, value in measurement_fields
        if (formatted := format_measurement(value)) is not None
    ]
    if measurements:
        lines.append(f"  物性：{'，'.join(measurements)}")

    fluid_items = []
    if fluid.get("fluid_type"):
        fluid_items.append(f"流体类型={fluid['fluid_type']}")
    hydrocarbon = format_measurement(fluid.get("hydrocarbon_saturation"))
    if hydrocarbon:
        fluid_items.append(f"含烃饱和度={hydrocarbon}")
    sw_result = fluid.get("sw_result")
    if isinstance(sw_result, dict):
        sw_payload = sw_result.get("result", {})
        if isinstance(sw_payload, dict):
            water = format_measurement(sw_payload.get("water_saturation"))
            if water:
                fluid_items.append(f"含水饱和度={water}")
    if fluid_items:
        lines.append(f"  流体：{'，'.join(fluid_items)}")

    validation = state.validation_result
    validation_text = "未执行"
    if validation:
        validation_text = (
            "Demo 模式跳过真实综合验证"
            if validation.result.get("demo_skipped")
            else validation.validation_status.value
        )
    lines.append(f"  综合验证：{validation_text}")
    lines.append("  结果性质：Mock/历史解释演示结果，不是独立专业复算结论")
    return "\n".join(lines)


def step_output_summary(outcome: StepOutcome, state: InterpretationState, step_id: StepId) -> str:
    patch = outcome.patch
    parts = []
    for field in patch.model_fields_set:
        value = getattr(patch, field)
        if field in {"raw_data", "processed_data"}:
            parts.append(raw_data_summary(value))
        elif field == "well" and value is not None:
            parts.append(f"well_id={value.well_id}")
        elif field == "data_requirements" and value is not None:
            parts.append(f"必需曲线[{', '.join(value.required_curves)}]")
        elif value is not None:
            parts.append(f"{field}：{compact_result(value)}")
    if outcome.missing_data:
        parts.append(f"缺失项[{', '.join(item.field for item in outcome.missing_data)}]")
    if outcome.warnings:
        parts.append(f"警告[{', '.join(outcome.warnings)}]")
    summary = "；".join(parts) or f"状态={outcome.status.value}；无缺失项"
    if step_id == StepId.W10:
        return f"{summary}\n{final_interpretation_summary(state)}"
    return summary


class InterpretationWorkflow:
    def __init__(
        self,
        nodes: list[WorkflowNode],
        store: InterpretationStateStore,
        telemetry: Telemetry,
    ) -> None:
        if [node.step_id for node in nodes] != list(StepId):
            raise WorkflowError("INVALID_STEP_ORDER", "Workflow 必须按 W01–W10 顺序构造")
        self.nodes = nodes
        self.store = store
        self.telemetry = telemetry

    async def run(self, state: InterpretationState) -> InterpretationState:
        if state.status != StepStatus.PENDING:
            raise WorkflowError("TASK_ALREADY_STARTED", "骨架暂不支持恢复已有任务，请创建新任务")
        attributes: JsonObject = {"task_id": state.task.task_id, "trace_id": state.trace_id}
        with self.telemetry.span("workflow", attributes):
            for node in self.nodes:
                state.current_step = node.step_id
                state.status = StepStatus.RUNNING
                input_data = step_input_data(state, node.step_id)
                input_summary = step_input_summary(state, node.step_id)
                record = StepExecution(step_id=node.step_id)
                state.executions.append(record)
                state.updated_at = utc_now()
                await self.store.save(state)
                with self.telemetry.span(
                    "workflow.step",
                    {
                        **attributes,
                        "step_id": node.step_id.value,
                        **step_observability(node.step_id),
                        "input_data": input_data,
                        "input_summary": input_summary,
                    },
                ):
                    try:
                        missing = node.precondition(state)
                        if missing:
                            outcome = StepOutcome(
                                status=StepStatus.BLOCKED,
                                missing_data=missing,
                                reason="节点前置结果缺失",
                            )
                        else:
                            # Nodes receive snapshots: all writes must return through StatePatch.
                            outcome = await node.execute(state.model_copy(deep=True))
                        if outcome.status not in COMPLETED | {
                            StepStatus.FAILED,
                            StepStatus.BLOCKED,
                            StepStatus.REVIEW_REQUIRED,
                        }:
                            raise WorkflowError("INVALID_NODE_STATUS", "节点返回了非终态状态")
                    except ApplicationError as exc:
                        outcome = StepOutcome(
                            status=StepStatus.FAILED,
                            reason="节点执行异常",
                            errors=[
                                ErrorDetail(
                                    code=exc.code,
                                    message=str(exc),
                                    retryable=exc.retryable,
                                    step_id=node.step_id,
                                )
                            ],
                        )
                    except Exception as exc:
                        # Never claim success for unexpected errors. Do not expose raw payloads.
                        self.telemetry.event(
                            "workflow.unexpected_error",
                            {
                                **attributes,
                                "step_id": node.step_id.value,
                                **step_observability(node.step_id),
                                "input_data": input_data,
                                "input_summary": input_summary,
                                "error_type": type(exc).__name__,
                            },
                        )
                        outcome = StepOutcome(
                            status=StepStatus.FAILED,
                            reason="节点出现非预期错误",
                            errors=[
                                ErrorDetail(
                                    code="INVALID_RESULT"
                                    if isinstance(exc, SchemaError)
                                    else "UNEXPECTED_ERROR",
                                    message="节点输出无效或执行异常；请查看事件记录",
                                    step_id=node.step_id,
                                )
                            ],
                        )
                    patch = outcome.patch.model_dump(mode="json", exclude_unset=True)
                    before = state.model_dump(mode="json", include=set(patch))
                    before["status"] = state.status.value
                    state = InterpretationState.model_validate(
                        {
                            **state.model_dump(mode="json"),
                            **patch,
                        }
                    )
                    record = state.executions[-1]
                    record.status = outcome.status
                    record.ended_at = utc_now()
                    record.warnings = outcome.warnings
                    record.errors = outcome.errors
                    state.status = outcome.status
                    state.missing_data.extend(outcome.missing_data)
                    state.warnings.extend(outcome.warnings)
                    state.errors.extend(outcome.errors)
                    state.review_required = outcome.status == StepStatus.REVIEW_REQUIRED
                    state.updated_at = utc_now()
                    if outcome.status in COMPLETED:
                        state.completed_steps.append(node.step_id)
                    state.changes.append(
                        StateChange(
                            actor="InterpretationWorkflow",
                            step_id=node.step_id,
                            reason=outcome.reason,
                            before=before,
                            after={**patch, "status": state.status.value},
                        )
                    )
                    self.telemetry.event(
                        "state.change",
                        {
                            **attributes,
                            "step_id": node.step_id.value,
                            **step_observability(node.step_id),
                            "status": state.status.value,
                            "reason": outcome.reason,
                            "output_data": step_output_data(outcome),
                            "output_summary": step_output_summary(outcome, state, node.step_id),
                        },
                    )
                    await self.store.save(state)
                if outcome.status not in COMPLETED:
                    break
            if len(state.completed_steps) == len(StepId):
                state.status = (
                    StepStatus.WARNING
                    if state.warnings
                    or any(item.status == StepStatus.WARNING for item in state.executions)
                    else StepStatus.SUCCESS
                )
            state.updated_at = utc_now()
            await self.store.save(state)
            self.telemetry.event("workflow.result", {**attributes, "status": state.status.value})
            return state
