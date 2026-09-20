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
