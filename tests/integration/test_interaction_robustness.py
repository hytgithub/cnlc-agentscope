"""真实 AgentScope ReAct、工具、后台执行和持久读模型的异常交互回归。"""

import asyncio
import json

import pytest
from agentscope.agent import ContextConfig
from agentscope.event import TextBlockDeltaEvent, ToolResultEndEvent
from agentscope.message import UserMsg
from agentscope.tool import Toolkit

from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent, MockTaskShellModel
from cnlc_agent.demo.task_tools import TaskCommandRunner, build_task_tools
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import ApplicationError, ToolError
from cnlc_agent.domain.models import MockFixture
from cnlc_agent.infrastructure.mock import MockModelGateway
from cnlc_agent.tools.mock import MockResultTool


def make_agent(data_dir, provider="fixture", mode="demo"):
    """使用真正的 Agent 与 Tool adapter，只替换模型和专业执行来源。"""

    runner = TaskCommandRunner(
        AppSettings(
            mode=mode,
            model_provider="mock",
            professional_provider=provider,
            mock_data_dir=data_dir,
            _env_file=None,
        ),
        PersistenceSettings(persistence="memory", _env_file=None),
    )
    agent = LoggingInterpretationDemoAgent(
        name="demo",
        system_prompt="",
        model=MockTaskShellModel(),
        toolkit=Toolkit(tools=build_task_tools(runner)),
        stream_step_delay_seconds=0,
        stream_report_chunk_delay_seconds=0,
    )
    return agent, runner


async def ask(agent, text):
    """消费完整回复并返回工具事实与最终文本。"""

    events = [event async for event in agent.reply_stream(UserMsg(name="user", content=text))]
    results = [event for event in events if isinstance(event, ToolResultEndEvent)]
    reply = agent.state.context[-1].get_text_content() or ""
    return results, reply, events


async def start(runner):
    result = await runner.run("WELL_MOCK_001")
    await runner.wait_for_completion(result.task_id, result.execution_id)
    return result


@pytest.mark.parametrize(
    "instruction",
    ["现在执行到哪里了", "给我报告", "上一版报告", "全部重跑", "孔隙度改成0.16", "改成0.16"],
)
async def test_no_task_does_not_create_or_guess(data_dir, instruction):
    agent, runner = make_agent(data_dir)
    results, reply, _ = await ask(agent, instruction)
    assert len(results) == 1 and results[0].metadata["error_code"] == "TASK_NOT_FOUND"
    assert "请先上传" in reply
    assert runner.observed_task_ids == set()
    assert runner.pending_clarification() is None
    await runner.dispatcher.shutdown()


async def test_clarification_completes_once_after_compression(data_dir):
    agent, runner = make_agent(data_dir)
    first = await start(runner)
    try:
        results, reply, _ = await ask(agent, "改成0.16")
        assert results[0].metadata["error_code"] == "CLARIFICATION_REQUIRED"
        assert "请明确" in reply
        assert runner.pending_clarification().known_value == 0.16
        assert len(await runner.repository.list_executions(first.task_id)) == 1
        await agent.compress_context(
            context_config=ContextConfig(trigger_ratio=0.01, reserve_ratio=0.005)
        )
        results, _, events = await ask(agent, "孔隙度")
        modified = results[0].metadata["result"]
        assert modified["command"] == "MODIFY" and modified["effective_override"]["por"] == 0.16
        assert modified["task_id"] == first.task_id
        assert runner.pending_clarification() is None
        assert len(await runner.repository.list_executions(first.task_id)) == 2
        report = "".join(event.delta for event in events if isinstance(event, TextBlockDeltaEvent))
        assert report == await runner.repository.get_execution_report(modified["execution_id"])
        await ask(agent, "孔隙度")
        assert len(await runner.repository.list_executions(first.task_id)) == 2
    finally:
        await runner.dispatcher.shutdown()


