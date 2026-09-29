"""GDSX 上传应用服务：校验二进制后原子创建首组业务事实。"""

from pathlib import Path
from uuid import uuid4

from cnlc_agent.application.ports import ArtifactStore, TaskRepository
from cnlc_agent.domain.artifacts import Artifact, ArtifactKind, GdsxUploadReceipt
from cnlc_agent.domain.errors import DataError
from cnlc_agent.domain.execution import Execution
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.gdsx_manifest import HDF5_MAGIC, inspect_gdsx


class GdsxIngressService:
    """上传内容不进入状态或数据库，只把逻辑引用和 manifest 交给 Repository。"""

    def __init__(
        self,
        repository: TaskRepository,
        store: ArtifactStore,
        *,
        max_upload_bytes: int,
    ) -> None:
        self.repository = repository
        self.store = store
        self.max_upload_bytes = max_upload_bytes

    async def ingest(
        self,
        content: bytes,
        *,
        filename: str,
        media_type: str,
        instruction: str,
        well_id: str | None = None,
    ) -> Execution:
        """创建默认 STAGED_CONFIRMATION 执行；同内容只复用物理对象，不复用授权。"""

        receipt = await self.stage(
            content,
            filename=filename,
            media_type=media_type,
            user_id="memory-demo",
            agent_id="memory-demo",
            session_id="memory-demo",
        )
        return await self.create_task(receipt, instruction=instruction, well_id=well_id)

    async def stage(
        self,
        content: bytes,
        *,
        filename: str,
        media_type: str,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> GdsxUploadReceipt:
        """multipart 阶段只保存 blob 与小型收据，不创建 Conversation 或 Task。"""

        if len(content) > self.max_upload_bytes:
            raise DataError("GDSX_UPLOAD_TOO_LARGE", "GDSX 文件超过独立上传大小限制")
        if not content:
            raise DataError("GDSX_FILE_INVALID", "GDSX 文件不能为空")
        safe_name = Path(filename).name
        allowed_media = {"application/x-hdf5", "application/octet-stream"}
        if (
            Path(safe_name).suffix.casefold() != ".gdsx"
            or media_type.casefold() not in allowed_media
            or content[: len(HDF5_MAGIC)] != HDF5_MAGIC
        ):
            raise DataError("GDSX_FILE_INVALID", "附件类型或 HDF5 文件头无效")
        stored = await self.store.put(content)
        receipt_id = str(uuid4())
        async with self.store.materialize(stored) as path:
            manifest = inspect_gdsx(path, receipt_id)
        receipt = GdsxUploadReceipt(
            artifact_id=receipt_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            filename=safe_name,
            media_type=media_type,
            storage_key=stored.storage_key,
            size_bytes=stored.size_bytes,
            content_sha256=stored.content_sha256,
            manifest=manifest,
        )
        await self.store.save_upload_receipt(receipt)
        return receipt

    async def create_task(
        self,
        receipt: GdsxUploadReceipt,
        *,
        instruction: str,
        well_id: str | None = None,
    ) -> Execution:
        """任务创建只消费已持久化收据，不再接收 GDSX bytes。"""

        technical_well_id = well_id or f"GDSX_{receipt.content_sha256[:24].upper()}"
        request = TaskRequest(well_id=technical_well_id, instruction=instruction)
        state = InterpretationState(task=request, mode="demo")
        artifact = Artifact(
            artifact_id=receipt.artifact_id,
            task_id=request.task_id,
            well_id=request.well_id,
            kind=ArtifactKind.SOURCE_GDSX,
            media_type=receipt.media_type,
            original_filename=receipt.filename,
            size_bytes=receipt.size_bytes,
            content_sha256=receipt.content_sha256,
            storage_key=receipt.storage_key,
        )
        async with self.store.materialize(receipt) as path:
            manifest = inspect_gdsx(path, artifact.artifact_id)
        version, revision, execution = await self.repository.create_gdsx_ingress(
            state, artifact, manifest
        )
        # 防御性检查：Repository 返回身份必须和保存内容一致。
        if version.content_sha256 != receipt.content_sha256:
            raise DataError("GDSX_INGRESS_INCONSISTENT", "GDSX 输入摘要不一致")
        if execution.state_snapshot.dataset_revision_id != revision.dataset_revision_id:
            raise DataError("GDSX_INGRESS_INCONSISTENT", "根数据集版本引用不一致")
        return execution
