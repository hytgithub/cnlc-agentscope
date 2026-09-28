"""统一 Tool 的 Schema、安全输出和真实 Runner 执行边界。"""

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.interaction_middleware import validate_grounded_operation_values
from cnlc_agent.demo.operation_models import OperationPlan
from cnlc_agent.demo.operation_parser import ClarificationSlot, PartialOperationPlan
from cnlc_agent.demo.operation_tool import OperationToolInput, build_agent_task_tools
from cnlc_agent.demo.task_tools import TaskCommandRunner


@pytest.fixture
async def operation_env(data_dir):
    runner = TaskCommandRunner(
        AppSettings(model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
    )
    tool = build_agent_task_tools(runner)[1]
    first = await runner.run("WELL_MOCK_001")
    await runner.wait_for_completion(first.task_id, first.execution_id)
    yield runner, tool, first
    await runner.dispatcher.shutdown()


def request(target="POROSITY", **fields):
    """独立结构化输入，不依赖 Mock 自然语言路由。"""
    return {
        "mode": "PLAN",
        "plan": {
            "input_classification": "EXECUTION_REQUEST",
            "persist_mode": "CREATE_VERSION",
            "original_instruction": "修改",
            "operations": [
                {
                    "operation_id": "op1",
                    "action": "MODIFY_PARAMETER",
                    "target": target,
                    "parameters": {"value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "1"}},
                    **fields,
                }
            ],
        },
    }


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("INTERVAL", {"interval_id": "invented"}),
        ("MULTI_INTERVAL", {"interval_ids": ["invented"]}),
        ("FILTER_SET", {"expression": "all", "resolved_ids": []}),
    ],
)
def test_model_cannot_submit_stable_interval_ids(kind, payload):
    with pytest.raises(ValidationError):
        OperationToolInput.model_validate({"request": request(scope={"kind": kind, **payload})})


def test_ordinal_is_partial_only_and_schema_hides_stable_ids():
    payload = request(scope={"kind": "INTERVAL_ORDINAL", "ordinal": 5})
    partial = PartialOperationPlan.model_validate(payload["plan"])
    assert partial.operations[0].scope.ordinal == 5
    with pytest.raises(ValidationError):
        OperationPlan.model_validate(partial.model_dump())
    schema = json.dumps(
        build_agent_task_tools(
            TaskCommandRunner(
                AppSettings(_env_file=None),
                PersistenceSettings(persistence="memory", _env_file=None),
            )
        )[1].input_schema
    )
    assert '"interval_id"' not in schema and '"interval_ids"' not in schema
    assert '"resolved_ids"' not in schema
    assert "INTERVAL_ORDINAL" in schema and "CLARIFICATION_REPLY" in schema


async def test_clarification_refresh_locks_task_and_executes_once(operation_env):
    runner, tool, first = operation_env
    runtime = runner._session_runtime_context
    runner.begin_interaction_turn()
    result = await tool.call(request=request(target=None))
    assert result.metadata["operation"]["outcome"] == "NEED_CLARIFICATION"
    pending = runner.pending_operation_clarification()
    assert pending.locked_references[0].task_id == first.task_id
    assert pending.locked_references[0].execution_id == first.execution_id
    assert "owner_token" not in result.content[0].text
    runner.end_interaction_turn()
    restored = deepcopy(runtime)
    runner.attach_session_runtime_context(restored)
    assert runner.pending_operation_clarification().owner_token == pending.owner_token
    runner.set_active_task("other-task")
    runner.begin_interaction_turn()
    patch = {"mode": "CLARIFICATION_REPLY", "patch": {"target": "POROSITY"}}
    result = await tool.call(request=patch)
    assert result.metadata["result"]["task_id"] == first.task_id
    assert len(await runner.repository.list_executions(first.task_id)) == 2
    again = await tool.call(request=patch)
    assert again.metadata["error_code"] == "CLARIFICATION_SLOT_INVALID"
    assert len(await runner.repository.list_executions(first.task_id)) == 2
    assert runner.pending_operation_clarification() is None