@pytest.mark.parametrize("unrelated", ["现在呢", "讲个笑话", "给我报告"])
async def test_pending_expires_after_unrelated_turn(data_dir, unrelated):
    agent, runner = make_agent(data_dir)
    first = await start(runner)
    try:
        await ask(agent, "改成0.16")
        await ask(agent, unrelated)
        assert runner.pending_clarification() is None
        results, reply, _ = await ask(agent, "孔隙度")
        assert not results and "名称和值" in reply
        assert len(await runner.repository.list_executions(first.task_id)) == 1
    finally:
        await runner.dispatcher.shutdown()


async def test_running_rejects_writes_and_reads_live_step(data_dir, monkeypatch):
    agent, runner = make_agent(data_dir)
    first = await start(runner)
    entered, released = asyncio.Event(), asyncio.Event()
    original = MockModelGateway.generate

    async def waiting(self, request):
        if request.purpose == "fluid":
            entered.set()
            await released.wait()
        return await original(self, request)

    monkeypatch.setattr(MockModelGateway, "generate", waiting)
    tools = {tool.name: tool for tool in build_task_tools(runner)}
    submitted = await tools["modify_well_interpretation"].call(por=0.17)
    execution = submitted.metadata["result"]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for instruction in ("孔隙度改成0.18", "全部重新跑", "再重新解释一次"):
            results, reply, _ = await ask(agent, instruction)
            assert results[0].metadata["error_code"] == "TASK_EXECUTION_ACTIVE"
            assert "#2" in reply and "W06" in reply
        for instruction in ("现在执行到哪里了", "现在呢", "处理到什么地方了"):
            results, _, _ = await ask(agent, instruction)
            assert results[0].metadata["result"]["execution_id"] == execution["execution_id"]
            assert results[0].metadata["result"]["current_step"] == "W06"
        results, _, _ = await ask(agent, "给我报告")
        assert results[0].metadata["error_code"] == "REPORT_NOT_READY"
        results, _, _ = await ask(agent, "上一版报告")
        assert results[0].metadata["result"]["execution_id"] == first.execution_id
        assert len(await runner.repository.list_executions(first.task_id)) == 2
    finally:
        released.set()
        await runner.wait_for_completion(first.task_id, execution["execution_id"])
        await runner.dispatcher.shutdown()


@pytest.mark.parametrize(
    "instruction,code",
    [
        ("上一版报告", "REPORT_NOT_FOUND"),
        ("上一口井的报告", "PREVIOUS_TASK_NOT_FOUND"),
        ("WELL_UNKNOWN 的报告", "SESSION_WELL_NOT_FOUND"),
        ("只重新算SW", "UNSUPPORTED_OPERATION"),
        ("重新识别岩性", "UNSUPPORTED_OPERATION"),
        ("只重新划分层段", "UNSUPPORTED_OPERATION"),
        ("Rw改成0.08", "UNSUPPORTED_PARAMETER"),
        ("Archie m改成2", "UNSUPPORTED_PARAMETER"),
        ("含水饱和度改成0.3", "UNSUPPORTED_PARAMETER"),
        ("把孔隙度改成0.16，然后全部重跑一次", "CLARIFICATION_REQUIRED"),
        ("修改POR，再重新算SW，再比较上一版", "CLARIFICATION_REQUIRED"),
        ("为什么2035-2038是水层？", "UNSUPPORTED_OPERATION"),
        ("什么是含水饱和度", "UNSUPPORTED_OPERATION"),
    ],
)
async def test_rejected_semantics_never_partially_execute(data_dir, instruction, code):
    agent, runner = make_agent(data_dir)
    first = await start(runner)
    try:
        results, reply, _ = await ask(agent, instruction)
        assert results[0].metadata["error_code"] == code
        assert "只处理单井" not in reply
        assert len(await runner.repository.list_executions(first.task_id)) == 1
    finally:
        await runner.dispatcher.shutdown()


async def test_no_change_and_modify_with_report(data_dir):
    agent, runner = make_agent(data_dir)
    first = await start(runner)
    try:
        await ask(agent, "把孔隙度改成0.16，解释完给我报告")
        results, reply, _ = await ask(agent, "孔隙度改成0.16")
        assert results[0].metadata["error_code"] == "NO_EFFECTIVE_CHANGE"
        assert "孔隙度已经是0.16" in reply and "没有产生新的执行" in reply
        assert len(await runner.repository.list_executions(first.task_id)) == 2
    finally:
        await runner.dispatcher.shutdown()


