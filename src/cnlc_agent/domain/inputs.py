"""当前 Demo 的规范化井输入版本；原始附件与临时路径不进入持久快照。"""

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from cnlc_agent.domain.artifacts import GdsxDatasetManifest
from cnlc_agent.domain.models import Contract, MockFixture, WellId, utc_now

InputSource = Literal["UPLOAD", "FIXTURE"]


class InputPayloadKind(StrEnum):
    """输入正文位置；历史行默认解释为 FIXTURE。"""

    FIXTURE = "FIXTURE"
    GDSX_ARTIFACT = "GDSX_ARTIFACT"


def fixture_digest(fixture: MockFixture) -> str:
    """仅对已校验的规范化内容排序序列化，保证等价输入得到相同 SHA-256。"""

    canonical = json.dumps(
        fixture.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class InterpretationInputVersion(Contract):
    """持续任务下的输入事实；Fixture 与 Artifact 路径互斥并保持旧数据兼容。"""

    input_version_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    task_id: str = Field(min_length=1)
    well_id: WellId
    sequence: int = Field(ge=1)
    source_type: InputSource
    payload_kind: InputPayloadKind = InputPayloadKind.FIXTURE
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: MockFixture | None = None
    source_artifact_id: str | None = None
    dataset_manifest: GdsxDatasetManifest | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_payload(self) -> "InterpretationInputVersion":
        """防止跨井快照或错误摘要进入版本历史。"""

        if self.payload_kind == InputPayloadKind.FIXTURE:
            if (
                self.payload is None
                or self.source_artifact_id is not None
                or self.dataset_manifest is not None
            ):
                raise ValueError("fixture input requires only payload")
            if self.well_id != self.payload.well.well_id:
                raise ValueError("input well_id must match payload")
            if self.content_sha256 != fixture_digest(self.payload):
                raise ValueError("input digest must match normalized payload")
        elif (
            self.payload is not None
            or self.source_artifact_id is None
            or self.dataset_manifest is None
        ):
            raise ValueError("artifact input requires artifact id and manifest without payload")
        elif self.dataset_manifest.artifact_id != self.source_artifact_id:
            raise ValueError("manifest artifact id must match input")
        return self
