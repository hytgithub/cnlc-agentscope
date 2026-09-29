"""阶段进度与确认 HTTP API 的最小会话隔离回归测试。"""

import asyncio
from pathlib import Path

import httpx
from agentscope.app.storage import RedisStorage
from fakeredis.aioredis import FakeRedis

from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.agentscope_app import create_demo_app
from cnlc_agent.demo.operation_interaction import (
    ConfirmStageRequest,
    OperationInteractionController,
)
from cnlc_agent.demo.task_context import TaskReference
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.domain.execution import ExecutionRunMode
from cnlc_agent.domain.models import MockFixture
from cnlc_agent.domain.session_binding import TaskSessionIdentity
from cnlc_agent.domain.stages import InterpretationStage


async def _wait_for_stage(runner: TaskCommandRunner, task_id: str, execution_id: str):
    """等待后台 Worker 到达阶段边界，避免测试依赖固定调度时序。"""

    for _ in range(100):
        progress = await runner.get_stage_progress(task_id, execution_id)
        if progress.waiting_confirmation_stage is not None:
            return progress
        await asyncio.sleep(0.01)
    raise AssertionError("staged execution did not reach confirmation boundary")


async def test_stage_progress_and_confirm_reuses_execution_with_session_guard(
    tmp_path: Path, data_dir: Path, monkeypatch
):
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
        session_identity=TaskSessionIdentity(
            user_id="alice", agent_id="agent-a", session_id="session-a"
        ),
    )
    app.state.cnlc_task_tools.runners[("alice", "agent-a", "session-a")] = runner
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())

    try:
        started = await runner.start_uploaded(
            fixture,
            "分阶段解释",
            run_mode=ExecutionRunMode.STAGED_CONFIRMATION,
        )
        task_id = started.task_id
        execution_id = started.execution_id
        waiting = await _wait_for_stage(runner, task_id, execution_id)
        assert waiting.execution_status.value == "WAITING_CONFIRMATION"
        assert waiting.waiting_confirmation_stage == InterpretationStage.DATA_DECODE
        assert waiting.stage_result is not None
        assert waiting.candidate_report is None

        base = (
            "/cnlc/interpretation/agents/agent-a/sessions/session-a/tasks/"
            f"{task_id}/executions/{execution_id}"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(base + "/stage-progress", headers={"X-User-ID": "alice"})
            assert response.status_code == 200
            body = response.json()
            assert body["execution_id"] == execution_id
            assert body["waiting_confirmation_stage"] == "DATA_DECODE"
            assert body["stage_status"] == "WAITING_CONFIRM"
            assert body["candidate_report_available"] is False

            stale = await client.post(
                base + "/stages/DATA_DECODE/confirm",
                headers={"X-User-ID": "alice"},
                json={"expected_stage_run_id": "stale-stage-run"},
            )
            assert stale.status_code == 409
            assert stale.json()["detail"] == "STAGE_CONFIRMATION_CONFLICT"

            confirmed = await client.post(
                base + "/stages/DATA_DECODE/confirm",
                headers={"X-User-ID": "alice"},
                json={"expected_stage_run_id": waiting.current_stage_run_id},
            )
            assert confirmed.status_code == 200
            assert confirmed.json()["execution_id"] == execution_id
            assert confirmed.json()["confirmed_stages"] == ["DATA_DECODE"]

            duplicate = await client.post(
                base + "/stages/DATA_DECODE/confirm",
                headers={"X-User-ID": "alice"},
                json={"expected_stage_run_id": waiting.current_stage_run_id},
            )
            assert duplicate.status_code == 409
            assert duplicate.json()["detail"] == "STAGE_CONFIRMATION_CONFLICT"

            next_waiting = await _wait_for_stage(runner, task_id, execution_id)
            assert next_waiting.execution_id == execution_id
            assert next_waiting.waiting_confirmation_stage == InterpretationStage.PREPROCESS

            denied = await client.get(
                base + "/stage-progress", headers={"X-User-ID": "bob"}
            )
            assert denied.status_code == 404
            assert denied.json()["detail"] == "TASK_NOT_FOUND"
    finally:
        await runner.dispatcher.shutdown()
        await redis.aclose()


async def test_chat_confirmation_resolves_active_and_named_waiting_wells(data_dir: Path):
    """两口井可交错停留；聊天确认与按钮共享同一 Runner 能力。"""

    runner = TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
    )
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    second_fixture = fixture.model_copy(
        update={"well": fixture.well.model_copy(update={"well_id": "WELL_MOCK_002"})}
    )
    try:
        first = await runner.start_uploaded(
            fixture, "井 A", run_mode=ExecutionRunMode.STAGED_CONFIRMATION
        )
        second = await runner.start_uploaded(
            second_fixture, "井 B", run_mode=ExecutionRunMode.STAGED_CONFIRMATION
        )
        await _wait_for_stage(runner, first.task_id, first.execution_id)
        await _wait_for_stage(runner, second.task_id, second.execution_id)

        named = await runner.confirm_waiting_stage(
            TaskReference(kind="WELL_ID", value="WELL_MOCK_001"), actor="chat"
        )
        assert named.command == "CONFIRM"
        assert named.well_name == fixture.well.name
        first_next = await _wait_for_stage(runner, first.task_id, first.execution_id)
        second_waiting = await runner.get_stage_progress(second.task_id, second.execution_id)
        assert first_next.waiting_confirmation_stage == InterpretationStage.PREPROCESS
        assert second_waiting.waiting_confirmation_stage == InterpretationStage.DATA_DECODE

        controller = OperationInteractionController(runner)
        runner.set_active_task(second.task_id)
        active = await controller.handle(ConfirmStageRequest(mode="CONFIRM_STAGE"))
        assert active.outcome == "SUCCESS"
        assert active.task_results[0].well_name == second_fixture.well.name
    finally:
        await runner.dispatcher.shutdown()
