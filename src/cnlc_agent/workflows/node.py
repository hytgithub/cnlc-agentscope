from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.models import MissingData
from cnlc_agent.domain.state import InterpretationState, StepOutcome


@dataclass(frozen=True)
class WorkflowNode:
    """不可变 Workflow 节点，绑定步骤、处理器和必需前置字段。"""

    step_id: StepId
    handler: Callable[[InterpretationState], Awaitable[StepOutcome]]
    required_fields: tuple[str, ...] = ()

    def precondition(self, state: InterpretationState) -> list[MissingData]:
        """把缺失前置结果转换为统一 Required 缺失项。"""

        return [
            MissingData(field=name, importance="Required", affected_step=self.step_id)
            for name in self.required_fields
            if getattr(state, name) is None
        ]

    async def execute(self, state: InterpretationState) -> StepOutcome:
        """执行节点处理器；状态提交仍由 InterpretationWorkflow 统一完成。"""

        return await self.handler(state)
