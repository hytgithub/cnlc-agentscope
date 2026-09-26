"""SessionTaskResolver 不同任务引用的确定性解析。"""

import pytest

from cnlc_agent.demo.task_context import SessionTaskResolver, TaskReference
from cnlc_agent.domain.errors import ApplicationError
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.infrastructure.mock import InMemoryTaskRepository


async def test_resolver_current_previous_well_and_task_references():
    repository = InMemoryTaskRepository()
    task_a = TaskRequest(well_id="WELL_A")
    task_b = TaskRequest(well_id="WELL_B")
    task_a2 = TaskRequest(well_id="WELL_A")
    for task in (task_a, task_b, task_a2):
        await repository.create_task(InterpretationState(task=task))
    resolver = SessionTaskResolver(
        repository,
        [task_a.task_id, task_b.task_id, task_a2.task_id],
    )

    assert (await resolver.resolve(TaskReference(kind="CURRENT"), task_b.task_id)).task_id == (
        task_b.task_id
    )
    assert (
        await resolver.resolve(TaskReference(kind="PREVIOUS_TASK"), task_b.task_id)
    ).task_id == task_a.task_id
    # 同井多 Task 按 Session binding 稳定顺序选最近一个。
    assert (
        await resolver.resolve(TaskReference(kind="WELL_ID", value="WELL_A"), None)
    ).task_id == task_a2.task_id
    assert (
        await resolver.resolve(
            TaskReference(kind="TASK_ID", value=task_b.task_id), task_a.task_id
        )
    ).task_id == task_b.task_id
    # active task 不在当前 Session binding 时只 fallback 到最近绑定任务。
    assert (
        await resolver.resolve(TaskReference(kind="CURRENT"), "other-session-task")
    ).task_id == task_a2.task_id


async def test_resolver_rejects_unbound_previous_and_well():
    repository = InMemoryTaskRepository()
    task = TaskRequest(well_id="WELL_A")
    await repository.create_task(InterpretationState(task=task))
    resolver = SessionTaskResolver(repository, [task.task_id])

    with pytest.raises(ApplicationError, match="没有上一口井") as previous:
        await resolver.resolve(TaskReference(kind="PREVIOUS_TASK"), task.task_id)
    assert previous.value.code == "PREVIOUS_TASK_NOT_FOUND"

    with pytest.raises(ApplicationError, match="WELL_B") as missing_well:
        await resolver.resolve(TaskReference(kind="WELL_ID", value="WELL_B"), task.task_id)
    assert missing_well.value.code == "SESSION_WELL_NOT_FOUND"

    with pytest.raises(ApplicationError) as unbound_task:
        await resolver.resolve(TaskReference(kind="TASK_ID", value="unbound"), task.task_id)
    assert unbound_task.value.code == "TASK_NOT_FOUND"

