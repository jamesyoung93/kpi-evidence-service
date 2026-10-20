from __future__ import annotations

import contextlib
import json
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from ..contracts import ALL_TABLES, columns
from ..settings import Settings
from .base import AccessDenied, Store, check_ident, scalar_value, sql_literal
from .ddl import TRANSACTIONAL_TABLES, add_column_sql, check_constraints, create_table_sql, normalize_expr, sql_type
from .duckdb_store import merge_sql
from .sqltext import (
    databricks_string,
    inline_params,
    plain_value,
    timestamp_text,
    used_params,
    utc_naive,
)

MAX_MARKERS = 200
LITERAL_BATCH_CHARS = 500_000

_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELED", "CLOSED")
_PERMISSION = re.compile(r"PERMISSION_DENIED|INSUFFICIENT_PERMISSIONS|does not have \w+ (?:privilege|on)", re.I)
_CONDITION = re.compile(r"^\s*\[([A-Z][A-Z0-9_.]*)\]")
_WRITE_TARGET = re.compile(r"\b(?:INSERT\s+INTO|MERGE\s+INTO|UPDATE|DELETE\s+FROM)\s+(?!SET\b)([\w.]+)", re.I)
_UNKNOWN_TARGET = "?"
_VOLUME_PATH = re.compile(r"^/Volumes/(\w+)/(\w+)/(\w+)/?$")


