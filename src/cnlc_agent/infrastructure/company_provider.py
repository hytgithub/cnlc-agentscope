"""真实公司批量 Provider：隔离本地文件、HTTP 协议和业务 Tool Contract。"""

import asyncio
from pathlib import Path
from typing import Protocol, cast

from pydantic import Field, JsonValue, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from cnlc_agent.application.company_results import project_prediction
from cnlc_agent.application.ports import TaskRepository
from cnlc_agent.domain.company_provider import (
    CompanyProviderCall,
    CompanyProviderCallStatus,
    CompanyProviderOperation,
    RealPredictionContext,
)
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import ApplicationError, ToolError
from cnlc_agent.domain.models import JsonObject, StageResult
from cnlc_agent.domain.tool_run import ToolExecutionMode
from cnlc_agent.infrastructure.company_api import CompanyApiClient, CompanyCallResult
from cnlc_agent.tools.company_batches import CompanyBatchResponse
from cnlc_agent.tools.contracts import ToolInput


class CompanyInputResolver(Protocol):
    """将业务逻辑 ID 解析为基础设施输入；Path 不离开此适配器边界。"""

    async def resolve_gdsx(
        self, task_id: str, execution_id: str, input_version_id: str
    ) -> Path: ...

    async def resolve_preprocess_operations(
        self, task_id: str, execution_id: str, input_version_id: str
    ) -> JsonObject: ...

    async def resolve_prediction_context(
        self, task_id: str, execution_id: str, input_version_id: str
    ) -> RealPredictionContext: ...


class CompanyArtifactSink(Protocol):
    """处理后 GDSX 的临时扩展点；返回逻辑引用，当前不写 PostgreSQL。"""

    async def save_processed_gdsx(
        self,
        task_id: str,
        execution_id: str,
        input_version_id: str,
        content: bytes,
    ) -> str: ...


class CompanyProviderSettings(BaseSettings):
    """临时本地解析器的显式配置；11G 将由正式 Artifact/Input 适配器替换。"""

    model_config = SettingsConfigDict(env_prefix="CNLC_COMPANY_", env_file=".env", extra="ignore")

    gdsx_path: Path
    well_name: str = Field(min_length=1, max_length=128)
    service_id: str = Field(min_length=1, max_length=128)
    task_config: JsonObject
    preprocess_operations: JsonObject


class ConfiguredCompanyInputResolver:
    """仅从明确配置解析本地验证输入，不从聊天、cwd 或 ActiveContext 猜路径。"""

    def __init__(self, settings: CompanyProviderSettings) -> None:
        self.settings = settings

    async def resolve_gdsx(self, task_id: str, execution_id: str, input_version_id: str) -> Path:
        del task_id, execution_id, input_version_id
        path = self.settings.gdsx_path.expanduser().resolve()
        if not path.is_file() or path.suffix.casefold() != ".gdsx":
            raise ToolError("COMPANY_FILE_INVALID", "配置的 GDSX 输入不存在或类型错误")
        return path

    async def resolve_preprocess_operations(
        self, task_id: str, execution_id: str, input_version_id: str
    ) -> JsonObject:
        del task_id, execution_id, input_version_id
        if not self.settings.preprocess_operations:
            raise ToolError("COMPANY_OPERATIONS_INVALID", "真实预处理操作不能为空")
        return dict(self.settings.preprocess_operations)

    async def resolve_prediction_context(
        self, task_id: str, execution_id: str, input_version_id: str
    ) -> RealPredictionContext:
        del task_id, execution_id, input_version_id
        return RealPredictionContext(
            well_name=self.settings.well_name,
            service_id=self.settings.service_id,
            task_config=self.settings.task_config,
        )


