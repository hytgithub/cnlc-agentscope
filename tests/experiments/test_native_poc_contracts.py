"""Task 012A 离线 Toolkit、Skill、Mock Contract 与隔离性测试。"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
from agentscope.credential import DashScopeCredential
from agentscope.message import AssistantMsg, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import ToolChunk

from experiments.agentscope_native_poc.agent import (
    SYSTEM_PROMPT,
    ExperimentConfig,
    build_agent,
    build_toolkit,
)
from experiments.agentscope_native_poc.grounding import (
    evaluate_response_grounding,
    parameter_expectation_matches,
)
from experiments.agentscope_native_poc.runner import (
    _actual_tool_calls,
    _agent_audit,
    _illegal_write_attempts,
    _skill_read_audit,
    evaluate_case,
    run_ab,
)
from experiments.agentscope_native_poc.state import MockState

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POC_ROOT = PROJECT_ROOT / "experiments" / "agentscope_native_poc"


def _tool_map(toolkit):
    return {tool.name: tool for group in toolkit.tool_groups for tool in group.tools}


@pytest.mark.asyncio
async def test_toolkit_has_same_mock_tools_and_skill_is_the_only_config_difference():
    no_skill = build_toolkit(ExperimentConfig.NO_SKILL, MockState.fixture())
    with_skill = build_toolkit(ExperimentConfig.WITH_SKILL, MockState.fixture())

    no_skill_schemas = await no_skill.get_tool_schemas()
    with_skill_schemas = await with_skill.get_tool_schemas()
    business_names = {
        "get_current_context",
        "start_full_interpretation",
        "query_interpretation_result",
        "preflight_modify_parameter",
        "apply_parameter_change",
        "compare_result_versions",
        "read_report",
        "confirm_stage",
    }
    no_skill_business = {
        schema["function"]["name"]: schema
        for schema in no_skill_schemas
        if schema["function"]["name"] in business_names
    }
    with_skill_business = {
        schema["function"]["name"]: schema
        for schema in with_skill_schemas
        if schema["function"]["name"] in business_names
    }
    assert set(no_skill_business) == business_names
    assert set(with_skill_business) == business_names
    assert no_skill_business == with_skill_business

    no_skill_schemas_by_name = {
        schema["function"]["name"]: schema for schema in no_skill_schemas
    }
    with_skill_schemas_by_name = {
        schema["function"]["name"]: schema for schema in with_skill_schemas
    }
    assert "Skill" not in no_skill_schemas_by_name
    skill_schema = with_skill_schemas_by_name["Skill"]["function"]
    assert skill_schema["name"] == "Skill"
    assert skill_schema["parameters"]["properties"]["skill"]["type"] == "string"
    assert skill_schema["parameters"]["required"] == ["skill"]

    assert await no_skill.get_skill_instructions() is None
    skill_instructions = await with_skill.get_skill_instructions()
    assert skill_instructions is not None
    for name in (
        "full-interpretation",
        "query-and-compare",
        "modify-and-rerun",
        "stage-control",
    ):
        assert name in skill_instructions

    assert "POR" not in SYSTEM_PROMPT and "W01" not in SYSTEM_PROMPT


def test_agent_prompt_is_identical_for_both_configurations_without_model_calls():
    model = ChatModelBase(
        credential=DashScopeCredential(name="offline-test", api_key="not-a-real-key"),
        model="qwen-plus",
        parameters=ChatModelBase.Parameters(),
        stream=False,
    )
    no_skill = build_agent(model, ExperimentConfig.NO_SKILL, MockState.fixture())
    with_skill = build_agent(model, ExperimentConfig.WITH_SKILL, MockState.fixture())

    assert no_skill._system_prompt == with_skill._system_prompt == SYSTEM_PROMPT
    assert no_skill.model is with_skill.model is model


@pytest.mark.asyncio
async def test_skill_registration_and_metadata_visibility_are_not_viewer_calls():
    model = ChatModelBase(
        credential=DashScopeCredential(name="offline-test", api_key="not-a-real-key"),
        model="qwen-plus",
        parameters=ChatModelBase.Parameters(),
        stream=False,
    )
    no_skill = build_agent(model, ExperimentConfig.NO_SKILL, MockState.fixture())
    with_skill = build_agent(model, ExperimentConfig.WITH_SKILL, MockState.fixture())

    no_skill_audit = await _agent_audit(no_skill, ExperimentConfig.NO_SKILL)
    with_skill_audit = await _agent_audit(with_skill, ExperimentConfig.WITH_SKILL)

    assert no_skill_audit["registered_skill_names"] == []
    assert no_skill_audit["skill_metadata_visible"] == []
    assert with_skill_audit["registered_skill_names"] == [
        "full-interpretation",
        "modify-and-rerun",
        "query-and-compare",
        "stage-control",
    ]
    assert {item["name"] for item in with_skill_audit["skill_metadata_visible"]} == set(
        with_skill_audit["registered_skill_names"]
    )
    assert no_skill_audit["base_system_prompt_sha256"] == with_skill_audit[
        "base_system_prompt_sha256"
    ]
    assert no_skill_audit["business_tool_schema_sha256"] == with_skill_audit[
        "business_tool_schema_sha256"
    ]


@pytest.mark.asyncio
async def test_skillviewer_reads_all_registered_skills():
    toolkit = build_toolkit(ExperimentConfig.WITH_SKILL)
    viewer = toolkit.builtin_skill_viewer.tool
    state = AgentState()
    for skill_name in (
        "full-interpretation",
        "query-and-compare",
        "modify-and-rerun",
        "stage-control",
    ):
        result = await viewer.call(skill=skill_name, _agent_state=state)
        assert isinstance(result, ToolChunk)
        markdown = result.content[0].text
        assert "#" in markdown
        assert len(markdown) > 80


@pytest.mark.asyncio
async def test_all_mock_tool_schemas_and_call_records_follow_contract():
    state = MockState.fixture()
    tools = _tool_map(build_toolkit(ExperimentConfig.NO_SKILL, state))
    expected_names = [
        "get_current_context",
        "start_full_interpretation",
        "query_interpretation_result",
        "preflight_modify_parameter",
        "apply_parameter_change",
        "compare_result_versions",
        "read_report",
        "confirm_stage",
    ]
    assert set(expected_names) == set(tools)
    schemas = await build_toolkit(ExperimentConfig.NO_SKILL, state).get_tool_schemas()
    schema_by_name = {schema["function"]["name"]: schema for schema in schemas}
    for tool_name in expected_names:
        schema = schema_by_name[tool_name]
        assert schema["function"]["name"] == tool_name
        assert schema["function"]["parameters"]["type"] == "object"

    context_chunk = await tools["get_current_context"].call()
    assert isinstance(context_chunk, ToolChunk)
    assert context_chunk.metadata["result"]["source"] == "task_012a_test_fixture"
    started = await tools["start_full_interpretation"].call(well_id="WELL-A")
    queried = await tools["query_interpretation_result"].call(
        execution_id="V2", scope="2035-2038m"
    )
    preflight = await tools["preflight_modify_parameter"].call(
        target="孔隙度", value=0.16, scope="whole_well"
    )
    applied = await tools["apply_parameter_change"].call(
        target="POROSITY", value=0.16, scope="whole_well"
    )
    compared = await tools["compare_result_versions"].call(
        left_execution_id="V1", right_execution_id="V2"
    )
    report = await tools["read_report"].call(selector="PREVIOUS")
    confirmed = await tools["confirm_stage"].call()
    assert started.metadata["result"]["status"] == "SIMULATED"
    assert queried.metadata["result"]["is_real_business_data"] is False
    assert preflight.metadata["result"]["status"] == "ALLOWED"
    assert applied.metadata["result"]["status"] == "SIMULATED"
    assert compared.metadata["result"]["status"] == "OK"
    assert report.metadata["result"]["execution_id"] == "V3"
    assert confirmed.metadata["result"]["status"] == "SIMULATED"
    assert [item.sequence for item in state.trace] == list(range(1, 9))
    assert [item.tool_name for item in state.trace] == expected_names
    assert len({item.call_id for item in state.trace}) == len(state.trace)
    assert state.trace[0].arguments == {}
    assert state.trace[0].result["is_real_business_data"] is False


@pytest.mark.asyncio
async def test_modification_requires_matching_successful_preflight():
    state = MockState.fixture()
    tools = _tool_map(build_toolkit(ExperimentConfig.NO_SKILL, state))

    rejected = await tools["apply_parameter_change"].call(
        target="POROSITY", value=0.16, scope="whole_well"
    )
    assert rejected.metadata["result"]["status"] == "REJECTED"
    assert state.current_execution_id == "V2"

    preflight = await tools["preflight_modify_parameter"].call(
        target="孔隙度", value=0.16, scope="whole_well"
    )
    assert preflight.metadata["result"]["status"] == "ALLOWED"
    applied = await tools["apply_parameter_change"].call(
        target="POROSITY", value=0.16, scope="whole_well"
    )
    assert applied.metadata["result"]["status"] == "SIMULATED"
    assert state.current_execution_id == "V3"
    assert [item.sequence for item in state.trace] == [1, 2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_status"),
    [
        ({"target": "POROSITY", "value": 0.16, "scope": "2035-2038m"}, "UNSUPPORTED"),
        ({"target": "RW", "value": 0.03}, "UNSUPPORTED"),
        ({"target": "POROSITY", "value": 0.16}, "NEED_CLARIFICATION"),
        ({"target": None, "value": 0.18}, "NEED_CLARIFICATION"),
    ],
)
async def test_unsupported_or_ambiguous_modifications_never_authorize_write(
    arguments, expected_status
):
    state = MockState.fixture()
    tools = _tool_map(build_toolkit(ExperimentConfig.NO_SKILL, state))
    result = await tools["preflight_modify_parameter"].call(**arguments)
    assert result.metadata["result"]["status"] == expected_status
    assert state.approved_change is None
    rejected = await tools["apply_parameter_change"].call(
        target="POROSITY", value=0.16, scope="whole_well"
    )
    assert rejected.metadata["result"]["status"] == "REJECTED"
    assert state.current_execution_id == "V2"


def test_fixed_cases_have_explicit_tool_expectations():
    cases = json.loads((POC_ROOT / "cases.json").read_text(encoding="utf-8"))["cases"]
    assert len(cases) == 16
    assert [case["case_id"] for case in cases] == [f"{number:02}" for number in range(1, 17)]
    assert all(
        {"expected_action", "allowed_tools", "forbidden_tools"} <= set(case)
        for case in cases
    )
    assert cases[1]["expected_action"] == "query_interpretation_result"
    assert "start_full_interpretation" in cases[1]["forbidden_tools"]
    assert cases[9]["expected_preflight_statuses"] == ["NEED_CLARIFICATION"]
    assert cases[10]["expected_action"] == "NO_TOOL"
    assert [case["case_id"] for case in cases if case.get("skill_required")] == [
        "13",
        "14",
        "15",
        "16",
    ]
    assert cases[4]["expected_preflight_statuses"] == ["NEED_CLARIFICATION"]
    assert cases[4]["clarification_required"] is True
    assert cases[13]["required_tool_sequence"] == [
        "get_current_context",
        "preflight_modify_parameter",
        "apply_parameter_change",
        "query_interpretation_result",
        "compare_result_versions",
    ]


def test_evaluator_enforces_preflight_order_and_out_of_domain_no_tool():
    modification_case = {
        "expected_action": "preflight_modify_parameter",
        "allowed_tools": ["preflight_modify_parameter", "apply_parameter_change"],
        "forbidden_tools": [],
    }
    valid_calls = [
        {
            "tool_name": "preflight_modify_parameter",
            "arguments": {},
            "result": {"status": "ALLOWED"},
        },
        {"tool_name": "apply_parameter_change", "arguments": {}, "result": {"status": "SIMULATED"}},
    ]
    assert evaluate_case(modification_case, valid_calls) == (True, None)
    assert not evaluate_case(modification_case, list(reversed(valid_calls)))[0]
    ambiguous_case = {
        "expected_action": "preflight_modify_parameter",
        "expected_preflight_statuses": ["NEED_CLARIFICATION"],
        "allowed_tools": ["preflight_modify_parameter"],
        "forbidden_tools": [],
    }
    wrong_preflight = [
        {
            "tool_name": "preflight_modify_parameter",
            "arguments": {"target": "cutoff_value"},
            "result": {"status": "UNSUPPORTED"},
        }
    ]
    assert not evaluate_case(ambiguous_case, wrong_preflight)[0]
    assert evaluate_case(
        {"expected_action": "NO_TOOL", "allowed_tools": [], "forbidden_tools": []},
        [],
    ) == (True, None)
    skill_for_out_of_domain = [
        {"tool_name": "Skill", "arguments": {"skill": "query-and-compare"}, "result": "..."}
    ]
    assert not evaluate_case(
        {"expected_action": "NO_TOOL", "allowed_tools": [], "forbidden_tools": []},
        skill_for_out_of_domain,
    )[0]


def test_evaluator_enforces_conditional_tool_sequences_and_stage_boundary():
    cases = json.loads((POC_ROOT / "cases.json").read_text(encoding="utf-8"))["cases"]
    case_13 = cases[12]
    calls_13 = [
        {"tool_name": "get_current_context", "arguments": {}, "result": {"status": "OK"}},
        {
            "tool_name": "query_interpretation_result",
            "arguments": {"scope": "2035-2038m"},
            "result": {"status": "OK", "fixture": True},
        },
    ]
    assert evaluate_case(case_13, calls_13) == (True, None)
    assert not evaluate_case(case_13, [calls_13[1]])[0]

    case_15 = cases[14]
    confirm_calls = [
        {
            "tool_name": "get_current_context",
            "arguments": {},
            "result": {"pending_stage": "intelligent_processing"},
        },
        {
            "tool_name": "confirm_stage",
            "arguments": {"stage": "intelligent_processing"},
            "result": {"status": "SIMULATED", "fixture": True},
        },
    ]
    assert evaluate_case(case_15, confirm_calls) == (True, None)
    wrong_stage_calls = [
        confirm_calls[0],
        {**confirm_calls[1], "arguments": {"stage": "report_generation"}},
    ]
    assert not evaluate_case(case_15, wrong_stage_calls)[0]


@pytest.mark.asyncio
async def test_model_runner_skips_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("CNLC_RUN_NATIVE_POC_MODEL", raising=False)
    assert await run_ab() == {
        "status": "SKIP",
        "reason": "CNLC_RUN_NATIVE_POC_MODEL 未设为 1。",
    }


def test_poc_source_does_not_import_production_execution_or_persistence():
    forbidden_modules = {
        "cnlc_agent.demo.task_tools",
        "cnlc_agent.demo.operation_execution_bridge",
        "cnlc_agent.workflows.stage_orchestrator",
        "sqlalchemy",
        "redis",
        "httpx",
    }
    for path in [*POC_ROOT.glob("*.py"), *(PROJECT_ROOT / "tests/experiments").glob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not (imported & forbidden_modules), path


def test_poc_import_does_not_load_production_task_runner():
    assert "cnlc_agent.demo.task_tools" not in sys.modules


@pytest.mark.parametrize(
    ("calls", "response", "expected_pass"),
    [
        (
            [
                {
                    "tool_name": "query_interpretation_result",
                    "arguments": {},
                    "result": {
                        "status": "OK",
                        "fixture": True,
                        "summary": "不包含专业计算值",
                    },
                }
            ],
            "这是测试 Fixture。当前孔隙度为 0.23。",
            False,
        ),
        (
            [
                {
                    "tool_name": "preflight_modify_parameter",
                    "arguments": {"target": "Rw", "value": 0.03},
                    "result": {"status": "UNSUPPORTED", "fixture": True},
                }
            ],
            "Task 012A Mock Fixture 中该参数未开放，本次未执行修改。",
            True,
        ),
        (
            [
                {
                    "tool_name": "preflight_modify_parameter",
                    "arguments": {"target": "Rw", "value": 0.03},
                    "result": {"status": "UNSUPPORTED", "fixture": True},
                }
            ],
            "Task 012A Mock Fixture 中该参数未开放，但我已成功修改。",
            False,
        ),
        (
            [
                {
                    "tool_name": "preflight_modify_parameter",
                    "arguments": {"target": "POROSITY", "value": 0.16},
                    "result": {"status": "NEED_CLARIFICATION", "fixture": True},
                }
            ],
            "这是模拟 Fixture；范围未提供，请明确全井还是层段，未执行修改。",
            True,
        ),
        (
            [
                {
                    "tool_name": "preflight_modify_parameter",
                    "arguments": {"target": "POROSITY", "value": 0.16},
                    "result": {
                        "status": "NEED_CLARIFICATION",
                        "fixture": True,
                        "message": "范围缺失",
                    },
                }
            ],
            "这是 Task 012A Mock Fixture。你要求将孔隙度修改为 0.16，但需要先补充范围。",
            True,
        ),
        (
            [
                {
                    "tool_name": "preflight_modify_parameter",
                    "arguments": {"target": "POROSITY", "value": 0.16},
                    "result": {"status": "NEED_CLARIFICATION", "fixture": True},
                }
            ],
            "Mock Fixture 范围不清，我会默认全井修改。",
            False,
        ),
        (
            [],
            "我已经查询了当前解释版本。",
            False,
        ),
        (
            [
                {
                    "tool_name": "query_interpretation_result",
                    "arguments": {},
                    "result": {
                        "status": "OK",
                        "fixture": True,
                        "execution_id": "V2",
                        "summary": "仅有测试 Fixture",
                    },
                }
            ],
            "V2 是 Task 012A Mock Fixture，不是真实测井结果；未返回岩性。",
            True,
        ),
    ],
)
def test_grounding_evaluator_covers_minimum_poc_boundaries(calls, response, expected_pass):
    result = evaluate_response_grounding(
        case={},
        calls=calls,
        response=response,
        authority_state={"current_execution_id": "V2", "known_execution_ids": ["V1", "V2"]},
    )
    assert result["grounding_pass"] is expected_pass


def test_grounding_evaluator_rejects_unreturned_lithology_and_unknown_version():
    calls = [
        {
            "tool_name": "query_interpretation_result",
            "arguments": {},
            "result": {"status": "OK", "fixture": True, "execution_id": "V2"},
        }
    ]
    result = evaluate_response_grounding(
        case={},
        calls=calls,
        response="Mock Fixture 查询结果：岩性为砂岩，来源版本 V9。",
        authority_state={"known_execution_ids": ["V1", "V2"]},
    )
    assert not result["grounding_pass"]
    assert any("岩性" in failure for failure in result["grounding_failures"])
    assert any("V9" in failure for failure in result["grounding_failures"])


def test_grounding_evaluator_rejects_unreturned_execution_status_for_known_version():
    result = evaluate_response_grounding(
        case={},
        calls=[
            {
                "tool_name": "query_interpretation_result",
                "arguments": {},
                "result": {"status": "OK", "fixture": True, "execution_id": "V2"},
            }
        ],
        response="Task 012A Mock Fixture：V2 版本已完成。",
        authority_state={"current_execution_id": "V2"},
    )
    assert not result["grounding_pass"]
    assert any("版本执行状态" in failure for failure in result["grounding_failures"])


def test_grounding_evaluator_accepts_tool_grounded_simulated_version_operations():
    result = evaluate_response_grounding(
        case={},
        calls=[
            {
                "tool_name": "apply_parameter_change",
                "arguments": {"target": "POROSITY", "value": 0.16},
                "result": {
                    "status": "SIMULATED",
                    "fixture": True,
                    "execution_id": "V3",
                },
            },
            {
                "tool_name": "compare_result_versions",
                "arguments": {"left_execution_id": "V2", "right_execution_id": "V3"},
                "result": {"status": "OK", "fixture": True},
            },
        ],
        response="Task 012A Mock Fixture 中已模拟修改，新版本为 V3，并已比较 V2 与 V3。",
        authority_state={"known_execution_ids": ["V1", "V2", "V3"]},
    )
    assert result["grounding_pass"]


def test_parameter_expectation_checks_target_value_and_explicit_missing_scope():
    case = {
        "parameter_expectation": {"target": "POROSITY", "value": 0.16, "scope": None}
    }
    calls = [
        {
            "tool_name": "preflight_modify_parameter",
            "arguments": {"target": "孔隙度", "value": 0.16},
            "result": {"status": "NEED_CLARIFICATION"},
        }
    ]
    assert parameter_expectation_matches(case, calls) is True
    calls[0]["arguments"]["scope"] = "whole_well"
    assert parameter_expectation_matches(case, calls) is False


def test_write_attempt_metric_normalizes_tool_parameter_aliases_but_flags_clarification_bypass():
    allowed_case = {"allowed_tools": ["preflight_modify_parameter", "apply_parameter_change"]}
    valid_calls = [
        {
            "tool_name": "preflight_modify_parameter",
            "arguments": {"target": "porosity", "value": 0.16, "scope": "whole_well"},
            "result": {
                "status": "ALLOWED",
                "target": "POROSITY",
                "value": 0.16,
                "scope": "whole_well",
            },
        },
        {
            "tool_name": "apply_parameter_change",
            "arguments": {"target": "POROSITY", "value": 0.16, "scope": "whole_well"},
            "result": {"status": "SIMULATED"},
        },
    ]
    assert _illegal_write_attempts(allowed_case, valid_calls) == 0
    ambiguous_case = {
        "allowed_tools": ["preflight_modify_parameter", "apply_parameter_change"],
        "clarification_required": True,
    }
    assert _illegal_write_attempts(ambiguous_case, valid_calls) == 1


def test_skill_call_step_and_returned_body_are_captured_as_model_visible_evidence():
    body = "# Stage method\nRead context before explicit confirmation."
    skill_call = ToolCallBlock(
        id="skill-call-1",
        name="Skill",
        input=json.dumps({"skill": "stage-control"}),
    )
    skill_result = ToolResultBlock(
        id="skill-call-1",
        name="Skill",
        output=[TextBlock(text=body)],
    )
    context_call = ToolCallBlock(id="context-call-1", name="get_current_context", input="{}")
    context_result = ToolResultBlock(
        id="context-call-1",
        name="get_current_context",
        output=[TextBlock(text='{"status":"OK","fixture":true}')],
    )
    state = MockState.fixture()
    state.record("get_current_context", {}, {"status": "OK", "fixture": True})
    agent = type(
        "AgentStub",
        (),
        {
            "state": type(
                "StateStub",
                (),
                {
                    "context": [
                        AssistantMsg(name="assistant", content=[skill_call]),
                        AssistantMsg(name="assistant", content=[skill_result]),
                        AssistantMsg(name="assistant", content=[context_call]),
                        AssistantMsg(name="assistant", content=[context_result]),
                    ]
                },
            )()
        },
    )()

    calls = _actual_tool_calls(agent, state)
    audit = _skill_read_audit([calls[0]], {"stage-control": body})
    assert calls[0]["tool_name"] == "Skill"
    assert calls[0]["sequence"] == 1
    assert calls[1]["tool_name"] == "get_current_context"
    assert audit[0]["step"] == 1
    assert audit[0]["body_returned_to_model"] is True
