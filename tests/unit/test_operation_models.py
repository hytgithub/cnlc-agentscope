"""操作契约的结构和往返测试；不把结构有效误认为已有执行能力。"""

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from cnlc_agent.demo.operation_models import (
    ActionType,
    DepthPointScope,
    DepthRangeScope,
    ExecutionReference,
    ExecutionReferenceKind,
    FilterSetScope,
    InputClassification,
    IntervalScope,
    MultiIntervalScope,
    OperationConstraint,
    OperationConstraintType,
    OperationEdge,
    OperationEdgeType,
    OperationInputReference,
    OperationNode,
    OperationPlan,
    OperationResolution,
    OperationScope,
    PersistMode,
    ResolutionConfidence,
    ResolutionOutcome,
    TargetType,
    ValueMode,
    ValueSpec,
)
from cnlc_agent.demo.task_context import TaskReference


@pytest.mark.parametrize(
    "enum,values",
    [
        (
            InputClassification,
            "EXECUTION_REQUEST READ_REQUEST CLARIFICATION_REPLY CORRECTION "
            "CONFIRMATION CANCELLATION CAPABILITY_QUERY META_REQUEST OUT_OF_DOMAIN",
        ),
        (
            ActionType,
            "QUERY EXPLAIN MODIFY_RESULT MODIFY_PARAMETER RECALCULATE REINTERPRET "
            "SWITCH_METHOD SWITCH_MODEL COMPARE SEGMENT_EDIT VALIDATE OVERRIDE RESTORE_VERSION "
            "SCENARIO COMMIT_SCENARIO REPORT HISTORY FULL_INTERPRET STATUS CANCEL_EXECUTION "
            "PAUSE_EXECUTION RESUME_EXECUTION RETRY_EXECUTION ACCEPT_RESULT REJECT_RESULT "
            "MARK_FINAL MARK_REVIEW NEW_WELL REPLACE_INPUT ADD_EVIDENCE",
        ),
    ],
)
def test_stable_input_and_action_serialization(enum, values):
    assert json.loads(json.dumps(list(enum))) == values.split()
    for value in values.split():
        assert enum(value).value == value


def test_single_plan_reuses_task_reference_and_separates_target_scope():
    reference = TaskReference(kind="TASK_ID", value="task-1")
    node = OperationNode(
        operation_id="op1",
        action=ActionType.QUERY,
        target=TargetType.POROSITY,
        task_reference=reference,
        execution_reference=ExecutionReference(kind=ExecutionReferenceKind.TASK_CURRENT),
        scope=DepthRangeScope(top=2035, bottom=2038, depth_reference="MD"),
    )
    plan = OperationPlan(
        input_classification=InputClassification.READ_REQUEST,
        operations=[node],
        persist_mode=PersistMode.PREVIEW,
        original_instruction="查询2035至2038米孔隙度",
    )
    assert node.task_reference is reference
    assert plan.model_dump(mode="json")["operations"][0]["target"] == "POROSITY"
    restored = OperationPlan.model_validate_json(plan.model_dump_json())
    assert restored == plan
    assert isinstance(restored.operations[0].scope, DepthRangeScope)
    with pytest.raises(ValidationError):
        OperationNode.model_validate({**node.model_dump(), "target": "2035-2038孔隙度"})


