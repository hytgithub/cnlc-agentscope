import math

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.domain.models import TaskRequest
from cnlc_agent.reports.formatter import MISSING, measurement, number, text
from cnlc_agent.reports.generator import ReportGenerator
from cnlc_agent.reports.models import Measurement, ReportStyle


async def _state(data_dir):
    app = build_application(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None)
    )
    state, _ = await app.run(TaskRequest(well_id="WELL_MOCK_001"))
    return state


async def test_standard_and_compact_render_same_normalized_result(data_dir):
    state = await _state(data_dir)
    generator = ReportGenerator()
    normalized = generator.normalize(state)
    before = normalized.model_dump()

    standard = generator.render(normalized, ReportStyle.STANDARD)
    compact = generator.render(normalized, ReportStyle.COMPACT)

    assert "# 虚构演示井 001测井评价报告" in standard
    assert "# 一、钻井与地质概况" in standard
    assert "## 2、主要目的层处理解释成果" in standard
    assert "# 虚构演示井 001测井解释报告" in compact
    assert "## 四、主要解释成果" in compact
    assert "## 八、存在问题及解释局限" in compact
    assert normalized.model_dump() == before
    assert "油水同层（预设）" in standard
    assert "油水同层（预设）" in compact


def test_report_style_configuration_defaults_and_switches(monkeypatch):
    monkeypatch.delenv("CNLC_REPORT_STYLE", raising=False)
    assert AppSettings(_env_file=None).report_style == ReportStyle.STANDARD

    monkeypatch.setenv("CNLC_REPORT_STYLE", "compact")
    assert AppSettings(_env_file=None).report_style == ReportStyle.COMPACT


async def test_application_uses_configured_report_style(data_dir):
    standard_app = build_application(
        AppSettings(mock_data_dir=data_dir, report_style="standard", _env_file=None)
    )
    compact_app = build_application(
        AppSettings(mock_data_dir=data_dir, report_style="compact", _env_file=None)
    )

    _, standard = await standard_app.run(TaskRequest(well_id="WELL_MOCK_001"))
    _, compact = await compact_app.run(TaskRequest(well_id="WELL_MOCK_001"))

    assert "测井评价报告" in standard
    assert "测井解释报告" in compact


def test_formatter_normalizes_missing_values_without_losing_zero():
    for value in (None, "", "None", "null", "NaN", math.nan, math.inf, -math.inf):
        assert text(value) == MISSING
        assert number(value) == MISSING

    assert number(0) == "0.00"
    assert measurement(Measurement(value=0, unit="mD")) == "0.00 mD"
    assert measurement(Measurement(value=0, unit="fraction")) == "0.00%"


async def test_final_reports_show_missing_marker_and_never_python_nulls(data_dir):
    state = await _state(data_dir)
    generator = ReportGenerator()
    normalized = generator.normalize(state)

    for style in ReportStyle:
        report = generator.render(normalized, style)
        assert MISSING in report
        lowered = report.casefold()
        assert "none" not in lowered
        assert "null" not in lowered
        assert "nan" not in lowered
        assert "W01" not in report
        assert "W10" not in report


async def test_report_localizes_known_model_warning(data_dir):
    state = await _state(data_dir)
    assert state.fluid_result is not None
    english_warning = (
        "Fluid interpretation relies on historical interval statistics; no real-time or "
        "independently computed saturation or fluid typing logs (e.g., NMR, MDT, DST) available"
    )
    state.fluid_result.warnings.append(english_warning)

    normalized = ReportGenerator().normalize(state)

    assert english_warning not in normalized.limitations
    assert (
        "本次流体识别主要依据历史层段统计及既有解释成果；含水饱和度为历史解释值，"
        "未根据原始测井曲线独立复算，流体类型结论也缺少独立验证资料"
        in normalized.limitations
    )


async def test_zero_survives_state_normalization_and_final_rendering(data_dir):
    state = await _state(data_dir)
    assert state.interval_result is not None
    assert state.petrophysics_result is not None
    interval = state.interval_result.result["intervals"][0]
    assert isinstance(interval, dict)
    interval["top_depth_m"] = 0
    interval["bottom_depth_m"] = 0
    interval["gross_thickness_m"] = 0
    interval["effective_thickness_m"] = 0
    state.petrophysics_result.result["porosity"] = {"value": 0, "unit": "fraction"}

    generator = ReportGenerator()
    normalized = generator.normalize(state)
    layer = normalized.layers[0]

    assert layer.top_depth_m == 0
    assert layer.bottom_depth_m == 0
    assert layer.gross_thickness_m == 0
    assert layer.effective_thickness_m == 0
    assert layer.porosity.value == 0
    for style in ReportStyle:
        report = generator.render(normalized, style)
        assert "0.00" in report
        assert "0.00%" in report
