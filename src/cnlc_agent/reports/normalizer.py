"""从 Workflow 管理的状态构造唯一、忠于事实的报告投影。"""

import math
from collections.abc import Iterable, Mapping

from cnlc_agent.domain.models import RawData, StageResult
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.reports.formatter import is_missing
from cnlc_agent.reports.models import (
    AcquisitionCurve,
    InterpretedLayer,
    Measurement,
    NormalizedInterpretationResult,
    ValidationSummary,
    WellSummary,
)

_CURVE_PURPOSES = {
    "GR": "岩性及泥质含量分析",
    "SP": "渗透层识别及地层对比",
    "CAL": "井径与井眼条件评价",
    "CALI": "井径与井眼条件评价",
    "AC": "孔隙结构与储层物性评价",
    "DT": "孔隙结构与储层物性评价",
    "DEN": "密度与孔隙度评价",
    "RHOB": "密度与孔隙度评价",
    "CNL": "中子孔隙度评价",
    "NPHI": "中子孔隙度评价",
    "RT": "地层电阻率与流体识别",
    "RLLD": "深电阻率与流体识别",
    "RLLS": "浅电阻率与侵入特征分析",
    "RXO": "冲洗带电阻率与侵入特征分析",
    "RS": "浅电阻率与侵入特征分析",
}

_REPORT_TEXT_REPLACEMENTS = {
    (
        "Fluid interpretation relies on historical interval statistics; no real-time or "
        "independently computed saturation or fluid typing logs (e.g., NMR, MDT, DST) available"
    ): (
        "本次流体识别主要依据历史层段统计及既有解释成果；含水饱和度为历史解释值，"
        "未根据原始测井曲线独立复算，流体类型结论也缺少独立验证资料"
    ),
}


def normalize_interpretation_result(
    state: InterpretationState,
) -> NormalizedInterpretationResult:
    """投影现有状态事实，不增加任何新的专业解释决策。"""

    raw = state.raw_data
    extensions = _mapping(state.well.extensions if state.well else {})
    logging_interval = _logging_interval(state, raw, extensions)
    well = WellSummary(
        well_id=state.well.well_id if state.well else state.task.well_id,
        name=_string(state.well.name if state.well else None),
        well_category=_alias_string(extensions, "well_category", "well_class", "well_kind"),
        well_type=_alias_string(extensions, "well_type", "trajectory_type"),
        completion_date=_alias_string(extensions, "completion_date", "final_date"),
        total_depth_m=_alias_float(extensions, "total_depth_m", "completion_depth_m"),
        completion_formation=_alias_string(
            extensions, "completion_formation", "completion_horizon"
        ),
        target_formation=_alias_string(extensions, "target_formation", "formation"),
        structural_location=_alias_string(
            extensions, "structural_location", "tectonic_location", "location"
        ),
        logging_interval=logging_interval,
        drilling_purpose=_string(state.task.instruction),
        wellbore_structure=_alias_string(extensions, "wellbore_structure", "casing_program"),
        drilling_fluid=_alias_string(
            extensions, "drilling_fluid", "drilling_fluid_properties", "mud_properties"
        ),
        other_notes=_alias_string(extensions, "description", "other_notes", "remarks"),
    )

    # 后续模板只读取归一化 DTO，不能再自行解释原始 State 的嵌套结构。
    curves = _curves(raw, logging_interval)
    auxiliary = _mapping(raw.auxiliary if raw else {})
    quality_summary, quality_evidence = _quality(state, raw)
    layers = _layers(state, raw)
    recommendations = _recommendations(state, layers)
    limitations = _limitations(state, raw, auxiliary)

    return NormalizedInterpretationResult(
        status=state.status.value,
        generated_date=state.updated_at.date().isoformat(),
        is_mock=_uses_mock_results(state),
        well=well,
        curves=curves,
        curve_names=tuple(curve.name for curve in curves),
        quality_summary=quality_summary,
        quality_evidence=quality_evidence,
        preprocessing=_preprocessing(state),
        geology_summary=_geology_summary(well, auxiliary),
        mud_logging_summary=_auxiliary_summary(auxiliary.get("mud_logging")),
        core_summary=_auxiliary_summary(auxiliary.get("core")),
        well_test_summary=_auxiliary_summary(auxiliary.get("well_test")),
        offset_well_summary=_auxiliary_summary(
            auxiliary.get("offset_well") or auxiliary.get("offset_wells")
        ),
        lithology_summary=_unique(layer.lithology for layer in layers),
        reservoir_summary=_unique(layer.reservoir_class for layer in layers),
        fluid_summary=_unique(
            value for layer in layers for value in (layer.fluid_type, layer.layer_type)
        ),
        layers=layers,
        validation=_validation(state),
        recommendations=recommendations,
        limitations=limitations,
    )


