"""真实 Fixture 层段投影、版本隔离、深度约束和筛选冻结语义。"""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from cnlc_agent.demo.operation_models import (
    DepthPointScope,
    DepthRangeScope,
    FilterSetScope,
    IntervalScope,
    MultiIntervalScope,
    WholeWellScope,
)
from cnlc_agent.demo.scope_resolver import IntervalIndex, ScopeResolver, resolve_interval_ordinal
from cnlc_agent.domain.errors import DataError
from cnlc_agent.domain.execution import ExecutionStatus
from cnlc_agent.domain.models import MockFixture, TaskRequest, utc_now
from cnlc_agent.domain.session_binding import SessionTaskBinding, TaskSessionIdentity
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository


@pytest.fixture
async def scope_env(fixture_data):
    fixture = MockFixture.model_validate(fixture_data)
    repository = InMemoryTaskRepository()
    identity = TaskSessionIdentity(user_id="user", agent_id="agent", session_id="session")
    for task_id in ("task", "foreign"):
        task = TaskRequest(task_id=task_id, well_id=fixture.well.well_id)
        await repository.create_task(InterpretationState(task=task))
        if task_id == "task":
            await repository.bind_task_to_session(
                SessionTaskBinding(**identity.model_dump(), task_id=task_id)
            )
        for sequence in (1, 2):
            state = InterpretationState(
                task=task,
                workflow_execution_id=f"{task_id}-v{sequence}",
                raw_data=fixture.raw_data,
                interval_result=fixture.outputs["intervals"],
            )
            await repository.create_execution(state)
            await repository.claim_execution(
                state.workflow_execution_id, "worker", utc_now() + timedelta(minutes=1)
            )
            await repository.finish_execution(
                state.workflow_execution_id, "worker", ExecutionStatus.SUCCESS
            )
    return repository, ScopeResolver(repository, identity)


async def test_whole_well_is_bound_without_local_intervals(scope_env):
    _, resolver = scope_env
    result = await resolver.resolve("task", "task-v1", WholeWellScope())
    assert result.task_id == "task" and result.execution_id == "task-v1"
    assert result.intervals == [] and result.depth_coverage is None
    assert result.resolution_source == "WHOLE_WELL"
    assert type(result).model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "scope",
    [
        DepthRangeScope(top=2000, bottom=2001, depth_reference="MD"),
        DepthPointScope(depth=2000.123, depth_reference="MD"),
        DepthPointScope(depth=2000, depth_reference="MD"),
        DepthPointScope(depth=2001, depth_reference="MD"),
    ],
)
async def test_real_depth_coverage_without_clipping_or_sample_snap(scope_env, scope):
    _, resolver = scope_env
    result = await resolver.resolve("task", "task-v1", scope)
    assert result.scope == scope
    assert result.depth_coverage.top == 2000 and result.depth_coverage.bottom == 2001
    assert result.depth_coverage.depth_reference == "MD"


@pytest.mark.parametrize(
    "scope,code",
    [
        (DepthRangeScope(top=1999, bottom=2001, depth_reference="MD"), "DEPTH_OUT_OF_RANGE"),
        (DepthRangeScope(top=2000, bottom=2002, depth_reference="MD"), "DEPTH_OUT_OF_RANGE"),
        (DepthPointScope(depth=1999, depth_reference="MD"), "DEPTH_OUT_OF_RANGE"),
        (DepthPointScope(depth=2002, depth_reference="MD"), "DEPTH_OUT_OF_RANGE"),
        (
            DepthRangeScope(top=2000, bottom=2001, depth_reference="TVD"),
            "DEPTH_REFERENCE_UNAVAILABLE",
        ),
        (DepthPointScope(depth=2000, depth_reference="TVDSS"), "DEPTH_REFERENCE_UNAVAILABLE"),
    ],
)
async def test_invalid_depth_requests_fail_explicitly(scope_env, scope, code):
    _, resolver = scope_env
    with pytest.raises(DataError) as error:
        await resolver.resolve("task", "task-v1", scope)
    assert error.value.code == code


