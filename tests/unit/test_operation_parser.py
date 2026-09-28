"""结构化输入严格性与 Partial/Complete 分界。"""

import json

import pytest
from pydantic import ValidationError

from cnlc_agent.demo.operation_models import OperationNode, OperationPlan
from cnlc_agent.demo.operation_parser import StructuredOperationParser, finalize_partial_plan


def payload():
    return {
        "input_classification": "EXECUTION_REQUEST",
        "original_instruction": "改成0.16",
        "operations": [
            {
                "operation_id": "change",
                "action": "MODIFY_PARAMETER",
                "parameters": {"value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "fraction"}},
            }
        ],
    }


def test_partial_json_and_missing_slots_without_guessing():
    parsed = StructuredOperationParser.parse(json.dumps(payload()))
    result = finalize_partial_plan(parsed)
    assert result.plan is None
    assert {issue.slot for issue in result.issues} == {"TARGET", "PERSIST_MODE"}
    assert parsed.operations[0].parameters.value.value == 0.16
    assert parsed.operations[0].target is None
    data = payload()
    data["persist_mode"] = "CREATE_VERSION"
    data["operations"][0]["target"] = "POROSITY"
    result = finalize_partial_plan(StructuredOperationParser.parse(data))
    assert isinstance(result.plan, OperationPlan)
    assert result.issues == []


@pytest.mark.parametrize(
    "location,key,value",
    [
        ("plan", "unknown", True),
        ("plan", "input_classification", "MAGIC"),
        ("node", "target", "UNKNOWN"),
        ("node", "action", "MODIFY"),
        ("node", "trusted", True),
        ("plan", "locked_references", []),
        ("node", "unknown", 1),
    ],
)
def test_unknown_fields_and_enums_rejected(location, key, value):
    data = payload()
    (data if location == "plan" else data["operations"][0])[key] = value
    with pytest.raises(ValidationError):
        StructuredOperationParser.parse(data)


@pytest.mark.parametrize(
    "extensions",
    [
        {"target": "POROSITY"},
        {"value": "bad"},
        {"trusted": True},
        {"nested": [{"locked_references": []}]},
    ],
)
def test_extensions_cannot_shadow_formal_slots(extensions):
    data = payload()
    data["operations"][0]["parameters"] = {"extensions": extensions}
    with pytest.raises(ValidationError):
        StructuredOperationParser.parse(data)


def test_extensions_remain_opaque_and_cannot_supply_value():
    data = payload()
    data["operations"][0]["parameters"] = {"extensions": {"unconfirmed_note": "0.16"}}
    result = finalize_partial_plan(StructuredOperationParser.parse(data))
    assert "VALUE" in {issue.slot for issue in result.issues}


def test_complete_contract_stays_strict_and_unknown_action_is_not_pending():
    data = payload()
    with pytest.raises(ValidationError):
        OperationPlan.model_validate(data)
    with pytest.raises(ValidationError):
        OperationNode.model_validate(data["operations"][0])
    del data["operations"][0]["action"]
    with pytest.raises(ValidationError):
        StructuredOperationParser.parse(data)
    with pytest.raises(ValidationError):
        StructuredOperationParser.parse("把孔隙度改成0.16")