async def test_cancel_and_cache_miss_do_not_revive_pending(operation_env):
    runner, tool, first = operation_env
    await tool.call(request=request(target=None))
    cancelled = await tool.call(request={"mode": "CANCEL"})
    assert cancelled.metadata["operation"]["created_execution_ids"] == []
    assert runner.pending_operation_clarification() is None
    result = await tool.call(
        request={"mode": "CLARIFICATION_REPLY", "patch": {"target": "POROSITY"}}
    )
    assert result.state == "error"
    await tool.call(request=request(target=None))
    runner.attach_session_runtime_context({"cnlc_active_task_id": first.task_id})
    assert runner.pending_operation_clarification() is None
    assert runner.interaction_context.view is None
    assert len(await runner.repository.list_executions(first.task_id)) == 1


async def test_unrelated_turn_expires_pending_and_new_owner_rejects(operation_env):
    runner, tool, _ = operation_env
    runner.begin_interaction_turn()
    await tool.call(request=request(target=None))
    context = deepcopy(runner._session_runtime_context)
    runner.end_interaction_turn()
    runner.begin_interaction_turn()
    runner.end_interaction_turn()
    assert runner.pending_operation_clarification() is None
    other = TaskCommandRunner(runner.settings, runner.persistence)
    other.attach_session_runtime_context(context)
    assert other.pending_operation_clarification() is None


@pytest.mark.parametrize(
    "scope",
    [
        {"kind": "INTERVAL_ORDINAL", "ordinal": 5},
        {"kind": "MULTI_INTERVAL_ORDINAL", "ordinals": [3, 5, 7, 3]},
    ],
)
async def test_ordinal_normalizes_then_rejects_local_write(operation_env, scope):
    runner, tool, first = operation_env
    # 使用受控真实版本快照扩充七层，保持每层身份来自服务端。
    stage = runner.repository._executions[first.execution_id].state_snapshot.interval_result
    stage.result["intervals"] = [
        {"top_depth_m": 2000 + i, "bottom_depth_m": 2001 + i} for i in range(7)
    ]
    result = await tool.controller.bridge.execute(
        PartialOperationPlan.model_validate(request(scope=scope)["plan"])
    )
    assert result.outcome == "KNOWN_UNSUPPORTED"
    resolved = result.resolved_scopes["op1"]
    assert [item.ordinal for item in resolved.intervals] == (
        [5] if "ordinal" in scope else [3, 5, 7]
    )
    assert all(item.execution_id == first.execution_id for item in resolved.intervals)
    assert len(await runner.repository.list_executions(first.task_id)) == 1
    invalid = await tool.call(
        request=request(scope={"kind": "MULTI_INTERVAL_ORDINAL", "ordinals": [3, 100]})
    )
    assert invalid.metadata["error_code"] == "INTERVAL_NOT_FOUND"
    assert len(await runner.repository.list_executions(first.task_id)) == 1


async def test_correction_re_resolves_scope_and_never_becomes_whole_well(operation_env):
    runner, tool, first = operation_env
    await tool.call(request=request(target=None))
    result = await tool.call(
        request={
            "mode": "CLARIFICATION_REPLY",
            "patch": {
                "input_classification": "CORRECTION",
                "target": "POROSITY",
                "scope": {"kind": "INTERVAL_ORDINAL", "ordinal": 6},
            },
        }
    )
    assert result.state == "error"
    assert result.metadata["error_code"] in {"INTERVAL_NOT_FOUND", "UNSUPPORTED_OPERATION"}
    assert len(await runner.repository.list_executions(first.task_id)) == 1