@pytest.mark.parametrize("missing", ["raw", "depths"])
async def test_missing_depth_data_is_not_guessed(scope_env, monkeypatch, missing):
    repository, resolver = scope_env
    execution = await repository.get_execution("task-v1")
    if missing == "raw":
        execution.state_snapshot.raw_data = None
    else:
        execution.state_snapshot.raw_data.curves = {}
        execution.state_snapshot.raw_data.depths = []
    monkeypatch.setattr(repository, "get_execution", AsyncMock(return_value=execution))
    with pytest.raises(DataError) as error:
        await resolver.resolve("task", "task-v1", DepthPointScope(depth=2000, depth_reference="MD"))
    assert error.value.code == "DEPTH_REFERENCE_UNAVAILABLE"


async def test_fixture_interval_identity_is_execution_local_and_deterministic(scope_env):
    repository, resolver = scope_env
    first = await repository.get_execution("task-v1")
    assert "interval_id" not in first.state_snapshot.interval_result.result["intervals"][0]
    before = first.model_dump_json()
    identity = resolve_interval_ordinal(first, 1)
    assert identity == IntervalIndex(first).identities()[0]
    assert identity == resolve_interval_ordinal(first, 1)
    assert identity.identity_source == "EXECUTION_ORDINAL"
    assert identity.top_depth == 2000 and identity.bottom_depth == 2001
    assert identity.ordinal == 1 and identity.execution_id == "task-v1"
    assert first.model_dump_json() == before
    other = await resolver.resolve_interval_ordinal("task", "task-v2", 1)
    assert other.interval_id != identity.interval_id
    result = await resolver.resolve(
        "task", "task-v1", IntervalScope(interval_id=identity.interval_id)
    )
    assert result.intervals == [identity]
    with pytest.raises(DataError) as error:
        await resolver.resolve("task", "task-v2", IntervalScope(interval_id=identity.interval_id))
    assert error.value.code == "INTERVAL_NOT_FOUND"


@pytest.mark.parametrize("ordinal", [0, -1, 5, True, "1", 1.0])
async def test_invalid_ordinals_do_not_use_negative_indices(scope_env, ordinal):
    _, resolver = scope_env
    with pytest.raises(DataError) as error:
        await resolver.resolve_interval_ordinal("task", "task-v1", ordinal)
    assert error.value.code == "INTERVAL_NOT_FOUND"


async def test_native_ids_are_preferred_and_also_version_qualified(scope_env):
    repository, _ = scope_env
    execution = await repository.get_execution("task-v1")
    rows = execution.state_snapshot.interval_result.result["intervals"]
    rows[0]["interval_id"] = "native:层/1"
    rows.append({**rows[0], "interval_id": "native:层/2"})
    before = IntervalIndex(execution).identities()
    rows.reverse()
    after = IntervalIndex(execution).identities()
    assert before[0].interval_id == after[1].interval_id
    assert after[1].ordinal == 2
    assert before[0].identity_source == "NATIVE"
    assert before[0].native_interval_id == "native:层/1"
    other = await repository.get_execution("task-v2")
    other.state_snapshot.interval_result.result["intervals"][0]["interval_id"] = "native:层/1"
    assert IntervalIndex(other).identities()[0].interval_id != before[0].interval_id


async def test_multi_interval_preserves_order_and_rejects_partial_resolution(
    scope_env, monkeypatch
):
    repository, resolver = scope_env
    execution = await repository.get_execution("task-v1")
    rows = execution.state_snapshot.interval_result.result["intervals"]
    rows.append({**rows[0], "top_depth_m": 2000.5})
    monkeypatch.setattr(repository, "get_execution", AsyncMock(return_value=execution))
    first, second = IntervalIndex(execution).identities()
    ids = [second.interval_id, first.interval_id, second.interval_id]
    result = await resolver.resolve("task", "task-v1", MultiIntervalScope(interval_ids=ids))
    assert result.intervals == [second, first]
    assert result.scope.interval_ids == ids  # 保留原始请求，去重只作用于可信结果集合。
    with pytest.raises(DataError) as error:
        await resolver.resolve(
            "task", "task-v1", MultiIntervalScope(interval_ids=[first.interval_id, "missing"])
        )
    assert error.value.code == "INTERVAL_NOT_FOUND"


