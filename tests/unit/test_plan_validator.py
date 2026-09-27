"""全计划图、能力及冲突验证，失败不能释放可执行子计划。"""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from cnlc_agent.application.commands import TaskCommands
from cnlc_agent.application.execution_dispatcher import InProcessExecutionDispatcher
from cnlc_agent.application.service import InterpretationTaskService
from cnlc_agent.demo.operation_capabilities import (
    OperationCapability,
    OperationCapabilityStatus,
    OperationCatalog,
)
from cnlc_agent.demo.operation_models import ActionType, OperationScopeKind
from cnlc_agent.demo.operation_parser import PartialOperationPlan
from cnlc_agent.demo.plan_validator import PlanValidator
from cnlc_agent.demo.reference_resolver import ResolvedTaskReference
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository


def node(op_id="op1", action="MODIFY_PARAMETER", task="A", **kwargs):
    return {
        "operation_id": op_id,
        "action": action,
        "target": "POROSITY",
        "task_reference": {"kind": "TASK_ID", "value": task},
        "parameters": {"value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "fraction"}},
        **kwargs,
    }


def plan(operations=None, **kwargs):
    return PartialOperationPlan.model_validate(
        {
            "input_classification": "EXECUTION_REQUEST",
            "original_instruction": "复合结构化请求",
            "persist_mode": "CREATE_VERSION",
            "operations": operations or [node()],
            **kwargs,
        }
    )


def enabled_catalog():
    return OperationCatalog(
        OperationCapability(
            action=action,
            status="ENABLED",
            description="测试注入的能力，不修改生产 Catalog",
            allowed_scopes=tuple(OperationScopeKind),
            supported_parameters=("por",),
        )
        for action in ActionType
    )


def resolved(task_id):
    return ResolvedTaskReference(
        task_id=task_id,
        well_id=f"WELL_{task_id}",
        current_execution_id=None,
        latest_successful_execution_id=None,
        resolution_source="EXPLICIT_TASK",
        match_count=1,
    )


def compound():
    return plan(
        [
            node(),
            node("op2", "RECALCULATE", input_refs=[{"operation_id": "op1"}]),
            node("op3", "COMPARE", input_refs=[{"operation_id": "op1"}, {"operation_id": "op2"}]),
        ],
        edges=[
            {"from_operation_id": "op1", "to_operation_id": "op2", "type": "SEQUENCE"},
            {"from_operation_id": "op2", "to_operation_id": "op3", "type": "COMPARE_DEPENDENCY"},
        ],
    )


def test_compound_plan_valid_as_a_whole():
    source = compound()
    before = source.model_dump_json()
    result = PlanValidator(enabled_catalog()).validate(source)
    assert result.graph_valid
    assert result.outcome == "EXECUTABLE"
    assert len(result.plan.operations) == 3
    assert not result.read_only
    assert source.model_dump_json() == before


def test_parallel_model_plan_is_structurally_valid_even_when_unsupported():
    source = plan(
        [
            node("a", "SWITCH_MODEL", parameters={"model_id": "model-A"}),
            node("b", "SWITCH_MODEL", parameters={"model_id": "model-B"}),
            node("compare", "COMPARE", input_refs=[{"operation_id": "a"}, {"operation_id": "b"}]),
        ]
    )
    result = PlanValidator(
        OperationCatalog().with_status(
            ActionType.COMPARE, OperationCapabilityStatus.NOT_IMPLEMENTED
        )
    ).validate(source)
    assert result.graph_valid
    assert result.outcome == "KNOWN_UNSUPPORTED"
    assert result.plan is None
    assert len(result.capability_issues) == 3


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "edge_missing",
        "self",
        "cycle",
        "input_missing",
        "input_self",
        "input_cycle",
        "condition_missing",
        "output_missing",
    ],
)
def test_all_graph_references_and_cycles_are_validated(case):
    data = compound().model_dump(mode="json")
    if case == "duplicate":
        data["operations"].append(deepcopy(data["operations"][0]))
    elif case == "edge_missing":
        data["edges"][0]["from_operation_id"] = "absent"
    elif case == "self":
        data["edges"][0]["from_operation_id"] = "op2"
    elif case == "cycle":
        data["edges"].append(
            {"from_operation_id": "op3", "to_operation_id": "op1", "type": "SEQUENCE"}
        )
    elif case.startswith("input"):
        data["operations"][0]["input_refs"] = [
            {
                "operation_id": {
                    "input_missing": "absent",
                    "input_self": "op1",
                    "input_cycle": "op3",
                }[case]
            }
        ]
    elif case == "condition_missing":
        data["conditions"] = [{"expression": "条件描述", "operation_ids": ["absent"]}]
    else:
        data["output_requirement"] = {"description": "比较结果", "operation_ids": ["absent"]}
    result = PlanValidator(enabled_catalog()).validate(PartialOperationPlan.model_validate(data))
    assert result.outcome == "REJECTED"
    assert not result.graph_valid
    assert result.issues[0].error_code == "INVALID_OPERATION_PLAN"
    assert result.plan is None


