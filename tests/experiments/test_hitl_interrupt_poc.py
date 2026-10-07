"""012D：真实 AgentScope 事件与现有阶段确认事实的隔离 A/B 契约。"""

import asyncio
import importlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from agentscope.credential import DashScopeCredential
from agentscope.event import (
    ConfirmResult,
    ExternalExecutionResultEvent,
    ReplyEndEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    ToolResultEndEvent,
    UserConfirmResultEvent,
    UserInterruptEvent,
)
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import TextBlock, ToolCallBlock, ToolResultBlock, UserMsg
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.state import AgentState
from pydantic import SecretStr

from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.execution import ExecutionRunMode
from cnlc_agent.domain.models import MockFixture
from cnlc_agent.domain.session_binding import TaskSessionIdentity
from cnlc_agent.domain.state import InterpretationState

EMITTED_EVENTS = []


@pytest.fixture(autouse=True)
def event_evidence(request):
    """仅在显式指定输出路径时导出本次真实事件；不写入产品状态。"""

    EMITTED_EVENTS.clear()
    yield
    if output := os.getenv("CNLC_HITL_EVIDENCE"):
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "test": request.node.nodeid,
                        "events": EMITTED_EVENTS,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


class CheckpointModel(ChatModelBase):
    """仅脚本化模型输出；Agent 权限、事件、Tool 和业务确认均运行真实实现。"""

    def __init__(self, target, name="confirm_checkpoint"):
        super().__init__(
            credential=DashScopeCredential(name="offline", api_key=SecretStr("test-only")),
            model="offline-hitl",
            parameters=self.Parameters(),
            stream=False,
        )
        self.formatter = DashScopeChatFormatter()
        self.target = target
        self.tool_name = name
        self.calls = 0

    async def _call_api(self, model_name, messages, tools=None, **kwargs):
        self.calls += 1
        if any(isinstance(b, ToolResultBlock) for m in messages for b in m.content):
            return ChatResponse(content=[TextBlock(text="请以阶段业务状态为准。")], is_last=True)
        return ChatResponse(
            content=[
                ToolCallBlock(
                    id=uuid4().hex, name=self.tool_name, input=self.target.model_dump_json()
                )
            ],
            is_last=True,
        )


@pytest.fixture
async def checkpoint(data_dir):
    """每组同一 Fixture，生产内存 Repository 保存实际 StageRun，不复制状态机。"""

    runner = TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
        session_identity=TaskSessionIdentity(user_id="poc-user", agent_id="poc", session_id="poc"),
    )
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    result = await runner.start_uploaded(
        fixture, "012D 固定测试确认点", run_mode=ExecutionRunMode.STAGED_CONFIRMATION
    )
    await runner.wait_for_completion(result.task_id, result.execution_id)
    try:
        module = importlib.import_module("experiments.agentscope_native_poc.hitl_runner")
        target = await module.CheckpointTarget.read(runner, result.task_id, result.execution_id)
        yield runner, module, target
    finally:
        await runner.dispatcher.shutdown()


async def parked(module, runner, target, state=None, external=False):
    adapter = module.HitlAdapter(runner, target)
    model = CheckpointModel(target, "external_checkpoint" if external else "confirm_checkpoint")
    agent = adapter.build_agent(model, state=state, external=external)
    events = [e async for e in agent.reply_stream(UserMsg(name="user", content="展示确认点"))]
    EMITTED_EVENTS.extend(
        e.model_dump(mode="json")
        for e in events
        if isinstance(e, (RequireUserConfirmEvent, RequireExternalExecutionEvent))
    )
    event = next(
        e
        for e in events
        if isinstance(e, RequireExternalExecutionEvent if external else RequireUserConfirmEvent)
    )
    return adapter, agent, model, event, events


def answer(event, confirmed=True):
    return UserConfirmResultEvent(
        reply_id=event.reply_id,
        confirm_results=[
            ConfirmResult(confirmed=confirmed, tool_call=event.tool_calls[0].model_copy(deep=True))
        ],
    )


async def snapshot(runner, target):
    return await runner.repository.get_execution(target.execution_id)


