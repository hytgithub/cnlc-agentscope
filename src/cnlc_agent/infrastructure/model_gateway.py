"""异步 OpenAI-Compatible 模型网关。

该适配器只负责配置、传输、重试和响应解析。Agent 与 Workflow 仅依赖精简的
``ModelGateway`` 端口并接收 JSON 对象，供应商 SDK 和鉴权凭据不会越过此边界。
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
    """异步调用 OpenAI-Compatible 的 ``/chat/completions`` 接口。

    ``max_retries`` 表示首次请求之外的附加传输尝试次数。只有超时、网络、限流和
    5xx 错误允许重试；鉴权和请求参数错误立即返回。Workflow 的业务 Retry/Rollback
    不属于本适配器职责。
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
        # 同时兼容传入服务根地址和完整 chat/completions 地址，避免重复拼接路径。
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
        """从统一配置创建网关，并在启动阶段尽早暴露缺失配置。"""

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
        """发送结构化请求；只对明确可恢复的传输类错误执行有限重试。"""

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
                # 不复制供应商异常原文，防止凭据、URL 或请求内容进入应用错误。
                raise ModelError("MODEL_UNKNOWN_ERROR", "模型请求发生未知错误") from None
        raise AssertionError("model request loop must return or raise")

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        """生成零温度、强制 JSON object 的最小 Chat Completions 请求。"""

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
        """执行单次 HTTP 请求并把状态码归类为稳定的 ModelError。"""

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
        """解析供应商响应，并兼容模型偶尔包裹的 Markdown 代码围栏。"""

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
        """执行有上限的指数退避，避免无限等待或请求风暴。"""

        if self.retry_backoff_seconds:
            await asyncio.sleep(min(self.retry_backoff_seconds * (2**attempt), 5.0))

    @staticmethod
    def _log_retry(request: ModelRequest, retry_number: int, code: str) -> None:
        """只记录任务标识和稳定错误码，不记录请求上下文或凭据。"""

        logger.warning(
            "model_retry task_id=%s trace_id=%s retry=%d error=%s",
            request.task_id,
            request.trace_id,
            retry_number,
            code,
        )

    async def aclose(self) -> None:
        """仅关闭本实例自行创建的 HTTP 客户端。"""

        if self._owns_client:
            await self.client.aclose()


# 兼容偏好架构命名 ``ModelGateway`` 的调用方。
OpenAICompatibleGateway = OpenAICompatibleModelGateway
