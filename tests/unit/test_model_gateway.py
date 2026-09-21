import json
from collections.abc import Iterator

import httpx
import pytest
from pydantic import SecretStr

from cnlc_agent.application.ports import ModelRequest
from cnlc_agent.config.settings import AppSettings, ConnectionSettings
from cnlc_agent.domain.errors import ModelError
from cnlc_agent.infrastructure.model_gateway import OpenAICompatibleModelGateway


@pytest.fixture
def model_request() -> ModelRequest:
    return ModelRequest(
        task_id="task-model-test",
        trace_id="trace-model-test",
        well_id="WELL_MOCK_001",
        purpose="fluid",
        context={"evidence": "fixture", "value": 1},
    )


def client_for(responses: list[httpx.Response | Exception], requests: list[httpx.Request]):
    iterator: Iterator[httpx.Response | Exception] = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        item = next(iterator)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def response(content: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"choices": [{"message": {"content": content}}]},
        request=httpx.Request("POST", "https://example.test/v1/chat/completions"),
    )


def gateway(responses, requests, **kwargs):
    return OpenAICompatibleModelGateway(
        base_url="https://example.test/v1",
        api_key=SecretStr("sk-test-secret"),
        model_name="qwen-plus",
        client=client_for(responses, requests),
        retry_backoff_seconds=0,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_json_object_response_and_openai_request_shape(model_request):
    requests = []
    service = gateway([response('{"decision":"oil","evidence":["RT"]}')], requests)
    result = await service.generate(model_request)
    assert result == {"decision": "oil", "evidence": ["RT"]}
    sent = json.loads(requests[0].content)
    assert sent["model"] == "qwen-plus"
    assert sent["response_format"] == {"type": "json_object"}
    assert "sk-test-secret" not in requests[0].content.decode()
    assert requests[0].headers["authorization"] == "Bearer sk-test-secret"
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "code"),
    [("not-json", "MODEL_INVALID_JSON"), ("[1, 2]", "MODEL_INVALID_JSON")],
)
async def test_invalid_model_content_is_model_error(model_request, content, code):
    service = gateway([response(content)], requests=[])
    with pytest.raises(ModelError) as caught:
        await service.generate(model_request)
    assert caught.value.code == code
    assert "sk-test-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_invalid_provider_envelope_is_model_error(model_request):
    service = gateway([httpx.Response(200, json={"choices": []})], requests=[])
    with pytest.raises(ModelError, match="模型响应") as caught:
        await service.generate(model_request)
    assert caught.value.code == "MODEL_INVALID_RESPONSE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "MODEL_AUTH_ERROR", False),
        (403, "MODEL_AUTH_ERROR", False),
        (429, "MODEL_RATE_LIMIT", True),
        (500, "MODEL_SERVER_ERROR", True),
        (503, "MODEL_SERVER_ERROR", True),
        (400, "MODEL_REQUEST_ERROR", False),
    ],
)
async def test_http_error_mapping(model_request, status, code, retryable):
    requests = []
    responses = [response({"error": "hidden"}, status)]
    if retryable:
        responses += [response('{"ok":true}')]
    service = gateway(responses, requests, max_retries=1)
    if retryable:
        assert await service.generate(model_request) == {"ok": True}
        assert len(requests) == 2
    else:
        with pytest.raises(ModelError) as caught:
            await service.generate(model_request)
        assert caught.value.code == code
        assert caught.value.retryable is retryable
    assert all("sk-test-secret" not in str(item) for item in requests)


@pytest.mark.asyncio
async def test_timeout_and_transport_retry_are_finite(model_request):
    requests = []
    service = gateway(
        [httpx.ReadTimeout("timed out"), httpx.ConnectError("connection failed")],
        requests,
        max_retries=1,
    )
    with pytest.raises(ModelError) as caught:
        await service.generate(model_request)
    assert caught.value.code == "MODEL_TRANSPORT_ERROR"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_unknown_transport_error_is_sanitized(model_request):
    service = gateway(
        [RuntimeError("url=https://secret.invalid?key=sk-test-secret")],
        [],
        max_retries=0,
    )
    with pytest.raises(ModelError) as caught:
        await service.generate(model_request)
    assert caught.value.code == "MODEL_UNKNOWN_ERROR"
    assert "secret.invalid" not in str(caught.value)
    assert "sk-test-secret" not in str(caught.value)


def test_real_provider_requires_all_configuration(monkeypatch):
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)
    with pytest.raises(ModelError) as caught:
        OpenAICompatibleModelGateway.from_settings(
            AppSettings(_env_file=None), ConnectionSettings(_env_file=None)
        )
    assert caught.value.code == "MODEL_CONFIG_MISSING"


def test_dashscope_settings_can_be_loaded_without_exposing_secret(monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-local-only")
    monkeypatch.setenv("MODEL_NAME", "qwen-plus")
    settings = ConnectionSettings(_env_file=None)
    assert settings.model_base_url and settings.model_base_url.endswith("/v1")
    assert settings.model_name == "qwen-plus"
    assert settings.model_api_key == SecretStr("sk-local-only")
    assert "sk-local-only" not in repr(settings)