class RealCompanyBatchProvider:
    """调用真实 HTTP 客户端并将可恢复的最小结果持久化为 ProviderCall。"""

    execution_mode = ToolExecutionMode.REAL

    def __init__(
        self,
        client: CompanyApiClient,
        repository: TaskRepository,
        resolver: CompanyInputResolver,
        artifact_sink: CompanyArtifactSink | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self.resolver = resolver
        self.artifact_sink = artifact_sink

    @staticmethod
    def _context(request: ToolInput) -> tuple[str, str]:
        execution_id = request.parameters.get("execution_id")
        input_version_id = request.parameters.get("input_version_id")
        if not isinstance(execution_id, str) or not isinstance(input_version_id, str):
            raise ToolError("COMPANY_CONTEXT_REQUIRED", "真实调用必须绑定执行和输入版本")
        return execution_id, input_version_id

    async def execute(self, stage: str, request: ToolInput) -> CompanyBatchResponse:
        """只实现已确认的预处理和预测能力，其他 operation 明确拒绝。"""

        if stage not in {
            CompanyProviderOperation.PREPROCESSING.value,
            CompanyProviderOperation.INTERPRETATION.value,
        }:
            raise ToolError("COMPANY_OPERATION_NOT_IMPLEMENTED", "公司真实接口尚未提供该 operation")
        execution_id, input_version_id = self._context(request)
        execution = await self.repository.get_execution(execution_id)
        if (
            execution is None
            or execution.task_id != request.task_id
            or execution.input_version_id != input_version_id
        ):
            raise ToolError("COMPANY_RESULT_VERSION_MISMATCH", "请求不属于当前执行及输入版本")
        if stage == CompanyProviderOperation.PREPROCESSING.value:
            return await self._preprocess(request, execution_id, input_version_id)
        return await self._interpret(request, execution_id, input_version_id)

    async def _start_call(
        self,
        request: ToolInput,
        execution_id: str,
        input_version_id: str,
        operation: CompanyProviderOperation,
        summary: JsonObject,
    ) -> CompanyProviderCall:
        call = CompanyProviderCall(
            task_id=request.task_id,
            execution_id=execution_id,
            input_version_id=input_version_id,
            provider_operation=operation,
            request_summary=summary,
        )
        return await self.repository.create_company_provider_call(call)

    async def _finish_failed(self, call: CompanyProviderCall, code: str) -> None:
        await self.repository.finish_company_provider_call(
            call.external_call_id,
            status=(
                CompanyProviderCallStatus.UNKNOWN
                if code == "COMPANY_TIMEOUT"
                else CompanyProviderCallStatus.FAILED
            ),
            normalized_result={},
            error_code=code,
        )

    async def _finish_unknown(self, call: CompanyProviderCall, code: str) -> None:
        """调用被超时取消时服务端结果不可判定，禁止降格为普通失败。"""

        await self.repository.finish_company_provider_call(
            call.external_call_id,
            status=CompanyProviderCallStatus.UNKNOWN,
            normalized_result={},
            error_code=code,
        )

    async def _existing_call(
        self,
        request: ToolInput,
        execution_id: str,
        input_version_id: str,
        operation: CompanyProviderOperation,
    ) -> CompanyProviderCall | None:
        """同一业务身份只允许一个真实调用；任何非成功状态都禁止自动重发。"""

        calls = await self.repository.list_company_provider_calls(
            request.task_id,
            execution_id=execution_id,
            operation=operation,
        )
        matching = [item for item in calls if item.input_version_id == input_version_id]
        if not matching:
            return None
        current = matching[-1]
        if current.status == CompanyProviderCallStatus.SUCCESS:
            return current
        codes = {
            CompanyProviderCallStatus.RUNNING: "COMPANY_PROVIDER_CALL_RUNNING",
            CompanyProviderCallStatus.UNKNOWN: "COMPANY_PROVIDER_CALL_UNKNOWN",
            CompanyProviderCallStatus.FAILED: "COMPANY_PROVIDER_CALL_FAILED",
        }
        raise ToolError(codes[current.status], "已有真实调用未成功，不允许自动重复提交")

    @staticmethod
    def _preprocess_response(call: CompanyProviderCall) -> CompanyBatchResponse:
        """从成功持久事实恢复 W03，不依赖进程内批量缓存。"""

        operation_keys = call.normalized_result.get("operation_keys", [])
        artifact_ref = call.normalized_result.get("processed_artifact_ref")
        qc = StageResult(
            status=StepStatus.WARNING,
            result={
                "provider_operation": "preprocessing",
                "operations_applied": True,
                "operation_keys": operation_keys,
                **({"processed_artifact_ref": artifact_ref} if artifact_ref is not None else {}),
            },
            evidence=["真实公司预处理接口已完成并返回预测输入"],
            warnings=["公司接口未提供正式 QC 统计，当前只确认预处理传输完成"],
            recommended_action="review_preprocessing_result",
            is_mock=False,
            source=f"company:real:preprocessing:{call.external_call_id}",
        )
        return CompanyBatchResponse(
            data={"qc": qc.model_dump(mode="json")},
            provider_call_id=call.provider_call_id,
            external_call_id=call.external_call_id,
            execution_mode=ToolExecutionMode.REAL,
            source=f"company:real:preprocessing:{call.external_call_id}",
            is_mock=False,
        )

    @staticmethod
    def _interpretation_response(call: CompanyProviderCall) -> CompanyBatchResponse:
        """从成功持久事实重新投影 W04-W08，保证重启后不重发预测。"""

        result_data = call.normalized_result.get("resultData")
        projection = project_prediction(
            CompanyCallResult(
                external_call_id=call.external_call_id,
                operation=CompanyProviderOperation.INTERPRETATION.value,
                execution_id=call.execution_id,
                input_version_id=call.input_version_id,
                data={"resultData": result_data},
                started_at=call.started_at,
                finished_at=call.finished_at or call.started_at,
            )
        )
        step_keys = {
            "lithology": "W04",
            "petrophysics": "W05",
            "sw": "W06",
            "fluid": "W06",
            "classification": "W07",
            "intervals": "W08",
        }
        by_step = {step.value: value for step, value in projection.steps.items()}
        data: JsonObject = {
            key: cast(JsonValue, by_step[step].model_dump(mode="json"))
            for key, step in step_keys.items()
        }
        return CompanyBatchResponse(
            data=data,
            provider_call_id=call.provider_call_id,
            external_call_id=call.external_call_id,
            execution_mode=ToolExecutionMode.REAL,
            source=f"company:real:interpretation:{call.external_call_id}",
            is_mock=False,
        )

    async def _finish_success(
        self, call: CompanyProviderCall, normalized: JsonObject
    ) -> CompanyProviderCall:
        try:
            return await self.repository.finish_company_provider_call(
                call.external_call_id,
                status=CompanyProviderCallStatus.SUCCESS,
                normalized_result=normalized,
            )
        except (ValidationError, ValueError) as exc:
            code = (
                "PROVIDER_RESULT_TOO_LARGE"
                if "PROVIDER_RESULT_TOO_LARGE" in str(exc)
                else "PROVIDER_RESULT_INVALID"
            )
            await self._finish_failed(call, code)
            raise ToolError(code, "公司规范化结果超出当前持久化边界") from None

    async def _preprocess(
        self, request: ToolInput, execution_id: str, input_version_id: str
    ) -> CompanyBatchResponse:
        existing = await self._existing_call(
            request,
            execution_id,
            input_version_id,
            CompanyProviderOperation.PREPROCESSING,
        )
        if existing is not None:
            return self._preprocess_response(existing)
        path = await self.resolver.resolve_gdsx(request.task_id, execution_id, input_version_id)
        operations = await self.resolver.resolve_preprocess_operations(
            request.task_id, execution_id, input_version_id
        )
        call = await self._start_call(
            request,
            execution_id,
            input_version_id,
            CompanyProviderOperation.PREPROCESSING,
            {
                "well_id": request.well_id,
                "operation_keys": cast(JsonValue, sorted(operations)),
                "effective_parameters": request.parameters.get("effective_override", {}),
            },
        )
        try:
            result = await self.client.preprocess(
                path,
                operations,
                execution_id,
                input_version_id,
                external_call_id=call.external_call_id,
            )
            artifact_ref = None
            if self.artifact_sink is not None:
                artifact_ref = await self.artifact_sink.save_processed_gdsx(
                    request.task_id,
                    execution_id,
                    input_version_id,
                    result.gdsx_content,
                )
            call_data = result.call.data
            raw_log_request = call_data.get("logReqJson") if isinstance(call_data, dict) else None
            if not isinstance(raw_log_request, (dict, list)) or not raw_log_request:
                raise ToolError("COMPANY_PREPROCESS_DATA_MISSING", "预处理缺少预测输入数据")
            normalized: JsonObject = {
                "provider_operation": CompanyProviderOperation.PREPROCESSING.value,
                "external_call_id": call.external_call_id,
                "input_version_id": input_version_id,
                "logReqJson": raw_log_request,
                "operation_keys": cast(JsonValue, sorted(operations)),
            }
            if artifact_ref is not None:
                normalized["processed_artifact_ref"] = artifact_ref
            persisted = await self._finish_success(call, normalized)
        except asyncio.CancelledError:
            stored = await self.repository.get_company_provider_call(call.external_call_id)
            if stored is not None and stored.status == CompanyProviderCallStatus.RUNNING:
                await asyncio.shield(self._finish_unknown(call, "COMPANY_TIMEOUT"))
            raise
        except ApplicationError as exc:
            stored = await self.repository.get_company_provider_call(call.external_call_id)
            if stored is not None and stored.status == CompanyProviderCallStatus.RUNNING:
                await self._finish_failed(call, exc.code)
            raise
        return self._preprocess_response(persisted)

    async def _interpret(
        self, request: ToolInput, execution_id: str, input_version_id: str
    ) -> CompanyBatchResponse:
        calls = await self.repository.list_company_provider_calls(
            request.task_id,
            execution_id=execution_id,
            operation=CompanyProviderOperation.PREPROCESSING,
        )
        matching = [
            item
            for item in calls
            if item.input_version_id == input_version_id
            and item.status == CompanyProviderCallStatus.SUCCESS
        ]
        if not matching:
            historical = await self.repository.list_company_provider_calls(
                request.task_id, operation=CompanyProviderOperation.PREPROCESSING
            )
            code = (
                "COMPANY_RESULT_VERSION_MISMATCH"
                if any(item.status == CompanyProviderCallStatus.SUCCESS for item in historical)
                else "COMPANY_PREPROCESS_RESULT_REQUIRED"
            )
            raise ToolError(code, "当前执行缺少同版本的真实预处理结果")
        preprocess = matching[-1]
        log_request = preprocess.normalized_result.get("logReqJson")
        if not isinstance(log_request, (dict, list)) or not log_request:
            raise ToolError("COMPANY_PREPROCESS_DATA_MISSING", "持久预处理结果缺少预测输入")
        existing = await self._existing_call(
            request,
            execution_id,
            input_version_id,
            CompanyProviderOperation.INTERPRETATION,
        )
        if existing is not None:
            return self._interpretation_response(existing)
        context = await self.resolver.resolve_prediction_context(
            request.task_id, execution_id, input_version_id
        )
        call = await self._start_call(
            request,
            execution_id,
            input_version_id,
            CompanyProviderOperation.INTERPRETATION,
            {
                "well_id": request.well_id,
                "well_name": context.well_name,
                "service_id": context.service_id,
                "task_config_keys": cast(JsonValue, sorted(context.task_config)),
                "preprocessing_call_id": preprocess.external_call_id,
                "effective_parameters": request.parameters.get("effective_override", {}),
            },
        )
        try:
            result = await self.client.predict(
                {
                    "wellName": context.well_name,
                    "serviceId": context.service_id,
                    "logReqJson": log_request,
                    "taskConfig": context.task_config,
                },
                execution_id,
                input_version_id,
                external_call_id=call.external_call_id,
            )
            result_data = result.data
            if not isinstance(result_data, dict) or "resultData" not in result_data:
                raise ToolError("COMPANY_PREDICTION_DATA_INVALID", "预测结果缺少规范化单井数据")
            normalized: JsonObject = {
                "provider_operation": CompanyProviderOperation.INTERPRETATION.value,
                "external_call_id": call.external_call_id,
                "input_version_id": input_version_id,
                "preprocessing_call_id": preprocess.external_call_id,
                "resultData": result_data["resultData"],
            }
            # 先完成字段结构校验，再把响应标记为可恢复成功事实。
            project_prediction(result)
            persisted = await self._finish_success(call, normalized)
        except asyncio.CancelledError:
            stored = await self.repository.get_company_provider_call(call.external_call_id)
            if stored is not None and stored.status == CompanyProviderCallStatus.RUNNING:
                await asyncio.shield(self._finish_unknown(call, "COMPANY_TIMEOUT"))
            raise
        except ApplicationError as exc:
            stored = await self.repository.get_company_provider_call(call.external_call_id)
            if stored is not None and stored.status == CompanyProviderCallStatus.RUNNING:
                await self._finish_failed(call, exc.code)
            raise
        return self._interpretation_response(persisted)
