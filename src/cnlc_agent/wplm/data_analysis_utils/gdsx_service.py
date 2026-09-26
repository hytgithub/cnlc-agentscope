"""GDSX 高层服务接口（供工具 / API 直接调用）。

本模块把「读文件 -> 计算 -> 落盘 JSON + 构造 echarts_option」封装为三个原子能力，
避免在各处重复拼装逻辑：
    - run_completeness: 完整度检测，输出结果 JSON 文件 + echarts 柱状图 option
    - run_expansion:    井径扩缩率计算，输出结果 JSON 文件 + echarts 折线图 option
    - run_crossplot:    密度-中子/声波-中子交会图，输出结果 JSON + 两张 echarts option
"""

from pathlib import Path
import math
import shutil
import zlib

import numpy as np
import pandas as pd

from cnlc_agent.wplm.data_analysis_utils import completeness, crossplot, expansion, reader
from cnlc_agent.pygdsx.curve import get_gdx_curve_name_list, rename_gdx_curve
from cnlc_agent.pygdsx.config import STANDARD_CURVE_DICT
from cnlc_agent.pygdsx.table import get_all_gdx_table_names, read_gdx_table_all_data
from cnlc_agent.pygdsx.utils import validate_gdsx_path, save_json

# 默认输出目录（项目内部，避免沙箱写外部受限）
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"

# 完整度检测默认统计的 9 条标准曲线（可按需传参 subset 选择其中若干条）
DEFAULT_COMPLETENESS_CURVES = ["AC", "CAL", "CNL", "DEN", "GR", "PE", "RT", "RXO", "SP"]


def _resolve_output_dir(output_dir: str | None) -> Path:
    return Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR


def _resolve_json_path(gdsx_path: Path, output_dir: Path, suffix: str) -> Path:
    return output_dir / f"{gdsx_path.stem}{suffix}.json"


def _resolve_option_path(gdsx_path: Path, output_dir: Path, suffix: str) -> Path:
    return output_dir / f"{gdsx_path.stem}{suffix}_option.json"


def _save_option(option: dict, gdsx_path: Path, out_dir: Path, suffix: str, overwrite: bool) -> Path:
    """将 echarts option 单独落盘为 JSON 文件，并返回其路径。"""
    opt_path = _resolve_option_path(gdsx_path, out_dir, suffix)
    opt_path.parent.mkdir(parents=True, exist_ok=True)
    save_json(option, opt_path, overwrite=overwrite)
    return opt_path


def _completeness_brief(report: dict) -> dict:
    """从完整度报告中提炼精简指标：仅逐曲线完整度 + 井/总点数，不含总体与组合指标。"""
    curves = {
        name: {
            "completeness_ratio": metric.get("completeness_ratio"),
            "total_points": metric.get("total_points"),
            "valid_points": metric.get("valid_points"),
        }
        for name, metric in report.get("curves", {}).items()
    }
    return {
        "curves": curves,
        "total_depth_points": report.get("total_depth_points"),
        "well_name": report.get("well_name"),
    }


def _axis_value(name: str) -> dict:
    """构造 echarts value 轴（数值轴）基本配置"""
    return {"type": "value", "name": name, "nameLocation": "middle"}


def _base_grid() -> dict:
    """通用网格留白，避免坐标轴标签被截断"""
    return {"left": 60, "right": 40, "top": 60, "bottom": 60, "containLabel": True}


def _echarts_completeness(report: dict) -> dict:
    """由完整度报告构造 echarts 柱状图 option（各曲线完整率）"""
    curves = report.get("curves", {})
    names = sorted(curves.keys(), key=lambda n: curves[n].get("completeness_ratio", 0))
    ratios = [curves[n].get("completeness_ratio", 0.0) for n in names]
    # 按完整率阈值预着色（>=80 绿 / >=50 黄 / 其余红），纯数据无 JS 函数
    bar_colors = [
        "#2e9e4f" if r >= 80 else ("#e8a33d" if r >= 50 else "#d64545")
        for r in ratios
    ]
    return {
        "title": {"text": "各曲线完整率对比", "left": "center"},
        "tooltip": {"trigger": "axis"},
        "legend": None,
        "grid": _base_grid(),
        "xAxis": {
            "type": "category",
            "data": names,
            "axisLabel": {"rotate": 45, "interval": 0, "width": 80, "overflow": "break"},
        },
        "yAxis": _axis_value("完整率(%)"),
        "series": [
            {
                "name": "完整率(%)",
                "type": "bar",
                # 逐柱颜色数据，纯 JSON 可序列化（>=80 绿 / >=50 黄 / 其余红）
                "data": [
                    {
                        "value": v,
                        "itemStyle": {"color": bar_colors[i]},
                    }
                    for i, v in enumerate(ratios)
                ],
            }
        ],
    }


def _echarts_expansion(result: dict) -> dict:
    """由井径扩缩率结果构造 echarts 阶梯折线图（按钻头分段）"""
    series = []
    for seg in result.get("segments", []):
        steps_x, steps_y = [], []
        for it in seg.get("intervals", []):
            if it.get("ratio_percent") is None:
                continue
            steps_x += [it["depth_from"], it["depth_to"]]
            steps_y += [it["ratio_percent"], it["ratio_percent"]]
        if steps_x:
            series.append({
                "name": f"BD{seg['bit_index']}(钻头{seg['bit_size']}cm)",
                "type": "line",
                "step": "end",
                "connectNulls": True,
                "showSymbol": False,
                "data": [[x, y] for x, y in zip(steps_x, steps_y)],
            })
    return {
        "title": {"text": f"井径扩缩径率（{result.get('well', '')}）", "left": "center"},
        "tooltip": {"trigger": "axis"},
        "legend": {"type": "scroll", "top": 30},
        "grid": _base_grid(),
        "xAxis": _axis_value("深度 (m)"),
        "yAxis": _axis_value("扩缩径率 (%)"),
        "series": series,
    }