@pytest.mark.parametrize("mode", ["current_confirm_stage", "native_hitl_wrapper"])
async def test_h01_h02_pause_confirm_next_stage_exactly_once(checkpoint, mode):
    runner, module, target = checkpoint
    before = await snapshot(runner, target)
    assert before.status == "WAITING_CONFIRMATION"
    assert [r.status for r in before.state_snapshot.stage_runs] == ["WAITING_CONFIRM"]
    assert [s.step_id for s in before.state_snapshot.executions] == ["W01"]
    adapter = module.HitlAdapter(runner, target)
    if mode == "current_confirm_stage":
        await adapter.current_confirm()
    else:
        adapter, agent, _, event, events = await parked(module, runner, target)
        assert event.tool_calls[0].state == "asking"
        assert not any(isinstance(e, ReplyEndEvent) for e in events)
        assert await snapshot(runner, target) == before
        await adapter.resume(agent, answer(event))
    await runner.wait_for_completion(target.task_id, target.execution_id)
    after = await snapshot(runner, target)
    assert [(r.stage, r.status) for r in after.state_snapshot.stage_runs] == [
        ("DATA_DECODE", "CONFIRMED"),
        ("PREPROCESS", "WAITING_CONFIRM"),
    ]
    assert [s.step_id for s in after.state_snapshot.executions] == ["W01", "W02", "W03"]


@pytest.mark.parametrize("mode", ["current_confirm_stage", "native_hitl_wrapper"])
async def test_h03_rejection_preserves_completed_result(checkpoint, mode):
    runner, module, target = checkpoint
    before = await snapshot(runner, target)
    if mode == "native_hitl_wrapper":
        adapter, agent, _, event, _ = await parked(module, runner, target)
        events = await adapter.resume(agent, answer(event, False))
        assert any(isinstance(e, ToolResultEndEvent) and e.state == "denied" for e in events)
        assert not agent.state.has_awaiting_tool_calls(agent.name)
    else:
        # 当前系统没有业务 reject 状态；不提交确认，保留 WAITING_CONFIRM。
        await module.HitlAdapter(runner, target).decline()
    assert await snapshot(runner, target) == before


@pytest.mark.parametrize("mode", ["current_confirm_stage", "native_hitl_wrapper"])
async def test_h04_concurrent_duplicate_does_not_confirm_second_checkpoint(checkpoint, mode):
    runner, module, target = checkpoint
    adapter = module.HitlAdapter(runner, target)
    if mode == "native_hitl_wrapper":
        adapter, agent, _, event, _ = await parked(module, runner, target)
        calls = [adapter.resume(agent, answer(event)) for _ in range(2)]
    else:
        calls = [adapter.current_confirm() for _ in range(2)]
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert isinstance(
        next(r for r in results if isinstance(r, Exception)), (ValueError, InfrastructureError)
    )
    await runner.wait_for_completion(target.task_id, target.execution_id)
    current = await snapshot(runner, target)
    assert [r.status for r in current.state_snapshot.stage_runs] == ["CONFIRMED", "WAITING_CONFIRM"]


@pytest.mark.parametrize("mode", ["current_confirm_stage", "native_hitl_wrapper"])
@pytest.mark.parametrize("stale", ["active", "execution", "stage", "ownership"])
async def test_h05_old_target_never_confirms_new_fact(checkpoint, mode, stale):
    runner, module, target = checkpoint
    adapter = module.HitlAdapter(runner, target)
    if mode == "native_hitl_wrapper":
        adapter, agent, _, event, _ = await parked(module, runner, target)
    if stale == "active":
        runner.set_active_task("another-task")
    elif stale == "execution":
        old = await snapshot(runner, target)
        await runner.repository.create_execution(
            InterpretationState(task=old.state_snapshot.task, mode="demo"),
            "RERUN",
            expected_current_execution_id=target.execution_id,
            run_mode=ExecutionRunMode.STAGED_CONFIRMATION,
        )
    elif stale == "stage":
        await adapter.current_confirm()
        await runner.wait_for_completion(target.task_id, target.execution_id)
    else:
        runner.session_identity = TaskSessionIdentity(
            user_id="other", agent_id="poc", session_id="poc"
        )
    before = await snapshot(runner, target)
    with pytest.raises(InfrastructureError):
        if mode == "current_confirm_stage":
            await adapter.current_confirm()
        else:
            await adapter.resume(agent, answer(event))
    assert await snapshot(runner, target) == before


