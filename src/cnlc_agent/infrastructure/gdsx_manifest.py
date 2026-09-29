"""复用项目 pygdsx 只读 API 生成无采样值的 GDSX 清单。"""

import hashlib
from pathlib import Path

import h5py  # type: ignore[import-untyped]

from cnlc_agent.domain.artifacts import CurveManifest, GdsxDatasetManifest
from cnlc_agent.domain.errors import DataError
from cnlc_agent.pygdsx.curve import get_gdx_curve_info, get_gdx_curve_name_list
from cnlc_agent.pygdsx.info import get_gdx_well_info
from cnlc_agent.pygdsx.table import get_all_gdx_table_names

HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"


def inspect_gdsx(path: Path, artifact_id: str) -> GdsxDatasetManifest:
    """验证 HDF5 并投影已明确元数据；不猜井号、曲线别名或业务语义。"""

    with path.open("rb") as stream:
        magic = stream.read(len(HDF5_MAGIC))
    if path.suffix.casefold() != ".gdsx" or magic != HDF5_MAGIC:
        raise DataError("GDSX_FILE_INVALID", "上传文件不是有效 GDSX/HDF5")
    try:
        with h5py.File(path, "r"):
            pass
    except OSError:
        raise DataError("GDSX_FILE_INVALID", "GDSX 文件无法由 HDF5 读取") from None
    names = get_gdx_curve_name_list(str(path)) or []
    curves = []
    warnings = ["PENDING_REAL_SAMPLE_VALIDATION：井信息、曲线元数据和表结构待真实样本确认"]
    for name in names:
        info = get_gdx_curve_info(str(path), name)
        if info is None:
            warnings.append(f"曲线 {name} 元数据无法读取")
            continue
        curves.append(
            CurveManifest(
                raw_name=name,
                standard_name=info.standardName if isinstance(info.standardName, str) else None,
                unit=info.dimension1Unit if isinstance(info.dimension1Unit, str) else None,
                depth_start=float(info.dimension1Start)
                if info.dimension1Start is not None
                else None,
                depth_end=float(info.dimension1End) if info.dimension1End is not None else None,
                depth_step=float(info.dimension1Step) if info.dimension1Step is not None else None,
                point_count=max(0, int(info.dimension1Length or 0)),
                dimension=max(1, int(info.dimension or 1)),
            )
        )
    tables = get_all_gdx_table_names(str(path)) or []
    well_info = get_gdx_well_info(str(path)) or {}
    depth_starts = [item.depth_start for item in curves if item.depth_start is not None]
    depth_ends = [item.depth_end for item in curves if item.depth_end is not None]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return GdsxDatasetManifest(
        artifact_id=artifact_id,
        size_bytes=path.stat().st_size,
        content_sha256=digest.hexdigest(),
        well_info_summary={str(key): value for key, value in list(well_info.items())[:32]},
        curve_count=len(curves),
        table_count=len(tables),
        curves=curves,
        table_names=tables,
        depth_summary={
            **({"start": min(depth_starts)} if depth_starts else {}),
            **({"end": max(depth_ends)} if depth_ends else {}),
        },
        warnings=warnings,
    )
