"""AgentScope Context Compression 的本轮输入与历史摘要分离回归。"""

import base64
import json
from uuid import uuid4

from agentscope.agent import ContextConfig
from agentscope.event import ToolResultEndEvent
from agentscope.message import DataBlock, TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse
from agentscope.tool import Toolkit

from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent, MockTaskShellModel
from cnlc_agent.demo.operation_tool import build_agent_task_tools
from cnlc_agent.demo.task_tools import TaskCommandRunner


class RecordingMockTaskShellModel(MockTaskShellModel):
    """只为断言 Mock 实际收到的末条用户消息，不保存业务 Authority。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_user_instruction = None

    async def _call_api(self, model_name, messages, **kwargs):
        users = [message for message in messages if message.role == "user"]
        self.last_user_instruction = users[-1].get_text_content() if users else None
        return await super()._call_api(model_name, messages, **kwargs)


def make_agent(data_dir, *, context_size=131_072):
    runner = TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
    )
    model = RecordingMockTaskShellModel(context_size=context_size)
    agent = LoggingInterpretationDemoAgent(
        name="demo",
        system_prompt="",
        model=model,
        toolkit=Toolkit(tools=build_agent_task_tools(runner)),
        stream_step_delay_seconds=0,
        stream_report_chunk_delay_seconds=0,
    )
    return agent, runner, model


def upload_message(payload, filename):
    return UserMsg(
        name="user",
        content=[
            TextBlock(text="帮我解释一下这口井"),
            DataBlock(
                name=filename,
                source={
                    "type": "base64",
                    "media_type": "application/json",
                    "data": base64.b64encode(
                        json.dumps(payload, ensure_ascii=False).encode()
                    ).decode(),
                },
            ),
        ],
    )


async def upload(agent, data_dir, *, well_id="WELL_MOCK_001"):
    payload = json.loads((data_dir / "WELL_MOCK_001.json").read_text(encoding="utf-8"))
    payload["well"]["well_id"] = well_id
    payload["well"]["name"] = f"测试井 {well_id}"
    events = [
        event
        async for event in agent.reply_stream(upload_message(payload, f"{well_id}.json"))
    ]
    result = next(event for event in events if isinstance(event, ToolResultEndEvent)).metadata[
        "result"
    ]
    await agent._task_runner.wait_for_completion(result["task_id"], result["execution_id"])
    return result


async def ask(agent, text):
    events = [event async for event in agent.reply_stream(UserMsg(name="user", content=text))]
    results = [event for event in events if isinstance(event, ToolResultEndEvent)]
    return results, events


async def force_compression(agent):
    await agent.compress_context(
        context_config=ContextConfig(trigger_ratio=0.01, reserve_ratio=0.005)
    )
    assert agent.state.summary is not None


async def test_summary_wells_never_replace_current_report_target(data_dir):
    """摘要可含旧井，CURRENT 与明确 WELL_ID 均按当前输入和 Resolver 处理。"""

    agent, runner, model = make_agent(data_dir)
    try:
        well_a = await upload(agent, data_dir)
        well_b = await upload(agent, data_dir, well_id="WELL_MOCK_002")
        await force_compression(agent)
        summary = str(agent.state.summary)
        assert "WELL_MOCK_001" in summary and "WELL_MOCK_002" in summary

        results, _ = await ask(agent, "给我报告")
        current_report = results[0].metadata["result"]
        assert model.last_user_instruction == "给我报告"
        assert current_report["task_id"] == well_b["task_id"]
        assert current_report["execution_id"] == well_b["execution_id"]

        results, _ = await ask(agent, "看看 WELL_MOCK_001 的报告")
        explicit_report = results[0].metadata["result"]
        assert model.last_user_instruction == "看看 WELL_MOCK_001 的报告"
        assert explicit_report["task_id"] == well_a["task_id"]

        results, _ = await ask(agent, "再看上一口井的报告")
        previous_report = results[0].metadata["result"]
        assert model.last_user_instruction == "再看上一口井的报告"
        assert previous_report["task_id"] == well_a["task_id"]
        assert len(await runner.repository.list_executions(well_a["task_id"])) == 1
        assert len(await runner.repository.list_executions(well_b["task_id"])) == 1
    finally:
        await runner.dispatcher.shutdown()


async def test_current_report_uses_authority_after_summary_version_is_stale(data_dir):
    """摘要停留在较早版本时，CURRENT 仍由 Repository 解析为最新版本。"""

    agent, runner, model = make_agent(data_dir)
    try:
        first = await upload(agent, data_dir)
        results, _ = await ask(agent, "全井孔隙度修改为0.16")
        second = results[0].metadata["result"]
        await runner.wait_for_completion(first["task_id"], second["execution_id"])
        await force_compression(agent)
        stale_summary = str(agent.state.summary)
        assert second["execution_id"] in stale_summary

        results, _ = await ask(agent, "全井孔隙度修改为0.17")
        third = results[0].metadata["result"]
        await runner.wait_for_completion(first["task_id"], third["execution_id"])
        assert third["execution_id"] != second["execution_id"]
        assert str(agent.state.summary) == stale_summary

        results, _ = await ask(agent, "看当前报告")
        current_report = results[0].metadata["result"]
        assert model.last_user_instruction == "看当前报告"
        assert current_report["execution_id"] == third["execution_id"]
        assert current_report["task_id"] == first["task_id"]
    finally:
        await runner.dispatcher.shutdown()


async def test_current_turn_scope_is_grounded_after_compression(data_dir):
    """当前明确整井可通过；缺范围时旧摘要不能替代本轮授权。"""

    agent, runner, model = make_agent(data_dir, context_size=8_192)
    try:
        first = await upload(agent, data_dir)
        agent.state.summary = (
            "<system-info>Earlier request: 全井孔隙度改成0.16；只作为历史对话摘要。</system-info>"
        )
        # 填充旧对话，确保 AgentScope 在下一轮推理前执行正常压缩流程。
        for index in range(8):
            agent.state.context.append(
                UserMsg(name="user", content=f"旧的普通讨论 {index} " + ("背景 " * 100))
            )
        results, _ = await ask(agent, "孔隙度改成0.17")
        assert model.last_user_instruction == "孔隙度改成0.17"
        assert results[0].metadata["error_code"] == "CLARIFICATION_REQUIRED"
        assert len(await runner.repository.list_executions(first["task_id"])) == 1

        agent.state.summary = (
            "<system-info>历史对话未说明修改范围；当前范围必须重新核对。</system-info>"
        )
        # 清除该轮 Pending，独立验证本轮明确表达不受摘要影响。
        runner.clear_operation_clarification()
        results, _ = await ask(agent, "全井孔隙度改成0.17")
        modified = results[0].metadata["result"]
        assert model.last_user_instruction == "全井孔隙度改成0.17"
        assert modified["task_id"] == first["task_id"]
        assert modified["effective_override"]["por"] == 0.17
        assert len(await runner.repository.list_executions(first["task_id"])) == 2
    finally:
        await runner.dispatcher.shutdown()


async def test_summary_whole_well_candidate_cannot_authorize_current_write(
    data_dir,
    monkeypatch,
):
    """历史摘要中的整井描述不能满足本轮 WHOLE_WELL 授权检查。"""

    agent, runner, _ = make_agent(data_dir)
    first = await upload(agent, data_dir)
    agent.state.summary = (
        "<system-info>Historical request: 全井孔隙度改成0.16；仅为历史对话摘要。</system-info>"
    )

    async def model_from_summary(*args, **kwargs):
        request = {
            "mode": "PLAN",
            "plan": {
                "input_classification": "EXECUTION_REQUEST",
                "persist_mode": "CREATE_VERSION",
                "original_instruction": "孔隙度改成0.17",
                "operations": [
                    {
                        "operation_id": "op1",
                        "action": "MODIFY_PARAMETER",
                        "target": "POROSITY",
                        "scope": {"kind": "WHOLE_WELL"},
                        "parameters": {
                            "value": {"mode": "ABSOLUTE", "value": 0.17, "unit": "1"}
                        },
                    }
                ],
            },
        }
        return ChatResponse(
            content=[
                ToolCallBlock(
                    id=uuid4().hex,
                    name="interpret_interpretation_operation",
                    input=json.dumps({"request": request}),
                )
            ],
            is_last=True,
        )

    monkeypatch.setattr(agent.model, "_call_api", model_from_summary)
    try:
        results, _ = await ask(agent, "孔隙度改成0.17")
        assert results[0].metadata["error_code"] == "CLARIFICATION_REQUIRED"
        assert len(await runner.repository.list_executions(first["task_id"])) == 1
    finally:
        await runner.dispatcher.shutdown()


async def test_summary_layer_hint_without_current_scope_is_clarified(data_dir, monkeypatch):
    """摘要给出的旧层号候选不通过本轮范围核验时澄清，不执行查询。"""

    agent, runner, _ = make_agent(data_dir)
    first = await upload(agent, data_dir)
    agent.state.summary = (
        "<system-info>Historical context: 第5层曾被查看；不作为本轮范围。</system-info>"
    )

    async def model_from_summary(*args, **kwargs):
        request = {
            "mode": "PLAN",
            "plan": {
                "input_classification": "READ_REQUEST",
                "persist_mode": "CREATE_VERSION",
                "original_instruction": "刚才那层怎么样",
                "operations": [
                    {
                        "operation_id": "op1",
                        "action": "QUERY",
                        "target": "INTERVAL",
                        "scope": {"kind": "INTERVAL_ORDINAL", "ordinal": 5},
                    }
                ],
            },
        }
        return ChatResponse(
            content=[
                ToolCallBlock(
                    id=uuid4().hex,
                    name="interpret_interpretation_operation",
                    input=json.dumps({"request": request}),
                )
            ],
            is_last=True,
        )

    monkeypatch.setattr(agent.model, "_call_api", model_from_summary)
    try:
        results, _ = await ask(agent, "刚才那层怎么样")
        assert results[0].metadata["error_code"] == "CLARIFICATION_REQUIRED"
        assert len(await runner.repository.list_executions(first["task_id"])) == 1
        assert runner.interaction_context.view is None
    finally:
        await runner.dispatcher.shutdown()
