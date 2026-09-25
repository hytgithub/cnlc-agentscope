"""Execution Web 视图只反映持久事实，并保持敏感快照边界。"""

from datetime import timedelta

from cnlc_agent.demo.read_models import present_execution_view, present_task_view
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.execution import Execution, ExecutionStatus, InterpretationTask
from cnlc_agent.domain.models import TaskRequest, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState, ReusedStep, StepExecution
from cnlc_agent.domain.tool_run import ToolExecutionMode, ToolRun, ToolRunStatus


class ReadRepository:
    """只实现投影使用的查询，证明读取不会进入业务服务或模型。"""

    def __init__(self, executions=(), runs=(), reports=None):
        self.executions = list(executions)
        self.runs = list(runs)
        self.reports = reports or {}

    async def list_executions(self, task_id):
        return [item for item in self.executions if item.task_id == task_id]

    async def list_tool_runs(self, execution_id):
        return [item for item in self.runs if item.execution_id == execution_id]

    async def get_execution_report(self, execution_id):
        return self.reports.get(execution_id)


def make_execution(
    sequence: int,
    *,
    status: ExecutionStatus = ExecutionStatus.QUEUED,
    completed: list[StepId] | None = None,
    reused: list[StepId] | None = None,
    current: StepId | None = None,
    markdown: str = "",
    start_step: StepId | None = StepId.W01,
) -> Execution:
    """构造满足领域生命周期约束的最小 Execution。"""

    now = utc_now()
    request = TaskRequest(task_id="TASK_VIEW", well_id="WELL_MOCK_001")
    completed = completed or []
    reused = reused or []
    executions = [
        StepExecution(
            step_id=step,
            status=(StepStatus.RUNNING if step == current else StepStatus.SUCCESS),
            ended_at=None if step == current else now,
        )
        for step in completed
        if step not in reused
    ]
    state = InterpretationState(
        task=request,
        status=(
            StepStatus.PENDING
            if status == ExecutionStatus.QUEUED
            else StepStatus.RUNNING
            if status == ExecutionStatus.RUNNING
            else StepStatus.SUCCESS
        ),
        current_step=current,
        completed_steps=completed,
        executions=executions,
        reused_steps=[
            ReusedStep(
                step_id=step,
                source_execution_id="EXEC_SOURCE",
                source_status=StepStatus.SUCCESS,
            )
            for step in reused
        ],
    )
    return Execution(
        execution_id=state.workflow_execution_id,
        task_id=request.task_id,
        sequence=sequence,
        status=status,
        state_snapshot=state,
        markdown=markdown,
        trigger_type="INITIAL" if sequence == 1 else "RERUN",
        override_snapshot=(
            InterpretationOverride(por=0.16) if sequence > 1 else InterpretationOverride()
        ),
        start_step=start_step,
        source_execution_id=None if sequence == 1 else "EXEC_SOURCE",
        planning_reason="INITIAL" if sequence == 1 else "OVERRIDE_CHANGED",
        started_at=None if status == ExecutionStatus.QUEUED else now,
        finished_at=(
            now if status not in {ExecutionStatus.QUEUED, ExecutionStatus.RUNNING} else None
        ),
        lease_owner="worker" if status == ExecutionStatus.RUNNING else None,
        lease_expires_at=now + timedelta(minutes=1) if status == ExecutionStatus.RUNNING else None,
    )


async def test_queued_and_running_w06_views_show_real_progress():
    queued = make_execution(1)
    queued_view = await present_execution_view(ReadRepository(), queued)  # type: ignore[arg-type]
    assert queued_view.execution_status == ExecutionStatus.QUEUED
    assert {step.display_status for step in queued_view.steps} == {"PENDING"}
    assert queued_view.report_markdown is None

    completed = list(StepId)[:5]
    running = make_execution(
        2,
        status=ExecutionStatus.RUNNING,
        completed=[*completed, StepId.W06],
        current=StepId.W06,
    )
    running_view = await present_execution_view(ReadRepository(), running)  # type: ignore[arg-type]
    statuses = {step.id: step.display_status for step in running_view.steps}
    assert running_view.current_step == StepId.W06
    assert all(statuses[step] == "SUCCESS" for step in completed)
    assert statuses[StepId.W06] == "RUNNING"
    assert statuses[StepId.W07] == "PENDING"


async def test_partial_full_and_report_only_stage_projection():
    partial = make_execution(2, reused=list(StepId)[:3], start_step=StepId.W04)
    partial_view = await present_execution_view(ReadRepository(), partial)  # type: ignore[arg-type]
    assert [stage.action for stage in partial_view.stages] == ["REUSE", "REUSE", "RUN", "RUN"]
    assert [step.display_status for step in partial_view.steps[:3]] == ["REUSED"] * 3

    full = make_execution(3)
    full_view = await present_execution_view(ReadRepository(), full)  # type: ignore[arg-type]
    assert [stage.action for stage in full_view.stages] == ["RUN"] * 4
    assert all(step.display_status != "REUSED" for step in full_view.steps)

    report_only = make_execution(4, reused=list(StepId), start_step=None)
    report_view = await present_execution_view(ReadRepository(), report_only)  # type: ignore[arg-type]
    assert [stage.action for stage in report_view.stages] == ["REUSE", "REUSE", "REUSE", "RUN"]
    assert all(step.display_status == "REUSED" for step in report_view.steps)


async def test_success_history_and_tool_run_are_safe_and_version_bound():
    first = make_execution(
        1,
        status=ExecutionStatus.SUCCESS,
        completed=list(StepId),
        markdown="# first",
    )
    second = make_execution(2)
    secret_run = ToolRun(
        task_id=first.task_id,
        execution_id=first.execution_id,
        step_id=StepId.W06,
        tool_code="calculate_sw",
        status=ToolRunStatus.SUCCESS,
        execution_mode=ToolExecutionMode.MOCK,
        source="mock:fixture",
        input_snapshot={"authorization": "secret"},
        output_snapshot={"raw_data": "secret", "values": [1, 2, 3]},
        finished_at=utc_now(),
    )
    repository = ReadRepository(
        executions=[first, second],
        runs=[secret_run],
        reports={first.execution_id: "# first"},
    )
    task = InterpretationTask(
        task_id=first.task_id,
        well_id="WELL_MOCK_001",
        current_execution_id=second.execution_id,
        latest_successful_execution_id=first.execution_id,
    )
    task_view = await present_task_view(repository, task)  # type: ignore[arg-type]
    assert [item.sequence for item in task_view.executions] == [2, 1]
    assert task_view.current_execution.execution_id == second.execution_id

    first_view = await present_execution_view(repository, first)  # type: ignore[arg-type]
    assert first_view.report_ready is True
    assert first_view.report_markdown == "# first"
    dumped = first_view.model_dump_json()
    assert "secret" not in dumped and "snapshot" not in dumped
    assert [run.tool_code for run in first_view.tool_runs] == ["calculate_sw"]
    assert all(run.tool_code != "qwen-plus" for run in first_view.tool_runs)
