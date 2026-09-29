"""从持久化执行事实生成有界的四阶段确认视图。"""

import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Literal, NamedTuple, Self, cast

from pydantic import Field, JsonValue, model_validator

from cnlc_agent.application.ports import TaskRepository
from cnlc_agent.application.stage_tool_runs import (
    MAX_STAGE_TOOL_RUNS,
    StageToolRunView,
    project_stage_tool_runs,
)
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.execution import Execution, ExecutionStatus, InterpretationTask
from cnlc_agent.domain.models import Contract, JsonObject, StageResult
from cnlc_agent.domain.stages import (
    InterpretationStage,
    StageRun,
    StageRunStatus,
    StageValidity,
)
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.domain.tool_run import ToolRun

MAX_ITEMS = 50
MAX_LIST_ITEMS = 50
MAX_TEXT_LENGTH = 1000
MAX_VIEW_BYTES = 128 * 1024
_FORBIDDEN_DATA_KEYS = {
    "raw_data",
    "processed_data",
    "depths",
    "values",
    "curve_values",
    "candidate_markdown",
    "markdown",
}

ConfirmationBlockerCode = Literal[
    "EXECUTION_NOT_CURRENT",
    "EXECUTION_NOT_WAITING_CONFIRMATION",
    "STAGE_NOT_WAITING_CONFIRM",
    "STAGE_RESULT_STALE",
]


class ConfirmationBlocker(Contract):
    """调用方可直接映射到确认页提示的稳定阻断原因。"""

    code: ConfirmationBlockerCode
    message: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)


class StageResultView(Contract):
    """确认页面使用的小型查询 DTO，不建立新的业务版本事实。"""

    task_id: str = Field(min_length=1)
    well_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    stage_run_id: str = Field(min_length=1)
    stage: InterpretationStage
    status: StageRunStatus
    validity: StageValidity
    headline: str = Field(max_length=MAX_TEXT_LENGTH)
    summary: str = Field(max_length=MAX_TEXT_LENGTH)
    metrics: JsonObject = Field(default_factory=dict)
    items: list[JsonObject] = Field(default_factory=list, max_length=MAX_ITEMS)
    warnings: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    conflicts: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    missing_items: list[str] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    input_refs: dict[str, str] = Field(default_factory=dict, max_length=64)
    output_refs: dict[str, str] = Field(default_factory=dict, max_length=64)
    can_confirm: bool
    confirmation_blockers: list[ConfirmationBlocker] = Field(default_factory=list, max_length=4)
    tool_runs: list[StageToolRunView] = Field(default_factory=list, max_length=MAX_STAGE_TOOL_RUNS)
    updated_at: datetime

    @model_validator(mode="after")
    def reject_large_business_payloads(self) -> Self:
        """即使 DTO 被其他调用方直接构造，也不能携带曲线正文或无界 JSON。"""

        def walk(value: JsonValue, depth: int = 0) -> None:
            if depth > 5:
                raise ValueError("stage result projection nesting is too deep")
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.casefold() in _FORBIDDEN_DATA_KEYS:
                        raise ValueError(f"stage result projection forbids field: {key}")
                    walk(child, depth + 1)
            elif isinstance(value, list):
                if len(value) > MAX_LIST_ITEMS:
                    raise ValueError("stage result projection list is too large")
                for child in value:
                    walk(child, depth + 1)
            elif isinstance(value, str) and len(value) > MAX_TEXT_LENGTH:
                raise ValueError("stage result projection text is too large")

        projection_payload = cast(
            JsonValue,
            self.model_dump(mode="json", exclude={"updated_at", "tool_runs"}),
        )
        walk(projection_payload)
        # ToolRunView 自身执行字段、深度和体积校验；这里再把它纳入整体大小上限。
        full_payload = self.model_dump(mode="json", exclude={"updated_at"})
        encoded = json.dumps(full_payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_VIEW_BYTES:
            raise ValueError("stage result projection is too large")
        return self


class _Projection(NamedTuple):
    headline: str
    summary: str
    metrics: JsonObject
    items: list[JsonObject]
    warnings: list[str]
    conflicts: list[str]
    missing_items: list[str]


def _bounded_strings(values: Iterable[str]) -> list[str]:
    """稳定去重并限制诊断文本数量和长度。"""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()[:MAX_TEXT_LENGTH]
        if item and item not in seen:
            seen.add(item)
            result.append(item)
        if len(result) == MAX_LIST_ITEMS:
            break
    return result


def _safe_scalar(value: object) -> JsonValue | None:
    """只允许有限标量进入展示 JSON，不透传任意业务嵌套对象。"""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:MAX_TEXT_LENGTH]
    return None


