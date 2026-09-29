"""真实公司 Provider 使用 MockTransport 验证协议、持久事实与 ToolRun 关联。"""

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from cnlc_agent.application.bootstrap import (
    PROFESSIONAL_PROVIDER_BATCH_STEPS,
    build_application,
)
from cnlc_agent.application.stage_orchestrator import StageOrchestrator
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.company_provider import (
    CompanyProviderCall,
    CompanyProviderCallStatus,
    CompanyProviderOperation,
    RealPredictionContext,
)
from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.errors import ToolError
from cnlc_agent.domain.execution import ExecutionRunMode, ExecutionStatus
from cnlc_agent.domain.models import MockFixture, TaskRequest, utc_now
from cnlc_agent.domain.stages import InterpretationStage
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.domain.tool_run import ToolExecutionMode
from cnlc_agent.infrastructure.company_api import CompanyApiClient, CompanyApiSettings
from cnlc_agent.infrastructure.company_provider import RealCompanyBatchProvider
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository
from cnlc_agent.infrastructure.telemetry import LoggingTelemetry
from cnlc_agent.tools.company_batches import build_company_real_tools
from cnlc_agent.tools.contracts import ToolCaller, ToolInput


class TestResolver:
    """测试显式绑定临时 GDSX 和预测上下文，不从进程环境猜输入。"""

    __test__ = False

    def __init__(self, path):
        self.path = path

    async def resolve_gdsx(self, task_id, execution_id, input_version_id):
        del task_id, execution_id, input_version_id
        return self.path

    async def resolve_preprocess_operations(self, task_id, execution_id, input_version_id):
        del task_id, execution_id, input_version_id
        return {"resample": {"enable": False}}

    async def resolve_prediction_context(self, task_id, execution_id, input_version_id):
        del task_id, execution_id, input_version_id
        return RealPredictionContext(
            well_name="explicit-well",
            service_id="explicit-service",
            task_config={"NUM": ["POR", "SW"]},
        )


class TestArtifactSink:
    """只返回逻辑引用；测试确认 Provider 不持久化传入的文件字节。"""

    __test__ = False

    saved_length = 0

    async def save_processed_gdsx(self, task_id, execution_id, input_version_id, content):
        del task_id, execution_id, input_version_id
        self.saved_length = len(content)
        return "artifact:processed:test"


def _api_settings():
    return CompanyApiSettings(
        preprocessing_url="http://preprocess.test/",
        prediction_url="http://predict.test/center-management/",
        token=SecretStr("provider-test-secret"),
        _env_file=None,
    )


def _handler(seen, *, prediction_size=1, prediction_status=200):
    hdf = b"\x89HDF\r\n\x1a\nreal-provider-test"

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("upload"):
            assert "authorization" not in request.headers
            return httpx.Response(
                200, json={"code": 200, "data": {"filePath": "/remote/input.gdsx"}}
            )
        if request.url.path.endswith("processForGDSX"):
            assert "authorization" not in request.headers
            return httpx.Response(
                200,
                json={"code": 200, "data": {"curves": ["GR"], "depthCount": 2}},
            )
        if request.url.path.endswith("download"):
            assert "authorization" not in request.headers
            return httpx.Response(200, content=hdf)
        assert request.headers["Authorization"] == "provider-test-secret"
        if prediction_status != 200:
            return httpx.Response(prediction_status, text="provider-test-secret")
        body = json.loads(request.content)
        assert body == {
            "wellName": "explicit-well",
            "serviceId": "explicit-service",
            "logReqJson": {"curves": ["GR"], "depthCount": 2},
            "taskConfig": {"NUM": ["POR", "SW"]},
        }
        values = [0.12] * prediction_size
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "evaluationData": {
                        "resultData": [
                            {
                                "curveList": [
                                    {"standardName": "POR", "curveData": values},
                                    {"standardName": "SW", "curveData": [0.45]},
                                ],
                                "JSJL": [1],
                                "ogResultList": [{"sdep": 1.0, "edep": 2.0, "result": 1}],
                            }
                        ]
                    }
                },
            },
        )

    return handler


