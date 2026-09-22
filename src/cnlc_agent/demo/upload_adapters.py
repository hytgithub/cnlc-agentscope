"""Deterministic adapters for explicitly synthetic uploaded demo data."""

from typing import Any, cast

from cnlc_agent.domain.enums import ValidationStatus
from cnlc_agent.domain.models import MockFixture

_CURVE_FIELDS = {
    "GR_API": ("GR", "API"),
    "CALI_mm": ("CALI", "mm"),
    "RHOB_g_cm3": ("DEN", "g/cm3"),
    "NPHI_v_v": ("CNL", "fraction"),
    "DT_us_ft": ("AC", "us/ft"),
    "RT_ohm_m": ("RT", "ohm.m"),
    "RS_ohm_m": ("RS", "ohm.m"),
    "DRHO_g_cm3": ("DRHO", "g/cm3"),
}


def adapt_synthetic_context(data: dict[str, Any]) -> MockFixture | None:
    """Map the known synthetic interpretation-context shape to MockFixture.

    The adapter only rearranges values already present in the upload. It does
    not calculate petrophysical properties or add professional conclusions.
    """

    if data.get("data_type") != "synthetic_single_well_interpretation_context":
        return None
    well = _dict(data.get("well"))
    log_data = _dict(data.get("log_data"))
    zone_rows = [
        row
        for item in _list(log_data.get("target_zone_statistics"))
        if (row := _dict(item)) and _number(row.get("top_m")) is not None
    ]
    zone_rows.sort(key=lambda item: cast(float, _number(item.get("top_m"))))
    if not well or not zone_rows:
        return None

    depths = [cast(float, _number(row["top_m"])) for row in zone_rows]
    if len(set(depths)) != len(depths):
        return None

    curves: dict[str, dict[str, Any]] = {}
    for source_name, (target_name, unit) in _CURVE_FIELDS.items():
        values = [
            _number(_dict(_dict(row.get("statistics")).get(source_name)).get("mean"))
            for row in zone_rows
        ]
        if any(value is not None for value in values):
            curves[target_name] = {"unit": unit, "values": values}
    if not curves:
        return None

    interpretations = [
        row for item in _list(data.get("interpretation_results")) if (row := _dict(item))
    ]
    source = "upload:synthetic-interpretation-context"
    outputs = {
        "qc": _stage(
            source,
            {"data_quality": data.get("data_quality", {})},
            ["使用上传文件中的 data_quality；未重新执行曲线质量算法"],
        ),
        "lithology": _stage(
            source,
            {"zones": [_zone_view(row, "lithology") for row in interpretations]},
            ["岩性结果来自上传文件 interpretation_results"],
        ),
        "petrophysics": _stage(
            source,
            {"zones": [_zone_view(row, "petrophysics") for row in interpretations]},
            ["物性结果来自上传文件 interpretation_results，未重新计算"],
        ),
        "sw": _stage(
            source,
            {
                "zones": [
                    {
                        "zone_id": row.get("zone_id"),
                        "water_saturation_fraction": _dict(row.get("petrophysics")).get(
                            "water_saturation_fraction"
                        ),
                    }
                    for row in interpretations
                ]
            },
            ["含水饱和度来自上传文件，未重新执行专业算法"],
        ),
        "fluid": _stage(
            source,
            {
                "zones": [
                    {
                        "zone_id": row.get("zone_id"),
                        "fluid_type": _dict(row.get("classification")).get("fluid_type"),
                    }
                    for row in interpretations
                ]
            },
            ["流体类型来自上传文件 interpretation_results"],
        ),
        "classification": _stage(
            source,
            {"zones": [_zone_view(row, "classification") for row in interpretations]},
            ["层分类来自上传文件 interpretation_results"],
        ),
        "intervals": _stage(
            source,
            {
                "intervals": [
                    {
                        "zone_id": row.get("zone_id"),
                        "top_depth_m": row.get("top_m"),
                        "bottom_depth_m": row.get("bottom_m"),
                        "gross_thickness_m": row.get("gross_thickness_m"),
                        "effective_thickness_m": row.get("net_thickness_m"),
                        "layer_type": _dict(row.get("classification")).get("fluid_type"),
                    }
                    for row in interpretations
                ]
            },
            ["层段和厚度直接取自上传文件 interpretation_results"],
        ),
    }

    tests = _list(data.get("well_test_and_production"))
    consistency = [
        str(_dict(item).get("consistency_with_log_interpretation", "")).lower() for item in tests
    ]
    validation_status = (
        ValidationStatus.INSUFFICIENT_EVIDENCE
        if not consistency
        else ValidationStatus.CONSISTENT
        if all(item == "consistent" for item in consistency)
        else ValidationStatus.PARTIAL_CONFLICT
    )
    validation = {
        **_stage(
            source,
            {
                "well_test_and_production": tests,
                "offset_wells": data.get("offset_wells", {}),
            },
            ["综合验证资料来自上传文件，未访问外部数据源"],
        ),
        "validation_status": validation_status,
    }

    auxiliary = {
        "core": data.get("core_and_mud_logging", {}),
        "mud_logging": data.get("core_and_mud_logging", {}),
        "well_test": tests,
        "offset_well": data.get("offset_wells", {}),
        "geology": data.get("geology", {}),
    }
    fixture = {
        "schema_version": "0.1-skeleton",
        "is_mock": True,
        "well": {
            "well_id": well.get("well_id"),
            "name": well.get("well_name") or well.get("well_id"),
            "extensions": {
                "upload_schema_version": data.get("schema_version"),
                "upload_data_type": data.get("data_type"),
                "source_is_synthetic": True,
            },
        },
        "raw_data": {
            "depth_unit": "m",
            "depth_reference": "MD",
            "depths": depths,
            "curves": curves,
            "auxiliary": auxiliary,
            "extensions": {
                "curve_alias_dictionary": log_data.get("curve_alias_dictionary", {}),
                "sample_basis": "target_zone_statistics.mean",
            },
        },
        "requirements": {
            "required_curves": list(curves),
            "recommended_sources": ["core", "mud_logging", "well_test", "offset_well"],
        },
        "outputs": outputs,
        "validation": validation,
    }
    return MockFixture.model_validate(fixture)


def _stage(source: str, result: dict[str, Any], evidence: list[str]) -> dict[str, Any]:
    return {
        "status": "SUCCESS",
        "result": result,
        "evidence": evidence,
        "is_mock": True,
        "source": source,
    }


def _zone_view(row: dict[str, Any], key: str) -> dict[str, Any]:
    return {"zone_id": row.get("zone_id"), key: row.get(key, {})}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
