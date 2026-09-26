"""Offline demo acceptance; Task 005 live-model integration remains separate."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cnlc_agent.domain.models import ErrorDetail, StageResult, TaskRequest
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.reports.assembler import ReportAssembler


@pytest.mark.parametrize("warning", [False, True])
def test_demo_cli_exports_complete_state(tmp_path, fixture_data, warning):
    if warning:
        fixture_data["raw_data"]["auxiliary"].pop("core")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "WELL_MOCK_001.json").write_text(json.dumps(fixture_data))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CNLC_") and not k.lower().endswith("_proxy")
    }
    env.update(CNLC_MODEL_PROVIDER="mock", CNLC_PERSISTENCE="memory")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cnlc_agent.main",
            "--well-id",
            "WELL_MOCK_001",
            "--data-dir",
            str(fixtures),
            "--output-dir",
            str(tmp_path / "outputs"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    exported = Path(summary["result"]).read_text()
    state = InterpretationState.model_validate_json(exported)
    assert state.status.value == ("WARNING" if warning else "SUCCESS")
    assert [step.value for step in state.completed_steps] == [f"W{i:02}" for i in range(1, 11)]
    markdown = Path(summary["report"]).read_text()
    for heading in [
        "钻井与地质概况",
        "测井采集资料质量评价",
        "快速处理与解释",
        "主要目的层处理解释成果",
        "测试建议",
        "岩石物理分析",
        "综合评价",
        "存在问题及建议",
    ]:
        assert heading in markdown
    assert state.fluid_result.result["fluid_type"] in markdown
    assert state.layer_classification.result["layer_type"] in markdown
    interval = state.interval_result.result["intervals"][0]
    for key in ["top_depth_m", "bottom_depth_m", "gross_thickness_m", "effective_thickness_m"]:
        assert str(interval[key]) in markdown
    assert "Demo" in markdown and "Mock" in markdown and '"is_mock": true' in exported
    assert "不代表经过独立专业复算" in markdown
    assert "W01" not in markdown and "W10" not in markdown
    assert "Traceback" not in result.stdout + result.stderr + exported + markdown


def test_export_redacts_secrets_errors_and_preserves_state():
    secret = "sk-demo-test-credential"
    raw_error = "PrivateProviderException: internal upstream response body"
    state = InterpretationState(task=TaskRequest(well_id="WELL_MOCK_001"))
    state.errors.append(ErrorDetail(code="MODEL_SERVER_ERROR", message=raw_error))
    state.fluid_result = StageResult(
        source="fixture:fluid",
        is_mock=True,
        result={"fluid_type": "油水（预设）", "api_key": secret},
        warnings=[f"provider echoed {secret}"],
    )
    before = state.model_dump_json()
    assembler = ReportAssembler()
    exported = assembler.to_json(state)
    markdown = assembler.to_markdown(state)
    for output in [exported, markdown]:
        assert secret not in output
        assert raw_error not in output
        assert "MODEL_SERVER_ERROR" in output
    InterpretationState.model_validate_json(exported)
    assert state.model_dump_json() == before


def test_explicit_demo_skip_is_not_missing_result():
    state = InterpretationState(task=TaskRequest(well_id="WELL_MOCK_001"))
    state.fluid_result = StageResult(
        source="demo",
        is_mock=True,
        result={"demo_skipped": True},
    )
    markdown = ReportAssembler().to_markdown(state)
    assert "Demo Skip：是" in markdown
    assert "未生成结果；不推断专业结论" in markdown
    assert json.loads(ReportAssembler().to_json(state))["fluid_result"]["result"]["demo_skipped"]


def test_cli_model_failure_does_not_leak_provider_response(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    secret = "sk-demo-e2e-fake-key"
    raw_error = "PrivateProviderException with internal diagnostic payload"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(f"{raw_error}: {secret}".encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("CNLC_") and not k.lower().endswith("_proxy")
        }
        env.update(
            CNLC_MODEL_PROVIDER="openai_compatible",
            CNLC_PERSISTENCE="memory",
            MODEL_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1",
            MODEL_API_KEY=secret,
            MODEL_NAME="demo-test",
            CNLC_MODEL_MAX_RETRIES="0",
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "cnlc_agent.main",
                "--data-dir",
                str(Path(__file__).parents[2] / "mock_data"),
                "--output-dir",
                str(tmp_path / "outputs"),
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 1
    assert result.stdout, result.stderr
    summary = json.loads(result.stdout)
    exported = Path(summary["result"]).read_text()
    markdown = Path(summary["report"]).read_text()
    state = InterpretationState.model_validate_json(exported)
    assert state.status.value == "FAILED"
    assert state.fluid_result is None
    assert "MODEL_AUTH_ERROR" in exported + markdown
    assert "任务未完成" in markdown
    for output in [result.stdout, result.stderr, exported, markdown]:
        assert secret not in output
        assert raw_error not in output