async def test_h06_serialized_state_restores_native_pending_in_new_agent(checkpoint):
    runner, module, target = checkpoint
    _, agent, _, event, _ = await parked(module, runner, target)
    saved = agent.state.model_dump_json()
    restored = AgentState.model_validate_json(saved)
    adapter = module.HitlAdapter(runner, target)
    new_agent = adapter.build_agent(CheckpointModel(target), state=restored)
    assert new_agent.state.has_awaiting_tool_calls(new_agent.name)
    await adapter.resume(new_agent, answer(event))
    await runner.wait_for_completion(target.task_id, target.execution_id)
    assert (await snapshot(runner, target)).state_snapshot.stage_runs[0].status == "CONFIRMED"


async def test_h06_without_agent_state_refresh_reconstructs_from_stage_fact(checkpoint):
    runner, module, target = checkpoint
    _, _, _, event, _ = await parked(module, runner, target)
    adapter = module.HitlAdapter(runner, target)
    fresh = adapter.build_agent(CheckpointModel(target))
    with pytest.raises(ValueError):
        await adapter.resume(fresh, answer(event))
    reread = await module.CheckpointTarget.read(runner, target.task_id, target.execution_id)
    adapter, fresh, _, new_event, _ = await parked(module, runner, reread)
    assert new_event.reply_id != event.reply_id
    await adapter.resume(fresh, answer(new_event))
    await runner.wait_for_completion(target.task_id, target.execution_id)
    assert (await snapshot(runner, target)).state_snapshot.stage_runs[1].status == "WAITING_CONFIRM"


async def test_h07_query_and_switch_during_pause_do_not_confirm(checkpoint):
    runner, module, target = checkpoint
    adapter, agent, model, event, _ = await parked(module, runner, target)
    before = await snapshot(runner, target)
    with pytest.raises(ValueError, match="waiting"):
        _ = [e async for e in agent.reply_stream(UserMsg(name="user", content="先看看数据质量"))]
    assert await snapshot(runner, target) == before
    await adapter.interrupt(agent, UserInterruptEvent(reply_id=event.reply_id))
    progress = await runner.get_stage_progress(target.task_id, target.execution_id)
    assert progress.stage_result is not None
    assert progress.stage_status == "WAITING_CONFIRM"
    assert model.calls == 1
    runner.set_active_task("another-task")
    with pytest.raises(InfrastructureError):
        await adapter.current_confirm()
    assert await snapshot(runner, target) == before


async def test_h08_interrupt_external_wait_does_not_cancel_independent_job(checkpoint):
    runner, module, target = checkpoint
    adapter, agent, model, event, _ = await parked(module, runner, target, external=True)
    entered, release = asyncio.Event(), asyncio.Event()
    completed = []

    async def external_job():
        entered.set()
        await release.wait()
        completed.append("late-fixture-result")

    job = asyncio.create_task(external_job())
    before = await snapshot(runner, target)
    try:
        await entered.wait()
        events = await adapter.interrupt(agent, UserInterruptEvent(reply_id=event.reply_id))
        assert (
            next(e for e in events if isinstance(e, ReplyEndEvent)).finished_reason == "interrupted"
        )
        assert any(isinstance(e, ToolResultEndEvent) and e.state == "interrupted" for e in events)
        assert not job.done()
        assert model.calls == 1
        release.set()
        await job
        assert completed == ["late-fixture-result"]
        with pytest.raises(ValueError):
            _ = [
                e
                async for e in agent.reply_stream(
                    ExternalExecutionResultEvent(
                        reply_id=event.reply_id,
                        execution_results=[
                            ToolResultBlock(
                                id=event.tool_calls[0].id,
                                name="external_checkpoint",
                                output="late-fixture-result",
                                state="success",
                            )
                        ],
                    )
                )
            ]
        assert await snapshot(runner, target) == before
    finally:
        release.set()
        await job


async def test_h09_existing_report_read_requires_no_hitl(checkpoint):
    runner, module, _ = checkpoint
    fixture = MockFixture.model_validate_json(
        (runner.settings.mock_data_dir / "WELL_MOCK_001.json").read_text()
    )
    result = await runner.start_uploaded(fixture, "独立报告读取语义测试")
    await runner.wait_for_completion(result.task_id, result.execution_id)
    report = await module.read_existing_report(runner, result.task_id, result.execution_id)
    assert "WELL_MOCK_001" in report
    assert module.REPORT_CONFIRMATION == "BUSINESS_CONFIRMATION_REQUIRED"