def _measurement(value: object) -> JsonObject | None:
    """保留已有 value/unit 测量事实，不计算或变换专业值。"""

    if not isinstance(value, Mapping):
        return None
    measurement: JsonObject = {}
    for key in ("value", "unit"):
        scalar = _safe_scalar(value.get(key))
        if scalar is not None:
            measurement[key] = scalar
    return measurement or None


def _small_result(result: StageResult | None, keys: tuple[str, ...]) -> JsonObject | None:
    """按白名单投影专业结果，数组和未知嵌套结构不会进入视图。"""

    if result is None:
        return None
    projection: JsonObject = {
        "status": result.status.value,
        "source": result.source[:MAX_TEXT_LENGTH],
        "is_mock": result.is_mock,
    }
    for key in keys:
        value = result.result.get(key)
        scalar = _safe_scalar(value)
        if scalar is not None:
            projection[key] = scalar
            continue
        measurement = _measurement(value)
        if measurement is not None:
            projection[key] = measurement
    return projection


def _embedded_water_saturation(result: StageResult | None) -> JsonObject | None:
    """读取 W06 已持久化的 SW Tool 结果，不在展示层重新计算饱和度。"""

    if result is None:
        return None
    wrapper = result.result.get("sw_result")
    if not isinstance(wrapper, Mapping):
        return None
    payload = wrapper.get("result")
    if not isinstance(payload, Mapping):
        return None
    return _measurement(payload.get("water_saturation"))