async def test_filter_set_unresolved_empty_and_frozen_ids(scope_env):
    _, resolver = scope_env
    with pytest.raises(DataError) as error:
        await resolver.resolve("task", "task-v1", FilterSetScope(filter_expression="所有水层"))
    assert error.value.code == "FILTER_SET_UNRESOLVED"
    empty = await resolver.resolve(
        "task", "task-v1", FilterSetScope(filter_expression="不求值", resolved_ids=[])
    )
    assert empty.scope.resolved_ids == [] and empty.intervals == []
    identity = await resolver.resolve_interval_ordinal("task", "task-v1", 1)
    frozen = FilterSetScope(
        filter_expression="任意文本均不执行", resolved_ids=[identity.interval_id]
    )
    result = await resolver.resolve("task", "task-v1", frozen)
    assert result.intervals == [identity]
    with pytest.raises(DataError) as error:
        await resolver.resolve("task", "task-v2", frozen)
    assert error.value.code == "INTERVAL_NOT_FOUND"


@pytest.mark.parametrize(
    "rows",
    [
        "bad",
        [None],
        [{"top_depth_m": "2000", "bottom_depth_m": 2001}],
        [{"top_depth_m": 2001, "bottom_depth_m": 2000}],
        [{"top_depth_m": True, "bottom_depth_m": 2001}],
        [{"top_depth_m": 2000, "bottom_depth_m": 2001, "interval_id": " "}],
        [{"top_depth_m": 2000, "bottom_depth_m": 2001, "interval_id": "same"}] * 2,
    ],
)
async def test_malformed_intervals_are_not_silently_skipped(scope_env, rows):
    repository, _ = scope_env
    execution = await repository.get_execution("task-v1")
    execution.state_snapshot.interval_result.result["intervals"] = rows
    with pytest.raises(DataError) as error:
        IntervalIndex(execution)
    assert error.value.code == "AMBIGUOUS_SCOPE"


async def test_missing_intervals_and_mutable_snapshot_cannot_produce_identity(scope_env):
    repository, _ = scope_env
    execution = await repository.get_execution("task-v1")
    execution.state_snapshot.interval_result = None
    with pytest.raises(DataError) as error:
        resolve_interval_ordinal(execution, 1)
    assert error.value.code == "INTERVAL_NOT_FOUND"
    active = execution.model_copy(update={"status": ExecutionStatus.QUEUED})
    with pytest.raises(DataError) as error:
        IntervalIndex(active)
    assert error.value.code == "STALE_CONTEXT_REFERENCE"


@pytest.mark.parametrize(
    "task_id,execution_id,code",
    [
        ("task", "foreign-v1", "EXECUTION_NOT_FOUND"),
        ("foreign", "foreign-v1", "TASK_NOT_FOUND"),
        ("task", "missing", "EXECUTION_NOT_FOUND"),
    ],
)
async def test_scope_revalidates_session_and_execution_ownership(
    scope_env, task_id, execution_id, code
):
    _, resolver = scope_env
    with pytest.raises(DataError) as error:
        await resolver.resolve(task_id, execution_id, WholeWellScope())
    assert error.value.code == code
    with pytest.raises(DataError) as error:
        await resolver.resolve_interval_ordinal(task_id, execution_id, 1)
    assert error.value.code == code


async def test_scope_resolution_never_writes_or_creates_executions(scope_env, monkeypatch):
    repository, resolver = scope_env
    before_task = (await repository.get_task("task")).model_dump_json()
    before = [item.model_dump_json() for item in await repository.list_executions("task")]
    for name in (
        "create_execution",
        "set_current_execution",
        "save_execution_state",
        "save_execution_report",
    ):
        monkeypatch.setattr(
            repository, name, AsyncMock(side_effect=AssertionError("resolver attempted mutation"))
        )
    identity = await resolver.resolve_interval_ordinal("task", "task-v1", 1)
    for scope in (
        WholeWellScope(),
        IntervalScope(interval_id=identity.interval_id),
        FilterSetScope(filter_expression="任意文本", resolved_ids=[]),
        DepthPointScope(depth=2000.25, depth_reference="MD"),
    ):
        await resolver.resolve("task", "task-v1", scope)
    assert (await repository.get_task("task")).model_dump_json() == before_task
    assert [item.model_dump_json() for item in await repository.list_executions("task")] == before
