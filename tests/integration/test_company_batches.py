"""四个 Mock 大步骤通过现有应用服务写入 W01-W10，并生成原有报告。"""

import asyncio
import json

import pytest

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.tool_run import ToolExecutionMode
from cnlc_agent.infrastructure.mock import MockWellRepository
from cnlc_agent.infrastructure.telemetry import LoggingTelemetry
from cnlc_agent.tools.company_batches import CompanyBatchResults, MockCompanyBatchProvider
from cnlc_agent.tools.contracts import ToolCaller, ToolInput


async def test_four_batches_fill_existing_workflow_and_report(data_dir):
    app = build_application(
        AppSettings(
            mode="demo",
            model_provider="mock",
            professional_provider="company_mock",
            mock_data_dir=data_dir,
            _env_file=None,
        )
    )
    try:
        state, report = await app.run(TaskRequest(well_id="WELL_MOCK_001"))
        assert state.status == StepStatus.SUCCESS
        assert state.completed_steps == list(StepId)
        assert "虚构演示井 001测井评价报告" in report
        assert state.petrophysics_result.result["porosity"]["value"] == 0.16
        assert state.fluid_result.result["sw_result"]["result"]["water_saturation"]["value"] == 0.45
        sources = {
            getattr(state, name).source
            for name in (
                "lithology_result",
                "petrophysics_result",
                "fluid_result",
                "layer_classification",
                "interval_result",
                "validation_result",
            )
        }
        assert len(sources) == 1
        assert all(
            getattr(state, name).is_mock
            for name in (
                "qc_result",
                "lithology_result",
                "petrophysics_result",
                "final_check",
            )
        )
        runs = await app.repository.list_tool_runs(state.workflow_execution_id)
        batches = [run for run in runs if run.tool_code.startswith("company_")]
        assert [run.tool_code for run in batches] == [
            "company_analysis",
            "company_preprocessing",
            "company_interpretation",
            "company_report",
        ]
        assert len({run.output_snapshot["metadata"]["external_call_id"] for run in batches}) == 4
        assert all(
            run.execution_mode == ToolExecutionMode.DERIVED for run in runs if run not in batches
        )
    finally:
        await app.close()


async def test_missing_child_result_does_not_inherit_batch_success(data_dir, fixture_data):
    del fixture_data["outputs"]["petrophysics"]
    (data_dir / "WELL_MOCK_001.json").write_text(json.dumps(fixture_data), encoding="utf-8")
    app = build_application(
        AppSettings(
            mode="demo",
            professional_provider="company_mock",
            model_provider="mock",
            mock_data_dir=data_dir,
            _env_file=None,
        )
    )
    try:
        state, _ = await app.run(TaskRequest(well_id="WELL_MOCK_001"))
        assert state.status == StepStatus.FAILED
        assert StepId.W04 in state.completed_steps
        assert StepId.W05 not in state.completed_steps
        assert state.petrophysics_result is None
        assert state.errors[-1].code == "COMPANY_STEP_RESULT_MISSING"
    finally:
        await app.close()


async def test_batch_is_shared_concurrently_but_isolated_between_executions(data_dir):
    provider = MockCompanyBatchProvider(MockWellRepository(data_dir))
    batches = CompanyBatchResults(provider, ToolCaller(LoggingTelemetry(), 10))
    # 无数据库的缓存测试只替换审计调用，不替换 provider 的批量返回逻辑。
    calls = []

    async def call(tool, request):
        calls.append(request.parameters["execution_id"])
        return await tool.execute(request)

    batches.caller.call = call
    request = ToolInput(
        task_id="task",
        trace_id="trace",
        well_id="WELL_MOCK_001",
        step_id=StepId.W04,
        parameters={"execution_id": "one"},
    )
    first, second = await asyncio.gather(
        batches.get("interpretation", request),
        batches.get("interpretation", request),
    )
    assert first.metadata == second.metadata
    first.data.clear()
    assert second.data
    newer = request.model_copy(update={"parameters": {"execution_id": "two"}})
    third = await batches.get("interpretation", newer)
    assert third.metadata["external_call_id"] != second.metadata["external_call_id"]
    assert calls == ["one", "two"]


@pytest.mark.parametrize(
    ("case", "expected_status", "last_step"),
    [
        ("missing_curve", StepStatus.BLOCKED, StepId.W02),
        ("sw_failed", StepStatus.FAILED, StepId.W06),
        ("conflict", StepStatus.REVIEW_REQUIRED, StepId.W09),
    ],
)
async def test_batch_results_preserve_workflow_gates(
    data_dir, fixture_data, case, expected_status, last_step
):
    if case == "missing_curve":
        name = fixture_data["requirements"]["required_curves"][0]
        del fixture_data["raw_data"]["curves"][name]
    elif case == "sw_failed":
        fixture_data["outputs"]["sw"]["status"] = "FAILED"
    else:
        fixture_data["validation"]["validation_status"] = "SERIOUS_CONFLICT"
    (data_dir / "WELL_MOCK_001.json").write_text(json.dumps(fixture_data), encoding="utf-8")
    app = build_application(
        AppSettings(
            mode="demo",
            model_provider="mock",
            professional_provider="company_mock",
            mock_data_dir=data_dir,
            _env_file=None,
        )
    )
    try:
        state, _ = await app.run(TaskRequest(well_id="WELL_MOCK_001"))
        assert state.status == expected_status
        assert last_step not in state.completed_steps
        assert StepId.W10 not in state.completed_steps
        assert state.final_check is None
    finally:
        await app.close()
