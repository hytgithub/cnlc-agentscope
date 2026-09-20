"""Every tool uses one input/output envelope; timeout is enforced by the caller."""

import asyncio
from typing import Protocol

from pydantic import Field
from pydantic import ValidationError as SchemaError

from cnlc_agent.application.ports import Telemetry
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import ApplicationError, ToolError
from cnlc_agent.domain.models import Contract, ErrorDetail, JsonObject, WellId


class ToolInput(Contract):
    task_id: str
    trace_id: str
    well_id: WellId
    step_id: StepId
    parameters: JsonObject = Field(default_factory=dict)


class ToolOutput(Contract):
    status: StepStatus
    data: JsonObject = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[ErrorDetail] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)


class Tool(Protocol):
    @property
    def name(self) -> str: ...

    async def execute(self, request: ToolInput) -> ToolOutput: ...


class ToolCaller:
    def __init__(self, telemetry: Telemetry, timeout_seconds: float) -> None:
        self.telemetry = telemetry
        self.timeout_seconds = timeout_seconds

    async def call(self, tool: Tool, request: ToolInput) -> ToolOutput:
        attributes: JsonObject = {
            "task_id": request.task_id,
            "trace_id": request.trace_id,
            "step_id": request.step_id.value,
            "tool": tool.name,
        }
        with self.telemetry.span("tool", attributes):
            try:
                output = await asyncio.wait_for(tool.execute(request), self.timeout_seconds)
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
