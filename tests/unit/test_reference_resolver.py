"""Operation 引用解析的授权、歧义、版本锚点和只读约束。"""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from cnlc_agent.demo.operation_models import ExecutionReference
from cnlc_agent.demo.reference_resolver import OperationReferenceResolver, ReferenceAccessMode
from cnlc_agent.demo.task_context import SessionTaskResolver, TaskReference
from cnlc_agent.domain.errors import DataError
from cnlc_agent.domain.execution import ExecutionStatus
from cnlc_agent.domain.models import TaskRequest, utc_now
from cnlc_agent.domain.session_binding import SessionTaskBinding, TaskSessionIdentity
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository


@pytest.fixture
async def reference_env():
    repository = InMemoryTaskRepository()
    identity = TaskSessionIdentity(user_id="user", agent_id="agent", session_id="session")
    bound_at = utc_now()
    for index, (task_id, well_id) in enumerate(
        [
            ("a1", "WELL_A"),
            ("b", "WELL_B"),
            ("a2", "WELL_A"),
            ("foreign", "WELL_OTHER"),
        ]
    ):
        request = TaskRequest(task_id=task_id, well_id=well_id)
        await repository.create_task(InterpretationState(task=request))
        if task_id != "foreign":
            await repository.bind_task_to_session(
                SessionTaskBinding(
                    **identity.model_dump(),
                    task_id=task_id,
                    created_at=bound_at + timedelta(seconds=index),
                )
            )
        for sequence in [1, 3, 7, 9] if task_id == "a1" else [1]:
            state = InterpretationState(
                task=request, workflow_execution_id=f"{task_id}-v{sequence}"
            )
            await repository.create_execution(state, sequence=sequence)
            await repository.claim_execution(
                state.workflow_execution_id, "worker", utc_now() + timedelta(minutes=1)
            )
            await repository.save_execution_report(state.workflow_execution_id, "测试报告")
            await repository.finish_execution(
                state.workflow_execution_id,
                "worker",
                ExecutionStatus.FAILED if sequence == 9 else ExecutionStatus.SUCCESS,
            )
    return repository, identity, OperationReferenceResolver(repository, identity)


@pytest.mark.parametrize("mode", list(ReferenceAccessMode))
async def test_current_only_task_resolves_without_active(reference_env, mode):
    repository, _, _ = reference_env
    identity = TaskSessionIdentity(user_id="solo", agent_id="agent", session_id="session")
    await repository.bind_task_to_session(SessionTaskBinding(**identity.model_dump(), task_id="b"))
    result = await OperationReferenceResolver(repository, identity).resolve_task(
        TaskReference(), mode
    )
    assert result.task_id == "b"
    assert result.resolution_source == "ONLY_TASK"


@pytest.mark.parametrize("mode", list(ReferenceAccessMode))
async def test_current_active_beats_latest_binding(reference_env, mode):
    _, _, resolver = reference_env
    result = await resolver.resolve_task(TaskReference(), mode, active_task_id="b")
    assert result.task_id == "b"
    assert result.resolution_source == "ACTIVE"


@pytest.mark.parametrize("active", [None, "missing", "foreign"])
async def test_current_multiple_tasks_read_fallback_write_ambiguity(reference_env, active):
    _, _, resolver = reference_env
    result = await resolver.resolve_task(TaskReference(), "READ_ONLY", active_task_id=active)
    assert result.task_id == "a2"
    assert result.resolution_source == "LATEST_BOUND"
    with pytest.raises(DataError) as error:
        await resolver.resolve_task(TaskReference(), "WRITE", active_task_id=active)
    assert error.value.code == "AMBIGUOUS_TASK_REFERENCE"


async def test_previous_task_needs_current_anchor(reference_env):
    _, _, resolver = reference_env
    reference = TaskReference(kind="PREVIOUS_TASK")
    result = await resolver.resolve_task(reference, "WRITE", active_task_id="b")
    assert result.task_id == "a1"
    assert result.anchor_task_id == "b"
    assert result.resolution_source == "PREVIOUS_BOUND"
    assert (await resolver.resolve_task(reference, "READ_ONLY")).task_id == "b"
    with pytest.raises(DataError) as error:
        await resolver.resolve_task(reference, "WRITE")
    assert error.value.code == "AMBIGUOUS_TASK_REFERENCE"
    with pytest.raises(DataError) as error:
        await resolver.resolve_task(reference, "WRITE", active_task_id="a1")
    assert error.value.code == "PREVIOUS_TASK_NOT_FOUND"


