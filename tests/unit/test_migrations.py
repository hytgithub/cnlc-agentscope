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
