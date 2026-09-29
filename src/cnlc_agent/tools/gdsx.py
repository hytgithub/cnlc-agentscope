"""Artifact-backed GDSX W01 解编 Tool；只返回 manifest，不返回曲线采样值。"""

from cnlc_agent.application.ports import ArtifactStore, TaskRepository
from cnlc_agent.domain.artifacts import StoredArtifact
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import ToolError
from cnlc_agent.domain.inputs import InputPayloadKind
from cnlc_agent.domain.models import DataRequirements, Well
from cnlc_agent.domain.tool_run import ToolExecutionMode
from cnlc_agent.infrastructure.gdsx_manifest import inspect_gdsx
from cnlc_agent.tools.contracts import Tool, ToolInput, ToolOutput


class GdsxArtifactWellDataTool:
    """按当前 InputVersion 和 Task 归属读取 Artifact；Fixture 继续走原实现。"""

    name = "get_well_data"
    execution_mode = ToolExecutionMode.REAL
    source = "gdsx:artifact:decode"

    def __init__(
        self,
        repository: TaskRepository,
        store: ArtifactStore,
        fallback: Tool,
        required_curves: tuple[str, ...] = (),
    ) -> None:
        self.repository = repository
        self.store = store
        self.fallback = fallback
        self.required_curves = required_curves

    async def execute(self, request: ToolInput) -> ToolOutput:
        input_version_id = request.parameters.get("input_version_id")
        if not isinstance(input_version_id, str):
            return await self.fallback.execute(request)
        version = await self.repository.get_input_version(input_version_id)
        if version is None or version.task_id != request.task_id:
            raise ToolError("INPUT_VERSION_TASK_MISMATCH", "输入版本不属于当前任务")
        if version.payload_kind != InputPayloadKind.GDSX_ARTIFACT:
            return await self.fallback.execute(request)
        assert version.source_artifact_id is not None
        artifact = await self.repository.get_artifact(request.task_id, version.source_artifact_id)
        if artifact is None or artifact.well_id != request.well_id:
            raise ToolError("ARTIFACT_TASK_MISMATCH", "源 GDSX 制品不属于当前任务或井")
        async with self.store.materialize(
            StoredArtifact(
                storage_key=artifact.storage_key,
                size_bytes=artifact.size_bytes,
                content_sha256=artifact.content_sha256,
            )
        ) as path:
            manifest = inspect_gdsx(path, artifact.artifact_id)
        if manifest != version.dataset_manifest:
            raise ToolError("GDSX_MANIFEST_MISMATCH", "GDSX 清单与输入版本不一致")
        well = Well(
            well_id=version.well_id,
            name=version.well_id,
            extensions={"technical_id": True, "source_artifact_id": artifact.artifact_id},
        )
        requirements = DataRequirements(required_curves=list(self.required_curves))
        return ToolOutput(
            status=StepStatus.WARNING if manifest.warnings else StepStatus.SUCCESS,
            data={
                "well": well.model_dump(mode="json"),
                "requirements": requirements.model_dump(mode="json"),
                "dataset_manifest": manifest.model_dump(mode="json"),
                "source_artifact_id": artifact.artifact_id,
            },
            warnings=list(manifest.warnings),
            metadata={
                "is_mock": False,
                "source": self.source,
                "artifact_id": artifact.artifact_id,
                "input_version_id": version.input_version_id,
            },
        )