async def test_focus_write_safe_and_clears_other_view(operation_env):
    runner, tool, first = operation_env
    second = await runner.run("WELL_MOCK_001")
    await runner.wait_for_completion(second.task_id, second.execution_id)
    runner.set_view_context(first.task_id, first.execution_id)
    before = len(runner.repository._executions)
    result = await tool.call(
        request={
            "mode": "SET_ACTIVE_CONTEXT",
            "task_reference": {"kind": "TASK_ID", "value": second.task_id},
        }
    )
    assert result.state == "success"
    assert runner.interaction_context.view is None
    assert len(runner.repository._executions) == before
    runner.set_active_task("missing")
    ambiguous = await tool.call(
        request={
            "mode": "SET_ACTIVE_CONTEXT",
            "task_reference": {"kind": "WELL_ID", "value": "WELL_MOCK_001"},
        }
    )
    assert ambiguous.metadata["error_code"] == "AMBIGUOUS_TASK_REFERENCE"
    assert runner.active_task_id == "missing"


async def test_explicit_focus_clears_same_task_historical_view(operation_env):
    """从历史 V1 显式切回本井后，后续隐式写入以当前版本为基线。"""
    runner, tool, first = operation_env
    second = await tool.call(request=request())
    assert second.state == "success"
    await runner.wait_for_completion(
        first.task_id, second.metadata["operation"]["created_execution_ids"][0]
    )
    runner.set_view_context(first.task_id, first.execution_id)
    focused = await tool.call(
        request={
            "mode": "SET_ACTIVE_CONTEXT",
            "task_reference": {"kind": "TASK_ID", "value": first.task_id},
        }
    )
    assert focused.state == "success"
    assert runner.interaction_context.view is None
    change = request()
    change["plan"]["operations"][0]["parameters"]["value"]["value"] = 0.18
    modified = await tool.call(request=change)
    assert modified.state == "success"
    assert len(modified.metadata["operation"]["created_execution_ids"]) == 1
    assert len(await runner.repository.list_executions(first.task_id)) == 3


async def test_read_status_without_irrelevant_persist_mode(operation_env):
    """真实模型省略只读保存模式时仍读取持久状态，且不产生版本。"""
    runner, tool, first = operation_env
    result = await tool.call(
        request={
            "mode": "PLAN",
            "plan": {
                "input_classification": "READ_REQUEST",
                "original_instruction": "现在到哪一步了",
                "operations": [
                    {"operation_id": "op1", "action": "STATUS", "target": "WELL"}
                ],
            },
        }
    )
    assert result.state == "success"
    assert len(result.metadata["operation"]["task_results"]) == 1
    assert len(await runner.repository.list_executions(first.task_id)) == 1


