"""GDSX 上传工件必须和输入版本绑定，且运行前重新校验内容摘要。"""

import pytest
from agentscope.message import Base64Source, DataBlock, UserMsg

from cnlc_agent.demo.uploads import UploadedGdsx, parse_uploaded_input
from cnlc_agent.domain.inputs import InterpretationInputVersion
from cnlc_agent.infrastructure.gdsx_artifacts import GdsxArtifactStore


def test_binary_hdf5_attachment_is_classified_as_gdsx():
    uploaded, _ = parse_uploaded_input([
        UserMsg(name="user", content=[DataBlock(source=Base64Source(data="iUhERg0KGgphY29udGVudA==", media_type="application/octet-stream"))])
    ])

    assert isinstance(uploaded, UploadedGdsx)
    assert uploaded.well_id.startswith("UPLOAD_GDSX_")


@pytest.mark.asyncio
async def test_gdsx_artifact_resolve_checks_digest(tmp_path):
    store = GdsxArtifactStore(tmp_path)
    artifact, digest = store.save(b"\x89HDF\r\n\x1a\ncontent")
    version = InterpretationInputVersion(
        task_id="task-1", well_id="UPLOAD_GDSX_TEST", sequence=1,
        source_type="GDSX", content_sha256=digest, gdsx_artifact=artifact,
    )

    path = await store.resolve(version)
    assert path.is_file()
    path.write_bytes(b"changed")
    with pytest.raises(Exception, match="校验失败"):
        await store.resolve(version)
