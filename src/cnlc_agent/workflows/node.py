from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.models import MissingData
from cnlc_agent.domain.state import InterpretationState, StepOutcome


@dataclass(frozen=True)
class WorkflowNode:
    step_id: StepId
    handler: Callable[[InterpretationState], Awaitable[StepOutcome]]
    required_fields: tuple[str, ...] = ()

    def precondition(self, state: InterpretationState) -> list[MissingData]:
        return [
            MissingData(field=name, importance="Required", affected_step=self.step_id)
            for name in self.required_fields
            if getattr(state, name) is None
        ]

    async def execute(self, state: InterpretationState) -> StepOutcome:
        return await self.handler(state)