@pytest.mark.parametrize("status", list(OperationCapabilityStatus))
def test_capability_query_never_offers_write_regardless_of_status(status):
    catalog = enabled_catalog().with_status(ActionType.RECALCULATE, status)
    source = plan(
        [node(action="RECALCULATE", target="WATER_SATURATION")],
        input_classification="CAPABILITY_QUERY",
        persist_mode=None,
    )
    result = PlanValidator(catalog).validate(source)
    assert result.outcome == "READ_ONLY"
    assert result.read_only
    assert result.plan is None
    assert result.capability_facts[0].capability_status == status


@pytest.mark.parametrize(
    "action,status",
    [("COMPARE", "NOT_IMPLEMENTED"), ("SEGMENT_EDIT", "UNVERIFIED"), ("OVERRIDE", "DISABLED")],
)
def test_capability_status_retained_without_hardcoded_action_rejection(action, status):
    inputs = [
        {"execution_reference": {"kind": "FIRST"}},
        {"execution_reference": {"kind": "TASK_CURRENT"}},
    ]
    source = plan([node(action=action, input_refs=inputs)])
    catalog = enabled_catalog().with_status(ActionType(action), OperationCapabilityStatus(status))
    result = PlanValidator(catalog).validate(source)
    assert result.outcome == "KNOWN_UNSUPPORTED"
    assert result.capability_issues[0].capability_status == status
    assert PlanValidator(enabled_catalog()).validate(source).outcome in {"EXECUTABLE", "READ_ONLY"}


def test_missing_catalog_entry_scope_and_parameter_fail_closed():
    assert PlanValidator(OperationCatalog([])).validate(plan()).outcome == "KNOWN_UNSUPPORTED"
    scope = {"kind": "INTERVAL", "interval_id": "layer-1"}
    result = PlanValidator().validate(plan([node(scope=scope)]))
    assert result.outcome == "KNOWN_UNSUPPORTED"
    assert result.capability_issues[0].capability_status == "ENABLED"
    result = PlanValidator().validate(
        plan(
            [
                node(
                    parameters={
                        "parameter_name": "rw",
                        "value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "fraction"},
                    }
                )
            ]
        )
    )
    assert result.outcome == "KNOWN_UNSUPPORTED"


def test_out_of_domain_never_executes_even_with_enabled_write():
    result = PlanValidator(enabled_catalog()).validate(plan(input_classification="OUT_OF_DOMAIN"))
    assert result.outcome == "REJECTED"
    assert result.plan is None


@pytest.mark.parametrize(
    "classification",
    ["CORRECTION", "CLARIFICATION_REPLY", "CONFIRMATION", "CANCELLATION", "META_REQUEST"],
)
def test_interaction_payloads_are_not_standalone_execution_plans(classification):
    result = PlanValidator(enabled_catalog()).validate(plan(input_classification=classification))
    assert result.outcome == "REJECTED"


def test_read_request_with_write_is_conflict():
    result = PlanValidator(enabled_catalog()).validate(plan(input_classification="READ_REQUEST"))
    assert result.outcome == "NEED_CLARIFICATION"
    assert result.conflicts[0].error_code == "MULTI_OPERATION_CONFLICT"