def _curves(raw: RawData | None, interval: str | None) -> tuple[AcquisitionCurve, ...]:
    if raw is None:
        return ()
    return tuple(
        AcquisitionCurve(
            name=name,
            unit=_string(curve.unit),
            interval=interval,
            valid_samples=sum(value is not None for value in curve.values),
            total_samples=len(curve.values),
            purpose=_CURVE_PURPOSES.get(name.upper(), "测井解释输入"),
        )
        for name, curve in raw.curves.items()
    )


def _logging_interval(
    state: InterpretationState,
    raw: RawData | None,
    extensions: Mapping[str, object],
) -> str | None:
    interval_result = _mapping(state.interval_result.result if state.interval_result else {})
    rows = _sequence_of_mappings(interval_result.get("intervals"))
    tops = [
        value for row in rows if (value := _alias_float(row, "top_depth_m", "top_m")) is not None
    ]
    bottoms = [
        value
        for row in rows
        if (value := _alias_float(row, "bottom_depth_m", "bottom_m")) is not None
    ]
    if tops and bottoms:
        return f"{min(tops):.2f}～{max(bottoms):.2f} m"
    auxiliary_interval = _mapping(raw.auxiliary.get("interval") if raw else None)
    top = _alias_float(auxiliary_interval, "top_depth_m", "top_m")
    bottom = _alias_float(auxiliary_interval, "bottom_depth_m", "bottom_m")
    if top is not None and bottom is not None:
        return f"{top:.2f}～{bottom:.2f} m"
    depths = raw.depths if raw else []
    if depths:
        return f"{min(depths):.2f}～{max(depths):.2f} m"
    return _string(extensions.get("logging_interval"))


