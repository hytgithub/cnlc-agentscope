"""验证参数到达真实调用边界，并且 Mock 不擅自改变专业预设数值。"""

import json

import httpx

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.models import MockFixture, TaskRequest
from cnlc_agent.domain.override import ExecutionContext, InterpretationOverride
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.mock import MockModelGateway
from cnlc_agent.infrastructure.model_gateway import OpenAICompatibleModelGateway
from cnlc_agent.tools.contracts import ToolCaller, ToolInput
from cnlc_agent.tools.mock import MockResultTool


async def test_multiround_parameters_reach_tools_models_and_metadata(data_dir, monkeypatch):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    original_fixture = fixture.model_dump_json()
    request = TaskRequest(well_id=fixture.well.well_id, instruction="PRIVATE_INSTRUCTION")
    calls = []
    models = []
    original_call = ToolCaller.call
    original_generate = MockModelGateway.generate

    async def record_call(caller, tool, tool_input):
        output = await original_call(caller, tool, tool_input)
        calls.append((tool.name, tool_input.model_copy(deep=True), output.model_copy(deep=True)))
        return output

    async def record_model(gateway, model_input):
        models.append(model_input.model_copy(deep=True))
        return await original_generate(gateway, model_input)

    monkeypatch.setattr(ToolCaller, "call", record_call)
    monkeypatch.setattr(MockModelGateway, "generate", record_model)

    async def materialize(version):
        (data_dir / f"{version.well_id}.json").write_text(version.payload.model_dump_json())

    initial, _ = await app.run_with_input(request, fixture, materialize)
    initial_saved = await app.repository.get_execution(initial.workflow_execution_id)
    assert initial.effective_override == InterpretationOverride()
    version = await app.repository.get_input_version(initial.input_version_id)
    assert version is not None
    expected = InterpretationOverride()
    for changes, start in [
        (InterpretationOverride(por=0.16, perm=0.16), StepId.W04),
        (InterpretationOverride(prediction_model="prediction-v2"), StepId.W04),
        (InterpretationOverride(sampling_interval=0.1), StepId.W02),
        (None, StepId.W01),
    ]:
        calls.clear()
        models.clear()
        if changes:
            expected = InterpretationOverride.model_validate(
                {**expected.model_dump(), **changes.model_dump(exclude_none=True)}
            )
        state, _ = await app.rerun_planned(
            request, changes=changes, force_full_rerun=changes is None, materialize=materialize
        )
        assert state.status == StepStatus.SUCCESS
        assert state.effective_override == expected
        assert state.input_version_id == version.input_version_id
        expected_steps = list(StepId)[list(StepId).index(start) :]
        assert [item.step_id for item in state.executions] == expected_steps
        assert state.raw_data == fixture.raw_data
        assert state.processed_data == fixture.raw_data
        assert InterpretationState.model_validate_json(state.model_dump_json()) == state
        saved = await app.repository.get_execution(state.workflow_execution_id)
        assert saved.override_snapshot == state.effective_override
        assert saved.input_version_id == state.input_version_id
        for name, tool_input, output in calls:
            assert set(tool_input.parameters) == set(ExecutionContext.model_fields)
            assert tool_input.parameters["execution_id"] == state.workflow_execution_id
            assert tool_input.parameters["effective_override"] == expected.model_dump(mode="json")
            assert "PRIVATE_INSTRUCTION" not in tool_input.model_dump_json()
            assert "depths" not in tool_input.parameters
            if name == "get_well_data":
                continue
            assert output.metadata["execution_id"] == state.workflow_execution_id
            assert output.metadata["effective_parameters"] == expected.model_dump(mode="json")
            assert output.metadata["prediction_model"] == expected.prediction_model
            assert output.metadata["source"] == "mock:fixture"
            assert output.metadata["prediction_source"] == "mock:prediction"
            assert output.metadata["parameter_propagated"] is True
            assert output.metadata["is_mock"] is True
            assert output.metadata["professionally_recalculated"] is False
            assert output.data["is_mock"] is True
            assert state.workflow_execution_id in output.data["source"]
        assert {name for name, _, _ in calls} >= {
            "identify_lithology", "evaluate_petrophysics", "calculate_sw", "merge_intervals"
        }
        for model_input in models:
            assert model_input.context["execution_id"] == state.workflow_execution_id
            assert model_input.context["effective_override"] == expected.model_dump(mode="json")
            assert "PRIVATE_INSTRUCTION" not in model_input.model_dump_json()
            assert "processed_data" not in model_input.context
            assert "raw_data" not in model_input.context
            assert "executions" not in model_input.context
        assert state.petrophysics_result.result == fixture.outputs["petrophysics"].result
        assert state.fluid_result.result["sw_result"]["result"] == fixture.outputs["sw"].result
        if start in {StepId.W01, StepId.W02}:
            preprocess = state.qc_result.result["preprocessing"]
            assert preprocess["requested_sampling_interval"] == 0.1
            assert preprocess["resampling_applied"] is False
            assert "pending" in preprocess["reason"]
        assert await app.repository.get_execution(initial.workflow_execution_id) == initial_saved
        assert await app.repository.get_input_version(version.input_version_id) == version
    assert fixture.model_dump_json() == original_fixture


