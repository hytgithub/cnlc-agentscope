"""所有 Tool 使用统一输入输出外壳，超时由调用器集中控制。"""

import asyncio
from typing import Protocol

from pydantic import Field
from pydantic import ValidationError as SchemaError

from cnlc_agent.application.ports import Telemetry
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import ApplicationError, ToolError
from cnlc_agent.domain.models import Contract, ErrorDetail, JsonObject, WellId


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

    async def execute(self, request: ToolInput) -> ToolOutput: ...


class ToolCaller:
    """集中负责 Tool 超时、输出校验、错误归类和 Trace。"""

    def __init__(self, telemetry: Telemetry, timeout_seconds: float) -> None:
        self.telemetry = telemetry
        self.timeout_seconds = timeout_seconds

    async def call(self, tool: Tool, request: ToolInput) -> ToolOutput:
        """执行一次 Tool 调用，仅接受 SUCCESS 或 WARNING 终态。"""

        attributes: JsonObject = {
            "task_id": request.task_id,
            "trace_id": request.trace_id,
            "step_id": request.step_id.value,
            "tool": tool.name,
        }
        with self.telemetry.span("tool", attributes):
            try:
                raw_output = await asyncio.wait_for(tool.execute(request), self.timeout_seconds)
                # 即使已经是 Pydantic 对象也重新校验，防止嵌套容器原地变更后绕过约束。
                payload = (
                    raw_output.model_dump(mode="python", warnings=False)
                    if isinstance(raw_output, ToolOutput)
                    else raw_output
                )
                output = ToolOutput.model_validate(payload)
            except TimeoutError as exc:
                raise ToolError("TOOL_TIMEOUT", f"工具超时：{tool.name}", retryable=True) from exc
            except ApplicationError:
                raise
            except SchemaError as exc:
                raise ToolError("INVALID_TOOL_OUTPUT", f"工具输出格式错误：{tool.name}") from exc
            except Exception as exc:
                raise ToolError("TOOL_FAILED", f"工具执行失败：{tool.name}") from exc
            if output.status not in (StepStatus.SUCCESS, StepStatus.WARNING) or output.errors:
                raise ToolError("TOOL_REPORTED_FAILURE", f"工具报告未成功：{tool.name}")
            self.telemetry.event("tool.result", {**attributes, "status": output.status.value})
            return output
