"""当前 Demo 的规范化井输入版本；原始附件与临时路径不进入持久快照。"""

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from cnlc_agent.domain.models import Contract, MockFixture, WellId, utc_now

InputSource = Literal["UPLOAD", "FIXTURE", "GDSX"]


class GdsxArtifact(Contract):
    """已上传 GDSX 的受控存储引用；不保存浏览器临时路径。"""

    storage_key: str = Field(min_length=1)
    original_filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)


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
    """持续任务的一份不可变输入事实，可为 Fixture 或上传的 GDSX。"""

    input_version_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    task_id: str = Field(min_length=1)
    well_id: WellId
    sequence: int = Field(ge=1)
    source_type: InputSource
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: MockFixture | None = None
    gdsx_artifact: GdsxArtifact | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_payload(self) -> "InterpretationInputVersion":
        """防止跨井快照或错误摘要进入版本历史。"""

        if self.source_type == "GDSX":
            if self.payload is not None or self.gdsx_artifact is None:
                raise ValueError("GDSX input must contain exactly one artifact reference")
            return self
        if self.payload is None or self.gdsx_artifact is not None:
            raise ValueError("fixture input must contain exactly one normalized payload")
        if self.well_id != self.payload.well.well_id:
            raise ValueError("input well_id must match payload")
        if self.content_sha256 != fixture_digest(self.payload):
            raise ValueError("input digest must match normalized payload")
        return self
