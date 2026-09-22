"""Central display formatting for every Markdown report renderer."""

import math
from collections.abc import Iterable

from cnlc_agent.reports.models import Measurement

MISSING = "未提供"
_MISSING_STRINGS = {"", "none", "null", "nan"}


def is_missing(value: object) -> bool:
    """Treat absent/non-finite values as missing while preserving numeric zero."""

    if value is None:
        return True
    if isinstance(value, float) and not math.isfinite(value):
        return True
    return isinstance(value, str) and value.strip().casefold() in _MISSING_STRINGS


def text(value: object) -> str:
    if is_missing(value):
        return MISSING
    return str(value).strip()


def number(value: object, decimals: int = 2) -> str:
    if is_missing(value) or isinstance(value, bool) or not isinstance(value, int | float):
        return MISSING
    return f"{float(value):.{decimals}f}"


def measurement(value: Measurement, decimals: int = 2) -> str:
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
    rendered = [text(value) for value in values if not is_missing(value)]
    return separator.join(rendered) if rendered else MISSING
