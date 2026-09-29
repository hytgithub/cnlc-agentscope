"""AgentScope 直接调用公司预处理和预测服务；qwen-agent-main 仅是协议参考。"""

import asyncio
import json
import time
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
    token: SecretStr | None = None
    login_url: str | None = None
    username: str | None = None
    password: SecretStr | None = None
    timeout_seconds: float = Field(default=660, gt=0, le=3600)
    gdsx_path: Path | None = None
    service_id: str | None = None
    create_people: str | None = None
    resample_interval: float = Field(default=0.1, gt=0)
    batch_size: int = Field(default=1024, gt=0, le=100000)
    business_url: str | None = None
    business_token: SecretStr | None = None
    report_type: str | None = None
    report_zone: str = ""
    report_gdsx_type: str | None = None
    report_minio_file_type: str | None = None
    report_poll_seconds: float = Field(default=3, gt=0, le=60)
    report_timeout_seconds: float = Field(default=900, gt=0, le=7200)

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
        """登录地址与业务基址一样，不允许携带凭据或查询串。"""
        if value is None:
            return value
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query:
            raise ValueError("登录地址必须是不含凭据和查询串的 HTTP(S) 地址")
        return value

    @field_validator("business_url")
    @classmethod
    def validate_business_url(cls, value: str | None) -> str | None:
        """报告业务网关独立配置；AgentScope 只调用接口，不导入其他项目源码。"""
        if value is None:
            return value
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query:
            raise ValueError("报告业务地址必须是不含凭据和查询串的 HTTP(S) 地址")
        return value.rstrip("/") + "/"

    @model_validator(mode="after")
    def validate_auth(self) -> "CompanyApiSettings":
        """支持短期 Token，或使用已验证的登录协议动态获取 Token。"""
        has_token = self.token is not None and bool(self.token.get_secret_value().strip())
        has_login = bool(self.login_url and self.username and self.password)
        if not has_token and not has_login:
            raise ValueError("请配置公司 Token，或完整的登录地址、用户名和密码")
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
        self._token = settings.token.get_secret_value() if settings.token is not None else None
        self._auth_lock = asyncio.Lock()

    async def aclose(self) -> None:
        """释放客户端；不停止任何公司服务。"""
        await self._client.aclose()

    async def _login(self) -> str:
        """按已验证的 POST query-params 协议取得预测中心 Token。"""
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
        """并发调用只刷新一次 Token，避免重复登录。"""
        async with self._auth_lock:
            if self._token and not refresh:
                return self._token
            self._token = await self._login()
            return self._token

    async def _post(
        self, url: str, *, authenticated: bool = False, **kwargs: Any
    ) -> httpx.Response:
        """预测不自动重试；仅在鉴权失败时刷新 Token 后重放一次请求。"""
        supplied_headers = kwargs.pop("headers", {})
        headers = dict(supplied_headers) if isinstance(supplied_headers, dict) else {}
        if authenticated:
            headers["Authorization"] = await self._auth_token()
        try:
            response = await self._client.post(url, headers=headers, **kwargs)
            if (
                authenticated
                and response.status_code in {401, 403}
                and self.settings.login_url
                and self.settings.username
                and self.settings.password
            ):
                headers["Authorization"] = await self._auth_token(refresh=True)
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

    async def _get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
        """读取正式业务任务状态；网络错误与 POST 使用相同的安全错误边界。"""
        try:
            response = await self._client.get(url, headers=headers)
            response.raise_for_status()
            return response
        except httpx.TimeoutException:
            raise ToolError("COMPANY_TIMEOUT", "公司接口超时；结果未知，请先核实服务端任务") from None
        except httpx.HTTPStatusError as exc:
            code = "COMPANY_AUTH_FAILED" if exc.response.status_code in {401, 403} else "COMPANY_HTTP_FAILED"
            raise ToolError(code, "公司接口请求失败，请检查鉴权和服务状态") from None
        except httpx.RequestError:
            raise ToolError("COMPANY_CONNECTION_FAILED", "无法连接公司接口，请检查内网连接") from None

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
        """查询运行中模型，并兼容公司接口出现过的两种响应信封。"""
        payload = _check_body(
            await self._post(
                self.settings.prediction_url + "center/ServiceInfo/get-page/v1",
                authenticated=True,
                json=parameters,
            )
        )
        outer = payload.get("obj")
        if not isinstance(outer, dict):
            outer = payload.get("data")
        items = outer.get("items") if isinstance(outer, dict) else outer
        if not isinstance(items, list):
            raise ToolError("COMPANY_MODEL_LIST_INVALID", "公司模型列表返回结构不合法")
        return items

    def _business_headers(self) -> dict[str, str]:
        token = (
            self.settings.business_token.get_secret_value().strip()
            if self.settings.business_token is not None
            else ""
        )
        if not self.settings.business_url or not token:
            raise ToolError(
                "COMPANY_REPORT_CONFIG_MISSING",
                "真实解释结论和报告接口尚未配置",
            )
        return {"Authorization": token}

    @staticmethod
    def _legacy_data(response: httpx.Response) -> JsonValue:
        """解析正式业务网关的 code/data 信封，不接受未确认成功的响应。"""
        try:
            payload = response.json()
        except ValueError:
            raise ToolError("COMPANY_INVALID_RESPONSE", "公司业务接口返回非 JSON 数据") from None
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise ToolError("COMPANY_REPORTED_FAILURE", "公司业务接口未确认成功")
        return payload.get("data")

    async def upload_report_gdsx(self, path: Path) -> JsonObject:
        """通过正式文件入口上传最终 GDSX，返回报告服务识别的 upload_id。"""
        headers = self._business_headers()
        if not self.settings.report_minio_file_type:
            raise ToolError("COMPANY_REPORT_CONFIG_MISSING", "未配置报告 GDSX 文件类型")
        with path.open("rb") as stream:
            response = await self._post(
                self.settings.business_url + "upload/minio",
                headers=headers,
                files={"file": (path.name, stream, "application/octet-stream")},
                data={"file_type": self.settings.report_minio_file_type},
            )
        data = self._legacy_data(response)
        results = data.get("upload_results") if isinstance(data, dict) else None
        item = results[0] if isinstance(results, list) and results else None
        if not isinstance(item, dict) or not item.get("upload_id"):
            raise ToolError("COMPANY_GDSX_UPLOAD_FAILED", "最终 GDSX 上传未返回 upload_id")
        return item

    async def get_gdsx_conclusions(self, upload_id: str) -> list[JsonObject]:
        """调用正式 GDSX 解释结论入口；不由 AgentScope 推测结论。"""
        response = await self._post(
            self.settings.business_url + "explanation/gdsx-interresult",
            headers=self._business_headers(),
            json={"author_info": {}, "gdsx_upload_ids": [upload_id]},
        )
        data = self._legacy_data(response)
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], list):
            data = data[0]
        if not isinstance(data, list) or not data or not all(isinstance(item, dict) for item in data):
            raise ToolError("COMPANY_INTERRESULT_EMPTY", "正式接口未返回可用解释结论")
        return data

    async def generate_report(self, payload: JsonObject) -> JsonObject:
        """提交真实异步报告任务并轮询终态；失败不会回退成本地报告。"""
        headers = self._business_headers()
        submitted = self._legacy_data(
            await self._post(
                self.settings.business_url + "explanation/report-tasks",
                headers=headers,
                json=payload,
            )
        )
        task_id = _pick_string(submitted, "task_id", "taskId", "id", "body")
        if not task_id and isinstance(submitted, list) and submitted:
            task_id = _pick_string(submitted[0], "task_id", "taskId", "id", "body")
        if not task_id:
            raise ToolError("COMPANY_REPORT_TASK_INVALID", "报告接口未返回 task_id")
        deadline = time.monotonic() + self.settings.report_timeout_seconds
        while time.monotonic() < deadline:
            progress = self._legacy_data(
                await self._get(
                    self.settings.business_url + f"explanation/report-tasks/{task_id}/progress",
                    headers=headers,
                )
            )
            status = _pick_string(progress, "status", "state").upper()
            if status in {"FINISHED", "SUCCESS", "DONE"}:
                result = dict(progress) if isinstance(progress, dict) else {"data": progress}
                result["task_id"] = task_id
                return result
            if status in {"FAILED", "FAIL", "ERROR"}:
                raise ToolError("COMPANY_REPORT_FAILED", "真实解释报告生成失败")
            await asyncio.sleep(self.settings.report_poll_seconds)
        raise ToolError("COMPANY_REPORT_TIMEOUT", "真实解释报告生成超时；任务状态未知")


def _pick_string(value: Any, *keys: str) -> str:
    """从已知响应键中读取非空字符串，不遍历或回显任意服务端内容。"""
    if not isinstance(value, dict):
        return ""
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    for container in ("data", "task_info"):
        nested = value.get(container)
        found = _pick_string(nested, *keys) if isinstance(nested, dict) else ""
        if found:
            return found
    return ""


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
