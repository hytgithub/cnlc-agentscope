"""只读 GDSX 演示入口：打印井信息、曲线数据和表格内容。

运行示例：
    uv run python -m cnlc_agent.pygdsx.demo_read_gdsx /path/to/well.gdsx
    uv run python -m cnlc_agent.pygdsx.demo_read_gdsx /path/to/well.gdsx --curve GR --all

默认每条曲线、每张表只显示前 3 条；--all 显示全部数据，建议重定向到文件。
此脚本不修改 GDSX，也不参与 AgentScope Workflow。
"""

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

import h5py

from cnlc_agent.pygdsx.curve import (
    get_gdx_curve_info,
    get_gdx_curve_name_list,
    read_gdx_curve_data_by_index,
)
from cnlc_agent.pygdsx.info import get_gdx_well_info
from cnlc_agent.pygdsx.table import (
    get_all_gdx_table_info,
    get_all_gdx_table_names,
    get_gdx_table_column_list,
    read_gdx_table_all_data,
)
from cnlc_agent.pygdsx.unified_utils import (
    get_the_only_log_group,
    hdf5_dataset2json_object,
    unite2str,
)


def _raw_node(node: h5py.Group | h5py.Dataset, include_all: bool) -> dict[str, Any]:
    """展示额外 HDF5 节点；完整模式用 Base64 保留未解析的原始字节。"""
    if isinstance(node, h5py.Group):
        return {
            "type": "group",
            "children": {
                unite2str(name): _raw_node(node[name], include_all) for name in node.keys()
            },
        }
    result: dict[str, Any] = {
        "type": "dataset",
        "shape": list(node.shape),
        "dtype": str(node.dtype),
        "byte_count": int(node.size * node.dtype.itemsize),
    }
    if include_all:
        result["data_base64"] = base64.b64encode(node[()].tobytes()).decode("ascii")
    return result


def _other_sections(source: Path, include_all: bool) -> dict[str, Any]:
    """补充读取 GDSX 原始结构中曲线、井信息、表格以外的节点。"""
    with h5py.File(source, "r") as handle:
        log_key = get_the_only_log_group(handle)
        if log_key is None:
            raise ValueError("文件中没有井次组")
        log = handle[log_key]
        if not isinstance(log, h5py.Group):
            raise ValueError("井次节点不是 HDF5 组")
        result: dict[str, Any] = {
            "log_group": unite2str(log_key),
            "root_meta": (
                hdf5_dataset2json_object(handle["meta"], "root_meta")
                if "meta" in handle
                else None
            ),
            "log_meta": (
                hdf5_dataset2json_object(log["meta"], "log_meta")
                if "meta" in log
                else None
            ),
            "extra_sections": {
                unite2str(name): _raw_node(log[name], include_all)
                for name in log.keys()
                if name not in {"curve", "table", "info", "meta"}
            },
        }
        return result


def inspect_gdsx(
    path: str | Path,
    sample_size: int = 3,
    include_all: bool = False,
    curves: list[str] | None = None,
    tables: list[str] | None = None,
) -> dict[str, Any]:
    """读取文件并返回可打印的结构；默认只保留数据预览，避免刷屏。"""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"GDSX 文件不存在：{source}")
    if source.suffix.lower() != ".gdsx":
        raise ValueError(f"文件后缀不是 .gdsx：{source}")
    if sample_size < 1:
        raise ValueError("sample_size 必须大于 0")

    all_curves = get_gdx_curve_name_list(str(source)) or []
    all_tables = get_all_gdx_table_names(str(source)) or []
    unknown_curves = set(curves or []) - set(all_curves)
    unknown_tables = set(tables or []) - set(all_tables)
    if unknown_curves or unknown_tables:
        raise ValueError(
            f"文件中不存在的曲线或表格：{sorted(unknown_curves | unknown_tables)}"
        )

    selected_curves = curves if curves is not None else all_curves
    selected_tables = tables if tables is not None else all_tables
    result: dict[str, Any] = {
        "file": str(source.resolve()),
        "well_info": get_gdx_well_info(str(source)) or {},
        "curve_count": len(all_curves),
        "table_count": len(all_tables),
        "curves": [],
        "tables": [],
    }
    result.update(_other_sections(source, include_all))

    for name in selected_curves:
        info = get_gdx_curve_info(str(source), name)
        if info is None:
            result["curves"].append({"name": name, "error": "曲线元信息读取失败"})
            continue
        point_count = int(info.dimension1Length or 0)
        # 底层按索引读取；预览模式只返回少量点，完整模式才返回整条曲线。
        read_count = point_count if include_all else min(point_count, sample_size)
        values = (
            read_gdx_curve_data_by_index(str(source), name, 0, read_count)
            if read_count > 0
            else []
        )
        result["curves"].append(
            {
                "name": name,
                "depth_start": info.dimension1Start,
                "depth_end": info.dimension1End,
                "depth_step": info.dimension1Step,
                "point_count": point_count,
                "dimension": info.dimension,
                "unit": info.dimension2Unit,
                "values": values,
                "values_truncated": not include_all and point_count > sample_size,
                **({"metadata": info.__dict__} if include_all else {}),
            }
        )

    for name in selected_tables:
        rows = read_gdx_table_all_data(str(source), name)
        columns = get_gdx_table_column_list(str(source), name)
        table_info = get_all_gdx_table_info(str(source), name) if include_all else None
        result["tables"].append(
            {
                "name": name,
                "columns": columns or [],
                "row_count": len(rows) if isinstance(rows, list) else None,
                "rows": rows if include_all or not isinstance(rows, list) else rows[:sample_size],
                "rows_truncated": (
                    not include_all and isinstance(rows, list) and len(rows) > sample_size
                ),
                **(
                    {"metadata": table_info.__dict__ if table_info else None}
                    if include_all
                    else {}
                ),
            }
        )
    return result


def main(argv: list[str] | None = None) -> int:
    """解析命令行参数，将读取结果以中文兼容的 JSON 打印到终端。"""
    parser = argparse.ArgumentParser(description="只读查看 GDSX 井信息、曲线和表格")
    parser.add_argument("path", help="要查看的 .gdsx 文件路径")
    parser.add_argument("--sample-size", type=int, default=3, help="每条曲线/表格预览条数")
    parser.add_argument("--all", action="store_true", help="打印全部曲线值和表格行")
    parser.add_argument("--compact", action="store_true", help="紧凑 JSON，适合完整数据导出")
    parser.add_argument("--curve", action="append", help="只打印指定曲线，可重复使用")
    parser.add_argument("--table", action="append", help="只打印指定表格，可重复使用")
    args = parser.parse_args(argv)
    try:
        data = inspect_gdsx(
            args.path,
            sample_size=args.sample_size,
            include_all=args.all,
            curves=args.curve,
            tables=args.table,
        )
    except (FileNotFoundError, ValueError, OSError) as exc:
        parser.exit(2, f"读取失败：{exc}\n")
    json.dump(
        data,
        sys.stdout,
        ensure_ascii=False,
        indent=None if args.compact else 2,
        separators=(",", ":") if args.compact else None,
        default=str,
    )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
