import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from cnlc_agent.agents.main_agent import MainAgent
from cnlc_agent.application.planning import DependencyResolver, ExecutionPlan
from cnlc_agent.application.ports import TaskRepository, Telemetry
from cnlc_agent.application.state_reuse import StateReuseAssembler, planned_start_step
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import DataError, InfrastructureError
from cnlc_agent.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    Execution,
    ExecutionStatus,
    execution_status_from_state,
)
from cnlc_agent.domain.inputs import InterpretationInputVersion
from cnlc_agent.domain.models import ErrorDetail, MockFixture, TaskRequest, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState, StateChange
from cnlc_agent.domain.tool_run import ToolRun
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

    async def get_execution_report(self, task_id: str, execution_id: str) -> str:
        """按所属任务读取历史 Execution 报告，拒绝跨任务访问和未完成版本。"""

        execution = await self.repository.get_execution(execution_id)
        if execution is None or execution.task_id != task_id:
            raise InfrastructureError("EXECUTION_NOT_FOUND", "指定执行不属于当前任务")
        if execution.status not in TERMINAL_EXECUTION_STATUSES:
            raise DataError("REPORT_NOT_READY", "执行报告尚未完成")
        markdown = await self.repository.get_execution_report(execution_id)
        if not markdown:
            raise DataError("REPORT_NOT_READY", "执行报告尚未完成")
        return markdown

    async def list_tool_runs(self, task_id: str, execution_id: str) -> list[ToolRun]:
        """任务归属在应用层核对，业务查询不依赖聊天上下文。"""

        execution = await self.repository.get_execution(execution_id)
        if execution is None or execution.task_id != task_id:
            raise InfrastructureError("EXECUTION_NOT_FOUND", "指定执行不属于当前任务")
        return await self.repository.list_tool_runs(execution_id)

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
        return await self.execute_prepared(
            state.workflow_execution_id,
            worker_id=f"sync:{uuid4().hex}",
        )

    async def prepare_initial_with_input(
        self,
        request: TaskRequest,
        fixture: MockFixture,
    ) -> Execution:
        """持久化 Task、InputVersion 与 QUEUED Execution，不在提交请求中运行业务流程。"""

        if fixture.well.well_id != request.well_id:
            raise DataError("TASK_WELL_MISMATCH", "上传井号与任务井号不一致")
        state = InterpretationState(task=request, mode=self.mode)
        with self.telemetry.span(
            "task.create",
            {"task_id": request.task_id, "trace_id": state.trace_id},
        ):
            await self.repository.create_task(state)
            version = await self.repository.create_input_version(request.task_id, fixture)
            state.input_version_id = version.input_version_id
            return await self.repository.create_execution(
                state,
                "INITIAL",
                input_version_id=version.input_version_id,
                start_step=StepId.W01,
                source_execution_id=None,
                planning_reason="INITIAL",
                expected_current_execution_id=None,
            )

    async def run_with_input(
        self,
        request: TaskRequest,
        fixture: MockFixture,
        materialize: Callable[[InterpretationInputVersion], Awaitable[None]],
    ) -> tuple[InterpretationState, str]:
        """保存上传的规范化输入，再由 Demo 适配器物化它并运行完整流程。"""

        execution = await self.prepare_initial_with_input(request, fixture)
        return await self.execute_prepared(
            execution.execution_id,
            worker_id=f"sync:{uuid4().hex}",
            materialize=materialize,
        )

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
        state = InterpretationState(
            task=request,
            mode=self.mode,
            input_version_id=selected_input_id,
            effective_override=(override or InterpretationOverride()).model_copy(deep=True),
        )
        execution = await self.repository.create_execution(
            state,
            "RERUN",
            input_version_id=selected_input_id,
            override_snapshot=override,
            start_step=StepId.W01,
            source_execution_id=task.latest_successful_execution_id,
            planning_reason="FULL_RERUN",
            expected_current_execution_id=task.current_execution_id,
        )
        return await self.execute_prepared(
            execution.execution_id,
            worker_id=f"sync:{uuid4().hex}",
            materialize=materialize,
        )

    async def plan_rerun(
        self,
        request: TaskRequest,
        *,
        input_version_id: str | None = None,
        changes: InterpretationOverride | None = None,
        force_full_rerun: bool = False,
    ) -> ExecutionPlan:
        """只读取历史事实并生成计划；不创建 Execution 或运行 Workflow。"""

        task = await self.repository.get_task(request.task_id)
        if task is None:
            raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
        if task.well_id != request.well_id:
            raise InfrastructureError("TASK_WELL_MISMATCH", "任务井号不一致")
        current = (
            await self.repository.get_execution(task.current_execution_id)
            if task.current_execution_id is not None
            else None
        )
        source = (
            await self.repository.get_execution(task.latest_successful_execution_id)
            if task.latest_successful_execution_id is not None
            else None
        )
        selected_id = (
            input_version_id
            or task.current_input_version_id
            or (current.input_version_id if current is not None else None)
            or (source.input_version_id if source is not None else None)
        )
        selected = (
            await self.repository.get_input_version(selected_id)
            if selected_id is not None
            else None
        )
        if selected_id is not None and selected is None:
            raise InfrastructureError("INPUT_VERSION_NOT_FOUND", "选中输入版本不存在")
        if selected is not None and selected.task_id != request.task_id:
            raise InfrastructureError("INPUT_VERSION_TASK_MISMATCH", "输入版本不属于此任务")
        source_input = (
            await self.repository.get_input_version(source.input_version_id)
            if source is not None and source.input_version_id is not None
            else None
        )
        return DependencyResolver().plan(
            task_id=request.task_id,
            selected_input=selected,
            source_execution=source,
            source_input=source_input,
            current_execution=current,
            requested_changes=changes,
            force_full_rerun=force_full_rerun,
        )

    async def rerun_planned(
        self,
        request: TaskRequest,
        *,
        input_version_id: str | None = None,
        changes: InterpretationOverride | None = None,
        force_full_rerun: bool = False,
        materialize: Callable[[InterpretationInputVersion], Awaitable[None]] | None = None,
    ) -> tuple[InterpretationState, str]:
        """生成确定性计划并执行局部重跑；旧 rerun 继续作为全流程兼容入口。"""

        plan = await self.plan_rerun(
            request,
            input_version_id=input_version_id,
            changes=changes,
            force_full_rerun=force_full_rerun,
        )
        return await self.execute_rerun_plan(request, plan, materialize=materialize)

    async def prepare_rerun_plan(
        self,
        request: TaskRequest,
        plan: ExecutionPlan,
    ) -> Execution:
        """重新读取并校验计划来源，原子创建可供 Worker claim 的执行记录。"""

        plan = ExecutionPlan.model_validate(plan.model_dump(mode="python"))
        task = await self.repository.get_task(request.task_id)
        if task is None:
            raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
        if task.well_id != request.well_id:
            raise InfrastructureError("TASK_WELL_MISMATCH", "任务井号不一致")
        source = (
            await self.repository.get_execution(plan.source_execution_id)
            if plan.source_execution_id is not None
            else None
        )
        selected = (
            await self.repository.get_input_version(plan.selected_input_version_id)
            if plan.selected_input_version_id is not None
            else None
        )
        source_input = (
            await self.repository.get_input_version(plan.source_input_version_id)
            if plan.source_input_version_id is not None
            else None
        )
        state = StateReuseAssembler().assemble(
            plan,
            source,
            request,
            selected_input=selected,
            source_input=source_input,
            mode=self.mode,
        )
        start_step = planned_start_step(plan)
        return await self.repository.create_execution(
            state,
            "RERUN",
            input_version_id=plan.selected_input_version_id,
            override_snapshot=plan.effective_override,
            start_step=start_step,
            source_execution_id=plan.source_execution_id,
            planning_reason=plan.planning_reason,
            expected_current_execution_id=plan.expected_current_execution_id,
        )

    async def execute_rerun_plan(
        self,
        request: TaskRequest,
        plan: ExecutionPlan,
        *,
        materialize: Callable[[InterpretationInputVersion], Awaitable[None]] | None = None,
    ) -> tuple[InterpretationState, str]:
        """同步兼容入口：先准备 Execution，再由同一 Worker 生命周期执行。"""

        execution = await self.prepare_rerun_plan(request, plan)
        return await self.execute_prepared(
            execution.execution_id,
            worker_id=f"sync:{uuid4().hex}",
            materialize=materialize,
        )

    async def execute_prepared(
        self,
        execution_id: str,
        *,
        worker_id: str,
        materialize: Callable[[InterpretationInputVersion], Awaitable[None]] | None = None,
        lease_seconds: float = 90.0,
    ) -> tuple[InterpretationState, str]:
        """claim 已准备执行，续租并写入唯一终态；提交请求不调用此方法等待结果。"""

        now = utc_now()
        claimed = await self.repository.claim_execution(
            execution_id,
            worker_id,
            now + timedelta(seconds=lease_seconds),
        )
        if not claimed:
            raise InfrastructureError("EXECUTION_NOT_CLAIMED", "执行已被其他 Worker 领取")
        execution = await self.repository.get_execution(execution_id)
        if execution is None:
            raise InfrastructureError("EXECUTION_NOT_FOUND", "执行记录不存在")
        request = execution.state_snapshot.task
        lease_lost = asyncio.Event()

        async def heartbeat() -> None:
            """执行期间按短于租约的周期续租，失败后阻止当前 Worker 提交成功终态。"""

            interval = max(1.0, lease_seconds / 3)
            while True:
                await asyncio.sleep(interval)
                heartbeat_at = utc_now()
                try:
                    renewed = await self.repository.renew_execution_lease(
                        execution_id,
                        worker_id,
                        heartbeat_at + timedelta(seconds=lease_seconds),
                    )
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "Execution lease renewal failed: type=%s", type(exc).__name__
                    )
                    lease_lost.set()
                    return
                if not renewed:
                    lease_lost.set()
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            if execution.input_version_id is not None and execution.start_step is not None:
                version = await self.repository.get_input_version(execution.input_version_id)
                if version is None:
                    raise InfrastructureError("INPUT_VERSION_NOT_FOUND", "输入版本不存在")
                if materialize is None:
                    raise InfrastructureError(
                        "INPUT_MATERIALIZER_REQUIRED", "输入执行需要受控物化适配器"
                    )
                await materialize(version)
            state, markdown = await self._run_execution(
                request,
                execution.state_snapshot.model_copy(deep=True),
                start_step=execution.start_step,
            )
            if lease_lost.is_set():
                raise InfrastructureError("EXECUTION_LEASE_LOST", "执行租约已失效")
            error_code = state.errors[-1].code if state.errors else None
            await self.repository.finish_execution(
                execution_id,
                worker_id,
                execution_status_from_state(state.status),
                error_code=error_code,
            )
            return state, markdown
        except asyncio.CancelledError:
            with suppress(InfrastructureError):
                await self.repository.finish_execution(
                    execution_id,
                    worker_id,
                    ExecutionStatus.FAILED,
                    error_code="BACKGROUND_EXECUTION_CANCELLED",
                )
            raise
        except Exception:
            with suppress(InfrastructureError):
                await self.repository.finish_execution(
                    execution_id,
                    worker_id,
                    ExecutionStatus.FAILED,
                    error_code="BACKGROUND_EXECUTION_FAILED",
                )
            raise
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _run_execution(
        self,
        request: TaskRequest,
        state: InterpretationState,
        *,
        start_step: StepId | None = StepId.W01,
    ) -> tuple[InterpretationState, str]:
        """共用原有业务主链；状态与报告由仓库按 Execution ID 保存。"""

        try:
            if start_step is None:
                # 仅重生成报告时，状态来自已校验的完整复用链，不进入业务节点。
                state.status = state.completed_status()
                state.current_step = None
                state.updated_at = utc_now()
            else:
                state = await self.main_agent.run(request, state=state, start_step=start_step)
                if state.status in {StepStatus.SUCCESS, StepStatus.WARNING}:
                    # Workflow 保留最后执行节点供其自身诊断；对已终结的任务清空运行指针。
                    state.current_step = None
                    state.updated_at = utc_now()
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
