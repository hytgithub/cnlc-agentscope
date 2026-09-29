"""Task 11G 的 Artifact Store、manifest 和原子 ingress 验收。"""

import base64
import hashlib
import json
from pathlib import Path

import h5py
import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.dataset_revision_service import DatasetRevisionService
from cnlc_agent.application.gdsx_ingress import GdsxIngressService
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.demo.agentscope_app import RejectDurableGdsxDataBlockMiddleware
from cnlc_agent.domain.artifacts import ArtifactKind, StoredArtifact
from cnlc_agent.domain.company_provider import CompanyProviderCall, CompanyProviderOperation
from cnlc_agent.domain.errors import DataError, InfrastructureError
from cnlc_agent.domain.execution import ExecutionRunMode
from cnlc_agent.domain.inputs import InputPayloadKind
from cnlc_agent.domain.stages import InterpretationStage, StageRunStatus
from cnlc_agent.domain.tool_run import ToolExecutionMode
from cnlc_agent.infrastructure.artifact_store import FilesystemArtifactStore
from cnlc_agent.infrastructure.company_api import CompanyApiClient, CompanyApiSettings
from cnlc_agent.infrastructure.company_provider import (
    ArtifactCompanyInputResolver,
    ArtifactCompanySink,
    CompanyProviderSettings,
    RealPredictionContext,
)
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository


class _UnusedResolver:
    async def resolve_gdsx(self, task_id, execution_id, input_version_id):
        raise AssertionError("DECODE 不应请求 company preprocessing path")

    async def release_gdsx(self, path):
        raise AssertionError("DECODE 不应释放 company preprocessing path")

    async def resolve_preprocess_operations(self, task_id, execution_id, input_version_id):
        return {}

    async def resolve_prediction_context(self, task_id, execution_id, input_version_id):
        return RealPredictionContext(well_name="unused", service_id="unused", task_config={})


def _json_dataset(value: object) -> np.ndarray:
    return np.frombuffer(json.dumps(value).encode(), dtype=np.uint8).reshape(-1, 1)


def _minimal_gdsx(path: Path) -> bytes:
    with h5py.File(path, "w") as file:
        well = file.create_group("well-visit")
        info = well.create_group("info")
        info.create_dataset("wellinfo", data=_json_dataset({"name": "pending-sample"}))
        curves = well.create_group("curve")
        curve = curves.create_group("GR")
        curve.create_dataset(
            "meta",
            data=_json_dataset(
                {
                    "name": "GR",
                    "standardName": "GR",
                    "dimension": 1,
                    "dimension1Unit": "m",
                    "dimension1Start": 1000.0,
                    "dimension1End": 1001.0,
                    "dimension1Step": 0.5,
                    "dimension1Length": 3,
                }
            ),
        )
        well.create_group("table")
    return path.read_bytes()


@pytest.mark.asyncio
async def test_gdsx_ingress_persists_references_without_curve_values(tmp_path: Path) -> None:
    source = tmp_path / "sample.gdsx"
    content = _minimal_gdsx(source)
    repository = InMemoryTaskRepository()
    store = FilesystemArtifactStore(tmp_path / "objects")
    service = GdsxIngressService(repository, store, max_upload_bytes=2 * 1024 * 1024)

    execution = await service.ingest(
        content,
        filename="../../sample.gdsx",
        media_type="application/x-hdf5",
        instruction="解编这口井",
    )

    assert execution.run_mode == ExecutionRunMode.STAGED_CONFIRMATION
    state = execution.state_snapshot
    assert state.dataset_revision_id is not None
    assert state.dataset_manifest is not None
    assert state.dataset_manifest.curve_count == 1
    assert "values" not in state.dataset_manifest.model_dump_json()
    versions = await repository.list_input_versions(execution.task_id)
    assert versions[0].payload_kind == InputPayloadKind.GDSX_ARTIFACT
    assert versions[0].payload is None
    artifacts = await repository.list_artifacts(execution.task_id)
    assert artifacts[0].kind == ArtifactKind.SOURCE_GDSX
    assert artifacts[0].original_filename == "sample.gdsx"
    assert artifacts[0].content_sha256 == hashlib.sha256(content).hexdigest()
    assert artifacts[0].size_bytes == len(content)
    assert not Path(artifacts[0].storage_key).is_absolute()
    encoded = base64.b64encode(content).decode()
    durable_models = "\n".join(
        [
            execution.model_dump_json(),
            state.model_dump_json(),
            versions[0].model_dump_json(),
            artifacts[0].model_dump_json(),
        ]
    )
    assert encoded not in durable_models
    with pytest.raises(DataError, match="当前不支持"):
        await DatasetRevisionService(repository).materialize(
            execution.task_id, state.dataset_revision_id
        )


