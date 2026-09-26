"""中子-密度 / 中子-声波交会图计算。

采集同一深度点的中子(CNL)与密度(DEN)、中子(CNL)与声波(AC)数据，
按绘图模板（密度-中子_and_声波-中子交会图.lct）固定轴刻度：
  - 密度-中子交会图：横轴中子 -10~60(%) 步长 10；纵轴密度 1.8~3.0(g/cm3)
    步长 0.2（inverse，1.8 在顶部、3.0 在底部）
  - 声波-中子交会图：横轴中子 -10~60(%) 步长 10；纵轴声波 100~500(μs/m)
    步长 50（100 在底部、500 在顶部）
输出结构化结果供 echarts 渲染。

单位说明：中子(CNL)为孔隙度（%），密度(DEN)单位 g/cm3，声波(AC)单位 μs/m，
均保留原始单位，不做换算。AC 缺失时声波-中子交会图散点为空但不影响整体流程。
"""

import logging
from typing import Any

import numpy as np
import pandas as pd

from cnlc_agent.pygdsx.utils import now_iso

logger = logging.getLogger("gdsx_core.crossplot")

# 中子/密度/声波无效/占位值（应视为缺失，不参与交会）
INVALID_VALUES = {-999.25, -99999, -9999}

# 物理可行值下限：中子孔隙度(%)、密度(g/cm3)均不可能 <= -100，
# 更小的负数为各工具不同的无效/占位哨兵值，一并剔除
NEG_THRESHOLD = -100.0

# 模板固定轴配置（.lct 绘图模板，轴名与模板一致用全角括号）
X_AXIS_CFG = {"name": "中子（%）", "min": -10.0, "max": 60.0, "step": 10.0}
DEN_Y_AXIS_CFG = {"name": "密度（g/cm³）", "min": 1.8, "max": 3.0, "step": 0.2, "inverse": True}
AC_Y_AXIS_CFG = {"name": "声波（μs/m）", "min": 100.0, "max": 500.0, "step": 50.0, "inverse": False}


def resolve_cross_series(
    df: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None, dict]:
    """定位中子(CNL)/密度(DEN)/声波(AC)列。

    返回 (depth, cnl, den, ac, names)；CNL/DEN 为必需曲线（缺失抛错），
    AC 为可选曲线（缺失时返回 None），names 形如
    {"cnl": "CNL", "den": "DEN", "ac": "AC" 或 None}。
    """
    names: dict[str, str | None] = {}
    for std in ("CNL", "DEN"):
        if std not in df.columns:
            raise ValueError(f"GDSX 中未发现{std}曲线；现有列：{list(df.columns)}")
        names[std.lower()] = std
    ac = None
    if "AC" in df.columns:
        ac = df["AC"].to_numpy(dtype=float)
        names["ac"] = "AC"
    else:
        names["ac"] = None

    depth = df["DEPTH"].to_numpy(dtype=float)
    cnl = df["CNL"].to_numpy(dtype=float)
    den = df["DEN"].to_numpy(dtype=float)
    return depth, cnl, den, ac, names


def build_valid_mask(depth: np.ndarray, *curves: np.ndarray) -> np.ndarray:
    """有效点掩码：深度及各曲线均非 NaN、非占位值、数值有限、大于物理下限"""
    mask = np.isfinite(depth)
    for c in curves:
        mask = (
            mask
            & np.isfinite(c)
            & ~np.isin(c, list(INVALID_VALUES))
            & (c > NEG_THRESHOLD)
        )
    return mask


def collect_pairs(
    depth: np.ndarray, x: np.ndarray, y: np.ndarray
) -> tuple[int, np.ndarray, np.ndarray]:
    """采集同一深度点上 x、y 均有效的点，返回 (点数, x 点值, y 点值)"""
    mask = build_valid_mask(depth, x, y)
    return int(mask.sum()), x[mask], y[mask]


def collect_points(
    depth: np.ndarray, cnl: np.ndarray, den: np.ndarray
) -> tuple[int, np.ndarray, np.ndarray]:
    """采集同一深度点上中子、密度均有效的点，返回 (点数, cnl 点值, den 点值)"""
    return collect_pairs(depth, cnl, den)


def analyze_crossplot(
    df: pd.DataFrame,
    well_info: dict,
    gdsx_path: Any,
    chart_path: Any | None = None,
    overwrite: bool = True,
) -> dict:
    """基于解析好的 DataFrame，采集中子-密度/中子-声波同深度点并输出结构化结果。"""
    depth, cnl, den, ac, names = resolve_cross_series(df)
    valid_count, cnl_pts, den_pts = collect_pairs(depth, cnl, den)
    if valid_count == 0:
        raise ValueError("GDSX 中无任何中子/密度同深度有效点(非 NaN、非占位值)")

    result: dict = {
        "generated_at": now_iso(),
        "source_gdsx": str(gdsx_path),
        "well": well_info.get("LEGALNAME", well_info.get("WELLNAME")),
        "curves": names,
        "conversion": {
            "depth_points": int(df.shape[0]),
            "valid_points": valid_count,
        },
        "data_range": {
            "cnl_min": round(float(np.min(cnl_pts)), 6),
            "cnl_max": round(float(np.max(cnl_pts)), 6),
            "den_min": round(float(np.min(den_pts)), 6),
            "den_max": round(float(np.max(den_pts)), 6),
        },
        "axes": {
            "density_neutron": {
                "x_axis": dict(X_AXIS_CFG),
                "y_axis": dict(DEN_Y_AXIS_CFG),
            },
            "acoustic_neutron": {
                "x_axis": dict(X_AXIS_CFG),
                "y_axis": dict(AC_Y_AXIS_CFG),
            },
        },
        "unit_note": (
            "密度-中子交会图：横轴中子(%)，纵轴密度(g/cm3)；"
            "声波-中子交会图：横轴中子(%)，纵轴声波(μs/m)；轴刻度按绘图模板固定"
        ),
    }

    # 声波(AC)为可选曲线：存在时补充中子-声波统计
    if ac is not None:
        ac_count, _, ac_pts = collect_pairs(depth, cnl, ac)
        result["conversion"]["valid_points_acoustic"] = ac_count
        if ac_count > 0:
            result["data_range"].update({
                "ac_min": round(float(np.min(ac_pts)), 6),
                "ac_max": round(float(np.max(ac_pts)), 6),
            })
    else:
        result["conversion"]["valid_points_acoustic"] = 0

    return result
