"""能力默认范围及业务开关测试；现有 Tool 的行为仍由原有测试保护。"""

import pytest
from pydantic import ValidationError

from cnlc_agent.demo.operation_capabilities import (
    OperationCapability,
    OperationCapabilityStatus,
    OperationCatalog,
)
from cnlc_agent.demo.operation_models import ActionType, OperationPlan, OperationScopeKind
from cnlc_agent.domain.override import InterpretationOverride


@pytest.mark.parametrize("status", list(OperationCapabilityStatus))
def test_only_enabled_capability_is_eligible(status):
    capability = OperationCapability(action="COMPARE", status=status, description="比较版本")
    catalog = OperationCatalog([capability])
    assert capability.is_executable is (status == OperationCapabilityStatus.ENABLED)
    assert catalog.is_executable(ActionType.COMPARE) is capability.is_executable
    assert OperationCapability.model_validate_json(capability.model_dump_json()) == capability


def test_capability_is_unverified_by_default_and_read_only():
    capability = OperationCapability(action="OVERRIDE", description="人工覆盖待业务确认")
    assert capability.status == OperationCapabilityStatus.UNVERIFIED
    assert not capability.is_executable
    with pytest.raises(ValidationError):
        capability.status = OperationCapabilityStatus.ENABLED


def test_default_catalog_covers_every_recognizable_action_once():
    capabilities = OperationCatalog().list_capabilities()
    assert len(capabilities) == len(ActionType)
    assert {item.action for item in capabilities} == set(ActionType)
    assert all(item.business_note for item in capabilities)
    assert not any(item.status == OperationCapabilityStatus.DISABLED for item in capabilities)


def test_enabled_defaults_match_existing_task_tools_and_override_contract():
    from cnlc_agent.demo.task_tools import build_task_tools

    catalog = OperationCatalog()
    enabled = {item.action: item for item in catalog.list_capabilities() if item.is_executable}
    assert set(enabled) == {
        ActionType.FULL_INTERPRET,
        ActionType.FULL_RERUN,
        ActionType.STATUS,
        ActionType.REPORT,
        ActionType.MODIFY_PARAMETER,
    }
    tool_names = {tool.name for tool in build_task_tools()}
    assert {item.handler_name for item in enabled.values()} <= tool_names
    assert all(item.allowed_scopes == (OperationScopeKind.WHOLE_WELL,) for item in enabled.values())
    assert set(enabled[ActionType.MODIFY_PARAMETER].supported_parameters) == set(
        InterpretationOverride.model_fields
    )
    assert "仅已有报告读取" in enabled[ActionType.REPORT].business_note


def test_unsupported_actions_remain_recognizable_without_handlers():
    catalog = OperationCatalog()
    for action in (
        ActionType.COMPARE,
        ActionType.SEGMENT_EDIT,
        ActionType.OVERRIDE,
        ActionType.SCENARIO,
        ActionType.REPLACE_INPUT,
        ActionType.ADD_EVIDENCE,
    ):
        capability = catalog.get(action)
        assert capability.status == OperationCapabilityStatus.UNVERIFIED
        assert capability.handler_name is None
        assert not capability.is_executable
    for action in (
        ActionType.RECALCULATE,
        ActionType.PAUSE_EXECUTION,
        ActionType.HISTORY,
        ActionType.NEW_WELL,
    ):
        assert catalog.get(action).status == OperationCapabilityStatus.NOT_IMPLEMENTED
        assert not catalog.is_executable(action)


def test_business_switch_changes_no_schema_or_other_catalog():
    catalog = OperationCatalog()
    schema = OperationPlan.model_json_schema()
    plan = OperationPlan.model_validate(
        {
            "input_classification": "EXECUTION_REQUEST",
            "persist_mode": "CREATE_VERSION",
            "original_instruction": "人工覆盖最终层类型",
            "operations": [
                {"operation_id": "op1", "action": "OVERRIDE", "target": "ZONE_CLASSIFICATION"}
            ],
        }
    )
    serialized = plan.model_dump_json()
    disabled = catalog.with_status(ActionType.OVERRIDE, OperationCapabilityStatus.DISABLED)
    assert not disabled.is_executable(ActionType.OVERRIDE)
    assert disabled.get(ActionType.OVERRIDE).status == OperationCapabilityStatus.DISABLED
    assert catalog.get(ActionType.OVERRIDE).status == OperationCapabilityStatus.UNVERIFIED
    planned = catalog.with_status(ActionType.COMPARE, OperationCapabilityStatus.NOT_IMPLEMENTED)
    enabled = planned.with_status(ActionType.COMPARE, OperationCapabilityStatus.ENABLED)
    assert enabled.is_executable(ActionType.COMPARE)
    assert not planned.is_executable(ActionType.COMPARE)
    assert OperationPlan.model_json_schema() == schema
    assert OperationPlan.model_validate_json(serialized) == plan
    # 已开放动作也可关闭；无真实 Tool 注册或运行状态发生变化。
    assert not catalog.with_status(
        ActionType.STATUS, OperationCapabilityStatus.DISABLED
    ).is_executable(ActionType.STATUS)


def test_empty_duplicate_missing_and_unknown_catalog_entries_fail_safely():
    empty = OperationCatalog([])
    assert empty.list_capabilities() == ()
    assert not empty.is_executable(ActionType.STATUS)
    with pytest.raises(KeyError):
        empty.get(ActionType.STATUS)
    entry = OperationCatalog().get(ActionType.STATUS)
    with pytest.raises(ValueError, match="duplicate"):
        OperationCatalog([entry, entry])
    with pytest.raises(ValueError):
        OperationCatalog().get("MADE_UP_ACTION")
    with pytest.raises(ValidationError):
        OperationCatalog().with_status(ActionType.STATUS, "MADE_UP_STATUS")


def test_full_rerun_is_enabled_separately_from_reinterpret():
    catalog = OperationCatalog()
    capability = catalog.get(ActionType.FULL_RERUN)
    assert capability.status == "ENABLED"
    assert capability.handler_name == "rerun_well_interpretation"
    assert capability.allowed_scopes == (OperationScopeKind.WHOLE_WELL,)
    assert not capability.supported_parameters
    assert catalog.get(ActionType.REINTERPRET).status == "NOT_IMPLEMENTED"