@pytest.mark.asyncio
async def test_gdsx_ingress_rejects_invalid_and_oversized_files(tmp_path: Path) -> None:
    repository = InMemoryTaskRepository()
    service = GdsxIngressService(
        repository, FilesystemArtifactStore(tmp_path / "objects"), max_upload_bytes=8
    )
    with pytest.raises(DataError) as oversized:
        await service.ingest(
            b"123456789",
            filename="bad.gdsx",
            media_type="application/octet-stream",
            instruction="decode",
        )
    assert oversized.value.code == "GDSX_UPLOAD_TOO_LARGE"
    with pytest.raises(DataError) as invalid:
        await GdsxIngressService(
            repository, FilesystemArtifactStore(tmp_path / "objects2"), max_upload_bytes=1024
        ).ingest(
            b"not-hdf5",
            filename="bad.gdsx",
            media_type="application/octet-stream",
            instruction="decode",
        )
    assert invalid.value.code == "GDSX_FILE_INVALID"


@pytest.mark.asyncio
async def test_same_bytes_share_storage_but_not_task_authorization(tmp_path: Path) -> None:
    source = tmp_path / "sample.gdsx"
    content = _minimal_gdsx(source)
    repository = InMemoryTaskRepository()
    store = FilesystemArtifactStore(tmp_path / "objects")
    service = GdsxIngressService(repository, store, max_upload_bytes=len(content) + 1)
    first = await service.ingest(
        content,
        filename="one.gdsx",
        media_type="application/x-hdf5",
        instruction="decode",
    )
    second = await service.ingest(
        content,
        filename="two.gdsx",
        media_type="application/x-hdf5",
        instruction="decode",
    )
    first_artifact = (await repository.list_artifacts(first.task_id))[0]
    second_artifact = (await repository.list_artifacts(second.task_id))[0]
    assert first_artifact.storage_key == second_artifact.storage_key
    assert first_artifact.artifact_id != second_artifact.artifact_id
    assert await repository.get_artifact(first.task_id, second_artifact.artifact_id) is None


@pytest.mark.asyncio
async def test_company_real_decode_uses_real_tool_and_real_revision_ref(tmp_path: Path) -> None:
    source = tmp_path / "sample.gdsx"
    content = _minimal_gdsx(source)
    repository = InMemoryTaskRepository()
    store = FilesystemArtifactStore(tmp_path / "objects")
    ingress = GdsxIngressService(repository, store, max_upload_bytes=len(content) + 1)
    execution = await ingress.ingest(
        content,
        filename="sample.gdsx",
        media_type="application/x-hdf5",
        instruction="decode",
    )
    api = CompanyApiClient(
        CompanyApiSettings(
            preprocessing_url="http://unused/preprocess",
            prediction_url="http://unused/predict",
            token=SecretStr("unused"),
            _env_file=None,
        ),
        transport=httpx.MockTransport(lambda _request: pytest.fail("DECODE 不应调用 company HTTP")),
    )
    app = build_application(
        AppSettings(
            mode="demo",
            professional_provider="company_real",
            model_provider="mock",
            artifact_root=tmp_path / "objects",
            _env_file=None,
        ),
        task_repository=repository,
        artifact_store=store,
        company_api_client=api,
        company_input_resolver=_UnusedResolver(),
    )
    try:
        state, _ = await app.execute_prepared(execution.execution_id, worker_id="decode-test")
    finally:
        await app.close()
    decode = next(run for run in state.stage_runs if run.stage == InterpretationStage.DECODE)
    assert decode.status == StageRunStatus.WAITING_CONFIRM
    assert decode.output_refs["dataset_revision_id"] == state.dataset_revision_id
    runs = await repository.list_tool_runs(execution.execution_id)
    real = next(run for run in runs if run.tool_code == "get_well_data")
    assert real.execution_mode == ToolExecutionMode.REAL
    assert real.source == "gdsx:artifact:decode"
    assert base64.b64encode(content).decode() not in real.model_dump_json()


