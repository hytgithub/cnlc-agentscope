import asyncio

import pytest
from pydantic import ValidationError

from cnlc_agent.config.settings import AppSettings, ConnectionSettings
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import DataError, ToolError
from cnlc_agent.domain.models import MockFixture, TaskRequest
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.mock import InMemoryStateStore, MockWellRepository
from cnlc_agent.infrastructure.telemetry import LoggingTelemetry
from cnlc_agent.tools.contracts import ToolCaller, ToolInput, ToolOutput


def test_schema_rejects_misaligned_curves_and_non_mock_fixture(fixture_data):
    fixture_data["raw_data"]["curves"]["GR"]["values"].pop()
    with pytest.raises(ValidationError, match="align"):
        MockFixture.model_validate(fixture_data)
    fixture_data["raw_data"]["curves"]["GR"]["values"].append(1)
    fixture_data["outputs"]["fluid"]["is_mock"] = False
    with pytest.raises(ValidationError, match="is_mock"):
        MockFixture.model_validate(fixture_data)


@pytest.mark.parametrize(
    "depths", [[2001, 2000, 2002], [2000, 2000, 2001], [2000, float("nan"), 2001]]
)
def test_bad_depths_rejected(fixture_data, depths):
    fixture_data["raw_data"]["depths"] = depths
    with pytest.raises(ValidationError):
        MockFixture.model_validate(fixture_data)


def test_configuration_and_secret_repr(monkeypatch):
    monkeypatch.setenv("CNLC_TOOL_TIMEOUT_SECONDS", "3.5")
    assert AppSettings(_env_file=None).tool_timeout_seconds == 3.5
    with pytest.raises(ValidationError):
        AppSettings(mode="real", _env_file=None)
    with pytest.raises(ValidationError):
        AppSettings(tool_timeout_seconds=0, _env_file=None)
    settings = ConnectionSettings(model_api_key="test-secret-value", _env_file=None)
    assert "test-secret-value" not in repr(settings)


async def test_store_keeps_isolated_snapshots():
    store = InMemoryStateStore()
    state = InterpretationState(task=TaskRequest(well_id="WELL_MOCK_001"))
    await store.save(state)
    state.warnings.append("local mutation")
    saved = await store.get(state.task.task_id)
    assert saved.warnings == []
    saved.warnings.append("read mutation")
    assert (await store.get(state.task.task_id)).warnings == []


async def test_loader_rejects_path_traversal(data_dir):
    with pytest.raises(DataError) as caught:
        await MockWellRepository(data_dir).load("../../outside")
    assert caught.value.code == "INVALID_WELL_ID"


async def test_tool_timeout_is_classified():
    class HangingTool:
        name = "hang"

        async def execute(self, request):
            await asyncio.Event().wait()

    caller = ToolCaller(LoggingTelemetry(), timeout_seconds=0.01)
    request = ToolInput(
        task_id="test", trace_id="trace", well_id="WELL_MOCK_001", step_id=StepId.W05
    )
    with pytest.raises(ToolError) as caught:
        await caller.call(HangingTool(), request)
    assert caught.value.code == "TOOL_TIMEOUT"
    assert caught.value.retryable is True


async def test_failed_tool_cannot_be_treated_as_success():
    class FailedTool:
        name = "failed"

        async def execute(self, request):
            return ToolOutput(status=StepStatus.FAILED, data={"porosity": 0.99})

    request = ToolInput(
        task_id="test", trace_id="trace", well_id="WELL_MOCK_001", step_id=StepId.W05
    )
    with pytest.raises(ToolError, match="未成功"):
        await ToolCaller(LoggingTelemetry(), 1).call(FailedTool(), request)


@pytest.mark.parametrize("bad_output", [None, {"status": "NOT_A_STATUS"}, {"data": {}}])
async def test_tool_boundary_rejects_invalid_return(bad_output):
    class InvalidTool:
        name = "invalid"

        async def execute(self, request):
            return bad_output

    request = ToolInput(task_id="test", trace_id="trace", well_id="WELL_MOCK_001", step_id="W05")
    with pytest.raises(ToolError) as caught:
        await ToolCaller(LoggingTelemetry(), 1).call(InvalidTool(), request)
    assert caught.value.code == "INVALID_TOOL_OUTPUT"


async def test_tool_boundary_revalidates_mutated_model():
    class MutatedTool:
        name = "mutated"

        async def execute(self, request):
            output = ToolOutput(status=StepStatus.SUCCESS)
            output.errors.append({"unknown": "invalid error"})
            return output

    request = ToolInput(task_id="test", trace_id="trace", well_id="WELL_MOCK_001", step_id="W05")
    with pytest.raises(ToolError) as caught:
        await ToolCaller(LoggingTelemetry(), 1).call(MutatedTool(), request)
    assert caught.value.code == "INVALID_TOOL_OUTPUT"