async def test_two_slot_clarification_continues_and_keeps_locked_task(operation_env):
    """TARGET、VALUE 分两轮补齐；Active 漂移不能改变首轮锁定任务。"""
    runner, tool, first = operation_env
    second = await runner.run("WELL_MOCK_001")
    await runner.wait_for_completion(second.task_id, second.execution_id)
    runner.set_active_task(first.task_id)
    runner.begin_interaction_turn()
    incomplete = request(target=None)
    incomplete["plan"]["operations"][0]["parameters"]["value"] = None
    initial = await tool.call(request=incomplete)
    assert initial.metadata["operation"]["outcome"] == "NEED_CLARIFICATION"
    assert {issue.slot for issue in runner.pending_operation_clarification().issues} >= {
        ClarificationSlot.TARGET,
        ClarificationSlot.VALUE,
    }
    runner.end_interaction_turn()

    runner.begin_interaction_turn()
    target = await tool.call(
        request={"mode": "CLARIFICATION_REPLY", "patch": {"target": "POROSITY"}}
    )
    assert target.metadata["operation"]["outcome"] == "NEED_CLARIFICATION"
    pending = runner.pending_operation_clarification()
    assert {issue.slot for issue in pending.issues} == {ClarificationSlot.VALUE}
    assert pending.created_turn == runner._interaction_turn
    assert pending.locked_references[0].task_id == first.task_id
    assert len(await runner.repository.list_executions(first.task_id)) == 1
    assert len(await runner.repository.list_executions(second.task_id)) == 1
    runner.set_active_task(second.task_id)
    runner.end_interaction_turn()

    runner.begin_interaction_turn()
    completed = await tool.call(
        request={
            "mode": "CLARIFICATION_REPLY",
            "patch": {"value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "1"}},
        }
    )
    assert completed.state == "success"
    assert completed.metadata["result"]["task_id"] == first.task_id
    assert len(await runner.repository.list_executions(first.task_id)) == 2
    assert len(await runner.repository.list_executions(second.task_id)) == 1
    assert runner.pending_operation_clarification() is None
    replay = await tool.call(
        request={
            "mode": "CLARIFICATION_REPLY",
            "patch": {"value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "1"}},
        }
    )
    assert replay.metadata["error_code"] == "CLARIFICATION_SLOT_INVALID"
    assert len(await runner.repository.list_executions(first.task_id)) == 2


async def test_three_slot_clarification_executes_only_after_last_patch(operation_env):
    """TARGET、VALUE、PERSIST_MODE 每轮形成新 Pending，最后一轮才执行。"""
    runner, tool, first = operation_env
    runner.begin_interaction_turn()
    incomplete = request(target=None)
    incomplete["plan"].pop("persist_mode")
    incomplete["plan"]["operations"][0]["parameters"]["value"] = None
    await tool.call(request=incomplete)
    assert len(await runner.repository.list_executions(first.task_id)) == 1
    runner.end_interaction_turn()

    patches = [
        {"target": "POROSITY"},
        {"value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "1"}},
        {"persist_mode": "CREATE_VERSION"},
    ]
    expected = [
        {ClarificationSlot.VALUE, ClarificationSlot.PERSIST_MODE},
        {ClarificationSlot.PERSIST_MODE},
    ]
    for index, patch in enumerate(patches):
        runner.begin_interaction_turn()
        result = await tool.call(request={"mode": "CLARIFICATION_REPLY", "patch": patch})
        if index < 2:
            assert result.metadata["operation"]["outcome"] == "NEED_CLARIFICATION"
            remaining = {
                issue.slot for issue in runner.pending_operation_clarification().issues
            }
            assert remaining == expected[index]
            assert len(await runner.repository.list_executions(first.task_id)) == 1
            runner.end_interaction_turn()
    assert result.state == "success"
    assert len(await runner.repository.list_executions(first.task_id)) == 2
    assert runner.pending_operation_clarification() is None


async def test_correction_continues_missing_value_and_never_widens_scope(operation_env):
    """层号修正后继续等待 VALUE，补值后局部能力拒绝且不退化整井。"""
    runner, tool, first = operation_env
    intervals = runner.repository._executions[first.execution_id].state_snapshot.interval_result
    intervals.result["intervals"] = [
        {"top_depth_m": 2000 + index, "bottom_depth_m": 2001 + index}
        for index in range(7)
    ]
    runner.begin_interaction_turn()
    incomplete = request(scope={"kind": "INTERVAL_ORDINAL", "ordinal": 5})
    incomplete["plan"]["operations"][0]["parameters"]["value"] = None
    first_reply = await tool.call(request=incomplete)
    assert first_reply.metadata["operation"]["outcome"] == "NEED_CLARIFICATION"
    runner.end_interaction_turn()

    runner.begin_interaction_turn()
    corrected = await tool.call(
        request={
            "mode": "CLARIFICATION_REPLY",
            "patch": {
                "input_classification": "CORRECTION",
                "scope": {"kind": "INTERVAL_ORDINAL", "ordinal": 6},
            },
        }
    )
    assert corrected.metadata["operation"]["outcome"] == "NEED_CLARIFICATION"
    pending = runner.pending_operation_clarification()
    assert {issue.slot for issue in pending.issues} == {ClarificationSlot.VALUE}
    assert pending.locked_references[0].scope.kind == "INTERVAL"
    runner.end_interaction_turn()

    runner.begin_interaction_turn()
    rejected = await tool.call(
        request={
            "mode": "CLARIFICATION_REPLY",
            "patch": {"value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "1"}},
        }
    )
    assert rejected.metadata["operation"]["outcome"] == "KNOWN_UNSUPPORTED"
    assert rejected.metadata["operation"]["created_execution_ids"] == []
    assert len(await runner.repository.list_executions(first.task_id)) == 1


async def test_cancel_after_continuation_ends_chain(operation_env):
    runner, tool, first = operation_env
    runner.begin_interaction_turn()
    incomplete = request(target=None)
    incomplete["plan"]["operations"][0]["parameters"]["value"] = None
    await tool.call(request=incomplete)
    runner.end_interaction_turn()
    runner.begin_interaction_turn()
    await tool.call(
        request={"mode": "CLARIFICATION_REPLY", "patch": {"target": "POROSITY"}}
    )
    assert runner.pending_operation_clarification() is not None
    runner.end_interaction_turn()
    runner.begin_interaction_turn()
    await tool.call(request={"mode": "CANCEL"})
    assert runner.pending_operation_clarification() is None
    runner.end_interaction_turn()
    runner.begin_interaction_turn()
    stale = await tool.call(
        request={
            "mode": "CLARIFICATION_REPLY",
            "patch": {"value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "1"}},
        }
    )
    assert stale.metadata["error_code"] == "CLARIFICATION_SLOT_INVALID"
    assert len(await runner.repository.list_executions(first.task_id)) == 1


async def test_new_plan_replaces_existing_continuation(operation_env):
    runner, tool, first = operation_env
    incomplete = request(target=None)
    incomplete["plan"]["operations"][0]["parameters"]["value"] = None
    await tool.call(request=incomplete)
    assert runner.pending_operation_clarification() is not None
    replacement = request()
    replacement["plan"]["operations"][0]["parameters"]["value"]["value"] = 0.18
    completed = await tool.call(request=replacement)
    assert completed.state == "success"
    assert runner.pending_operation_clarification() is None
    assert len(await runner.repository.list_executions(first.task_id)) == 2


def value_reply(value):
    return {
        "mode": "CLARIFICATION_REPLY",
        "patch": {"value": {"mode": "ABSOLUTE", "value": value, "unit": "1"}},
    }


def ordinal_correction(ordinal):
    return {
        "mode": "CLARIFICATION_REPLY",
        "patch": {
            "input_classification": "CORRECTION",
            "scope": {"kind": "INTERVAL_ORDINAL", "ordinal": ordinal},
        },
    }


def multi_ordinal_plan(ordinals):
    scope = {"kind": "MULTI_INTERVAL_ORDINAL", "ordinals": ordinals}
    return {"mode": "PLAN", "plan": request(scope=scope)["plan"]}


@pytest.mark.parametrize(
    "user_text,request_payload,valid",
    [
        (
            "0.16",
            value_reply(0.16),
            True,
        ),
        (
            "0.16",
            value_reply(0.18),
            False,
        ),
        (
            "16%",
            value_reply(0.16),
            True,
        ),
        (
            "16%",
            value_reply(0.18),
            False,
        ),
        (
            "不对，是第6层",
            ordinal_correction(6),
            True,
        ),
        (
            "不对，是第6层",
            ordinal_correction(7),
            False,
        ),
        (
            "第3、5、7层孔隙度改成0.16",
            multi_ordinal_plan([3, 5, 7]),
            True,
        ),
        (
            "第3、5、7层孔隙度改成0.16",
            multi_ordinal_plan([3, 5, 8]),
            False,
        ),
    ],
)
def test_current_turn_value_and_ordinal_grounding(user_text, request_payload, valid):
    structured = OperationToolInput.model_validate({"request": request_payload}).request
    if valid:
        validate_grounded_operation_values(structured, user_text)
    else:
        with pytest.raises(ValueError):
            validate_grounded_operation_values(structured, user_text)
