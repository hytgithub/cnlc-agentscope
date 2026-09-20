"""Sequential skeleton with auditable state changes and explicit terminal branches.

Automatic retry/rollback is intentionally deferred until its policy is designed.
Serious validation conflicts stop for review in this task.
"""

from pydantic import ValidationError as SchemaError

from cnlc_agent.application.ports import InterpretationStateStore, Telemetry
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.errors import ApplicationError, WorkflowError
from cnlc_agent.domain.models import ErrorDetail, JsonObject, utc_now
from cnlc_agent.domain.state import (
    InterpretationState,
    StateChange,
    StepExecution,
    StepOutcome,
)
from cnlc_agent.workflows.node import WorkflowNode

COMPLETED = {StepStatus.SUCCESS, StepStatus.WARNING}


class InterpretationWorkflow:
    def __init__(
        self,
        nodes: list[WorkflowNode],
        store: InterpretationStateStore,
        telemetry: Telemetry,
    ) -> None:
        if [node.step_id for node in nodes] != list(StepId):
            raise WorkflowError("INVALID_STEP_ORDER", "Workflow 必须按 W01–W10 顺序构造")
        self.nodes = nodes
        self.store = store
        self.telemetry = telemetry

    async def run(self, state: InterpretationState) -> InterpretationState:
        if state.status != StepStatus.PENDING:
            raise WorkflowError("TASK_ALREADY_STARTED", "骨架暂不支持恢复已有任务，请创建新任务")
        attributes: JsonObject = {"task_id": state.task.task_id, "trace_id": state.trace_id}
        with self.telemetry.span("workflow", attributes):
            for node in self.nodes:
                state.current_step = node.step_id
                state.status = StepStatus.RUNNING
                record = StepExecution(step_id=node.step_id)
                state.executions.append(record)
                state.updated_at = utc_now()
                await self.store.save(state)
                with self.telemetry.span(
                    "workflow.step",
                    {
                        **attributes,
                        "step_id": node.step_id.value,
                    },
                ):
                    try:
                        missing = node.precondition(state)
                        if missing:
                            outcome = StepOutcome(
                                status=StepStatus.BLOCKED,
                                missing_data=missing,
                                reason="节点前置结果缺失",
                            )
                        else:
                            # Nodes receive snapshots: all writes must return through StatePatch.
                            outcome = await node.execute(state.model_copy(deep=True))
                        if outcome.status not in COMPLETED | {
                            StepStatus.FAILED,
                            StepStatus.BLOCKED,
                            StepStatus.REVIEW_REQUIRED,
                        }:
                            raise WorkflowError("INVALID_NODE_STATUS", "节点返回了非终态状态")
                    except ApplicationError as exc:
                        outcome = StepOutcome(
                            status=StepStatus.FAILED,
                            reason="节点执行异常",
                            errors=[
                                ErrorDetail(
                                    code=exc.code,
                                    message=str(exc),
                                    retryable=exc.retryable,
                                    step_id=node.step_id,
                                )
                            ],
                        )
                    except Exception as exc:
                        # Never claim success for unexpected errors. Do not expose raw payloads.
                        self.telemetry.event(
                            "workflow.unexpected_error",
                            {
                                **attributes,
                                "step_id": node.step_id.value,
                                "error_type": type(exc).__name__,
                            },
                        )
                        outcome = StepOutcome(
                            status=StepStatus.FAILED,
                            reason="节点出现非预期错误",
                            errors=[
                                ErrorDetail(
                                    code="INVALID_RESULT"
                                    if isinstance(exc, SchemaError)
                                    else "UNEXPECTED_ERROR",
                                    message="节点输出无效或执行异常；请查看事件记录",
                                    step_id=node.step_id,
                                )
                            ],
                        )
                    patch = outcome.patch.model_dump(mode="json", exclude_unset=True)
                    before = state.model_dump(mode="json", include=set(patch))
                    before["status"] = state.status.value
                    state = InterpretationState.model_validate(
                        {
                            **state.model_dump(mode="json"),
                            **patch,
                        }
                    )
                    record = state.executions[-1]
                    record.status = outcome.status
                    record.ended_at = utc_now()
                    record.warnings = outcome.warnings
                    record.errors = outcome.errors
                    state.status = outcome.status
                    state.missing_data.extend(outcome.missing_data)
                    state.warnings.extend(outcome.warnings)
                    state.errors.extend(outcome.errors)
                    state.review_required = outcome.status == StepStatus.REVIEW_REQUIRED
                    state.updated_at = utc_now()
                    if outcome.status in COMPLETED:
                        state.completed_steps.append(node.step_id)
                    state.changes.append(
                        StateChange(
                            actor="InterpretationWorkflow",
                            step_id=node.step_id,
                            reason=outcome.reason,
                            before=before,
                            after={**patch, "status": state.status.value},
                        )
                    )
                    self.telemetry.event(
                        "state.change",
                        {
                            **attributes,
                            "step_id": node.step_id.value,
                            "status": state.status.value,
                            "reason": outcome.reason,
                        },
                    )
                    await self.store.save(state)
                if outcome.status not in COMPLETED:
                    break
            if len(state.completed_steps) == len(StepId):
                state.status = (
                    StepStatus.WARNING
                    if state.warnings
                    or any(item.status == StepStatus.WARNING for item in state.executions)
                    else StepStatus.SUCCESS
                )
            state.updated_at = utc_now()
            await self.store.save(state)
            self.telemetry.event("workflow.result", {**attributes, "status": state.status.value})
            return state
