"""数据完整性校验。

- 无效值判定：-999.25 或 -99999 视为无效（多为测井软件的曲线占位值）。
- 曲线名规范化：依据 config 的映射表，将原始曲线名统一为约定的规范名。
- 提供基于 DataFrame 的完整度计算，与数据来源（GDSX / CSV）解耦。
"""

from typing import Any

import numpy as np
import pandas as pd

from cnlc_agent.pygdsx.config import RAW_DATA_NAME_SWITCH_MAP, STANDARD_CURVE_DICT
from cnlc_agent.pygdsx.utils import _curve_key

# 无效值定义（视为缺失）
INVALID_VALUES = [-999.25, -99999]


def is_valid_value(value: float) -> bool:
    """判定单值是否有效（非无效占位值且非 NaN）"""
    return value not in INVALID_VALUES and not np.isnan(value)


def normalize_curve_name(curve_name: str) -> str:
    """按映射表规范化曲线名（先精确匹配，再大小写不敏感匹配，最后按别名映射）。

    例：'AT20'、'at20'、'AT20(OHM-M)' 均规范为其标准名 'RXO'。
    """
    if curve_name in RAW_DATA_NAME_SWITCH_MAP:
        return RAW_DATA_NAME_SWITCH_MAP[curve_name]
    key = _curve_key(curve_name)
    for raw_name, standard in RAW_DATA_NAME_SWITCH_MAP.items():
        if _curve_key(raw_name) == key:
            return standard
    for standard, aliases in STANDARD_CURVE_DICT.items():
        for alias in aliases:
            if _curve_key(alias) == key:
                return standard
    return curve_name


def _column_metric(df: pd.DataFrame, column_name: str, total_points: int) -> dict:
    """计算单条曲线列的完整度指标（有效/无效/完整率，含有效值列表）"""
    data_vals = df[column_name].values
    valid_mask = np.array([is_valid_value(x) for x in data_vals])
    valid_count = int(np.sum(valid_mask))
    return {
        "total_points": total_points,
        "valid_points": valid_count,
        "invalid_points": total_points - valid_count,
        "completeness_ratio": round(valid_count / total_points * 100, 2) if total_points else 0.0,
        "valid_values": data_vals[valid_mask].tolist() if valid_count else [],
    }


def analyze_completeness(
    df: pd.DataFrame,
    well_name: str,
    file_path: str | None = None,
    curves: list[str] | None = None,
) -> dict[str, Any]:
    """对已加载的 DataFrame 计算数据完整性（不依赖来源）。

    Args:
        df: 含曲线列的 DataFrame（列名可为原始曲线名，内部会规范化）
        well_name: 井名
        file_path: 数据来源文件路径（可选，仅用于标记）
        curves: 需要统计的曲线名白名单（可空）。传 None 时统计全部曲线；
            传具体列表时仅统计列内可匹配到这些曲线的列
            （未在数据中出现的曲线以 0 完整度占位，便于前端统一展示）。

    Returns:
        包含完整度统计信息的字典（逐曲线 + 总体 + 特定组合）。
    """
    stats = {
        "well_name": well_name,
        "file_path": file_path,
        "status": "success",
        "total_depth_points": len(df),
        "curves": {},
    }

    below_99925_values: set = set()
    curve_columns = []  # 参与完整度统计的曲线列（不含深度列）

    if curves is not None:
        # 白名单模式：曲线已标准化命名，直接按标准名在列中定位进行统计。
        wanted = [normalize_curve_name(c) for c in curves]
        for standard in wanted:
            col = standard if standard in df.columns else None
            if col is None:
                stats["curves"][standard] = {
                    "total_points": 0, "valid_points": 0, "invalid_points": 0,
                    "completeness_ratio": 0.0, "valid_values": [],
                }
                continue
            stats["curves"][standard] = _column_metric(df, col, len(df))
            curve_columns.append(col)
            data_vals = df[col].values
            for value in data_vals:
                if isinstance(value, (int, float)) and value < -999.25:
                    below_99925_values.add(value)
    else:
        # 全量模式：遍历全部非深度列统计。
        for column_name in df.columns:
            if column_name.lower() in ["depth", "depths"]:
                continue
            normalized = normalize_curve_name(column_name)
            stats["curves"][normalized] = _column_metric(df, column_name, len(df))
            curve_columns.append(column_name)
            data_vals = df[column_name].values
            for value in data_vals:
                if isinstance(value, (int, float)) and value < -999.25:
                    below_99925_values.add(value)

    stats["below_99925_values"] = sorted(list(below_99925_values))

    # 总体完整度：同一行内所有曲线均为有效值的占比
    total_points = len(df)
    if curve_columns:
        all_valid = np.ones(total_points, dtype=bool)
        for column_name in curve_columns:
            data_vals = df[column_name].values
            all_valid = all_valid & np.array([is_valid_value(x) for x in data_vals])
        stats["overall_completeness"] = {
            "total_points": total_points,
            "valid_points": int(np.sum(all_valid)),
            "completeness_ratio": round(np.sum(all_valid) / total_points * 100, 2) if total_points else 0.0,
        }
    else:
        stats["overall_completeness"] = {
            "total_points": total_points, "valid_points": 0, "completeness_ratio": 0.0,
        }

    # 定位特定曲线在 DataFrame 中的原始列名
    def _find(*names: str):
        found = {}
        for column_name in df.columns:
            norm = normalize_curve_name(column_name)
            for n in names:
                if norm == n and n not in found:
                    found[n] = column_name
        return found

    por = _find("POR").get("POR")
    sw = _find("SW").get("SW")
    perm = _find("PERM").get("PERM")
    gr = _find("GR").get("GR")
    ac = _find("AC").get("AC")
    rt = _find("RT").get("RT")
    conclusion = next(
        (c for c in df.columns if "CONCLUSION" in c.upper()), None
    )

    def _group_completeness(col_names: list) -> dict:
        if all(c is not None for c in col_names):
            masks = [np.array([is_valid_value(x) for x in df[c].values]) for c in col_names]
            merged = np.ones(total_points, dtype=bool)
            for m in masks:
                merged = merged & m
            return {
                "total_points": total_points,
                "valid_points": int(np.sum(merged)),
                "completeness_ratio": round(np.sum(merged) / total_points * 100, 2) if total_points else 0.0,
            }
        return {"total_points": total_points, "valid_points": 0, "completeness_ratio": 0.0}

    # stats["por_sw_perm_conclusion_completeness"] = _group_completeness([por, sw, perm, conclusion])
    # stats["gr_ac_rt_por_sw_perm_completeness"] = _group_completeness([gr, ac, rt, por, sw, perm])
    # stats["por_sw_perm_completeness"] = _group_completeness([por, sw, perm])

    return stats