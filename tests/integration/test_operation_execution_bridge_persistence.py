"""真实 PostgreSQL/Redis 验证 Operation Bridge 的授权、命令和 Context 边界。"""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine

from cnlc_agent.application.commands import ModifyInterpretationCommand
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.demo.operation_execution_bridge import OperationExecutionBridge
from cnlc_agent.demo.operation_parser import PartialOperationPlan
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.domain.errors import DataError
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.session_binding import TaskSessionIdentity
from cnlc_agent.infrastructure.database import PostgreSQLTaskRepository, TaskRow

ROOT = Path(__file__).resolve().parents[2]
DB_URL = os.environ.get("CNLC_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("CNLC_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not (DB_URL and REDIS_URL),
    reason="Set CNLC_TEST_DATABASE_URL and CNLC_TEST_REDIS_URL for real services",
)


@pytest.fixture(scope="module")
def migrated_operation_bridge():
    """显式升级真实测试库；应用运行时本身仍不自动创建或修改 Schema。"""

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": DB_URL or ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, "Real PostgreSQL migration failed"


def operation(action: str, task_id: str, **operation_fields: object) -> PartialOperationPlan:
    """构造最小完整程序化计划；测试不经过自然语言或 ReAct。"""

    node: dict[str, object] = {
        "operation_id": "op1",
        "action": action,
        "target": "POROSITY" if action == "MODIFY_PARAMETER" else "WELL",
        "task_reference": {"kind": "TASK_ID", "value": task_id},
        "scope": {"kind": "WHOLE_WELL"},
    }
    node.update(operation_fields)
    return PartialOperationPlan.model_validate(
        {
            "input_classification": (
                "READ_REQUEST" if action in {"STATUS", "REPORT"} else "EXECUTION_REQUEST"
            ),
            "persist_mode": "CREATE_VERSION",
            "original_instruction": "真实持久化执行桥测试",
            "operations": [node],
        }
    )


async def test_operation_bridge_real_persistence_and_binding(
    migrated_operation_bridge,
):
    """真实适配器下读不建版本、写持久化，缓存焦点不能绕过 Binding。"""

    del migrated_operation_bridge
    token = uuid4().hex
    prefix = f"cnlc:test:operation-bridge:{token}"
    identity = TaskSessionIdentity(
        user_id=f"bridge-user-{token}",
        agent_id="bridge-agent",
        session_id=f"bridge-session-{token}",
    )
    settings = AppSettings(
        mode="demo",
        model_provider="mock",
        mock_data_dir=ROOT / "mock_data",
        _env_file=None,
    )
    persistence = PersistenceSettings(
        persistence="postgres-redis", redis_prefix=prefix, _env_file=None
    )
    connections = ConnectionSettings(
        database_url=DB_URL, redis_url=REDIS_URL, _env_file=None
    )
    runner = TaskCommandRunner(
        settings=settings,
        persistence=persistence,
        connections=connections,
        session_identity=identity,
    )
    engine = create_async_engine(DB_URL)
    repository = PostgreSQLTaskRepository(engine)
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    task_id = ""
    try:
        initial = await runner.run("WELL_MOCK_001")
        task_id = initial.task_id
        await runner.wait_for_completion(task_id, initial.execution_id)
        bridge = OperationExecutionBridge(runner)

        before = await repository.list_executions(task_id)
        status = await bridge.execute(operation("STATUS", task_id))
        assert status.outcome == "SUCCESS"
        assert status.results[0].execution_id == initial.execution_id
        assert len(await repository.list_executions(task_id)) == len(before) == 1

        modified = await bridge.execute(
            operation(
                "MODIFY_PARAMETER",
                task_id,
                parameters={
                    "value": {"mode": "ABSOLUTE", "value": 0.17, "unit": "1"}
                },
                execution_reference={"kind": "TASK_CURRENT"},
            )
        )
        assert modified.outcome == "SUCCESS"
        assert len(modified.created_execution_ids) == 1
        modified_id = modified.created_execution_ids[0]
        assert len(await repository.list_executions(task_id)) == 2
        await runner.wait_for_completion(task_id, modified_id)

        # E1 的 expected-current 前置条件必须在真实 Repository 上拒绝旧计划。
        with pytest.raises(DataError) as stale:
            await runner.execute(
                ModifyInterpretationCommand(
                    task_id=task_id, changes=InterpretationOverride(por=0.18)
                ),
                expected_current_execution_id=initial.execution_id,
            )
        assert stale.value.code == "STALE_EXECUTION_PLAN"
        assert len(await repository.list_executions(task_id)) == 2

        current_before_read = (await repository.get_task(task_id)).current_execution_id
        report = await bridge.execute(
            operation(
                "REPORT",
                task_id,
                target="REPORT",
                execution_reference={
                    "kind": "EXECUTION_ID",
                    "execution_id": initial.execution_id,
                },
            )
        )
        assert report.outcome == "SUCCESS"
        assert report.results[0].execution_id == initial.execution_id
        assert runner.interaction_context.view.execution_id == initial.execution_id
        assert (await repository.get_task(task_id)).current_execution_id == current_before_read

        foreign = TaskCommandRunner(
            settings=settings,
            persistence=persistence,
            connections=connections,
            session_identity=identity.model_copy(update={"user_id": f"foreign-{token}"}),
        )
        # Redis/runtime 中出现真实 ID 仍只是 hint，不能替代 PostgreSQL Binding。
        foreign.attach_session_runtime_context({"cnlc_active_task_id": task_id})
        denied = await OperationExecutionBridge(foreign).execute(operation("STATUS", task_id))
        assert denied.error_code == "TASK_NOT_FOUND"
        assert len(await repository.list_executions(task_id)) == 2
    finally:
        await runner.dispatcher.shutdown()
        if task_id:
            async with engine.begin() as connection:
                await connection.execute(delete(TaskRow).where(TaskRow.task_id == task_id))
        keys = await redis.keys(f"{prefix}*")
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
        await engine.dispose()
