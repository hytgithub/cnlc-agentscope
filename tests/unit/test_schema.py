import json
from pathlib import Path

import pytest

from cnlc_agent.schema import CONTRACTS, export_schemas, main


def test_checked_in_schemas_match_runtime():
    assert export_schemas(Path(__file__).resolve().parents[2] / "schemas", check=True)
    assert len(CONTRACTS) == 8


def test_schema_check_detects_drift_without_overwriting(tmp_path):
    assert export_schemas(tmp_path)
    target = tmp_path / "well-data.schema.json"
    target.write_text("{}", encoding="utf-8")
    assert not export_schemas(tmp_path, check=True)
    assert target.read_text() == "{}"


def test_validate_fixture_and_runtime_only_constraints(tmp_path, fixture_data, capsys):
    path = tmp_path / "well.json"
    path.write_text(json.dumps(fixture_data), encoding="utf-8")
    assert main(["validate", "mock-fixture", str(path)]) == 0
    fixture_data["raw_data"]["curves"]["GR"]["values"].pop()
    path.write_text(json.dumps(fixture_data), encoding="utf-8")
    assert main(["validate", "mock-fixture", str(path)]) == 1
    assert "raw_data: value_error" in capsys.readouterr().err


@pytest.mark.parametrize("content", ["{", '{"well_id":"secret/invalid"}'])
def test_validation_errors_do_not_echo_input(tmp_path, capsys, content):
    path = tmp_path / "input.json"
    path.write_text(content, encoding="utf-8")
    assert main(["validate", "task-request", str(path)]) == 1
    assert "secret" not in capsys.readouterr().err


def test_missing_file_has_io_exit_code(tmp_path):
    assert main(["validate", "well-data", str(tmp_path / "missing")]) == 2