def _nice_freq_max(v: float) -> float:
    """把最大频率值(%)向上取整到 5 的倍数（至少 5），用作频数轴 max。

    固定 0~50 的轴会把峰值仅 10~20% 的柱/曲线压得很矮，改为按数据自适应后
    直方图与正态曲线能铺满面板高度。
    """
    v = max(float(v), 0.0)
    return max(5.0, math.ceil(v / 5.0) * 5.0)


def _freq_axis(vmax: float) -> dict:
    """频数(%)轴公共刻度：min=0、max=vmax（数据自适应 nice 值）、
    间隔 5%（max<=20）或 10%（max>20），隐藏最小值 0 的刻度文字。
    """
    interval = 10.0 if vmax > 20 else 5.0
    return {
        "min": 0.0,
        "max": vmax,
        "interval": interval,
        "axisLabel": {"showMinLabel": False, "color": AXIS_LINE_COLOR},
    }


def _bin_index(arr: np.ndarray, vmin: float, vmax: float, bins: int) -> np.ndarray:
    """把值映射为 [0, bins) 桶号（越界值裁剪到两端桶）"""
    return np.clip(((arr - vmin) / (vmax - vmin) * bins).astype(int), 0, bins - 1)


def _histogram_freq(pts, vmin: float, vmax: float, bins: int = 50):
    """等宽分桶统计相对频率(%)。

    在 [vmin, vmax] 上分 bins 桶，仅统计落在轴范围内的点（分母为全部点数），
    返回 (桶中心列表, 频率%列表)；无点时返回空列表。
    """
    arr = np.asarray(pts, dtype=float)
    total = int(arr.size)
    if total == 0 or vmax <= vmin:
        return [], []
    edges = np.linspace(vmin, vmax, bins + 1)
    in_range = (arr >= vmin) & (arr <= vmax)
    counts = np.bincount(_bin_index(arr[in_range], vmin, vmax, bins), minlength=bins)
    centers = (edges[:-1] + edges[1:]) / 2.0
    freqs = counts / total * 100.0
    return centers.tolist(), freqs.tolist()