def _result_details(
    results: Iterable[StageResult | None],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """汇总既有诊断和证据文本，不对其内容做二次推理。"""

    present = [result for result in results if result is not None]
    warnings = _bounded_strings(item for result in present for item in result.warnings)
    conflicts = _bounded_strings(item for result in present for item in result.conflicts)
    missing = _bounded_strings(item for result in present for item in result.missing_evidence)
    evidence = _bounded_strings(item for result in present for item in result.evidence)
    return warnings, conflicts, missing, evidence


def _sampling_interval(depths: list[float]) -> float | None:
    """仅在统一深度轴等间隔时返回数据摘要，不做重采样。"""

    if len(depths) < 2:
        return None
    intervals = [right - left for left, right in zip(depths, depths[1:], strict=False)]
    first = intervals[0]
    tolerance = max(abs(first) * 1e-9, 1e-9)
    if all(math.isclose(item, first, rel_tol=1e-9, abs_tol=tolerance) for item in intervals):
        return first
    return None


def _decode_projection(state: InterpretationState, run: StageRun) -> _Projection:
    raw = state.raw_data
    well_id = state.well.well_id if state.well is not None else state.task.well_id
    metrics: JsonObject = {"well_id": well_id}
    items: list[JsonObject] = []
    missing: list[str] = []
    warnings = list(run.warnings)
    if state.well is not None:
        metrics["well_name"] = state.well.name[:MAX_TEXT_LENGTH]
        source = _safe_scalar(state.well.extensions.get("source"))
        if source is not None:
            metrics["data_source"] = source
    if state.input_version_id is not None:
        metrics["input_version_id"] = state.input_version_id
        metrics["data_source_ref"] = state.input_version_id
    elif input_ref := run.input_refs.get("input_version_id"):
        metrics["input_version_id"] = input_ref
        metrics["data_source_ref"] = input_ref
    if "data_source" not in metrics:
        metrics["data_source"] = (
            "InputVersion" if "data_source_ref" in metrics else "当前契约未提供来源类型"
        )
    if dataset_ref := run.output_refs.get("dataset_revision_id"):
        metrics["dataset_revision_id"] = dataset_ref
    if state.source_artifact_id is not None:
        metrics["source_artifact_id"] = state.source_artifact_id
    if state.dataset_manifest is not None:
        manifest = state.dataset_manifest
        metrics.update(
            {
                "curve_count": manifest.curve_count,
                "table_count": manifest.table_count,
                "depth_summary": manifest.depth_summary,
                "artifact_size_bytes": manifest.size_bytes,
                "artifact_sha256_prefix": manifest.content_sha256[:12],
            }
        )
        items.extend(
            {
                "raw_name": curve.raw_name,
                "standard_name": curve.standard_name,
                "unit": curve.unit,
                "point_count": curve.point_count,
            }
            for curve in manifest.curves[:MAX_ITEMS]
        )
        return _Projection(
            f"GDSX 数据解编完成，共识别 {manifest.curve_count} 条曲线。",
            f"已读取 {manifest.table_count} 张表；曲线采样值未进入结果视图。",
            metrics,
            items,
            _bounded_strings(warnings + manifest.warnings),
            [],
            missing,
        )
    if raw is None:
        missing.append("well_curve_summary: 当前阶段没有可展示的数据摘要")
        return _Projection(
            "数据解编结果不可用",
            "当前阶段未提供井曲线摘要。",
            metrics,
            items,
            _bounded_strings(warnings),
            [],
            missing,
        )

    curve_names = list(raw.curves)
    metrics.update(
        {
            "depth_unit": raw.depth_unit,
            "depth_reference": raw.depth_reference,
            "curve_count": len(curve_names),
            "sample_count": len(raw.depths),
        }
    )
    if raw.depths:
        metrics["depth_min"] = raw.depths[0]
        metrics["depth_max"] = raw.depths[-1]
    interval = _sampling_interval(raw.depths)
    if interval is not None:
        metrics["sampling_interval"] = interval
    else:
        metrics["sampling_interval_summary"] = "当前数据无法确定统一采样间隔"
    required = state.data_requirements.required_curves if state.data_requirements else []
    absent = sorted(set(required) - set(curve_names))
    metrics["missing_required_curve_count"] = len(absent)
    missing.extend(f"required_curve:{name}" for name in absent)
    items.extend(
        {
            "curve_code": name,
            "unit": raw.curves[name].unit,
            "sample_count": len(raw.curves[name].values),
        }
        for name in curve_names[:MAX_ITEMS]
    )
    if len(curve_names) > MAX_ITEMS:
        warnings.append(f"曲线清单仅展示前 {MAX_ITEMS} 条")
    if raw.depths:
        headline = (
            f"数据解编完成，共识别 {len(curve_names)} 条曲线，深度范围 "
            f"{raw.depths[0]:g}–{raw.depths[-1]:g} {raw.depth_unit}。"
        )
    else:
        headline = f"数据解编完成，共识别 {len(curve_names)} 条曲线。"
    summary = f"统一深度轴包含 {len(raw.depths)} 个采样点；必需曲线缺失 {len(absent)} 条。"
    return _Projection(
        headline,
        summary,
        metrics,
        items,
        _bounded_strings(warnings),
        [],
        _bounded_strings(missing),
    )


def _operation_items(qc: StageResult | None) -> list[JsonObject]:
    """只列出工具明确返回的预处理动作及是否执行。"""

    if qc is None:
        return []
    items: list[JsonObject] = []
    correction = qc.result.get("correction_applied")
    if isinstance(correction, bool):
        items.append({"operation": "curve_correction", "applied": correction})
    for key in ("operations", "operations_applied", "preprocessing_operations"):
        raw_operations = qc.result.get(key)
        if not isinstance(raw_operations, list):
            continue
        for operation in raw_operations:
            scalar = _safe_scalar(operation)
            if scalar is not None:
                items.append({"operation": scalar})
            elif isinstance(operation, Mapping):
                projected: JsonObject = {}
                for field in ("operation", "name", "applied", "status"):
                    value = _safe_scalar(operation.get(field))
                    if value is not None:
                        projected[field] = value
                if projected:
                    items.append(projected)
            if len(items) == MAX_ITEMS:
                return items
    return items


def _preprocess_projection(state: InterpretationState, run: StageRun) -> _Projection:
    qc = state.qc_result
    raw = state.raw_data
    processed = state.processed_data
    result = qc.result if qc is not None else {}
    preprocessing = result.get("preprocessing")
    preprocess_map = preprocessing if isinstance(preprocessing, Mapping) else {}
    requested = _safe_scalar(preprocess_map.get("requested_sampling_interval"))
    if requested is None:
        requested = _safe_scalar(state.effective_override.sampling_interval)
    resampling = _safe_scalar(preprocess_map.get("resampling_applied"))
    metrics: JsonObject = {
        "qc_status": qc.status.value if qc is not None else "UNAVAILABLE",
        "input_curve_count": len(raw.curves) if raw is not None else 0,
        "output_curve_count": len(processed.curves) if processed is not None else 0,
        "requested_sampling_interval": requested,
        "resampling_applied": resampling,
        "preprocess_result_ref": run.output_refs.get("preprocess_revision_id"),
    }
    if state.source_artifact_id is not None:
        metrics["source_artifact_id"] = state.source_artifact_id
    if state.processed_artifact_id is not None:
        metrics["processed_artifact_id"] = state.processed_artifact_id
    if raw is not None:
        metrics["input_missing_sample_count"] = sum(
            value is None for curve in raw.curves.values() for value in curve.values
        )
    if processed is not None:
        metrics["output_missing_sample_count"] = sum(
            value is None for curve in processed.curves.values() for value in curve.values
        )
    anomaly = None
    for key in ("anomaly_summary", "anomaly_count", "outlier_count"):
        value = _safe_scalar(result.get(key))
        if value is not None:
            metrics[key] = value
            anomaly = value
    missing: list[str] = []
    if anomaly is None:
        metrics["anomaly_summary"] = "当前工具未提供该统计"
        missing.append("anomaly_summary: 当前工具未提供该统计")
    reason = _safe_scalar(preprocess_map.get("reason"))
    if reason is not None:
        metrics["preprocessing_reason"] = reason
    warnings, conflicts, evidence_missing, _ = _result_details([qc])
    missing.extend(evidence_missing)
    warnings = _bounded_strings([*run.warnings, *warnings])
    quality = _safe_scalar(result.get("quality")) or _safe_scalar(result.get("overall_quality"))
    if quality is not None:
        metrics["quality"] = quality
    headline = (
        f"数据预处理与质量控制完成，输入 {metrics['input_curve_count']} 条曲线，"
        f"输出 {metrics['output_curve_count']} 条曲线。"
    )
    summary = (
        f"QC 状态为 {metrics['qc_status']}；"
        f"重采样实际执行状态为 {resampling if resampling is not None else '未提供'}。"
    )
    return _Projection(
        headline,
        summary,
        metrics,
        _operation_items(qc),
        warnings,
        conflicts,
        _bounded_strings(missing),
    )


_INTERPRET_FIELDS: dict[str, tuple[str, ...]] = {
    "lithology": ("lithology", "dominant_lithology", "summary"),
    "petrophysics": (
        "vsh",
        "porosity",
        "permeability",
        "water_saturation",
        "hydrocarbon_saturation",
        "reservoir",
        "reservoir_class",
        "summary",
    ),
    "fluid": (
        "fluid_type",
        "water_saturation",
        "hydrocarbon_saturation",
        "summary",
    ),
    "classification": (
        "layer_type",
        "classification",
        "reservoir_class",
        "fluid_type",
        "summary",
    ),
    "validation": ("summary", "status"),
    "final_check": ("summary", "status", "structural_check_passed"),
}


def _interval_items(state: InterpretationState) -> list[JsonObject]:
    """按白名单生成层段小表；缺少的专业字段保持缺失。"""

    result = state.interval_result
    raw_intervals = result.result.get("intervals") if result is not None else None
    if not isinstance(raw_intervals, list):
        return []
    aliases = {
        "interval_id": ("interval_id", "zone_id", "layer_no"),
        "top_depth": ("top_depth_m", "top_m", "start_depth_m"),
        "bottom_depth": ("bottom_depth_m", "bottom_m", "end_depth_m"),
        "thickness": ("gross_thickness_m", "thickness_m"),
        "effective_thickness": ("effective_thickness_m",),
        "lithology": ("lithology",),
        "fluid_or_layer_class": ("fluid_type", "layer_type", "classification"),
        "vsh": ("vsh",),
        "porosity": ("porosity",),
        "permeability": ("permeability",),
        "water_saturation": ("water_saturation", "sw"),
    }
    rows: list[JsonObject] = []
    for raw_row in raw_intervals[:MAX_ITEMS]:
        if not isinstance(raw_row, Mapping):
            continue
        row: JsonObject = {}
        for output_key, source_keys in aliases.items():
            for source_key in source_keys:
                value = raw_row.get(source_key)
                scalar = _safe_scalar(value)
                if scalar is not None:
                    row[output_key] = scalar
                    break
                measurement = _measurement(value)
                if measurement is not None:
                    row[output_key] = measurement
                    break
        if row:
            rows.append(row)
    return rows


def _interpret_projection(state: InterpretationState, run: StageRun) -> _Projection:
    named_results: tuple[tuple[str, StageResult | None], ...] = (
        ("lithology", state.lithology_result),
        ("petrophysics", state.petrophysics_result),
        ("fluid", state.fluid_result),
        ("classification", state.layer_classification),
        ("intervals", state.interval_result),
        ("validation", state.validation_result),
        ("final_check", state.final_check),
    )
    metrics: JsonObject = {}
    missing: list[str] = []
    for name, result in named_results:
        if result is None:
            missing.append(f"{name}: 当前没有正式结果")
            continue
        if name == "intervals":
            continue
        projection = _small_result(result, _INTERPRET_FIELDS[name])
        if projection is not None:
            if name == "fluid" and "water_saturation" not in projection:
                water_saturation = _embedded_water_saturation(result)
                if water_saturation is not None:
                    projection["water_saturation"] = water_saturation
            metrics[name] = projection
    if state.validation_result is not None:
        metrics["validation_status"] = state.validation_result.validation_status.value
    intervals = _interval_items(state)
    metrics["interval_count"] = len(intervals)
    warnings, conflicts, evidence_missing, evidence = _result_details(
        result for _, result in named_results
    )
    if evidence:
        evidence_values: list[JsonValue] = [item for item in evidence]
        metrics["evidence_summary"] = evidence_values
    missing.extend(evidence_missing)
    headline = f"解释阶段完成，形成 {len(intervals)} 个解释层段。"
    validation = metrics.get("validation_status", "当前无验证结果")
    summary = f"岩性、物性、流体和层分类按现有结构化结果汇总；验证状态为 {validation}。"
    return _Projection(
        headline,
        summary,
        metrics,
        intervals,
        _bounded_strings([*run.warnings, *warnings]),
        conflicts,
        _bounded_strings(missing),
    )


def _report_projection(
    state: InterpretationState, execution: Execution, run: StageRun
) -> _Projection:
    candidate_exists = bool(execution.markdown)
    is_formal = candidate_exists and execution.status in {
        ExecutionStatus.SUCCESS,
        ExecutionStatus.WARNING,
    }
    config_ref = run.input_refs.get("report_config_revision_id")
    style = None
    prefix = "report-config:style:"
    if config_ref is not None and config_ref.startswith(prefix):
        style = config_ref[len(prefix) :]
    intervals = _interval_items(state)
    conclusions: list[str] = []
    for result, keys in (
        (state.final_check, ("summary", "result")),
        (state.validation_result, ("summary",)),
        (state.layer_classification, ("summary", "layer_type", "classification")),
    ):
        if result is None:
            continue
        for key in keys:
            value = _safe_scalar(result.result.get(key))
            if isinstance(value, str) and value:
                conclusions.append(value)
    report_state = "READY" if is_formal else "CANDIDATE" if candidate_exists else "UNAVAILABLE"
    metrics: JsonObject = {
        "report_state": report_state,
        "report_style": style,
        "candidate_markdown_exists": candidate_exists,
        "interval_count": len(intervals),
        "report_ref": run.output_refs.get("report_revision_id"),
    }
    if conclusions:
        conclusion_values: list[JsonValue] = [item for item in _bounded_strings(conclusions)]
        metrics["core_conclusion_summary"] = conclusion_values
    results = (
        state.lithology_result,
        state.petrophysics_result,
        state.fluid_result,
        state.layer_classification,
        state.interval_result,
        state.validation_result,
        state.final_check,
    )
    warnings, conflicts, missing, _ = _result_details(results)
    headline = (
        "候选报告已生成，等待确认。"
        if candidate_exists and not is_formal
        else ("正式报告已生成。" if is_formal else "报告尚未生成。")
    )
    summary = f"报告样式为 {style or '当前未提供'}，包含 {len(intervals)} 个解释层段的现有结果。"
    return _Projection(
        headline,
        summary,
        metrics,
        [],
        _bounded_strings([*run.warnings, *warnings]),
        conflicts,
        missing,
    )


class StageResultProjector:
    """以显式 Task、Execution、StageRun 标识读取并投影阶段事实。"""

    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository

    async def project(self, task_id: str, execution_id: str, stage_run_id: str) -> StageResultView:
        """读取历史或当前执行；任何跨任务组合都按不存在处理。"""

        task = await self.repository.get_task(task_id)
        execution = await self.repository.get_execution(execution_id)
        if task is None or execution is None or execution.task_id != task_id:
            raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在或不属于任务")
        tool_runs = await self.repository.list_tool_runs(execution_id)
        return self.project_loaded(task, execution, stage_run_id, tool_runs=tool_runs)

    def project_loaded(
        self,
        task: InterpretationTask,
        execution: Execution,
        stage_run_id: str,
        *,
        tool_runs: list[ToolRun] | None = None,
    ) -> StageResultView:
        """纯投影 helper；调用方必须显式提供已查询的 ToolRun，不访问仓库。"""

        if execution.task_id != task.task_id or (
            execution.state_snapshot.task.well_id != task.well_id
        ):
            raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在或不属于任务")
        run = next(
            (
                item
                for item in execution.state_snapshot.stage_runs
                if item.id == stage_run_id and item.task_id == task.task_id
            ),
            None,
        )
        if run is None:
            raise InfrastructureError("STAGE_RUN_NOT_FOUND", "阶段运行不存在或归属不匹配")

        state = execution.state_snapshot
        if run.stage == InterpretationStage.DECODE:
            projection = _decode_projection(state, run)
        elif run.stage == InterpretationStage.PREPROCESS:
            projection = _preprocess_projection(state, run)
        elif run.stage == InterpretationStage.INTERPRET:
            projection = _interpret_projection(state, run)
        else:
            projection = _report_projection(state, execution, run)

        blockers: list[ConfirmationBlocker] = []
        if task.current_execution_id != execution.execution_id:
            blockers.append(
                ConfirmationBlocker(
                    code="EXECUTION_NOT_CURRENT", message="该结果属于历史 Execution"
                )
            )
        if execution.status != ExecutionStatus.WAITING_CONFIRMATION:
            blockers.append(
                ConfirmationBlocker(
                    code="EXECUTION_NOT_WAITING_CONFIRMATION",
                    message="Execution 当前不处于等待确认状态",
                )
            )
        if run.status != StageRunStatus.WAITING_CONFIRM:
            blockers.append(
                ConfirmationBlocker(
                    code="STAGE_NOT_WAITING_CONFIRM", message="阶段结果当前不等待确认"
                )
            )
        if run.validity != StageValidity.CURRENT:
            blockers.append(
                ConfirmationBlocker(code="STAGE_RESULT_STALE", message="阶段结果已经失效")
            )
        timestamps = [
            value
            for value in (
                execution.updated_at,
                run.started_at,
                run.finished_at,
                run.confirmed_at,
                run.stale_at,
            )
            if value is not None
        ]
        return StageResultView(
            task_id=task.task_id,
            well_id=task.well_id,
            execution_id=execution.execution_id,
            stage_run_id=run.id,
            stage=run.stage,
            status=run.status,
            validity=run.validity,
            headline=projection.headline,
            summary=projection.summary,
            metrics=projection.metrics,
            items=projection.items,
            warnings=projection.warnings,
            conflicts=projection.conflicts,
            missing_items=projection.missing_items,
            input_refs=dict(run.input_refs),
            output_refs=dict(run.output_refs),
            can_confirm=not blockers,
            confirmation_blockers=blockers,
            tool_runs=project_stage_tool_runs(tool_runs or [], run.stage),
            updated_at=max(timestamps),
        )
