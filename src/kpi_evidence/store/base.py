from __future__ import annotations

import abc
import contextlib
import math
import re
from collections.abc import Iterator, Sequence
from typing import Any

import pandas as pd

from ..contracts import ALL_TABLES, columns, from_row, to_row
from ..settings import Settings

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AccessDenied(PermissionError):
    """Raised by runners when the executing identity may not read an object (message keeps PERMISSION_DENIED)."""


class MetricQueryError(ValueError):
    """The reconloop runner contract was violated (not one row/one 'value' column, NULL or non-finite)."""


def check_ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


def sql_literal(value: Any) -> str:
    """Render a Python scalar as a SQL literal (only for values that cannot be bound as parameters)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite literal")
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def scalar_value(df: pd.DataFrame) -> float:
    """Enforce reconloop's runner contract on a query result."""
    if list(df.columns) != ["value"] or len(df) != 1:
        raise MetricQueryError(
            f"metric query must return exactly one row and one column named 'value' (got {len(df)} rows, columns {list(df.columns)})")
    raw = df.iloc[0, 0]
    if raw is None or (isinstance(raw, float) and math.isnan(raw)) or pd.isna(raw):
        raise MetricQueryError("metric query returned NULL; explicitly use COALESCE if zero is intended")
    value = float(raw)
    if not math.isfinite(value):
        raise MetricQueryError("metric query must return a finite value")
    return value


class Store(abc.ABC):
    dialect: str

    def __init__(self, settings: Settings):
        self.settings = settings
        self._platform_access = None

    @property
    def platform_access(self):
        """The platform this store's KPI SQL runs on; the store's own database unless one is attached."""
        if self._platform_access is None:
            from ..platform_access import StorePlatform

            self._platform_access = StorePlatform(self)
        return self._platform_access

    def attach_platform(self, access) -> None:
        self._platform_access = access

    def fq(self, table: str) -> str:
        return self.settings.table(check_ident(table))

    def gov(self, view: str) -> str:
        return self.settings.platform_qualified(self.settings.gov_schema, check_ident(view))

    def platform(self, name: str) -> str:
        return self.settings.platform_qualified(self.settings.platform_schema, check_ident(name))

    @abc.abstractmethod
    def query(self, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame: ...

    @abc.abstractmethod
    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None: ...

    def execute_script(self, script: str) -> None:
        for statement in split_sql(script):
            self.execute(statement)

    @abc.abstractmethod
    def _write_frame(self, cls: type, frame: pd.DataFrame, mode: str, match: dict[str, Any] | None = None) -> int:
        ...

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Best-effort atomic unit."""
        yield

    @abc.abstractmethod
    def runner(self) -> Any:
        """A reconloop-compatible runner: ``run(sql) -> float`` with the one-row/one-'value' contract."""

    @abc.abstractmethod
    def ensure_schema(self) -> None:
        """Create schemas and every service table if missing (idempotent)."""

    def ensure_views(self) -> None:
        """Create or replace the service views (``src/sql/service_views.sql``)."""
        from ..settings import default_sql_dir

        path = default_sql_dir() / "service_views.sql"
        if not path.exists():
            return
        script = path.read_text(encoding="utf-8").replace("${schema}", self.settings.schema_prefix(self.settings.schema))
        script = script.replace("${gov}", self.settings.schema_prefix(self.settings.gov_schema))
        self.execute_script(script)

    def write_dataframe(self, qualified_table: str, frame: pd.DataFrame, mode: str = "overwrite") -> int:
        """Create/replace (mode='overwrite') or append to a plain table from a DataFrame."""
        raise NotImplementedError(f"{type(self).__name__}.write_dataframe")

    def frame(self, records: Sequence[Any]) -> pd.DataFrame:
        if not records:
            return pd.DataFrame()
        cls = type(records[0])
        if any(type(r) is not cls for r in records):
            raise TypeError("records must share one type")
        rows = [to_row(r) for r in records]
        return pd.DataFrame(rows, columns=[c[0] for c in columns(cls)])

    def _dedupe(self, cls: type, frame: pd.DataFrame) -> pd.DataFrame:
        return frame.drop_duplicates(subset=list(cls.KEY), keep="last")

    def insert(self, records: Sequence[Any]) -> int:
        if not records:
            return 0
        cls = type(records[0])
        return self._write_frame(cls, self.frame(records), "insert")

    def insert_new(self, records: Sequence[Any]) -> int:
        if not records:
            return 0
        cls = type(records[0])
        return self._write_frame(cls, self._dedupe(cls, self.frame(records)), "insert_new")

    def upsert(self, records: Sequence[Any]) -> int:
        if not records:
            return 0
        cls = type(records[0])
        return self._write_frame(cls, self._dedupe(cls, self.frame(records)), "upsert")

    def replace(self, cls: type, match: dict[str, Any], records: Sequence[Any]) -> int:
        """Delete rows of ``cls`` whose columns equal ``match`` then insert ``records`` (may be empty)."""
        for key in match:
            check_ident(key)
        frame = self.frame(records) if records else pd.DataFrame(columns=[c[0] for c in columns(cls)])
        if records:
            frame = self._dedupe(cls, frame)
        return self._write_frame(cls, frame, "replace", match=match)

    def read_df(self, cls_or_table: type | str, where: str = "", params: dict[str, Any] | None = None,
                order_by: str = "", limit: int | None = None, columns_sql: str = "*") -> pd.DataFrame:
        table = cls_or_table if isinstance(cls_or_table, str) else cls_or_table.TABLE
        sql = f"SELECT {columns_sql} FROM {self.fq(table)}"
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, params)

    def read(self, cls: type, where: str = "", params: dict[str, Any] | None = None,
             order_by: str = "", limit: int | None = None) -> list[Any]:
        df = self.read_df(cls, where, params, order_by, limit)
        return [from_row(cls, row) for row in df.to_dict(orient="records")]

    def get(self, cls: type, **key: Any) -> Any | None:
        if set(key) != set(cls.KEY):
            raise ValueError(f"{cls.__name__}.get needs exactly {cls.KEY}")
        where = " AND ".join(f"{check_ident(k)} = :{k}" for k in key)
        rows = self.read(cls, where, key, limit=2)
        return rows[0] if rows else None

    def count(self, cls: type, where: str = "", params: dict[str, Any] | None = None) -> int:
        df = self.read_df(cls, where, params, columns_sql="COUNT(*) AS n")
        return int(df.iloc[0, 0])


def split_sql(script: str) -> list[str]:
    """Split a SQL script on semicolons outside quotes and comments; drops empty statements."""
    out, buf, quote, i = [], [], None, 0
    while i < len(script):
        ch = script[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif script.startswith("--", i):
            j = script.find("\n", i)
            i = len(script) if j < 0 else j
            continue
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def table_classes() -> dict[str, type]:
    return {cls.TABLE: cls for cls in ALL_TABLES}
