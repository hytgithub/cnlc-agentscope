from cnlc_agent.application.ports import Telemetry
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.workflows.interpretation_workflow import InterpretationWorkflow


class MainAgent:
    """任务级编排入口：启动固定 Workflow，并返回统一解释状态。"""

    def __init__(self, workflow: InterpretationWorkflow, telemetry: Telemetry) -> None:
        self.workflow = workflow
        self.telemetry = telemetry

    async def run(
        self, request: TaskRequest, *, state: InterpretationState | None = None
    ) -> InterpretationState:
        """使用已有状态或创建新状态，执行一次完整 W01-W10 流程。"""

        state = state if state is not None else InterpretationState(task=request)
        with self.telemetry.span(
            "agent",
            {
                "agent": "MainAgent",
                "task_id": request.task_id,
                "trace_id": state.trace_id,
            },
        ):
            return await self.workflow.run(state)