def _quality(
    state: InterpretationState, raw: RawData | None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    facts: list[str] = []
    evidence: list[str] = []
    if raw is not None:
        facts.append(f"本次提供{len(raw.curves)}条测井曲线，共{len(raw.depths)}个深度采样点。")
        missing_samples = sum(
            value is None for curve in raw.curves.values() for value in curve.values
        )
        if missing_samples:
            facts.append(f"现有曲线共发现{missing_samples}个缺失采样值。")
        elif raw.curves:
            facts.append("已提供曲线在当前采样范围内未发现空采样值。")
    qc = state.qc_result
    result = _mapping(qc.result if qc else {})
    nested_quality = _mapping(result.get("data_quality"))
    quality = _alias_string(result, "quality", "overall_quality") or _alias_string(
        nested_quality, "overall_level", "quality"
    )
    if quality:
        facts.append(f"结构化质量评价为“{quality}”。")
    borehole = _alias_string(result, "borehole_condition", "caliper_condition")
    if borehole:
        facts.append(f"井眼条件评价：{borehole}。")
    if result.get("correction_applied") is True:
        facts.append("结构化结果标记已执行校正。")
    elif result.get("correction_applied") is False:
        facts.append("结构化结果未标记已执行环境校正。")
    if qc is not None:
        evidence.extend(qc.evidence)
    return _unique(facts), _unique(evidence)


def _preprocessing(state: InterpretationState) -> tuple[str, ...]:
    steps = ["加载井资料并检查深度与曲线数据结构"]
    if state.qc_result is not None:
        steps.append("执行曲线质量检查并记录井眼影响与数据限制")
    if state.lithology_result is not None:
        steps.append("形成结构化岩性识别结果")
    if state.petrophysics_result is not None:
        steps.append("形成结构化储层物性评价结果")
    if state.fluid_result is not None:
        steps.append("结合已有物性、电性及辅助证据形成流体识别结果")
    if state.layer_classification is not None:
        steps.append("形成油气水层分类结果")
    if state.interval_result is not None:
        steps.append("整理解释层段及厚度结果")
    return tuple(steps)


def _geology_summary(well: WellSummary, auxiliary: Mapping[str, object]) -> tuple[str, ...]:
    notes: list[str] = []
    if well.target_formation:
        notes.append(f"本次资料标记的目标层位为{well.target_formation}。")
    geology = _mapping(auxiliary.get("geology"))
    labels = {
        "block": "区块",
        "formation": "层位",
        "target_formation": "目的层",
        "structural_location": "构造位置",
        "summary": "地质概况",
    }
    for key, label in labels.items():
        value = _string(geology.get(key))
        if value:
            notes.append(f"{label}：{value}。")
    return _unique(notes)


def _layers(state: InterpretationState, raw: RawData | None) -> tuple[InterpretedLayer, ...]:
    interval_result = _mapping(state.interval_result.result if state.interval_result else {})
    interval_rows = _sequence_of_mappings(interval_result.get("intervals"))
    layers: list[InterpretedLayer] = []
    for index, interval in enumerate(interval_rows):
        # 优先按 zone_id 对齐各阶段结果；没有标识时才回退到同序号位置。
        zone_id = _alias_string(interval, "zone_id", "interval_id")
        lithology = _stage_payload(state.lithology_result, "lithology", zone_id, index)
        petrophysics = _stage_payload(state.petrophysics_result, "petrophysics", zone_id, index)
        fluid = _stage_payload(state.fluid_result, "fluid", zone_id, index)
        classification = _stage_payload(
            state.layer_classification, "classification", zone_id, index
        )
        top = _alias_float(interval, "top_depth_m", "top_m", "start_depth_m")
        bottom = _alias_float(interval, "bottom_depth_m", "bottom_m", "end_depth_m")
        gross = _alias_float(interval, "gross_thickness_m", "thickness_m")
        # 总厚度可由明确层界相减得到；其他专业参数绝不在报告层补算。
        if gross is None and top is not None and bottom is not None:
            gross = bottom - top
        layer_no = interval.get("layer_no")
        layer_id = zone_id or (str(layer_no) if not is_missing(layer_no) else None)
        water = _measurement_from(
            petrophysics,
            ("water_saturation", "water_saturation_fraction", "sw"),
            fraction_keys={"water_saturation_fraction", "sw"},
        )
        if water.value is None:
            water = _water_saturation(fluid)
        recommendation = _explicit_recommendation(interval, classification, fluid)
        layers.append(
            InterpretedLayer(
                layer_id=layer_id,
                formation=_alias_string(interval, "formation", "horizon"),
                top_depth_m=top,
                bottom_depth_m=bottom,
                gross_thickness_m=gross,
                effective_thickness_m=_alias_float(
                    interval, "effective_thickness_m", "net_thickness_m"
                ),
                lithology=_alias_string(interval, "lithology")
                or _lithology(lithology)
                or _alias_string(classification, "lithology"),
                reservoir_class=_alias_string(interval, "reservoir_class")
                or _alias_string(classification, "reservoir_class")
                or _alias_string(petrophysics, "reservoir", "reservoir_class"),
                layer_type=_alias_string(interval, "layer_type", "interpretation_result")
                or _alias_string(
                    classification, "layer_type", "interpretation_result", "fluid_type"
                ),
                gr=_curve_measurement(raw, ("GR",), index, top, len(interval_rows)),
                acoustic=_curve_measurement(raw, ("AC", "DT"), index, top, len(interval_rows)),
                density=_curve_measurement(raw, ("DEN", "RHOB"), index, top, len(interval_rows)),
                resistivity=_curve_measurement(
                    raw, ("RT", "RLLD", "ILD"), index, top, len(interval_rows)
                ),
                vsh=_measurement_from(
                    petrophysics,
                    ("vsh", "shale_volume", "vsh_fraction"),
                    fraction_keys={"vsh", "vsh_fraction"},
                ),
                porosity=_measurement_from(
                    petrophysics,
                    ("porosity", "porosity_fraction", "porosity_percent"),
                    fraction_keys={"porosity_fraction"},
                ),
                permeability=_measurement_from(
                    petrophysics, ("permeability", "permeability_md"), default_unit="mD"
                ),
                water_saturation=water,
                hydrocarbon_saturation=_measurement_from(
                    fluid,
                    (
                        "hydrocarbon_saturation",
                        "hydrocarbon_saturation_fraction",
                        "oil_saturation_fraction",
                    ),
                    fraction_keys={
                        "hydrocarbon_saturation_fraction",
                        "oil_saturation_fraction",
                    },
                ),
                total_hydrocarbon=_curve_measurement(
                    raw, ("TOTAL_HC", "TG", "TGas"), index, top, len(interval_rows)
                ),
                heavy_hydrocarbon=_curve_measurement(
                    raw, ("HEAVY_HC",), index, top, len(interval_rows)
                ),
                fluid_type=_alias_string(fluid, "fluid_type")
                or _alias_string(classification, "fluid_type"),
                evidence=_stage_items(
                    state.lithology_result,
                    state.petrophysics_result,
                    state.fluid_result,
                    state.layer_classification,
                    field="evidence",
                ),
                warnings=_stage_items(
                    state.lithology_result,
                    state.petrophysics_result,
                    state.fluid_result,
                    state.layer_classification,
                    field="warnings",
                ),
                recommendation=recommendation,
            )
        )
    return tuple(layers)


def _stage_payload(
    stage: StageResult | None, nested_key: str, zone_id: str | None, index: int
) -> dict[str, object]:
    result = _mapping(stage.result if stage else {})
    zones = _sequence_of_mappings(result.get("zones"))
    if zones:
        candidate = next(
            (row for row in zones if zone_id and _string(row.get("zone_id")) == zone_id),
            zones[index] if index < len(zones) else {},
        )
        nested = _mapping(candidate.get(nested_key))
        return nested or candidate
    return result


def _curve_measurement(
    raw: RawData | None,
    aliases: tuple[str, ...],
    interval_index: int,
    top_depth: float | None,
    interval_count: int,
) -> Measurement:
    """按层顶深度或明确的一一对应关系读取曲线值，不做插值推断。"""

    if raw is None:
        return Measurement()
    curve = next((raw.curves[name] for name in aliases if name in raw.curves), None)
    if curve is None:
        return Measurement()
    value: float | None = None
    if top_depth is not None:
        sample_index = next(
            (
                index
                for index, depth in enumerate(raw.depths)
                if math.isclose(depth, top_depth, rel_tol=0, abs_tol=1e-6)
            ),
            None,
        )
        if sample_index is not None:
            value = curve.values[sample_index]
    if value is None and interval_count == len(curve.values) and interval_index < len(curve.values):
        value = curve.values[interval_index]
    if value is None and interval_count == 1:
        # 单层统计型 Fixture 可使用已有有效采样的均值；该值仍来自上传数据。
        valid = [item for item in curve.values if item is not None and math.isfinite(item)]
        value = sum(valid) / len(valid) if valid else None
    return Measurement(value=value, unit=curve.unit)


def _measurement_from(
    payload: Mapping[str, object],
    keys: tuple[str, ...],
    *,
    default_unit: str | None = None,
    fraction_keys: set[str] | None = None,
) -> Measurement:
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, Mapping):
            mapped = _mapping(value)
            return Measurement(
                value=_float_or_string(mapped.get("value")),
                unit=_string(mapped.get("unit")) or default_unit,
            )
        unit = "fraction" if fraction_keys and key in fraction_keys else default_unit
        if key.endswith("_percent"):
            unit = "%"
        return Measurement(value=_float_or_string(value), unit=unit)
    return Measurement()


