"""交互焦点隔离、兼容桥与短期状态恢复边界。"""

from copy import deepcopy

import pytest
from agentscope.state import AgentState
from pydantic import ValidationError

from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.demo.interaction_context import (
    INTERACTION_CONTEXT_KEY,
    LEGACY_ACTIVE_TASK_KEY,
    ActiveContext,
    CompareContext,
    ContextExecutionReference,
    InteractionContext,
    RecentContext,
    SelectedIntervalReference,
    ViewContext,
)
from cnlc_agent.demo.operation_models import IntervalScope
from cnlc_agent.demo.task_tools import TaskCommandRunner


@pytest.fixture
def runner():
    return TaskCommandRunner(
        settings=AppSettings(mode="demo", model_provider="mock", _env_file=None),
        persistence=PersistenceSettings(_env_file=None),
        connections=ConnectionSettings(_env_file=None),
    )


def _recent():
    return RecentContext(
        last_operation_id="op-1",
        last_compare=CompareContext(
            left=ContextExecutionReference(task_id="A", execution_id="A-V2"),
            right=ContextExecutionReference(task_id="B", execution_id="B-V1"),
        ),
        last_scenario_id="scenario-1",
        selected_intervals=[
            SelectedIntervalReference(task_id="A", execution_id="A-V2", interval_id="layer-5")
        ],
    )


def _populate(runner):
    runner.set_active_task("A")
    runner.set_active_base("A", "A-V2", IntervalScope(interval_id="layer-5"))
    runner.set_view_context("B", "B-V1", IntervalScope(interval_id="layer-3"))
    runner.set_recent_context(_recent())


def test_empty_context_roundtrip_and_defaults_are_independent():
    first = InteractionContext()
    assert InteractionContext.model_validate_json(first.model_dump_json()) == first
    assert ActiveContext(task_id="A").base_execution_id is None
    assert ViewContext(task_id="B").execution_id is None
    first.recent.selected_intervals.append(_recent().selected_intervals[0])
    assert InteractionContext().recent == RecentContext()


@pytest.mark.parametrize("model", [ActiveContext, ViewContext])
@pytest.mark.parametrize("task_id", ["", "  ", None])
def test_task_ids_are_nonblank(model, task_id):
    with pytest.raises(ValidationError):
        model(task_id=task_id)


@pytest.mark.parametrize(
    "model,version_key", [(ActiveContext, "base_execution_id"), (ViewContext, "execution_id")]
)
@pytest.mark.parametrize("version", [None, "", "  "])
def test_scope_requires_nonblank_execution(model, version_key, version):
    with pytest.raises(ValidationError):
        model(task_id="A", scope=IntervalScope(interval_id="layer-5"), **{version_key: version})


@pytest.mark.parametrize("missing", ["task_id", "execution_id", "interval_id"])
def test_selected_interval_requires_version_qualified_identity(missing):
    data = {"task_id": "A", "execution_id": "V2", "interval_id": "layer-5"}
    data.pop(missing)
    with pytest.raises(ValidationError):
        SelectedIntervalReference.model_validate(data)
    with pytest.raises(ValidationError):
        SelectedIntervalReference.model_validate({**data, missing: " "})


@pytest.mark.parametrize("side", ["left", "right"])
def test_compare_requires_both_task_and_execution(side):
    data = {
        "left": {"task_id": "A", "execution_id": "V1"},
        "right": {"task_id": "B", "execution_id": "V2"},
    }
    data[side] = {}
    with pytest.raises(ValidationError):
        CompareContext.model_validate(data)


def test_switch_task_clears_base_scope_and_syncs_legacy(runner):
    context = {}
    runner.attach_session_runtime_context(context)
    _populate(runner)
    previous = runner.interaction_context
    runner.set_active_task("A")
    assert runner.interaction_context == previous
    runner.set_active_task("B")
    assert runner.active_task_id == context[LEGACY_ACTIVE_TASK_KEY] == "B"
    assert runner.interaction_context.active == ActiveContext(task_id="B")
    assert context[INTERACTION_CONTEXT_KEY]["active"] == ActiveContext(task_id="B").model_dump()
    assert runner.interaction_context.view == previous.view
    assert runner.interaction_context.recent == previous.recent


