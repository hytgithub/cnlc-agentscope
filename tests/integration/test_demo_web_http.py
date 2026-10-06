"""Official HTTP Chat/Session/SSE integration, with only Redis and model I/O faked."""

import asyncio
import base64
import json
from pathlib import Path

import h5py
import httpx
import numpy as np
from agentscope.app.storage import RedisStorage
from agentscope.event import EventType
from fakeredis.aioredis import FakeRedis
from test_task_react import ScriptedTaskModel

from cnlc_agent.demo.agentscope_app import (
    BACKEND_MODEL_CREDENTIAL_ID,
    BACKEND_MODEL_OWNER_ID,
    create_demo_app,
)


def _minimal_gdsx(path: Path) -> bytes:
    """构造只含已知 pygdsx 元数据形状的动态测试文件。"""

    def encoded(value):
        return np.frombuffer(json.dumps(value).encode(), dtype=np.uint8).reshape(-1, 1)

    with h5py.File(path, "w") as file:
        well = file.create_group("well-visit")
        info = well.create_group("info")
        info.create_dataset("wellinfo", data=encoded({"name": "pending"}))
        curves = well.create_group("curve")
        curve = curves.create_group("GR")
        curve.create_dataset(
            "meta",
            data=encoded(
                {
                    "name": "GR",
                    "standardName": "GR",
                    "dimension": 1,
                    "dimension1Length": 1,
                }
            ),
        )
        well.create_group("table")
    return path.read_bytes()


