"""当前 Demo 的规范化井输入版本；原始附件与临时路径不进入持久快照。"""

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from cnlc_agent.domain.models import Contract, MockFixture, WellId, utc_now

InputSource = Literal["UPLOAD", "FIXTURE"]


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
    """持续任务下的一份独立输入事实快照；本阶段只支持已校验 MockFixture。"""

    input_version_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    task_id: str = Field(min_length=1)
    well_id: WellId
    sequence: int = Field(ge=1)
    source_type: InputSource
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: MockFixture
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_payload(self) -> "InterpretationInputVersion":
        """防止跨井快照或错误摘要进入版本历史。"""

        if self.well_id != self.payload.well.well_id:
            raise ValueError("input well_id must match payload")
        if self.content_sha256 != fixture_digest(self.payload):
            raise ValueError("input digest must match normalized payload")
        return self
