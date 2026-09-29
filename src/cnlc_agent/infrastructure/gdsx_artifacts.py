"""GDSX 上传工件的受控存储与任务绑定解析。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from cnlc_agent.domain.errors import DataError, InfrastructureError
from cnlc_agent.domain.inputs import GdsxArtifact, InterpretationInputVersion


class GdsxArtifactStore:
    """以随机对象标识保存上传文件；执行时只按 InputVersion 受控解析。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def save(self, content: bytes, original_filename: str = "uploaded.gdsx") -> tuple[GdsxArtifact, str]:
        if not content:
            raise DataError("GDSX_FILE_EMPTY", "上传的 GDSX 文件为空")
        digest = hashlib.sha256(content).hexdigest()
        storage_key = f"input-artifacts/{uuid4().hex}/source.gdsx"
        target = self.root / storage_key
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        except OSError as exc:
            raise InfrastructureError("GDSX_ARTIFACT_WRITE_FAILED", "上传文件保存失败") from exc
        return GdsxArtifact(storage_key=storage_key, original_filename=Path(original_filename).name or "uploaded.gdsx", size_bytes=len(content)), digest

    async def resolve(self, version: InterpretationInputVersion) -> Path:
        if version.source_type != "GDSX" or version.gdsx_artifact is None:
            raise DataError("GDSX_INPUT_REQUIRED", "当前执行没有绑定 GDSX 输入文件")
        path = (self.root / version.gdsx_artifact.storage_key).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            raise InfrastructureError("GDSX_ARTIFACT_PATH_INVALID", "输入文件存储引用无效") from None
        if not path.is_file() or path.suffix.lower() != ".gdsx":
            raise InfrastructureError("GDSX_ARTIFACT_NOT_FOUND", "上传的 GDSX 文件不存在")
        if hashlib.sha256(path.read_bytes()).hexdigest() != version.content_sha256:
            raise InfrastructureError("GDSX_ARTIFACT_TAMPERED", "上传的 GDSX 文件校验失败")
        return path


class InputArtifactResolver:
    """真实专业 Tool 的受控输入解析器，拒绝环境变量或浏览器路径注入。"""

    def __init__(self, repository, store: GdsxArtifactStore) -> None:
        self.repository = repository
        self.store = store

    async def resolve_gdsx(self, task_id: str, input_version_id: str) -> Path:
        version = await self.repository.get_input_version(input_version_id)
        if version is None:
            raise InfrastructureError("INPUT_VERSION_NOT_FOUND", "输入版本不存在")
        if version.task_id != task_id:
            raise DataError("INPUT_VERSION_TASK_MISMATCH", "输入版本不属于当前任务")
        return await self.store.resolve(version)
