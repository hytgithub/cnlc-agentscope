"""按需用真实井文件验证 GDSX 解析与 AgentScope 工具调用。"""

import asyncio
import json
import os
from pathlib import Path

import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool import Toolkit, ToolResponse

from cnlc_agent.pygdsx.curve import get_gdx_curve_name_list
from cnlc_agent.wplm.tools import GdsxCompletenessTool


@pytest.mark.asyncio
async def test_real_gdsx_sample() -> None:
    sample = os.environ.get("CNLC_GDSX_TEST_FILE")
    if not sample:
        pytest.skip("设置 CNLC_GDSX_TEST_FILE 后运行真实井文件测试")
    path = Path(sample)
    assert await asyncio.to_thread(path.is_file)
    assert await asyncio.to_thread(get_gdx_curve_name_list, str(path))

    toolkit = Toolkit(tools=[GdsxCompletenessTool()])
    chunks = [
        item
        async for item in toolkit.call_tool(
            ToolCallBlock(
                id="real-gdsx",
                name="gdsx_completeness",
                input=json.dumps({"gdsx_path": str(path), "curves": ["GR"], "save_files": False}),
            ),
            AgentState(),
        )
    ]
    response = chunks[-1]
    assert isinstance(response, ToolResponse)
    assert response.state == ToolResultState.SUCCESS
    result = json.loads(response.content[0].text)
    assert result["success"] is True
    assert "GR" in result["report"]["curves"]