async def _execution(data_dir, repository):
    fixture = MockFixture.model_validate_json(
        (data_dir / "WELL_MOCK_001.json").read_text(encoding="utf-8")
    )
    request = TaskRequest(well_id=fixture.well.well_id)
    state = InterpretationState(task=request, mode="demo")
    await repository.create_task(state)
    version = await repository.create_input_version(request.task_id, fixture)
    execution = await repository.create_execution(
        state, "INITIAL", input_version_id=version.input_version_id
    )
    return request, state, version, execution


def _tool_input(request, state, execution, version, step):
    return ToolInput(
        task_id=request.task_id,
        trace_id=state.trace_id,
        well_id=request.well_id,
        step_id=step,
        parameters={
            "execution_id": execution.execution_id,
            "input_version_id": version.input_version_id,
            "effective_override": {},
        },
    )


async def test_real_preprocess_prediction_persist_and_correlate_tool_runs(data_dir, tmp_path):
    source = tmp_path / "input.gdsx"
    source.write_bytes(b"\x89HDF\r\n\x1a\ninput")
    seen = []
    repository = InMemoryTaskRepository()
    request, state, version, execution = await _execution(data_dir, repository)
    sink = TestArtifactSink()
    client = CompanyApiClient(_api_settings(), transport=httpx.MockTransport(_handler(seen)))
    provider = RealCompanyBatchProvider(client, repository, TestResolver(source), sink)
    caller = ToolCaller(LoggingTelemetry(), 10, repository)
    tools = build_company_real_tools(provider, caller)
    try:
        qc = await caller.call(
            tools["check_curve_quality"],
            _tool_input(request, state, execution, version, StepId.W03),
        )
        lithology = await caller.call(
            tools["identify_lithology"],
            _tool_input(request, state, execution, version, StepId.W04),
        )
        assert len(seen) == 4
        assert sink.saved_length > 8
        assert qc.data["is_mock"] is False
        assert lithology.data["is_mock"] is False

        calls = await repository.list_company_provider_calls(request.task_id)
        assert [item.provider_operation for item in calls] == [
            CompanyProviderOperation.PREPROCESSING,
            CompanyProviderOperation.INTERPRETATION,
        ]
        assert all(item.status == CompanyProviderCallStatus.SUCCESS for item in calls)
        assert all(item.provider_call_id for item in calls)
        assert len({item.provider_call_id for item in calls}) == 2
        assert calls[0].normalized_result["logReqJson"] == {
            "curves": ["GR"],
            "depthCount": 2,
        }
        assert "gdsx_content" not in calls[0].model_dump_json().casefold()

        runs = await repository.list_tool_runs(execution.execution_id)
        physical = [run for run in runs if run.tool_code.startswith("company_")]
        derived = [run for run in runs if not run.tool_code.startswith("company_")]
        assert [run.execution_mode for run in physical] == [
            ToolExecutionMode.REAL,
            ToolExecutionMode.REAL,
        ]
        assert all(run.execution_mode == ToolExecutionMode.DERIVED for run in derived)
        for run in physical:
            related = [
                item
                for item in derived
                if item.source_external_call_id == run.source_external_call_id
            ]
            assert related
            assert run.output_snapshot["metadata"]["provider_call_id"]
            assert all(
                item.output_snapshot["metadata"]["provider_call_id"]
                == run.output_snapshot["metadata"]["provider_call_id"]
                for item in related
            )

        # 模拟进程重启：新的 Provider 不得再次发送已经成功的物理调用。
        calls_before_restart = len(seen)
        restarted = RealCompanyBatchProvider(client, repository, TestResolver(source), sink)
        await restarted.execute(
            "preprocessing", _tool_input(request, state, execution, version, StepId.W03)
        )
        await restarted.execute(
            "interpretation", _tool_input(request, state, execution, version, StepId.W04)
        )
        assert len(seen) == calls_before_restart
        serialized = json.dumps(
            {
                "calls": [item.model_dump(mode="json") for item in calls],
                "runs": [item.model_dump(mode="json") for item in runs],
                "execution": execution.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
        assert "provider-test-secret" not in serialized
        assert "/remote/input.gdsx" not in serialized
        assert str(source) not in serialized
    finally:
        await client.aclose()


async def test_company_real_capabilities_stop_at_w08(tmp_path):
    """真实公司 profile 不得借 Mock operation 冒充 W09/W10 能力。"""

    assert PROFESSIONAL_PROVIDER_BATCH_STEPS["company_real"] == frozenset(
        {StepId.W03, StepId.W04, StepId.W05, StepId.W06, StepId.W07, StepId.W08}
    )
    source = tmp_path / "input.gdsx"
    source.write_bytes(b"\x89HDF\r\n\x1a\ninput")
    repository = InMemoryTaskRepository()
    caller = ToolCaller(LoggingTelemetry(), 10, repository)
    client = CompanyApiClient(_api_settings(), transport=httpx.MockTransport(_handler([])))
    try:
        tools = build_company_real_tools(
            RealCompanyBatchProvider(client, repository, TestResolver(source)), caller
        )
        assert set(tools) == {
            "check_curve_quality",
            "identify_lithology",
            "evaluate_petrophysics",
            "calculate_sw",
            "identify_fluid",
            "classify_layer",
            "merge_intervals",
        }
        assert "validate_interpretation" not in tools
        assert "prepare_report" not in tools
    finally:
        await client.aclose()


async def test_company_real_w09_uses_validation_agent_and_w10_stays_local(data_dir, tmp_path):
    """W09/W10/REPORT 不得因 company_real profile 触发不存在的真实 API。"""

    source = tmp_path / "input.gdsx"
    source.write_bytes(b"\x89HDF\r\n\x1a\ninput")
    seen = []
    client = CompanyApiClient(_api_settings(), transport=httpx.MockTransport(_handler(seen)))
    app = build_application(
        AppSettings(
            mode="demo",
            model_provider="mock",
            professional_provider="company_real",
            mock_data_dir=data_dir,
            _env_file=None,
        ),
        company_api_client=client,
        company_input_resolver=TestResolver(source),
    )
    fixture = MockFixture.model_validate_json(
        (data_dir / "WELL_MOCK_001.json").read_text(encoding="utf-8")
    )
    state = InterpretationState(
        task=TaskRequest(well_id=fixture.well.well_id),
        mode="demo",
        well=fixture.well,
        raw_data=fixture.raw_data,
        processed_data=fixture.raw_data,
        data_requirements=fixture.requirements,
        qc_result=fixture.outputs["qc"],
        lithology_result=fixture.outputs["lithology"],
        petrophysics_result=fixture.outputs["petrophysics"],
        fluid_result=fixture.outputs["fluid"],
        layer_classification=fixture.outputs["classification"],
        interval_result=fixture.outputs["intervals"],
    )
    nodes = {node.step_id: node for node in app.main_agent.workflow.nodes}
    try:
        validated = await nodes[StepId.W09].execute(state)
        assert validated.patch.validation_result is not None
        assert validated.patch.validation_result.result.get("demo_skipped") is not True
        state.validation_result = validated.patch.validation_result
        state.completed_steps = list(StepId)[:-1]

        checked = await nodes[StepId.W10].execute(state)
        assert checked.patch.final_check is not None
        assert checked.patch.final_check.source == "skeleton:structural-final-check"
        state.final_check = checked.patch.final_check
        state.status = checked.status
        assert "单井" in app.reports.to_markdown(state)
        assert seen == []
    finally:
        await app.close()


async def test_company_real_bootstrap_runs_staged_preprocess_with_injected_adapters(
    data_dir, tmp_path
):
    """组合根只接线真实 Provider；W01 仍由明确 Fixture 输入适配器承担。"""

    source = tmp_path / "input.gdsx"
    source.write_bytes(b"\x89HDF\r\n\x1a\ninput")
    client = CompanyApiClient(_api_settings(), transport=httpx.MockTransport(_handler([])))
    app = build_application(
        AppSettings(
            mode="demo",
            model_provider="mock",
            professional_provider="company_real",
            mock_data_dir=data_dir,
            _env_file=None,
        ),
        company_api_client=client,
        company_input_resolver=TestResolver(source),
    )
    fixture = MockFixture.model_validate_json(
        (data_dir / "WELL_MOCK_001.json").read_text(encoding="utf-8")
    )
    request = TaskRequest(well_id=fixture.well.well_id)
    execution = await app.prepare_initial_with_input(
        request, fixture, run_mode=ExecutionRunMode.STAGED_CONFIRMATION
    )
    orchestrator = StageOrchestrator(app)

    async def materialize(_version):
        return None

    try:
        decoded = await orchestrator.execute_next_stage(
            execution.execution_id, worker_id="real-decode", materialize=materialize
        )
        await orchestrator.confirm_stage(
            request.task_id,
            execution.execution_id,
            InterpretationStage.DECODE,
            decoded.state_snapshot.stage_runs[-1].id,
            actor="test",
        )
        preprocessed = await orchestrator.execute_next_stage(
            execution.execution_id,
            worker_id="real-preprocess",
            materialize=materialize,
        )
        view = await orchestrator.get_stage_result(
            request.task_id,
            execution.execution_id,
            preprocessed.state_snapshot.stage_runs[-1].id,
        )
        assert {item.tool_code for item in view.tool_runs} == {
            "company_preprocessing",
            "check_curve_quality",
        }
        assert (
            next(
                item for item in view.tool_runs if item.tool_code == "company_preprocessing"
            ).execution_mode
            == ToolExecutionMode.REAL
        )
        serialized = view.model_dump_json()
        assert "provider-test-secret" not in serialized
        assert str(source) not in serialized
        assert "/remote/input.gdsx" not in serialized
    finally:
        await app.close()


async def test_real_provider_rejects_unsupported_and_version_mismatch(data_dir, tmp_path):
    source = tmp_path / "input.gdsx"
    source.write_bytes(b"\x89HDF\r\n\x1a\ninput")
    repository = InMemoryTaskRepository()
    request, state, version, execution = await _execution(data_dir, repository)
    client = CompanyApiClient(_api_settings(), transport=httpx.MockTransport(_handler([])))
    provider = RealCompanyBatchProvider(client, repository, TestResolver(source))
    preprocess_request = _tool_input(request, state, execution, version, StepId.W03)
    try:
        for operation in ("analysis", "report"):
            with pytest.raises(ToolError) as caught:
                await provider.execute(operation, preprocess_request)
            assert caught.value.code == "COMPANY_OPERATION_NOT_IMPLEMENTED"

        await provider.execute("preprocessing", preprocess_request)
        fixture = MockFixture.model_validate_json(
            (data_dir / "WELL_MOCK_001.json").read_text(encoding="utf-8")
        )
        other_input = await repository.create_input_version(request.task_id, fixture)
        wrong_input = preprocess_request.model_copy(
            update={
                "step_id": StepId.W04,
                "parameters": {
                    **preprocess_request.parameters,
                    "input_version_id": other_input.input_version_id,
                },
            }
        )
        with pytest.raises(ToolError) as caught:
            await provider.execute("interpretation", wrong_input)
        assert caught.value.code == "COMPANY_RESULT_VERSION_MISMATCH"

        assert await repository.claim_execution(
            execution.execution_id, "finish-v1", utc_now() + timedelta(minutes=1)
        )
        await repository.finish_execution(
            execution.execution_id, "finish-v1", ExecutionStatus.FAILED
        )
        state_v2 = InterpretationState(task=request, mode="demo")
        execution_v2 = await repository.create_execution(
            state_v2, "RERUN", input_version_id=version.input_version_id
        )
        wrong_execution = _tool_input(request, state_v2, execution_v2, version, StepId.W04)
        with pytest.raises(ToolError) as caught:
            await provider.execute("interpretation", wrong_execution)
        assert caught.value.code == "COMPANY_RESULT_VERSION_MISMATCH"
    finally:
        await client.aclose()


async def test_real_provider_failure_is_persisted_without_secret(data_dir, tmp_path):
    source = tmp_path / "input.gdsx"
    source.write_bytes(b"\x89HDF\r\n\x1a\ninput")
    repository = InMemoryTaskRepository()
    request, state, version, execution = await _execution(data_dir, repository)
    # 先成功预处理，再让真实预测返回 401。
    success = CompanyApiClient(_api_settings(), transport=httpx.MockTransport(_handler([])))
    provider = RealCompanyBatchProvider(success, repository, TestResolver(source))
    await provider.execute(
        "preprocessing", _tool_input(request, state, execution, version, StepId.W03)
    )
    await success.aclose()

    failed = CompanyApiClient(
        _api_settings(),
        transport=httpx.MockTransport(_handler([], prediction_status=401)),
    )
    provider = RealCompanyBatchProvider(failed, repository, TestResolver(source))
    try:
        with pytest.raises(ToolError) as caught:
            await provider.execute(
                "interpretation",
                _tool_input(request, state, execution, version, StepId.W04),
            )
        assert caught.value.code == "COMPANY_AUTH_FAILED"
        calls = await repository.list_company_provider_calls(request.task_id)
        assert calls[-1].status == CompanyProviderCallStatus.FAILED
        assert calls[-1].provider_call_id
        assert calls[-1].error_code == "COMPANY_AUTH_FAILED"
        assert calls[-1].normalized_result == {}
        assert "provider-test-secret" not in calls[-1].model_dump_json()
        assert "provider-test-secret" not in str(caught.value)
        before = len(calls)
        with pytest.raises(ToolError) as repeated:
            await provider.execute(
                "interpretation",
                _tool_input(request, state, execution, version, StepId.W04),
            )
        assert repeated.value.code == "COMPANY_PROVIDER_CALL_FAILED"
        assert len(await repository.list_company_provider_calls(request.task_id)) == before
    finally:
        await failed.aclose()


async def test_timeout_becomes_unknown_and_is_not_retried(data_dir, tmp_path):
    """超时无法判断服务端结果，必须保留 UNKNOWN 身份并关闭自动重发。"""

    source = tmp_path / "input.gdsx"
    source.write_bytes(b"\x89HDF\r\n\x1a\ninput")
    repository = InMemoryTaskRepository()
    request, state, version, execution = await _execution(data_dir, repository)
    seen = []

    def timeout_handler(http_request):
        seen.append(http_request)
        raise httpx.ReadTimeout("unsafe raw timeout", request=http_request)

    client = CompanyApiClient(_api_settings(), transport=httpx.MockTransport(timeout_handler))
    provider = RealCompanyBatchProvider(client, repository, TestResolver(source))
    tool_input = _tool_input(request, state, execution, version, StepId.W03)
    try:
        with pytest.raises(ToolError) as caught:
            await provider.execute("preprocessing", tool_input)
        assert caught.value.code == "COMPANY_TIMEOUT"
        calls = await repository.list_company_provider_calls(request.task_id)
        assert len(calls) == 1
        assert calls[0].status == CompanyProviderCallStatus.UNKNOWN
        assert calls[0].provider_call_id
        assert calls[0].error_code == "COMPANY_TIMEOUT"
        first_request_count = len(seen)

        with pytest.raises(ToolError) as repeated:
            await provider.execute("preprocessing", tool_input)
        assert repeated.value.code == "COMPANY_PROVIDER_CALL_UNKNOWN"
        assert len(seen) == first_request_count
        assert len(await repository.list_company_provider_calls(request.task_id)) == 1
    finally:
        await client.aclose()


async def test_outer_tool_timeout_also_marks_provider_unknown(data_dir, tmp_path):
    """ToolCaller 先于 HTTP 客户端超时时，取消边界也必须落 UNKNOWN。"""

    source = tmp_path / "input.gdsx"
    source.write_bytes(b"\x89HDF\r\n\x1a\ninput")
    repository = InMemoryTaskRepository()
    request, state, version, execution = await _execution(data_dir, repository)

    async def slow_handler(http_request):
        await asyncio.sleep(1)
        return httpx.Response(500, request=http_request)

    client = CompanyApiClient(_api_settings(), transport=httpx.MockTransport(slow_handler))
    provider = RealCompanyBatchProvider(client, repository, TestResolver(source))
    caller = ToolCaller(LoggingTelemetry(), 0.01, repository)
    tool = build_company_real_tools(provider, caller)["check_curve_quality"]
    try:
        with pytest.raises(ToolError) as caught:
            await caller.call(tool, _tool_input(request, state, execution, version, StepId.W03))
        assert caught.value.code == "TOOL_TIMEOUT"
        calls = await repository.list_company_provider_calls(request.task_id)
        assert len(calls) == 1
        assert calls[0].status == CompanyProviderCallStatus.UNKNOWN
        assert calls[0].error_code == "COMPANY_TIMEOUT"
    finally:
        await client.aclose()


async def test_running_provider_call_is_not_resubmitted(data_dir, tmp_path):
    """遗留 RUNNING 表示另一 Worker 可能仍在执行，当前请求不得重复提交。"""

    source = tmp_path / "input.gdsx"
    source.write_bytes(b"\x89HDF\r\n\x1a\ninput")
    repository = InMemoryTaskRepository()
    request, state, version, execution = await _execution(data_dir, repository)
    call = await repository.create_company_provider_call(
        CompanyProviderCall(
            task_id=request.task_id,
            execution_id=execution.execution_id,
            input_version_id=version.input_version_id,
            provider_operation=CompanyProviderOperation.PREPROCESSING,
            request_summary={"well_id": request.well_id},
        )
    )
    seen = []
    client = CompanyApiClient(_api_settings(), transport=httpx.MockTransport(_handler(seen)))
    provider = RealCompanyBatchProvider(client, repository, TestResolver(source))
    try:
        with pytest.raises(ToolError) as caught:
            await provider.execute(
                "preprocessing",
                _tool_input(request, state, execution, version, StepId.W03),
            )
        assert caught.value.code == "COMPANY_PROVIDER_CALL_RUNNING"
        assert seen == []
        assert (await repository.get_company_provider_call(call.external_call_id)).status == (
            CompanyProviderCallStatus.RUNNING
        )
    finally:
        await client.aclose()


async def test_oversized_prediction_is_rejected_without_truncation(data_dir, tmp_path):
    source = tmp_path / "input.gdsx"
    source.write_bytes(b"\x89HDF\r\n\x1a\ninput")
    repository = InMemoryTaskRepository()
    request, state, version, execution = await _execution(data_dir, repository)
    client = CompanyApiClient(
        _api_settings(),
        transport=httpx.MockTransport(_handler([], prediction_size=140_000)),
    )
    provider = RealCompanyBatchProvider(client, repository, TestResolver(source))
    try:
        await provider.execute(
            "preprocessing", _tool_input(request, state, execution, version, StepId.W03)
        )
        with pytest.raises(ToolError) as caught:
            await provider.execute(
                "interpretation",
                _tool_input(request, state, execution, version, StepId.W04),
            )
        assert caught.value.code == "PROVIDER_RESULT_TOO_LARGE"
        calls = await repository.list_company_provider_calls(request.task_id)
        assert calls[-1].status == CompanyProviderCallStatus.FAILED
        assert calls[-1].normalized_result == {}
        assert calls[-1].error_code == "PROVIDER_RESULT_TOO_LARGE"
    finally:
        await client.aclose()
