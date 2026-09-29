"""真实公司 Provider 调用的有界持久事实；不承载文件字节或业务版本。"""

import json
import math
from datetime import datetime
from enum import StrEnum
from typing import Self, cast
from uuid import uuid4

from pydantic import Field, JsonValue, model_validator

from cnlc_agent.domain.models import Contract, JsonObject, utc_now

MAX_PROVIDER_NORMALIZED_RESULT_BYTES = 512 * 1024
MAX_PROVIDER_REQUEST_SUMMARY_BYTES = 16 * 1024
MAX_PROVIDER_JSON_DEPTH = 10
_SENSITIVE_KEYS = {
    "authorization",
    "token",
    "password",
    "secret",
    "credential",
    "serverpath",
    "filepath",
    "gdsxcontent",
}


class CompanyProviderOperation(StrEnum):
    """当前已识别的公司批量能力；不等同于四个业务阶段。"""

    ANALYSIS = "analysis"
    PREPROCESSING = "preprocessing"
    INTERPRETATION = "interpretation"
    REPORT = "report"


class CompanyProviderCallStatus(StrEnum):
    """一次真实 Provider 物理调用的生命周期。"""

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


def _validate_safe_json(value: JsonValue, *, max_bytes: int, label: str) -> None:
    """拒绝敏感字段、路径、非有限数及无界 JSON，避免污染 JSONB。"""

    def walk(item: JsonValue, depth: int = 0) -> None:
        if depth > MAX_PROVIDER_JSON_DEPTH:
            raise ValueError(f"{label} nesting is too deep")
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = key.replace("_", "").replace("-", "").casefold()
                if normalized in _SENSITIVE_KEYS:
                    raise ValueError(f"{label} contains forbidden field")
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{label} contains non-finite number")
        elif isinstance(item, str) and (
            item.startswith(("/", "~/", "file://"))
            or (len(item) >= 3 and item[1:3] in {":/", ":\\"})
        ):
            raise ValueError(f"{label} contains local or server path")

    walk(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON") from exc
    if len(encoded) > max_bytes:
        raise ValueError("PROVIDER_RESULT_TOO_LARGE")


class CompanyProviderCall(Contract):
    """跨进程恢复真实调用所需的最小事实，独立于有损 ToolRun 摘要。"""

    provider_call_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=128)
    external_call_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=128)
    task_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    input_version_id: str = Field(min_length=1)
    provider_operation: CompanyProviderOperation
    status: CompanyProviderCallStatus = CompanyProviderCallStatus.RUNNING
    request_summary: JsonObject = Field(default_factory=dict)
    normalized_result: JsonObject = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    error_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_persisted_boundary(self) -> Self:
        """运行态和终态必须自洽，所有可持久 JSON 都经过同一安全边界。"""

        _validate_safe_json(
            cast(JsonValue, self.request_summary),
            max_bytes=MAX_PROVIDER_REQUEST_SUMMARY_BYTES,
            label="provider request summary",
        )
        _validate_safe_json(
            cast(JsonValue, self.normalized_result),
            max_bytes=MAX_PROVIDER_NORMALIZED_RESULT_BYTES,
            label="provider normalized result",
        )
        if self.status == CompanyProviderCallStatus.RUNNING:
            if self.finished_at is not None or self.error_code is not None:
                raise ValueError("running provider call cannot have terminal fields")
            if self.normalized_result:
                raise ValueError("running provider call cannot have normalized result")
        else:
            if self.finished_at is None:
                raise ValueError("terminal provider call requires finished_at")
            if self.status == CompanyProviderCallStatus.SUCCESS:
                if self.error_code is not None or not self.normalized_result:
                    raise ValueError("successful provider call requires result without error")
            elif not self.error_code or self.normalized_result:
                raise ValueError("unsuccessful provider call requires error without result")
        return self


class RealPredictionContext(Contract):
    """真实预测显式配置；不包含预处理产生的 logReqJson。"""

    well_name: str = Field(min_length=1, max_length=128)
    service_id: str = Field(min_length=1, max_length=128)
    task_config: JsonObject

    @model_validator(mode="after")
    def validate_context(self) -> Self:
        _validate_safe_json(
            cast(JsonValue, self.task_config),
            max_bytes=MAX_PROVIDER_REQUEST_SUMMARY_BYTES,
            label="prediction task config",
        )
        if not self.task_config:
            raise ValueError("prediction task config cannot be empty")
        return self
