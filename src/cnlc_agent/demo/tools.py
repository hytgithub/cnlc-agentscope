"""The single AgentScope business tool exposed by the demo service."""

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk
from pydantic import BaseModel, ConfigDict

from cnlc_agent.application.runtime import application_runtime
from cnlc_agent.application.service import InterpretationTaskService
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.state import InterpretationState

RUN_TOOL_NAME = "run_well_interpretation"

ServiceContextFactory = Callable[
    [],
    AbstractAsyncContextManager[InterpretationTaskService],
]


class DemoToolResult(BaseModel):
    """Compact, stable result rendered by AgentScope as a Tool Result."""

    model_config = ConfigDict(extra="forbid")

    status: StepStatus
    task_id: str
    well_id: str
    completed_steps: list[StepId]
    step_statuses: dict[StepId, StepStatus]
    summary: str
    report_markdown: str


def _default_service_context() -> AbstractAsyncContextManager[InterpretationTaskService]:
    settings = AppSettings(mode="demo")
    connections = ConnectionSettings()
    if settings.model_provider != "mock" and connections.model_name != "qwen-plus":
        raise ValueError("AgentScope Demo 的 MODEL_NAME 必须为 qwen-plus")
    return application_runtime(
        settings,
        persistence=PersistenceSettings(),
        connections=connections,
    )


class InterpretationToolRunner:
    """Adapts the existing application service without duplicating workflow logic."""

    def __init__(self, service_context: ServiceContextFactory = _default_service_context) -> None:
        self._service_context = service_context

    async def run(self, well_id: str) -> DemoToolResult:
        async with self._service_context() as service:
            state, report_markdown = await service.run(TaskRequest(well_id=well_id))
        return DemoToolResult(
            status=state.status,
            task_id=state.task.task_id,
            well_id=state.task.well_id,
            completed_steps=state.completed_steps,
            step_statuses=_step_statuses(state),
            summary=_summary(state),
            report_markdown=report_markdown,
        )


def _step_statuses(state: InterpretationState) -> dict[StepId, StepStatus]:
    statuses = {step_id: StepStatus.PENDING for step_id in StepId}
    statuses.update({execution.step_id: execution.status for execution in state.executions})
    return statuses


def _summary(state: InterpretationState) -> str:
    if state.final_check is not None:
        summary = state.final_check.result.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
    completed = ", ".join(step.value for step in state.completed_steps) or "无"
    return f"井 {state.task.well_id} 解释任务状态为 {state.status.value}；已完成步骤：{completed}。"


_default_runner = InterpretationToolRunner()


async def run_well_interpretation(well_id: str) -> dict[str, object]:
    """对指定井执行现有 W01-W10 解释流程并返回摘要和 Markdown 报告。

    Args:
        well_id: 井标识，例如 ``WELL_MOCK_001``。
    """

    result = await _default_runner.run(well_id)
    return result.model_dump(mode="json")


def build_interpretation_tool(
    runner: InterpretationToolRunner | None = None,
) -> ToolBase:
    """Build the one business Tool registered in the AgentScope service."""

    return RunWellInterpretationTool(runner)


class RunWellInterpretationTool(ToolBase):
    """AgentScope Tool wrapper around :func:`run_well_interpretation`."""

    name = RUN_TOOL_NAME
    description = (
        "对 well_id 执行现有单井常规测井解释流程 W01-W10，返回各步骤状态、"
        "解释摘要和 Markdown 报告。"
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "well_id": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
                "description": "待解释的井标识，例如 WELL_MOCK_001。",
            },
        },
        "required": ["well_id"],
        "additionalProperties": False,
    }
    is_concurrency_safe = True
    is_read_only = False

    def __init__(self, runner: InterpretationToolRunner | None = None) -> None:
        super().__init__()
        self._runner = runner

    async def check_permissions(self, *_args: Any, **_kwargs: Any) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="用户请求执行单井解释时允许调用现有 InterpretationTaskService。",
        )

    async def call(self, *args: Any, **kwargs: Any) -> ToolChunk:
        if args:
            raise TypeError("run_well_interpretation 只接受关键字参数")
        well_id = cast(str, kwargs["well_id"])
        if self._runner is None:
            payload = await run_well_interpretation(well_id)
        else:
            result = await self._runner.run(well_id)
            payload = result.model_dump(mode="json")
        return ToolChunk(
            content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
            metadata={"result": payload},
        )
