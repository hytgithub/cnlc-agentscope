"""Task 012C 的原生 Task Tools 与隔离业务 Fixture 合同测试。"""

from __future__ import annotations

import json

import pytest
from agentscope.message import ToolCallBlock
from agentscope.state import AgentState, Task
from agentscope.tool import TaskCreate, TaskGet, TaskList, TaskUpdate, Toolkit

from experiments.agentscope_native_poc.plan_runner import (
    BUSINESS_TOOLS,
    CASES,
    PROMPT,
    PlanState,
    _schema_digest,
    build_agent,
    evaluate,
    native_tool_smoke,
    review_record,
)
from experiments.agentscope_native_poc.plan_runner import (
    summarize as summarize_plan,
)


@pytest.mark.asyncio
async def test_current_venv_native_task_tools_register_and_inject_state() -> None:
    result = await native_tool_smoke()
    assert result["toolkit_registration"] == ["TaskCreate", "TaskGet", "TaskList", "TaskUpdate"]
    assert result["agent_state_injection"] is True
    assert result["crud_smoke"] == [("1", "in_progress")]


@pytest.mark.asyncio
async def test_ab_business_tool_schema_and_base_prompt_are_equal() -> None:
    class NeverCalledModel:
        pass

    # 只比较 Toolkit schema，模型不会被调用；显式验证两组共用相同 Prompt。
    plain = build_agent(NeverCalledModel(), PlanState(), False)  # type: ignore[arg-type]
    plan = build_agent(NeverCalledModel(), PlanState(), True)  # type: ignore[arg-type]
    assert plain._system_prompt == plan._system_prompt == PROMPT
    assert await _schema_digest(plain, task_tools=False) == await _schema_digest(
        plan, task_tools=False
    )
    plain_names = {schema["function"]["name"] for schema in await plain.toolkit.get_tool_schemas()}
    plan_names = {schema["function"]["name"] for schema in await plan.toolkit.get_tool_schemas()}
    assert plan_names - plain_names == {"TaskCreate", "TaskGet", "TaskList", "TaskUpdate"}


def test_task_context_is_not_business_authority() -> None:
    state = AgentState()
    original = ("TASK-A", "WELL-A", "A-V2", 2)
    fixture = PlanState()
    state.tasks_context.tasks.append(
        Task(
            id="1",
            subject="query",
            description="compare",
            metadata={"execution_id": "FAKE"},
        ),
    )
    assert (fixture.task_id, fixture.well_id, fixture.execution_id, fixture.revision) == original
    assert {tool.name for tool in (item(fixture) for item in BUSINESS_TOOLS)} == {
        "get_authority_context",
        "switch_session_well",
        "query_authority_result",
        "compare_result_versions",
        "preflight_modify_parameter",
        "apply_parameter_change",
    }
    schemas = [tool.input_schema for tool in (TaskCreate(), TaskGet(), TaskList(), TaskUpdate())]
    assert all(
        not {"execution_id", "scope", "version"}.intersection(schema.get("properties", {}))
        for schema in schemas
    )


@pytest.mark.asyncio
async def test_task_tool_crud_uses_agent_state_only() -> None:
    state = AgentState()
    toolkit = Toolkit(tools=[TaskCreate(), TaskGet(), TaskList(), TaskUpdate()])
    for name, arguments in (
        ("TaskCreate", {"subject": "query", "description": "then compare"}),
        ("TaskUpdate", {"task_id": "1", "status": "in_progress"}),
        ("TaskList", {}),
        ("TaskGet", {"task_id": "1"}),
    ):
        call = ToolCallBlock(id=name, name=name, input=json.dumps(arguments))
        result = [chunk async for chunk in toolkit.call_tool(call, state)][-1]
        assert result.state.value == "success"
    assert len(state.tasks_context.tasks) == 1
    assert state.tasks_context.tasks[0].state == "in_progress"


