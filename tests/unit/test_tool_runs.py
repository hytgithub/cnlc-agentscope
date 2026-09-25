"""版本化 ToolRun 的真实调用、失败归类和审计数据边界。"""

import asyncio
import json

import pytest

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.planning import ExecutionPlan, ExecutionStage, PlanAction
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import InfrastructureError, ToolError
from cnlc_agent.domain.models import MockFixture, TaskRequest
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.domain.tool_run import ToolExecutionMode, ToolRun, ToolRunStatus
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository
from cnlc_agent.infrastructure.telemetry import LoggingTelemetry
from cnlc_agent.tools.audit import bounded
from cnlc_agent.tools.contracts import ToolCaller, ToolInput, ToolOutput

ALL_TOOLS = [
    "get_well_data", "check_curve_quality", "identify_lithology",
    "evaluate_petrophysics", "calculate_sw", "merge_intervals",
]
INTERPRET_TOOLS = ALL_TOOLS[2:]


async def seeded(data_dir):
    repository = InMemoryTaskRepository()
    app = build_application(
        AppSettings(mock_data_dir=data_dir, _env_file=None), task_repository=repository
    )
    fixture = MockFixture.model_validate_json((data_dir / "WELL_MOCK_001.json").read_text())
    request = TaskRequest(well_id=fixture.well.well_id)

    async def materialize(version):
        (data_dir / f"{version.well_id}.json").write_text(version.payload.model_dump_json())

    initial, report = await app.run_with_input(request, fixture, materialize)
    return app, request, initial, report, materialize


async def test_planned_executions_persist_only_physically_called_tools(data_dir):
    app, request, initial, first_report, materialize = await seeded(data_dir)
    old = await app.list_tool_runs(request.task_id, initial.workflow_execution_id)
    assert [item.tool_code for item in old] == ALL_TOOLS
    assert [item.step_id for item in old] == [
        StepId.W01, StepId.W03, StepId.W04, StepId.W05, StepId.W06, StepId.W08
    ]
    assert all(item.execution_id == initial.workflow_execution_id for item in old)
    assert all(item.status == ToolRunStatus.SUCCESS for item in old)
    assert all(item.execution_mode == ToolExecutionMode.MOCK for item in old)
    assert all(item.source and item.source_external_call_id is None for item in old)
    assert all(item.started_at <= item.finished_at for item in old)
    assert [await app.repository.get_tool_run(item.tool_run_id) for item in old] == old
    raw_w01 = json.dumps(old[0].model_dump(mode="json"))
    assert '"depths"' not in raw_w01 and '"values"' not in raw_w01
    assert old[0].output_snapshot["data_keys"] == ["well", "raw_data", "requirements"]

    scenarios = [
        (InterpretationOverride(por=0.16, perm=0.16), False, INTERPRET_TOOLS),
        (InterpretationOverride(sampling_interval=0.1), False, ALL_TOOLS[1:]),
        (None, True, ALL_TOOLS),
    ]
    for changes, full, expected in scenarios:
        state, _ = await app.rerun_planned(
            request, changes=changes, force_full_rerun=full, materialize=materialize
        )
        runs = await app.list_tool_runs(request.task_id, state.workflow_execution_id)
        assert [item.tool_code for item in runs] == expected
        assert all(item.execution_id == state.workflow_execution_id for item in runs)
        assert all(item.input_snapshot["execution_context"]["execution_id"] ==
                   state.workflow_execution_id for item in runs)
        assert await app.list_tool_runs(request.task_id, initial.workflow_execution_id) == old

    source = await app.repository.get_execution(state.workflow_execution_id)
    plan = await app.plan_rerun(request, force_full_rerun=True)
    payload = plan.model_dump(mode="python")
    payload["planning_reason"] = "REPORT_ONLY"
    for item in payload["stage_plans"]:
        if item["stage"] != ExecutionStage.REPORT:
            item["action"] = PlanAction.REUSE
    report_only, report = await app.execute_rerun_plan(
        request, ExecutionPlan.model_validate(payload)
    )
    assert report_only.executions == []
    assert await app.list_tool_runs(request.task_id, report_only.workflow_execution_id) == []
    assert report_only.workflow_execution_id != source.execution_id
    old_report = await app.get_execution_report(request.task_id, initial.workflow_execution_id)
    assert old_report == first_report
    new_report = await app.get_execution_report(request.task_id, report_only.workflow_execution_id)
    assert new_report == report
    assert await app.repository.get_report(request.task_id) == report