async def test_prediction_identifier_never_changes_qwen_gateway(data_dir, monkeypatch):
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    sent = []

    def handle(request):
        payload = json.loads(request.content)
        message = json.loads(payload["messages"][1]["content"])
        sent.append((payload["model"], message["context"]))
        result = fixture.outputs[message["purpose"]].model_dump_json()
        return httpx.Response(200, json={"choices": [{"message": {"content": result}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = OpenAICompatibleModelGateway(
            base_url="https://example.test/v1", api_key="test-only", model_name="qwen-plus",
            client=client,
        )
        monkeypatch.setattr(
            OpenAICompatibleModelGateway, "from_settings", classmethod(lambda cls, *args: gateway)
        )
        app = build_application(
            AppSettings(mode="demo", model_provider="real", mock_data_dir=data_dir, _env_file=None)
        )
        request = TaskRequest(well_id=fixture.well.well_id)

        async def materialize(version):
            (data_dir / f"{version.well_id}.json").write_text(version.payload.model_dump_json())

        await app.run_with_input(request, fixture, materialize)
        state, _ = await app.rerun_planned(
            request, changes=InterpretationOverride(prediction_model="prediction-v2"),
            materialize=materialize,
        )
        assert state.status == StepStatus.SUCCESS
        assert len(sent) == 4
        assert all(name == "qwen-plus" for name, _ in sent)
        assert all(
            context["effective_override"]["prediction_model"] == "prediction-v2"
            for _, context in sent[-2:]
        )
        assert gateway.model_name == "qwen-plus"


async def test_mock_only_annotates_copies_and_preserves_fixture(fixture_data):
    fixture = MockFixture.model_validate(fixture_data)
    before = fixture.model_dump_json()

    class SharedFixture:
        async def load(self, well_id):
            return fixture

    tool = MockResultTool("evaluate_petrophysics", "petrophysics", SharedFixture())
    for model in ("prediction-v2", "prediction-v3"):
        context = ExecutionContext(
            execution_id=f"exec-{model}",
            effective_override=InterpretationOverride(por=0.16, perm=0.16, prediction_model=model),
        )
        output = await tool.execute(ToolInput(
            task_id="task", trace_id="trace", well_id=fixture.well.well_id, step_id=StepId.W05,
            parameters=context.model_dump(mode="json"),
        ))
        assert output.data["result"] == fixture.outputs["petrophysics"].result
        assert output.metadata["prediction_model"] == model
        output.data["result"]["test_mutation"] = True
        assert fixture.model_dump_json() == before


async def test_legacy_full_rerun_also_populates_state_context(data_dir):
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    request = TaskRequest(well_id="WELL_MOCK_001")
    first, _ = await app.run(request)
    override = InterpretationOverride(por=0.16)
    state, _ = await app.rerun(request, override=override)
    assert first.effective_override == InterpretationOverride()
    assert state.effective_override == override
    assert [item.step_id for item in state.executions] == list(StepId)
    override.por = 0.2
    assert state.effective_override.por == 0.16