def _water_saturation(fluid: Mapping[str, object]) -> Measurement:
    sw_result = _mapping(fluid.get("sw_result"))
    result = _mapping(sw_result.get("result"))
    return _measurement_from(
        result,
        ("water_saturation", "water_saturation_fraction", "sw"),
        fraction_keys={"water_saturation_fraction", "sw"},
    )


def _lithology(payload: Mapping[str, object]) -> str | None:
    direct = _alias_string(payload, "lithology", "primary")
    if direct:
        return direct
    nested = _mapping(payload.get("lithology"))
    return _alias_string(nested, "primary", "name", "type")


def _explicit_recommendation(*payloads: Mapping[str, object]) -> str | None:
    for payload in payloads:
        value = _alias_string(payload, "recommendation", "recommended_action", "suggestion")
        if value and value.casefold() not in {"continue", "none", "null", "nan"}:
            return value
    return None


def _validation(state: InterpretationState) -> ValidationSummary:
    validation = state.validation_result
    if validation is None:
        return ValidationSummary()
    result = _mapping(validation.result)
    return ValidationSummary(
        status=validation.validation_status.value,
        summary=_alias_string(result, "summary", "conclusion"),
        evidence=tuple(validation.evidence),
        conflicts=tuple(validation.conflicts),
        missing_evidence=tuple(validation.missing_evidence),
        recommended_action=(
            validation.recommended_action
            if validation.recommended_action.casefold() not in {"continue", "none", "null", "nan"}
            else None
        ),
    )


