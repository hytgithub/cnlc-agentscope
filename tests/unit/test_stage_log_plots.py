"""阶段曲线投影测试：大阶段只能展示已经形成的真实数据版本。"""

from cnlc_agent.demo.log_plot import build_log_plot
from cnlc_agent.domain.models import LogCurve, RawData, TaskRequest, Well
from cnlc_agent.domain.state import InterpretationState


def test_raw_and_preprocessed_plots_use_distinct_persisted_data():
    state = InterpretationState(
        task=TaskRequest(task_id="task-1", well_id="well-1", instruction="解释"),
        well=Well(well_id="well-1", name="well"),
        raw_data=RawData(
            depths=[1.0],
            curves={"GR_RAW": LogCurve(unit="API", values=[10.0])},
        ),
        processed_data=RawData(
            depths=[1.0],
            curves={"GR": LogCurve(unit="API", values=[11.0])},
        ),
    )

    raw = build_log_plot(state, "RAW")
    processed = build_log_plot(state, "PREPROCESSED")

    assert raw is not None and [curve.name for curve in raw.curves] == ["GR_RAW"]
    assert processed is not None and [curve.name for curve in processed.curves] == ["GR"]
    assert processed.curves[0].source == "company:processed_data"
