"""Exercise the exact Web UI message shape through AgentScope to the existing workflow."""

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import cast

import pytest
from agentscope.app._router._schema._chat import ChatRequest
from agentscope.event import (
    ReplyEndEvent,
    TextBlockDeltaEvent,
    ThinkingBlockDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import AssistantMsg, Msg, TextBlock, UserMsg
from agentscope.model import ChatModelBase
from agentscope.tool import Toolkit

from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent
from cnlc_agent.demo.tools import RunWellInterpretationTool
from cnlc_agent.demo.uploads import MAX_UPLOAD_BYTES, UploadError, parse_upload
from cnlc_agent.infrastructure.mock import MockModelGateway
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
        toolkit=Toolkit(tools=[RunWellInterpretationTool()]),
    )


async def collect_reply(message):
    agent = demo_agent()
    events = [event async for event in agent.reply_stream(message)]
    reply = AssistantMsg(id=events[0].reply_id, name=agent.name, content=[])
    for event in events:
        reply.append_event(event)  # Official service and UI use this same event projection.
    return events, reply


async def test_upload_runs_all_steps_and_streams_unmodified_report(data_dir):
    data = json.loads((data_dir / "WELL_MOCK_001.json").read_bytes())
    del data["well"]["well_id"]
    data["well"]["name"] = "上传的专属演示井"
    data["outputs"]["fluid"]["result"] = {"fluid_type": "UPLOAD_FLUID"}
    events, reply = await collect_reply(uploaded_message(json.dumps(data).encode()))

    calls = [e for e in events if isinstance(e, ToolCallStartEvent)]
    assert len(calls) == 1 and calls[0].tool_call_name == "run_well_interpretation"
    result_event = next(e for e in events if isinstance(e, ToolResultEndEvent))
    result = result_event.metadata["result"]
    assert result["status"] == "SUCCESS"
    assert result["well_id"].startswith("UPLOAD_")
    assert result["task_id"]
    assert result["completed_steps"] == [f"W{i:02}" for i in range(1, 11)]
    assert [step["id"] for step in result["steps"]] == [f"W{i:02}" for i in range(1, 11)]
    assert result["steps"][5]["source"] == "qwen-plus"
    assert result["steps"][8]["source"] == "demo-skip"
    assert result["steps"][5]["output_summary"]["result"]["fluid_type"] == "UPLOAD_FLUID"
    text = reply.get_text_content()
    progress_block = reply.get_content_blocks("thinking")[0]
    progress = progress_block.thinking
    assert result["report_markdown"] in text
    assert "上传的专属演示井" in text
    assert "UPLOAD_FLUID" in text
    assert all(word in text for word in ["Demo", "Mock", "流体", "油气水层", "层段"])
    assert "正在处理" not in text
    assert "正在生成报告" not in text
    assert progress_block.finished_at is not None
    report_deltas = [event for event in events if isinstance(event, TextBlockDeltaEvent)]
    assert len(report_deltas) > 1
    assert "".join(event.delta for event in report_deltas) == result["report_markdown"]
    for step in result["completed_steps"]:
        assert f"{step}：SUCCESS" in progress
    w06_start = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, ThinkingBlockDeltaEvent) and "W06" in e.delta and "正在处理" in e.delta
    )
    w06_end = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, ThinkingBlockDeltaEvent) and "W06：SUCCESS" in e.delta
    )
    assert w06_start < w06_end < events.index(result_event)
    assert isinstance(events[-1], ReplyEndEvent)
    assert event_observer.get() is None


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
    assert result["status"] == "SUCCESS"
    assert result["well_id"] == "WELL_001"
    assert "合成井 001" in reply.get_text_content()


async def test_concurrent_uploads_with_same_well_id_are_isolated(data_dir):
    data = json.loads((data_dir / "WELL_MOCK_001.json").read_bytes())
    messages = []
    for name in ("UPLOAD_A", "UPLOAD_B"):
        data["well"]["name"] = name
        messages.append(uploaded_message(json.dumps(data).encode(), "../../anything.json"))
    first, second = await asyncio.gather(*(collect_reply(m) for m in messages))
    assert "UPLOAD_A" in first[1].get_text_content()
    assert "UPLOAD_B" not in first[1].get_text_content()
    assert "UPLOAD_B" in second[1].get_text_content()
    assert "UPLOAD_A" not in second[1].get_text_content()
    assert "UPLOAD_A" not in (data_dir / "WELL_MOCK_001.json").read_text()


@pytest.mark.parametrize("content", [b"not json secret-token", b'{"well":{}}', b"[]"])
async def test_invalid_upload_has_safe_reply_and_does_not_start_workflow(content):
    events, reply = await collect_reply(uploaded_message(content))
    assert not any(isinstance(e, ToolCallStartEvent) for e in events)
    assert "格式无效" in reply.get_text_content()
    assert "secret-token" not in reply.get_text_content()


async def test_no_attachment_prompts_for_file_not_ids():
    events, reply = await collect_reply(UserMsg(name="user", content="帮我解释一下这口井"))
    assert "请上传" in reply.get_text_content()
    assert "well_id" not in reply.get_text_content()
    assert not any(isinstance(e, ToolCallStartEvent) for e in events)


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

    monkeypatch.setattr("cnlc_agent.demo.tools.run_uploaded_well", broken)
    events, reply = await collect_reply(
        uploaded_message((data_dir / "WELL_MOCK_001.json").read_bytes())
    )
    serialized = json.dumps([event.model_dump(mode="json") for event in events])
    assert "sk-private-test-key" not in serialized
    assert "RAW_MODEL_EXCEPTION" not in serialized
    assert "解释任务失败" in reply.get_text_content()
    assert next(e for e in events if isinstance(e, ToolResultEndEvent)).state == "error"


async def test_progress_arrives_while_model_is_waiting_and_interrupt_cleans_up(
    data_dir, monkeypatch
):
    entered, released, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = MockModelGateway.generate

    async def waiting_model(self, request):
        if request.purpose == "fluid":
            entered.set()
            try:
                await released.wait()
            finally:
                cleaned.set()
        return await original(self, request)

    monkeypatch.setattr(MockModelGateway, "generate", waiting_model)
    agent = demo_agent()
    events = []
    progress_seen = asyncio.Event()

    async def consume():
        async for event in agent.reply_stream(
            uploaded_message((data_dir / "WELL_MOCK_001.json").read_bytes())
        ):
            events.append(event)
            if isinstance(event, ThinkingBlockDeltaEvent) and "W06" in event.delta:
                progress_seen.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(progress_seen.wait(), 5)
        assert not released.is_set()
        assert not any(isinstance(e, ToolResultEndEvent) for e in events)
        task.cancel()
        await asyncio.wait_for(task, 5)
        assert cleaned.is_set()
        assert events[-1].finished_reason == "interrupted"
        assert next(e for e in events if isinstance(e, ToolResultEndEvent)).state == "interrupted"
        assert agent.toolkit.tool_groups[0].tools[0].upload is None
    finally:
        released.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
