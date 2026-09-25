"""ToolRun 的有界审计投影；不复制原始井数据、模型上下文或任意附件。"""

import re
from collections.abc import Mapping

from pydantic import JsonValue

from cnlc_agent.domain.models import JsonObject
from cnlc_agent.domain.override import InterpretationOverride

MAX_TEXT = 128
MAX_ITEMS = 8
MAX_DEPTH = 4
SENSITIVE = re.compile(r"api.?key|token|password|secret|authorization|credential", re.I)
BEARER = re.compile(r"(?i)bearer\s+\S+|\bsk-[A-Za-z0-9_.-]+")
ASSIGNED_SECRET = re.compile(
    r"(?i)(?:api.?key|token|password|secret|authorization|credential)\s*[:=]\s*\S+"
)
BULK_FIELDS = {
    "depths", "curves", "values", "raw_data", "processed_data", "payload", "report",
    "instruction",
}
METADATA_KEYS = {
    "is_mock", "source", "fixture_source", "execution_id", "input_version_id",
    "prediction_model", "effective_parameters", "parameter_propagated",
    "professionally_recalculated", "preprocessing",
}


def bounded(value: object, depth: int = 0) -> JsonValue:
    """先按键脱敏，再限制深度、长度和数量；不保留任意对象 repr。"""

    if depth >= MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        return {
            str(key)[:MAX_TEXT]: (
                "[REDACTED]" if SENSITIVE.search(str(key)) else bounded(item, depth + 1)
            )
            for key, item in list(value.items())[:MAX_ITEMS]
            if str(key).lower() not in BULK_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [bounded(item, depth + 1) for item in value[:MAX_ITEMS]]
    if isinstance(value, str):
        return ASSIGNED_SECRET.sub("[REDACTED]", BEARER.sub("[REDACTED]", value))[:MAX_TEXT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[UNSUPPORTED]"


def tool_input_snapshot(well_id: str, step_id: str, parameters: JsonObject) -> JsonObject:
    """只保留正规执行参数，忽略意外的曲线、instruction 和敏感扩展字段。"""

    raw_override = parameters.get("effective_override")
    allowed = set(InterpretationOverride.model_fields)
    override = (
        {key: value for key, value in raw_override.items() if key in allowed}
        if isinstance(raw_override, dict)
        else {}
    )
    return {
        "well_id": bounded(well_id),
        "step_id": step_id,
        "execution_context": {
            "execution_id": bounded(parameters.get("execution_id")),
            "input_version_id": bounded(parameters.get("input_version_id")),
            "effective_override": bounded(override),
        },
    }


def tool_output_snapshot(
    status: str, data: JsonObject, warnings: list[str], metadata: JsonObject
) -> JsonObject:
    """只记字段名与白名单元数据；W01 的 raw_data/曲线值从不进入快照。"""

    result = data.get("result")
    return {
        "status": status,
        "data_keys": bounded(list(data)),
        "result_keys": bounded(list(result)) if isinstance(result, dict) else [],
        "warnings": bounded(warnings),
        "metadata": bounded(
            {key: value for key, value in metadata.items() if key in METADATA_KEYS}
        ),
    }
