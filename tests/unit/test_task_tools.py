"""任务工具的会话隔离、输入重建、错误和注册边界。"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from agentscope.tool import Toolkit

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.agentscope_app import SessionTaskToolFactory
from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent
from cnlc_agent.demo.task_tools import ALLOWED_TASK_TOOLS, TaskCommandRunner, build_task_tools
from cnlc_agent.domain.models import MockFixture
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
    assert contexts == ["postgres-redis", "postgres-redis"]
    assert await session.repository.get_task(first["task_id"]) is None


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
