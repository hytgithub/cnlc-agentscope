"""澄清补槽、固定引用、显式修正与过期清理，独立于旧 ReAct 路由。"""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from cnlc_agent.demo.interaction_context import ActiveContext, InteractionContext
from cnlc_agent.demo.operation_clarification import (
    PENDING_OPERATION_KEY,
    ClarificationPatch,
    ClarificationPatchError,
    LockedOperationReference,
    OperationClarificationStore,
)
from cnlc_agent.demo.operation_context_resolver import OperationContextResolver
from cnlc_agent.demo.operation_parser import (
    ClarificationIssue,
    PartialOperationPlan,
    finalize_partial_plan,
)
from cnlc_agent.demo.plan_validator import PlanValidator
from cnlc_agent.demo.reference_resolver import ResolvedTaskReference


def partial():
    return PartialOperationPlan.model_validate(
        {
            "input_classification": "EXECUTION_REQUEST",
            "persist_mode": "CREATE_VERSION",
            "original_instruction": "WELL_A 改成0.16",
            "operations": [
                {
                    "operation_id": "change",
                    "action": "MODIFY_PARAMETER",
                    "task_reference": {"kind": "WELL_ID", "value": "WELL_A"},
                    "parameters": {
                        "value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "fraction"}
                    },
                }
            ],
        }
    )


def lock():
    resolved = ResolvedTaskReference(
        task_id="A",
        well_id="WELL_A",
        current_execution_id="V4",
        latest_successful_execution_id="V4",
        resolution_source="EXPLICIT_WELL",
        match_count=1,
    )
    return LockedOperationReference.from_resolved("change", resolved)


@pytest.fixture
def env():
    context = {"cnlc_pending_clarification": {"old": "unchanged"}}
    now = [100.0]
    store = OperationClarificationStore(context, ttl_seconds=60, clock=lambda: now[0])
    source = partial()
    pending = store.save(
        source, finalize_partial_plan(source).issues, turn=1, locked_references=[lock()]
    )
    return context, now, store, pending


def test_target_reply_preserves_value_and_locked_task_after_active_changes(env):
    context, _, store, saved = env
    changed_active = InteractionContext(active=ActiveContext(task_id="B"))
    updated = store.apply_patch(ClarificationPatch(target="POROSITY"), turn=2)
    resolved = OperationContextResolver().resolve(updated.partial_plan, changed_active)
    final = finalize_partial_plan(resolved.plan)
    assert final.issues == []
    assert final.plan.operations[0].task_reference.value == "A"
    assert final.plan.operations[0].parameters.value.value == 0.16
    assert updated.expires_at == saved.expires_at
    assert updated.created_turn == saved.created_turn
    assert context["cnlc_pending_clarification"] == {"old": "unchanged"}


def test_store_self_validates_missing_target_and_patch_can_fill_it():
    context = {}
    store = OperationClarificationStore(context, ttl_seconds=60, clock=lambda: 100.0)
    saved = store.save(partial(), [], turn=1)
    assert [(issue.operation_id, issue.slot) for issue in saved.issues] == [("change", "TARGET")]
    updated = store.apply_patch(ClarificationPatch(target="POROSITY"), turn=2)
    assert updated.partial_plan.operations[0].target == "POROSITY"
    assert updated.issues == []


def test_store_deduplicates_schema_issue_and_preserves_caller_detail():
    store = OperationClarificationStore({}, ttl_seconds=60, clock=lambda: 100.0)
    caller = ClarificationIssue(
        operation_id="change",
        slot="TARGET",
        error_code="CLARIFICATION_REQUIRED",
        message="请明确孔隙度或渗透率",
        evidence={"source": "context"},
    )
    saved = store.save(partial(), [caller, caller], turn=1)
    targets = [issue for issue in saved.issues if issue.slot == "TARGET"]
    assert len(targets) == 1
    assert targets[0].message == caller.message
    assert targets[0].evidence == caller.evidence


def test_complete_plan_without_caller_issue_cannot_be_saved_as_pending():
    source = partial()
    source.operations[0].target = "POROSITY"
    store = OperationClarificationStore({}, ttl_seconds=60, clock=lambda: 100.0)
    with pytest.raises(ClarificationPatchError, match="不能保存"):
        store.save(source, [], turn=1)


