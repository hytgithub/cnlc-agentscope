"""AgentScope 直接调用公司预处理和预测服务；qwen-agent-main 仅是协议参考。"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import Field, JsonValue, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cnlc_agent.domain.errors import ToolError
from cnlc_agent.domain.models import Contract, JsonObject, utc_now


class CompanyApiSettings(BaseSettings):
    """公司服务独立配置，不依赖 qwen-agent-main 启动，也不复用聊天模型密钥。"""

    model_config = SettingsConfigDict(env_prefix="CNLC_COMPANY_", env_file=".env", extra="ignore")
    preprocessing_url: str
    prediction_url: str
    token: SecretStr
    timeout_seconds: float = Field(default=660, gt=0, le=3600)

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

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        """凭据仅从环境或本地配置读取，不复制参考仓库中的账号信息。"""
        if not value.get_secret_value().strip():
            raise ValueError("公司预测服务凭据不能为空")
        return value


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
        raise ToolError("COMPANY_REPORTED_FAILURE", "公司服务未确认业务成功")
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

    async def aclose(self) -> None:
        """释放客户端；不停止任何公司服务。"""
        await self._client.aclose()

    async def _post(
        self, url: str, *, authenticated: bool = False, **kwargs: Any
    ) -> httpx.Response:
        """失败不自动重试，避免长时预测因超时而重复创建任务。"""
        headers = {"Authorization": self.settings.token.get_secret_value()} if authenticated else {}
        try:
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
