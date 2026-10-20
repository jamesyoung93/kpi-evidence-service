from __future__ import annotations

import contextlib
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from ..contracts import ALL_TABLES, columns
from ..settings import Settings
from .base import AccessDenied, Store, check_ident, scalar_value
from .ddl import add_column_sql, create_table_sql, sql_type
from .sqltext import replace_markers, used_params


def to_duckdb_params(sql: str) -> str:
    """Rewrite ``:name`` markers to DuckDB's ``$name``."""
    return replace_markers(sql, lambda name: "$" + name)


def merge_sql(cls: type, table: str, source: str, mode: str) -> str:
    """MERGE statement for ``insert_new`` (insert-only on KEY) or ``upsert`` (update changed non-key columns)."""
    cols = [c[0] for c in columns(cls)]
    col_list = ", ".join(cols)
    on = " AND ".join(f"t.{k} = s.{k}" for k in cls.KEY)
    values = ", ".join(f"s.{c}" for c in cols)
    non_key = [c for c in cols if c not in cls.KEY]
    matched = ""
    if mode == "upsert" and non_key:
        changed = " OR ".join(f"t.{c} IS DISTINCT FROM s.{c}" for c in non_key)
        update = ", ".join(f"{c} = s.{c}" for c in non_key)
        matched = f"WHEN MATCHED AND ({changed}) THEN UPDATE SET {update} "
    elif mode not in ("insert_new", "upsert"):
        raise ValueError(mode)
    return (f"MERGE INTO {table} AS t USING {source} AS s ON {on} {matched}"
            f"WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({values})")


def _typed_source(cls: type, frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for name, kind, _nullable, _enum in columns(cls):
        col = out[name]
        try:
            if kind == "BIGINT":
                out[name] = col.astype("Int64")
            elif kind == "DOUBLE":
                out[name] = pd.to_numeric(col).astype("float64")
            elif kind == "BOOLEAN":
                out[name] = col.astype("boolean")
            elif kind in ("DATE", "TIMESTAMP"):
                out[name] = pd.to_datetime(col)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"{cls.__name__}.{name}: values do not fit {kind}: {exc}") from exc
    return out


def _count(result: Any) -> int:
    row = result.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