@pytest.mark.parametrize("tamper", ["reply_id", "call_id", "input", "name", "rules"])
async def test_native_result_cannot_rewrite_locked_identity(checkpoint, tamper):
    runner, module, target = checkpoint
    adapter, agent, _, event, _ = await parked(module, runner, target)
    result = answer(event)
    confirmation = result.confirm_results[0]
    if tamper == "reply_id":
        result.reply_id = "wrong-reply"
    elif tamper == "call_id":
        confirmation.tool_call.id = "unknown-call"
    elif tamper == "input":
        data = json.loads(confirmation.tool_call.input)
        data["stage_run_id"] = "another-stage"
        confirmation.tool_call.input = json.dumps(data)
    elif tamper == "name":
        confirmation.tool_call.name = "external_checkpoint"
    else:
        from agentscope.permission import PermissionRule

        confirmation.rules = [
            PermissionRule(
                tool_name="confirm_checkpoint", behavior="allow", rule_content=None, source="user"
            )
        ]
    before = await snapshot(runner, target)
    with pytest.raises(ValueError):
        await adapter.resume(agent, result)
    assert await snapshot(runner, target) == before
    assert agent.state.has_awaiting_tool_calls(agent.name)


async def test_native_framework_ignores_reply_id_but_rejects_unknown_call_id(checkpoint):
    """刻画源码暴露的边界：原生 reply_id 不绑定业务对象，项目不能依赖它放行。"""

    runner, module, target = checkpoint
    _, agent, _, event, _ = await parked(module, runner, target)
    invalid = answer(event)
    invalid.confirm_results[0].tool_call.id = "invalid-call"
    with pytest.raises(ValueError, match="not waiting"):
        _ = [e async for e in agent.reply_stream(invalid)]
    wrong_reply = answer(event, False)
    wrong_reply.reply_id = "wrong-reply"
    events = [e async for e in agent.reply_stream(wrong_reply)]
    assert any(isinstance(e, ToolResultEndEvent) and e.state == "denied" for e in events)
    with pytest.raises(ValueError, match="not waiting"):
        _ = [e async for e in agent.reply_stream(answer(event))]
    assert (await snapshot(runner, target)).state_snapshot.stage_runs[0].status == "WAITING_CONFIRM"


@pytest.mark.parametrize("mode", ["current_confirm_stage", "native_hitl_wrapper"])
async def test_h02_preprocess_checkpoint_uses_same_authoritative_resume(checkpoint, mode):
    runner, module, first = checkpoint
    await module.HitlAdapter(runner, first).current_confirm()
    await runner.wait_for_completion(first.task_id, first.execution_id)
    target = await module.CheckpointTarget.read(runner, first.task_id, first.execution_id)
    assert target.stage == "PREPROCESS"
    before = await snapshot(runner, target)
    if mode == "current_confirm_stage":
        await module.HitlAdapter(runner, target).current_confirm()
    else:
        adapter, agent, _, event, _ = await parked(module, runner, target)
        assert await snapshot(runner, target) == before
        await adapter.resume(agent, answer(event))
    await runner.wait_for_completion(target.task_id, target.execution_id)
    after = await snapshot(runner, target)
    assert [(r.stage, r.status) for r in after.state_snapshot.stage_runs] == [
        ("DATA_DECODE", "CONFIRMED"),
        ("PREPROCESS", "CONFIRMED"),
        ("INTERPRET", "WAITING_CONFIRM"),
    ]
    assert [s.step_id for s in after.state_snapshot.executions] == [
        "W01",
        "W02",
        "W03",
        "W04",
        "W05",
        "W06",
        "W07",
        "W08",
        "W09",
        "W10",
    ]


async def test_h07_real_task_switch_retains_old_waiting_stage(checkpoint):
    runner, module, target = checkpoint
    adapter, agent, _, event, _ = await parked(module, runner, target)
    before = await snapshot(runner, target)
    fixture = MockFixture.model_validate_json(
        (runner.settings.mock_data_dir / "WELL_MOCK_001.json").read_text()
    )
    fixture.well.well_id = "WELL_POC_002"
    other = await runner.start_uploaded(
        fixture, "先解释另一口测试井", run_mode=ExecutionRunMode.STAGED_CONFIRMATION
    )
    await runner.wait_for_completion(other.task_id, other.execution_id)
    assert runner.active_task_id == other.task_id
    with pytest.raises(InfrastructureError):
        await adapter.resume(agent, answer(event))
    assert await snapshot(runner, target) == before
    other_execution = await runner.repository.get_execution(other.execution_id)
    assert [r.status for r in other_execution.state_snapshot.stage_runs] == ["WAITING_CONFIRM"]