def test_conditions_not_evaluated_even_with_all_capabilities_enabled():
    result = PlanValidator(enabled_catalog()).validate(
        plan(
            conditions=[{"expression": "Sw > 60%，然后继续直到满足条件", "operation_ids": ["op1"]}]
        )
    )
    assert result.outcome == "KNOWN_UNSUPPORTED"
    assert result.issues[0].error_code == "CONDITIONAL_EXECUTION_UNSUPPORTED"
    assert result.plan is None


@pytest.mark.parametrize(
    "action,slot",
    [
        ("MODIFY_RESULT", "VALUE"),
        ("MODIFY_PARAMETER", "VALUE"),
        ("SWITCH_MODEL", "METHOD"),
        ("SWITCH_METHOD", "METHOD"),
        ("COMPARE", "COMPARE_TARGET"),
    ],
)
def test_required_action_slots(action, slot):
    source = plan([node(action=action, parameters={})])
    result = PlanValidator(enabled_catalog()).validate(source)
    assert result.outcome == "NEED_CLARIFICATION"
    assert slot in {issue.slot for issue in result.missing_slots}
    assert result.plan is None


def test_cross_task_writes_blocked_but_read_compare_allowed():
    validator = PlanValidator(enabled_catalog())
    result = validator.validate(plan([node(), node("op2", "RECALCULATE", task="B")]))
    assert result.outcome == "KNOWN_UNSUPPORTED"
    assert result.issues[0].error_code == "CROSS_TASK_WRITE_UNSUPPORTED"
    compare = plan(
        [
            node(
                action="COMPARE",
                input_refs=[
                    {
                        "task_reference": {"kind": "TASK_ID", "value": task},
                        "execution_reference": {"kind": "TASK_CURRENT"},
                    }
                    for task in ("A", "B")
                ],
            )
        ]
    )
    assert validator.validate(compare).outcome == "READ_ONLY"


def test_unresolved_multi_write_requires_task_resolution():
    source = plan(
        [node(), node("op2", "RECALCULATE", task_reference={"kind": "WELL_ID", "value": "WELL_A"})]
    )
    validator = PlanValidator(enabled_catalog())
    assert validator.validate(source).outcome == "NEED_CLARIFICATION"
    assert (
        validator.validate(source, resolved_task_references={"op2": resolved("A")}).outcome
        == "EXECUTABLE"
    )
    assert (
        validator.validate(source, resolved_task_references={"op2": resolved("B")}).outcome
        == "KNOWN_UNSUPPORTED"
    )


def test_raw_task_id_mapping_cannot_claim_resolver_result():
    source = plan(
        [node(), node("op2", "RECALCULATE", task_reference={"kind": "WELL_ID", "value": "WELL_A"})]
    )
    with pytest.raises(TypeError, match="ResolvedTaskReference"):
        PlanValidator(enabled_catalog()).validate(
            source,
            resolved_task_references={"op2": "A"},  # type: ignore[dict-item]
        )


def test_full_interpret_without_existing_task_has_planning_qualification():
    source = plan(
        [
            node(
                action="FULL_INTERPRET",
                task_reference=None,
                target="WELL",
                scope={"kind": "WHOLE_WELL"},
                parameters={},
            )
        ]
    )
    result = PlanValidator().validate(source)
    assert result.outcome == "EXECUTABLE"
    assert result.missing_slots == []


def test_new_well_without_existing_task_is_unsupported_by_catalog_not_task_missing():
    source = plan([node(action="NEW_WELL", task_reference=None, target="WELL", parameters={})])
    result = PlanValidator().validate(source)
    assert result.outcome == "KNOWN_UNSUPPORTED"
    assert result.capability_issues[0].capability_status == "NOT_IMPLEMENTED"
    assert not any(issue.slot == "TASK" for issue in result.missing_slots)


