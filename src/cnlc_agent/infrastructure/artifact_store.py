"""内容寻址的本地 Artifact Store；可由同一 Port 替换为共享对象存储。"""

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from cnlc_agent.domain.artifacts import GdsxUploadReceipt, StoredArtifact
from cnlc_agent.domain.errors import InfrastructureError


class FilesystemArtifactStore:
    """在配置根目录原子写入 GDSX；上传文件名从不参与物理路径构造。"""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        if self.root not in candidate.parents:
            raise InfrastructureError("ARTIFACT_STORAGE_KEY_INVALID", "制品存储键越界")
        return candidate

    async def put(self, content: bytes) -> StoredArtifact:
        """先写临时文件并 fsync，再原子 rename 到内容寻址目标。"""

        if not content:
            raise InfrastructureError("ARTIFACT_EMPTY", "制品内容不能为空")
        digest = hashlib.sha256(content).hexdigest()
        key = f"sha256/{digest[:2]}/{digest}.gdsx"
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            descriptor, temporary = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return StoredArtifact(storage_key=key, size_bytes=len(content), content_sha256=digest)

    async def exists(self, storage_key: str) -> bool:
        return self._path(storage_key).is_file()

    async def verify(self, stored: StoredArtifact) -> bool:
        path = self._path(stored.storage_key)
        if not path.is_file() or path.stat().st_size != stored.size_bytes:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == stored.content_sha256

    async def save_upload_receipt(self, receipt: GdsxUploadReceipt) -> None:
        """原子保存无 bytes 的上传收据；它不是 Task 授权或 Artifact metadata。"""

        directory = self.root / "receipts"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{receipt.artifact_id}.json"
        descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(receipt.model_dump(mode="json"), stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    async def get_upload_receipt(
        self, artifact_id: str, *, user_id: str, agent_id: str, session_id: str
    ) -> GdsxUploadReceipt | None:
        """按完整会话身份读取收据，文件名只接受 UUID 形状的业务 ID。"""

        if not artifact_id or any(char not in "0123456789abcdef-" for char in artifact_id):
            return None
        path = self.root / "receipts" / f"{artifact_id}.json"
        if not path.is_file():
            return None
        try:
            receipt = GdsxUploadReceipt.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise InfrastructureError("ARTIFACT_INTEGRITY_FAILED", "上传收据损坏") from None
        if (receipt.user_id, receipt.agent_id, receipt.session_id) != (
            user_id,
            agent_id,
            session_id,
        ):
            return None
        return receipt

    @asynccontextmanager
    async def materialize(self, stored: StoredArtifact) -> AsyncIterator[Path]:
        """校验 size/SHA 后生成临时副本，损坏内容绝不交给第三方 API。"""

        source = self._path(stored.storage_key)
        if not await self.verify(stored):
            raise InfrastructureError("ARTIFACT_INTEGRITY_FAILED", "制品内容缺失或完整性校验失败")
        with tempfile.TemporaryDirectory(prefix="cnlc-gdsx-") as directory:
            target = Path(directory) / "source.gdsx"
            shutil.copyfile(source, target)
            copied_digest = hashlib.sha256()
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    copied_digest.update(chunk)
            if (
                target.stat().st_size != stored.size_bytes
                or copied_digest.hexdigest() != stored.content_sha256
            ):
                raise InfrastructureError(
                    "ARTIFACT_INTEGRITY_FAILED", "制品复制期间完整性发生变化"
                )
            yield target
