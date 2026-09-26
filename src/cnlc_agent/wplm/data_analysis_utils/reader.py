"""GDSX 文件读取：底层读取函数 + 高维组装，返回以深度为索引的 DataFrame。

本模块以模块级函数承载全部 GDSX 读取逻辑（自 curve.py / info.py 收敛而来），
供完整性、扩缩径、绘图等业务使用，数据容器 WellData 仅承载结果、不含业务逻辑。

读取原理：
    - GDSX 实质是 HDF5 文件；曲线名、曲线元信息(meta)以「JSON 转字节」方式存储，
      数值数据(data)以原始字节流存储。
    - 深度轴由 meta 的起点/步长按 np.arange(N)*step+start 重建。
"""

import struct
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

import numpy as np
import pandas as pd
from cnlc_agent.pygdsx.info import get_gdx_well_info
from cnlc_agent.pygdsx.curve import get_gdx_curve_name_list,get_gdx_curve_info,curveinfo2xy,read_gdx_curve_data_by_index



def get_well_info(full_log_path: str) -> dict:
    """读取井次信息（可能为空字典）"""
    info = get_gdx_well_info(full_log_path)
    return info if isinstance(info, dict) else {}


# ===================== 下：组装为 DataFrame =====================
@dataclass
class WellData:
    """单口井的解析结果容器（纯数据）"""
    well_info: dict = field(default_factory=dict)
    df: pd.DataFrame = field(default_factory=pd.DataFrame)  # DEPTH 为首列


def _read_curve_data(gdsx_path: str, curve_name: str):
    """读取单条曲线原始数据，返回 (labels, depth, matrix, step) 或 None"""
    info = get_gdx_curve_info(gdsx_path, curve_name)
    if info is None:
        warnings.warn(f"曲线 {curve_name} 元信息读取失败，已跳过")
        return None

    n = int(getattr(info, "dimension1Length", 0) or 0)
    if n <= 0:
        warnings.warn(f"曲线 {curve_name} 数据点数为 {n}，已跳过")
        return None

    npw_rep = curveinfo2xy(info, curve_name)
    if npw_rep is None:
        warnings.warn(f"曲线 {curve_name} 维度信息异常，已跳过")
        return None
    npw = npw_rep[0]

    data = read_gdx_curve_data_by_index(gdsx_path, curve_name, 0, n)
    if data is None or len(data) == 0:
        warnings.warn(f"曲线 {curve_name} 数据读取失败，已跳过")
        return None

    # 深度轴：由元信息中的起点/步长构造
    start = float(getattr(info, "dimension1Start", 0) or 0)
    step = float(getattr(info, "dimension1Step", 0) or 0)
    if step <= 0:
        warnings.warn(f"曲线 {curve_name} 深度步长无效({step})，已跳过")
        return None
    depth = np.arange(n, dtype=float) * step + start

    if npw == 1:
        labels = [curve_name]
        matrix = np.array([float(d) if d is not None else np.nan for d in data])
    else:
        labels = [f"{curve_name}.{j + 1}" for j in range(npw)]
        matrix = np.array(
            [[float(v) if v is not None else np.nan for v in row] for row in data]
        )

    return labels, depth, matrix, step


def to_dataframe(gdsx_path: str) -> pd.DataFrame:
    """读取全部曲线，以最深/最密的曲线为基准深度轴对齐生成 DataFrame（DEPTH 为首列）"""
    curve_names = get_gdx_curve_name_list(gdsx_path) or []
    if not curve_names:
        raise ValueError(f"GDSX 文件 {gdsx_path} 中不包含任何曲线数据")

    reads = []
    for name in curve_names:
        try:
            result = _read_curve_data(gdsx_path, name)
        except Exception as e:  # noqa: BLE001
            warnings.warn(f"读取曲线 {name} 时发生错误：{e}，已跳过")
            continue
        if result is not None:
            reads.append(result)

    if not reads:
        raise ValueError(f"GDSX 文件 {gdsx_path} 中所有曲线均读取失败")

    # 以点数最多（通常最密/最深）的曲线作为基准深度轴
    reference = max(reads, key=lambda r: len(r[1]))
    master_depth = np.asarray(reference[1], dtype=float)
    master_depth = np.sort(master_depth)
    master_depth = master_depth[np.unique(master_depth, return_index=True)[1]]

    frames = []
    for labels, depth, matrix, step in reads:
        columns = [matrix] if matrix.ndim == 1 else matrix.T
        series_list = []
        for col_vals, label in zip(columns, labels):
            s = pd.Series(col_vals, index=pd.Index(depth, name="DEPTH"))
            if s.index.has_duplicates or not s.index.is_monotonic_increasing:
                s = s[~s.index.duplicated(keep="first")].sort_index()
            s = s.reindex(
                pd.Index(master_depth, name="DEPTH"),
                method="nearest",
                tolerance=max(step / 2.0, 1e-6),
            )
            series_list.append(pd.Series(s.to_numpy(), index=master_depth, name=label))
        frames.append(pd.concat(series_list, axis=1))

    combined = pd.concat(frames, axis=1)
    combined = combined[~combined.index.duplicated(keep="first")].sort_index()

    # 去除重复列名，保证列唯一
    seen: dict = {}
    new_columns = []
    for c in combined.columns:
        if c in seen:
            seen[c] += 1
            new_columns.append(f"{c}.{seen[c]}")
        else:
            seen[c] = 1
            new_columns.append(c)
    combined.columns = new_columns

    # 若存在名为 DEPTH 的曲线列，改名避免与深度列冲突
    col_map = {}
    for c in combined.columns:
        if c == "DEPTH":
            cand = "DEPTH_CURVE"
            while cand in combined.columns or cand in col_map.values():
                cand += "_"
            col_map[c] = cand
    if col_map:
        combined = combined.rename(columns=col_map)

    df = combined.reset_index()
    df = df.rename(columns={"index": "DEPTH"})
    df = df[["DEPTH"] + [c for c in df.columns if c != "DEPTH"]]
    df["DEPTH"] = pd.to_numeric(df["DEPTH"], errors="coerce")
    df = df.dropna(subset=["DEPTH"]).sort_values("DEPTH").reset_index(drop=True)
    return df


def load_well(gdsx_path: str | Path) -> WellData:
    """从 GDSX 文件加载井次信息与全部曲线，返回 WellData 容器"""
    path = str(gdsx_path)
    return WellData(well_info=get_well_info(path), df=to_dataframe(path))