def test_task_conflict_can_be_resolved_by_normal_reply_end_to_end():
    ctx = InteractionContext(
        active=ActiveContext(task_id="A"),
        view={"task_id": "B", "execution_id": "B-V1"},
    )
    source = partial()
    source.operations[0].task_reference = None
    source.operations[0].target = "POROSITY"
    first = OperationContextResolver().resolve(source, ctx)
    assert first.issues[0].error_code == "VIEW_ACTIVE_CONTEXT_CONFLICT"
    assert first.issues[0].slot == "TASK"
    store = OperationClarificationStore({}, ttl_seconds=60, clock=lambda: 100.0)
    store.save(first.plan, first.issues, turn=1)
    updated = store.apply_patch(
        ClarificationPatch(task_reference={"kind": "TASK_ID", "value": "A"}), turn=2
    )
    second = OperationContextResolver().resolve(updated.partial_plan, ctx)
    result = PlanValidator().validate(second)
    assert second.issues == []
    assert result.outcome == "EXECUTABLE"


@pytest.mark.parametrize(
    "execution_reference",
    [{"kind": "TASK_CURRENT"}, {"kind": "EXECUTION_ID", "execution_id": "A-V1"}],
)
def test_execution_conflict_can_be_resolved_by_normal_reply_end_to_end(execution_reference):
    ctx = InteractionContext(
        active=ActiveContext(task_id="A"),
        view={"task_id": "A", "execution_id": "A-V1"},
    )
    source = partial()
    source.operations[0].task_reference = None
    source.operations[0].target = "POROSITY"
    first = OperationContextResolver().resolve(source, ctx)
    assert first.issues[0].error_code == "VIEW_ACTIVE_CONTEXT_CONFLICT"
    assert first.issues[0].slot == "EXECUTION"
    store = OperationClarificationStore({}, ttl_seconds=60, clock=lambda: 100.0)
    store.save(first.plan, first.issues, turn=1)
    updated = store.apply_patch(ClarificationPatch(execution_reference=execution_reference), turn=2)
    second = OperationContextResolver().resolve(updated.partial_plan, ctx)
    result = PlanValidator().validate(second)
    assert second.issues == []
    assert result.outcome == "EXECUTABLE"


@pytest.mark.parametrize(
    "extra",
    [
        {"task_reference": {"kind": "TASK_ID", "value": "B"}},
        {"execution_reference": {"kind": "TASK_CURRENT"}},
        {"value": {"mode": "ABSOLUTE", "value": 0.2, "unit": "fraction"}},
        {"persist_mode": "PREVIEW"},
    ],
)
def test_target_only_reply_cannot_smuggle_other_changes(env, extra):
    context, _, store, _ = env
    before = deepcopy(context)
    with pytest.raises(ClarificationPatchError) as error:
        store.apply_patch(
            ClarificationPatch.model_validate({"target": "POROSITY", **extra}), turn=2
        )
    assert error.value.code == "CLARIFICATION_SLOT_INVALID"
    assert context == before


def test_correction_replaces_pending_scope_and_invalidates_affected_lock(env):
    _, _, store, _ = env
    source = partial()
    source.operations[0].scope = {"kind": "INTERVAL", "interval_id": "layer-5"}
    source.operations[0].execution_reference = {"kind": "EXECUTION_ID", "execution_id": "V2"}
    locked = LockedOperationReference(
        operation_id="change", task_id="A", execution_id="V2", scope=source.operations[0].scope
    )
    store.save(source, finalize_partial_plan(source).issues, turn=1, locked_references=[locked])
    updated = store.apply_patch(
        ClarificationPatch.model_validate(
            {
                "input_classification": "CORRECTION",
                "scope": {"kind": "INTERVAL", "interval_id": "layer-6"},
            }
        ),
        turn=2,
    )
    assert updated.partial_plan.operations[0].scope.interval_id == "layer-6"
    assert updated.partial_plan.operations[0].task_reference.value == "A"
    assert updated.locked_references == []
    assert updated.issues[0].slot == "TARGET"


def test_task_correction_clears_old_version_and_scope_including_shared_context(env):
    _, _, store, _ = env
    source = partial()
    source.shared_context.execution_reference = {"kind": "EXECUTION_ID", "execution_id": "A-V2"}
    source.shared_context.scope = {"kind": "INTERVAL", "interval_id": "layer-5"}
    store.save(source, finalize_partial_plan(source).issues, turn=1, locked_references=[lock()])
    updated = store.apply_patch(
        ClarificationPatch.model_validate(
            {
                "input_classification": "CORRECTION",
                "task_reference": {"kind": "TASK_ID", "value": "B"},
            }
        ),
        turn=2,
    )
    op = updated.partial_plan.operations[0]
    assert op.task_reference.value == "B"
    assert op.execution_reference is None and op.scope is None
    assert updated.partial_plan.shared_context.execution_reference is None
    assert updated.partial_plan.shared_context.scope is None
    assert updated.locked_references == []