@pytest.mark.parametrize("mode", list(ReferenceAccessMode))
async def test_explicit_unique_well_and_bound_task(reference_env, mode):
    _, _, resolver = reference_env
    for kind, value, source in [
        ("WELL_ID", "WELL_B", "EXPLICIT_WELL"),
        ("TASK_ID", "b", "EXPLICIT_TASK"),
    ]:
        result = await resolver.resolve_task(TaskReference(kind=kind, value=value), mode)
        assert result.task_id == "b"
        assert result.match_count == 1
        assert result.resolution_source == source


async def test_same_well_read_is_latest_write_uses_matching_active(reference_env):
    _, _, resolver = reference_env
    reference = TaskReference(kind="WELL_ID", value="WELL_A")
    result = await resolver.resolve_task(reference, "READ_ONLY", active_task_id="a1")
    assert result.task_id == "a2" and result.match_count == 2
    assert result.resolution_source == "LATEST_BOUND"
    result = await resolver.resolve_task(reference, "WRITE", active_task_id="a1")
    assert result.task_id == "a1" and result.match_count == 2
    assert result.resolution_source == "ACTIVE"
    for active in (None, "b", "foreign", "missing"):
        with pytest.raises(DataError) as error:
            await resolver.resolve_task(reference, "WRITE", active_task_id=active)
        assert error.value.code == "AMBIGUOUS_TASK_REFERENCE"


@pytest.mark.parametrize(
    "reference,code",
    [
        (TaskReference(kind="TASK_ID", value="foreign"), "TASK_NOT_FOUND"),
        (TaskReference(kind="TASK_ID", value="missing"), "TASK_NOT_FOUND"),
        (TaskReference(kind="WELL_ID", value="WELL_OTHER"), "SESSION_WELL_NOT_FOUND"),
    ],
)
async def test_unbound_identifiers_never_authorize(reference_env, reference, code):
    _, _, resolver = reference_env
    with pytest.raises(DataError) as error:
        await resolver.resolve_task(reference, "READ_ONLY")
    assert error.value.code == code


@pytest.mark.parametrize("field", ["user_id", "agent_id", "session_id"])
async def test_entire_session_identity_is_required(reference_env, field):
    repository, identity, _ = reference_env
    resolver = OperationReferenceResolver(repository, identity.model_copy(update={field: "other"}))
    with pytest.raises(DataError) as error:
        await resolver.resolve_task(TaskReference(kind="TASK_ID", value="a1"), "WRITE")
    assert error.value.code == "TASK_NOT_FOUND"
    with pytest.raises(DataError) as error:
        await resolver.resolve_execution("a1", ExecutionReference(kind="TASK_CURRENT"))
    assert error.value.code == "TASK_NOT_FOUND"


@pytest.mark.parametrize(
    "payload,base,expected",
    [
        ({"kind": "TASK_CURRENT"}, "a1-v3", "a1-v9"),
        ({"kind": "ACTIVE_BASE"}, "a1-v3", "a1-v3"),
        ({"kind": "LATEST_SUCCESSFUL"}, None, "a1-v7"),
        ({"kind": "FIRST"}, None, "a1-v1"),
        ({"kind": "SEQUENCE", "sequence": 3}, None, "a1-v3"),
        ({"kind": "EXECUTION_ID", "execution_id": "a1-v7"}, None, "a1-v7"),
        ({"kind": "PREVIOUS"}, None, "a1-v7"),
        ({"kind": "PREVIOUS"}, "a1-v7", "a1-v3"),
    ],
)
async def test_execution_selectors_and_anchor(reference_env, payload, base, expected):
    _, _, resolver = reference_env
    result = await resolver.resolve_execution(
        "a1", ExecutionReference(**payload), active_base_execution_id=base
    )
    assert result.execution_id == expected
    assert result.task_id == "a1"
    assert result.resolution_source == payload["kind"]
    if payload["kind"] == "PREVIOUS":
        assert result.anchor_execution_id == (base or "a1-v9")
    assert "state_snapshot" not in result.model_dump()
    assert type(result).model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("base", [None, "missing", "b-v1", ""])
async def test_active_base_never_falls_back(reference_env, base):
    _, _, resolver = reference_env
    with pytest.raises(DataError) as error:
        await resolver.resolve_execution(
            "a1", ExecutionReference(kind="ACTIVE_BASE"), active_base_execution_id=base
        )
    assert error.value.code == "STALE_CONTEXT_REFERENCE"
    if base is not None:
        with pytest.raises(DataError) as error:
            await resolver.resolve_execution(
                "a1", ExecutionReference(kind="PREVIOUS"), active_base_execution_id=base
            )
        assert error.value.code == "STALE_CONTEXT_REFERENCE"