def _recommendations(
    state: InterpretationState, layers: tuple[InterpretedLayer, ...]
) -> tuple[str, ...]:
    items = [layer.recommendation for layer in layers]
    for stage in (
        state.fluid_result,
        state.layer_classification,
        state.interval_result,
        state.validation_result,
    ):
        if stage is not None and stage.recommended_action.casefold() not in {
            "continue",
            "none",
            "null",
            "nan",
        }:
            items.append(stage.recommended_action)
    return _unique(items)


def _limitations(
    state: InterpretationState,
    raw: RawData | None,
    auxiliary: Mapping[str, object],
) -> tuple[str, ...]:
    items: list[str | None] = []
    items.extend(
        f"缺失资料：{entry.field}（影响{entry.affected_step.value}）"
        for entry in state.missing_data
    )
    items.extend(_report_text(value) for value in state.warnings)
    for stage in (
        state.qc_result,
        state.lithology_result,
        state.petrophysics_result,
        state.fluid_result,
        state.layer_classification,
        state.interval_result,
        state.validation_result,
    ):
        if stage is not None:
            items.extend(_report_text(value) for value in stage.warnings)
            items.extend(f"缺少验证证据：{_report_text(value)}" for value in stage.missing_evidence)
    extensions = _mapping(raw.extensions if raw else {})
    for key in ("missing_raw_curves", "missing_input_curves"):
        missing_curves = _sequence(extensions.get(key))
        if missing_curves:
            items.append(f"未提供测井曲线：{'、'.join(map(str, missing_curves))}")
    for key, label in (
        ("core", "岩心取心及实验分析资料"),
        ("mud_logging", "完整录井显示资料"),
        ("well_test", "试油或试气验证资料"),
    ):
        if not _has_content(auxiliary.get(key)):
            items.append(f"本次未提供{label}")
    if not _has_content(auxiliary.get("offset_well")) and not _has_content(
        auxiliary.get("offset_wells")
    ):
        items.append("本次未提供邻井对比资料")
    return _unique(items)


def _uses_mock_results(state: InterpretationState) -> bool:
    stages = (
        state.qc_result,
        state.lithology_result,
        state.petrophysics_result,
        state.fluid_result,
        state.layer_classification,
        state.interval_result,
        state.validation_result,
    )
    return state.mode in {"mock", "demo"} or any(
        stage is not None and stage.is_mock for stage in stages
    )


def _stage_items(*stages: StageResult | None, field: str) -> tuple[str, ...]:
    values: list[str] = []
    for stage in stages:
        if stage is not None:
            values.extend(getattr(stage, field))
    return _unique(values)


def _report_text(value: object) -> str:
    rendered = _string(value) or ""
    return _REPORT_TEXT_REPLACEMENTS.get(rendered, rendered)


def _auxiliary_summary(value: object) -> str | None:
    if not _has_content(value):
        return None
    if isinstance(value, Mapping):
        mapped = _mapping(value)
        summary = _alias_string(mapped, "summary", "conclusion", "description")
        return summary or "已提供结构化资料，当前 Schema 未定义可直接展示的摘要字段。"
    if isinstance(value, list | tuple):
        return f"已提供{len(value)}条结构化资料。"
    return _string(value)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _sequence(value: object) -> list[object]:
    return list(value) if isinstance(value, list | tuple) else []


def _sequence_of_mappings(value: object) -> list[dict[str, object]]:
    return [mapped for item in _sequence(value) if (mapped := _mapping(item))]


def _string(value: object) -> str | None:
    if is_missing(value) or isinstance(value, Mapping | list | tuple):
        return None
    return str(value).strip()


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _float_or_string(value: object) -> float | str | None:
    numeric = _float(value)
    return numeric if numeric is not None else _string(value)


def _alias_string(mapping: Mapping[str, object], *keys: str) -> str | None:
    return next((value for key in keys if (value := _string(mapping.get(key))) is not None), None)


def _alias_float(mapping: Mapping[str, object], *keys: str) -> float | None:
    return next((value for key in keys if (value := _float(mapping.get(key))) is not None), None)


def _has_content(value: object) -> bool:
    if is_missing(value):
        return False
    if isinstance(value, Mapping | list | tuple):
        return bool(value)
    return True


def _unique(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        rendered = _string(value)
        if rendered and rendered not in result:
            result.append(rendered)
    return tuple(result)
