"""Opt-in network check for the configured DashScope OpenAI-compatible endpoint."""

import os

import pytest

from cnlc_agent.application.ports import ModelRequest
from cnlc_agent.infrastructure.model_gateway import OpenAICompatibleModelGateway

_RUN = os.getenv("CNLC_RUN_REAL_MODEL_TEST") == "1"
_API_KEY = os.getenv("MODEL_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
pytestmark = pytest.mark.skipif(
    not (_RUN and _API_KEY),
    reason=(
        "Set CNLC_RUN_REAL_MODEL_TEST=1 and MODEL_API_KEY/"
        "DASHSCOPE_API_KEY for the opt-in network test"
    ),
)


@pytest.mark.asyncio
async def test_dashscope_qwen_plus_returns_json_object():
    gateway = OpenAICompatibleModelGateway(
        base_url=os.getenv("MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        api_key=_API_KEY or "",
        model_name=os.getenv("MODEL_NAME", "qwen-plus"),
        timeout_seconds=float(os.getenv("CNLC_MODEL_TIMEOUT_SECONDS", "30")),
        max_retries=int(os.getenv("CNLC_MODEL_MAX_RETRIES", "2")),
    )
    try:
        result = await gateway.generate(
            ModelRequest(
                task_id="task-04-network-probe",
                trace_id="trace-04-network-probe",
                well_id="WELL_MODEL_PROBE",
                purpose="json_object_probe",
                context={"probe": "task-04", "return": "one JSON object"},
            )
        )
    finally:
        await gateway.aclose()
    assert isinstance(result, dict)
    assert result
