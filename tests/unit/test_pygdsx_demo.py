"""验证 GDSX 只读演示入口的预览与完整输出。"""

import json
from pathlib import Path

import pytest

from cnlc_agent.pygdsx.curve import (
    BDCurveInfo,
    create_gdx_curve,
    write_gdx_curve_data_by_index,
)
from cnlc_agent.pygdsx.demo_read_gdsx import inspect_gdsx, main
from cnlc_agent.pygdsx.info import set_gdx_well_info
from cnlc_agent.pygdsx.table import BDTableInfo, create_gdsx_table, write_gdx_table_all_data
from cnlc_agent.pygdsx.welllog import create_gdx_log


@pytest.fixture
def demo_gdsx(tmp_path: Path) -> Path:
    """使用现有写入 API 构造一口最小测试井。"""
    path = tmp_path / "demo.gdsx"
    assert create_gdx_log(str(path))
    assert set_gdx_well_info(str(path), {"LEGALNAME": "DEMO"})
    curve = BDCurveInfo()
    curve.name = "GR"
    curve.dimension1Start = 100.0
    curve.dimension1End = 101.0
    curve.dimension1Step = 0.5
    curve.dimension1Length = 3
    curve.dimension2Unit = "API"
    assert create_gdx_curve(str(path), curve)
    assert write_gdx_curve_data_by_index(str(path), "GR", 0, 3, [10, 20, 30])

    table = BDTableInfo()
    table.tableName = "OGRESULT"
    table.tableType = "OGRESULT"
    assert create_gdsx_table(str(path), "OGRESULT", table, [{"name": "result"}])
    assert write_gdx_table_all_data(str(path), "OGRESULT", [{"result": "water"}])
    return path


def test_preview_and_all_values(demo_gdsx: Path) -> None:
    preview = inspect_gdsx(demo_gdsx, sample_size=2)
    assert preview["well_info"]["LEGALNAME"] == "DEMO"
    assert preview["curve_count"] == 1
    assert preview["curves"][0]["values"] == [10, 20]
    assert preview["curves"][0]["values_truncated"] is True
    assert preview["tables"][0]["rows"] == [{"result": "water"}]

    complete = inspect_gdsx(demo_gdsx, include_all=True)
    assert complete["curves"][0]["values"] == [10, 20, 30]
    assert complete["curves"][0]["metadata"]["dimension2Unit"] == "API"
    assert complete["tables"][0]["metadata"]["tableType"] == "OGRESULT"
    assert complete["curves"][0]["values_truncated"] is False


def test_cli_prints_json(demo_gdsx: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(demo_gdsx), "--curve", "GR", "--compact"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["curves"][0]["name"] == "GR"


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        inspect_gdsx(tmp_path / "missing.gdsx")