async def test_multipart_gdsx_ingress_does_not_create_conversation_message(tmp_path, monkeypatch):
    monkeypatch.setenv("CNLC_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("CNLC_PROFESSIONAL_PROVIDER", "company_real")
    monkeypatch.setenv("CNLC_PERSISTENCE", "memory")
    monkeypatch.setenv("CNLC_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("CNLC_COMPANY_WELL_NAME", "explicit")
    monkeypatch.setenv("CNLC_COMPANY_SERVICE_ID", "service")
    monkeypatch.setenv("CNLC_COMPANY_TASK_CONFIG", "{}")
    monkeypatch.setenv("CNLC_COMPANY_PREPROCESS_OPERATIONS", '{"resample":{"enable":false}}')
    monkeypatch.setenv("CNLC_COMPANY_PREPROCESSING_URL", "http://unused/preprocess")
    monkeypatch.setenv("CNLC_COMPANY_PREDICTION_URL", "http://unused/predict")
    monkeypatch.setenv("CNLC_COMPANY_TOKEN", "unused")
    redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr(
        "cnlc_agent.demo.agentscope_app._redis_storage",
        lambda _: RedisStorage(connection_pool=redis.connection_pool),
    )
    app = create_demo_app(workspace_dir=tmp_path / "workspaces")
    headers = {"X-User-ID": "gdsx-user"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client:
            agent = await client.post("/agent/", json={"name": "GDSX"})
            agent_id = agent.json()["agent_id"]
            session = await client.post(
                "/sessions/",
                json={
                    "agent_id": agent_id,
                    "name": "GDSX ingress",
                    "chat_model_config": {
                        "type": "cnlc_mock_shell_model",
                        "credential_id": BACKEND_MODEL_CREDENTIAL_ID,
                        "model": "qwen-plus",
                        "parameters": {},
                    },
                },
            )
            session_id = session.json()["session_id"]
            content = _minimal_gdsx(tmp_path / "minimal.gdsx")
            response = await client.post(
                "/cnlc/interpretation/artifacts/gdsx",
                data={"agent_id": agent_id, "session_id": session_id, "instruction": "decode"},
                files={"file": ("minimal.gdsx", content, "application/x-hdf5")},
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["size"] == len(content)
            assert "data" not in payload and "base64" not in response.text.lower()

            # 同一用户的另一会话也不能消费该收据。
            other_session = await client.post(
                "/sessions/",
                json={
                    "agent_id": agent_id,
                    "name": "Other session",
                    "chat_model_config": {
                        "type": "cnlc_mock_shell_model",
                        "credential_id": BACKEND_MODEL_CREDENTIAL_ID,
                        "model": "qwen-plus",
                        "parameters": {},
                    },
                },
            )
            other_session_id = other_session.json()["session_id"]
            cross_session = await client.post(
                "/cnlc/interpretation/tasks/gdsx",
                json={
                    "artifact_id": payload["artifact_id"],
                    "agent_id": agent_id,
                    "session_id": other_session_id,
                },
            )
            assert cross_session.status_code == 404
            assert cross_session.json()["detail"] == "ARTIFACT_NOT_FOUND"

            # 其他用户上传同一 bytes 会复用 blob，但 receipt 和授权必须独立。
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"X-User-ID": "other-user"},
            ) as other_client:
                other_agent = await other_client.post("/agent/", json={"name": "Other"})
                other_agent_id = other_agent.json()["agent_id"]
                other_session_response = await other_client.post(
                    "/sessions/",
                    json={
                        "agent_id": other_agent_id,
                        "name": "Other owner",
                        "chat_model_config": {
                            "type": "cnlc_mock_shell_model",
                            "credential_id": BACKEND_MODEL_CREDENTIAL_ID,
                            "model": "qwen-plus",
                            "parameters": {},
                        },
                    },
                )
                other_owner_session_id = other_session_response.json()["session_id"]
                other_upload = await other_client.post(
                    "/cnlc/interpretation/artifacts/gdsx",
                    data={"agent_id": other_agent_id, "session_id": other_owner_session_id},
                    files={"file": ("same.gdsx", content, "application/x-hdf5")},
                )
                assert other_upload.status_code == 200, other_upload.text
                other_payload = other_upload.json()
                assert other_payload["sha256"] == payload["sha256"]
                assert other_payload["artifact_id"] != payload["artifact_id"]

                cross_user = await other_client.post(
                    "/cnlc/interpretation/tasks/gdsx",
                    json={
                        "artifact_id": payload["artifact_id"],
                        "agent_id": other_agent_id,
                        "session_id": other_owner_session_id,
                    },
                )
                assert cross_user.status_code == 404
                assert cross_user.json()["detail"] == "ARTIFACT_NOT_FOUND"

                reverse_cross_user = await client.post(
                    "/cnlc/interpretation/tasks/gdsx",
                    json={
                        "artifact_id": other_payload["artifact_id"],
                        "agent_id": agent_id,
                        "session_id": session_id,
                    },
                )
                assert reverse_cross_user.status_code == 404
                assert reverse_cross_user.json()["detail"] == "ARTIFACT_NOT_FOUND"

                task_request = {
                    "artifact_id": other_payload["artifact_id"],
                    "agent_id": other_agent_id,
                    "session_id": other_owner_session_id,
                    "instruction": "decode once",
                }
                first, second = await asyncio.gather(
                    other_client.post("/cnlc/interpretation/tasks/gdsx", json=task_request),
                    other_client.post("/cnlc/interpretation/tasks/gdsx", json=task_request),
                )
                assert sorted([first.status_code, second.status_code]) == [200, 409]
                conflict = first if first.status_code == 409 else second
                assert conflict.json()["detail"] == "GDSX_INGRESS_CONFLICT"

            task = await client.post(
                "/cnlc/interpretation/tasks/gdsx",
                json={
                    "artifact_id": payload["artifact_id"],
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "instruction": "decode",
                },
            )
            assert task.status_code == 200, task.text
            duplicate = await client.post(
                "/cnlc/interpretation/tasks/gdsx",
                json={
                    "artifact_id": payload["artifact_id"],
                    "agent_id": agent_id,
                    "session_id": session_id,
                },
            )
            assert duplicate.status_code == 409
            messages, _ = await app.state.storage.list_messages("gdsx-user", session_id, limit=20)
            assert messages == []


async def test_official_chat_upload_sse_and_saved_report(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv("CNLC_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("CNLC_PERSISTENCE", "memory")
    monkeypatch.setenv("CNLC_STREAM_STEP_DELAY_SECONDS", "0")
    monkeypatch.setenv("CNLC_STREAM_REPORT_CHUNK_DELAY_SECONDS", "0")
    redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr(
        "cnlc_agent.demo.agentscope_app._redis_storage",
        lambda _: RedisStorage(connection_pool=redis.connection_pool),
    )

    async def offline_shell_model(*args, **kwargs):
        return ScriptedTaskModel()

    monkeypatch.setattr("agentscope.app._service._chat.get_model", offline_shell_model)
    app = create_demo_app(workspace_dir=tmp_path / "workspaces")
    headers = {"X-User-ID": "upload-test"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client:
            response = await client.post("/agent/", json={"name": "Upload demo"})
            assert response.status_code == 201, response.text
            agent_id = response.json()["agent_id"]
            # Mock provider 由后端发布无密钥的本地模型，Web 不应要求用户配置公网凭证。
            credential = await app.state.storage.get_credential(
                BACKEND_MODEL_OWNER_ID,
                BACKEND_MODEL_CREDENTIAL_ID,
            )
            assert credential is not None
            assert credential.data["type"] == "cnlc_mock_shell_credential"
            assert "api_key" not in credential.data
            response = await client.post(
                "/sessions/",
                json={
                    "agent_id": agent_id,
                    "name": "Upload integration test",
                    "chat_model_config": {
                        "type": "cnlc_mock_shell_model",
                        "credential_id": BACKEND_MODEL_CREDENTIAL_ID,
                        "model": "qwen-plus",
                        "parameters": {},
                    },
                },
            )
            assert response.status_code == 201, response.text
            session_id = response.json()["session_id"]
            path = f"/sessions/{session_id}/stream"
            ready, finished, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
            events = []

            async def receive():
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.start":
                    assert message["status"] == 200
                    ready.set()
                if message["type"] == "http.response.body":
                    for line in message.get("body", b"").decode().splitlines():
                        if line.startswith("data: "):
                            event = json.loads(line[6:])
                            events.append(event)
                            if event["type"] == EventType.REPLY_END:
                                finished.set()

            stream = asyncio.create_task(
                app(
                    {
                        "type": "http",
                        "asgi": {"version": "3.0", "spec_version": "2.0"},
                        "http_version": "1.1",
                        "method": "GET",
                        "scheme": "http",
                        "path": path,
                        "raw_path": path.encode(),
                        "query_string": f"agent_id={agent_id}".encode(),
                        "headers": [(b"x-user-id", b"upload-test")],
                        "server": ("test", 80),
                        "client": ("test", 1234),
                        "root_path": "",
                    },
                    receive,
                    send,
                )
            )
            try:
                await asyncio.wait_for(ready.wait(), 5)
                await asyncio.sleep(0.05)  # Let the official SSE feeder subscribe.
                response = await client.post(
                    "/chat/",
                    json={
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "input": {
                            "name": "user",
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "帮我解释一下这口井"},
                                {
                                    "type": "data",
                                    "name": "well.json",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "application/json",
                                        "data": base64.b64encode(
                                            (data_dir / "WELL_MOCK_001.json").read_bytes()
                                        ).decode(),
                                    },
                                },
                            ],
                        },
                    },
                )
                assert response.status_code == 200, response.text
                await asyncio.wait_for(finished.wait(), 10)
                result = next(e for e in events if e["type"] == EventType.TOOL_RESULT_END)
                first = result["metadata"]["result"]
                assert first["execution_status"] == "QUEUED"
                runner = app.state.cnlc_task_tools.runners[("upload-test", agent_id, session_id)]
                execution_path = (
                    f"/cnlc/interpretation/agents/{agent_id}/sessions/{session_id}"
                    f"/tasks/{first['task_id']}/executions/{first['execution_id']}"
                )
                waiting_stages = []
                for expected_stage in ("DATA_DECODE", "PREPROCESS", "INTERPRET", "REPORT"):
                    async with asyncio.timeout(5):
                        while True:
                            progress_response = await client.get(
                                execution_path + "/stage-progress"
                            )
                            assert progress_response.status_code == 200
                            progress = progress_response.json()
                            if progress["waiting_confirmation_stage"] == expected_stage:
                                break
                            await asyncio.sleep(0.01)
                    waiting_stages.append(progress["waiting_confirmation_stage"])
                    assert progress["execution_status"] == "WAITING_CONFIRMATION"
                    if expected_stage == "DATA_DECODE":
                        assert progress["confirmed_stages"] == []
                        assert progress["stage_status"] == "WAITING_CONFIRM"
                    if expected_stage == "REPORT":
                        assert progress["candidate_report_available"] is True
                        before_confirm = await client.get(execution_path)
                        assert before_confirm.status_code == 200
                        assert before_confirm.json()["report_ready"] is False
                        assert before_confirm.json()["report_markdown"] is None
                    confirmed = await client.post(
                        execution_path + f"/stages/{expected_stage}/confirm",
                        json={"expected_stage_run_id": progress["current_stage_run_id"]},
                    )
                    assert confirmed.status_code == 200, confirmed.text
                    assert confirmed.json()["execution_id"] == first["execution_id"]
                assert waiting_stages == ["DATA_DECODE", "PREPROCESS", "INTERPRET", "REPORT"]
                completed = await runner.wait_for_completion(
                    first["task_id"], first["execution_id"]
                )
                assert completed.execution_status == "SUCCESS"
                report = await runner.repository.get_execution_report(first["execution_id"])
                assert report
                tool_result_index = next(
                    index
                    for index, event in enumerate(events)
                    if event["type"] == EventType.TOOL_RESULT_END
                )
                reply_end_index = next(
                    index
                    for index, event in enumerate(events)
                    if event["type"] == EventType.REPLY_END
                )
                assert tool_result_index < reply_end_index
                progress = "".join(
                    event["delta"]
                    for event in events[tool_result_index + 1 : reply_end_index]
                    if event["type"] == EventType.THINKING_BLOCK_DELTA
                )
                assistant_text = "".join(
                    event["delta"]
                    for event in events
                    if event["type"] == EventType.TEXT_BLOCK_DELTA
                )
                assert "▶ W01" in progress
                assert "当前阶段已完成，结果已发送到聊天中，等待确认继续" in progress
                assert "数据解编阶段结果" in assistant_text
                assert "确认并继续" in assistant_text
                assert "cnlc-stage-confirm" in assistant_text
                assert "api_key" not in json.dumps(events)
                # The service persists the same projected Assistant message after REPLY_END.
                async with asyncio.timeout(5):
                    while True:
                        messages, _ = await app.state.storage.list_messages(
                            "upload-test", session_id
                        )
                        if any(m.role == "assistant" for m in messages):
                            break
                        await asyncio.sleep(0.01)
                assert not any((m.get_text_content() or "") == report for m in messages)
                # 每轮官方服务重新创建 Agent/Tools，内存仓库仍由同一会话 runner 持有。
                for text, command in [
                    ("只重新算Sw", None),
                    ("把全井孔隙度、渗透率改成0.16", "MODIFY"),
                    ("给我上一版报告", "GET_REPORT"),
                    ("现在处理到哪里了？", "STATUS"),
                    ("全井改成0.17", None),
                    ("孔隙度", "MODIFY"),
                ]:
                    execution_count = len(await runner.repository.list_executions(first["task_id"]))
                    previous_count = sum(m.role == "assistant" for m in messages)
                    events.clear()
                    finished.clear()
                    response = await client.post(
                        "/chat/",
                        json={
                            "agent_id": agent_id,
                            "session_id": session_id,
                            "input": {
                                "name": "user",
                                "role": "user",
                                "content": [{"type": "text", "text": text}],
                            },
                        },
                    )
                    assert response.status_code == 200, response.text
                    await asyncio.wait_for(finished.wait(), 10)
                    tool_results = [e for e in events if e["type"] == EventType.TOOL_RESULT_END]
                    assert len(tool_results) == 1
                    operation = tool_results[0]["metadata"]["operation"]
                    # 官方 HTTP 每轮新建 Agent，仍须保留同一 Runner 的 Pending owner。
                    if command is None:
                        assert operation["outcome"] == (
                            "NEED_CLARIFICATION"
                            if text == "全井改成0.17"
                            else "KNOWN_UNSUPPORTED"
                        ), operation
                        assert not operation["created_execution_ids"]
                        assert (
                            len(await runner.repository.list_executions(first["task_id"]))
                            == execution_count
                        )
                    else:
                        assert tool_results[0]["state"] == "success", tool_results
                    payload = tool_results[0]["metadata"]["result"]
                    if command is not None:
                        assert payload["command"] == command
                        assert payload["task_id"] == first["task_id"]
                    if command == "MODIFY":
                        assert payload["reused_steps"] == ["W01", "W02", "W03"]
                        assert payload["execution_status"] == "QUEUED"
                        await runner.wait_for_completion(first["task_id"], payload["execution_id"])
                        assert (
                            len(await runner.repository.list_executions(first["task_id"]))
                            == execution_count + 1
                        )
                    if command == "GET_REPORT":
                        assert payload["execution_id"] == first["execution_id"]
                        assert payload["report_markdown"] == report
                    async with asyncio.timeout(5):
                        while True:
                            messages, _ = await app.state.storage.list_messages(
                                "upload-test", session_id
                            )
                            if sum(m.role == "assistant" for m in messages) > previous_count:
                                break
                            await asyncio.sleep(0.01)
            finally:
                disconnected.set()
                await asyncio.wait_for(stream, 5)
    await redis.aclose()
