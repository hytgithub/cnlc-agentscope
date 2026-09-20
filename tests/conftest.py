import json
from pathlib import Path

import pytest


@pytest.fixture
def fixture_data():
    path = Path(__file__).parents[1] / "mock_data" / "WELL_MOCK_001.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def data_dir(tmp_path, fixture_data):
    root = tmp_path / "fixtures"
    root.mkdir()
    (root / "WELL_MOCK_001.json").write_text(
        json.dumps(fixture_data, ensure_ascii=False),
        encoding="utf-8",
    )
    return root