def test_compound_plan_keeps_operation_outputs_distinct_from_executions():
    payload = {
        "input_classification": "EXECUTION_REQUEST",
        "shared_context": {
            "task_reference": {"kind": "CURRENT"},
            "execution_reference": {"kind": "ACTIVE_BASE"},
            "scope": {"kind": "INTERVAL", "interval_id": "interval-stable-5"},
        },
        "operations": [
            {
                "operation_id": "op1",
                "action": "MODIFY_RESULT",
                "target": "POROSITY",
                "parameters": {"value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "1"}},
                "output_alias": "edited_porosity",
            },
            {
                "operation_id": "op2",
                "action": "RECALCULATE",
                "target": "WELL",
                "input_refs": [{"operation_id": "op1"}],
            },
            {
                "operation_id": "op3",
                "action": "COMPARE",
                "target": "EXECUTION",
                "input_refs": [
                    {"operation_id": "op2"},
                    {"execution_reference": {"kind": "PREVIOUS"}},
                ],
            },
        ],
        "edges": [
            {"from_operation_id": "op1", "to_operation_id": "op2", "type": "DATA_DEPENDENCY"},
            {"from_operation_id": "op2", "to_operation_id": "op3", "type": "COMPARE_DEPENDENCY"},
        ],
        "persist_mode": "CREATE_VERSION",
        "output_requirement": {"description": "返回比较结果", "operation_ids": ["op3"]},
        "original_instruction": "把第5层孔隙度改成0.16，重新算一下，再和上一版比较。",
    }
    plan = OperationPlan.model_validate(payload)
    assert OperationPlan.model_validate_json(plan.model_dump_json()) == plan
    assert [node.action for node in plan.operations] == [
        ActionType.MODIFY_RESULT,
        ActionType.RECALCULATE,
        ActionType.COMPARE,
    ]
    assert plan.operations[1].input_refs[0].execution_reference is None
    assert "execution_id" not in OperationNode.model_fields
    # 这里只接受已解析的稳定标识示例，不实现“第5层”的解析。
    assert plan.shared_context.scope.interval_id == "interval-stable-5"


@pytest.mark.parametrize("kind", list(ExecutionReferenceKind))
def test_execution_reference_round_trip(kind):
    payload = {"kind": kind}
    if kind == ExecutionReferenceKind.SEQUENCE:
        payload["sequence"] = 3
    if kind == ExecutionReferenceKind.EXECUTION_ID:
        payload["execution_id"] = "execution-xxx"
    reference = ExecutionReference.model_validate(payload)
    assert ExecutionReference.model_validate_json(reference.model_dump_json()) == reference
    assert reference.kind.value == kind.value


@pytest.mark.parametrize("sequence", [0, -1, 1.5, "3", True, None])
def test_execution_sequence_rejects_invalid_values(sequence):
    with pytest.raises(ValidationError):
        ExecutionReference(kind="SEQUENCE", sequence=sequence)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "EXECUTION_ID"},
        {"kind": "EXECUTION_ID", "execution_id": " "},
        {"kind": "SEQUENCE", "sequence": 3, "execution_id": "exec"},
        {"kind": "TASK_CURRENT", "sequence": 3},
        {"kind": "ACTIVE_BASE", "execution_id": "exec"},
    ],
)
def test_execution_reference_rejects_conflicting_selectors(payload):
    with pytest.raises(ValidationError):
        ExecutionReference.model_validate(payload)


def test_current_version_is_not_active_base():
    current = ExecutionReference(kind="TASK_CURRENT")
    base = ExecutionReference(kind="ACTIVE_BASE")
    assert current != base
    assert current.model_dump(mode="json")["kind"] == "TASK_CURRENT"
    assert base.model_dump(mode="json")["kind"] == "ACTIVE_BASE"


@pytest.mark.parametrize("top,bottom", [(2, 2), (3, 2), (float("nan"), 3), (2, float("inf"))])
def test_invalid_depth_ranges_are_rejected(top, bottom):
    with pytest.raises(ValidationError):
        DepthRangeScope(top=top, bottom=bottom, depth_reference="MD")


@pytest.mark.parametrize("reference", ["MD", "TVD", "TVDSS"])
def test_depth_point_and_signed_ranges_preserve_reference(reference):
    point = DepthPointScope(depth=-5, depth_reference=reference)
    interval = DepthRangeScope(top=-5, bottom=5, depth_reference=reference)
    assert point.depth_reference == interval.depth_reference == reference
    assert point.unit == interval.unit == "m"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "WHOLE_WELL", "interval_id": "layer-1"},
        {"kind": "INTERVAL", "layer_number": 5},
        {"kind": "DEPTH_POINT", "depth": 5},
        {"kind": "DEPTH_RANGE", "top": 1, "bottom": 3, "depth_reference": "UNKNOWN"},
    ],
)
def test_scope_discriminator_rejects_mixed_or_incomplete_fields(payload):
    with pytest.raises(ValidationError):
        TypeAdapter(OperationScope).validate_python(payload)


@pytest.mark.parametrize("ids", [[], [""], [" "]])
def test_multi_interval_requires_nonempty_stable_ids(ids):
    with pytest.raises(ValidationError):
        MultiIntervalScope(interval_ids=ids)


def test_all_scope_variants_round_trip_and_filter_resolution_is_explicit():
    adapter = TypeAdapter(OperationScope)
    scopes = [
        {"kind": "WHOLE_WELL"},
        {"kind": "INTERVAL", "interval_id": "i1"},
        {"kind": "MULTI_INTERVAL", "interval_ids": ["i1", "i2"]},
        {"kind": "DEPTH_RANGE", "top": 2, "bottom": 3, "depth_reference": "MD"},
        {"kind": "DEPTH_POINT", "depth": 2, "depth_reference": "TVD"},
        {"kind": "FILTER_SET", "filter_expression": "所有水层", "resolved_ids": ["i1", "i3"]},
    ]
    for payload in scopes:
        scope = adapter.validate_python(payload)
        assert adapter.validate_json(adapter.dump_json(scope)) == scope
    assert FilterSetScope(filter_expression="所有水层").resolved_ids is None
    assert FilterSetScope(filter_expression="所有水层", resolved_ids=[]).resolved_ids == []


