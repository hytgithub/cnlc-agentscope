"""Task 012A 离线 Toolkit、Skill、Mock Contract 与隔离性测试。"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
from agentscope.credential import DashScopeCredential
from agentscope.model import ChatModelBase
from agentscope.state import AgentState
from agentscope.tool import ToolChunk

from experiments.agentscope_native_poc.agent import (
    SYSTEM_PROMPT,
    ExperimentConfig,
    build_agent,
    build_toolkit,
)
from experiments.agentscope_native_poc.runner import evaluate_case, run_ab
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

    assert await no_skill.get_skill_instructions() is None
    skill_instructions = await with_skill.get_skill_instructions()
    assert skill_instructions is not None
    for name in ("full-interpretation", "query-and-compare", "modify-and-rerun"):
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
async def test_skillviewer_reads_all_three_registered_skills():
    toolkit = build_toolkit(ExperimentConfig.WITH_SKILL)
    viewer = toolkit.builtin_skill_viewer.tool
    state = AgentState()
    for skill_name in ("full-interpretation", "query-and-compare", "modify-and-rerun"):
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
    assert len(cases) == 12
    assert [case["case_id"] for case in cases] == [f"{number:02}" for number in range(1, 13)]
    assert all(
        {"expected_action", "allowed_tools", "forbidden_tools"} <= set(case)
        for case in cases
    )
    assert cases[1]["expected_action"] == "query_interpretation_result"
    assert "start_full_interpretation" in cases[1]["forbidden_tools"]
    assert cases[9]["expected_preflight_statuses"] == ["NEED_CLARIFICATION"]
    assert cases[10]["expected_action"] == "NO_TOOL"


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