@pytest.mark.parametrize(
    "failure", ["ttl", "owner", "future_turn", "late_turn", "malformed", "mismatched_lock", "null"]
)
def test_invalid_lifecycle_clears_pending(env, failure):
    context, now, store, _ = env
    turn = 2
    if failure == "ttl":
        now[0] = 160
    elif failure == "owner":
        store = OperationClarificationStore(context, ttl_seconds=60, clock=lambda: now[0])
    elif failure == "future_turn":
        turn = 0
    elif failure == "late_turn":
        turn = 3
    elif failure == "mismatched_lock":
        context[PENDING_OPERATION_KEY]["partial_plan"]["operations"][0]["task_reference"][
            "value"
        ] = "B"
    elif failure == "null":
        context[PENDING_OPERATION_KEY] = None
    else:
        context[PENDING_OPERATION_KEY] = {"partial_plan": "broken"}
    assert store.peek(turn=turn) is None
    assert PENDING_OPERATION_KEY not in context
    with pytest.raises(ClarificationPatchError):
        store.apply_patch(ClarificationPatch(target="POROSITY"), turn=turn)


def test_cancel_consume_and_new_operation_replace_old_pending(env):
    context, _, store, _ = env
    assert store.consume(turn=2) is not None
    assert store.peek(turn=2) is None
    with pytest.raises(ClarificationPatchError):
        store.apply_patch(
            ClarificationPatch(input_classification="CORRECTION", target="POROSITY"), turn=2
        )
    source = partial()
    source.original_instruction = "新独立请求"
    store.save(source, finalize_partial_plan(source).issues, turn=2)
    assert store.peek(turn=2).original_instruction == "新独立请求"
    store.cancel()
    assert PENDING_OPERATION_KEY not in context
    assert "cnlc_pending_clarification" in context


def test_partial_task_lock_allows_missing_execution_to_be_filled(env):
    _, _, store, _ = env
    source = partial()
    issues = [
        *finalize_partial_plan(source).issues,
        ClarificationIssue(operation_id="change", slot="EXECUTION", message="请选择版本"),
    ]
    store.save(source, issues, turn=1, locked_references=[lock()])
    updated = store.apply_patch(
        ClarificationPatch.model_validate(
            {"execution_reference": {"kind": "EXECUTION_ID", "execution_id": "A-V2"}}
        ),
        turn=2,
    )
    assert updated.partial_plan.operations[0].execution_reference.execution_id == "A-V2"
    assert updated.partial_plan.operations[0].task_reference.value == "A"


@pytest.mark.parametrize(
    "payload",
    [
        {"target": "POROSITY", "locked": True},
        {"target": "POROSITY", "locked_references": []},
        {"target": None},
        {},
        {"parameters": {"value": 0.16}},
        {"target": "POROSITY", "unknown": 1},
    ],
)
def test_patch_schema_cannot_claim_trust_or_write_arbitrary_fields(payload):
    with pytest.raises(ValidationError):
        ClarificationPatch.model_validate(payload)


def test_peek_and_save_return_isolated_objects(env):
    _, _, store, saved = env
    saved.partial_plan.operations.clear()
    fetched = store.peek(turn=1)
    fetched.partial_plan.operations.clear()
    assert len(store.peek(turn=1).partial_plan.operations) == 1


def test_multiple_operation_patch_requires_operation_id(env):
    _, _, store, _ = env
    source = partial()
    other = source.operations[0].model_copy(deep=True)
    other.operation_id = "other"
    source.operations.append(other)
    store.save(source, finalize_partial_plan(source).issues, turn=1)
    with pytest.raises(ClarificationPatchError):
        store.apply_patch(ClarificationPatch(target="POROSITY"), turn=2)
    updated = store.apply_patch(ClarificationPatch(operation_id="other", target="POROSITY"), turn=2)
    assert updated.partial_plan.operations[0].target is None
    assert updated.partial_plan.operations[1].target == "POROSITY"