def test_absolute_delta_and_percent_change_are_distinct():
    values = [
        ValueSpec(mode="ABSOLUTE", value=16, unit="%"),
        ValueSpec(mode="DELTA", value=2, unit="percentage_point"),
        ValueSpec(mode="PERCENT_CHANGE", value=2, unit="%"),
    ]
    assert [value.mode for value in values] == list(ValueMode)
    assert len({value.model_dump_json() for value in values}) == 3
    assert ValueSpec(mode="DELTA", value=-2, unit="percentage_point").value == -2


@pytest.mark.parametrize("value,unit", [(float("inf"), "%"), (True, "%"), ("16", "%"), (16, " ")])
def test_values_require_finite_numbers_and_explicit_units(value, unit):
    with pytest.raises(ValidationError):
        ValueSpec(mode="ABSOLUTE", value=value, unit=unit)


def test_constraints_compose_and_carry_excluded_objects():
    constraints = [
        OperationConstraint(type=kind)
        for kind in OperationConstraintType
        if kind
        not in {OperationConstraintType.EXCLUDE_MODEL, OperationConstraintType.EXCLUDE_SCOPE}
    ] + [
        OperationConstraint(type="EXCLUDE_MODEL", model_id="prediction-v1"),
        OperationConstraint(type="EXCLUDE_SCOPE", scope=IntervalScope(interval_id="i9")),
    ]
    node = OperationNode(
        operation_id="op1",
        action="RECALCULATE",
        target="POROSITY",
        constraints=constraints,
    )
    assert len(node.constraints) == 7
    assert OperationNode.model_validate_json(node.model_dump_json()) == node
    for payload in (
        {"type": "EXCLUDE_MODEL"},
        {"type": "EXCLUDE_SCOPE"},
        {"type": "NO_FULL_RERUN", "model_id": "model"},
    ):
        with pytest.raises(ValidationError):
            OperationConstraint.model_validate(payload)


@pytest.mark.parametrize("edge_type", list(OperationEdgeType))
def test_edges_require_structured_nonempty_endpoints(edge_type):
    edge = OperationEdge(from_operation_id="op1", to_operation_id="op2", type=edge_type)
    assert OperationEdge.model_validate_json(edge.model_dump_json()) == edge
    with pytest.raises(ValidationError):
        OperationEdge(from_operation_id="", to_operation_id="op2", type=edge_type)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation_id": "op1", "execution_reference": {"kind": "FIRST"}},
        {"operation_id": "op1", "task_reference": {"kind": "CURRENT"}},
    ],
)
def test_input_reference_rejects_ambiguous_sources(payload):
    with pytest.raises(ValidationError):
        OperationInputReference.model_validate(payload)


def test_capability_question_and_conditions_are_data_only():
    plan = OperationPlan(
        input_classification="CAPABILITY_QUERY",
        persist_mode="PREVIEW",
        original_instruction="你能只重新算Sw吗？",
        operations=[
            OperationNode(operation_id="op1", action="RECALCULATE", target="WATER_SATURATION")
        ],
        conditions=[{"expression": "如果已有结果可用", "operation_ids": ["op1"]}],
    )
    result = OperationResolution(
        outcome=ResolutionOutcome.KNOWN_UNSUPPORTED,
        confidence=ResolutionConfidence.HIGH,
        plan=plan,
        warnings=["仅表达能力询问，不发起重算"],
    )
    assert OperationResolution.model_validate_json(result.model_dump_json()) == result
    assert plan.operations[0].task_reference is None  # 不猜当前任务。
    for outcome in ResolutionOutcome:
        assert OperationResolution(outcome=outcome, confidence="LOW").plan is None
    with pytest.raises(ValidationError):
        OperationPlan.model_validate({**plan.model_dump(), "persist_mode": "OVERRIDE_CURRENT"})


def test_schema_exports_discriminated_scopes_and_reused_task_reference():
    schema = OperationPlan.model_json_schema()
    assert schema["$defs"]["TaskReference"] == TaskReference.model_json_schema()
    scope = schema["$defs"]["OperationNode"]["properties"]["scope"]["anyOf"][0]
    assert scope["discriminator"]["propertyName"] == "kind"
    assert len(scope["oneOf"]) == 6
    assert set(PersistMode) == {PersistMode.PREVIEW, PersistMode.CREATE_VERSION}
