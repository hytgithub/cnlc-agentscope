"""Official HTTP Chat/Session/SSE integration, with only Redis and model I/O faked."""

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
from agentscope.app.storage import RedisStorage
from agentscope.event import EventType
from fakeredis.aioredis import FakeRedis

from cnlc_agent.demo.agentscope_app import create_demo_app


async def test_official_chat_upload_sse_and_saved_report(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv("CNLC_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("CNLC_PERSISTENCE", "memory")
    redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr(
        "cnlc_agent.demo.agentscope_app._redis_storage",
        lambda _: RedisStorage(connection_pool=redis.connection_pool),
    )

    async def offline_shell_model(*args, **kwargs):
        return SimpleNamespace(model="qwen-plus")

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
            response = await client.post(
                "/credential/",
                json={
                    "data": {
                        "type": "dashscope_credential",
                        "name": "offline",
                        "api_key": "sk-offline-test-only",
                    }
                },
            )
            assert response.status_code == 201, response.text
            credential_id = response.json()["credential_id"]
            response = await client.post(
                "/sessions/",
                json={
                    "agent_id": agent_id,
                    "name": "Upload integration test",
                    "chat_model_config": {
                        "type": "dashscope_chat_model",
                        "credential_id": credential_id,
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
                assert result["metadata"]["result"]["status"] == "SUCCESS"
                report = result["metadata"]["result"]["report_markdown"]
                report_chunks = [
                    e["delta"] for e in events if e["type"] == EventType.TEXT_BLOCK_DELTA
                ]
                assert len(report_chunks) > 1
                assert "".join(report_chunks) == report
                assert all(
                    any(f"W{i:02}：SUCCESS" in e.get("delta", "") for e in events)
                    for i in range(1, 11)
                )
                assert "sk-offline-test-only" not in json.dumps(events)
                # The service persists the same projected Assistant message after REPLY_END.
                async with asyncio.timeout(5):
                    while True:
                        messages, _ = await app.state.storage.list_messages(
                            "upload-test", session_id
                        )
                        if any(m.role == "assistant" for m in messages):
                            break
                        await asyncio.sleep(0.01)
                assert any(report in (m.get_text_content() or "") for m in messages)
            finally:
                disconnected.set()
                await asyncio.wait_for(stream, 5)
    await redis.aclose()
