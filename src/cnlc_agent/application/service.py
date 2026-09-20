from cnlc_agent.agents.main_agent import MainAgent
from cnlc_agent.application.ports import TaskRepository, Telemetry
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.reports.assembler import ReportAssembler


class InterpretationTaskService:
    def __init__(
        self,
        main_agent: MainAgent,
        repository: TaskRepository,
        reports: ReportAssembler,
        telemetry: Telemetry,
    ) -> None:
        self.main_agent = main_agent
        self.repository = repository
        self.reports = reports
        self.telemetry = telemetry

    async def run(self, request: TaskRequest) -> tuple[InterpretationState, str]:
        state = await self.main_agent.run(request)
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
