from collections.abc import Awaitable, Callable
from typing import Literal

from cnlc_agent.agents.main_agent import MainAgent
from cnlc_agent.application.ports import TaskRepository, Telemetry
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.models import ErrorDetail, TaskRequest, utc_now
from cnlc_agent.domain.state import InterpretationState, StateChange
from cnlc_agent.reports.assembler import ReportAssembler


class InterpretationTaskService:
    """单井解释应用服务，负责任务生命周期、异常落库与报告生成。"""

    def __init__(
        self,
        main_agent: MainAgent,
        repository: TaskRepository,
        reports: ReportAssembler,
        telemetry: Telemetry,
        mode: Literal["mock", "demo"] = "mock",
        close_callbacks: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> None:
        self.main_agent = main_agent
        self.repository = repository
        self.reports = reports
        self.telemetry = telemetry
        self.mode = mode
        self.close_callbacks = close_callbacks or []

    async def run(self, request: TaskRequest) -> tuple[InterpretationState, str]:
        """创建持续任务及首个执行，保持现有入口返回契约。"""

        state = InterpretationState(task=request, mode=self.mode)
        with self.telemetry.span(
            "task.create",
            {
                "task_id": request.task_id,
                "trace_id": state.trace_id,
            },
        ):
            await self.repository.create(state)
        return await self._run_execution(request, state)

    async def rerun(self, request: TaskRequest) -> tuple[InterpretationState, str]:
        """在已有任务下创建新执行；Task 01 仍完整运行 W01～W10。"""

        task = await self.repository.get_task(request.task_id)
        if task is None:
            raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
        if task.well_id != request.well_id:
            raise InfrastructureError("TASK_WELL_MISMATCH", "任务井号不一致")
        state = InterpretationState(task=request, mode=self.mode)
        await self.repository.create_execution(state, "RERUN")
        return await self._run_execution(request, state)

    async def _run_execution(
        self, request: TaskRequest, state: InterpretationState
    ) -> tuple[InterpretationState, str]:
        """共用原有业务主链；状态与报告由仓库按 Execution ID 保存。"""

        try:
            state = await self.main_agent.run(request, state=state)
        except InfrastructureError as exc:
            # 持久化故障不能伪装成业务成功；尽量从长期存储恢复最近快照并形成诊断报告。
            self.telemetry.event(
                "persistence.error",
                {
                    "task_id": request.task_id,
                    "trace_id": state.trace_id,
                    "code": exc.code,
                },
            )
            execution = await self.repository.get_execution(state.workflow_execution_id)
            state = execution.state_snapshot if execution is not None else state
            previous_status = state.status
            state.status = StepStatus.FAILED
            state.updated_at = utc_now()
            error = ErrorDetail(code=exc.code, message=str(exc), step_id=state.current_step)
            state.errors.append(error)
            if state.current_step is not None:
                state.changes.append(
                    StateChange(
                        actor="InterpretationTaskService",
                        step_id=state.current_step,
                        reason=exc.code,
                        before={"status": previous_status.value},
                        after={
                            "status": state.status.value,
                            "error": error.model_dump(mode="json"),
                        },
                    )
                )
            if state.executions and state.executions[-1].status == StepStatus.RUNNING:
                # 同步修正最后一条执行记录，避免任务失败但步骤仍显示 RUNNING。
                state.executions[-1].status = StepStatus.FAILED
                state.executions[-1].ended_at = state.updated_at
                state.executions[-1].errors.append(error)
        with self.telemetry.span(
            "report",
            {
                "task_id": request.task_id,
                "trace_id": state.trace_id,
            },
        ):
            markdown = self.reports.to_markdown(state)
            await self.repository.save(state, markdown)
        return state, markdown

    async def close(self) -> None:
        """按注册顺序的逆序关闭模型客户端等应用资源。"""

        for close in reversed(self.close_callbacks):
            await close()
