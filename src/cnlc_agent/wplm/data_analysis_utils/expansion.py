"""井径扩径/缩径率计算。

依据井次信息中的钻头配置（BD1/BD2/... 与 DRILLBITDEPTH1/2/...），结合井径(CAL)
曲线，按钻头分段、每段按 30 米区间计算平均井径相对钻头直径的扩缩径率。

算法约定：
    1. 钻头直径单位可能为 cm 或 mm：>100 视为毫米，统一转 cm 并保留两位小数。
    2. 第一段从"首个有效井径深度"到 DRILLBITDEPTH1；后续段逐钻头推进。
    3. 后一钻头与前一钻头钻深相同视为重复，跳过。
    4. 端点不超过该钻头完钻深度，也不超过井次信息 ENDDEPTH。
    5. 扩缩径率(%) = (平均井径 - 钻头直径) / 钻头直径 * 100（正扩径、负缩径）。
"""

import logging
from typing import Any

import numpy as np
import pandas as pd

from cnlc_agent.pygdsx.config import RAW_DATA_FACTOR_MAP
from cnlc_agent.pygdsx.utils import now_iso

logger = logging.getLogger("gdsx_core.expansion")

# 井径无效/占位值（应视为缺失；井径必须为正）
CAL_INVALID_VALUES = {-999.25, -99999, -9999, 0.0}

# 区间长度(米)
INTERVAL_M = 30.0


def resolve_cal_series(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, str]:
    """从 DataFrame 定位井径列，返回 (depth, cal_cm, 列名)，cal_cm 已换算为 cm"""
    # 曲线已标准化命名，井径列即为标准名 "CAL"
    cal_name = "CAL"
    if cal_name not in df.columns:
        raise ValueError(f"GDSX 中未发现井径(CAL)曲线；现有列：{list(df.columns)}")

    factor = RAW_DATA_FACTOR_MAP.get(cal_name, 1.0)
    depth = df["DEPTH"].to_numpy(dtype=float)
    cal = df[cal_name].to_numpy(dtype=float) * factor
    return depth, cal, cal_name


def build_valid_mask(cal: np.ndarray, depth: np.ndarray) -> np.ndarray:
    """有效井径掩码：非 NaN、非占位、且 >0、深度有限"""
    return (
        ~np.isnan(cal)
        & ~np.isin(cal, list(CAL_INVALID_VALUES))
        & (cal > 0)
        & np.isfinite(depth)
    )


def first_valid_depth(depth: np.ndarray, cal: np.ndarray) -> float | None:
    """首个有效井径数据的深度（有效=非 NaN、非占位值、且井径>0）"""
    valid = build_valid_mask(cal, depth)
    idx = np.argmax(valid) if valid.any() else -1
    return float(depth[idx]) if idx >= 0 else None


def parse_bits(well_info: dict) -> list[dict]:
    """解析 BD1/BD2/... 与其 DRILLBITDEPTH1/2/...，返回按钻深排序、统一 cm 的钻头列表"""
    bits = []
    i = 1
    while True:
        bd = well_info.get(f"BD{i}")
        dp = well_info.get(f"DRILLBITDEPTH{i}")
        if bd is None or dp is None:
            break
        try:
            size = float(bd)
            if size > 100:  # 毫米口径，转为厘米
                size = size / 10.0
            size = round(size, 2)  # 统一两位小数，消除浮点尾数
            bits.append({"index": i, "size": size, "depth": float(dp)})
        except (TypeError, ValueError):
            break
        i += 1
    if not bits:
        raise ValueError("井次信息中未找到 BD/DRILLBITDEPTH 钻头配置")
    bits.sort(key=lambda b: b["depth"])
    return bits


def dedupe_bits(bits: list[dict]) -> list[dict]:
    """后一钻头与前一钻头钻深相同视为重复，跳过（保留靠前的一个）"""
    deduped = []
    prev_depth = None
    for b in bits:
        if prev_depth is not None and abs(b["depth"] - prev_depth) < 1e-6:
            logger.info(
                "钻头 BD%d 与 BD%d 钻深相同(%.3f)，视为重复并跳过",
                b["index"], deduped[-1]["index"], b["depth"],
            )
            continue
        deduped.append(b)
        prev_depth = b["depth"]
    return deduped