@pytest.mark.parametrize(
    "payload,base",
    [
        ({"kind": "EXECUTION_ID", "execution_id": "b-v1"}, None),
        ({"kind": "EXECUTION_ID", "execution_id": "missing"}, None),
        ({"kind": "SEQUENCE", "sequence": 2}, None),
        ({"kind": "PREVIOUS"}, "a1-v1"),
    ],
)
async def test_missing_or_cross_task_execution_rejected(reference_env, payload, base):
    _, _, resolver = reference_env
    with pytest.raises(DataError) as error:
        await resolver.resolve_execution(
            "a1", ExecutionReference(**payload), active_base_execution_id=base
        )
    assert error.value.code == "EXECUTION_NOT_FOUND"


@pytest.mark.parametrize(
    "kind,field",
    [
        ("TASK_CURRENT", "current_execution_id"),
        ("LATEST_SUCCESSFUL", "latest_successful_execution_id"),
    ],
)
@pytest.mark.parametrize("pointer", [None, "missing", "b-v1"])
async def test_task_pointers_are_revalidated(reference_env, monkeypatch, kind, field, pointer):
    repository, _, resolver = reference_env
    task = await repository.get_task("a1")
    task = task.model_copy(update={field: pointer})
    monkeypatch.setattr(repository, "get_task", AsyncMock(return_value=task))
    with pytest.raises(DataError) as error:
        await resolver.resolve_execution("a1", ExecutionReference(kind=kind))
    assert error.value.code == "EXECUTION_NOT_FOUND"


async def test_sequence_selection_ignores_repository_order_and_rejects_duplicates(
    reference_env, monkeypatch
):
    repository, _, resolver = reference_env
    items = await repository.list_executions("a1")
    monkeypatch.setattr(
        repository, "list_executions", AsyncMock(return_value=list(reversed(items)))
    )
    assert (await resolver.resolve_execution("a1", ExecutionReference(kind="FIRST"))).sequence == 1
    assert (
        await resolver.resolve_execution("a1", ExecutionReference(kind="PREVIOUS"))
    ).sequence == 7
    monkeypatch.setattr(repository, "list_executions", AsyncMock(return_value=[items[0], items[0]]))
    for payload in ({"kind": "FIRST"}, {"kind": "SEQUENCE", "sequence": 1}, {"kind": "PREVIOUS"}):
        with pytest.raises(DataError) as error:
            await resolver.resolve_execution("a1", ExecutionReference(**payload))
        assert error.value.code == "AMBIGUOUS_EXECUTION_REFERENCE"


async def test_execution_list_cannot_leak_other_task(reference_env, monkeypatch):
    repository, _, resolver = reference_env
    monkeypatch.setattr(
        repository, "list_executions", AsyncMock(return_value=await repository.list_executions("b"))
    )
    with pytest.raises(DataError) as error:
        await resolver.resolve_execution("a1", ExecutionReference(kind="FIRST"))
    assert error.value.code == "EXECUTION_NOT_FOUND"


async def test_resolution_is_read_only_and_legacy_fallback_is_unchanged(reference_env, monkeypatch):
    repository, identity, resolver = reference_env
    before = [(await repository.get_task(task)).model_dump_json() for task in ("a1", "b", "a2")]
    executions = [item.model_dump_json() for item in await repository.list_executions("a1")]
    for name in (
        "create_execution",
        "set_current_execution",
        "save_execution_state",
        "save_execution_report",
        "bind_task_to_session",
    ):
        monkeypatch.setattr(
            repository, name, AsyncMock(side_effect=AssertionError("resolver attempted mutation"))
        )
    await resolver.resolve_task(TaskReference(), "READ_ONLY")
    await resolver.resolve_execution("a1", ExecutionReference(kind="PREVIOUS"))
    assert [
        (await repository.get_task(task)).model_dump_json() for task in ("a1", "b", "a2")
    ] == before
    assert [item.model_dump_json() for item in await repository.list_executions("a1")] == executions
    legacy = SessionTaskResolver(repository, await repository.list_session_task_ids(identity))
    assert (await legacy.resolve(TaskReference(), None)).task_id == "a2"
    assert (
        await legacy.resolve(TaskReference(kind="WELL_ID", value="WELL_A"), "a1")
    ).task_id == "a2"
