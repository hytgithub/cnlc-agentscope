"""Async OpenAI-Compatible model gateway.

The adapter owns only transport concerns.  Agents and workflows depend on the
small ``ModelGateway`` port and receive JSON objects; no provider SDK or
credential crosses that boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from cnlc_agent.application.ports import ModelRequest
from cnlc_agent.config.settings import AppSettings, ConnectionSettings
from cnlc_agent.domain.errors import ModelError
from cnlc_agent.domain.models import JsonObject

logger = logging.getLogger(__name__)

_DEFAULT_SYSTEM_PROMPT = (
    "You are a structured-output assistant. Use only the JSON context supplied "
    "by the caller. Return one JSON object and no markdown or explanation."
)


class OpenAICompatibleModelGateway:
    """Call an OpenAI-compatible ``/chat/completions`` endpoint asynchronously.

    ``max_retries`` is the number of additional transport attempts after the
    first request. Only timeout, transport, rate-limit and 5xx failures are
    retried. Authentication and invalid-request failures are returned
    immediately. Workflow retry/rollback remains outside this adapter.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr | str,
        model_name: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.25,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip():
            raise ModelError("MODEL_CONFIG_MISSING", "模型服务地址未配置")
        if not model_name.strip():
            raise ModelError("MODEL_CONFIG_MISSING", "模型名称未配置")
        secret = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not secret or not secret.strip():
            raise ModelError("MODEL_CONFIG_MISSING", "模型鉴权凭据未配置")
        if timeout_seconds <= 0:
            raise ModelError("MODEL_CONFIG_INVALID", "模型超时时间必须大于 0")
        if max_retries < 0:
            raise ModelError("MODEL_CONFIG_INVALID", "模型重试次数不能为负数")
        if retry_backoff_seconds < 0:
            raise ModelError("MODEL_CONFIG_INVALID", "模型重试退避时间不能为负数")

        normalized = base_url.rstrip("/")
        if normalized.endswith("/chat/completions"):
            endpoint = normalized
        else:
            endpoint = f"{normalized}/chat/completions"
        self.endpoint = endpoint
        self.model_name = model_name
        self._api_key = secret
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    @classmethod
    def from_settings(
        cls,
        settings: AppSettings,
        connections: ConnectionSettings | None = None,
    ) -> OpenAICompatibleModelGateway:
        connections = connections or ConnectionSettings()
        if connections.model_base_url is None:
            raise ModelError("MODEL_CONFIG_MISSING", "MODEL_BASE_URL 未配置")
        if connections.model_api_key is None:
            raise ModelError("MODEL_CONFIG_MISSING", "MODEL_API_KEY 未配置")
        if connections.model_name is None:
            raise ModelError("MODEL_CONFIG_MISSING", "MODEL_NAME 未配置")
        return cls(
            base_url=connections.model_base_url,
            api_key=connections.model_api_key,
            model_name=connections.model_name,
            timeout_seconds=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
            retry_backoff_seconds=settings.model_retry_backoff_seconds,
        )

    async def generate(self, request: ModelRequest) -> JsonObject:
        payload = self._payload(request)
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                response = await self._post(payload)
                return self._parse_response(response)
            except ModelError as exc:
                if not exc.retryable or attempt + 1 >= attempts:
                    raise
                self._log_retry(request, attempt + 1, exc.code)
                await self._backoff(attempt)
            except httpx.TimeoutException:
                error = ModelError("MODEL_TIMEOUT", "模型请求超时", retryable=True)
                if attempt + 1 >= attempts:
                    raise error from None
                self._log_retry(request, attempt + 1, error.code)
                await self._backoff(attempt)
            except httpx.TransportError:
                error = ModelError("MODEL_TRANSPORT_ERROR", "模型网络请求失败", retryable=True)
                if attempt + 1 >= attempts:
                    raise error from None
                self._log_retry(request, attempt + 1, error.code)
                await self._backoff(attempt)
            except (TimeoutError, OSError):
                error = ModelError("MODEL_TRANSPORT_ERROR", "模型网络请求失败", retryable=True)
                if attempt + 1 >= attempts:
                    raise error from None
                self._log_retry(request, attempt + 1, error.code)
                await self._backoff(attempt)
            except Exception:
                # Never copy provider exception text into application errors.
                raise ModelError("MODEL_UNKNOWN_ERROR", "模型请求发生未知错误") from None
        raise AssertionError("model request loop must return or raise")

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        try:
            context = json.dumps(request.context, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raise ModelError("MODEL_INVALID_REQUEST", "模型请求上下文不是有效 JSON") from None
        return {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": _DEFAULT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"purpose": request.purpose, "context": json.loads(context)},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }

    async def _post(self, payload: Mapping[str, Any]) -> httpx.Response:
        try:
            response = await self.client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise
        except httpx.TransportError:
            raise
        except (TimeoutError, OSError):
            raise
        except Exception:
            raise ModelError("MODEL_UNKNOWN_ERROR", "模型请求发生未知错误") from None

        if response.status_code in {401, 403}:
            raise ModelError("MODEL_AUTH_ERROR", "模型鉴权失败")
        if response.status_code == 429:
            raise ModelError("MODEL_RATE_LIMIT", "模型请求受到限流", retryable=True)
        if response.status_code in {408, 504}:
            raise ModelError("MODEL_TIMEOUT", "模型服务请求超时", retryable=True)
        if 500 <= response.status_code <= 599:
            raise ModelError("MODEL_SERVER_ERROR", "模型服务暂时不可用", retryable=True)
        if response.status_code >= 400:
            raise ModelError("MODEL_REQUEST_ERROR", "模型请求被服务拒绝")
        return response

    @staticmethod
    def _parse_response(response: httpx.Response) -> JsonObject:
        try:
            envelope = response.json()
        except (ValueError, json.JSONDecodeError):
            raise ModelError("MODEL_INVALID_RESPONSE", "模型响应不是有效 JSON") from None
        if not isinstance(envelope, dict):
            raise ModelError("MODEL_INVALID_RESPONSE", "模型响应顶层必须是 JSON object")
        try:
            content = envelope["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ModelError(
                "MODEL_INVALID_RESPONSE", "模型响应缺少 choices.message.content"
            ) from None
        if not isinstance(content, str) or not content.strip():
            raise ModelError("MODEL_INVALID_RESPONSE", "模型响应内容为空或类型无效")
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                text = "\n".join(lines[1:-1]).strip()
        try:
            result = json.loads(text)
        except (ValueError, json.JSONDecodeError):
            raise ModelError("MODEL_INVALID_JSON", "模型内容不是有效 JSON object") from None
        if not isinstance(result, dict):
            raise ModelError("MODEL_INVALID_JSON", "模型内容顶层必须是 JSON object")
        return result

    async def _backoff(self, attempt: int) -> None:
        if self.retry_backoff_seconds:
            await asyncio.sleep(min(self.retry_backoff_seconds * (2**attempt), 5.0))

    @staticmethod
    def _log_retry(request: ModelRequest, retry_number: int, code: str) -> None:
        logger.warning(
            "model_retry task_id=%s trace_id=%s retry=%d error=%s",
            request.task_id,
            request.trace_id,
            retry_number,
            code,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()


# Short alias for callers that prefer the architecture's ModelGateway wording.
OpenAICompatibleGateway = OpenAICompatibleModelGateway
