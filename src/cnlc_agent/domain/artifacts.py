"""GDSX 二进制制品及小型解编清单；禁止携带曲线采样值或物理路径。"""

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import Field, model_validator

from cnlc_agent.domain.models import Contract, JsonObject, WellId, utc_now


class ArtifactKind(StrEnum):
    """当前 GDSX 制品类型。"""

    SOURCE_GDSX = "SOURCE_GDSX"
    PROCESSED_GDSX = "PROCESSED_GDSX"


class Artifact(Contract):
    """数据库只保存制品元数据和逻辑 storage key，不保存文件字节。"""

    artifact_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    task_id: str = Field(min_length=1)
    well_id: WellId
    kind: ArtifactKind
    media_type: str = Field(min_length=1, max_length=128)
    original_filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_key: str = Field(pattern=r"^[a-z0-9][a-z0-9._/-]{1,511}$")
    source_artifact_id: str | None = None
    created_from_execution_id: str | None = None
    provider_call_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def logical_storage_key_only(self) -> "Artifact":
        """物理绝对路径、file URI 和路径穿越不得进入业务元数据。"""

        invalid_prefix = self.storage_key.startswith(("/", "~/", "file://"))
        if invalid_prefix or ".." in self.storage_key.split("/"):
            raise ValueError("artifact storage_key must be a logical key")
        processed_refs = (
            self.source_artifact_id,
            self.created_from_execution_id,
            self.provider_call_id,
        )
        if self.kind == ArtifactKind.SOURCE_GDSX and any(processed_refs):
            raise ValueError("source artifact cannot carry processed lineage")
        if self.kind == ArtifactKind.PROCESSED_GDSX and not all(processed_refs):
            raise ValueError("processed artifact requires source, execution and provider call")
        return self


class CurveManifest(Contract):
    """单条曲线的只读摘要，不包含 values。"""

    raw_name: str = Field(min_length=1, max_length=128)
    standard_name: str | None = Field(default=None, max_length=128)
    unit: str | None = Field(default=None, max_length=64)
    depth_start: float | None = None
    depth_end: float | None = None
    depth_step: float | None = None
    point_count: int = Field(ge=0)
    dimension: int = Field(ge=1, le=4)


class GdsxDatasetManifest(Contract):
    """GDSX 解编后可持久化的小型清单；字段映射等待真实样本确认。"""

    artifact_id: str = Field(min_length=1)
    size_bytes: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    well_info_summary: JsonObject = Field(default_factory=dict)
    curve_count: int = Field(ge=0)
    table_count: int = Field(ge=0)
    curves: list[CurveManifest] = Field(default_factory=list)
    table_names: list[str] = Field(default_factory=list)
    depth_summary: JsonObject = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def counts_match(self) -> "GdsxDatasetManifest":
        if self.curve_count != len(self.curves) or self.table_count != len(self.table_names):
            raise ValueError("manifest counts must match summaries")
        return self


class StoredArtifact(Contract):
    """ArtifactStore 写入结果；应用层据此创建归属元数据。"""

    storage_key: str
    size_bytes: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GdsxUploadReceipt(StoredArtifact):
    """multipart 上传后的短小持久收据；任务创建只需 artifact_id。"""

    artifact_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=128)
    manifest: GdsxDatasetManifest
    created_at: datetime = Field(default_factory=utc_now)