def test_evaluator_flags_failure_continuation_and_order() -> None:
    fixture = PlanState()
    case = next(item for item in CASES if item["case_id"] == "P04")
    calls = [
        {"tool_name": "preflight_modify_parameter", "result": {"status": "NEED_CLARIFICATION"}},
        {"tool_name": "compare_result_versions", "result": {"status": "OK"}},
    ]
    metrics = evaluate(case, calls, fixture, "需要补充范围")
    assert metrics["stop_on_failure"] is False
    assert metrics["write_safety"] is False
    assert metrics["step_order"] is True
    assert metrics["step_completion"] == 1

    case = next(item for item in CASES if item["case_id"] == "P02")
    out_of_order = [
        {"tool_name": "compare_result_versions", "result": {"status": "OK"}},
        {"tool_name": "preflight_modify_parameter", "result": {"status": "ALLOWED"}},
        {"tool_name": "apply_parameter_change", "result": {"status": "SIMULATED"}},
        {"tool_name": "query_authority_result", "result": {"status": "OK"}},
    ]
    assert evaluate(case, out_of_order, fixture, "Fixture")["step_order"] is False


def test_summary_keeps_plan_usage_and_business_metrics_separate() -> None:
    row = {
        "mode": "plan",
        "case_id": "P01",
        "metrics": {
            "plan_usage": False,
            "step_completion": {"completed": 1, "total": 2, "ratio": 0.5, "steps": {}},
            "step_order": False,
            "stop_on_failure": True,
            "authority_binding": True,
            "scope_binding": False,
            "write_safety": True,
            "grounding": True,
            "tool_route": False,
        },
    }
    summary = summarize_plan("fixture-id", [row])
    assert summary["mode"]["plan"]["plan_usage"] == {"calls": 0, "runs": 1}
    assert summary["mode"]["plan"]["step_order"] == {"passed": 0, "total": 1}
    assert summary["mode"]["plan"]["step_completion"] == {"steps_completed": 1, "steps_total": 2}


def test_review_evaluator_catches_scope_and_missing_query_steps() -> None:
    p01 = {
        "case_id": "P01",
        "actual_tool_calls": [
            {"tool_name": "switch_session_well", "turn_index": 2, "result": {"well_id": "WELL-A"}},
            {
                "tool_name": "query_authority_result",
                "turn_index": 2,
                "arguments": {"scope_reference": "2035–2038m"},
                "result": {"well_id": "WELL-A", "scope": "whole_well"},
            },
        ],
        "responses": ["查询 Fixture 的 WELL-A whole_well"],
    }
    metrics = review_record(p01)
    assert metrics["step_completion"]["completed"] == 1
    assert metrics["scope_binding"] is False
    assert metrics["authority_binding"] is False

    p03 = {
        "case_id": "P03",
        "actual_tool_calls": [
            {
                "tool_name": "get_authority_context",
                "result": {
                    "task_id": "TASK-A",
                    "well_id": "WELL-A",
                    "execution_id": "A-V2",
                    "revision": 2,
                },
            },
            {
                "tool_name": "compare_result_versions",
                "result": {"left_execution_id": "A-V1", "right_execution_id": "A-V2"},
            },
        ],
        "responses": ["与上一版 A-V1 比较当前 A-V2"],
    }
    metrics = review_record(p03)
    assert metrics["step_completion"]["completed"] == 1
    assert metrics["tool_route"] is False


def test_review_evaluator_requires_fresh_authority_after_revision_change() -> None:
    p06 = {
        "case_id": "P06",
        "actual_tool_calls": [
            {
                "tool_name": "query_authority_result",
                "turn_index": 0,
                "result": {
                    "task_id": "TASK-A",
                    "well_id": "WELL-A",
                    "execution_id": "A-V2",
                    "revision": 2,
                },
            },
            {
                "tool_name": "compare_result_versions",
                "turn_index": 1,
                "result": {
                    "left_execution_id": "A-V1",
                    "right_execution_id": "A-V2",
                    "revision": 2,
                },
            },
        ],
        "responses": ["查询 A-V2 并比较 A-V1", "继续比较 A-V1 和 A-V2"],
    }
    metrics = review_record(p06)
    assert metrics["step_completion"]["completed"] == 1
    assert metrics["authority_binding"] is False
    assert metrics["tool_route"] is True
