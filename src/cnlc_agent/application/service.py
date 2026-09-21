from collections.abc import Awaitable, Callable

from cnlc_agent.agents.main_agent import MainAgent
from cnlc_agent.application.ports import TaskRepository, Telemetry
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.models import ErrorDetail, TaskRequest, utc_now
from cnlc_agent.domain.state import InterpretationState, StateChange
from cnlc_agent.reports.assembler import ReportAssembler


class InterpretationTaskService:
    def __init__(
        self,
        main_agent: MainAgent,
        repository: TaskRepository,
        reports: ReportAssembler,
        telemetry: Telemetry,
        close_callbacks: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> None:
        self.main_agent = main_agent
        self.repository = repository
        self.reports = reports
        self.telemetry = telemetry
        self.close_callbacks = close_callbacks or []

    async def run(self, request: TaskRequest) -> tuple[InterpretationState, str]:
        state = InterpretationState(task=request)
        with self.telemetry.span(
            "task.create",
            {
                "task_id": request.task_id,
                "trace_id": state.trace_id,
            },
        ):
            await self.repository.create(state)
        try:
            state = await self.main_agent.run(request, state=state)
        except InfrastructureError as exc:
            self.telemetry.event(
                "persistence.error",
                {
                    "task_id": request.task_id,
                    "trace_id": state.trace_id,
                    "code": exc.code,
                },
            )
            state = await self.repository.get(request.task_id) or state
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
        for close in reversed(self.close_callbacks):
            await close()
