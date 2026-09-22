"""所有 Markdown 报告渲染器共享的显示格式化规则。"""

import math
from collections.abc import Iterable

from cnlc_agent.reports.models import Measurement

MISSING = "未提供"
_MISSING_STRINGS = {"", "none", "null", "nan"}


def is_missing(value: object) -> bool:
    """把空值和非有限数视为缺失，同时保留有业务意义的数值零。"""

    if value is None:
        return True
    if isinstance(value, float) and not math.isfinite(value):
        return True
    return isinstance(value, str) and value.strip().casefold() in _MISSING_STRINGS


def text(value: object) -> str:
    """把普通值转换为展示文本，缺失值统一显示“未提供”。"""

    if is_missing(value):
        return MISSING
    return str(value).strip()


def number(value: object, decimals: int = 2) -> str:
    """格式化有限数值；布尔值不作为数字输出。"""

    if is_missing(value) or isinstance(value, bool) or not isinstance(value, int | float):
        return MISSING
    return f"{float(value):.{decimals}f}"


def measurement(value: Measurement, decimals: int = 2) -> str:
    """按单位格式化测量值，其中 fraction 统一转换为百分比。"""

    if is_missing(value.value):
        return MISSING
    if isinstance(value.value, bool):
        return text(value.value)
    if isinstance(value.value, int | float):
        numeric = float(value.value)
        if not math.isfinite(numeric):
            return MISSING
        if value.unit == "fraction":
            return f"{numeric * 100:.{decimals}f}%"
        rendered = f"{numeric:.{decimals}f}"
    else:
        rendered = text(value.value)
    unit = "" if is_missing(value.unit) or value.unit == "fraction" else f" {value.unit}"
    return f"{rendered}{unit}"


def markdown_cell(value: object) -> str:
    """转义 Markdown 表格单元格中的 HTML、竖线和换行。"""

    return (
        text(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("\n", " / ")
        .replace("\r", "")
    )


def join_present(values: Iterable[object], separator: str = "、") -> str:
    """忽略缺失项后连接展示值；没有有效项时返回统一缺失文案。"""

    rendered = [text(value) for value in values if not is_missing(value)]
    return separator.join(rendered) if rendered else MISSING