@pytest.mark.parametrize("action", ["FULL_INTERPRET", "NEW_WELL"])
def test_task_creation_action_cannot_be_reinterpreted_as_existing_task_rerun(action):
    source = plan([node(action=action, target="WELL", scope={"kind": "WHOLE_WELL"}, parameters={})])
    result = PlanValidator().validate(source)
    assert result.outcome == "REJECTED"
    assert result.issues[0].error_code == "INVALID_OPERATION_PLAN"


def test_task_creation_capability_query_remains_read_only_even_with_reference():
    source = plan(
        [node(action="FULL_INTERPRET", target="WELL", parameters={})],
        input_classification="CAPABILITY_QUERY",
    )
    result = PlanValidator().validate(source)
    assert result.outcome == "READ_ONLY"
    assert result.read_only
    assert result.plan is None


@pytest.mark.parametrize(
    "mode,action,constraint",
    [
        ("CREATE_VERSION", "MODIFY_PARAMETER", "DO_NOT_PERSIST"),
        ("PREVIEW", "COMMIT_SCENARIO", None),
        ("CREATE_VERSION", "FULL_INTERPRET", "NO_FULL_RERUN"),
    ],
)
def test_persistence_and_full_rerun_conflicts(mode, action, constraint):
    task_reference = None if action == "FULL_INTERPRET" else {"kind": "TASK_ID", "value": "A"}
    source = plan(
        [
            node(
                action=action,
                task_reference=task_reference,
                constraints=[{"type": constraint}] if constraint else [],
            )
        ],
        persist_mode=mode,
    )
    result = PlanValidator(enabled_catalog()).validate(source)
    assert result.outcome == "NEED_CLARIFICATION"
    assert result.conflicts[0].error_code == "MULTI_OPERATION_CONFLICT"


@pytest.mark.parametrize(
    "excluded,expected",
    [
        (
            [{"kind": "INTERVAL", "interval_id": "a"}, {"kind": "INTERVAL", "interval_id": "b"}],
            "conflict",
        ),
        ([{"kind": "INTERVAL", "interval_id": "a"}], "valid"),
        ([{"kind": "DEPTH_RANGE", "top": 1, "bottom": 2, "depth_reference": "MD"}], "uncertain"),
    ],
)
def test_only_scope_exclusion_without_guessing(excluded, expected):
    source = plan(
        [
            node(
                scope={"kind": "MULTI_INTERVAL", "interval_ids": ["a", "b"]},
                constraints=[
                    {"type": "ONLY_SCOPE"},
                    *[{"type": "EXCLUDE_SCOPE", "scope": scope} for scope in excluded],
                ],
            )
        ]
    )
    result = PlanValidator(enabled_catalog()).validate(source)
    if expected == "conflict":
        assert result.conflicts[0].error_code == "MULTI_OPERATION_CONFLICT"
    elif expected == "uncertain":
        assert result.missing_slots[0].slot == "CONFLICT_RESOLUTION"
    else:
        assert result.outcome == "EXECUTABLE"


async def test_whole_plan_no_execution_before_compare_clarification(monkeypatch):
    calls = []
    for owner, method in [
        (TaskCommands, "prepare_modify"),
        (TaskCommands, "prepare_full_rerun"),
        (InterpretationTaskService, "prepare_initial_with_input"),
        (InterpretationTaskService, "execute_prepared"),
        (InProcessExecutionDispatcher, "submit"),
        (InMemoryTaskRepository, "create_execution"),
    ]:
        guard = AsyncMock(side_effect=AssertionError("planning called execution layer"))
        monkeypatch.setattr(owner, method, guard)
        calls.append(guard)
    repository = InMemoryTaskRepository()
    before = await repository.list_executions("A")
    source = plan([node(), node("op2", "COMPARE", input_refs=[{"operation_id": "op1"}])])
    result = PlanValidator(enabled_catalog()).validate(source)
    assert result.outcome == "NEED_CLARIFICATION"
    assert result.plan is None
    assert result.missing_slots[0].error_code == "COMPARE_TARGET_REQUIRED"
    assert await repository.list_executions("A") == before == []
    for guard in calls:
        guard.assert_not_called()
