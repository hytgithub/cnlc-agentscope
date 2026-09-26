"""任务工具的会话隔离、输入重建、错误和注册边界。"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.tool import Toolkit

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.agentscope_app import SessionTaskToolFactory
from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent
from cnlc_agent.demo.task_tools import ALLOWED_TASK_TOOLS, TaskCommandRunner, build_task_tools
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.models import MockFixture
from cnlc_agent.domain.session_binding import TaskSessionIdentity
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository


def configured_runner(data_dir):
    return TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
    )


async def test_memory_session_upload_modify_status_previous_and_isolation(data_dir, monkeypatch):
    session = configured_runner(data_dir)
    tools = {tool.name: tool for tool in build_task_tools(session)}
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    roots = []
    original = session.context

    @asynccontextmanager
    async def track(root):
        roots.append(root)
        async with original(root) as service:
            yield service

    monkeypatch.setattr(session, "context", track)
    start = tools["run_well_interpretation"]
    start.upload = fixture, "帮我解释一下这口井"
    result = await start.call(well_id=fixture.well.well_id)
    start.upload = None
    first = result.metadata["result"]
    assert first["execution_status"] == "QUEUED"
    await session.wait_for_completion(first["task_id"], first["execution_id"])
    assert all(not root.exists() for root in roots)
    changed = await tools["modify_well_interpretation"].call(
        task_id=first["task_id"], por=0.16, perm=0.16
    )
    second = changed.metadata["result"]
    await session.wait_for_completion(first["task_id"], second["execution_id"])
    assert changed.state == "success"
    assert second["reused_steps"] == ["W01", "W02", "W03"]
    status = await tools["get_interpretation_status"].call(task_id=first["task_id"])
    assert status.metadata["result"]["execution_id"] == second["execution_id"]
    previous = await tools["get_interpretation_report"].call(
        task_id=first["task_id"], selector="PREVIOUS"
    )
    assert previous.metadata["result"]["report_markdown"] == (
        await session.repository.get_execution_report(first["execution_id"])
    )
    assert all(not root.exists() for root in roots)
    other = {tool.name: tool for tool in build_task_tools(configured_runner(data_dir))}
    missing = await other["get_interpretation_status"].call(task_id=first["task_id"])
    assert missing.metadata["error_code"] == "TASK_NOT_FOUND"
    assert other["run_well_interpretation"].upload is None
    await session.dispatcher.shutdown()


async def test_tools_reject_unknown_and_no_effective_change(data_dir):
    session = configured_runner(data_dir)
    tools = {tool.name: tool for tool in build_task_tools(session)}
    started = await tools["run_well_interpretation"].call(well_id="WELL_MOCK_001")
    first = started.metadata["result"]
    await session.wait_for_completion(first["task_id"], first["execution_id"])
    modify = tools["modify_well_interpretation"]
    for parameter in ("sw", "rw", "archie_m", "archie_n", "unknown_parameter", "start_step"):
        result = await modify.call(task_id=first["task_id"], **{parameter: 0.16})
        assert result.metadata["error_code"] == "INVALID_COMMAND"
    assert (await modify.call(task_id=first["task_id"])).metadata["error_code"] == "EMPTY_OVERRIDE"
    changed = await modify.call(task_id=first["task_id"], por=0.16)
    await session.wait_for_completion(first["task_id"], changed.metadata["result"]["execution_id"])
    result = await modify.call(task_id=first["task_id"], por=0.16)
    assert result.metadata["error_code"] == "NO_EFFECTIVE_CHANGE"
    assert "Traceback" not in json.dumps(result.model_dump())
    await session.dispatcher.shutdown()


async def test_binding_failure_does_not_submit_background_execution(data_dir, monkeypatch):
    """归属未持久化时返回稳定错误，不能向用户伪装任务已提交。"""

    session = TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
        session_identity=TaskSessionIdentity(
            user_id="alice", agent_id="agent-a", session_id="session-a"
        ),
    )

    async def fail_binding(_binding):
        raise InfrastructureError(
            "SESSION_TASK_BINDING_FAILED", "database secret must stay private"
        )

    submit = AsyncMock()
    monkeypatch.setattr(session.repository, "bind_task_to_session", fail_binding)
    monkeypatch.setattr(session.dispatcher, "submit", submit)
    tool = {item.name: item for item in build_task_tools(session)}[
        "run_well_interpretation"
    ]
    result = await tool.call(well_id="WELL_MOCK_001")
    assert result.metadata["error_code"] == "SESSION_TASK_BINDING_FAILED"
    assert "secret" not in result.metadata["message"]
    assert session.observed_task_ids == set()
    submit.assert_not_awaited()


async def test_session_factory_keeps_same_session_and_separates_identity(monkeypatch):
    monkeypatch.setenv("CNLC_PERSISTENCE", "memory")
    factory = SessionTaskToolFactory()
    assert factory.get_existing_runner("user", "agent", "missing") is None
    assert factory.runners == {}
    first = await factory("user", "agent", "session")
    assert factory.get_existing_runner("user", "agent", "session") is first[1].runner
    again = await factory("user", "agent", "session")
    assert first[1].runner is again[1].runner
    for identity in [("other", "agent", "session"), ("user", "other", "session"),
                     ("user", "agent", "other")]:
        other = await factory(*identity)
        assert first[1].runner.repository is not other[1].runner.repository


async def test_postgres_mode_uses_existing_runtime(data_dir, monkeypatch):
    session = configured_runner(data_dir)
    session.persistence.persistence = "postgres-redis"
    repository = InMemoryTaskRepository()
    contexts = []

    @asynccontextmanager
    async def runtime(settings, persistence, connections):
        contexts.append(persistence.persistence)
        app = build_application(settings, task_repository=repository)
        try:
            yield app
        finally:
            await app.close()

    monkeypatch.setattr("cnlc_agent.demo.task_tools.application_runtime", runtime)
    tools = {t.name: t for t in build_task_tools(session)}
    started = await tools["run_well_interpretation"].call(well_id="WELL_MOCK_001")
    first = started.metadata["result"]
    result = await tools["get_interpretation_status"].call(task_id=first["task_id"])
    assert result.state == "success"
    assert contexts == ["postgres-redis", "postgres-redis", "postgres-redis"]
    assert await session.repository.get_task(first["task_id"]) is None


async def test_postgres_factory_restores_binding_for_status_and_modify(data_dir, monkeypatch):
    """新 Factory 没有进程缓存时，仍从 durable repository 恢复 Task Tool 归属。"""

    repository = InMemoryTaskRepository()

    @asynccontextmanager
    async def runtime(settings, persistence, connections):
        del persistence, connections
        app = build_application(settings, task_repository=repository)
        try:
            yield app
        finally:
            await app.close()

    monkeypatch.setattr("cnlc_agent.demo.task_tools.application_runtime", runtime)
    settings = AppSettings(
        mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None
    )
    persistence = PersistenceSettings(persistence="postgres-redis", _env_file=None)
    first_factory = SessionTaskToolFactory(settings=settings, persistence=persistence)
    first_tools = {
        tool.name: tool for tool in await first_factory("alice", "agent-a", "session-a")
    }
    started = await first_tools["run_well_interpretation"].call(well_id="WELL_MOCK_001")
    first = started.metadata["result"]
    await first_tools["get_interpretation_status"].runner.wait_for_completion(
        first["task_id"], first["execution_id"]
    )
    await first_factory.shutdown()

    restored_factory = SessionTaskToolFactory(settings=settings, persistence=persistence)
    assert restored_factory.runners == {}
    restored_tools = {
        tool.name: tool for tool in await restored_factory("alice", "agent-a", "session-a")
    }
    restored_runner = restored_tools["get_interpretation_status"].runner
    assert first["task_id"] in restored_runner.observed_task_ids
    restored_runner.observed_task_ids.clear()
    status = await restored_tools["get_interpretation_status"].call(task_id=first["task_id"])
    assert status.state == "success"
    assert first["task_id"] in restored_runner.observed_task_ids
    modified = await restored_tools["modify_well_interpretation"].call(
        task_id=first["task_id"], por=0.17
    )
    assert modified.state == "success"
    await restored_runner.wait_for_completion(
        first["task_id"], modified.metadata["result"]["execution_id"]
    )
    assert await restored_factory.get_or_restore_runner(
        "bob", "agent-a", "session-a"
    ) is None
    assert await restored_factory.get_or_restore_runner(
        "alice", "agent-b", "session-a"
    ) is None
    assert await restored_factory.get_or_restore_runner(
        "alice", "agent-a", "session-b"
    ) is None
    await restored_factory.shutdown()


async def test_factory_restart_restores_multiple_tasks_and_previous_well(
    data_dir, fixture_data, monkeypatch
):
    """进程级 runner 丢失后，Binding 恢复多井顺序并 fallback 到最近井。"""

    repository = InMemoryTaskRepository()

    @asynccontextmanager
    async def runtime(settings, persistence, connections):
        del persistence, connections
        app = build_application(settings, task_repository=repository)
        try:
            yield app
        finally:
            await app.close()

    monkeypatch.setattr("cnlc_agent.demo.task_tools.application_runtime", runtime)
    settings = AppSettings(
        mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None
    )
    persistence = PersistenceSettings(persistence="postgres-redis", _env_file=None)
    identity = ("alice", "agent-a", "multi-well-session")

    first_factory = SessionTaskToolFactory(settings=settings, persistence=persistence)
    first_tools = {tool.name: tool for tool in await first_factory(*identity)}
    started_a = await first_tools["run_well_interpretation"].call(
        well_id="WELL_MOCK_001"
    )
    task_a = started_a.metadata["result"]
    runner_a = first_tools["get_interpretation_status"].runner
    await runner_a.wait_for_completion(task_a["task_id"], task_a["execution_id"])

    fixture_b_data = json.loads(json.dumps(fixture_data))
    fixture_b_data["well"]["well_id"] = "WELL_MOCK_002"
    fixture_b_data["well"]["name"] = "虚构演示井 002"
    start_tool = first_tools["run_well_interpretation"]
    start_tool.upload = MockFixture.model_validate(fixture_b_data), "帮我解释第二口井"
    started_b = await start_tool.call(well_id="WELL_MOCK_002")
    start_tool.upload = None
    task_b = started_b.metadata["result"]
    await runner_a.wait_for_completion(task_b["task_id"], task_b["execution_id"])
    await first_factory.shutdown()

    restored_factory = SessionTaskToolFactory(settings=settings, persistence=persistence)
    restored_tools = {tool.name: tool for tool in await restored_factory(*identity)}
    restored_runner = restored_tools["get_interpretation_status"].runner
    assert restored_runner.active_task_id is None
    current = await restored_tools["get_interpretation_status"].call(
        task_reference={"kind": "CURRENT"}
    )
    assert current.metadata["result"]["task_id"] == task_b["task_id"]
    previous = await restored_tools["get_interpretation_report"].call(
        task_reference={"kind": "PREVIOUS_TASK"},
        selector="LATEST_SUCCESSFUL",
    )
    assert previous.metadata["result"]["task_id"] == task_a["task_id"]
    assert restored_runner.active_task_id == task_a["task_id"]
    await restored_factory.shutdown()


def test_agent_rejects_every_unapproved_tool(data_dir):
    tools = build_task_tools(configured_runner(data_dir))
    for name in ("calculate_sw", "identify_lithology", "evaluate_petrophysics", "unknown"):
        with pytest.raises(ValueError, match="任务级 Tool"):
            LoggingInterpretationDemoAgent(
                name="test", system_prompt="", model=SimpleNamespace(model="qwen-plus"),
                toolkit=Toolkit(tools=[*tools, SimpleNamespace(name=name)]),
            )
    agent = LoggingInterpretationDemoAgent(
        name="test", system_prompt="", model=SimpleNamespace(model="qwen-plus"),
        toolkit=Toolkit(tools=tools),
    )
    names = {t.name for group in agent.toolkit.tool_groups for t in group.tools}
    assert names == ALLOWED_TASK_TOOLS
