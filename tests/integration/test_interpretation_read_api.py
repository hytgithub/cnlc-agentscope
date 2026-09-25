"""测井解释只读 HTTP API 的会话绑定与仓库读取验证。"""

from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from agentscope.app.storage import RedisStorage
from fakeredis.aioredis import FakeRedis

from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.agentscope_app import create_demo_app
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.domain.models import MockFixture, TaskRequest


async def test_read_api_queued_detail_and_session_isolation(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv("CNLC_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("CNLC_PERSISTENCE", "memory")
    redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr(
        "cnlc_agent.demo.agentscope_app._redis_storage",
        lambda _: RedisStorage(connection_pool=redis.connection_pool),
    )
    app = create_demo_app(workspace_dir=tmp_path / "workspaces")
    runner = TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
    )
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    request = TaskRequest(well_id=fixture.well.well_id)
    with TemporaryDirectory() as directory:
        async with runner.context(Path(directory)) as service:
            execution = await service.prepare_initial_with_input(request, fixture)
    runner.observed_task_ids.add(request.task_id)
    app.state.cnlc_task_tools.runners[("owner", "agent-a", "session-a")] = runner

    async def reject_model_calls(*_args, **_kwargs):
        raise AssertionError("read API must not call a model")

    monkeypatch.setattr(
        "cnlc_agent.infrastructure.mock.MockModelGateway.generate", reject_model_calls
    )

    path = f"/cnlc/interpretation/agents/agent-a/sessions/session-a/tasks/{request.task_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path, headers={"X-User-ID": "owner"})
        assert response.status_code == 200
        body = response.json()
        assert body["current_execution"]["execution_status"] == "QUEUED"
        assert [step["display_status"] for step in body["current_execution"]["steps"]] == [
            "PENDING"
        ] * 10
        assert body["current_execution"]["report_markdown"] is None

        detail = await client.get(
            f"{path}/executions/{execution.execution_id}", headers={"X-User-ID": "owner"}
        )
        assert detail.status_code == 200
        assert detail.json()["execution_id"] == execution.execution_id

        for denied_path, headers in [
            (path.replace("session-a", "session-b"), {"X-User-ID": "owner"}),
            (path, {"X-User-ID": "other"}),
            (path, {}),
            (path.replace(request.task_id, "TASK_UNKNOWN"), {"X-User-ID": "owner"}),
        ]:
            denied = await client.get(denied_path, headers=headers)
            assert denied.status_code == 404
            assert denied.json()["detail"] == "TASK_NOT_FOUND"
    await runner.dispatcher.shutdown()
    await redis.aclose()