def test_active_base_rejects_task_switch_and_has_no_repository_side_effect(runner):
    with pytest.raises(ValueError, match="match current active task"):
        runner.set_active_base("A", "V2")
    runner.set_active_task("A")
    runner.set_active_base("A", "V2", IntervalScope(interval_id="layer-5"))
    before = runner.interaction_context
    with pytest.raises(ValueError, match="match current active task"):
        runner.set_active_base("B", "V3")
    assert runner.interaction_context == before
    assert runner.observed_task_ids == set()
    runner.set_active_base("A", "V3")
    assert runner.interaction_context.active == ActiveContext(task_id="A", base_execution_id="V3")


def test_view_and_recent_updates_do_not_change_active(runner):
    runner.set_active_task("A")
    runner.set_active_base("A", "V2")
    active = runner.interaction_context.active
    runner.set_view_context("B", "V1", IntervalScope(interval_id="layer-3"))
    view = runner.interaction_context.view
    runner.set_recent_context(_recent())
    assert runner.active_task_id == "A"
    assert runner.interaction_context.active == active
    assert runner.interaction_context.view == view
    runner.clear_view_context()
    assert runner.interaction_context.active == active
    assert runner.interaction_context.view is None
    assert runner.interaction_context.recent == _recent()


def test_invalid_updates_are_atomic_and_snapshots_are_isolated(runner):
    context = {}
    runner.attach_session_runtime_context(context)
    _populate(runner)
    before = deepcopy(context)
    with pytest.raises(ValidationError):
        runner.set_view_context("B", scope=IntervalScope(interval_id="layer-1"))
    with pytest.raises(ValidationError):
        runner.set_active_base("A", " ")
    with pytest.raises(ValidationError):
        runner.set_active_task(" ")
    assert context == before
    recent = _recent()
    runner.set_recent_context(recent)
    recent.selected_intervals.clear()
    snapshot = runner.interaction_context
    snapshot.recent.selected_intervals.clear()
    snapshot.active.task_id = "other"
    assert context == before
    assert runner.interaction_context.recent == _recent()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "broken",
        [],
        {"active": {}},
        {"active": {"task_id": "A", "scope": {"kind": "INTERVAL", "interval_id": "x"}}},
    ],
)
def test_malformed_new_context_falls_back_to_minimal_legacy(runner, raw):
    _populate(runner)
    context = {INTERACTION_CONTEXT_KEY: raw, LEGACY_ACTIVE_TASK_KEY: "legacy"}
    runner.attach_session_runtime_context(context)
    expected = InteractionContext(active=ActiveContext(task_id="legacy"))
    assert runner.active_task_id == "legacy"
    assert runner.interaction_context == expected
    assert context[INTERACTION_CONTEXT_KEY] == expected.model_dump(mode="json")


@pytest.mark.parametrize("legacy", [None, "", " ", 123, {}])
def test_invalid_legacy_cannot_resurrect_prior_runtime_state(runner, legacy):
    _populate(runner)
    context = {LEGACY_ACTIVE_TASK_KEY: legacy}
    runner.attach_session_runtime_context(context)
    assert runner.interaction_context == InteractionContext()
    assert runner.active_task_id is None
    assert LEGACY_ACTIVE_TASK_KEY not in context


def test_only_legacy_restores_minimal_active(runner):
    _populate(runner)
    runner.attach_session_runtime_context({LEGACY_ACTIVE_TASK_KEY: "A"})
    assert runner.interaction_context == InteractionContext(active=ActiveContext(task_id="A"))


@pytest.mark.parametrize("legacy", ["A", "conflicting-task"])
def test_agent_state_refresh_preserves_all_new_context_and_syncs_legacy(runner, legacy):
    state = AgentState()
    runner.attach_session_runtime_context(state.middle_context)
    _populate(runner)
    before = runner.interaction_context
    restored = AgentState.model_validate_json(state.model_dump_json())
    restored.middle_context[LEGACY_ACTIVE_TASK_KEY] = legacy
    runner.attach_session_runtime_context(restored.middle_context)
    assert runner.interaction_context == before
    assert runner.active_task_id == restored.middle_context[LEGACY_ACTIVE_TASK_KEY] == "A"


def test_new_format_without_active_removes_stale_legacy(runner):
    state = InteractionContext(view=ViewContext(task_id="B"))
    context = {INTERACTION_CONTEXT_KEY: state.model_dump(), LEGACY_ACTIVE_TASK_KEY: "A"}
    runner.attach_session_runtime_context(context)
    assert runner.active_task_id is None
    assert LEGACY_ACTIVE_TASK_KEY not in context
    assert runner.interaction_context == state
