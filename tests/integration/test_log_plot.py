"""多道图必须来自当前执行数据，旧摘要、缺值与历史版本不得被补造或混用。"""

import asyncio
import json
from pathlib import Path

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.demo.log_plot import build_log_plot
from cnlc_agent.demo.read_models import present_execution_view
from cnlc_agent.domain.models import MockFixture, TaskRequest


async def test_plot_flows_through_company_batches_and_execution_read_model():
    app = build_application(
        AppSettings(
            mode="demo",
            professional_provider="company_mock",
            model_provider="mock",
            _env_file=None,
        )
    )
    fixture = MockFixture.model_validate_json(
        await asyncio.to_thread(
            Path("mock_data/WELL_MOCK_PLOT_001.json").read_text, encoding="utf-8"
        )
    )
    try:
        state, _ = await app.run(TaskRequest(well_id=fixture.well.well_id))
        execution = await app.repository.get_execution(state.workflow_execution_id)
        view = await present_execution_view(app.repository, execution)
        plot = view.log_plot
        assert plot is not None
        assert len(plot.curves) == 20
        assert len(plot.intervals) == 20
        assert all(len(c.depths) == len(c.values) == 1201 for c in plot.curves)
        assert all(c.depths[0] == 2000 and c.depths[-1] == 2120 for c in plot.curves)
        assert all(i.formation and i.layer_no and i.lithology for i in plot.intervals)
        assert next(c for c in plot.curves if c.name == "GR").values[30:34] == [None] * 4
        por = next(c for c in plot.curves if c.name == "POR")
        assert (
            por.values
            == fixture.outputs["petrophysics"].result["curve_data"]["curves"]["POR"]["values"]
        )
        assert por.is_mock and "company:interpretation" in por.source
        original = state.model_dump_json()
        state.effective_override.por = 0.25
        modified = build_log_plot(state)
        assert not any(c.name == "POR" for c in modified.curves)
        assert any("尚未重算" in w for w in modified.warnings)
        # 原来取出的历史图保持自己的数据，修改参数不能污染它。
        assert (
            por.values
            == fixture.outputs["petrophysics"].result["curve_data"]["curves"]["POR"]["values"]
        )
        assert json.loads(original)["effective_override"]["por"] is None
    finally:
        await app.close()


async def test_legacy_summary_is_not_expanded_and_invalid_samples_do_not_break_report(data_dir):
    app = build_application(
        AppSettings(
            mode="demo",
            professional_provider="company_mock",
            model_provider="mock",
            mock_data_dir=data_dir,
            _env_file=None,
        )
    )
    try:
        state, _ = await app.run(TaskRequest(well_id="WELL_MOCK_001"))
        plot = build_log_plot(state)
        assert all(c.kind == "raw" for c in plot.curves)
        assert len(plot.intervals) == 1
        state.petrophysics_result.result["curve_data"] = {
            "depths": [2000, 2001],
            "curves": {"POR": {"unit": "fraction", "values": [0.1]}},
        }
        state.interval_result.result["intervals"] = [
            {
                "top_depth_m": True,
                "bottom_depth_m": 2001,
                "layer_type": "无效层段",
            }
        ]
        plot = build_log_plot(state)
        assert len(plot.curves) == 6
        assert not plot.intervals
        assert any("格式无效" in w for w in plot.warnings)
        assert "auxiliary" not in plot.model_dump_json()
    finally:
        await app.close()
