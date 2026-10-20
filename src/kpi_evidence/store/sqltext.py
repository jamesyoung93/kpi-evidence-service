from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from .base import sql_literal

_SKIP_STANDARD = r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|`[^`]*`|--[^\n]*|/\*.*?\*/"""
_SKIP_BACKSLASH = r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`[^`]*`|--[^\n]*|/\*.*?\*/"""
_MARKER = r"(?<![:\w]):([A-Za-z_]\w*)"
_SCANNERS = {
    False: re.compile(f"({_SKIP_STANDARD})|{_MARKER}", re.S),
    True: re.compile(f"({_SKIP_BACKSLASH})|{_MARKER}", re.S),
}


def replace_markers(sql: str, replace: Callable[[str], str], *, backslash_escapes: bool = False) -> str:
    """Return ``sql`` with every ``:name`` marker outside literals/comments replaced by ``replace(name)``."""

    def _sub(m: re.Match) -> str:
        return m.group(1) if m.group(1) is not None else replace(m.group(2))

    return _SCANNERS[backslash_escapes].sub(_sub, sql)


def marker_names(sql: str, *, backslash_escapes: bool = False) -> list[str]:
    """Distinct ``:name`` markers in order of first use (literals and comments are skipped)."""
    names: list[str] = []
    for m in _SCANNERS[backslash_escapes].finditer(sql):
        if m.group(2) is not None and m.group(2) not in names:
            names.append(m.group(2))
    return names


def used_params(sql: str, params: dict[str, Any] | None, *, backslash_escapes: bool = False) -> dict[str, Any]:
    """Keep only the parameters the statement references."""
    if not params:
        return {}
    names = set(marker_names(sql, backslash_escapes=backslash_escapes))
    return {k: v for k, v in params.items() if k in names}


def is_null(value: Any) -> bool:
    """None, NaN, NaT and pd.NA are all SQL NULL."""
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    try:
        import pandas as pd

        return value is pd.NaT or value is pd.NA
    except ImportError:
        return False


def plain_value(value: Any) -> Any:
    """Normalize numpy/pandas scalars and enums to plain Python values (NULL-likes become None)."""
    if is_null(value):
        return None
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        return value.item()
    return value


def utc_naive(value: datetime) -> datetime:
    """Aware datetimes -> naive UTC (the service stores naive UTC timestamps)."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def timestamp_text(value: datetime) -> str:
    """ISO-8601 UTC text with an explicit ``Z`` so the instant does not depend on the session time zone."""
    return utc_naive(value).isoformat() + "Z"


def databricks_string(text: str) -> str:
    """A Spark SQL string literal: backslashes and quotes escaped with a backslash."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def databricks_literal(value: Any) -> str:
    """Render a Python scalar as a Databricks SQL literal (inlined values inside BEGIN ATOMIC blocks)."""
    value = plain_value(value)
    if value is None or isinstance(value, (bool, int, float)):
        return sql_literal(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite literal")
        return f"CAST('{value}' AS DECIMAL(38, {max(0, -value.as_tuple().exponent)}))"
    if isinstance(value, datetime):
        return f"TIMESTAMP '{timestamp_text(value)}'"
    if isinstance(value, date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, (list, tuple, dict, set)):
        raise TypeError(f"cannot inline a {type(value).__name__} as a SQL literal")
    return databricks_string(str(value))


def inline_params(sql: str, params: dict[str, Any] | None, *, dialect: str = "databricks") -> str:
    """Replace ``:name`` markers with literals (only for statements that cannot carry parameters)."""
    params = params or {}
    render = databricks_literal if dialect == "databricks" else sql_literal

    def _render(name: str) -> str:
        if name not in params:
            raise KeyError(f"no value for SQL parameter :{name}")
        return render(params[name])

    return replace_markers(sql, _render, backslash_escapes=dialect == "databricks")
