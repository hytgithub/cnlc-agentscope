"""Offline acceptance for the AgentScope Web adapter."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
from agentscope.app.storage import RedisStorage
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase
from agentscope.tool import Toolkit, ToolResponse
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cnlc_agent.application.runtime import application_runtime
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.demo.agentscope_app import (
    BACKEND_MODEL_CREDENTIAL_ID,
    create_demo_app,
    demo_agent_tools,
)
from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent
from cnlc_agent.demo.tools import (
    RUN_TOOL_NAME,
    InterpretationToolRunner,
    build_interpretation_tool,
)
from cnlc_agent.domain.enums import StepId, StepStatus


def _runner(data_dir: Path) -> InterpretationToolRunner:
    settings = AppSettings(
        mode="demo",
        model_provider="mock",
        mock_data_dir=data_dir,
        _env_file=None,
    )
    persistence = PersistenceSettings(persistence="memory", _env_file=None)
    connections = ConnectionSettings(_env_file=None)
    return InterpretationToolRunner(lambda: application_runtime(settings, persistence, connections))


async def test_tool_reuses_task_service_and_returns_stable_result(data_dir):
    result = await _runner(data_dir).run("WELL_MOCK_001")

    assert result.status == StepStatus.SUCCESS
    assert result.well_id == "WELL_MOCK_001"
    assert result.completed_steps == list(StepId)
    assert result.step_statuses == {step_id: StepStatus.SUCCESS for step_id in StepId}
    assert result.summary
    assert "# 虚构演示井 001测井评价报告" in result.report_markdown
    assert "W10" not in result.report_markdown
    assert set(result.model_dump()) == {
        "status",
        "task_id",
        "well_id",
        "completed_steps",
        "step_statuses",
        "steps",
        "summary",
        "report_markdown",
    }


async def test_demo_agent_registers_and_calls_only_interpretation_tool(data_dir):
    tool = build_interpretation_tool(_runner(data_dir))
    model = cast(
        ChatModelBase,
        SimpleNamespace(model="qwen-plus"),
    )
    agent = LoggingInterpretationDemoAgent(
        name="ignored",
        system_prompt="ignored",
        model=model,
        toolkit=Toolkit(tools=[tool]),
    )
    schemas = await agent.toolkit.get_tool_schemas()
    assert [schema["function"]["name"] for schema in schemas] == [RUN_TOOL_NAME]

    chunks = [
        chunk
        async for chunk in agent.toolkit.call_tool(
            ToolCallBlock(
                id="tool-call-1",
                name=RUN_TOOL_NAME,
                input=json.dumps({"well_id": "WELL_MOCK_001"}),
            ),
            agent.state,
        )
    ]
    response = chunks[-1]
    assert isinstance(response, ToolResponse)
    assert isinstance(response.content[0], TextBlock)
    payload = json.loads(response.content[0].text)
    assert payload["status"] == "SUCCESS"
    assert list(payload["step_statuses"]) == [step.value for step in StepId]
    assert "report_markdown" in payload


async def test_agent_service_adapter_constructs_offline(tmp_path):
    secret = "agent-service-test-password"
    app = create_demo_app(
        connections=ConnectionSettings(
            redis_url=SecretStr(f"redis://:{secret}@localhost:6379/0"),
            _env_file=None,
        ),
        workspace_dir=tmp_path / "workspaces",
    )

    paths = set(app.openapi()["paths"])
    assert "/health" in paths
    assert "/chat/" in paths
    assert app.state.custom_agent_cls is LoggingInterpretationDemoAgent
    tools = await demo_agent_tools("user", "agent", "session")
    assert [tool.name for tool in tools] == [RUN_TOOL_NAME]

    with TestClient(app, headers={"X-User-ID": "demo-test"}) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert secret not in json.dumps(app.openapi())
    assert secret not in response.text


async def test_backend_model_is_shared_without_exposing_api_key(tmp_path, monkeypatch):
    redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr(
        "cnlc_agent.demo.agentscope_app._redis_storage",
        lambda _: RedisStorage(connection_pool=redis.connection_pool),
    )
    api_key = "sk-backend-only-test"
    app = create_demo_app(
        connections=ConnectionSettings(
            redis_url=SecretStr("redis://localhost:6379/0"),
            model_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model_name="qwen-plus",
            model_api_key=SecretStr(api_key),
            _env_file=None,
        ),
        workspace_dir=tmp_path / "workspaces",
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-User-ID": "admin"},
        ) as client:
            response = await client.get("/credential/")

    assert response.status_code == 200
    credential = next(
        item for item in response.json()["credentials"] if item["id"] == BACKEND_MODEL_CREDENTIAL_ID
    )
    assert credential["editable"] is False
    assert credential["data"] == {
        "type": "dashscope_credential",
        "name": "CNLC Backend Model",
    }
    assert api_key not in response.text
    await redis.aclose()
