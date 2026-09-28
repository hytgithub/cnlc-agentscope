"""通过真实内存 TaskCommands/Execution 验证执行桥授权、版本与全计划安全边界。"""

from unittest.mock import AsyncMock

import pytest

from cnlc_agent.application.commands import FullRerunCommand, ModifyInterpretationCommand
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.demo.interaction_context import ActiveContext, RecentContext
from cnlc_agent.demo.operation_execution_bridge import (
    OperationBridgeResult,
    OperationExecutionBridge,
)
from cnlc_agent.demo.operation_models import OperationPlan
from cnlc_agent.demo.operation_parser import PartialOperationPlan
from cnlc_agent.demo.scope_resolver import IntervalIndex
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.session_binding import TaskSessionIdentity


@pytest.fixture
async def env(data_dir):
    runner = TaskCommandRunner(
        settings=AppSettings(
            mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None
        ),
        persistence=PersistenceSettings(_env_file=None),
        connections=ConnectionSettings(_env_file=None),
        session_identity=TaskSessionIdentity(
            user_id="user", agent_id="agent", session_id="session"
        ),
    )
    initial = await runner.run("WELL_MOCK_001")
    await runner.wait_for_completion(initial.task_id, initial.execution_id)
    yield runner, initial, OperationExecutionBridge(runner)
    # 真正写命令保留后台行为，测试退出前收齐 Worker，避免悬挂任务污染其他测试。
    for task_id in runner.observed_task_ids:
        for execution in await runner.repository.list_executions(task_id):
            await runner.dispatcher.wait(execution.execution_id)


