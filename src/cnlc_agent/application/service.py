from collections.abc import Awaitable, Callable
from typing import Literal

from cnlc_agent.agents.main_agent import MainAgent
from cnlc_agent.application.ports import TaskRepository, Telemetry
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import DataError, InfrastructureError
from cnlc_agent.domain.inputs import InterpretationInputVersion
from cnlc_agent.domain.models import ErrorDetail, MockFixture, TaskRequest, utc_now
from cnlc_agent.domain.override import InterpretationOverride
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

    async def run_with_input(
        self,
        request: TaskRequest,
        fixture: MockFixture,
        materialize: Callable[[InterpretationInputVersion], Awaitable[None]],
    ) -> tuple[InterpretationState, str]:
        """保存上传的规范化输入，再由 Demo 适配器物化它并运行完整流程。"""

        if fixture.well.well_id != request.well_id:
            raise DataError("TASK_WELL_MISMATCH", "上传井号与任务井号不一致")
        state = InterpretationState(task=request, mode=self.mode)
        with self.telemetry.span(
            "task.create",
            {"task_id": request.task_id, "trace_id": state.trace_id},
        ):
            await self.repository.create_task(state)
            version = await self.repository.create_input_version(request.task_id, fixture)
            # 使用仓库读回的版本作为当前 W01 临时文件的唯一来源。
            saved = await self.repository.get_input_version(version.input_version_id)
            if saved is None:
                raise InfrastructureError("INPUT_VERSION_NOT_FOUND", "输入版本保存后无法读取")
            await materialize(saved)
            await self.repository.create_execution(
                state, "INITIAL", input_version_id=saved.input_version_id
            )
        return await self._run_execution(request, state)

    async def rerun(
        self,
        request: TaskRequest,
        *,
        input_version_id: str | None = None,
        override: InterpretationOverride | None = None,
        materialize: Callable[[InterpretationInputVersion], Awaitable[None]] | None = None,
    ) -> tuple[InterpretationState, str]:
        """创建绑定输入/Override 的新执行；本阶段仍完整运行 W01～W10。"""

        task = await self.repository.get_task(request.task_id)
        if task is None:
            raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
        if task.well_id != request.well_id:
            raise InfrastructureError("TASK_WELL_MISMATCH", "任务井号不一致")
        if override is not None and not override.has_changes():
            raise DataError("EMPTY_OVERRIDE", "参数修改命令至少需要一个变化")
        selected_input_id = input_version_id or task.current_input_version_id
        if selected_input_id is not None:
            selected_input = await self.repository.get_input_version(selected_input_id)
            if selected_input is None:
                raise InfrastructureError("INPUT_VERSION_NOT_FOUND", "输入版本不存在")
            if selected_input.task_id != request.task_id:
                raise InfrastructureError("INPUT_VERSION_TASK_MISMATCH", "输入版本不属于此任务")
            if materialize is None:
                raise InfrastructureError(
                    "INPUT_MATERIALIZER_REQUIRED", "上传输入重跑需要受控物化适配器"
                )
            await materialize(selected_input)
        state = InterpretationState(task=request, mode=self.mode)
        await self.repository.create_execution(
            state,
            "RERUN",
            input_version_id=selected_input_id,
            override_snapshot=override,
        )
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