def build_segments(
    bits: list[dict], start_depth: float, end_depth: float
) -> list[dict]:
    """组装各钻头计算分段，返回 [{bit_index, bit_size, depth_from, depth_to}, ...]"""
    segments = []
    for i, b in enumerate(bits):
        seg_from = start_depth if i == 0 else bits[i - 1]["depth"]
        seg_to = min(b["depth"], end_depth)  # 不超过该钻头完钻深度与 ENDDEPTH
        if seg_to <= seg_from:
            logger.info(
                "分段 BD%d 深度区间 [%.3f, %.3f] 无效，跳过", b["index"], seg_from, seg_to
            )
            continue
        segments.append({
            "bit_index": b["index"],
            "bit_size": b["size"],
            "depth_from": seg_from,
            "depth_to": seg_to,
        })
    return segments


def split_intervals(seg_from: float, seg_to: float) -> list[tuple[float, float]]:
    """按 30m 划分区间，最后一个区间为剩余深度（可不足 30m）"""
    intervals = []
    start = seg_from
    while start < seg_to:
        end = min(start + INTERVAL_M, seg_to)
        intervals.append((start, end))
        start = end
    return intervals


def calc_segment(
    depth: np.ndarray, cal: np.ndarray, segment: dict, valid_mask: np.ndarray
) -> dict:
    """对单个钻头分段内各 30 米区间计算平均井径与扩缩径率"""
    bit_size = segment["bit_size"]
    interval_rows = []
    for a, b in split_intervals(segment["depth_from"], segment["depth_to"]):
        m = valid_mask & (depth >= a) & (depth < b)
        pts = cal[m]
        if pts.size == 0:
            interval_rows.append({
                "depth_from": round(a, 3), "depth_to": round(b, 3),
                "point_count": 0, "mean_cal": None, "ratio_percent": None,
                "category": "no_data",
            })
            continue
        mean_cal = float(pts.mean())
        ratio = (mean_cal - bit_size) / bit_size * 100.0
        category = "expanded" if ratio > 0.1 else ("shrunk" if ratio < -0.1 else "normal")
        interval_rows.append({
            "depth_from": round(a, 3), "depth_to": round(b, 3),
            "point_count": int(pts.size), "mean_cal": round(mean_cal, 3),
            "ratio_percent": round(ratio, 3), "category": category,
        })
    segment["intervals"] = interval_rows
    return segment


def analyze_expansion(
    df: pd.DataFrame,
    well_info: dict,
    gdsx_path: Any,
    chart_path: Any | None = None,
    overwrite: bool = True,
) -> dict:
    """基于解析好的 DataFrame 与井次信息，计算各钻头分段扩缩径率并返回结构化结果。

    若传入 chart_path 且未禁用图表，会同步生成「深度 × 扩缩径率」折线图。
    """
    depth, cal, cal_name = resolve_cal_series(df)
    bits = dedupe_bits(parse_bits(well_info))

    start_depth = first_valid_depth(depth, cal)
    if start_depth is None:
        raise ValueError("GDSX 中无任何有效井径数据(>0 且非占位值)")

    end_depth = well_info.get("ENDDEPTH")
    end_depth = float(end_depth) if end_depth not in (None, "") else bits[-1]["depth"]

    valid_mask = build_valid_mask(cal, depth)
    segments = build_segments(bits, start_depth, end_depth)
    segment_results = [calc_segment(depth, cal, seg, valid_mask) for seg in segments]

    result = {
        "generated_at": now_iso(),
        "source_gdsx": str(gdsx_path),
        "well": well_info.get("LEGALNAME", well_info.get("WELLNAME")),
        "well_info_snapshot": {
            "start_valid_cal_depth": round(start_depth, 3),
            "end_depth": round(end_depth, 3),
            "bits": [{k: b[k] for k in ("index", "size", "depth")} for b in bits],
        },
        "cal_curve": cal_name,
        "unit_note": "井径已换算为 cm，钻头直径取 cm，扩缩径率为百分数",
        "segments": segment_results,
    }
    return result