@pytest.mark.asyncio
async def test_artifact_resolver_and_processed_sink_survive_store_restart(tmp_path: Path) -> None:
    source = tmp_path / "sample.gdsx"
    content = _minimal_gdsx(source)
    repository = InMemoryTaskRepository()
    root = tmp_path / "objects"
    ingress = GdsxIngressService(
        repository, FilesystemArtifactStore(root), max_upload_bytes=len(content) + 1
    )
    execution = await ingress.ingest(
        content,
        filename="sample.gdsx",
        media_type="application/x-hdf5",
        instruction="decode",
    )
    # 新 Store/Resolver 实例模拟服务重启，物化路径在 release 后必须清理。
    settings = CompanyProviderSettings(
        well_name="explicit",
        service_id="service",
        task_config={},
        preprocess_operations={"resample": {"enable": False}},
        _env_file=None,
    )
    resolver = ArtifactCompanyInputResolver(repository, FilesystemArtifactStore(root), settings)
    assert execution.input_version_id is not None
    path = await resolver.resolve_gdsx(
        execution.task_id, execution.execution_id, execution.input_version_id
    )
    assert path.read_bytes() == content
    await resolver.release_gdsx(path)
    assert not path.exists()

    call = await repository.create_company_provider_call(
        CompanyProviderCall(
            task_id=execution.task_id,
            execution_id=execution.execution_id,
            input_version_id=execution.input_version_id,
            provider_operation=CompanyProviderOperation.PREPROCESSING,
            request_summary={"well_id": execution.state_snapshot.task.well_id},
        )
    )
    assert base64.b64encode(content).decode() not in call.model_dump_json()
    source_artifact = (await repository.list_artifacts(execution.task_id))[0]
    processed_id = await ArtifactCompanySink(
        repository, FilesystemArtifactStore(root)
    ).save_processed_gdsx(
        execution.task_id,
        execution.execution_id,
        execution.input_version_id,
        content,
        source_artifact.artifact_id,
        call.provider_call_id,
    )
    processed = await repository.get_artifact(execution.task_id, processed_id)
    assert processed is not None
    assert processed.kind == ArtifactKind.PROCESSED_GDSX
    assert processed.source_artifact_id == source_artifact.artifact_id
    assert processed.provider_call_id == call.provider_call_id


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["modify", "truncate"])
async def test_materialize_rejects_corrupted_physical_blob(tmp_path: Path, mutation: str) -> None:
    source = tmp_path / "sample.gdsx"
    content = _minimal_gdsx(source)
    store = FilesystemArtifactStore(tmp_path / "objects")
    stored = await store.put(content)
    physical = store.root / stored.storage_key
    if mutation == "modify":
        changed = bytearray(content)
        changed[-1] ^= 1
        physical.write_bytes(changed)
    else:
        physical.write_bytes(content[:-8])
    with pytest.raises(InfrastructureError) as error:
        async with store.materialize(stored):
            pytest.fail("损坏制品不得产生临时 Path")
    assert error.value.code == "ARTIFACT_INTEGRITY_FAILED"


@pytest.mark.asyncio
async def test_failed_second_task_never_deletes_shared_blob(tmp_path: Path) -> None:
    source = tmp_path / "sample.gdsx"
    content = _minimal_gdsx(source)
    store = FilesystemArtifactStore(tmp_path / "objects")
    first_repo = InMemoryTaskRepository()
    first = await GdsxIngressService(first_repo, store, max_upload_bytes=len(content) + 1).ingest(
        content,
        filename="first.gdsx",
        media_type="application/x-hdf5",
        instruction="decode",
    )
    first_artifact = (await first_repo.list_artifacts(first.task_id))[0]

    class FailingRepository(InMemoryTaskRepository):
        async def create_gdsx_ingress(self, state, artifact, manifest):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "模拟事务失败")

    with pytest.raises(InfrastructureError):
        await GdsxIngressService(
            FailingRepository(), store, max_upload_bytes=len(content) + 1
        ).ingest(
            content,
            filename="second.gdsx",
            media_type="application/x-hdf5",
            instruction="decode",
        )
    async with store.materialize(
        StoredArtifact(
            storage_key=first_artifact.storage_key,
            size_bytes=first_artifact.size_bytes,
            content_sha256=first_artifact.content_sha256,
        )
    ) as materialized:
        assert materialized.read_bytes() == content


def test_durable_chat_rejects_gdsx_before_conversation_handler() -> None:
    downstream_called = False
    app = FastAPI()
    app.add_middleware(RejectDurableGdsxDataBlockMiddleware, enabled=True)

    @app.post("/chat/")
    async def chat() -> dict[str, bool]:
        nonlocal downstream_called
        downstream_called = True
        return {"stored": True}

    response = TestClient(app).post(
        "/chat/",
        json={
            "input": {
                "role": "user",
                "content": [
                    {
                        "type": "data",
                        "name": "well.gdsx",
                        "source": {
                            "type": "base64",
                            "media_type": "application/x-hdf5",
                            "data": "R0RTX0JZVEVT",
                        },
                    }
                ],
            }
        },
    )
    assert response.status_code == 415
    assert response.json()["detail"] == "GDSX_DURABLE_DATABLOCK_UNSUPPORTED"
    assert not downstream_called
