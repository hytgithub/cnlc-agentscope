"""从执行快照提取多道测井图；不生成采样值，不将层段摘要扩展成连续曲线。"""

from math import isfinite

from pydantic import Field, ValidationError

from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.models import Contract, RawData, StageResult
from cnlc_agent.domain.state import InterpretationState


class PlotCurve(Contract):
    """每条曲线保留自己的深度网格、单位和来源。"""

    name: str
    unit: str
    depths: list[float]
    values: list[float | None]
    source: str
    is_mock: bool
    kind: str


class PlotInterval(Contract):
    """只接收明确提供顶底深的解释层段。"""

    top: float
    bottom: float
    label: str
    source: str
    is_mock: bool
    formation: str = ""
    layer_no: str = ""
    lithology: str = ""


class LogPlotView(Contract):
    """供当前和历史执行复用的绘图白名单；不包含任意辅助资料。"""

    well_name: str
    depth_unit: str = "m"
    depth_reference: str = "MD"
    curves: list[PlotCurve] = Field(default_factory=list)
    intervals: list[PlotInterval] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def build_log_plot(state: InterpretationState) -> LogPlotView | None:
    """图由当前快照的原始曲线、专业逐点结果和层段生成，兼容没有图数据的旧任务。"""
    raw = state.raw_data
    if raw is None or state.well is None:
        return None
    view = LogPlotView(well_name=state.well.name)

    def append_curves(data: RawData, source: str, is_mock: bool, kind: str) -> None:
        # 首版限制单条曲线大小，明确缺项；不悄悄抽点或跨缺失段插值。
        if len(data.depths) > 20000:
            view.warnings.append("该版本超过 20000 个深度点，暂未绘制；请选取井段")
            return
        for name, curve in data.curves.items():
            view.curves.append(
                PlotCurve(
                    name=name,
                    unit=curve.unit,
                    depths=list(data.depths),
                    values=list(curve.values),
                    source=source,
                    is_mock=is_mock,
                    kind=kind,
                )
            )

    append_curves(raw, "input:raw_data", True, "raw")
    for result in (state.lithology_result, state.petrophysics_result, state.fluid_result):
        if result is None or result.status not in {StepStatus.SUCCESS, StepStatus.WARNING}:
            continue
        payloads = [result]
        # Sw 已作为 W06 的子结果持久化，仍保留其自身来源。
        sw = result.result.get("sw_result")
        if isinstance(sw, dict):
            try:
                payloads.append(StageResult.model_validate(sw))
            except ValidationError:
                view.warnings.append("含水饱和度子结果格式无效，未绘制")
        for payload in payloads:
            if payload.status not in {StepStatus.SUCCESS, StepStatus.WARNING}:
                continue
            samples = payload.result.get("curve_data")
            if samples is None:
                continue
            try:
                data = RawData.model_validate(samples)
            except ValidationError:
                view.warnings.append("专业曲线深度或采样格式无效，未绘制")
                continue
            # 用户指定单个 POR/PERM 只改变摘要，未发生专业逐点重算。
            excluded = set()
            if state.effective_override.por is not None:
                excluded.add("POR")
            if state.effective_override.perm is not None:
                excluded.add("PERM")
            for name in excluded.intersection(data.curves):
                del data.curves[name]
                view.warnings.append(f"{name} 已修改摘要参数，逐点结果尚未重算，暂不绘制旧曲线")
            append_curves(data, payload.source, payload.is_mock, "interpreted")

    result = state.interval_result
    if result is not None and result.status in {StepStatus.SUCCESS, StepStatus.WARNING}:
        intervals = result.result.get("intervals", [])
        if isinstance(intervals, list):
            for item in intervals:
                if not isinstance(item, dict):
                    continue
                top, bottom = item.get("top_depth_m"), item.get("bottom_depth_m")
                label = item.get("layer_type")
                if (
                    not isinstance(top, (int, float))
                    or isinstance(top, bool)
                    or not isinstance(bottom, (int, float))
                    or isinstance(bottom, bool)
                    or not isinstance(label, str)
                ):
                    view.warnings.append("层段缺少有效顶底深或分类，未绘制")
                    continue
                if not isfinite(top) or not isfinite(bottom) or bottom <= top:
                    view.warnings.append("层段底深必须大于顶深，未绘制")
                    continue
                view.intervals.append(
                    PlotInterval(
                        top=top,
                        bottom=bottom,
                        label=label,
                        source=result.source,
                        is_mock=result.is_mock,
                        formation=str(item.get("formation") or "")[:64],
                        layer_no=str(item.get("layer_no") or "")[:32],
                        lithology=str(item.get("lithology") or "")[:64],
                    )
                )
    view.warnings.insert(0, "Mock 演示：只绘制已提供的采样点；连线仅作展示，不代表专业插值")
    return view
