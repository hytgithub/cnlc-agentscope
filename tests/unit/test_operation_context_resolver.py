"""显式引用优先、读写焦点隔离和歧义阻断。"""

import pytest

from cnlc_agent.demo.interaction_context import (
    ActiveContext,
    InteractionContext,
    RecentContext,
    SelectedIntervalReference,
    ViewContext,
)
from cnlc_agent.demo.operation_context_resolver import OperationContextResolver
from cnlc_agent.demo.operation_parser import PartialOperationPlan
from cnlc_agent.demo.plan_validator import PlanValidator


def plan(action="MODIFY_PARAMETER", **node):
    return PartialOperationPlan.model_validate(
        {
            "input_classification": "EXECUTION_REQUEST",
            "original_instruction": "结构化请求",
            "persist_mode": "CREATE_VERSION",
            "operations": [
                {
                    "operation_id": "op",
                    "action": action,
                    "target": "POROSITY",
                    "parameters": {
                        "value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "fraction"}
                    },
                    **node,
                }
            ],
        }
    )


def context(base=None, view_task="B"):
    return InteractionContext(
        active=ActiveContext(task_id="A", base_execution_id=base),
        view=ViewContext(task_id=view_task, execution_id="view-V1"),
    )


@pytest.mark.parametrize("action", ["QUERY", "EXPLAIN", "COMPARE", "HISTORY", "REPORT", "STATUS"])
def test_reads_prefer_view_and_do_not_mutate_context(action):
    ctx = context()
    original = ctx.model_dump_json()
    source = plan(action)
    resolved = OperationContextResolver().resolve(source, ctx)
    op = resolved.plan.operations[0]
    assert op.task_reference.value == "B"
    assert op.execution_reference.execution_id == "view-V1"
    assert ctx.model_dump_json() == original
    assert source.operations[0].task_reference is None


@pytest.mark.parametrize(
    "action",
    [
        "MODIFY_PARAMETER",
        "MODIFY_RESULT",
        "RECALCULATE",
        "REINTERPRET",
        "SWITCH_MODEL",
        "SWITCH_METHOD",
        "SEGMENT_EDIT",
        "OVERRIDE",
        "RESTORE_VERSION",
        "COMMIT_SCENARIO",
        "MARK_FINAL",
    ],
)
def test_implicit_write_cannot_choose_between_view_and_active(action):
    resolved = OperationContextResolver().resolve(plan(action), context())
    assert resolved.issues[0].error_code == "VIEW_ACTIVE_CONTEXT_CONFLICT"
    assert resolved.issues[0].slot == "TASK"
    assert resolved.plan.operations[0].task_reference is None
    validated = PlanValidator().validate(resolved)
    assert validated.outcome == "NEED_CLARIFICATION"
    assert validated.plan is None


def test_same_task_historical_view_requires_base_clarification():
    resolved = OperationContextResolver().resolve(plan(), context(view_task="A"))
    assert resolved.issues[0].error_code == "VIEW_ACTIVE_CONTEXT_CONFLICT"
    assert resolved.issues[0].slot == "EXECUTION"
    assert resolved.plan.operations[0].execution_reference is None


@pytest.mark.parametrize("action", ["FULL_INTERPRET", "NEW_WELL"])
def test_task_creation_actions_do_not_inherit_active_or_require_existing_task(action):
    resolved = OperationContextResolver().resolve(plan(action), context(base="A-V2"))
    assert resolved.issues == []
    op = resolved.plan.operations[0]
    assert op.task_reference is None
    assert op.execution_reference is None
    assert op.scope is None


def test_explicit_active_base_beats_view_version():
    resolved = OperationContextResolver().resolve(plan(), context(base="active-V2", view_task="A"))
    assert [issue.slot for issue in resolved.issues] == ["SCOPE"]
    assert resolved.plan.operations[0].execution_reference.execution_id == "active-V2"


@pytest.mark.parametrize("task_id", ["B", "C"])
def test_explicit_task_wins_without_foreign_version_inheritance(task_id):
    resolved = OperationContextResolver().resolve(
        plan(task_reference={"kind": "TASK_ID", "value": task_id}), context(base="active-V2")
    )
    assert [issue.slot for issue in resolved.issues] == ["SCOPE"]
    assert resolved.plan.operations[0].task_reference.value == task_id
    assert resolved.plan.operations[0].execution_reference is None


def test_explicit_version_and_scope_never_overwritten_and_shared_is_explicit():
    source = plan(
        execution_reference={"kind": "TASK_CURRENT"},
        scope={"kind": "INTERVAL", "interval_id": "explicit-layer"},
    )
    source.shared_context.task_reference = {"kind": "TASK_ID", "value": "B"}
    resolved = OperationContextResolver().resolve(source, context(base="V2"))
    op = resolved.plan.operations[0]
    assert op.task_reference.value == "B"
    assert op.execution_reference.kind == "TASK_CURRENT"
    assert op.scope.interval_id == "explicit-layer"


def test_scope_inherits_only_with_matching_task_and_version():
    ctx = InteractionContext.model_validate(
        {
            "active": {
                "task_id": "A",
                "base_execution_id": "V2",
                "scope": {"kind": "INTERVAL", "interval_id": "active-layer"},
            },
            "view": {
                "task_id": "A",
                "execution_id": "V1",
                "scope": {"kind": "INTERVAL", "interval_id": "view-layer"},
            },
        }
    )
    resolver = OperationContextResolver()
    assert resolver.resolve(plan(), ctx).plan.operations[0].scope.interval_id == "active-layer"
    assert resolver.resolve(plan("QUERY"), ctx).plan.operations[0].scope.interval_id == "view-layer"
    explicit = resolver.resolve(plan(execution_reference={"kind": "TASK_CURRENT"}), ctx)
    assert explicit.plan.operations[0].scope is None
    assert [issue.slot for issue in explicit.issues] == ["SCOPE"]


def test_view_only_cannot_supply_write_task_and_recent_is_opt_in():
    ctx = InteractionContext(
        view=ViewContext(task_id="B"),
        recent=RecentContext(
            selected_intervals=[
                SelectedIntervalReference(task_id="B", execution_id="V1", interval_id="i")
            ]
        ),
    )
    resolver = OperationContextResolver()
    resolved = resolver.resolve(plan(), ctx)
    assert resolved.plan.operations[0].task_reference is None
    assert resolved.plan.operations[0].scope is None
    assert resolved.issues[0].slot == "TASK"
    selected = resolver.resolve_recent_selection(ctx)
    selected.clear()
    assert len(ctx.recent.selected_intervals) == 1
    assert resolver.resolve_last_compare(ctx) is None
    ctx.view = None
    ctx.active = ActiveContext(task_id="A", base_execution_id="V2")
    assert resolver.resolve(plan("QUERY"), ctx).plan.operations[0].task_reference.value == "A"
