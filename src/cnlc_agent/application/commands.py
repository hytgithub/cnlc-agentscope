"""任务级命令及受控查询投影；专业执行依赖仍由现有任务服务决定。"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal

from pydantic import Field

from cnlc_agent.application.service import InterpretationTaskService
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import DataError
from cnlc_agent.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    Execution,
    ExecutionStatus,
    PlanningReason,
)
from cnlc_agent.domain.inputs import InterpretationInputVersion
from cnlc_agent.domain.models import Contract, JsonObject, TaskRequest
from cnlc_agent.domain.override import InterpretationOverride


class GetStatusCommand(Contract):
    """只以持久任务标识查询状态，不接受模型提供的井号或状态。"""

    task_id: str = Field(min_length=1)


class ModifyInterpretationCommand(GetStatusCommand):
    """复用唯一 Override 契约，禁止模型指定执行起点。"""

    changes: InterpretationOverride


class FullRerunCommand(GetStatusCommand):
    """全量重跑保留任务当前有效参数。"""


class GetReportCommand(GetStatusCommand):
    """历史选择由应用层解析，显式执行标识也必须核对归属。"""

    selector: Literal["CURRENT", "PREVIOUS", "LATEST_SUCCESSFUL"] = "CURRENT"
    execution_id: str | None = Field(default=None, min_length=1)


class TaskCommandResult(Contract):
    """面向交互层的事实投影，不包含完整 State、曲线或 ToolRun 快照。"""

    command: Literal["START", "MODIFY", "FULL_RERUN", "STATUS", "GET_REPORT"]
    task_id: str
    well_id: str
    execution_id: str
    current_execution_id: str | None
    execution_sequence: int
    execution_status: ExecutionStatus
    workflow_status: StepStatus
    current_step: StepId | None
    completed_steps: list[StepId]
    reused_steps: list[StepId]
    effective_override: InterpretationOverride
    input_version_id: str | None
    start_step: StepId | None
    source_execution_id: str | None
    planning_reason: PlanningReason
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    report_ready: bool
    tool_run_summary: JsonObject
    summary: str
    report_markdown: str | None = None


class TaskCommands:
    """任务命令应用边界：查询真实仓库，修改只委托 rerun_planned。"""

    def __init__(self, service: InterpretationTaskService) -> None:
        self.service = service

    async def request(self, task_id: str) -> TaskRequest:
        """由仓库取得井标识，避免模型组合错误的 task/well。"""

        task = await self.service.repository.get_task(task_id)
        if task is None:
            raise DataError("TASK_NOT_FOUND", "请先上传井资料或开始一次解释任务")
        return TaskRequest(task_id=task_id, well_id=task.well_id)

    async def modify(
        self, command: ModifyInterpretationCommand,
        materialize: Callable[[InterpretationInputVersion], Awaitable[None]],
    ) -> TaskCommandResult:
        """至少提供一个参数；是否实际变化继续由现有 Resolver 校验。"""

        if not command.changes.has_changes():
            raise DataError("EMPTY_OVERRIDE", "请明确要修改的参数名称和值")
        state, _ = await self.service.rerun_planned(
            await self.request(command.task_id), changes=command.changes, materialize=materialize
        )
        return await self.project(command.task_id, state.workflow_execution_id, "MODIFY", True)

    async def prepare_modify(
        self, command: ModifyInterpretationCommand,
    ) -> TaskCommandResult:
        """生成计划并只创建 QUEUED Execution，供后台调度器执行。"""

        if not command.changes.has_changes():
            raise DataError("EMPTY_OVERRIDE", "请明确要修改的参数名称和值")
        request = await self.request(command.task_id)
        plan = await self.service.plan_rerun(request, changes=command.changes)
        execution = await self.service.prepare_rerun_plan(request, plan)
        return await self.project(command.task_id, execution.execution_id, "MODIFY")

    async def full_rerun(
        self, command: FullRerunCommand,
        materialize: Callable[[InterpretationInputVersion], Awaitable[None]],
    ) -> TaskCommandResult:
        """不传空 Override；已有服务负责继承当前配置。"""

        state, _ = await self.service.rerun_planned(
            await self.request(command.task_id), force_full_rerun=True, materialize=materialize
        )
        return await self.project(command.task_id, state.workflow_execution_id, "FULL_RERUN", True)

    async def prepare_full_rerun(
        self, command: FullRerunCommand,
    ) -> TaskCommandResult:
        """只提交全量计划；当前有效参数由 Resolver 继承。"""

        request = await self.request(command.task_id)
        plan = await self.service.plan_rerun(request, force_full_rerun=True)
        execution = await self.service.prepare_rerun_plan(request, plan)
        return await self.project(command.task_id, execution.execution_id, "FULL_RERUN")

    async def status(self, command: GetStatusCommand) -> TaskCommandResult:
        """仅返回 Execution 的真实持久状态，不从聊天上下文推断。"""

        await self.request(command.task_id)
        task = await self.service.repository.get_task(command.task_id)
        assert task is not None
        if task.current_execution_id is None:
            raise DataError("EXECUTION_NOT_FOUND", "任务尚无执行版本")
        return await self.project(command.task_id, task.current_execution_id, "STATUS")

    async def report(self, command: GetReportCommand) -> TaskCommandResult:
        """上一版按当前版本之前的最大序号确定，不猜测 execution_id。"""

        await self.request(command.task_id)
        task = await self.service.repository.get_task(command.task_id)
        assert task is not None
        selected = command.execution_id
        if selected is None:
            selected = task.current_execution_id
            if command.selector == "LATEST_SUCCESSFUL":
                selected = task.latest_successful_execution_id
            elif command.selector == "PREVIOUS":
                executions = await self.service.repository.list_executions(command.task_id)
                current = next((e for e in executions if e.execution_id == selected), None)
                previous = [e for e in executions if current and e.sequence < current.sequence]
                selected = (
                    max(previous, key=lambda e: e.sequence).execution_id if previous else None
                )
        if selected is None:
            raise DataError("REPORT_NOT_FOUND", "没有符合条件的历史报告")
        return await self.project(command.task_id, selected, "GET_REPORT", True)

    async def project(
        self, task_id: str, execution_id: str,
        command: Literal["START", "MODIFY", "FULL_RERUN", "STATUS", "GET_REPORT"],
        include_report: bool = False,
    ) -> TaskCommandResult:
        """只挑选可信的标识、步骤与统计值，报告来自 Execution.markdown。"""

        execution: Execution | None = await self.service.repository.get_execution(execution_id)
        if execution is None or execution.task_id != task_id:
            raise DataError("EXECUTION_NOT_FOUND", "指定执行不属于当前任务")
        task = await self.service.repository.get_task(task_id)
        assert task is not None
        runs = await self.service.list_tool_runs(task_id, execution_id)
        state = execution.state_snapshot
        return TaskCommandResult(
            command=command, task_id=task_id, well_id=task.well_id,
            execution_id=execution_id, current_execution_id=task.current_execution_id,
            execution_sequence=execution.sequence,
            execution_status=execution.status, workflow_status=state.status,
            current_step=state.current_step, completed_steps=state.completed_steps,
            reused_steps=[item.step_id for item in state.reused_steps],
            effective_override=execution.override_snapshot,
            input_version_id=execution.input_version_id,
            start_step=execution.start_step,
            source_execution_id=execution.source_execution_id,
            planning_reason=execution.planning_reason,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            error_code=execution.error_code,
            report_ready=(
                execution.status in TERMINAL_EXECUTION_STATUSES and bool(execution.markdown)
            ),
            tool_run_summary={
                "count": len(runs), "last_tool": runs[-1].tool_code if runs else None,
                "failed_count": sum(run.status == "FAILED" for run in runs),
            },
            summary=(
                f"第 {execution.sequence} 版执行状态：{execution.status.value}；"
                f"Workflow 状态：{state.status.value}。"
            ),
            report_markdown=(
                await self.service.get_execution_report(task_id, execution_id)
                if include_report else None
            ),
        )