class DuckDBStore(Store):
    dialect = "duckdb"

    def __init__(self, settings: Settings, path: str | Path | None = None, read_only: bool = False,
                 keep_open: bool = False):
        super().__init__(settings)
        self.path = str(path) if path is not None else str(settings.local_db_path())
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.read_only = read_only
        self._lock = threading.RLock()
        self._local = threading.local()
        self._keep_open = self.path == ":memory:" or bool(keep_open)
        self._memory_con = duckdb.connect(":memory:") if self.path == ":memory:" else None

    @property
    def _tx_con(self) -> duckdb.DuckDBPyConnection | None:
        return getattr(self._local, "con", None)

    @_tx_con.setter
    def _tx_con(self, con: duckdb.DuckDBPyConnection | None) -> None:
        self._local.con = con

    def _held(self) -> duckdb.DuckDBPyConnection | None:
        if self._keep_open and self._memory_con is None:
            self._memory_con = self._connect_with_retry()
        return self._memory_con

    def close(self) -> None:
        """Checkpoint and close the held connection; a file store reopens it on next use."""
        with self._lock:
            if self._memory_con is None:
                return
            if self.path != ":memory:" and not self.read_only:
                with contextlib.suppress(duckdb.Error):
                    self._memory_con.execute("CHECKPOINT")
            self._memory_con.close()
            self._memory_con = None
            if self.path == ":memory:":
                self._keep_open = False

    @contextlib.contextmanager
    def _con(self) -> Iterator[duckdb.DuckDBPyConnection]:
        if self._tx_con is not None:
            yield self._tx_con
            return
        if self._keep_open:
            with self._lock:
                yield self._held()
            return
        con = self._connect_with_retry()
        try:
            yield con
        finally:
            con.close()

    def _connect_with_retry(self, attempts: int = 40, delay: float = 0.25) -> duckdb.DuckDBPyConnection:
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return duckdb.connect(self.path, read_only=self.read_only)
            except duckdb.IOException as exc:
                last = exc
                time.sleep(delay)
        raise RuntimeError(f"could not open {self.path}: {last}")

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """One DuckDB transaction for the block; nested blocks join the outer one; errors roll back."""
        if self._tx_con is not None:
            yield
            return
        with self._lock:
            con = self._held() or self._connect_with_retry()
            self._tx_con = con
            try:
                con.execute("BEGIN TRANSACTION")
                yield
                con.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(Exception):
                    con.execute("ROLLBACK")
                raise
            finally:
                self._tx_con = None
                if not self._keep_open:
                    con.close()

    def query(self, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        with self._con() as con:
            return con.execute(to_duckdb_params(sql), used_params(sql, params) or None).df()

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        with self._con() as con:
            con.execute(to_duckdb_params(sql), used_params(sql, params) or None)

    def register_frame(self, name: str, frame: pd.DataFrame, table: str) -> None:
        """Create or replace a plain table from a DataFrame (used to load demo platform data)."""
        check_ident(name)
        self.write_dataframe(table, frame, mode="overwrite")

    def write_dataframe(self, qualified_table: str, frame: pd.DataFrame, mode: str = "overwrite") -> int:
        for part in qualified_table.split("."):
            check_ident(part)
        with self._con() as con:
            con.register("_frame", frame)
            try:
                if mode == "overwrite":
                    con.execute(f"CREATE OR REPLACE TABLE {qualified_table} AS SELECT * FROM _frame")
                elif mode == "append":
                    con.execute(f"INSERT INTO {qualified_table} SELECT * FROM _frame")
                else:
                    raise ValueError(mode)
            finally:
                with contextlib.suppress(duckdb.Error):
                    con.unregister("_frame")
        return len(frame)

    def _write_frame(self, cls: type, frame: pd.DataFrame, mode: str, match: dict[str, Any] | None = None) -> int:
        if mode not in ("insert", "insert_new", "upsert", "replace"):
            raise ValueError(mode)
        table = self.fq(cls.TABLE)
        cols = [c[0] for c in columns(cls)]
        casts = ", ".join(f"CAST(s.{n} AS {sql_type(k, 'duckdb')}) AS {n}" for n, k, _n, _e in columns(cls))
        with self._con() as con:
            if mode == "replace":
                where = " AND ".join(f"{check_ident(k)} = ${k}" for k in (match or {}))
                if not where:
                    raise ValueError("replace needs a non-empty match filter")
                con.execute(f"DELETE FROM {table} WHERE {where}", match)
                mode = "insert"
            if frame.empty:
                return 0
            con.register("_src_raw", _typed_source(cls, frame))
            try:
                con.execute(f"CREATE OR REPLACE TEMP VIEW _src AS SELECT {casts} FROM _src_raw AS s")
                if mode == "insert":
                    col_list = ", ".join(cols)
                    return _count(con.execute(f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM _src"))
                return _count(con.execute(merge_sql(cls, table, "_src", mode)))
            finally:
                with contextlib.suppress(duckdb.Error):
                    con.execute("DROP VIEW IF EXISTS _src")
                with contextlib.suppress(duckdb.Error):
                    con.unregister("_src_raw")

    def ensure_schema(self) -> None:
        """Create schemas, every service table (adding contract columns an older file lacks) and the views."""
        s = self.settings
        with self._con() as con:
            for schema in {s.schema, s.gov_schema, s.platform_schema, *s.restricted_schemas}:
                con.execute(f"CREATE SCHEMA IF NOT EXISTS {check_ident(schema)}")
            for cls in ALL_TABLES:
                con.execute(create_table_sql(cls, self.fq(cls.TABLE), "duckdb"))
            existing = con.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = $schema",
                {"schema": s.schema}).fetchall()
            have: dict[str, set[str]] = {}
            for table_name, column_name in existing:
                have.setdefault(table_name.lower(), set()).add(column_name.lower())
            for cls in ALL_TABLES:
                present = have.get(cls.TABLE, set())
                for name, kind, _nullable, _enum in columns(cls):
                    if present and name not in present:
                        con.execute(add_column_sql(self.fq(cls.TABLE), name, kind, "duckdb"))
        self.ensure_views()

    def runner(self) -> "DuckDBRunner":
        return DuckDBRunner(self)


class DuckDBRunner:
    """reconloop runner over the store's DuckDB database, with simulated access control."""

    def __init__(self, store: DuckDBStore):
        self.store = store
        self._restricted = [
            (s, re.compile(rf"(?<![\w$])[\"`]?{re.escape(s)}[\"`]?\s*\.", re.I))
            for s in store.settings.restricted_schemas]
        self.calls = 0

    def check_access(self, sql: str) -> None:
        for schema, pattern in self._restricted:
            if pattern.search(sql):
                raise AccessDenied(
                    f"PERMISSION_DENIED: the pipeline identity lacks SELECT on objects in schema "
                    f"'{schema}' (local access simulation)")

    def run(self, sql: str) -> float:
        self.check_access(sql)
        self.calls += 1
        return scalar_value(self.store.query(sql))

    def close(self) -> None:
        return None
