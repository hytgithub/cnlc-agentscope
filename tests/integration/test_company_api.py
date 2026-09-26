"""用 HTTP 协议桩验证真实客户端与映射；不宣称通过公司内网或专业验收。"""

import json

import httpx
import pytest
from pydantic import SecretStr

from cnlc_agent.application.company_results import project_prediction
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import ToolError
from cnlc_agent.infrastructure.company_api import (
    CompanyApiClient,
    CompanyApiSettings,
    extract_prediction_data,
)


def settings():
    """测试地址只由 MockTransport 消费，禁止依赖本机或公司服务。"""
    return CompanyApiSettings(
        preprocessing_url="http://preprocess.test/",
        prediction_url="http://predict.test/center-management/",
        token=SecretStr("private-test-token"),
        _env_file=None,
    )


def request_body():
    return {
        "wellName": "test-well",
        "serviceId": "test-service",
        "logReqJson": {"curves": []},
        "taskConfig": {"NUM": ["POR", "SW"]},
    }


async def test_one_prediction_maps_multiple_steps_without_repeating_call():
    """一次调用共享来源，真实返回中没有的内容不能由 Mock 补齐。"""
    requests = []
    original = {
        "curveList": [
            {"standardName": "POR", "curveData": [0.12]},
            {"standardName": "SW", "curveData": [0.45]},
        ],
        "JSJL": ["油层"],
        "ogResultList": [{"sdep": 1, "edep": 2, "result": "油层"}],
    }

    def handler(request):
        requests.append(request)
        assert (
            request.url.path == "/center-management/center/InferenceLog/encodingInferenceBySyn/v1"
        )
        assert request.headers["Authorization"] == "private-test-token"
        return httpx.Response(
            200, json={"code": 200, "data": {"evaluationData": {"resultData": [original]}}}
        )

    client = CompanyApiClient(settings(), transport=httpx.MockTransport(handler))
    try:
        call = await client.predict(request_body(), "execution", "input-v1")
        projection = project_prediction(call)
        patch = projection.state_patch("execution", "input-v1")
        assert len(requests) == 1
        assert projection.steps[StepId.W04].status == StepStatus.BLOCKED
        assert projection.steps[StepId.W05].status == StepStatus.REVIEW_REQUIRED
        assert patch.petrophysics_result.result["prediction_curves"]["POR"]["curveData"] == [0.12]
        assert all(not result.is_mock for result in projection.steps.values())
        assert len({result.source for result in projection.steps.values()}) == 1
        assert "private-test-token" not in call.model_dump_json()
        assert call.data["resultData"][0] == original
        with pytest.raises(ToolError, match="当前执行"):
            projection.state_patch("other-execution", "input-v1")
    finally:
        await client.aclose()


async def test_preprocessing_uses_server_path_and_keeps_original_file(tmp_path):
    """真实请求顺序为上传、处理、下载；不修改原文件且不向预处理服务发送预测凭据。"""
    source = tmp_path / "well.gdsx"
    content = b"\x89HDF\r\n\x1a\nprotocol-test"
    source.write_bytes(content)
    seen = []

    def handler(request):
        seen.append(request.url.path)
        assert "authorization" not in request.headers
        if request.url.path.endswith("upload"):
            return httpx.Response(
                200, json={"code": 200, "data": {"filePath": "/server/well.gdsx"}}
            )
        body = json.loads(request.content)
        if request.url.path.endswith("processForGDSX"):
            assert body["files"] == ["/server/well.gdsx"]
            assert body["resample"]["enable"] is False
            return httpx.Response(
                200, json={"success": True, "data": {"input": "real-service-data"}}
            )
        assert body == {"filePath": "/server/well.gdsx"}
        return httpx.Response(200, content=content)

    client = CompanyApiClient(settings(), transport=httpx.MockTransport(handler))
    try:
        result = await client.preprocess(
            source, {"resample": {"enable": False}}, "execution", "input-v1"
        )
        assert len(seen) == 3
        assert result.gdsx_content == content
        assert source.read_bytes() == content
        assert result.call.data["logReqJson"] == {"input": "real-service-data"}
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "kind,code",
    [
        ("auth", "COMPANY_AUTH_FAILED"),
        ("business", "COMPANY_REPORTED_FAILURE"),
        ("html", "COMPANY_INVALID_RESPONSE"),
        ("timeout", "COMPANY_TIMEOUT"),
        ("unknown", "COMPANY_UNCONFIRMED_RESPONSE"),
        ("empty", "COMPANY_PREDICTION_DATA_INVALID"),
    ],
)
async def test_company_failures_do_not_retry_or_leak_details(kind, code):
    requests = []

    def handler(request):
        requests.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("private-test-token", request=request)
        if kind == "auth":
            return httpx.Response(401, text="private-test-token")
        if kind == "html":
            return httpx.Response(200, text="private-test-token")
        payload = (
            {"success": False, "message": "private-test-token"}
            if kind == "business"
            else {"code": 200, "data": {}}
            if kind == "empty"
            else {"data": {}}
        )
        return httpx.Response(200, json=payload)

    client = CompanyApiClient(settings(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ToolError) as caught:
            await client.predict(request_body(), "execution", "input-v1")
        assert caught.value.code == code
        assert "private-test-token" not in str(caught.value)
        assert len(requests) == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        {"resultData": {"curveList": []}},
        {"data": {"evaluationData": {"resultData": {"curveList": []}}}},
        {"data": {"data": {"evaluationData": {"resultData": [{"curveList": []}]}}}},
    ],
)
def test_supported_result_wrappers(payload):
    assert extract_prediction_data(payload) == [{"curveList": []}]


def test_multiple_wells_cannot_be_silently_assigned_to_one_task():
    with pytest.raises(ToolError):
        extract_prediction_data({"resultData": [{"well": 1}, {"well": 2}]})