async def test_failed_status_safe_and_full_rerun_uses_application(data_dir, monkeypatch):
    agent, runner = make_agent(data_dir)
    original = MockResultTool.execute

    async def broken(self, request):
        if self.name == "calculate_sw":
            raise ToolError("TEST_W06_FAILED", "secret http://internal token private/path")
        return await original(self, request)

    monkeypatch.setattr(MockResultTool, "execute", broken)
    first = await start(runner)
    try:
        results, reply, _ = await ask(agent, "为什么失败了")
        payload = results[0].metadata["result"]
        assert payload["execution_status"] == "FAILED" and payload["failed_step"] == "W06"
        assert payload["error_code"] == "TEST_W06_FAILED"
        assert "W06" in reply and "secret" not in json.dumps(payload) and "http" not in reply
        results, _, _ = await ask(agent, "给我报告")
        assert results[0].metadata["result"]["execution_id"] == first.execution_id
        runner.repository._executions[first.execution_id].markdown = ""
        results, _, _ = await ask(agent, "给我报告")
        assert results[0].metadata["error_code"] == "REPORT_NOT_READY"
        monkeypatch.setattr(MockResultTool, "execute", original)
        results, _, _ = await ask(agent, "全部重跑")
        assert results[0].metadata["result"]["command"] == "FULL_RERUN"
        assert len(await runner.repository.list_executions(first.task_id)) == 2
    finally:
        await runner.dispatcher.shutdown()


@pytest.mark.parametrize("provider", ["fixture", "company_mock"])
async def test_provider_start_and_status_smoke(data_dir, provider):
    agent, runner = make_agent(data_dir, provider)
    try:
        # 正式 START Tool 通过 AgentScope ReAct，而非直接调用辅助方法。
        results, _, _ = await ask(agent, "开始解释 WELL_MOCK_001")
        first = results[0].metadata["result"]
        results, _, _ = await ask(agent, "现在呢")
        assert results[0].metadata["result"]["execution_status"] == "SUCCESS"
        assert results[0].metadata["result"]["task_id"] == first["task_id"]
    finally:
        await runner.dispatcher.shutdown()


async def test_blocked_missing_data_is_read_from_snapshot(data_dir, fixture_data):
    agent, runner = make_agent(data_dir, mode="mock")
    fixture_data["raw_data"]["curves"] = {}
    fixture = MockFixture.model_validate(fixture_data)
    first = await runner.start_uploaded(fixture, "缺曲线测试")
    await runner.wait_for_completion(first.task_id, first.execution_id)
    try:
        results, reply, _ = await ask(agent, "缺什么")
        payload = results[0].metadata["result"]
        assert payload["execution_status"] == "BLOCKED" and payload["missing_data"]
        assert payload["current_step"] == "W02"
        assert all(item["affected_step"] for item in payload["missing_data"])
        assert "缺少资料" in reply and "W02" in reply
    finally:
        await runner.dispatcher.shutdown()


@pytest.mark.parametrize("status", ["WARNING", "REVIEW_REQUIRED"])
async def test_terminal_interaction_status_facts(data_dir, status):
    agent, runner = make_agent(data_dir)
    first = await start(runner)
    # 场景注入只用于读模型回归，执行入口仍是正式 Tool/Application。
    execution = runner.repository._executions[first.execution_id]
    execution.status = status
    execution.state_snapshot.status = StepStatus(status)
    execution.state_snapshot.review_required = status == "REVIEW_REQUIRED"
    try:
        results, reply, _ = await ask(agent, "现在呢")
        assert results[0].metadata["result"]["execution_status"] == status
        assert ("人工复核" if status == "REVIEW_REQUIRED" else "存在 warning") in reply
        if status == "WARNING":
            results, _, _ = await ask(agent, "给我报告")
            assert results[0].metadata["result"]["report_ready"]
            results, _, _ = await ask(agent, "孔隙度改成0.19")
            assert results[0].metadata["result"]["command"] == "MODIFY"
    finally:
        await runner.dispatcher.shutdown()