async def test_workflow_tool_failure_persists_failed_run_without_changing_business_error(
    data_dir, fixture_data
):
    fixture_data["outputs"].pop("petrophysics")
    (data_dir / "WELL_MOCK_001.json").write_text(json.dumps(fixture_data))
    app = build_application(AppSettings(mock_data_dir=data_dir, _env_file=None))
    request = TaskRequest(well_id="WELL_MOCK_001")
    state, _ = await app.run(request)
    assert state.status == StepStatus.FAILED
    assert state.errors[-1].code == "MOCK_TOOL_RESULT_MISSING"
    runs = await app.list_tool_runs(request.task_id, state.workflow_execution_id)
    assert [item.tool_code for item in runs] == ALL_TOOLS[:4]
    assert runs[-1].status == ToolRunStatus.FAILED
    assert runs[-1].error_code == "MOCK_TOOL_RESULT_MISSING"


async def test_report_and_tool_queries_reject_other_task(data_dir):
    app, request, initial, _, _ = await seeded(data_dir)
    other = TaskRequest(well_id="WELL_MOCK_001")
    for query in (app.get_execution_report, app.list_tool_runs):
        with pytest.raises(InfrastructureError) as caught:
            await query(other.task_id, initial.workflow_execution_id)
        assert caught.value.code == "EXECUTION_NOT_FOUND"


class AuditTool:
    name = "audit_test_tool"
    execution_mode = ToolExecutionMode.MOCK
    source = "mock:test"

    def __init__(self, behavior):
        self.behavior = behavior

    async def execute(self, request):
        return await self.behavior(request)


async def caller_context(repository):
    request = TaskRequest(well_id="WELL_MOCK_001")
    state = InterpretationState(task=request)
    await repository.create(state)
    tool_input = ToolInput(
        task_id=request.task_id, trace_id=state.trace_id, well_id=request.well_id,
        step_id=StepId.W05, parameters=state.execution_context().model_dump(mode="json"),
    )
    return state, tool_input


@pytest.mark.parametrize(
    "behavior,expected_code",
    [
        ("timeout", "TOOL_TIMEOUT"),
        ("invalid", "INVALID_TOOL_OUTPUT"),
        ("reported", "TOOL_REPORTED_FAILURE"),
        ("tool_error", "MOCK_TOOL_RESULT_MISSING"),
        ("unexpected", "TOOL_FAILED"),
    ],
)
async def test_failed_tool_run_is_durable_and_original_error_propagates(behavior, expected_code):
    repository = InMemoryTaskRepository()
    state, tool_input = await caller_context(repository)

    async def execute(request):
        if behavior == "timeout":
            await asyncio.sleep(0.02)
        if behavior == "invalid":
            return {"invalid": "not a ToolOutput"}
        if behavior == "reported":
            return ToolOutput(status=StepStatus.FAILED)
        if behavior == "tool_error":
            raise ToolError("MOCK_TOOL_RESULT_MISSING", "secret should not be saved")
        raise RuntimeError("Bearer private-key")

    caller = ToolCaller(LoggingTelemetry(), 0.001, repository)
    with pytest.raises(ToolError) as caught:
        await caller.call(AuditTool(execute), tool_input)
    assert caught.value.code == expected_code
    runs = await repository.list_tool_runs(state.workflow_execution_id)
    assert len(runs) == 1
    assert runs[0].status == ToolRunStatus.FAILED
    assert runs[0].error_code == expected_code
    assert "secret" not in runs[0].error_message
    assert "private-key" not in json.dumps(runs[0].model_dump(mode="json"))


