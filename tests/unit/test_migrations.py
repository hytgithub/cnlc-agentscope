import os
import subprocess
import sys
from pathlib import Path


def test_initial_migration_generates_postgresql_sql():
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://localhost/cnlc"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE interpretation_task" in result.stdout
    assert "snapshot JSONB NOT NULL" in result.stdout
    assert "markdown TEXT NOT NULL" in result.stdout
    assert "ix_interpretation_task_status" in result.stdout
    assert "CREATE TABLE interpretation_execution" in result.stdout
    assert "uq_execution_task_sequence" in result.stdout
    assert "current_execution_id" in result.stdout
    assert "latest_successful_execution_id" in result.stdout
    assert "INSERT INTO interpretation_execution" in result.stdout
    assert "CREATE TABLE interpretation_input_version" in result.stdout
    assert "uq_input_version_task_sequence" in result.stdout
    assert "current_input_version_id" in result.stdout
    assert "input_version_id" in result.stdout
    assert "override_snapshot" in result.stdout
    assert "CREATE TABLE interpretation_tool_run" in result.stdout
    assert "ix_interpretation_tool_run_execution_id" in result.stdout
    assert "ADD COLUMN start_step" in result.stdout
    assert "ADD COLUMN lease_owner" in result.stdout
    assert "ADD COLUMN lease_expires_at" in result.stdout
    assert "ADD COLUMN error_code" in result.stdout
    assert "WORKER_LEASE_EXPIRED" not in result.stdout
    assert "LEGACY_EXECUTION_INCOMPLETE" in result.stdout
    assert "CREATE TABLE interpretation_session_task_binding" in result.stdout
    assert "pk_interpretation_session_task_binding" in result.stdout
    assert "ix_session_task_binding_session" in result.stdout
    assert "ix_session_task_binding_task_id" in result.stdout
    assert "CREATE TABLE conversation_session" in result.stdout
    assert "CREATE TABLE conversation_message" in result.stdout
    assert "pk_conversation_session" in result.stdout
    assert "uq_conversation_message_session_sequence" in result.stdout
    assert "ix_conversation_session_last_active" in result.stdout
    assert "ix_conversation_message_session_sequence" in result.stdout
    assert (
        "FOREIGN KEY(execution_id) REFERENCES interpretation_execution "
        "(execution_id) ON DELETE CASCADE"
        in result.stdout
    )
    assert (
        "FOREIGN KEY(task_id) REFERENCES interpretation_task (task_id) ON DELETE CASCADE"
        in result.stdout
    )


def test_versioned_execution_migration_downgrades_to_0001():
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0002:0001", "--sql"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://localhost/cnlc"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "DROP TABLE interpretation_execution" in result.stdout
    assert "DROP COLUMN current_execution_id" in result.stdout


def test_versioned_input_migration_downgrades_to_0002():
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0003:0002", "--sql"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://localhost/cnlc"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "DROP TABLE interpretation_input_version" in result.stdout
    assert "DROP COLUMN input_version_id" in result.stdout


def test_tool_run_migration_downgrades_to_0003():
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0004:0003", "--sql"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://localhost/cnlc"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "DROP TABLE interpretation_tool_run" in result.stdout


def test_execution_lifecycle_migration_downgrades_to_0004():
    """0005 回退只删除新增的生命周期列与索引。"""

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0005:0004", "--sql"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://localhost/cnlc"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "DROP INDEX ix_interpretation_execution_lease_expires_at" in result.stdout
    assert "DROP COLUMN lease_owner" in result.stdout
    assert "DROP COLUMN start_step" in result.stdout


def test_session_task_binding_migration_downgrades_to_0005():
    """0006 回退只删除绑定索引和表。"""

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0006:0005", "--sql"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://localhost/cnlc"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "DROP INDEX ix_session_task_binding_task_id" in result.stdout
    assert "DROP INDEX ix_session_task_binding_session" in result.stdout
    assert "DROP TABLE interpretation_session_task_binding" in result.stdout


def test_session_task_binding_migration_upgrades_from_0005():
    """0006 明确创建四元组主键、级联外键与两个查询索引。"""

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0005:0006", "--sql"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://localhost/cnlc"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE interpretation_session_task_binding" in result.stdout
    assert (
        "PRIMARY KEY (user_id, agent_id, session_id, task_id)" in result.stdout
    )
    assert (
        "FOREIGN KEY(task_id) REFERENCES interpretation_task (task_id) ON DELETE CASCADE"
        in result.stdout
    )
    assert "CREATE INDEX ix_session_task_binding_session" in result.stdout
    assert "CREATE INDEX ix_session_task_binding_task_id" in result.stdout


def test_conversation_migration_round_trip_sql():
    """0007 明确隔离对话生命周期，并支持独立回退。"""

    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0006:0007", "--sql"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://localhost/cnlc"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    assert "CREATE TABLE conversation_session" in upgrade.stdout
    assert "CREATE TABLE conversation_message" in upgrade.stdout
    assert "PRIMARY KEY (user_id, agent_id, session_id)" in upgrade.stdout
    assert "UNIQUE (user_id, agent_id, session_id, sequence)" in upgrade.stdout
    assert "ON DELETE CASCADE" in upgrade.stdout

    downgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0007:0006", "--sql"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": "postgresql+asyncpg://localhost/cnlc"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert downgrade.returncode == 0, downgrade.stderr
    assert "DROP TABLE conversation_message" in downgrade.stdout
    assert "DROP TABLE conversation_session" in downgrade.stdout
