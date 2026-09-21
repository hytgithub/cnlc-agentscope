import json
import subprocess
import sys
from pathlib import Path


def test_cli_exports_both_reports(tmp_path):
    repository = Path(__file__).parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "cnlc_agent.main", "--output-dir", str(tmp_path)],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "SUCCESS"
    payload = json.loads(Path(summary["result"]).read_text(encoding="utf-8"))
    assert len(payload["completed_steps"]) == 10
    assert "Mock" in Path(summary["report"]).read_text(encoding="utf-8")


def test_cli_unknown_well_exits_nonzero_with_diagnostics(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cnlc_agent.main",
            "--well-id",
            "MISSING",
            "--output-dir",
            str(tmp_path),
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    summary = json.loads(result.stdout)
    assert summary["status"] == "FAILED"
    assert "诊断摘要" in Path(summary["report"]).read_text(encoding="utf-8")


def test_history_output_cannot_escape_output_directory(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cnlc_agent import main as cli
    from cnlc_agent.domain.models import TaskRequest
    from cnlc_agent.domain.state import InterpretationState

    state = InterpretationState(task=TaskRequest(task_id="../outside", well_id="WELL_1"))
    repository = AsyncMock()
    repository.get.return_value = state
    repository.get_report.return_value = "diagnostic report"

    @asynccontextmanager
    async def runtime(settings):
        yield SimpleNamespace(repository=repository)

    monkeypatch.setattr(cli, "application_runtime", runtime)
    monkeypatch.setattr(
        sys, "argv", ["cnlc-agent", "--task-id", "../outside", "--output-dir", str(tmp_path)]
    )
    assert cli.main() == 1
    assert not (tmp_path.parent / "outside").exists()
    assert len(list(tmp_path.glob("*/result.json"))) == 1
