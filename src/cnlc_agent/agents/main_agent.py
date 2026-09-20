from cnlc_agent.application.ports import Telemetry
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.workflows.interpretation_workflow import InterpretationWorkflow


class MainAgent:
    """Task-level skeleton: starts the fixed workflow and returns its state."""

    def __init__(self, workflow: InterpretationWorkflow, telemetry: Telemetry) -> None:
        self.workflow = workflow
        self.telemetry = telemetry

    async def run(self, request: TaskRequest) -> InterpretationState:
        state = InterpretationState(task=request)
        with self.telemetry.span(
            "agent",
            {
                "agent": "MainAgent",
                "task_id": request.task_id,
                "trace_id": state.trace_id,
            },
        ):
            return await self.workflow.run(state)
