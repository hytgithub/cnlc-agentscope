"""通用工具函数：日志、文件校验、输出路径解析、JSON 结构化存储。

本模块不依赖具体业务规则，仅提供被各业务/CLI 共用的基础设施。
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from cnlc_agent.pygdsx.config import STANDARD_CURVE_DICT


def setup_logging(level: str = "INFO", name: str = "gdsx") -> None:
    """统一配置日志输出"""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logging.getLogger(name)


def validate_gdsx_path(needlen_path_t: str) -> Path | None:
    """校验输入路径为存在的 .gdsx 文件，合法返回 Path，否则返回 None"""
    path = Path(needlen_path_t)
    if not path.exists():
        logging.error("输入文件不存在：%s", path)
        return None
    if not path.suffix.lower() == ".gdsx":
        logging.error("输入文件不是 GDSX 文件：%s", path)
        return None
    return path


def resolve_output_dir(gdsx_path: Path, output_dir: str | None) -> Path:
    """确定输出目录（默认与输入文件同目录）"""
    return Path(output_dir) if output_dir else gdsx_path.parent


def resolve_output_path(
    gdsx_path: Path,
    output_dir: Path,
    suffix: str,
    custom_name: str | None = None,
    ext: str = "json",
) -> Path:
    """按 <井名><_suffix>.<ext> 生成输出路径，支持自定义文件名（不含后缀）"""
    stem_name = custom_name or f"{gdsx_path.stem}{suffix}"
    return output_dir / f"{stem_name}.{ext}"


def check_overwrite(path: Path, overwrite: bool, description: str = "输出文件") -> None:
    """是否覆盖检查，未开启覆盖且已存在则抛错"""
    if path.exists() and not overwrite:
        raise FileExistsError(f"目标{description}已存在且未开启覆盖：{path}")


def save_json(data: dict, json_path: Path, overwrite: bool = True) -> Path:
    """将结构化字典写入 JSON 文件"""
    check_overwrite(json_path, overwrite, "JSON")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logging.getLogger("gdsx").info("结构化结果已保存：%s", json_path)
    return json_path


def now_iso() -> str:
    """当前时间的 ISO 字符串（到秒）"""
    return datetime.now().isoformat(timespec="seconds")


def _curve_key(col_name: str) -> str:
    """提取列名主体用于别名匹配：去掉单位后缀((...)、[...] 或 .xxx)并转大写。

    例：'AT20(OHM-M)' -> 'AT20'；'CAL(IN)' -> 'CAL'；'GR.GAPI' -> 'GR'。
    """
    name = col_name.strip()
    for sep in ("(", "[", "."):
        if sep in name:
            name = name.split(sep, 1)[0].strip()
            break
    return name.upper()


def resolve_curve_column(df: pd.DataFrame, standard_name: str) -> str | None:
    """按标准名在 DataFrame 中定位其数据列（不区分大小写，忽略单位后缀）。

    依据 STANDARD_CURVE_DICT 中该标准名的别名顺序读取：首个匹配到数据列的
    别名即为实际列。例：标准名 "RXO" 依次尝试 RXO/AT20/RLLS/...，数据里只有
    "AT20" 标签时返回 "AT20"，且 AT20 优先级高于 RLLS 等别名。

    Args:
        df: 含曲线列的 DataFrame（列名可为原始名，可能带单位）。
        standard_name: 标准曲线名，如 "RXO"、"CAL"，不区分大小写。

    Returns:
        匹配到的实际列名；找不到时返回 None。
    """
    aliases = STANDARD_CURVE_DICT.get(standard_name.upper(), [standard_name.upper()])
    for alias in aliases:
        alias_key = _curve_key(alias)
        for col in df.columns:
            if _curve_key(col) == alias_key:
                return col
    return None