async def test_warning_terminal_immutable_and_binding():
    repository = InMemoryTaskRepository()
    first, tool_input = await caller_context(repository)

    async def warning(request):
        return ToolOutput(status=StepStatus.WARNING, warnings=["demo warning"])

    await ToolCaller(LoggingTelemetry(), 1, repository).call(AuditTool(warning), tool_input)
    record = (await repository.list_tool_runs(first.workflow_execution_id))[0]
    assert record.status == ToolRunStatus.WARNING
    assert record.output_snapshot["warnings"] == ["demo warning"]
    with pytest.raises(InfrastructureError) as caught:
        await repository.finish_tool_run(
            record.tool_run_id, status=ToolRunStatus.FAILED, source="mock:test",
            output_snapshot={}, error_code="LATE_FAILURE",
        )
    assert caught.value.code == "TOOL_RUN_ALREADY_FINISHED"
    other_request = TaskRequest(well_id="WELL_MOCK_001")
    await repository.create(InterpretationState(task=other_request))
    with pytest.raises(InfrastructureError) as caught:
        await repository.create_tool_run(ToolRun(
            task_id=other_request.task_id,
            execution_id=first.workflow_execution_id,
            step_id=StepId.W05, tool_code="bad", execution_mode=ToolExecutionMode.MOCK,
            source="mock:test",
        ))
    assert caught.value.code == "TOOL_RUN_EXECUTION_MISMATCH"


async def test_bound_persistence_failure_is_not_silently_successful():
    class FailingRepository(InMemoryTaskRepository):
        async def finish_tool_run(self, *args, **kwargs):
            raise InfrastructureError("AUDIT_WRITE_FAILED", "审计写入失败")

    repository = FailingRepository()
    state, tool_input = await caller_context(repository)

    async def success(request):
        return ToolOutput(status=StepStatus.SUCCESS)

    with pytest.raises(InfrastructureError) as caught:
        await ToolCaller(LoggingTelemetry(), 1, repository).call(AuditTool(success), tool_input)
    assert caught.value.code == "AUDIT_WRITE_FAILED"
    assert (await repository.list_tool_runs(state.workflow_execution_id))[0].status == (
        ToolRunStatus.RUNNING
    )


async def test_snapshot_sanitizes_unbounded_and_sensitive_metadata():
    repository = InMemoryTaskRepository()
    state, tool_input = await caller_context(repository)
    tool_input.parameters["authorization"] = "Bearer input-secret"
    tool_input.parameters["raw_data"] = {"depths": list(range(1000))}

    async def response(request):
        return ToolOutput(
            status=StepStatus.SUCCESS,
            data={"raw_data": {"depths": list(range(1000))}},
            warnings=["Bearer warn-secret", "w" * 1000],
            metadata={
                "source": "mock:fixture", "api_key": "sk-top-secret",
                "token": "hidden", "password": "hidden", "secret": "hidden",
                "authorization": "Bearer meta-secret", "credential": "hidden",
                "preprocessing": {"safe": ["x" * 1000] * 100},
                "effective_parameters": {"curves": {"GR": {"values": list(range(1000))}}},
            },
        )

    await ToolCaller(LoggingTelemetry(), 1, repository).call(AuditTool(response), tool_input)
    record = (await repository.list_tool_runs(state.workflow_execution_id))[0]
    text = json.dumps(record.model_dump(mode="json"))
    for secret in ("input-secret", "warn-secret", "top-secret", "meta-secret", "hidden"):
        assert secret not in text
    assert '"depths"' not in text and '"values"' not in text
    assert len(text) < 2500
    assert bounded({"deep": {"nested": {"a": {"b": "value"}}}})["deep"]["nested"]["a"]["b"] == (
        "[TRUNCATED]"
    )


async def test_unbound_standalone_tool_remains_compatible():
    async def response(request):
        return ToolOutput(status=StepStatus.SUCCESS)

    request = ToolInput(
        task_id="task", trace_id="trace", well_id="WELL_MOCK_001", step_id=StepId.W05
    )
    result = await ToolCaller(LoggingTelemetry(), 1).call(AuditTool(response), request)
    assert result.status == StepStatus.SUCCESS
