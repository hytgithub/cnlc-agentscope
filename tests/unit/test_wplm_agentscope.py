"""验证测井工具迁移后的 AgentScope Schema、调用和失败状态。"""

import asyncio
import json
from pathlib import Path

import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import Toolkit, ToolResponse

from cnlc_agent.wplm.file_decode_tool import FileDecodeTool
from cnlc_agent.wplm.tools import GdsxCompletenessTool, WellLogServiceListTool


async def test_gdsx_tool_registers_and_reports_missing_file() -> None:
    tool = GdsxCompletenessTool()
    toolkit = Toolkit(tools=[tool, FileDecodeTool(), WellLogServiceListTool()])
    schemas = await toolkit.get_tool_schemas()
    assert [item["function"]["name"] for item in schemas] == [
        "gdsx_completeness", "file_decode", "welllog_service_list"
    ]
    schema = schemas[0]["function"]["parameters"]
    assert schema["required"] == ["gdsx_path"]
    assert schema["properties"]["need_chart"]["type"] == "boolean"

    chunks = [
        item
        async for item in toolkit.call_tool(
            ToolCallBlock(
                id="missing-file",
                name=tool.name,
                input=json.dumps({"gdsx_path": "/does/not/exist.gdsx"}),
            ),
            AgentState(),
        )
    ]
    response = chunks[-1]
    assert isinstance(response, ToolResponse)
    assert response.state == ToolResultState.ERROR
    assert json.loads(response.content[0].text)["success"] is False


async def test_file_decode_missing_input_is_error() -> None:
    response = await FileDecodeTool().call(raw_file_path="/does/not/exist.gdsx")
    assert response.state == ToolResultState.ERROR
    assert json.loads(response.content[0].text)["success"] is False


async def test_file_decode_preserves_gdsx_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / "well.gdsx"
    await asyncio.to_thread(original.write_bytes, b"original")

    class FakeResponse:
        """模拟上传、转换与下载服务返回，不进行网络请求。"""

        def __init__(self, payload: dict | None = None, content: bytes = b"") -> None:
            self.payload = payload or {}
            self.content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    def fake_post(url: str, **_kwargs: object) -> FakeResponse:
        if url.endswith("/upload"):
            return FakeResponse({"data": {"filePath": "uploaded"}})
        if url.endswith("/convert"):
            return FakeResponse({"code": 200, "data": {"result": "converted"}})
        return FakeResponse(content=b"converted")

    monkeypatch.setattr("cnlc_agent.wplm.file_decode_tool.requests.post", fake_post)
    response = await FileDecodeTool().call(raw_file_path=str(original))
    result = json.loads(response.content[0].text)
    assert response.state == ToolResultState.SUCCESS
    assert await asyncio.to_thread(original.read_bytes) == b"original"
    assert await asyncio.to_thread(Path(result["output_file_path"]).read_bytes) == b"converted"
