"""交互策略矩阵及澄清生命周期；不依赖模型推断业务状态。"""

import pytest

from cnlc_agent.demo.interaction_state import (
    InteractionPhase,
    InteractionPolicy,
    InteractionSnapshot,
)
from cnlc_agent.domain.execution import ExecutionStatus


@pytest.mark.parametrize("action", ["STATUS", "GET_REPORT", "MODIFY", "FULL_RERUN"])
def test_no_task_rejects_without_creation(action):
    snapshot = InteractionSnapshot()
    assert snapshot.phase == InteractionPhase.NO_TASK
    decision = InteractionPolicy.decide(snapshot, action)
    assert decision.decision == "REJECT"
    assert decision.error_code == "TASK_NOT_FOUND"


@pytest.mark.parametrize("status", list(ExecutionStatus))
@pytest.mark.parametrize("action", ["START", "STATUS", "MODIFY", "FULL_RERUN", "GET_REPORT"])
def test_execution_state_matrix(status, action):
    active = status in {ExecutionStatus.QUEUED, ExecutionStatus.RUNNING}
    snapshot = InteractionSnapshot(
        session_has_task=True,
        execution_status=status,
        execution_sequence=3,
        current_step="W06",
        report_ready=not active,
    )
    assert snapshot.phase == (InteractionPhase.ACTIVE if active else InteractionPhase.READY)
    decision = InteractionPolicy.decide(snapshot, action)
    if active and action in {"MODIFY", "FULL_RERUN"}:
        assert decision.error_code == "TASK_EXECUTION_ACTIVE"
        assert "#3" in decision.message and "W06" in decision.message
    elif active and action == "GET_REPORT":
        assert decision.error_code == "REPORT_NOT_READY"
    else:
        assert decision.decision == ("READ_ONLY" if action in {"STATUS", "GET_REPORT"} else "ALLOW")


@pytest.mark.parametrize("selector", ["PREVIOUS", "LATEST_SUCCESSFUL", "EXPLICIT"])
def test_active_allows_strict_historical_report_selection(selector):
    snapshot = InteractionSnapshot(session_has_task=True, execution_status=ExecutionStatus.RUNNING)
    assert (
        InteractionPolicy.decide(snapshot, "GET_REPORT", selector=selector).decision == "READ_ONLY"
    )


@pytest.mark.parametrize(
    "flag,code",
    [
        ("ambiguous", "CLARIFICATION_REQUIRED"),
        ("conflict", "CLARIFICATION_REQUIRED"),
        ("unsupported_operation", "UNSUPPORTED_OPERATION"),
        ("unsupported_parameter", "UNSUPPORTED_PARAMETER"),
    ],
)
def test_semantic_decisions_are_explicit(flag, code):
    snapshot = InteractionSnapshot(session_has_task=True, execution_status=ExecutionStatus.SUCCESS)
    decision = InteractionPolicy.decide(snapshot, "MODIFY", **{flag: True})
    assert decision.error_code == code
    assert decision.decision in {"CLARIFY", "REJECT"}


def test_pending_restart_corruption_timeout_and_next_turn():
    """完整 pending 在同 runner 可用；不完整或无法证明生命周期时安全丢弃。"""

    from cnlc_agent.config.settings import AppSettings, PersistenceSettings
    from cnlc_agent.demo.task_context import TaskReference
    from cnlc_agent.demo.task_tools import TaskCommandRunner

    runner = TaskCommandRunner(AppSettings(_env_file=None), PersistenceSettings(_env_file=None))
    context = {}
    runner.attach_session_runtime_context(context)
    runner.begin_interaction_turn()
    runner.save_pending(TaskReference(), "task-a", 0.16)
    assert runner.pending_clarification().known_value == 0.16
    runner.end_interaction_turn()
    runner.begin_interaction_turn()
    assert runner.pending_clarification() is not None
    runner.end_interaction_turn()
    assert runner.pending_clarification() is None

    runner.save_pending(TaskReference(), "task-a", 0.16)
    context["cnlc_pending_clarification"]["expires_at"] = 0
    assert runner.pending_clarification() is None
    context["cnlc_pending_clarification"] = {"known_value": 0.16}
    assert runner.pending_clarification() is None
    runner.save_pending(TaskReference(), "task-a", 0.16)
    restarted = TaskCommandRunner(AppSettings(_env_file=None), PersistenceSettings(_env_file=None))
    restarted.attach_session_runtime_context(context)
    assert restarted.pending_clarification() is None
    assert "cnlc_pending_clarification" not in context
