"""Exercise the exact Web UI message shape through AgentScope to the existing workflow."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from agentscope.app._router._schema._chat import ChatRequest
from agentscope.event import (
    ReplyEndEvent,
    TextBlockDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import AssistantMsg, Msg, TextBlock, UserMsg
from agentscope.model import ChatModelBase
from agentscope.tool import Toolkit

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.commands import GetStatusCommand
from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent
from cnlc_agent.demo.task_tools import TaskCommandRunner, build_task_tools
from cnlc_agent.demo.uploads import MAX_UPLOAD_BYTES, UploadError, parse_upload
from cnlc_agent.domain.inputs import fixture_digest
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository, MockModelGateway
from cnlc_agent.infrastructure.telemetry import event_observer


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    monkeypatch.setenv("CNLC_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("CNLC_PERSISTENCE", "memory")


def uploaded_message(data: bytes, name: str = "well.json") -> Msg:
    # Same POST body as TextInput -> useMessages.send -> chatApi.trigger.
    request = ChatRequest.model_validate(
        {
            "agent_id": "demo-agent",
            "session_id": "demo-session",
            "input": {
                "role": "user",
                "name": "user",
                "content": [
                    {"type": "text", "text": "帮我解释一下这口井"},
                    {
                        "type": "data",
                        "name": name,
                        "source": {
                            "type": "base64",
                            "media_type": "application/json",
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    },
                ],
            },
        }
    )
    assert isinstance(request.input, Msg)
    return request.input


def demo_agent() -> LoggingInterpretationDemoAgent:
    # No shell model is called: the attachment route must preserve the exact report.
    return LoggingInterpretationDemoAgent(
        name="demo",
        system_prompt="",
        model=cast(ChatModelBase, SimpleNamespace(model="qwen-plus")),
        toolkit=Toolkit(tools=build_task_tools()),
    )


async def collect_reply(message):
    agent = demo_agent()
    events = [event async for event in agent.reply_stream(message)]
    reply = AssistantMsg(id=events[0].reply_id, name=agent.name, content=[])
    for event in events:
        reply.append_event(event)  # Official service and UI use this same event projection.
    # 旧上传测试验证消息投影；后台任务显式等待并清理，不改变已返回的 QUEUED 事件。
    submission = next(
        (e.metadata.get("result") for e in events if isinstance(e, ToolResultEndEvent)), None
    )
    runner = agent.toolkit.tool_groups[0].tools[0]._runner
    if submission and isinstance(runner, TaskCommandRunner):
        await runner.wait_for_completion(submission["task_id"], submission["execution_id"])
        await runner.dispatcher.shutdown()
    return events, reply


async def test_upload_submits_execution_without_waiting_for_report(data_dir):
    data = json.loads((data_dir / "WELL_MOCK_001.json").read_bytes())
    del data["well"]["well_id"]
    data["well"]["name"] = "上传的专属演示井"
    data["outputs"]["fluid"]["result"] = {"fluid_type": "UPLOAD_FLUID"}
    events, reply = await collect_reply(uploaded_message(json.dumps(data).encode()))

    calls = [e for e in events if isinstance(e, ToolCallStartEvent)]
    assert len(calls) == 1 and calls[0].tool_call_name == "run_well_interpretation"
    result_event = next(e for e in events if isinstance(e, ToolResultEndEvent))
    result = result_event.metadata["result"]
    assert result["execution_status"] == "QUEUED"
    assert result["well_id"].startswith("UPLOAD_")
    assert result["task_id"]
    assert result["completed_steps"] == []
    assert result["report_ready"] is False
    text = reply.get_text_content()
    progress_block = reply.get_content_blocks("thinking")[0]
    progress = progress_block.thinking
    assert "解释任务已提交" in text
    assert result["execution_id"] in text
    assert "UPLOAD_FLUID" not in text
    assert progress_block.finished_at is not None
    report_deltas = [event for event in events if isinstance(event, TextBlockDeltaEvent)]
    assert "".join(event.delta for event in report_deltas) == text
    assert "W06" not in progress
    assert isinstance(events[-1], ReplyEndEvent)
    assert event_observer.get() is None


async def test_upload_persists_normalized_input_before_temporary_file_is_removed(
    data_dir, monkeypatch
):
    repository = InMemoryTaskRepository()
    roots: list[Path] = []

    @asynccontextmanager
    async def shared_runtime(self, root):
        roots.append(root)
        settings = self.settings.model_copy(update={"mock_data_dir": root})
        app = build_application(settings, task_repository=repository)
        try:
            yield app
        finally:
            await app.close()

    monkeypatch.setattr(TaskCommandRunner, "context", shared_runtime)
    data = json.loads((data_dir / "WELL_MOCK_001.json").read_bytes())
    del data["well"]["well_id"]
    upload = json.dumps(data).encode()
    events, reply = await collect_reply(uploaded_message(upload))
    assert "解释任务已提交" in reply.get_text_content()
    result_event = next(e for e in events if isinstance(e, ToolResultEndEvent))
    task_id = result_event.metadata["result"]["task_id"]
    versions = await repository.list_input_versions(task_id)
    assert len(versions) == 1
    version = versions[0]
    expected_fixture, _ = parse_upload([uploaded_message(upload)])
    assert version.payload == expected_fixture
    assert version.well_id.startswith("UPLOAD_")
    assert version.source_type == "UPLOAD"
    assert version.content_sha256 == fixture_digest(expected_fixture)
    assert roots and not roots[0].exists()
    restored = await repository.get_input_version(version.input_version_id)
    assert restored is not None and restored.payload == expected_fixture
    execution = (await repository.list_executions(task_id))[0]
    assert execution.input_version_id == version.input_version_id
    assert execution.status == "SUCCESS"
    task = await repository.get_task(task_id)
    assert task is not None and task.current_input_version_id == version.input_version_id


async def test_synthetic_interpretation_context_is_adapted_and_runs():
    data = {
        "schema_version": "1.0",
        "data_type": "synthetic_single_well_interpretation_context",
        "well": {"well_id": "WELL_001", "well_name": "合成井 001"},
        "log_data": {
            "target_zone_statistics": [
                {
                    "zone_id": "ZONE_01",
                    "top_m": 2400.0,
                    "bottom_m": 2410.0,
                    "statistics": {
                        "GR_API": {"mean": 45.0},
                        "RHOB_g_cm3": {"mean": 2.4},
                        "NPHI_v_v": {"mean": 0.18},
                        "DT_us_ft": {"mean": 75.0},
                        "RT_ohm_m": {"mean": 20.0},
                    },
                },
                {
                    "zone_id": "ZONE_02",
                    "top_m": 2420.0,
                    "bottom_m": 2430.0,
                    "statistics": {
                        "GR_API": {"mean": 55.0},
                        "RHOB_g_cm3": {"mean": 2.45},
                        "NPHI_v_v": {"mean": 0.2},
                        "DT_us_ft": {"mean": 78.0},
                        "RT_ohm_m": {"mean": 12.0},
                    },
                },
            ]
        },
        "interpretation_results": [
            {
                "zone_id": "ZONE_01",
                "top_m": 2400.0,
                "bottom_m": 2410.0,
                "gross_thickness_m": 10.0,
                "net_thickness_m": 8.0,
                "lithology": {"primary": "fine_sandstone"},
                "petrophysics": {"water_saturation_fraction": 0.35},
                "classification": {"fluid_type": "oil"},
            }
        ],
        "data_quality": {"overall_level": "synthetic"},
        "well_test_and_production": [{"consistency_with_log_interpretation": "consistent"}],
    }

    fixture, _ = parse_upload([uploaded_message(json.dumps(data).encode())])
    assert fixture.well.well_id == "WELL_001"
    assert fixture.well.name == "合成井 001"
    assert fixture.is_mock is True
    assert fixture.raw_data.depths == [2400.0, 2420.0]
    assert set(fixture.raw_data.curves) == {"GR", "DEN", "CNL", "AC", "RT"}
    assert all(result.is_mock for result in fixture.outputs.values())

    events, reply = await collect_reply(uploaded_message(json.dumps(data).encode()))
    result = next(e for e in events if isinstance(e, ToolResultEndEvent)).metadata["result"]
    assert result["execution_status"] == "QUEUED"
    assert result["well_id"] == "WELL_001"
    assert "解释任务已提交" in reply.get_text_content()


async def test_concurrent_uploads_with_same_well_id_are_isolated(data_dir):
    data = json.loads((data_dir / "WELL_MOCK_001.json").read_bytes())
    messages = []
    for name in ("UPLOAD_A", "UPLOAD_B"):
        data["well"]["name"] = name
        messages.append(uploaded_message(json.dumps(data).encode(), "../../anything.json"))
    first, second = await asyncio.gather(*(collect_reply(m) for m in messages))
    first_id = next(
        e for e in first[0] if isinstance(e, ToolResultEndEvent)
    ).metadata["result"]["task_id"]
    second_id = next(
        e for e in second[0] if isinstance(e, ToolResultEndEvent)
    ).metadata["result"]["task_id"]
    assert first_id != second_id
    assert "解释任务已提交" in first[1].get_text_content()
    assert "解释任务已提交" in second[1].get_text_content()
    assert "UPLOAD_A" not in (data_dir / "WELL_MOCK_001.json").read_text()


@pytest.mark.parametrize("content", [b"not json secret-token", b'{"well":{}}', b"[]"])
async def test_invalid_upload_has_safe_reply_and_does_not_start_workflow(content):
    events, reply = await collect_reply(uploaded_message(content))
    assert not any(isinstance(e, ToolCallStartEvent) for e in events)
    assert "格式无效" in reply.get_text_content()
    assert "secret-token" not in reply.get_text_content()


async def test_no_attachment_delegates_to_next_handler():
    from cnlc_agent.demo.upload_reply import UploadInterpretationReply

    agent = demo_agent()
    middleware = UploadInterpretationReply(agent.toolkit.tool_groups[0].tools[0])
    message = UserMsg(name="user", content="把孔隙度改成0.16")
    received = []

    async def next_handler(**kwargs):
        received.append(kwargs["inputs"])
        yield message

    result = [event async for event in middleware.on_reply(
        agent, {"inputs": message}, next_handler
    )]
    assert received == [message]
    assert result == [message]


def test_text_attachment_compatibility_and_size_limit(data_dir):
    text = (data_dir / "WELL_MOCK_001.json").read_text()
    fixture, _ = parse_upload(
        [UserMsg(name="user", content=[TextBlock(text=f"[File: well.txt]\n{text}")])]
    )
    assert fixture.well.well_id == "WELL_MOCK_001"
    with pytest.raises(UploadError, match="5 MiB"):
        parse_upload([uploaded_message(b"x" * (MAX_UPLOAD_BYTES + 1))])


async def test_raw_service_error_is_not_exposed(data_dir, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("sk-private-test-key RAW_MODEL_EXCEPTION https://private")

    monkeypatch.setattr(TaskCommandRunner, "start_uploaded", broken)
    events, reply = await collect_reply(
        uploaded_message((data_dir / "WELL_MOCK_001.json").read_bytes())
    )
    serialized = json.dumps([event.model_dump(mode="json") for event in events])
    assert "sk-private-test-key" not in serialized
    assert "RAW_MODEL_EXCEPTION" not in serialized
    assert "解释任务失败" in reply.get_text_content()
    assert next(e for e in events if isinstance(e, ToolResultEndEvent)).state == "error"


async def test_upload_returns_while_background_model_is_waiting(data_dir, monkeypatch):
    entered, released = asyncio.Event(), asyncio.Event()
    original = MockModelGateway.generate

    async def waiting_model(self, request):
        if request.purpose == "fluid":
            entered.set()
            await released.wait()
        return await original(self, request)

    monkeypatch.setattr(MockModelGateway, "generate", waiting_model)
    agent = demo_agent()
    events = [event async for event in agent.reply_stream(
        uploaded_message((data_dir / "WELL_MOCK_001.json").read_bytes())
    )]
    result = next(e for e in events if isinstance(e, ToolResultEndEvent)).metadata["result"]
    runner = agent.toolkit.tool_groups[0].tools[0]._runner
    assert isinstance(runner, TaskCommandRunner)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        status = await runner.execute(GetStatusCommand(task_id=result["task_id"]))
        assert status.execution_status == "RUNNING"
        assert status.current_step == "W06"
        assert not released.is_set()
    finally:
        released.set()
        await runner.wait_for_completion(result["task_id"], result["execution_id"])
        await runner.dispatcher.shutdown()
