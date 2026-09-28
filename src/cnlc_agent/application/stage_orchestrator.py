"""四阶段确认模式的应用编排；不复制 W01～W10 节点实现。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timedelta

from cnlc_agent.application.ports import TaskRepository
from cnlc_agent.application.service import InterpretationTaskService
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import InfrastructureError, WorkflowError
from cnlc_agent.domain.execution import (
    Execution,
    ExecutionRunMode,
    ExecutionStatus,
    execution_status_from_state,
)
from cnlc_agent.domain.inputs import InterpretationInputVersion
from cnlc_agent.domain.models import Contract, utc_now
from cnlc_agent.domain.stage_runtime import begin_stage, finish_stage
from cnlc_agent.domain.stages import (
    STAGE_ORDER,
    InterpretationStage,
    StageRun,
    StageRunStatus,
    StageValidity,
)

START_STAGE = {
    StepId.W01: InterpretationStage.DECODE,
    StepId.W02: InterpretationStage.PREPROCESS,
    StepId.W04: InterpretationStage.INTERPRET,
    None: InterpretationStage.REPORT,
}


class StageProgress(Contract):
    """面向调用方的最小阶段进度投影，仅包含稳定标识和状态。"""

    task_id: str
    well_id: str
    execution_id: str
    execution_sequence: int
    run_mode: ExecutionRunMode
    execution_status: ExecutionStatus
    current_stage: InterpretationStage | None = None
    current_stage_run_id: str | None = None
    stage_status: StageRunStatus | None = None
    stage_validity: StageValidity | None = None
    confirmed_stages: list[InterpretationStage]
    waiting_confirmation_stage: InterpretationStage | None = None
    next_stage: InterpretationStage | None = None
    started_at: datetime | None = None
    updated_at: datetime
    summary: str = ""
    warnings: list[str]
    candidate_report: str | None = None


class StageOrchestrator:
    """领取同一 Execution 的下一阶段，并在阶段边界暂停或确认。"""

    def __init__(self, service: InterpretationTaskService) -> None:
        self.service = service
        self.repository: TaskRepository = service.repository

    @staticmethod
    def _execution_runs(execution: Execution) -> list[StageRun]:
        """历史或复用 StageRun 不参与当前 Execution 的推进判断。"""

        return [
            run for run in execution.state_snapshot.stage_runs
            if run.execution_id == execution.execution_id
        ]

    @classmethod
    def _next_stage(cls, execution: Execution) -> InterpretationStage | None:
        """从持久化起点和当前 Execution 的运行记录确定唯一下一阶段。"""

        first = START_STAGE[execution.start_step]
        allowed = STAGE_ORDER[STAGE_ORDER.index(first) :]
        by_stage = {run.stage: run for run in cls._execution_runs(execution)}
        for stage in allowed:
            run = by_stage.get(stage)
            if run is None:
                return stage
            if run.status == StageRunStatus.CONFIRMED and run.validity == StageValidity.CURRENT:
                continue
            if run.status == StageRunStatus.WAITING_CONFIRM:
                return None
            raise WorkflowError("STAGE_DEPENDENCY_UNAVAILABLE", "当前阶段记录不能继续推进")
        return None

    async def get_progress(self, task_id: str, execution_id: str) -> StageProgress:
        """按显式 Task/Execution 标识读取进度，不依赖会话活动上下文。"""

        task = await self.repository.get_task(task_id)
        execution = await self.repository.get_execution(execution_id)
        if task is None or execution is None or execution.task_id != task_id:
            raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在或不属于任务")
        runs = self._execution_runs(execution)
        waiting = next(
            (
                run for run in runs
                if run.status == StageRunStatus.WAITING_CONFIRM
                and run.validity == StageValidity.CURRENT
            ),
            None,
        )
        current = waiting or (runs[-1] if runs else None)
        return StageProgress(
            task_id=task_id,
            well_id=task.well_id,
            execution_id=execution_id,
            execution_sequence=execution.sequence,
            run_mode=execution.run_mode,
            execution_status=execution.status,
            current_stage=current.stage if current else None,
            current_stage_run_id=current.id if current else None,
            stage_status=current.status if current else None,
            stage_validity=current.validity if current else None,
            confirmed_stages=[
                run.stage for run in runs
                if run.status == StageRunStatus.CONFIRMED
                and run.validity == StageValidity.CURRENT
            ],
            waiting_confirmation_stage=waiting.stage if waiting else None,
            next_stage=self._next_stage(execution),
            started_at=execution.started_at,
            updated_at=execution.updated_at,
            summary=current.summary if current else "",
            warnings=list(current.warnings) if current else [],
            candidate_report=(
                execution.markdown
                if waiting is not None and waiting.stage == InterpretationStage.REPORT
                else None
            ),
        )

    async def confirm_stage(
        self,
        task_id: str,
        execution_id: str,
        stage: InterpretationStage,
        expected_stage_run_id: str,
        *,
        actor: str,
    ) -> Execution:
        """确认候选；非最终阶段重排同一 Execution，报告确认后形成终态。"""

        if stage == InterpretationStage.REPORT:
            return await self.repository.confirm_final_stage_and_finish(
                task_id, execution_id, expected_stage_run_id, actor
            )
        return await self.repository.confirm_stage_and_requeue(
            task_id, execution_id, stage, expected_stage_run_id, actor
        )

    async def execute_next_stage(
        self,
        execution_id: str,
        *,
        worker_id: str,
        materialize: Callable[[InterpretationInputVersion], Awaitable[None]] | None = None,
        lease_seconds: float = 90.0,
    ) -> Execution:
        """运行恰好一个阶段，成功后原子暂停并释放 Worker lease。"""

        before_claim = await self.repository.get_execution(execution_id)
        if before_claim is None:
            raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
        if before_claim.run_mode != ExecutionRunMode.STAGED_CONFIRMATION:
            raise InfrastructureError("INVALID_EXECUTION_MODE", "执行不是分阶段确认模式")
        stage = self._next_stage(before_claim)
        if stage is None:
            raise WorkflowError("STAGE_DEPENDENCY_UNAVAILABLE", "没有可执行的下一阶段")
        claimed = await self.repository.claim_execution(
            execution_id, worker_id, utc_now() + timedelta(seconds=lease_seconds)
        )
        if not claimed:
            raise InfrastructureError("EXECUTION_NOT_CLAIMED", "执行已被其他 Worker 领取")
        execution = await self.repository.get_execution(execution_id)
        assert execution is not None
        lease_lost = asyncio.Event()

        async def heartbeat() -> None:
            """阶段执行期间续租；暂停提交后由 finally 立即取消。"""

            interval = max(1.0, lease_seconds / 3)
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await self.repository.renew_execution_lease(
                        execution_id,
                        worker_id,
                        utc_now() + timedelta(seconds=lease_seconds),
                    )
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "Stage lease renewal failed: type=%s", type(exc).__name__
                    )
                    lease_lost.set()
                    return
                if not renewed:
                    lease_lost.set()
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            if execution.input_version_id is not None:
                version = await self.repository.get_input_version(execution.input_version_id)
                if version is None:
                    raise InfrastructureError("INPUT_VERSION_NOT_FOUND", "输入版本不存在")
                if materialize is None:
                    raise InfrastructureError(
                        "INPUT_MATERIALIZER_REQUIRED", "输入执行需要受控物化适配器"
                    )
                await materialize(version)
            state = execution.state_snapshot.model_copy(deep=True)
            markdown = execution.markdown
            if stage == InterpretationStage.REPORT:
                state.status = state.completed_status()
                state.current_step = None
                begin_stage(
                    state,
                    stage,
                    report_config_ref=(
                        f"report-config:style:{self.service.reports.generator.style.value}"
                    ),
                )
                markdown = self.service.reports.to_markdown(state)
                finish_stage(state, stage, auto_confirm=False)
            else:
                state = await self.service.main_agent.workflow.run_stage(state, stage)
            if lease_lost.is_set():
                raise InfrastructureError("EXECUTION_LEASE_LOST", "执行租约已失效")
            if state.status not in {StepStatus.SUCCESS, StepStatus.WARNING}:
                markdown = self.service.reports.to_markdown(state)
                await self.repository.save(state, markdown)
                return await self.repository.finish_execution(
                    execution_id,
                    worker_id,
                    execution_status_from_state(state.status),
                    error_code=state.errors[-1].code if state.errors else None,
                )
            return await self.repository.pause_execution_for_confirmation(
                execution_id, worker_id, state, markdown
            )
        except asyncio.CancelledError:
            await self.service._record_stage_failure(
                execution_id, worker_id, "BACKGROUND_EXECUTION_CANCELLED"
            )
            with suppress(InfrastructureError):
                await self.repository.finish_execution(
                    execution_id,
                    worker_id,
                    ExecutionStatus.FAILED,
                    error_code="BACKGROUND_EXECUTION_CANCELLED",
                )
            raise
        except Exception:
            await self.service._record_stage_failure(
                execution_id, worker_id, "BACKGROUND_EXECUTION_FAILED"
            )
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
            with suppress(asyncio.CancelledError):
                await heartbeat_task
