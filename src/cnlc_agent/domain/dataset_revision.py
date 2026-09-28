"""数据集版本与增量修改契约；版本只保存 lineage metadata。"""

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import uuid4

from pydantic import Field, model_validator

from cnlc_agent.domain.errors import DataError
from cnlc_agent.domain.models import Contract, RawData, WellId, utc_now


class DatasetChangeType(StrEnum):
    """当前唯一开放的数据修改类型。"""

    CURVE_SAMPLE_PATCH = "CURVE_SAMPLE_PATCH"


class DatasetPatchSampleRequest(Contract):
    """调用方只提供真实深度和目标值，不提供可信索引或原值。"""

    depth_m: float
    new_value: float | None


class DatasetCurvePatchRequest(Contract):
    """一条曲线的外部修改请求。"""

    curve_code: str = Field(min_length=1, max_length=64)
    unit: str = Field(min_length=1, max_length=32)
    samples: list[DatasetPatchSampleRequest] = Field(min_length=1)


class DatasetPatchRequest(Contract):
    """一次显式基线 Patch 请求，可同时修改多条曲线。"""

    depth_unit: Literal["m"] = "m"
    depth_reference: Literal["MD"] = "MD"
    curves: list[DatasetCurvePatchRequest] = Field(min_length=1)


class DatasetSampleChange(Contract):
    """服务端解析后的单点变化，before_value 来自可信基线。"""

    sample_index: int = Field(ge=0)
    depth_m: float
    before_value: float | None
    after_value: float | None


class DatasetCurveChange(Contract):
    """ChangeSet 中一条曲线的稀疏变化。"""

    curve_code: str = Field(min_length=1, max_length=64)
    unit: str = Field(min_length=1, max_length=32)
    samples: list[DatasetSampleChange] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_samples(self) -> Self:
        indexes = [sample.sample_index for sample in self.samples]
        if len(indexes) != len(set(indexes)):
            raise ValueError("duplicate sample_index in curve change")
        return self


class DatasetChangeSet(Contract):
    """不可变的增量事实；curve_changes 只包含被修改的采样点。"""

    change_set_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    task_id: str = Field(min_length=1)
    well_id: WellId
    base_revision_id: str = Field(min_length=1)
    change_type: DatasetChangeType = DatasetChangeType.CURVE_SAMPLE_PATCH
    curve_changes: list[DatasetCurveChange] = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: str = Field(min_length=1)
    source_execution_id: str | None = None
    reason: str | None = None
    original_instruction: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def unique_curves(self) -> Self:
        """一个 ChangeSet 内每条曲线只出现一次，避免单位和点位语义冲突。"""

        curve_codes = [change.curve_code for change in self.curve_changes]
        if len(curve_codes) != len(set(curve_codes)):
            raise ValueError("duplicate curve_code in change set")
        return self


class DatasetRevision(Contract):
    """不可变 Dataset lineage 节点；不携带 RawData 或完整曲线。"""

    dataset_revision_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    task_id: str = Field(min_length=1)
    well_id: WellId
    sequence: int = Field(ge=1)
    root_input_version_id: str = Field(min_length=1)
    parent_revision_id: str | None = None
    change_set_id: str | None = None
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_from_execution_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def root_or_child(self) -> Self:
        if (self.parent_revision_id is None) != (self.change_set_id is None):
            raise ValueError("root and child revision references must be paired")
        return self


def root_lineage(content_sha256: str) -> str:
    """根 lineage 表达 InputVersion 身份，不冒充物化数据内容摘要。"""

    return hashlib.sha256(content_sha256.encode("ascii")).hexdigest()


def child_lineage(parent_lineage: str, change_set_sha256: str) -> str:
    """子 lineage 由父 lineage 和增量摘要共同确定。"""

    value = f"{parent_lineage}:{change_set_sha256}"
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def change_set_digest(changes: list[DatasetCurveChange]) -> str:
    """只序列化服务端解析后的持久字段，并消除列表输入顺序差异。"""

    canonical_changes = [
        {
            "curve_code": change.curve_code,
            "unit": change.unit,
            "samples": [
                {
                    "sample_index": sample.sample_index,
                    "depth_m": sample.depth_m,
                    "before_value": sample.before_value,
                    "after_value": sample.after_value,
                }
                for sample in sorted(change.samples, key=lambda item: item.sample_index)
            ],
        }
        for change in sorted(changes, key=lambda item: (item.curve_code, item.unit))
    ]
    payload = json.dumps(
        canonical_changes,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_curve_changes(raw_data: RawData, changes: list[DatasetCurveChange]) -> RawData:
    """校验稀疏事实后修改深拷贝，永不改写 InputVersion.payload。"""

    result = raw_data.model_copy(deep=True)
    for curve_change in changes:
        curve = result.curves.get(curve_change.curve_code)
        if curve is None or curve.unit != curve_change.unit:
            raise DataError("INVALID_STORED_CHANGE_SET", "ChangeSet 曲线或单位与基线不一致")
        for sample in curve_change.samples:
            if sample.sample_index >= len(result.depths):
                raise DataError("INVALID_STORED_CHANGE_SET", "ChangeSet 采样索引越界")
            if result.depths[sample.sample_index] != sample.depth_m:
                raise DataError("INVALID_STORED_CHANGE_SET", "ChangeSet 深度与采样索引不一致")
            if curve.values[sample.sample_index] != sample.before_value:
                raise DataError("INVALID_STORED_CHANGE_SET", "ChangeSet 原值与基线不一致")
            curve.values[sample.sample_index] = sample.after_value
    return result
