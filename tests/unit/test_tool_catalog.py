"""Tool Catalog 是现有业务 Tool 与 provider batch 的唯一步骤归属来源。"""

import pytest

from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.errors import ToolError
from cnlc_agent.domain.stages import InterpretationStage
from cnlc_agent.infrastructure.mock import MockWellRepository
from cnlc_agent.infrastructure.telemetry import LoggingTelemetry
from cnlc_agent.tools.catalog import (
    BUSINESS_TOOL_BINDINGS,
    PROVIDER_BATCH_BINDINGS,
    TOOL_BINDINGS,
    tool_binding,
    validate_injected_tools,
)
from cnlc_agent.tools.contracts import ToolCaller, ToolInput
from cnlc_agent.tools.mock import GetWellDataTool


def test_catalog_has_unique_business_and_provider_bindings():
    assert len(BUSINESS_TOOL_BINDINGS) == 10
    assert len(PROVIDER_BATCH_BINDINGS) == 4
    assert len({item.tool_code for item in TOOL_BINDINGS}) == len(TOOL_BINDINGS)
    assert all(not item.provider_batch for item in BUSINESS_TOOL_BINDINGS)
    assert all(item.provider_batch for item in PROVIDER_BATCH_BINDINGS)
    assert tool_binding("prepare_report").allowed_steps == (StepId.W10,)
    assert tool_binding("prepare_report").business_stage == InterpretationStage.INTERPRET
    assert tool_binding("company_report").business_stage == InterpretationStage.INTERPRET
    assert tool_binding("company_interpretation").allowed_steps == tuple(
        list(StepId)[3:9]
    )


async def test_registered_tool_wrong_step_is_rejected_before_execution(data_dir, monkeypatch):
    tool = GetWellDataTool(MockWellRepository(data_dir))
    executed = False
    original = tool.execute

    async def observe(request):
        nonlocal executed
        executed = True
        return await original(request)

    monkeypatch.setattr(tool, "execute", observe)
    request = ToolInput(
        task_id="task",
        trace_id="trace",
        well_id="WELL_MOCK_001",
        step_id=StepId.W05,
    )
    with pytest.raises(ToolError) as caught:
        await ToolCaller(LoggingTelemetry(), 1).call(tool, request)
    assert caught.value.code == "TOOL_STEP_MISMATCH"
    assert executed is False


def test_bootstrap_validation_rejects_unknown_or_misnamed_tool(data_dir):
    tool = GetWellDataTool(MockWellRepository(data_dir))
    with pytest.raises(ValueError, match="key does not match"):
        validate_injected_tools({"wrong": tool})

    tool.name = "unknown_tool"
    with pytest.raises(ValueError, match="not registered"):
        validate_injected_tools({"unknown_tool": tool})