async def test_model_batch_conflict_is_rejected_before_any_write(data_dir, monkeypatch):
    """即使模型违反提示同时提议两个写 Tool，也不能部分执行业务。"""

    from uuid import uuid4

    from agentscope.message import ToolCallBlock
    from agentscope.model import ChatResponse

    agent, runner = make_agent(data_dir)
    first = await start(runner)

    async def conflicting(*args, **kwargs):
        return ChatResponse(
            content=[
                ToolCallBlock(
                    id=uuid4().hex,
                    name="modify_well_interpretation",
                    input=json.dumps({"por": 0.16}),
                ),
                ToolCallBlock(id=uuid4().hex, name="rerun_well_interpretation", input="{}"),
            ],
            is_last=True,
        )

    monkeypatch.setattr(agent.model, "_call_api", conflicting)
    try:
        results, reply, _ = await ask(agent, "修改后全部重跑")
        assert results[0].metadata["error_code"] == "CLARIFICATION_REQUIRED"
        assert "本次未执行任何修改" in reply
        assert len(await runner.repository.list_executions(first.task_id)) == 1
    finally:
        await runner.dispatcher.shutdown()


async def test_pending_is_anchored_to_named_well_and_refresh_state(data_dir, fixture_data):
    """指定井的澄清目标不随 active 变化；Session 状态重载不靠聊天补数值。"""

    from cnlc_agent.demo.task_context import TaskReference

    agent, runner = make_agent(data_dir)
    first = await start(runner)
    fixture_data["well"]["well_id"] = "WELL_MOCK_002"
    second = await runner.start_uploaded(MockFixture.model_validate(fixture_data), "第二口井")
    await runner.wait_for_completion(second.task_id, second.execution_id)
    runner.set_active_task(first.task_id)
    try:
        results, _, _ = await ask(agent, "WELL_MOCK_002 改成0.16")
        assert results[0].metadata["error_code"] == "CLARIFICATION_REQUIRED"
        assert runner.pending_clarification().task_id == second.task_id
        assert runner.pending_clarification().known_value == 0.16
        assert runner.active_task_id == first.task_id
        # 模拟 Browser refresh 后 AgentScope 重载同一 Session State，runner 仍属于同 Session。
        restored_state = type(agent.state).model_validate_json(agent.state.model_dump_json())
        restored = LoggingInterpretationDemoAgent(
            name="demo",
            system_prompt="",
            model=MockTaskShellModel(),
            state=restored_state,
            toolkit=Toolkit(tools=build_task_tools(runner)),
            stream_step_delay_seconds=0,
            stream_report_chunk_delay_seconds=0,
        )
        tools = {tool.name: tool for tool in build_task_tools(runner)}
        # 工具未显式给引用时，parameter_name 仍使用服务端固定的 pending 目标。
        completed = await tools["modify_well_interpretation"].call(parameter_name="por")
        changed = completed.metadata["result"]
        assert changed["task_id"] == second.task_id
        await runner.wait_for_completion(second.task_id, changed["execution_id"])
        results, _, _ = await ask(restored, "现在呢")
        assert results[0].metadata["result"]["task_id"] == second.task_id
        assert len(await runner.repository.list_executions(first.task_id)) == 1
        assert len(await runner.repository.list_executions(second.task_id)) == 2
        # 解析未知井不能改变已成功选择的 active。
        with pytest.raises(ApplicationError) as missing:
            await runner.resolve_task_reference(TaskReference(kind="WELL_ID", value="WELL_UNKNOWN"))
        assert missing.value.code == "SESSION_WELL_NOT_FOUND"
        assert runner.active_task_id == second.task_id
    finally:
        await runner.dispatcher.shutdown()


@pytest.mark.parametrize("instruction", ["今天天气怎么样", "帮我写Java代码", "讲个笑话"])
async def test_out_of_domain_does_not_call_any_task_tool(data_dir, instruction):
    agent, runner = make_agent(data_dir)
    first = await start(runner)
    try:
        results, reply, _ = await ask(agent, instruction)
        assert not results and "只处理单井" in reply
        assert len(await runner.repository.list_executions(first.task_id)) == 1
    finally:
        await runner.dispatcher.shutdown()
