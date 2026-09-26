"""把持久化 Execution 投影为浏览器可安全读取的只读视图。"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from cnlc_agent.application.planning import STAGE_STEPS, ExecutionStage, PlanAction
from cnlc_agent.application.ports import TaskRepository
from cnlc_agent.demo.presentation import present_steps
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    Execution,
    ExecutionStatus,
    InterpretationTask,
    PlanningReason,
)
from cnlc_agent.domain.models import Contract
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.tool_run import ToolExecutionMode, ToolRunStatus

StepDisplayStatus = Literal[
    "REUSED", "PENDING", "RUNNING", "SUCCESS", "WARNING", "FAILED", "BLOCKED",
    "REVIEW_REQUIRED", "SKIPPED",
]


class InterpretationStageView(Contract):
    """四阶段只展示本 Execution 已确定的 RUN/REUSE 计划事实。"""

    stage: ExecutionStage
    name: str
    action: PlanAction


class InterpretationStepView(Contract):
    """在既有安全步骤摘要上增加仅用于 UI 的 REUSED 状态。"""

    id: StepId
    name: str
    status: StepStatus
    display_status: StepDisplayStatus
    source: str
    input_summary: dict[str, object] = Field(default_factory=dict)
    output_summary: dict[str, object] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class InterpretationToolRunView(Contract):
    """专业工具调用白名单视图；不传递任意输入、输出快照或异常文本。"""

    tool_run_id: str
    step_id: StepId
    tool_code: str
    status: ToolRunStatus
    execution_mode: ToolExecutionMode
    source: str
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None


class InterpretationExecutionSummary(Contract):
    """执行历史列表所需的稳定字段。"""

    execution_id: str
    sequence: int
    execution_status: ExecutionStatus
    workflow_status: StepStatus
    planning_reason: PlanningReason
    start_step: StepId | None
    source_execution_id: str | None
    effective_override: InterpretationOverride
    input_version_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    report_ready: bool


class InterpretationExecutionView(InterpretationExecutionSummary):
    """单个历史 Execution 的完整安全展示视图。"""

    current_step: StepId | None
    completed_steps: list[StepId]
    reused_steps: list[StepId]
    stages: list[InterpretationStageView]
    steps: list[InterpretationStepView]
    tool_runs: list[InterpretationToolRunView]
    report_markdown: str | None


class InterpretationTaskView(Contract):
    """持续任务及其倒序执行历史，供前端选择具体版本。"""

    task_id: str
    well_id: str
    current_execution_id: str | None
    latest_successful_execution_id: str | None
    current_input_version_id: str | None
    executions: list[InterpretationExecutionSummary]
    current_execution: InterpretationExecutionView | None


_STAGE_NAMES = {
    ExecutionStage.DATA_DECODE: "数据解编",
    ExecutionStage.PREPROCESS: "数据预处理",
    ExecutionStage.INTERPRET: "智能处理",
    ExecutionStage.REPORT: "报告生成",
}


def _report_ready(execution: Execution) -> bool:
    return execution.status in TERMINAL_EXECUTION_STATUSES and bool(execution.markdown)


def present_execution_summary(execution: Execution) -> InterpretationExecutionSummary:
    """仅从 Execution 快照构造列表项，不查询模型或外部服务。"""

    return InterpretationExecutionSummary(
        execution_id=execution.execution_id,
        sequence=execution.sequence,
        execution_status=execution.status,
        workflow_status=execution.state_snapshot.status,
        planning_reason=execution.planning_reason,
        start_step=execution.start_step,
        source_execution_id=execution.source_execution_id,
        effective_override=execution.override_snapshot,
        input_version_id=execution.input_version_id,
        created_at=execution.created_at,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        error_code=execution.error_code,
        report_ready=_report_ready(execution),
    )


def _present_stages(reused_steps: set[StepId]) -> list[InterpretationStageView]:
    """聚合真实复用步骤；REPORT 按现有执行设计始终运行。"""

    views: list[InterpretationStageView] = []
    for stage in ExecutionStage:
        steps = STAGE_STEPS[stage]
        action = (
            PlanAction.REUSE
            if stage is not ExecutionStage.REPORT
            and bool(steps)
            and all(step in reused_steps for step in steps)
            else PlanAction.RUN
        )
        views.append(InterpretationStageView(stage=stage, name=_STAGE_NAMES[stage], action=action))
    return views


async def present_execution_view(
    repository: TaskRepository, execution: Execution
) -> InterpretationExecutionView:
    """读取一个 Execution 的步骤、ToolRun 与本版本报告，不混入当前版本数据。"""

    reused = {item.step_id for item in execution.state_snapshot.reused_steps}
    steps = [
        InterpretationStepView(
            **step.model_dump(mode="python"),
            display_status="REUSED" if step.id in reused else step.status.value,
        )
        for step in present_steps(execution.state_snapshot)
    ]
    runs = await repository.list_tool_runs(execution.execution_id)
    tool_runs = [
        InterpretationToolRunView(
            tool_run_id=run.tool_run_id,
            step_id=run.step_id,
            tool_code=run.tool_code,
            status=run.status,
            execution_mode=run.execution_mode,
            source=run.source,
            started_at=run.started_at,
            finished_at=run.finished_at,
            error_code=run.error_code,
        )
        for run in runs
    ]
    report = (
        await repository.get_execution_report(execution.execution_id)
        if _report_ready(execution)
        else None
    )
    return InterpretationExecutionView(
        **present_execution_summary(execution).model_dump(mode="python"),
        current_step=execution.state_snapshot.current_step,
        completed_steps=list(execution.state_snapshot.completed_steps),
        reused_steps=[item.step_id for item in execution.state_snapshot.reused_steps],
        stages=_present_stages(reused),
        steps=steps,
        tool_runs=tool_runs,
        report_markdown=report,
    )


async def present_task_view(
    repository: TaskRepository, task: InterpretationTask
) -> InterpretationTaskView:
    """按序号倒序返回历史，当前详情仍由任务指针精确选择。"""

    executions = sorted(
        await repository.list_executions(task.task_id), key=lambda item: item.sequence, reverse=True
    )
    current = next(
        (item for item in executions if item.execution_id == task.current_execution_id), None
    )
    return InterpretationTaskView(
        task_id=task.task_id,
        well_id=task.well_id,
        current_execution_id=task.current_execution_id,
        latest_successful_execution_id=task.latest_successful_execution_id,
        current_input_version_id=task.current_input_version_id,
        executions=[present_execution_summary(item) for item in executions],
        current_execution=(
            await present_execution_view(repository, current) if current is not None else None
        ),
    )