class _StatementInfo:
    def __init__(self, message: str, *, error_code: str | None = None, sql_state: str | None = None,
                 statement_id: str | None = None, state: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.sql_state = sql_state
        self.statement_id = statement_id
        self.state = state
        m = _CONDITION.match(message)
        self.condition = m.group(1) if m else error_code

    def getCondition(self) -> str | None:
        return self.condition

    getErrorClass = getCondition


class StatementError(_StatementInfo, RuntimeError):
    """A warehouse statement failed, was canceled or closed."""


class StatementAccessDenied(_StatementInfo, AccessDenied):
    """A warehouse statement failed because the identity lacks a privilege (a PermissionError)."""


class TransactionReadAfterWrite(RuntimeError):
    """A read inside ``transaction()`` touched a table the block already wrote (writes are deferred)."""


def statement_failure(message: str | None, **info: Any) -> Exception:
    """Build the exception for a failed statement, keeping the server message intact."""
    text = message or f"statement {info.get('state') or 'FAILED'} ({info.get('error_code') or 'no error code'})"
    is_permission = _PERMISSION.search(text) or info.get("error_code") == "PERMISSION_DENIED"
    return (StatementAccessDenied if is_permission else StatementError)(text, **info)


def coerce(value: Any, kind: str) -> Any:
    """Python value for a contract column kind (STRING/JSON/DOUBLE/BIGINT/BOOLEAN/DATE/TIMESTAMP)."""
    value = plain_value(value)
    if value is None:
        return None
    if kind == "BIGINT":
        return int(value)
    if kind == "DOUBLE":
        number = float(value)
        return number if math.isfinite(number) else None
    if kind == "BOOLEAN":
        return bool(value)
    if kind == "DATE":
        if isinstance(value, datetime):
            return value.date()
        return date.fromisoformat(value[:10]) if isinstance(value, str) else value
    if kind == "TIMESTAMP":
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return utc_naive(value) if isinstance(value, datetime) else datetime(value.year, value.month, value.day)
    return str(value)


def value_text(value: Any, kind: str) -> str | None:
    """Parameter-marker text for a coerced value (the SQL CASTs it to the column type)."""
    if value is None:
        return None
    if kind == "TIMESTAMP":
        return timestamp_text(value)
    if kind == "DATE":
        return value.isoformat()
    if kind == "BOOLEAN":
        return "true" if value else "false"
    if kind == "DOUBLE":
        return repr(float(value))
    return str(value)


def cell_literal(value: Any, kind: str) -> str:
    """Inlined literal for a coerced value (wrapped in a CAST by the caller)."""
    if value is None:
        return "NULL"
    if kind in ("BIGINT", "DOUBLE", "BOOLEAN"):
        return sql_literal(value)
    return databricks_string(value_text(value, kind))


def rows_for(cls: type, frame: pd.DataFrame) -> list[tuple]:
    """Frame rows as tuples of plain Python values in contract column order."""
    cols = columns(cls)
    return [tuple(coerce(v, kind) for v, (_n, kind, _nl, _e) in zip(row, cols))
            for row in frame[[c[0] for c in cols]].itertuples(index=False, name=None)]


def values_source(names: Sequence[str], kinds: Sequence[str], rows: Sequence[tuple],
                  render: Callable[[int, int, Any, str], str]) -> str:
    """"""
    tuples = []
    for r, row in enumerate(rows):
        cells = [f"CAST({render(r, c, value, kind)} AS {sql_type(kind, 'databricks')})"
                 for c, (value, kind) in enumerate(zip(row, kinds))]
        tuples.append("(" + ", ".join(cells) + ")")
    return f"(SELECT * FROM VALUES {', '.join(tuples)} AS v({', '.join(names)}))"


def spark_schema(cls: type) -> Any:
    """Explicit ``StructType`` for a contract dataclass (imports pyspark lazily)."""
    from pyspark.sql import types as T

    spark_types = {"STRING": T.StringType, "JSON": T.StringType, "DOUBLE": T.DoubleType, "BIGINT": T.LongType,
                   "BOOLEAN": T.BooleanType, "DATE": T.DateType, "TIMESTAMP": T.TimestampType}
    return T.StructType([T.StructField(name, spark_types[kind](), nullable)
                         for name, kind, nullable, _enum in columns(cls)])


def spark_frame(cls: type, rows: Sequence[tuple]) -> pd.DataFrame:
    """Pandas source for ``createDataFrame``: object columns of plain values; timestamps as UTC instants."""
    names = [c[0] for c in columns(cls)]
    frame = pd.DataFrame(list(rows), columns=names, dtype=object)
    for name, kind, _nullable, _enum in columns(cls):
        if kind == "TIMESTAMP":
            frame[name] = pd.to_datetime(frame[name]).dt.tz_localize("UTC")
    return frame


def infer_kind(series: pd.Series) -> str:
    """Contract-style kind for an arbitrary DataFrame column (demo loads through write_dataframe)."""
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(dtype):
        return "BIGINT"
    if pd.api.types.is_float_dtype(dtype):
        return "DOUBLE"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "TIMESTAMP"
    for value in series:
        value = plain_value(value)
        if value is None:
            continue
        if isinstance(value, bool):
            return "BOOLEAN"
        if isinstance(value, int):
            return "BIGINT"
        if isinstance(value, float):
            return "DOUBLE"
        if isinstance(value, datetime):
            return "TIMESTAMP"
        if isinstance(value, date):
            return "DATE"
        return "STRING"
    return "STRING"


def _type_name(col: Any) -> str:
    name = getattr(col.type_name, "value", col.type_name)
    text = (col.type_text or "").upper()
    if text.startswith("TIMESTAMP_NTZ"):
        return "TIMESTAMP_NTZ"
    if name:
        return str(name).upper()
    return re.split(r"[^A-Z_]", text or "STRING")[0] or "STRING"


def _iso_datetimes(raw: list[Any], *, utc: bool) -> pd.Series:
    series = pd.Series(raw, dtype=object)
    try:
        return pd.to_datetime(series, utc=utc, format="ISO8601")
    except (TypeError, ValueError):
        return pd.to_datetime(series, utc=utc)


def _convert(type_name: str, raw: list[Any]) -> pd.Series:
    if type_name in ("INT", "LONG", "SHORT", "BYTE"):
        return pd.Series([None if v is None else int(v) for v in raw], dtype="Int64")
    if type_name in ("DOUBLE", "FLOAT", "DECIMAL"):
        return pd.Series([math.nan if v is None else float(v) for v in raw], dtype="float64")
    if type_name == "BOOLEAN":
        return pd.Series([None if v is None else str(v).lower() == "true" for v in raw], dtype="boolean")
    if type_name == "DATE":
        return pd.to_datetime(pd.Series(raw, dtype=object))
    if type_name == "TIMESTAMP":
        return _iso_datetimes(raw, utc=True).dt.tz_convert(None)
    if type_name == "TIMESTAMP_NTZ":
        return _iso_datetimes(raw, utc=False)
    if type_name in ("ARRAY", "MAP", "STRUCT"):
        return pd.Series([None if v is None else json.loads(v) for v in raw], dtype=object)
    return pd.Series(raw, dtype=object)


def typed_frame(manifest_columns: Sequence[Any], rows: Sequence[Sequence[Any]]) -> pd.DataFrame:
    """JSON_ARRAY rows (strings or null) -> DataFrame typed like DuckDB's ``.df()`` from the manifest schema."""
    cols = sorted(manifest_columns, key=lambda c: c.position if c.position is not None else 0)
    data = {}
    for i, col in enumerate(cols):
        data[i] = _convert(_type_name(col), [r[i] if i < len(r) else None for r in rows]).reset_index(drop=True)
    frame = pd.DataFrame(data, index=range(len(rows))) if cols else pd.DataFrame()
    frame.columns = [c.name for c in cols]
    return frame


def _affected(result: pd.DataFrame | None, default: int) -> int:
    if result is None or result.empty:
        return default
    for name in ("num_affected_rows", "num_inserted_rows"):
        if name in result.columns and not pd.isna(result[name].iloc[0]):
            return int(result[name].iloc[0])
    return default


def compound_statement(statements: Sequence[str]) -> str:
    body = "".join(f"  {s.strip().rstrip(';').strip()};\n" for s in statements)
    return f"BEGIN ATOMIC\n{body}END;"


def active_spark() -> Any | None:
    """The running Spark session in a job task or notebook, else None (the app has no Spark)."""
    try:
        from pyspark.sql import SparkSession
    except ImportError:
        return None
    session = SparkSession.getActiveSession()
    if session is None and os.environ.get("DATABRICKS_RUNTIME_VERSION"):
        session = SparkSession.builder.getOrCreate()
    return session


@dataclass
class _TxBuffer:
    statements: list[str] = field(default_factory=list)
    tables: set[str] = field(default_factory=set)

    def add(self, sql: str, table: str | None = None) -> None:
        self.statements.append(sql)
        targets = {table} if table else {m.group(1) for m in _WRITE_TARGET.finditer(sql)}
        self.tables.update(t.lower() for t in targets or {_UNKNOWN_TARGET})


class DatabricksStore(Store):
    dialect = "databricks"

    def __init__(self, settings: Settings, spark: Any | None = None, client: Any | None = None, *,
                 warehouse_id: str | None = None, poll_interval: float = 0.5, timeout_s: float | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        super().__init__(settings)
        if settings.is_local:
            raise ValueError("DatabricksStore needs settings.backend='databricks' (names are catalog-qualified)")
        check_ident(settings.catalog)
        self.spark = spark
        self.mode = "spark" if spark is not None else "warehouse"
        self.warehouse_id = warehouse_id or settings.warehouse_id
        if self.mode == "warehouse" and not self.warehouse_id:
            raise ValueError("warehouse mode needs settings.warehouse_id (or pass spark=<SparkSession>)")
        self.atomic = bool(settings.extra.get("atomic", False))
        self.poll_interval = poll_interval
        self.timeout_s = float(timeout_s if timeout_s is not None else settings.extra.get("statement_timeout_s", 900))
        self._client = client
        self._sleep = sleep
        self._local = threading.local()

    @classmethod
    def from_runtime(cls, settings: Settings, client: Any | None = None) -> "DatabricksStore":
        """Spark mode when a Spark session is available (job task, notebook), else warehouse mode (app)."""
        spark = None if settings.extra.get("store_mode") == "warehouse" else active_spark()
        return cls(settings, spark=spark, client=client)

    @property
    def client(self) -> Any:
        if self._client is None:
            from databricks.sdk import WorkspaceClient

            self._client = WorkspaceClient()
        return self._client

    @property
    def _tx(self) -> _TxBuffer | None:
        return getattr(self._local, "tx", None)

    @_tx.setter
    def _tx(self, value: _TxBuffer | None) -> None:
        self._local.tx = value

    def query(self, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        self._guard_read(sql)
        if self.mode == "spark":
            return self._spark_sql(sql, params).toPandas()
        return self._statement(sql, params)

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        tx = self._tx
        if tx is not None:
            tx.add(inline_params(sql, used_params(sql, params, backslash_escapes=True)))
            return
        self._run(sql, params)

    def _run(self, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame | None:
        if self.mode == "spark":
            result = self._spark_sql(sql, params)
            rows = result.collect()
            return pd.DataFrame([tuple(r) for r in rows], columns=list(result.columns))
        return self._statement(sql, params)

    def _guard_read(self, sql: str) -> None:
        tx = self._tx
        if tx is None:
            return
        text = sql.lower()
        hit = sorted(t for t in tx.tables if t != _UNKNOWN_TARGET and t in text)
        if hit:
            raise TransactionReadAfterWrite(
                f"read of {', '.join(hit)} inside store.transaction() after writing it: on Databricks the writes "
                "are sent as one BEGIN ATOMIC block when the transaction ends, so this read cannot see them; "
                "read before writing")

    def _spark_sql(self, sql: str, params: dict[str, Any] | None) -> Any:
        args = {k: plain_value(v) for k, v in used_params(sql, params, backslash_escapes=True).items()}
        return self.spark.sql(sql, args=args) if args else self.spark.sql(sql)

    def _statement(self, sql: str, params: dict[str, Any] | None = None, *, row_limit: int | None = None,
                   parameters: list[Any] | None = None) -> pd.DataFrame:
        if parameters is None:
            parameters = statement_parameters(used_params(sql, params, backslash_escapes=True))
        resp = self._submit(sql, parameters, row_limit)
        return self._result_frame(resp)

    def _submit(self, sql: str, parameters: list[Any], row_limit: int | None) -> Any:
        from databricks.sdk.service.sql import Disposition, ExecuteStatementRequestOnWaitTimeout, Format

        api = self.client.statement_execution
        kwargs: dict[str, Any] = dict(
            statement=sql, warehouse_id=self.warehouse_id, wait_timeout="30s",
            on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
            disposition=Disposition.INLINE, format=Format.JSON_ARRAY)
        if parameters:
            kwargs["parameters"] = parameters
        if row_limit is not None:
            kwargs["row_limit"] = row_limit
        resp = api.execute_statement(**kwargs)
        deadline = time.monotonic() + self.timeout_s
        delay = self.poll_interval
        while _state(resp) not in _TERMINAL:
            if time.monotonic() >= deadline:
                with contextlib.suppress(Exception):
                    api.cancel_execution(resp.statement_id)
                raise TimeoutError(f"statement {resp.statement_id} still {_state(resp)} after "
                                   f"{self.timeout_s:.0f}s; canceled")
            self._sleep(delay)
            delay = min(delay * 1.5, 5.0)
            resp = api.get_statement(resp.statement_id)
        if _state(resp) != "SUCCEEDED":
            error = resp.status.error if resp.status else None
            raise statement_failure(
                getattr(error, "message", None),
                error_code=_enum_text(getattr(error, "error_code", None)),
                sql_state=getattr(resp.status, "sql_state", None), statement_id=resp.statement_id,
                state=_state(resp))
        return resp

    def _result_frame(self, resp: Any) -> pd.DataFrame:
        manifest = resp.manifest
        if manifest is None or manifest.schema is None or not manifest.schema.columns:
            return pd.DataFrame()
        if manifest.truncated:
            raise StatementError(
                f"statement {resp.statement_id} result was truncated (INLINE results are capped at 25 MiB); "
                "narrow the query", statement_id=resp.statement_id, state="SUCCEEDED")
        rows: list[Sequence[Any]] = []
        data = resp.result
        while data is not None:
            rows.extend(data.data_array or [])
            nxt = data.next_chunk_index
            data = (self.client.statement_execution.get_statement_result_chunk_n(resp.statement_id, nxt)
                    if nxt is not None else None)
        return typed_frame(manifest.schema.columns, rows)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Run writes one by one, or as one BEGIN ATOMIC block when enabled and every table is catalog-managed."""
        if self._tx is not None or not self.atomic:
            yield
            return
        tx = _TxBuffer()
        self._tx = tx
        try:
            yield
        finally:
            self._tx = None
        if not tx.statements:
            return
        if tx.tables <= self.catalog_managed_tables():
            self._run(compound_statement(tx.statements))
            return
        for statement in tx.statements:
            self._run(statement)

    def catalog_managed_tables(self) -> set[str]:
        """Lowercase qualified names of the service tables created with catalog-managed commits."""
        return {self.fq(name).lower() for name in TRANSACTIONAL_TABLES}

    def _write_frame(self, cls: type, frame: pd.DataFrame, mode: str, match: dict[str, Any] | None = None) -> int:
        if mode not in ("insert", "insert_new", "upsert", "replace"):
            raise ValueError(mode)
        table = self.fq(cls.TABLE)
        tx = self._tx
        if mode == "replace":
            if not match:
                raise ValueError("replace needs a non-empty match filter")
            delete = f"DELETE FROM {table} WHERE " + " AND ".join(f"{check_ident(k)} = :{k}" for k in match)
            if tx is not None:
                tx.add(inline_params(delete, match), table)
            else:
                self._run(delete, match)
            mode = "insert"
        if frame.empty:
            return 0
        rows = rows_for(cls, frame)
        if tx is not None:
            names = [c[0] for c in columns(cls)]
            kinds = [c[1] for c in columns(cls)]
            source = values_source(names, kinds, rows, lambda _r, _c, value, kind: cell_literal(value, kind))
            tx.add(self._write_sql(cls, table, source, mode), table)
            return len(rows)
        if self.mode == "spark":
            return self._spark_write(cls, table, rows, mode)
        return self._warehouse_write(cls, table, rows, mode)

    @staticmethod
    def _write_sql(cls: type, table: str, source: str, mode: str) -> str:
        if mode == "insert":
            col_list = ", ".join(c[0] for c in columns(cls))
            return f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {source} AS s"
        return merge_sql(cls, table, source, mode)

    def _spark_write(self, cls: type, table: str, rows: list[tuple], mode: str) -> int:
        view = f"kpi_src_{cls.TABLE}_{uuid.uuid4().hex[:12]}"
        self.spark.createDataFrame(spark_frame(cls, rows), schema=spark_schema(cls)).createOrReplaceTempView(view)
        try:
            return _affected(self._run(self._write_sql(cls, table, view, mode)), len(rows))
        finally:
            with contextlib.suppress(Exception):
                self.spark.catalog.dropTempView(view)

    def _warehouse_write(self, cls: type, table: str, rows: list[tuple], mode: str) -> int:
        from databricks.sdk.service.sql import StatementParameterListItem

        names = [c[0] for c in columns(cls)]
        kinds = [c[1] for c in columns(cls)]
        per_statement = max(1, MAX_MARKERS // len(names))
        total = 0
        for start in range(0, len(rows), per_statement):
            batch = rows[start:start + per_statement]
            params: list[Any] = []

            def marker(r: int, c: int, value: Any, kind: str) -> str:
                name = f"p{r}_{c}"
                params.append(StatementParameterListItem(name=name, value=value_text(value, kind)))
                return f":{name}"

            source = values_source(names, kinds, batch, marker)
            result = self._statement(self._write_sql(cls, table, source, mode), parameters=params)
            total += _affected(result, len(batch))
        return total

    def register_frame(self, name: str, frame: pd.DataFrame, table: str) -> None:
        """Create or replace a plain table from a DataFrame (parity with ``DuckDBStore.register_frame``)."""
        check_ident(name)
        self.write_dataframe(table, frame, mode="overwrite")

    def write_dataframe(self, qualified_table: str, frame: pd.DataFrame, mode: str = "overwrite") -> int:
        """Create/replace (``overwrite``) or append to a plain table; demo platform loads."""
        parts = [check_ident(p) for p in qualified_table.split(".")]
        if mode not in ("overwrite", "append"):
            raise ValueError(mode)
        if self._tx is not None:
            raise RuntimeError("write_dataframe cannot run inside store.transaction() (DDL is not transactional)")
        if len(parts) == 3:
            self._run(f"CREATE SCHEMA IF NOT EXISTS {parts[0]}.{parts[1]}")
        if self.mode == "spark":
            writer = self.spark.createDataFrame(frame).write.mode(mode)
            if mode == "overwrite":
                writer = writer.option("overwriteSchema", "true")
            writer.saveAsTable(qualified_table)
            return len(frame)
        names = [check_ident(str(c)) for c in frame.columns]
        kinds = [infer_kind(frame[c]) for c in frame.columns]
        if frame.empty:
            if mode == "overwrite":
                cols = ", ".join(f"{n} {sql_type(k, 'databricks')}" for n, k in zip(names, kinds))
                self._run(f"CREATE OR REPLACE TABLE {qualified_table} ({cols})")
            return 0
        rows = [tuple(coerce(v, k) for v, k in zip(row, kinds)) for row in frame.itertuples(index=False, name=None)]
        first = True
        for batch in _literal_batches(rows, kinds):
            source = values_source(names, kinds, batch, lambda _r, _c, value, kind: cell_literal(value, kind))
            if first and mode == "overwrite":
                self._run(f"CREATE OR REPLACE TABLE {qualified_table} AS SELECT * FROM {source} AS s")
            else:
                self._run(f"INSERT INTO {qualified_table} SELECT * FROM {source} AS s")
            first = False
        return len(frame)

    def ensure_schema(self) -> None:
        """Schemas, every service table (plus contract columns an older table lacks), constraints, views."""
        s = self.settings
        for schema in dict.fromkeys((s.schema, s.gov_schema)):
            self.execute(f"CREATE SCHEMA IF NOT EXISTS {s.schema_prefix(check_ident(schema))}")
        for cls in ALL_TABLES:
            self.execute(create_table_sql(cls, self.fq(cls.TABLE), "databricks"))
        self._add_missing_columns()
        for cls in ALL_TABLES:
            self._ensure_constraints(cls)
        if s.extra.get("create_volume"):
            self.execute(f"CREATE VOLUME IF NOT EXISTS {self.volume_name()}")
        self.ensure_views()

    def volume_name(self) -> str:
        """``catalog.schema.volume`` of the reports Volume (from ``settings.volume_path`` when it is a Volume)."""
        m = _VOLUME_PATH.match(self.settings.volume_path or "")
        if m:
            return ".".join(m.groups())
        return f"{self.settings.schema_prefix(self.settings.schema)}.reports"

    def _add_missing_columns(self) -> None:
        s = self.settings
        found = self.query(
            f"SELECT table_name, column_name FROM {s.catalog}.information_schema.columns "
            "WHERE table_schema = :schema", {"schema": s.schema})
        have: dict[str, set[str]] = {}
        for table_name, column_name in zip(found.get("table_name", []), found.get("column_name", [])):
            have.setdefault(str(table_name).lower(), set()).add(str(column_name).lower())
        for cls in ALL_TABLES:
            present = have.get(cls.TABLE)
            if not present:
                continue
            for name, kind, _nullable, _enum in columns(cls):
                if name not in present:
                    self.execute(add_column_sql(self.fq(cls.TABLE), name, kind, "databricks"))

    def _ensure_constraints(self, cls: type) -> None:
        wanted = check_constraints(cls)
        if not wanted:
            return
        table = self.fq(cls.TABLE)
        try:
            props = self.query(f"SHOW TBLPROPERTIES {table}")
            keys = props["key"] if "key" in props.columns else props.iloc[:, 0]
            values = props["value"] if "value" in props.columns else props.iloc[:, 1]
            existing = {str(k).lower(): str(v) for k, v in zip(keys, values)}
        except Exception:
            existing = {}
        for name, expr in wanted:
            current = existing.get(f"delta.constraints.{name.lower()}")
            if current is not None and normalize_expr(current) == normalize_expr(expr):
                continue
            self.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
            self.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expr})")

    def runner(self) -> "DatabricksRunner":
        return DatabricksRunner(self)


class DatabricksRunner:
    """reconloop runner (``SparkRunner``-compatible) over the store's Spark session or warehouse."""

    def __init__(self, store: DatabricksStore):
        self.store = store
        self.calls = 0

    def run(self, sql: str) -> float:
        self.calls += 1
        if self.store.mode == "spark":
            result = self.store.spark.sql(sql)
            rows = result.take(2)
            frame = pd.DataFrame([tuple(r) for r in rows], columns=list(result.columns))
        else:
            frame = self.store._statement(sql, row_limit=2)
        return scalar_value(frame)

    def close(self) -> None:
        return None


def _enum_text(value: Any) -> str | None:
    return None if value is None else str(getattr(value, "value", value))


def _state(resp: Any) -> str | None:
    status = getattr(resp, "status", None)
    return _enum_text(getattr(status, "state", None))


def statement_parameters(params: dict[str, Any]) -> list[Any]:
    """Typed ``StatementParameterListItem``s for query parameters (scalars only; NULL is a value-less item)."""
    from databricks.sdk.service.sql import StatementParameterListItem as Item

    out = []
    for name, raw in params.items():
        value = plain_value(raw)
        if value is None:
            out.append(Item(name=name))
        elif isinstance(value, bool):
            out.append(Item(name=name, value="true" if value else "false", type="BOOLEAN"))
        elif isinstance(value, int):
            out.append(Item(name=name, value=str(value), type="BIGINT"))
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"parameter :{name} is not finite")
            out.append(Item(name=name, value=repr(value), type="DOUBLE"))
        elif isinstance(value, datetime):
            out.append(Item(name=name, value=timestamp_text(value), type="TIMESTAMP"))
        elif isinstance(value, date):
            out.append(Item(name=name, value=value.isoformat(), type="DATE"))
        elif isinstance(value, (list, tuple, set, dict)):
            raise TypeError(f"parameter :{name}: ARRAY/MAP/STRUCT parameters are not supported by the "
                            "Statement Execution API; pass scalars")
        else:
            out.append(Item(name=name, value=str(value), type="STRING"))
    return out


def _literal_batches(rows: list[tuple], kinds: Sequence[str]) -> Iterator[list[tuple]]:
    batch: list[tuple] = []
    size = 0
    for row in rows:
        width = sum(len(cell_literal(v, k)) + 24 for v, k in zip(row, kinds))
        if batch and size + width > LITERAL_BATCH_CHARS:
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += width
    if batch:
        yield batch
