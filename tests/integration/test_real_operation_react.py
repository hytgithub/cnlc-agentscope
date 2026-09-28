"""显式启用的真实 qwen-plus ReAct 验收；专业计算使用正式 Fixture Tool。"""

import asyncio
import os

import pytest
from agentscope.credential import DashScopeCredential
from agentscope.event import ToolResultEndEvent
from agentscope.message import ToolCallBlock, UserMsg
from agentscope.tool import Toolkit

from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent
from cnlc_agent.demo.operation_tool import OPERATION_TOOL_NAME, build_agent_task_tools
from cnlc_agent.demo.task_tools import TaskCommandRunner

pytestmark = pytest.mark.skipif(
    os.getenv("CNLC_RUN_REAL_MODEL_TEST") != "1",
    reason="Set CNLC_RUN_REAL_MODEL_TEST=1 for real qwen-plus ReAct acceptance",
)


async def test_real_qwen_operation_clarification_and_atomicity(data_dir):
    """只断言工具路径和业务事实，不依赖模型的精确措辞。"""
    connections = ConnectionSettings()
    assert connections.model_api_key and connections.model_base_url
    credential = DashScopeCredential(
        name="E2 real acceptance",
        api_key=connections.model_api_key,
        base_url=connections.model_base_url,
    )
    model_class = credential.get_chat_model_class()
    model = model_class(
        credential=credential,
        model="qwen-plus",
        parameters=model_class.Parameters(temperature=0, parallel_tool_calls=False),
        stream=False,
    )
    runner = TaskCommandRunner(
        AppSettings(model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
    )
    agent = LoggingInterpretationDemoAgent(
        name="acceptance",
        system_prompt="",
        model=model,
        toolkit=Toolkit(tools=build_agent_task_tools(runner)),
        stream_step_delay_seconds=0,
        stream_report_chunk_delay_seconds=0,
    )
    first = await runner.run("WELL_MOCK_001")
    await runner.wait_for_completion(first.task_id, first.execution_id)

    async def ask(text):
        async with asyncio.timeout(90):
            events = [e async for e in agent.reply_stream(UserMsg(name="user", content=text))]
        results = [e for e in events if isinstance(e, ToolResultEndEvent)]
        assert len(results) == 1
        calls = [
            block
            for message in agent.state.context[-1:]
            for block in message.content
            if isinstance(block, ToolCallBlock)
        ]
        assert len(calls) == 1 and calls[0].name == OPERATION_TOOL_NAME
        print(
            "REAL_CASE",
            text,
            [b.input for b in calls],
            {k: v for k, v in results[0].metadata.items() if k in {"error_code", "message"}},
        )
        # 框架在进入 Tool 前拒绝不合法结构时也必须安全终止；真实模型不保证每轮 Schema 合规。
        return results[0].metadata.get(
            "operation",
            {
                "outcome": "REJECTED",
                "created_execution_ids": [],
                **results[0].metadata,
            },
        )

    try:
        changed = await ask("孔隙度、渗透率都改成0.16")
        assert len(changed["created_execution_ids"]) == 1, changed
        assert len(await runner.repository.list_executions(first.task_id)) == 2
        missing = await ask("改成0.17")
        assert missing["outcome"] == "NEED_CLARIFICATION"
        assert len(await runner.repository.list_executions(first.task_id)) == 2
        completed = await ask("孔隙度")
        assert len(completed["created_execution_ids"]) == 1
        assert completed["task_results"][0]["effective_override"]["por"] == 0.17
        assert len(await runner.repository.list_executions(first.task_id)) == 3
        capability = await ask("你能只重新算Sw吗")
        assert capability["outcome"] == "READ_ONLY"
        assert capability["capability_facts"]
        local = await ask("第5层孔隙度改成0.16")
        assert local["outcome"] in {"REJECTED", "KNOWN_UNSUPPORTED"}
        assert not local["created_execution_ids"]
        conditional = await ask("如果Sw大于60%就改成水层")
        assert conditional["error_code"] in {
            "CONDITIONAL_EXECUTION_UNSUPPORTED",
            "INVALID_OPERATION_PLAN",
        }
        assert len(await runner.repository.list_executions(first.task_id)) == 3
        await ask("改成0.18")
        await ask("算了")
        assert runner.pending_operation_clarification() is None
        cancelled = await ask("孔隙度")
        assert not cancelled["created_execution_ids"]
        assert len(await runner.repository.list_executions(first.task_id)) == 3
    finally:
        await runner.dispatcher.shutdown()
