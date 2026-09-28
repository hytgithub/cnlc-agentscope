"""AgentScope 直接调用公司预处理和预测服务；qwen-agent-main 仅是协议参考。"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import Field, JsonValue, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cnlc_agent.domain.errors import ToolError
from cnlc_agent.domain.models import Contract, JsonObject, utc_now


class CompanyApiSettings(BaseSettings):
    """公司服务独立配置，不依赖 qwen-agent-main 启动，也不复用聊天模型密钥。"""

    model_config = SettingsConfigDict(env_prefix="CNLC_COMPANY_", env_file=".env", extra="ignore")
    preprocessing_url: str
    prediction_url: str
    # 可直接提供短期 Token，也可使用 yuce_api.py 同样的登录流程。
    token: SecretStr | None = None
    login_url: str | None = None
    username: str | None = None
    password: SecretStr | None = None
    timeout_seconds: float = Field(default=660, gt=0, le=3600)
    # 真实模式先以受控本地文件作为输入，Web GDSX 上传接入后会覆盖这一路径。
    gdsx_path: Path | None = None
    service_id: str | None = None
    create_people: str | None = None
    preprocess_operations: JsonObject | None = None
    task_config: JsonObject | None = None
    # yuce_api.py 始终携带该字段；为空时由预测管理服务选择默认编码服务。
    encoding_url: str = ""
    batch_size: int = Field(default=1024, gt=0, le=100000)

    @field_validator("preprocessing_url", "prediction_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """基址不允许携带凭据或查询串；prediction_url 截止到 center-management。"""
        url = httpx.URL(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise ValueError("需要不含凭据、查询串的 HTTP(S) 服务基址")
        return value.rstrip("/") + "/"

    @field_validator("login_url")
    @classmethod
    def validate_login_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query:
            raise ValueError("登录地址必须是不含凭据和查询串的 HTTP(S) 地址")
        return value

    @model_validator(mode="after")
    def validate_auth(self) -> "CompanyApiSettings":
        """认证二选一：已有 Token，或完整的登录配置。"""
        has_token = self.token is not None and bool(self.token.get_secret_value().strip())
        has_login = bool(self.login_url and self.username and self.password)
        if not has_token and not has_login:
            raise ValueError(
                "请配置 CNLC_COMPANY_TOKEN，或同时配置 LOGIN_URL、USERNAME、PASSWORD"
            )
        return self


class CompanyCallResult(Contract):
    """一次大步骤调用的来源记录；细分功能共享此 ID，不虚构多次专业计算。"""

    external_call_id: str = Field(default_factory=lambda: uuid4().hex)
    operation: str
    execution_id: str = Field(min_length=1)
    input_version_id: str = Field(min_length=1)
    data: JsonValue
    started_at: datetime
    finished_at: datetime


class PreprocessingResult(Contract):
    """预处理同时提供预测请求数据和真实文件字节，落盘由应用层管理。"""

    call: CompanyCallResult
    gdsx_content: bytes


def _check_body(response: httpx.Response) -> JsonObject:
    """HTTP 200 不等于业务成功；仅接受代码中可识别的成功信封。"""
    try:
        value = response.json()
    except ValueError:
        raise ToolError("COMPANY_INVALID_RESPONSE", "公司服务返回非 JSON 数据") from None
    if not isinstance(value, dict):
        raise ToolError("COMPANY_INVALID_RESPONSE", "公司服务返回结构不合法")
    if value.get("success") is False or ("code" in value and value["code"] != 200):
        # 仅呈现服务端的业务码和简短说明，不保存完整响应，避免把内部数据、
        # 调试栈或鉴权信息带入任务状态和前端。
        code = value.get("code", "unknown")
        # 仅接受公司标准响应信封的 msg；message/error 可能来自网关或下游
        # 原始异常，不进入浏览器可见状态。
        detail = value.get("msg")
        if isinstance(detail, str) and detail.strip():
            detail = " ".join(detail.split())[:240]
            message = f"公司服务业务失败（code={code}）：{detail}"
        else:
            message = f"公司服务业务失败（code={code}），未返回可展示说明"
        raise ToolError("COMPANY_REPORTED_FAILURE", message)
    if value.get("success") is not True and value.get("code") != 200:
        raise ToolError("COMPANY_UNCONFIRMED_RESPONSE", "公司服务缺少已知的成功标识")
    return value


class CompanyApiClient:
    """直接复用 wplm/tools.py 的 HTTP 协议；不使用 Flask 路由或固定 Mock 输出。"""

    def __init__(
        self, settings: CompanyApiSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_seconds, connect=10),
            follow_redirects=False,
            transport=transport,
        )
        self._token: str | None = (
            settings.token.get_secret_value() if settings.token is not None else None
        )
        self._auth_lock = asyncio.Lock()

    async def aclose(self) -> None:
        """释放客户端；不停止任何公司服务。"""
        await self._client.aclose()

    async def _login(self) -> str:
        """按 yuce_api.py 协议登录；不记录账号、密码或 Token。"""
        if not (self.settings.login_url and self.settings.username and self.settings.password):
            raise ToolError("COMPANY_AUTH_CONFIG_MISSING", "公司接口未配置有效认证信息")
        try:
            response = await self._client.post(
                self.settings.login_url,
                params={
                    "username": self.settings.username,
                    "password": self.settings.password.get_secret_value(),
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException:
            raise ToolError("COMPANY_TIMEOUT", "公司登录接口超时") from None
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError):
            raise ToolError("COMPANY_AUTH_FAILED", "公司登录接口请求失败") from None
        token = payload.get("data", {}).get("token") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("code") != 200
            or not isinstance(token, str)
            or not token.strip()
        ):
            raise ToolError("COMPANY_AUTH_FAILED", "公司登录失败")
        return token

    async def _auth_token(self, *, refresh: bool = False) -> str:
        async with self._auth_lock:
            if self._token and not refresh:
                return self._token
            self._token = await self._login()
            return self._token

    async def _post(
        self, url: str, *, authenticated: bool = False, **kwargs: Any
    ) -> httpx.Response:
        """调用 API；认证失败时刷新 Token 后仅重试一次。"""
        token = await self._auth_token() if authenticated else None
        headers = {"Authorization": token} if token else {}
        try:
            response = await self._client.post(url, headers=headers, **kwargs)
            if (
                authenticated
                and response.status_code in {401, 403}
                and self.settings.login_url
                and self.settings.username
                and self.settings.password
            ):
                token = await self._auth_token(refresh=True)
                headers["Authorization"] = token
                response = await self._client.post(url, headers=headers, **kwargs)
            response.raise_for_status()
            return response
        except httpx.TimeoutException:
            raise ToolError(
                "COMPANY_TIMEOUT", "公司接口超时；结果未知，请先核实服务端任务"
            ) from None
        except httpx.HTTPStatusError as exc:
            code = (
                "COMPANY_AUTH_FAILED"
                if exc.response.status_code in {401, 403}
                else "COMPANY_HTTP_FAILED"
            )
            raise ToolError(code, "公司接口请求失败，请检查鉴权和服务状态") from None
        except httpx.RequestError:
            raise ToolError(
                "COMPANY_CONNECTION_FAILED", "无法连接公司接口，请检查内网连接"
            ) from None

    @staticmethod
    def _check_context(execution_id: str, input_version_id: str) -> None:
        """调用必须归属于明确执行和输入版本，防止跨任务混用结果。"""
        if not execution_id or not input_version_id:
            raise ToolError("COMPANY_CONTEXT_REQUIRED", "调用必须绑定执行和输入版本")

    async def preprocess(
        self,
        path: Path,
        operations: JsonObject,
        execution_id: str,
        input_version_id: str,
    ) -> PreprocessingResult:
        """上传、处理、下载构成一次预处理；子操作参数由调用方明确提供。"""
        self._check_context(execution_id, input_version_id)
        if not await asyncio.to_thread(path.is_file) or path.suffix.lower() != ".gdsx":
            raise ToolError("COMPANY_FILE_INVALID", "需要一个存在的 GDSX 文件")
        if not operations or "files" in operations:
            raise ToolError(
                "COMPANY_OPERATIONS_INVALID", "请提供预处理参数，不能自行覆盖服务端文件引用"
            )
        started = utc_now()
        base = self.settings.preprocessing_url
        with path.open("rb") as stream:
            uploaded = _check_body(
                await self._post(
                    base + "api/file/upload",
                    files={"file": (path.name, stream, "application/octet-stream")},
                )
            )
        data = uploaded.get("data")
        server_path = data.get("filePath") if isinstance(data, dict) else None
        if not isinstance(server_path, str) or not server_path:
            raise ToolError("COMPANY_UPLOAD_PATH_MISSING", "上传结果缺少服务端文件标识")
        processed = _check_body(
            await self._post(
                base + "api/data/processForGDSX",
                json={**operations, "files": [server_path]},
            )
        )
        log_req = processed.get("data")
        if not isinstance(log_req, (dict, list)) or not log_req:
            raise ToolError("COMPANY_PREPROCESS_DATA_MISSING", "预处理缺少预测输入数据")
        downloaded = await self._post(base + "api/file/download", json={"filePath": server_path})
        # GDSX 使用 HDF5；避免把 HTTP 200 的 JSON 错误页当成处理后文件。
        if not downloaded.content.startswith(b"\x89HDF\r\n\x1a\n"):
            raise ToolError("COMPANY_INVALID_GDSX", "下载结果不是可识别的 GDSX/HDF5 文件")
        return PreprocessingResult(
            call=CompanyCallResult(
                operation="preprocessing",
                execution_id=execution_id,
                input_version_id=input_version_id,
                started_at=started,
                finished_at=utc_now(),
                data={"logReqJson": log_req, "operations": operations},
            ),
            gdsx_content=downloaded.content,
        )

    async def predict(
        self,
        parameters: JsonObject,
        execution_id: str,
        input_version_id: str,
    ) -> CompanyCallResult:
        """保留原始结构化预测结果，避免旧网关在后处理后仅返回文件地址。"""
        self._check_context(execution_id, input_version_id)
        required = ("wellName", "serviceId", "logReqJson", "taskConfig")
        if any(not parameters.get(key) for key in required):
            raise ToolError(
                "COMPANY_PREDICT_INPUT_MISSING", "预测缺少井名、服务标识、数据或任务配置"
            )
        started = utc_now()
        # 公司同步推理协议要求该字段始终存在；留空表示使用服务端默认编码服务。
        parameters = {**parameters, "encodingUrl": self.settings.encoding_url}
        payload = _check_body(
            await self._post(
                self.settings.prediction_url + "center/InferenceLog/encodingInferenceBySyn/v1",
                authenticated=True,
                json=parameters,
            )
        )
        # 只保留专业数据，不让上游自由文本、鉴权或错误详情进入任务状态。
        result = extract_prediction_data(payload)
        return CompanyCallResult(
            operation="interpretation",
            execution_id=execution_id,
            input_version_id=input_version_id,
            data={"resultData": [item for item in result]},
            started_at=started,
            finished_at=utc_now(),
        )

    async def list_models(self, parameters: JsonObject) -> JsonValue:
        """沿用公司的模型服务查询参数；不猜测 serviceId。"""
        payload = _check_body(
            await self._post(
                self.settings.prediction_url + "center/ServiceInfo/get-page/v1",
                authenticated=True,
                json=parameters,
            )
        )
        return payload.get("data")


def extract_prediction_data(payload: JsonObject) -> list[JsonObject]:
    """兼容旧后处理代码确认的三种 resultData 包装，拒绝空结果和多井混合。"""
    value: Any = payload.get("resultData")
    outer = payload.get("data")
    if value is None and isinstance(outer, dict):
        evaluation = outer.get("evaluationData")
        if isinstance(evaluation, dict):
            value = evaluation.get("resultData")
        inner = outer.get("data")
        if value is None and isinstance(inner, dict):
            inner_evaluation = inner.get("evaluationData")
            if isinstance(inner_evaluation, dict):
                value = inner_evaluation.get("resultData")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if isinstance(value, dict):
        value = [value]
    if (
        not isinstance(value, list)
        or len(value) != 1
        or not isinstance(value[0], dict)
        or not value[0]
    ):
        raise ToolError("COMPANY_PREDICTION_DATA_INVALID", "预测结果必须包含一口井的结构化数据")
    return value