def _normal_curve_freq(pts, vmin: float, vmax: float, total: int, bins: int = 50):
    """拟合正态分布并按桶中心取值，返回与 _histogram_freq 同尺度的频率(%)曲线。

    以轴范围内数据拟合 N(mu, sigma)，曲线取各桶概率质量 × 范围内点数 / 总点数
    × 100，与直方图柱同尺度可比。
    """
    arr = np.asarray(pts, dtype=float)
    if arr.size == 0 or vmax <= vmin or total <= 0:
        return [], []
    inside = arr[(arr >= vmin) & (arr <= vmax)]
    if inside.size < 2:
        return [], []
    mu = float(inside.mean())
    sigma = float(inside.std(ddof=1))
    if sigma <= 0:
        return [], []
    edges = np.linspace(vmin, vmax, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    sqrt2 = math.sqrt(2.0)

    def cdf(z: float) -> float:
        return 0.5 * (1.0 + math.erf(z / sqrt2))

    vals = [
        (cdf((hi - mu) / sigma) - cdf((lo - mu) / sigma)) * inside.size / total * 100.0
        for lo, hi in zip(edges[:-1], edges[1:])
    ]
    return centers.tolist(), vals


def _point_freq_counts(x_pts, y_pts, x_min, x_max, y_min, y_max, bins: int = 50):
    """二维频数统计：把点划入 bins×bins 网格，返回每点所在格的计数与最大计数。"""
    arr_x = np.asarray(x_pts, dtype=float)
    arr_y = np.asarray(y_pts, dtype=float)
    total = int(arr_x.size)
    if total == 0:
        return np.array([]), 0
    ix = _bin_index(arr_x, x_min, x_max, bins)
    iy = _bin_index(arr_y, y_min, y_max, bins)
    counts = np.zeros((bins, bins), dtype=int)
    in_range = (
        (arr_x >= x_min) & (arr_x <= x_max) & (arr_y >= y_min) & (arr_y <= y_max)
    )
    np.add.at(counts, (ix[in_range], iy[in_range]), 1)
    freq = counts[ix, iy]
    return freq, int(counts.max())


# 频数分档彩虹色带（10 档，低频→高频，取自用户模板色标条：青→蓝→黄绿→绿→棕红）
FREQ_RAINBOW_COLORS = [
    "#00ffff", "#1aa9eb", "#02b1ff", "#c0ff01", "#c5fd04",
    "#00e500", "#01bf7f", "#ac9933", "#ad4c19", "#8c1f00",
]

# 色标条固定范围（lct 模板 ColorBar：0~10、10 档）
FREQ_COLOR_MIN = 0.0
FREQ_COLOR_MAX = 10.0

# 频数直方图与正态曲线颜色（lct 模板渲染效果：顶部红色、右侧蓝色）
TOP_HIST_COLOR = "#ff0000"
RIGHT_HIST_COLOR = "#0000ff"
# 网格线灰色、轴线黑色（lct 模板 Grid / Backbone 颜色）
GRID_LINE_COLOR = "#808080"
AXIS_LINE_COLOR = "#000000"


def _build_crossplot_option(
    title: str,
    x_pts,
    y_pts,
    x_cfg: dict,
    y_cfg: dict,
) -> dict:
    """按 .lct 模板构造单张交会图 echarts option（频数图模式）。

    布局为互相拼接的三块 grid + 右侧色标条（visualMap）：
      - grid0 主散点图：散点按所在 bins×bins 网格的频数着色（10 档彩虹分档色带，
        低频青色→高频深棕红；piecewise visualMap 固定 0~10 分 10 档）
      - grid1 顶部 x 变量频数直方图（红色柱状）+ 红色正态分布曲线
      - grid2 右侧 y 变量频数直方图（蓝色横向柱状）+ 蓝色正态分布曲线
    三块 grid 边缘相接：顶部直方图底边 = 主图顶边，右侧直方图左边 = 主图右边。

    轴刻度固定（不随数据自动扩展）：
      - 密度-中子：X 中子 -10~60 步长 10；Y 密度 1.8~3.0 步长 0.2（inverse，
        1.8 在顶部、3.0 在底部）
      - 声波-中子：X 中子 -10~60 步长 10；Y 声波 100~500 步长 50（正向，
        100 在底部、500 在顶部）
      - 频数轴 0~峰值自适应（向上取整到 5 的倍数），隐藏 0 刻度值
    """
    x_name = x_cfg["name"]
    x_min, x_max, x_step = x_cfg["min"], x_cfg["max"], x_cfg["step"]
    y_name = y_cfg["name"]
    y_min, y_max, y_step = y_cfg["min"], y_cfg["max"], y_cfg["step"]
    y_inverse = y_cfg.get("inverse", False)

    arr_x = np.asarray(x_pts, dtype=float)
    arr_y = np.asarray(y_pts, dtype=float)
    total = int(arr_x.size)

    # 主散点：[x, y, 所在网格频数]，频数截断到色标固定范围 0~10
    #（lct 模板 ColorBar Lower 0 / Upper 10 / ColorLevel 10）
    freq, _ = _point_freq_counts(arr_x, arr_y, x_min, x_max, y_min, y_max)
    level = np.clip(freq, 0, FREQ_COLOR_MAX)
    pts = [
        [round(float(a), 6), round(float(b), 6), int(f)]
        for a, b, f in zip(arr_x, arr_y, level)
    ]

    # 顶部 X 直方图 / 右侧 Y 直方图（50 桶相对频率）及各自正态曲线。
    # 直方图面板的槽位轴用 50 个桶中心做类目轴（文字隐藏），类目槽中心与主图
    # 线性轴的桶中心逐点重合，bar 宽度按百分比即可与 LEAD 柱宽对齐；
    # 纯 JSON option 不能带 renderItem 函数，故不用 custom series。
    x_centers, x_freqs = _histogram_freq(arr_x, x_min, x_max)
    y_centers, y_freqs = _histogram_freq(arr_y, y_min, y_max)
    _, x_norms = _normal_curve_freq(arr_x, x_min, x_max, total)
    _, y_norms = _normal_curve_freq(arr_y, y_min, y_max, total)
    x_hist_data = [round(float(f), 4) for f in x_freqs]
    x_norm_data = [round(float(v), 4) for v in x_norms]
    y_hist_data = [round(float(f), 4) for f in y_freqs]
    y_norm_data = [round(float(v), 4) for v in y_norms]

    # 频数轴最大值按实际数据自适应（柱状图与正态曲线峰值的 nice 值），
    # 避免固定 0~50 的轴把峰值仅 10~20% 的柱/曲线压得很矮
    x_axis_max = _nice_freq_max(
        max([*x_freqs, *x_norms]) if (x_freqs or x_norms) else 0.0
    )
    y_axis_max = _nice_freq_max(
        max([*y_freqs, *y_norms]) if (y_freqs or y_norms) else 0.0
    )

    axis_text = {"color": AXIS_LINE_COLOR, "fontSize": 13}
    grid_line = {"lineStyle": {"color": GRID_LINE_COLOR}}
    black_line = {"show": True, "lineStyle": {"color": AXIS_LINE_COLOR}}

    def border_axis(
        axis_type: str, position: str, grid_index: int, data=None, vmax: float = 0.0
    ) -> dict:
        """仅画边框线的辅助轴（无刻度文字、无网格），补齐面板四周黑框"""
        axis = {
            "type": axis_type, "position": position, "gridIndex": grid_index,
            "axisLine": black_line, "axisTick": {"show": False},
            "axisLabel": {"show": False}, "splitLine": {"show": False},
        }
        if data is not None:
            axis["data"] = data
        else:
            axis.update(_freq_axis(vmax))
            # 边框轴只画轴线，刻度文字仍强制隐藏
            axis["axisLabel"] = {"show": False}
        return axis

    # 分档色标（自上而下 10.0→1.0，低频青→高频深棕红，单值标签对齐 LEAD 色标条）
    pieces = [
        {"gt": i, "lte": i + 1, "label": f"{i + 1:.1f}", "color": color}
        for i, color in reversed(list(enumerate(FREQ_RAINBOW_COLORS)))
    ]

    return {
        "title": {"text": title, "left": "center", "top": 0},
        "tooltip": {"trigger": "item"},
        "animation": False,
        "grid": [
            # 主散点图
            {"left": "12%", "right": "26%", "top": "22%", "bottom": "12%"},
            # 顶部 X 频数直方图（底边与主图顶边相接）
            {"left": "12%", "right": "26%", "top": "7%", "bottom": "78%"},
            # 右侧 Y 频数直方图（左边与主图右边相接）
            {"left": "74%", "right": "12%", "top": "22%", "bottom": "12%"},
        ],
        "xAxis": [
            {
                "type": "value", "name": x_name, "nameLocation": "middle",
                "nameGap": 30, "nameTextStyle": {"color": AXIS_LINE_COLOR},
                "min": x_min, "max": x_max, "interval": x_step,
                "axisLine": black_line, "axisTick": {"show": False},
                "axisLabel": axis_text, "splitLine": grid_line,
                "gridIndex": 0,
            },
            # 顶部直方图 X 槽位轴（类目中心=桶中心，轴线即主图上边框）
            {
                "type": "category", "data": x_centers, "gridIndex": 1,
                "axisLine": black_line, "axisTick": {"show": False},
                "axisLabel": {"show": False}, "splitLine": {"show": False},
            },
            # 顶部直方图上边框
            border_axis("category", "top", 1, data=x_centers),
            # 右侧直方图频数(%)轴
            {
                "type": "value", "name": "频数(%)", "nameLocation": "middle",
                "nameGap": 30, "nameTextStyle": {"color": AXIS_LINE_COLOR},
                "gridIndex": 2,
                "axisLine": black_line, "axisTick": {"show": False},
                "axisLabel": axis_text, "splitLine": grid_line,
                **_freq_axis(y_axis_max),
            },
            # 右侧直方图上边框
            border_axis("value", "top", 2, vmax=y_axis_max),
        ],
        "yAxis": [
            {
                "type": "value", "name": y_name, "nameLocation": "middle",
                "nameGap": 40, "nameRotate": 90,
                "nameTextStyle": {"color": AXIS_LINE_COLOR},
                "min": y_min, "max": y_max, "interval": y_step,
                "axisLine": black_line, "axisTick": {"show": False},
                "axisLabel": axis_text, "splitLine": grid_line,
                "inverse": y_inverse, "gridIndex": 0,
            },
            # 顶部直方图频数(%)轴
            {
                "type": "value", "name": "频数(%)", "nameLocation": "middle",
                "nameGap": 40, "nameRotate": 90,
                "nameTextStyle": {"color": AXIS_LINE_COLOR},
                "gridIndex": 1,
                "axisLine": black_line, "axisTick": {"show": False},
                "axisLabel": axis_text, "splitLine": grid_line,
                **_freq_axis(x_axis_max),
            },
            # 顶部直方图右边框
            border_axis("value", "right", 1, vmax=x_axis_max),
            # 右侧直方图 Y 槽位轴：类目槽序与主图 Y 轴分箱对齐（Y 类目轴首槽
            # 默认在底部，与主图正向一致；主图 inverse 时首槽需在顶部，故同样
            # inverse）；轴线即主图右边界，与 LEAD 渲染一致显示为蓝色
            {
                "type": "category", "data": y_centers, "gridIndex": 2,
                "inverse": y_inverse,
                "axisLine": {"show": True,
                             "lineStyle": {"color": RIGHT_HIST_COLOR, "width": 1.5}},
                "axisTick": {"show": False},
                "axisLabel": {"show": False}, "splitLine": {"show": False},
            },
            # 右侧直方图右边框
            border_axis("category", "right", 2, data=y_centers),
        ],
        "visualMap": {
            "type": "piecewise",
            "show": True,
            "seriesIndex": 0,
            "dimension": 2,
            "pieces": pieces,
            "orient": "vertical",
            "right": "1%",
            "top": "22%",
            "itemWidth": 16,
            "itemHeight": 12,
            "itemGap": 2,
        },
        "series": [
            {
                "name": title, "type": "scatter", "symbol": "diamond",
                "symbolSize": 2,
                "xAxisIndex": 0, "yAxisIndex": 0, "data": pts,
                "itemStyle": {"opacity": 0.9},
            },
            {
                "name": f"{x_name}频数", "type": "bar",
                "xAxisIndex": 1, "yAxisIndex": 1, "data": x_hist_data,
                "barWidth": "80%", "itemStyle": {"color": TOP_HIST_COLOR},
                "z": 2,
            },
            {
                "name": f"{x_name}正态", "type": "line",
                "xAxisIndex": 1, "yAxisIndex": 1, "data": x_norm_data,
                "showSymbol": False, "smooth": True,
                "lineStyle": {"color": TOP_HIST_COLOR, "width": 2}, "z": 3,
            },
            {
                "name": f"{y_name}频数", "type": "bar",
                "xAxisIndex": 3, "yAxisIndex": 3, "data": y_hist_data,
                "barWidth": "80%", "itemStyle": {"color": RIGHT_HIST_COLOR},
                "z": 2,
            },
            {
                "name": f"{y_name}正态", "type": "line",
                "xAxisIndex": 3, "yAxisIndex": 3, "data": y_norm_data,
                "showSymbol": False, "smooth": True,
                "lineStyle": {"color": RIGHT_HIST_COLOR, "width": 2}, "z": 3,
            },
        ],
    }


def _echarts_crossplot(
    result: dict, cnl_den_pts, den_pts, cnl_ac_pts, ac_pts
) -> dict:
    """由交会图结果与原始点构造密度-中子/声波-中子两张 echarts option。

    密度图用 (cnl_den_pts, den_pts)，声波图用 (cnl_ac_pts, ac_pts)，
    两组点各自来自独立的同深度有效点采集（点数可能不同）。
    返回 {"density_neutron": {...}, "acoustic_neutron": {...}}；
    AC 缺失时声波-中子图散点为空但 option 仍正常生成。
    """
    well = result.get("well", "")
    density_option = _build_crossplot_option(
        f"{well}    密度-中子交会图",
        cnl_den_pts, den_pts,
        crossplot.X_AXIS_CFG, crossplot.DEN_Y_AXIS_CFG,
    )
    acoustic_option = _build_crossplot_option(
        f"{well}    声波-中子交会图",
        cnl_ac_pts, ac_pts,
        crossplot.X_AXIS_CFG, crossplot.AC_Y_AXIS_CFG,
    )
    return {"density_neutron": density_option, "acoustic_neutron": acoustic_option}


def run_completeness(
    gdsx_path: str,
    output_dir: str | None = None,
    overwrite: bool = True,
    need_chart: bool = True,
    save_files: bool = False,
    curves: list[str] | None = None,
) -> dict:
    """计算单井 GDSX 数据完整度，落盘结果 JSON。

    Args:
        need_chart: 是否需要生成 echarts 柱状图 option，默认 False 不绘图。
        save_files: 是否需要将结果 JSON 与 option JSON 落盘。仅本地测试需要，
            部署后置 False 不写文件。
        curves: 需要统计的曲线名列表。默认使用 9 条标准曲线
            (AC/CAL/CNL/DEN/GR/PE/RT/RXO/SP)，可传子集选择其中若干条。

    Returns:
        {"success": bool, "json_path": str, "option_path": str,
         "report": {...逐曲线指标...}, "echarts_option": {...柱状图...} or None,
         "message": str}
    """
    path = validate_gdsx_path(gdsx_path)
    if path is None:
        return {"success": False, "message": f"输入文件不存在或不是 GDSX：{gdsx_path}"}

    out_dir = _resolve_output_dir(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = _resolve_json_path(path, out_dir, "_completeness")

    if not curves:
        curves = list(DEFAULT_COMPLETENESS_CURVES)

    try:
        well = reader.load_well(path)
        report = completeness.analyze_completeness(
            well.df, path.stem, str(path), curves=curves
        )
        if report.get("status") != "success":
            return {"success": False, "message": f"完整性分析失败：{report.get('error')}"}

        brief = _completeness_brief(report)
        if save_files:
            save_json(brief, json_path, overwrite=overwrite)

        option, option_path = None, ""
        if need_chart:
            option = _echarts_completeness(report)
            if save_files:
                option_path = str(_save_option(option, path, out_dir, "_completeness", overwrite))

        return {
            "success": True,
            "json_path": str(json_path),
            "option_path": option_path,
            "report": brief,
            "echarts_option": option,
            "message": "完整度计算完成",
        }
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": f"完整度计算异常：{e}"}


def run_expansion(
    gdsx_path: str,
    output_dir: str | None = None,
    overwrite: bool = True,
    need_chart: bool = True,
    save_files: bool = False,
) -> dict:
    """计算单井 GDSX 井径扩缩率，落盘结果 JSON。

    Args:
        need_chart: 是否需要生成 echarts 折线图 option，默认 True 绘图。
        save_files: 是否需要将结果 JSON 与 option JSON 落盘。仅本地测试需要，
            部署后置 False 不写文件。

    Returns:
        {"success": bool, "json_path": str, "option_path": str,
         "result": {...}, "echarts_option": {...折线图...} or None,
         "message": str}
    """
    path = validate_gdsx_path(gdsx_path)
    if path is None:
        return {"success": False, "message": f"输入文件不存在或不是 GDSX：{gdsx_path}"}

    out_dir = _resolve_output_dir(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = _resolve_json_path(path, out_dir, "_expansion")

    try:
        well = reader.load_well(path)
        result = expansion.analyze_expansion(
            well.df, well.well_info, path,
            chart_path=None, overwrite=overwrite,
        )
        if save_files:
            save_json(result, json_path, overwrite=overwrite)

        option, option_path = None, ""
        if need_chart:
            option = _echarts_expansion(result)
            if save_files:
                option_path = str(_save_option(option, path, out_dir, "_expansion", overwrite))

        return {
            "success": True,
            "json_path": str(json_path),
            "option_path": option_path,
            "result": result,
            "echarts_option": option,
            "message": "井径扩缩率计算完成",
        }
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": f"井径扩缩率计算异常：{e}"}


def run_crossplot(
    gdsx_path: str,
    output_dir: str | None = None,
    overwrite: bool = True,
    need_chart: bool = True,
    save_files: bool = False,
) -> dict:
    """计算单井 GDSX 密度-中子/声波-中子交会图，落盘结果 JSON。

    按绘图模板产出两张 echarts option（各自含主散点 + 顶部频数直方图 +
    右侧频数折线三块 grid），分别落盘为
    {井名}_crossplot_density_neutron_option.json 与
    {井名}_crossplot_acoustic_neutron_option.json。

    Args:
        need_chart: 是否需要生成 echarts option，默认 True 绘图。
        save_files: 是否需要将结果 JSON 与 option JSON 落盘。仅本地测试需要，
            部署后置 False 不写文件。

    Returns:
        {"success": bool, "json_path": str, "option_paths": {...两张图...},
         "result": {...}, "echarts_option": {...两张图...} or None,
         "message": str}
    """
    path = validate_gdsx_path(gdsx_path)
    if path is None:
        return {"success": False, "message": f"输入文件不存在或不是 GDSX：{gdsx_path}"}

    out_dir = _resolve_output_dir(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = _resolve_json_path(path, out_dir, "_crossplot")

    try:
        well = reader.load_well(path)
        result = crossplot.analyze_crossplot(
            well.df, well.well_info, path,
            chart_path=None, overwrite=overwrite,
        )
        if save_files:
            save_json(result, json_path, overwrite=overwrite)

        option, option_paths = None, {}
        if need_chart:
            # 采集散点用于 echarts 渲染（交会图结果本身不带原始点）；
            # 密度对与声波对各自独立采集，有效点集可能不同
            depth, cnl, den, ac, _ = crossplot.resolve_cross_series(well.df)
            _, cnl_den_pts, den_pts = crossplot.collect_pairs(depth, cnl, den)
            if ac is not None:
                _, cnl_ac_pts, ac_pts = crossplot.collect_pairs(depth, cnl, ac)
            else:
                cnl_ac_pts, ac_pts = np.array([]), np.array([])
            option = _echarts_crossplot(
                result, cnl_den_pts, den_pts, cnl_ac_pts, ac_pts
            )
            if save_files:
                option_paths = {
                    "density_neutron": str(_save_option(
                        option["density_neutron"], path, out_dir,
                        "_crossplot_density_neutron", overwrite,
                    )),
                    "acoustic_neutron": str(_save_option(
                        option["acoustic_neutron"], path, out_dir,
                        "_crossplot_acoustic_neutron", overwrite,
                    )),
                }

        return {
            "success": True,
            "json_path": str(json_path),
            "option_paths": option_paths,
            "result": result,
            "echarts_option": option,
            "message": "密度中子，声波中子交会图计算完成",
        }
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": f"中子密度交会图计算异常：{e}"}


def _normalize_key(name: str) -> str:
    """曲线名规范化键（去单位后缀前缀、统一大写）用于大小写不敏感匹配。"""
    key = str(name).strip()
    for sep in ("(", "[", "."):
        if sep in key:
            key = key.split(sep, 1)[0].strip()
            break
    return key.upper()


def run_standardize(
    read_gdsx_file_path: str,
    write_gdsx_file_folder: str,
    write_gdsx_file_name: str,
    standard_curve_dict: dict[str, list[str]] | None = None,
) -> dict:
    """对 GDSX 曲线名做标准化命名，另存为一个新的 GDSX 副本并返回新路径。

    逐个标准名，按其别名顺序（STANDARD_CURVE_DICT 中顺序即读取优先顺序）选择
    「当前文件里存在、且该标准名尚未被占用（未存在于文件、也未被本次改名生成）」
    的别名，改名为标准名，原名写入该曲线 description（{"renamed": true,
    "raw_name": "原名"}）。同一标准名的后续别名因标准名已被占用而不再改名；
    已是标准名（或不在映射表中的曲线）保持原名不动。

    Args:
        read_gdsx_file_path:  源 GDSX 文件绝对路径。
        write_gdsx_file_folder: 新 GDSX 文件所在目录绝对路径。
        write_gdsx_file_name:  新 GDSX 文件名（含 .gdsx 后缀）。
        standard_curve_dict:   曲线名映射字典（标准名 → 别名列表）。默认取 config.STANDARD_CURVE_DICT。

    Returns:
        {"success": bool, "gdsx_file_path": str, "renamed": list[dict],
         "message": str}
    """
    std_dict = standard_curve_dict or STANDARD_CURVE_DICT

    # 0. 校验源文件
    src = validate_gdsx_path(read_gdsx_file_path)
    if src is None:
        return {"success": False, "message": f"输入文件不存在或不是 GDSX：{read_gdsx_file_path}"}

    # 1. 目标目录与文件路径
    out_dir = Path(write_gdsx_file_folder)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / write_gdsx_file_name

    # 2. 复制源文件为副本，在副本上改名
    try:
        shutil.copy2(src, target)
    except OSError as e:  # noqa: BLE001
        return {"success": False, "message": f"复制 GDSX 文件失败：{e}"}

    # 3. 读取副本曲线列表
    curve_names = get_gdx_curve_name_list(str(target)) or []
    if not curve_names:
        return {"success": False, "gdsx_file_path": str(target),
                "message": "GDSX 文件无曲线，未做任何改名"}

    # 当前文件中曲线键集合（大写规范化），用于判定标准名占用关系
    present_keys = {_normalize_key(c) for c in curve_names}
    # 已占用/将被占用的标准名（标准名已存在 或 已由别名改名生成），同一标准名只改一次
    occupied = {std for std in std_dict if _normalize_key(std) in present_keys}

    renamed_log: list[dict] = []

    for standard, aliases in std_dict.items():
        std_key = _normalize_key(standard)
        if std_key in occupied:
            # 标准名已存在或被占用，跳过该标准名全部别名
            continue
        # 按别名顺序（映射表顺序即优先顺序）取第一个存在的别名进行改名
        for alias in aliases:
            a_key = _normalize_key(alias)
            if a_key == std_key:
                continue  # 别名即标准名本身，不再处理
            if a_key in present_keys:
                # 目标标准名尚未占用，按优先级改该别名 -> 标准名
                ok = rename_gdx_curve(str(target), alias, standard)
                if not ok:
                    continue
                present_keys.discard(a_key)
                occupied.add(std_key)
                present_keys.add(std_key)
                renamed_log.append({"standard_name": standard, "raw_name": alias})
                break  # 该标准名已占用，其余别名不改

    return {
        "success": True,
        "gdsx_file_path": str(target),
        "renamed": renamed_log,
        "message": f"曲线标准化完成，共改名 {len(renamed_log)} 条曲线",
    }


# ===================== 油气结论(OGRESULT)储层分布统计 =====================
# 油气结论表格在部分文件中的键名为 OGRESULT，在另一部分文件中为中文字面名（油气结论），
# 依次按此顺序查找，均取名为 sdep/edep/result 三列结构完整的表格。
OGRESULT_TABLE_NAMES = ["OGRESULT", "油气结论"]


def _row_val(row: dict, name: str):
    """大小写不敏感地从行字典取值（优先精确匹配），避免列名大小写差异导致取不到。"""
    if not isinstance(row, dict):
        return None
    if name in row:
        return row.get(name)
    low = name.lower()
    for k, v in row.items():
        if k.lower() == low:
            return v
    return None


def _has_ogresult_cols(row) -> bool:
    """判断行是否具备 sdep/edep/result 三列（大小写不敏感）。"""
    return all(_row_val(row, c) is not None for c in ("sdep", "edep", "result"))


def _find_ogresult_table(path: Path) -> list | None:
    """从 GDSX 中定位油气结论表格数据（list[dict]，每行含 sdep/edep/result 等列）。

    优先按 OGRESULT_TABLE_NAMES 顺序查找；未命中时兜底扫描所有表格，
    取首个具备 sdep/edep/result 三列结构的表格（列名大小写不敏感）。
    """
    # 先取实际存在的表名，仅对真实存在的表名调用读取，
    # 避免探测目标名 OGRESULT 缺失时触发 pygdsx 内部的 KeyError 警告。
    all_names = get_all_gdx_table_names(str(path)) or []
    for name in OGRESULT_TABLE_NAMES:
        if name in all_names:
            try:
                data = read_gdx_table_all_data(str(path), name)
            except Exception:  # noqa: BLE001
                data = None
            if data:
                return data
    # 兜底：扫描全部表格
    for name in all_names:
        try:
            cand = read_gdx_table_all_data(str(path), name)
        except Exception:  # noqa: BLE001
            cand = None
        if cand and _has_ogresult_cols(cand[0]):
            return cand
    return None


def _compute_ogresult_distribution(data: list) -> list:
    """按结论(result)分组统计储层分布：段数、累计厚度、平均厚度。

    累计厚度取各段 (edep - sdep) 之和，作为储层厚度的直接量度；
    列名大小写不敏感，对非数值/异常的 sdep、edep 行做容错跳过。
    """
    agg: dict = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        result = str(_row_val(row, "result") or "").strip() or "未知"
        try:
            thick = max(0.0, float(_row_val(row, "edep")) - float(_row_val(row, "sdep")))
        except (TypeError, ValueError):
            continue
        item = agg.setdefault(result, {"result": result, "count": 0, "total_thickness": 0.0})
        item["count"] += 1
        item["total_thickness"] += thick

    stats = []
    for item in agg.values():
        item["total_thickness"] = round(item["total_thickness"], 2)
        item["avg_thickness"] = (
            round(item["total_thickness"] / item["count"], 2) if item["count"] else 0.0
        )
        stats.append(item)
    stats.sort(key=lambda x: x["total_thickness"], reverse=True)
    return stats


def _echarts_ogresult_distribution(stats: list, well: str) -> dict:
    """由储层分布统计构造 echarts 柱状图 option。

    横轴为结论类别，纵轴为「累计厚度(m)」，各结论类按累计厚度降序排列；
    每类储层使用固定映射颜色（见 ogresult_color），便于跨任务统一视觉。
    """
    names = [s["result"] for s in stats]
    return {
        "title": {"text": f"油气结论储层分布（{well}）", "left": "center"},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": _base_grid(),
        "xAxis": {
            "type": "category",
            "data": names,
            "axisLabel": {"rotate": 30, "interval": 0},
        },
        "yAxis": {
            "type": "value",
            "name": "累计厚度(m)",
            "nameLocation": "end",
            "nameGap": 20,
            "axisLabel": {"margin": 10},
        },
        "series": [
            {
                "name": "累计厚度(m)",
                "type": "bar",
                "barMaxWidth": 40,
                "data": [
                    {"value": s["total_thickness"],
                     "itemStyle": {"color": ogresult_color(s["result"])}}
                    for s in stats
                ],
            }
        ],
    }


# --- 储层类型颜色映射（后续任务复用，勿删）---
# 固定映射保证同一种结论在任意井/任意图表中颜色一致；
# 未在映射表内出现的结论按稳定哈希从后备调色板中取色，保证跨进程一致。
OGRESULT_STANDARD_COLORS = {
    "油层": "#d64545",
    "差油层": "#ff8c00",
    "气层": "#e8a33d",
    "差气层": "#f0d04a",
    "气水同层": "#9acd32",
    "油水同层": "#cd853f",
    "含油水层": "#4da6ff",
    "水层": "#2e6be6",
    "含气水层": "#5ac8fa",
    "干层": "#8d8d8d",
    "Ⅰ类储层": "#1b7a4d",
    "Ⅱ类储层": "#3aa06b",
    "Ⅲ类储层": "#6fbf9a",
    "夹矸": "#4a4a4a",
    "未知": "#999999",
}
OGRESULT_FALLBACK_PALETTE = [
    "#5470c6", "#91cc75", "#fac858", "#ee6666", "#73c0de", "#3ba272",
    "#fc8452", "#9a60b4", "#ea7ccc", "#2e6be6", "#46b8f5", "#f36c6c",
]


def ogresult_color(result: str) -> str:
    """取某类储层结论的固定颜色：命中标准映射直接用，否则按稳定哈希取后备调色板。"""
    key = (result or "").strip() or "未知"
    if key in OGRESULT_STANDARD_COLORS:
        return OGRESULT_STANDARD_COLORS[key]
    idx = zlib.crc32(key.encode("utf-8")) % len(OGRESULT_FALLBACK_PALETTE)
    return OGRESULT_FALLBACK_PALETTE[idx]


def run_ogresult_distribution(
    gdsx_path: str,
    output_dir: str | None = None,
    overwrite: bool = True,
    need_chart: bool = True,
    save_files: bool = False,
) -> dict:
    """统计 GDSX 油气结论(储层)分布，落盘结果 JSON 并可选生成 echarts 柱状图 option。

    从 GDSX 中读取油气结论表格(键名兼容 OGRESULT/油气结论)，以结论(result)分类别
    统计段数、累计与平均厚度(取各段 edep-sdep 之和)，并返回 echarts option 供渲染。

    Returns:
        {"success": bool, "json_path": str, "option_path": str,
         "result": [...逐结论分布...], "echarts_option": {...柱状图...} or None,
         "message": str}
    """
    path = validate_gdsx_path(gdsx_path)
    if path is None:
        return {"success": False, "message": f"输入文件不存在或不是 GDSX：{gdsx_path}"}

    out_dir = _resolve_output_dir(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = _resolve_json_path(path, out_dir, "_ogresult")

    try:
        data = _find_ogresult_table(path)
        if not data:
            return {
                "success": True,
                "json_path": str(json_path),
                "result": [],
                "echarts_option": None,
                "message": "GDSX 文件中未找到油气结论(OGRESULT)表格",
            }

        well = path.stem
        stats = _compute_ogresult_distribution(data)
        brief = {"well_name": well, "distribution": stats}
        if save_files:
            save_json(brief, json_path, overwrite=overwrite)

        option, option_path = None, ""
        if need_chart and stats:
            option = _echarts_ogresult_distribution(stats, well)
            if save_files:
                option_path = str(_save_option(option, path, out_dir, "_ogresult", overwrite))

        return {
            "success": True,
            "json_path": str(json_path),
            "option_path": option_path,
            "result": brief,
            "echarts_option": option,
            "message": f"油气结论储层分布统计完成，共 {sum(s['count'] for s in stats)} 个层段",
        }
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": f"油气结论储层分布统计异常：{e}"}


# ===================== 油气结论交会散点图（POR/PERM/SW 两两） =====================
# 仅取油气结论表格覆盖深度范围内的点，每点按其所在储层结论的固定颜色着色，
# 按结论分序列输出，PERM 轴取对数。产出三张散点图：
#   por_perm: POR(x) × PERM(y, log)
#   por_sw:   POR(x) × SW(y)
#   perm_sw:  PERM(x, log) × SW(y)


def _find_df_column(df: pd.DataFrame, target: str) -> str | None:
    """在 DataFrame 中按列名大小写不敏感查找目标曲线列，返回实际列名。"""
    low = target.lower()
    for col in df.columns:
        if str(col).lower() == low:
            return str(col)
    return None


def _collect_ogresult_segments(data: list) -> list:
    """从油气结论表构建储层深度区间：[(sdep, edep, 结论, 颜色)]，sdep/edep 为 float。"""
    segs = []
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            sdep = float(_row_val(row, "sdep"))
            edep = float(_row_val(row, "edep"))
        except (TypeError, ValueError):
            continue
        result = str(_row_val(row, "result") or "").strip() or "未知"
        segs.append((sdep, edep, result, ogresult_color(result)))
    return segs


def _depth_to_reservoir(depths, segs: list) -> list:
    """把每个深度点映射到其所在储层区间的 (结论, 颜色)；不在任何区间内的点返回 None。"""
    segs = sorted(segs, key=lambda s: s[0])
    mapping = []
    for d in depths:
        mark = None
        for sdep, edep, result, color in segs:
            if sdep <= d <= edep:
                mark = (result, color)
                break
        mapping.append(mark)
    return mapping


def _build_ogresult_crossplot_option(
    title: str, points_by_type: dict, x_cfg: dict, y_cfg: dict
) -> dict:
    """构建油气结论交会散点图 echarts option。

    points_by_type: {结论: [[x, y], ...]}，每类结论一个散点序列，颜色取 ogresult_color；
    x_cfg/y_cfg 为轴配置 {name, type}，type 为 "log" 时轴取对数。
    """
    labels = [k for k in points_by_type if points_by_type[k]]

    def _axis(cfg: dict) -> dict:
        if cfg.get("type") == "log":
            return {
                "type": "log",
                "name": cfg["name"],
                "nameLocation": "middle",
                "nameGap": 30,
                "nameTextStyle": {"color": "#333"},
                "logBase": 10,
                "axisLabel": {"margin": 10},
            }
        return {
            "type": "value",
            "name": cfg["name"],
            "nameLocation": "middle",
            "nameGap": 30,
            "nameTextStyle": {"color": "#333"},
            "axisLabel": {"margin": 10},
        }

    return {
        "title": {"text": title, "left": "center", "top": 0},
        "tooltip": {"trigger": "item"},
        "legend": {"type": "scroll", "top": 40, "data": labels},
        "grid": {"left": 70, "right": 40, "top": 90, "bottom": 60, "containLabel": True},
        "xAxis": _axis(x_cfg),
        "yAxis": _axis(y_cfg),
        "series": [
            {
                "name": label,
                "type": "scatter",
                "symbol": "circle",
                "symbolSize": 5,
                "itemStyle": {"color": ogresult_color(label), "opacity": 0.85},
                "data": points_by_type[label],
            }
            for label in labels
        ],
    }


def _partition_by_reservoir(xs, ys, marks) -> dict:
    """按储层结论分组收集 (x, y) 点，跳过数值无效点。marks 为并行 (_row_val 结果)。"""
    grouped: dict = {}
    for x, y, mark in zip(xs, ys, marks):
        if mark is None:
            continue
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            continue
        if not (np.isfinite(fx) and np.isfinite(fy)):
            continue
        result, _color = mark
        grouped.setdefault(result, []).append([fx, fy])
    return grouped


def run_ogresult_crossplot(
    gdsx_path: str,
    output_dir: str | None = None,
    overwrite: bool = True,
    need_chart: bool = True,
    save_files: bool = False,
) -> dict:
    """依据油气结论表深度范围，绘制 POR/PERM/SW 两两交会散点图，产出 3 张 echarts option。

    只取落在油气结论表各储层区间内的深度点，PERM 取对数(log10)。返回
    {"por_perm": {...}, "por_sw": {...}, "perm_sw": {...}}；落盘文件名即各图表键名。
    """
    path = validate_gdsx_path(gdsx_path)
    if path is None:
        return {"success": False, "message": f"输入文件不存在或不是 GDSX：{gdsx_path}"}

    out_dir = _resolve_output_dir(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        data = _find_ogresult_table(path)
        if not data:
            return {"success": True, "echarts_option": None, "message": "GDSX 文件中未找到油气结论(OGRESULT)表格"}

        segs = _collect_ogresult_segments(data)
        if not segs:
            return {"success": True, "echarts_option": None, "message": "油气结论表无有效储层深度区间"}

        well = reader.load_well(path)
        df = well.df
        depth_col = "DEPTH"
        por_col = _find_df_column(df, "POR")
        perm_col = _find_df_column(df, "PERM")
        sw_col = _find_df_column(df, "SW")
        missing = [c for c, v in {"POR": por_col, "PERM": perm_col, "SW": sw_col}.items() if v is None]
        if missing:
            return {"success": False, "message": f"缺少曲线：{'/'.join(missing)}"}

        marks = _depth_to_reservoir(df[depth_col].to_numpy(), segs)
        depth_arr = df[depth_col].to_numpy()
        por = df[por_col].to_numpy()
        perm = df[perm_col].to_numpy()
        sw = df[sw_col].to_numpy()

        if need_chart:
            # 对含 PERM 的图仅保留 PERM>0 的点（对数轴不允许 <=0）
            perm_ok = perm > 0
            por_perm = _partition_by_reservoir(
                por[perm_ok], perm[perm_ok], [m for m, ok in zip(marks, perm_ok) if ok]
            )
            por_sw = _partition_by_reservoir(por, sw, marks)
            perm_sw = _partition_by_reservoir(
                perm[perm_ok], sw[perm_ok], [m for m, ok in zip(marks, perm_ok) if ok]
            )
            well_name = path.stem
            option = {
                "por_perm": _build_ogresult_crossplot_option(
                    f"{well_name} 孔隙度-渗透率交会", por_perm,
                    {"name": "孔隙度(%), POR", "type": "value"},
                    {"name": "渗透率(mD), PERM(log)", "type": "log"},
                ),
                "por_sw": _build_ogresult_crossplot_option(
                    f"{well_name} 孔隙度-含水饱和度交会", por_sw,
                    {"name": "孔隙度(%), POR", "type": "value"},
                    {"name": "含水饱和度(%), SW", "type": "value"},
                ),
                "perm_sw": _build_ogresult_crossplot_option(
                    f"{well_name} 渗透率-含水饱和度交会", perm_sw,
                    {"name": "渗透率(mD), PERM(log)", "type": "log"},
                    {"name": "含水饱和度(%), SW", "type": "value"},
                ),
            }

            option_paths = {}
            if save_files:
                for key, opt in option.items():
                    option_paths[key] = str(_save_option(opt, path, out_dir, f"_ogresult_crossplot_{key}", overwrite))

            return {
                "success": True,
                "option_paths": option_paths,
                "echarts_option": option,
                "message": "POR/PERM/SW 油气结论交会散点图计算完成",
            }

        return {"success": True, "echarts_option": None, "message": "need_chart=false，未生成图表"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": f"油气结论交会散点图计算异常：{e}"}