def node(action="MODIFY_PARAMETER", *, op_id="op1", target=None, **kwargs):
    data = {
        "operation_id": op_id,
        "action": action,
        "target": target or ("POROSITY" if action == "MODIFY_PARAMETER" else "WELL"),
    }
    if action == "MODIFY_PARAMETER":
        data["parameters"] = {"value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "1"}}
    data.update(kwargs)
    return data


def plan(*nodes, **kwargs):
    return PartialOperationPlan.model_validate(
        {
            "input_classification": "EXECUTION_REQUEST",
            "persist_mode": "CREATE_VERSION",
            "original_instruction": "结构化测试请求",
            "operations": list(nodes) or [node()],
            **kwargs,
        }
    )


async def count(runner):
    return len(runner.repository._executions)


async def reject(env, source, code):
    runner, _, bridge = env
    before = await count(runner)
    context = runner.interaction_context
    result = await bridge.execute(source)
    assert result.error_code == code, result
    assert result.created_execution_ids == []
    assert await count(runner) == before
    assert runner.interaction_context == context
    return result


async def second_version(runner, initial):
    result = await runner.execute(FullRerunCommand(task_id=initial.task_id))
    await runner.wait_for_completion(result.task_id, result.execution_id)
    return result


async def versions(runner, initial, total):
    """构造连续终态版本，返回按 sequence 排列的命令结果。"""

    items = [initial]
    while len(items) < total:
        items.append(await second_version(runner, initial))
    return items


@pytest.mark.parametrize(
    "selector",
    [
        None,
        "TASK_CURRENT",
        "PREVIOUS",
        "FIRST",
        "LATEST_SUCCESSFUL",
        "SEQUENCE",
        "EXECUTION_ID",
        "ACTIVE_BASE",
    ],
)
async def test_report_uses_resolved_version_and_only_updates_view(env, selector):
    runner, first, bridge = env
    current = await second_version(runner, first)
    runner.set_active_task("C")
    runner.set_active_base("C", "C-V2")
    reference = {"kind": selector} if selector else None
    if selector == "SEQUENCE":
        reference["sequence"] = 1
    if selector == "EXECUTION_ID":
        reference["execution_id"] = first.execution_id
    if selector == "ACTIVE_BASE":
        runner.set_active_task(first.task_id)
        runner.set_active_base(first.task_id, first.execution_id)
    before = runner.interaction_context.active
    source = plan(
        node(
            "REPORT",
            task_reference={"kind": "TASK_ID", "value": first.task_id},
            execution_reference=reference,
        )
    )
    result = await bridge.execute(source)
    assert result.outcome == "SUCCESS", result
    expected = (
        first.execution_id
        if selector in {"PREVIOUS", "FIRST", "SEQUENCE", "EXECUTION_ID", "ACTIVE_BASE"}
        else current.execution_id
    )
    assert result.results[0].execution_id == expected
    assert result.results[0].report_markdown
    assert runner.interaction_context.view.execution_id == expected
    assert runner.interaction_context.active == before
    assert not result.created_execution_ids
    assert OperationBridgeResult.model_validate_json(result.model_dump_json()) == result
    assert "state_snapshot" not in result.model_dump_json()


@pytest.mark.parametrize("selector", [None, "TASK_CURRENT"])
async def test_status_reads_current(env, selector):
    runner, first, bridge = env
    result = await bridge.execute(
        plan(node("STATUS", execution_reference={"kind": selector} if selector else None))
    )
    assert result.outcome == "SUCCESS"
    assert result.results[0].execution_id == first.execution_id
    assert runner.interaction_context.view.execution_id == first.execution_id


async def test_report_previous_is_relative_to_matching_view_not_task_current(env):
    runner, first, bridge = env
    v1, v2, v3, _v4 = await versions(runner, first, 4)
    runner.set_view_context(first.task_id, v3.execution_id)
    result = await bridge.execute(
        plan(
            node(
                "REPORT",
                task_reference={"kind": "TASK_ID", "value": first.task_id},
                execution_reference={"kind": "PREVIOUS"},
            )
        )
    )
    assert result.outcome == "SUCCESS", result
    assert result.results[0].execution_id == v2.execution_id
    assert result.resolved_executions["op1"].anchor_execution_id == v3.execution_id
    assert result.results[0].execution_id != v1.execution_id


async def test_report_previous_uses_view_even_when_active_is_another_task(env):
    runner, first, bridge = env
    _v1, v2, v3, _v4 = await versions(runner, first, 4)
    runner.set_active_task("C")
    runner.set_active_base("C", "C-V5")
    runner.set_view_context(first.task_id, v3.execution_id)
    before = runner.interaction_context.active
    result = await bridge.execute(
        plan(
            node(
                "REPORT",
                task_reference={"kind": "TASK_ID", "value": first.task_id},
                execution_reference={"kind": "PREVIOUS"},
            )
        )
    )
    assert result.results[0].execution_id == v2.execution_id
    assert runner.interaction_context.active == before


async def test_report_previous_does_not_leak_view_anchor_across_tasks(env):
    runner, first, bridge = env
    _a1, _a2, a3, _a4 = await versions(runner, first, 4)
    other = await runner.run("WELL_MOCK_001")
    await runner.wait_for_completion(other.task_id, other.execution_id)
    other_current = await second_version(runner, other)
    runner.set_view_context(first.task_id, a3.execution_id)
    result = await bridge.execute(
        plan(
            node(
                "REPORT",
                task_reference={"kind": "TASK_ID", "value": other.task_id},
                execution_reference={"kind": "PREVIOUS"},
            )
        )
    )
    assert result.results[0].task_id == other.task_id
    assert result.results[0].execution_id == other.execution_id
    assert result.resolved_executions["op1"].anchor_execution_id == other_current.execution_id


async def test_stale_matching_view_previous_does_not_fall_back(env):
    runner, first, _ = env
    await second_version(runner, first)
    runner.set_view_context(first.task_id, "missing-view-execution")
    await reject(
        env,
        plan(
            node(
                "REPORT",
                task_reference={"kind": "TASK_ID", "value": first.task_id},
                execution_reference={"kind": "PREVIOUS"},
            )
        ),
        "STALE_CONTEXT_REFERENCE",
    )


async def test_report_previous_uses_active_base_without_view(env):
    runner, first, bridge = env
    _v1, v2, v3, _v4 = await versions(runner, first, 4)
    runner.set_active_base(first.task_id, v3.execution_id)
    runner.clear_view_context()
    result = await bridge.execute(plan(node("REPORT", execution_reference={"kind": "PREVIOUS"})))
    assert result.results[0].execution_id == v2.execution_id
    assert result.resolved_executions["op1"].anchor_execution_id == v3.execution_id


async def test_report_previous_uses_task_current_without_context_anchor(env):
    runner, first, bridge = env
    _v1, _v2, v3, v4 = await versions(runner, first, 4)
    runner.clear_active_base()
    runner.clear_view_context()
    result = await bridge.execute(plan(node("REPORT", execution_reference={"kind": "PREVIOUS"})))
    assert result.results[0].execution_id == v3.execution_id
    assert result.resolved_executions["op1"].anchor_execution_id == v4.execution_id


@pytest.mark.parametrize("kind", ["PREVIOUS", "FIRST", "EXECUTION_ID"])
async def test_status_rejects_historical_selection(env, kind):
    reference = {"kind": kind}
    if kind == "EXECUTION_ID":
        reference["execution_id"] = env[1].execution_id
    await reject(
        env,
        plan(node("STATUS", execution_reference=reference)),
        "STATUS_EXECUTION_SELECTION_UNSUPPORTED",
    )


@pytest.mark.parametrize(
    "target,parameters,expected",
    [
        ("POROSITY", {"value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "1"}}, {"por": 0.16}),
        (
            "PERMEABILITY",
            {"value": {"mode": "ABSOLUTE", "value": 0.4, "unit": "mD"}},
            {"perm": 0.4},
        ),
        (
            "WELL",
            {
                "parameter_name": "sampling_interval",
                "value": {"mode": "ABSOLUTE", "value": 0.2, "unit": "m"},
            },
            {"sampling_interval": 0.2},
        ),
        ("MODEL", {"model_id": "model-v2"}, {"prediction_model": "model-v2"}),
        (
            "WELL",
            {"parameter_name": "prediction_model", "model_id": "model-v2"},
            {"prediction_model": "model-v2"},
        ),
    ],
)
async def test_supported_parameters_really_create_one_execution(env, target, parameters, expected):
    runner, first, bridge = env
    result = await bridge.execute(plan(node(target=target, parameters=parameters)))
    assert result.outcome == "SUCCESS", result
    assert await count(runner) == 2
    assert len(result.created_execution_ids) == 1
    assert result.results[0].command == "MODIFY"
    assert result.results[0].effective_override == InterpretationOverride(**expected)
    assert result.results[0].source_execution_id == first.execution_id
    completed = await runner.wait_for_completion(first.task_id, result.created_execution_ids[0])
    assert completed.report_ready


async def test_multi_modify_aggregation_and_duplicate_deduplication(env, monkeypatch):
    runner, _, bridge = env
    execute = AsyncMock(wraps=runner.execute)
    monkeypatch.setattr(runner, "execute", execute)
    result = await bridge.execute(
        plan(
            node(),
            node(op_id="same"),
            node(
                op_id="perm",
                target="PERMEABILITY",
                parameters={"value": {"mode": "ABSOLUTE", "value": 0.4, "unit": "mD"}},
            ),
        )
    )
    assert result.outcome == "SUCCESS", result
    execute.assert_awaited_once()
    command = execute.call_args.args[0]
    assert isinstance(command, ModifyInterpretationCommand)
    assert command.changes == InterpretationOverride(por=0.16, perm=0.4)
    assert await count(runner) == 2


async def test_conflicting_modify_values_do_not_execute(env):
    await reject(
        env,
        plan(
            node(),
            node(
                op_id="other",
                parameters={"value": {"mode": "ABSOLUTE", "value": 0.18, "unit": "1"}},
            ),
        ),
        "MULTI_OPERATION_CONFLICT",
    )


@pytest.mark.parametrize("mode", ["DELTA", "PERCENT_CHANGE"])
async def test_relative_values_do_not_execute(env, mode):
    await reject(
        env,
        plan(node(parameters={"value": {"mode": mode, "value": 0.2, "unit": "1"}})),
        "UNSUPPORTED_VALUE_MODE",
    )


@pytest.mark.parametrize("target,name", [("POROSITY", "perm"), ("PERMEABILITY", "por")])
async def test_target_parameter_conflict(env, target, name):
    await reject(
        env,
        plan(
            node(
                target=target,
                parameters={
                    "parameter_name": name,
                    "value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "1"},
                },
            )
        ),
        "INVALID_OPERATION_PLAN",
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {"value": {"mode": "ABSOLUTE", "value": 16.0, "unit": "%"}},
        {"parameter_name": "prediction_model", "model_id": "bad model !"},
        {"value": {"mode": "ABSOLUTE", "value": -0.1, "unit": "1"}},
    ],
)
async def test_override_contract_not_unit_conversion(env, parameters):
    parameters = {"parameter_name": "por", **parameters}
    await reject(env, plan(node(target="WELL", parameters=parameters)), "INVALID_OPERATION_PLAN")


async def test_model_id_missing_is_clarification(env):
    result = await reject(env, plan(node(target="MODEL", parameters={})), "CLARIFICATION_REQUIRED")
    assert result.outcome == "NEED_CLARIFICATION"


async def test_rerun_keeps_task_and_inherits_override(env):
    runner, first, bridge = env
    modified = await bridge.execute(plan(node()))
    await runner.wait_for_completion(first.task_id, modified.created_execution_ids[0])
    result = await bridge.execute(plan(node("FULL_RERUN")))
    assert result.outcome == "SUCCESS"
    assert result.results[0].command == "FULL_RERUN"
    assert result.results[0].effective_override.por == 0.16
    assert result.results[0].task_id == first.task_id
    assert await count(runner) == 3
    assert len(runner.repository._tasks) == 1


@pytest.mark.parametrize(
    "parameters",
    [
        {"model_id": "model-a"},
        {"method_id": "method-a"},
        {"value": {"mode": "ABSOLUTE", "value": 0.2, "unit": "1"}},
    ],
)
async def test_rerun_rejects_parameters(env, parameters):
    await reject(env, plan(node("FULL_RERUN", parameters=parameters)), "INVALID_OPERATION_PLAN")


async def test_reinterpret_does_not_become_rerun(env):
    result = await reject(env, plan(node("REINTERPRET")), "UNSUPPORTED_OPERATION")
    assert result.outcome == "KNOWN_UNSUPPORTED"
    assert result.validation.capability_facts[0].capability_status == "NOT_IMPLEMENTED"


@pytest.mark.parametrize("kind", ["PREVIOUS", "FIRST", "SEQUENCE", "EXECUTION_ID", "ACTIVE_BASE"])
async def test_historical_base_write_rejected(env, kind):
    runner, first, _ = env
    await second_version(runner, first)
    reference = {"kind": kind}
    if kind == "SEQUENCE":
        reference["sequence"] = 1
    if kind == "EXECUTION_ID":
        reference["execution_id"] = first.execution_id
    if kind == "ACTIVE_BASE":
        runner.set_active_base(first.task_id, first.execution_id)
    await reject(
        env, plan(node(execution_reference=reference)), "HISTORICAL_BASE_WRITE_UNSUPPORTED"
    )


async def test_current_active_base_cleared_only_after_success(env):
    runner, first, bridge = env
    execution = await runner.repository.get_execution(first.execution_id)
    interval = IntervalIndex(execution).identities()[0]
    from cnlc_agent.demo.operation_models import IntervalScope

    runner.set_active_base(
        first.task_id, first.execution_id, IntervalScope(interval_id=interval.interval_id)
    )
    runner.set_view_context("other", "other-v1")
    runner.set_recent_context(RecentContext(last_operation_id="old"))
    before = runner.interaction_context
    result = await bridge.execute(
        plan(
            node(
                task_reference={"kind": "TASK_ID", "value": first.task_id},
                execution_reference={"kind": "ACTIVE_BASE"},
                scope={"kind": "WHOLE_WELL"},
            )
        )
    )
    assert result.outcome == "SUCCESS", result
    assert runner.interaction_context.active == ActiveContext(task_id=first.task_id)
    assert runner.interaction_context.view == before.view
    assert runner.interaction_context.recent == before.recent


@pytest.mark.parametrize("action", ["REPORT", "FULL_RERUN"])
async def test_compound_write_rejected_before_first_command(env, action, monkeypatch):
    runner, _, _ = env
    execute = AsyncMock(wraps=runner.execute)
    monkeypatch.setattr(runner, "execute", execute)
    await reject(
        env,
        plan(
            node(),
            node(action, op_id="next"),
            edges=[{"from_operation_id": "op1", "to_operation_id": "next", "type": "SEQUENCE"}],
        ),
        "COMPOUND_EXECUTION_UNSUPPORTED",
    )
    execute.assert_not_awaited()


async def test_read_only_plan_stable_dependency_order(env, monkeypatch):
    runner, _, bridge = env
    execute = AsyncMock(wraps=runner.execute)
    monkeypatch.setattr(runner, "execute", execute)
    result = await bridge.execute(
        plan(
            node("REPORT", op_id="report"),
            node("STATUS", op_id="status"),
            edges=[
                {"from_operation_id": "status", "to_operation_id": "report", "type": "SEQUENCE"}
            ],
        )
    )
    assert result.outcome == "SUCCESS"
    assert [r.command for r in result.results] == ["STATUS", "GET_REPORT"]
    assert result.read_only and not result.created_execution_ids
    assert await count(runner) == 1


async def test_all_read_commands_adapt_before_any_execute(env, monkeypatch):
    runner, _, _ = env
    execute = AsyncMock(wraps=runner.execute)
    monkeypatch.setattr(runner, "execute", execute)
    await reject(
        env,
        plan(node("STATUS"), node("REPORT", op_id="bad", parameters={"model_id": "x"})),
        "INVALID_OPERATION_PLAN",
    )
    execute.assert_not_awaited()


async def test_current_ambiguous_write_uses_b_resolver(env):
    runner, _, _ = env
    another = await runner.run("WELL_MOCK_001")
    await runner.wait_for_completion(another.task_id, another.execution_id)
    runner.attach_session_runtime_context({})
    await reject(env, plan(node(task_reference={"kind": "CURRENT"})), "AMBIGUOUS_TASK_REFERENCE")


async def test_view_active_conflict_prevents_write(env):
    runner, _, _ = env
    runner.set_view_context("other", "other-v1")
    await reject(env, plan(node()), "VIEW_ACTIVE_CONTEXT_CONFLICT")


@pytest.mark.parametrize("change", ["binding", "current"])
async def test_revalidation_stops_revoked_or_stale_write(env, monkeypatch, change):
    runner, first, bridge = env
    old = first.execution_id
    current = await second_version(runner, first)
    runner.set_active_base(first.task_id, current.execution_id)
    original = bridge.validator.validate

    def validate(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "binding":
            runner.repository._session_task_bindings.clear()
        else:
            runner.repository._tasks[first.task_id].current_execution_id = old
        return result

    monkeypatch.setattr(bridge.validator, "validate", validate)
    execute = AsyncMock(wraps=runner.execute)
    monkeypatch.setattr(runner, "execute", execute)
    await reject(
        env, plan(node()), "TASK_NOT_FOUND" if change == "binding" else "STALE_CONTEXT_REFERENCE"
    )
    execute.assert_not_awaited()


@pytest.mark.parametrize("action", ["MODIFY_PARAMETER", "FULL_RERUN"])
async def test_interaction_policy_blocks_active_execution(env, monkeypatch, action):
    runner, first, _ = env
    # 暂不派发 Worker，保留真实 QUEUED 记录以确定性检查 Policy。
    monkeypatch.setattr(runner.dispatcher, "submit", AsyncMock())
    await runner.execute(FullRerunCommand(task_id=first.task_id))
    execute = AsyncMock(wraps=runner.execute)
    monkeypatch.setattr(runner, "execute", execute)
    await reject(env, plan(node(action)), "TASK_EXECUTION_ACTIVE")
    execute.assert_not_awaited()


async def test_initial_interpret_creates_nothing(env):
    runner, _, _ = env
    before = len(runner.repository._tasks), len(runner.repository._inputs)
    await reject(env, plan(node("FULL_INTERPRET")), "INITIAL_INPUT_ROUTE_REQUIRED")
    assert (len(runner.repository._tasks), len(runner.repository._inputs)) == before


@pytest.mark.parametrize("other", ["MODIFY_PARAMETER", "REPORT", "FULL_RERUN"])
async def test_initial_interpret_compound_plan_is_rejected_as_a_whole(env, other):
    runner, _, _ = env
    before = (len(runner.repository._tasks), len(runner.repository._inputs), await count(runner))
    await reject(
        env,
        plan(node("FULL_INTERPRET"), node(other, op_id="other")),
        "COMPOUND_EXECUTION_UNSUPPORTED",
    )
    after = (len(runner.repository._tasks), len(runner.repository._inputs), await count(runner))
    assert after == before


async def test_initial_interpret_condition_is_validated_before_route_redirect(env):
    await reject(
        env,
        plan(
            node("FULL_INTERPRET"),
            conditions=[{"expression": "有资料", "operation_ids": ["op1"]}],
        ),
        "CONDITIONAL_EXECUTION_UNSUPPORTED",
    )


async def test_initial_interpret_missing_target_needs_clarification(env):
    source = plan(node("FULL_INTERPRET"))
    source.operations[0].target = None
    result = await reject(env, source, "CLARIFICATION_REQUIRED")
    assert result.outcome == "NEED_CLARIFICATION"


async def test_capability_query_initial_interpret_stays_read_only(env):
    runner, _, bridge = env
    before = await count(runner)
    result = await bridge.execute(
        plan(node("FULL_INTERPRET"), input_classification="CAPABILITY_QUERY")
    )
    assert result.outcome == "READ_ONLY"
    assert result.results == []
    assert await count(runner) == before


async def test_out_of_domain_initial_interpret_is_rejected_before_redirect(env):
    result = await reject(
        env,
        plan(node("FULL_INTERPRET"), input_classification="OUT_OF_DOMAIN"),
        "OUT_OF_DOMAIN",
    )
    assert result.outcome == "REJECTED"


async def test_missing_session_identity_fails_closed(env):
    env[0].session_identity = None
    await reject(env, plan(node()), "OPERATION_SESSION_IDENTITY_REQUIRED")


async def test_extensions_never_become_override(env):
    runner, _, bridge = env
    result = await bridge.execute(
        plan(
            node(
                parameters={
                    "value": {"mode": "ABSOLUTE", "value": 0.16, "unit": "1"},
                    "extensions": {"rw": 0.08, "start_step": "W06", "perm": 99},
                }
            )
        )
    )
    assert result.outcome == "SUCCESS"
    assert result.results[0].effective_override == InterpretationOverride(por=0.16)
    assert result.results[0].start_step == "W04"


async def test_shadowed_ids_in_complete_model_extensions_rejected(env):
    source = OperationPlan.model_validate(plan().model_dump())
    source.operations[0].parameters.extensions = {"task_id": "other", "execution_id": "other-v1"}
    await reject(env, source, "INVALID_OPERATION_PLAN")


async def test_extensions_cannot_supply_missing_value(env):
    await reject(
        env, plan(node(parameters={"extensions": {"por": 0.16}})), "CLARIFICATION_REQUIRED"
    )


@pytest.mark.parametrize(
    "scope,code",
    [
        ({"kind": "INTERVAL", "interval_id": "other-version-layer"}, "INTERVAL_NOT_FOUND"),
        ({"kind": "MULTI_INTERVAL", "interval_ids": ["other-version-layer"]}, "INTERVAL_NOT_FOUND"),
        ({"kind": "DEPTH_RANGE", "top": 2500.0, "bottom": 2501.0, "depth_reference": "MD"}, None),
        ({"kind": "DEPTH_POINT", "depth": 2500.0, "depth_reference": "MD"}, None),
        ({"kind": "FILTER_SET", "filter_expression": "anything"}, "FILTER_SET_UNRESOLVED"),
    ],
)
async def test_local_scope_never_turns_into_whole_well(env, scope, code):
    runner, _, bridge = env
    before = await count(runner)
    result = await bridge.execute(plan(node(scope=scope)))
    assert result.error_code in (
        {code} if code else {"DEPTH_OUT_OF_RANGE", "UNSUPPORTED_OPERATION"}
    )
    assert not result.created_execution_ids
    assert await count(runner) == before


@pytest.mark.parametrize(
    "patch",
    [
        {"persist_mode": "PREVIEW"},
        {"operations": [node(constraints=[{"type": "USE_RULE_ONLY"}])]},
        {"operations": [node(input_refs=[{"execution_reference": {"kind": "TASK_CURRENT"}}])]},
    ],
)
async def test_unrepresentable_intent_not_silently_dropped(env, patch):
    await reject(env, plan(**patch), "UNSUPPORTED_OPERATION")


async def test_capability_query_does_not_execute(env, monkeypatch):
    runner, _, bridge = env
    execute = AsyncMock(wraps=runner.execute)
    monkeypatch.setattr(runner, "execute", execute)
    result = await bridge.execute(plan(node("FULL_RERUN"), input_classification="CAPABILITY_QUERY"))
    assert result.outcome == "READ_ONLY"
    assert result.results == []
    execute.assert_not_awaited()


@pytest.mark.parametrize("action", ["MODIFY_PARAMETER", "FULL_RERUN"])
async def test_pointer_change_after_bridge_preflight_still_rejected(env, monkeypatch, action):
    runner, first, bridge = env
    current = await second_version(runner, first)
    # 模拟 Application 规划开始前版本变更；锁内 B 重检之后也不能丢掉原基线。
    from cnlc_agent.application.service import InterpretationTaskService

    original = InterpretationTaskService.plan_rerun

    async def changed(service, *args, **kwargs):
        from datetime import timedelta

        from cnlc_agent.domain.models import utc_now

        previous = await runner.repository.get_execution(current.execution_id)
        state = previous.state_snapshot.model_copy(deep=True)
        state.workflow_execution_id = "concurrent-v3"
        await runner.repository.create_execution(state, input_version_id=previous.input_version_id)
        await runner.repository.claim_execution(
            "concurrent-v3", "other-worker", utc_now() + timedelta(minutes=1)
        )
        await runner.repository.save_execution_report("concurrent-v3", "并发版本报告")
        await runner.repository.finish_execution("concurrent-v3", "other-worker", "SUCCESS")
        return await original(service, *args, **kwargs)

    monkeypatch.setattr(InterpretationTaskService, "plan_rerun", changed)
    before = await count(runner)
    result = await bridge.execute(
        plan(
            node(
                action,
                execution_reference={"kind": "EXECUTION_ID", "execution_id": current.execution_id},
            )
        ),
    )
    assert result.error_code == "STALE_EXECUTION_PLAN"
    assert result.created_execution_ids == []
    assert await count(runner) == before + 1  # 只有模拟的并发写者新增一版。


async def test_binding_revoked_while_waiting_for_runner_is_rechecked(env, monkeypatch):
    runner, _, _ = env
    original = runner.execute

    async def revoked(command, **kwargs):
        runner.repository._session_task_bindings.clear()
        return await original(command, **kwargs)

    monkeypatch.setattr(runner, "execute", revoked)
    await reject(env, plan(node()), "TASK_NOT_FOUND")


async def test_binding_authorizes_even_without_observed_cache(env):
    runner, first, bridge = env
    runner.observed_task_ids.clear()
    result = await bridge.execute(plan(node("STATUS")))
    assert result.outcome == "SUCCESS"
    assert result.results[0].task_id == first.task_id


async def test_foreign_task_id_not_authorized_by_active_or_cache(env):
    runner, first, _ = env
    runner.session_identity = TaskSessionIdentity(
        user_id="other", agent_id="agent", session_id="session"
    )
    await reject(
        env,
        plan(node(task_reference={"kind": "TASK_ID", "value": first.task_id})),
        "TASK_NOT_FOUND",
    )


async def test_valid_local_scope_is_rejected_by_capability(env):
    runner, first, _ = env
    execution = await runner.repository.get_execution(first.execution_id)
    interval = IntervalIndex(execution).identities()[0]
    await reject(
        env,
        plan(node(scope={"kind": "INTERVAL", "interval_id": interval.interval_id})),
        "UNSUPPORTED_OPERATION",
    )


@pytest.mark.parametrize("kind", ["CURRENT", "WELL_ID"])
async def test_symbolic_task_write_keeps_matching_active_historical_base(env, kind):
    runner, first, _ = env
    await second_version(runner, first)
    runner.set_active_base(first.task_id, first.execution_id)
    reference = {"kind": kind}
    if kind == "WELL_ID":
        reference["value"] = first.well_id
    await reject(env, plan(node(task_reference=reference)), "HISTORICAL_BASE_WRITE_UNSUPPORTED")


async def test_current_active_base_symbolic_write_is_allowed(env):
    runner, first, bridge = env
    runner.set_active_base(first.task_id, first.execution_id)
    result = await bridge.execute(plan(node(task_reference={"kind": "CURRENT"})))
    assert result.outcome == "SUCCESS"
    assert runner.interaction_context.active.base_execution_id is None


async def test_duplicate_operation_ids_reject_before_building_resolution_map(env):
    runner, first, _ = env
    other = await runner.run("WELL_MOCK_001")
    await runner.wait_for_completion(other.task_id, other.execution_id)
    result = await reject(
        env,
        plan(
            node(task_reference={"kind": "TASK_ID", "value": first.task_id}),
            node(task_reference={"kind": "TASK_ID", "value": other.task_id}),
        ),
        "INVALID_OPERATION_PLAN",
    )
    assert not result.validation.graph_valid
    assert not result.resolved_tasks
