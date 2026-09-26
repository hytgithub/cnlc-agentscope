"""Official HTTP Chat/Session/SSE integration, with only Redis and model I/O faked."""

import asyncio
import base64
import json

import httpx
from agentscope.app.storage import RedisStorage
from agentscope.event import EventType
from fakeredis.aioredis import FakeRedis
from test_task_react import ScriptedTaskModel

from cnlc_agent.demo.agentscope_app import (
    BACKEND_MODEL_CREDENTIAL_ID,
    BACKEND_MODEL_OWNER_ID,
    create_demo_app,
)


async def test_official_chat_upload_sse_and_saved_report(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv("CNLC_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("CNLC_PERSISTENCE", "memory")
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
                completed = await runner.wait_for_completion(
                    first["task_id"], first["execution_id"]
                )
                assert completed.execution_status == "SUCCESS"
                report = await runner.repository.get_execution_report(first["execution_id"])
                report_chunks = [
                    e["delta"] for e in events if e["type"] == EventType.TEXT_BLOCK_DELTA
                ]
                assert "".join(report_chunks) == report
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
                positions = [progress.index(f"  ▶ W{i:02}") for i in range(1, 11)]
                assert positions == sorted(positions)
                for tool in (
                    "get_well_data",
                    "check_curve_quality",
                    "identify_lithology",
                    "evaluate_petrophysics",
                    "calculate_sw",
                    "merge_intervals",
                ):
                    assert f"调用工具：{tool}" in progress
                    assert f"✓ {tool} 执行成功" in progress
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
                assert any((m.get_text_content() or "") == report for m in messages)
                # 每轮官方服务重新创建 Agent/Tools，内存仓库仍由同一会话 runner 持有。
                for text, command in [
                    ("把孔隙度、渗透率改成0.16", "MODIFY"),
                    ("给我上一版报告", "GET_REPORT"),
                    ("现在处理到哪里了？", "STATUS"),
                ]:
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
                    assert tool_results[0]["state"] == "success", tool_results
                    payload = tool_results[0]["metadata"]["result"]
                    assert payload["command"] == command
                    assert payload["task_id"] == first["task_id"]
                    if command == "MODIFY":
                        assert payload["reused_steps"] == ["W01", "W02", "W03"]
                        assert payload["execution_status"] == "QUEUED"
                        await runner.wait_for_completion(first["task_id"], payload["execution_id"])
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
