"""所有 Tool 使用统一输入输出外壳，超时由调用器集中控制。"""

import asyncio
from typing import Protocol

from pydantic import Field
from pydantic import ValidationError as SchemaError

from cnlc_agent.application.ports import TaskRepository, Telemetry
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import ApplicationError, InfrastructureError, ToolError
from cnlc_agent.domain.models import Contract, ErrorDetail, JsonObject, WellId
from cnlc_agent.domain.tool_run import ToolExecutionMode, ToolRun, ToolRunStatus
from cnlc_agent.tools.audit import bounded, tool_input_snapshot, tool_output_snapshot


class ToolInput(Contract):
    """Tool 调用输入，携带任务、追踪、井号和步骤上下文。"""

    task_id: str
    trace_id: str
    well_id: WellId
    step_id: StepId
    parameters: JsonObject = Field(default_factory=dict)


class ToolOutput(Contract):
    """Tool 标准输出，包含状态、数据、告警、错误和元数据。"""

    status: StepStatus
    data: JsonObject = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[ErrorDetail] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)


class Tool(Protocol):
    """业务 Tool 的最小执行契约。"""

    @property
    def name(self) -> str: ...

    @property
    def execution_mode(self) -> ToolExecutionMode: ...

    @property
    def source(self) -> str: ...

    async def execute(self, request: ToolInput) -> ToolOutput: ...


class ToolCaller:
    """集中负责 Tool 超时、输出校验、错误归类和 Trace。"""

    def __init__(
        self,
        telemetry: Telemetry,
        timeout_seconds: float,
        repository: TaskRepository | None = None,
    ) -> None:
        self.telemetry = telemetry
        self.timeout_seconds = timeout_seconds
        self.repository = repository

    async def call(self, tool: Tool, request: ToolInput) -> ToolOutput:
        """执行一次 Tool 调用，仅接受 SUCCESS 或 WARNING 终态。"""

        attributes: JsonObject = {
            "task_id": request.task_id,
            "trace_id": request.trace_id,
            "step_id": request.step_id.value,
            "tool": tool.name,
        }
        with self.telemetry.span("tool", attributes):
            run: ToolRun | None = None
            execution_id = request.parameters.get("execution_id")
            if isinstance(execution_id, str) and execution_id:
                if self.repository is None:
                    raise InfrastructureError(
                        "TOOL_RUN_REPOSITORY_REQUIRED", "执行工具审计需要任务仓库"
                    )
                mode = getattr(tool, "execution_mode", None)
                source = getattr(tool, "source", None)
                if not isinstance(mode, ToolExecutionMode) or not isinstance(source, str):
                    raise ToolError("TOOL_DESCRIPTOR_REQUIRED", "工具缺少明确来源描述")
                run = await self.repository.create_tool_run(ToolRun(
                    task_id=request.task_id,
                    execution_id=execution_id,
                    step_id=request.step_id,
                    tool_code=tool.name,
                    execution_mode=mode,
                    source=str(bounded(source)),
                    input_snapshot=tool_input_snapshot(
                        request.well_id, request.step_id.value, request.parameters
                    ),
                ))

            async def fail(error: ApplicationError) -> None:
                """先持久化安全失败，再由外层保持原有错误分类。"""

                if run is not None:
                    assert self.repository is not None
                    await self.repository.finish_tool_run(
                        run.tool_run_id,
                        status=ToolRunStatus.FAILED,
                        source=run.source,
                        output_snapshot=tool_output_snapshot("FAILED", {}, [], {}),
                        error_code=error.code,
                        error_message="工具调用失败；请按错误代码排查",
                    )

            try:
                raw_output = await asyncio.wait_for(tool.execute(request), self.timeout_seconds)
                # 即使已经是 Pydantic 对象也重新校验，防止嵌套容器原地变更后绕过约束。
                payload = (
                    raw_output.model_dump(mode="python", warnings=False)
                    if isinstance(raw_output, ToolOutput)
                    else raw_output
                )
                output = ToolOutput.model_validate(payload)
                if output.status not in (StepStatus.SUCCESS, StepStatus.WARNING) or output.errors:
                    raise ToolError("TOOL_REPORTED_FAILURE", f"工具报告未成功：{tool.name}")
            except TimeoutError as exc:
                error = ToolError("TOOL_TIMEOUT", f"工具超时：{tool.name}", retryable=True)
                await fail(error)
                raise error from exc
            except ApplicationError as exc:
                await fail(exc)
                raise
            except SchemaError as exc:
                error = ToolError("INVALID_TOOL_OUTPUT", f"工具输出格式错误：{tool.name}")
                await fail(error)
                raise error from exc
            except Exception as exc:
                error = ToolError("TOOL_FAILED", f"工具执行失败：{tool.name}")
                await fail(error)
                raise error from exc
            if run is not None:
                assert self.repository is not None
                metadata_source = output.metadata.get("source")
                await self.repository.finish_tool_run(
                    run.tool_run_id,
                    status=(ToolRunStatus.WARNING if output.status == StepStatus.WARNING
                            else ToolRunStatus.SUCCESS),
                    source=str(bounded(metadata_source)) if isinstance(metadata_source, str)
                           else run.source,
                    output_snapshot=tool_output_snapshot(
                        output.status.value, output.data, output.warnings, output.metadata
                    ),
                )
            self.telemetry.event("tool.result", {**attributes, "status": output.status.value})
            return output
