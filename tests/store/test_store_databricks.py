from __future__ import annotations

import dataclasses
import re
import sys
import types
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
import pytest
from databricks.sdk.service.sql import (
    ColumnInfo,
    ColumnInfoTypeName,
    Disposition,
    ExecuteStatementRequestOnWaitTimeout,
    Format,
    ResultData,
    ResultManifest,
    ResultSchema,
    ServiceError,
    ServiceErrorCode,
    StatementResponse,
    StatementState,
    StatementStatus,
)

from kpi_evidence.contracts import (
    ALL_TABLES,
    DecisionSource,
    Document,
    DocumentFormat,
    DocumentStatus,
    ExtractedValue,
    ExtractionMethod,
    Publication,
    ReviewAction,
    ReviewDecision,
    columns,
    to_row,
)
from kpi_evidence.settings import Settings
from kpi_evidence.store import databricks_store as dbs
from kpi_evidence.store.base import AccessDenied, MetricQueryError
from kpi_evidence.store.databricks_store import (
    DatabricksStore,
    StatementAccessDenied,
    StatementError,
    TransactionReadAfterWrite,
)
from kpi_evidence.store.ddl import check_constraints
from kpi_evidence.store.sqltext import databricks_literal, inline_params, marker_names

T0 = datetime(2026, 6, 10, 9, 30, 15, 250000)
TRICKY = "it's \\ fine; -- :not_a_marker"


class FakeDF:
    def __init__(self, columns=(), rows=()):
        self.columns = list(columns)
        self._rows = [tuple(r) for r in rows]

    def collect(self):
        return list(self._rows)

    def take(self, n):
        return self._rows[:n]

    def toPandas(self):
        return pd.DataFrame(self._rows, columns=self.columns)


class FakeWriter:
    def __init__(self, frame):
        self.frame, self.options, self._mode = frame, {}, None

    def mode(self, mode):
        self._mode = mode
        return self

    def option(self, key, value):
        self.options[key] = value
        return self

    def saveAsTable(self, table):
        self.frame.spark.saved.append((table, self._mode, dict(self.options), self.frame.data))


class FakeSparkFrame:
    def __init__(self, spark, data, schema):
        self.spark, self.data, self.schema = spark, data, schema

    def createOrReplaceTempView(self, name):
        self.spark.views[name] = self

    @property
    def write(self):
        return FakeWriter(self)


class FakeSpark:
    def __init__(self, responder=None):
        self.calls: list[tuple[str, dict | None]] = []
        self.created: list[FakeSparkFrame] = []
        self.views: dict[str, FakeSparkFrame] = {}
        self.dropped: list[str] = []
        self.saved: list = []
        self.responder = responder or (lambda sql, args: FakeDF())
        spark = self

        class _Catalog:
            def dropTempView(self, name):
                spark.dropped.append(name)

        self.catalog = _Catalog()

    def sql(self, sql, args=None):
        self.calls.append((sql, args))
        return self.responder(sql, args)

    def createDataFrame(self, data, schema=None):
        frame = FakeSparkFrame(self, data, schema)
        self.created.append(frame)
        return frame

    @property
    def statements(self):
        return [c[0] for c in self.calls]


def col(name, type_name, position):
    return ColumnInfo(name=name, type_name=ColumnInfoTypeName(type_name), type_text=type_name, position=position)


def ok(cols=(), rows=(), sid="s1", next_chunk=None, truncated=None):
    manifest = ResultManifest(schema=ResultSchema(columns=[col(n, t, i) for i, (n, t) in enumerate(cols)]),
                              truncated=truncated) if cols else None
    result = ResultData(data_array=[list(r) for r in rows], next_chunk_index=next_chunk) if cols else None
    return StatementResponse(statement_id=sid, status=StatementStatus(state=StatementState.SUCCEEDED),
                             manifest=manifest, result=result)


def merge_metrics(affected, inserted=None, sid="s1"):
    inserted = affected if inserted is None else inserted
    return ok([("num_affected_rows", "LONG"), ("num_updated_rows", "LONG"), ("num_deleted_rows", "LONG"),
               ("num_inserted_rows", "LONG")], [[str(affected), str(affected - inserted), "0", str(inserted)]], sid)


def failed(message, code="BAD_REQUEST", sid="s1"):
    return StatementResponse(statement_id=sid, status=StatementStatus(
        state=StatementState.FAILED, error=ServiceError(error_code=ServiceErrorCode(code), message=message),
        sql_state="42P01"))


def running(sid="s1"):
    return StatementResponse(statement_id=sid, status=StatementStatus(state=StatementState.RUNNING))


class FakeStatementAPI:
    def __init__(self, responder):
        self.responder = responder
        self.requests: list[dict] = []
        self.polls: list[str] = []
        self.canceled: list[str] = []
        self.pending: dict[str, list] = {}
        self.chunks: dict[tuple[str, int], ResultData] = {}

    def execute_statement(self, **kwargs):
        self.requests.append(kwargs)
        out = self.responder(kwargs["statement"], kwargs)
        if isinstance(out, list):
            self.pending[out[0].statement_id] = list(out[1:])
            return out[0]
        return out

    def get_statement(self, statement_id):
        self.polls.append(statement_id)
        queue = self.pending[statement_id]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def get_statement_result_chunk_n(self, statement_id, chunk_index):
        return self.chunks[(statement_id, chunk_index)]

    def cancel_execution(self, statement_id):
        self.canceled.append(statement_id)


class FakeWorkspaceClient:
    def __init__(self, responder=None):
        self.statement_execution = FakeStatementAPI(responder or (lambda sql, kw: ok()))

    @property
    def statements(self):
        return [r["statement"] for r in self.statement_execution.requests]

    @property
    def requests(self):
        return self.statement_execution.requests


@pytest.fixture()
def db_settings(tmp_path) -> Settings:
    return Settings.from_env(backend="databricks", catalog="main", warehouse_id="wh-123",
                             local_dir=tmp_path / ".kpi_evidence")


def warehouse_store(settings, responder=None, **kwargs):
    client = FakeWorkspaceClient(responder)
    sleeps: list[float] = []
    store = DatabricksStore(settings, client=client, sleep=sleeps.append, **kwargs)
    store.sleeps = sleeps
    return store, client


@pytest.fixture()
def spark_types(monkeypatch):
    try:
        from pyspark.sql import types as T

        return T
    except ImportError:
        T = types.ModuleType("pyspark.sql.types")

        @dataclasses.dataclass(frozen=True)
        class _Type:
            pass

        for name in ("StringType", "DoubleType", "LongType", "BooleanType", "DateType", "TimestampType"):
            setattr(T, name, type(name, (_Type,), {}))

        @dataclasses.dataclass
        class StructField:
            name: str
            dataType: object
            nullable: bool = True

        @dataclasses.dataclass
        class StructType:
            fields: list

        T.StructField, T.StructType = StructField, StructType
        pyspark, sql = types.ModuleType("pyspark"), types.ModuleType("pyspark.sql")
        sql.types, pyspark.sql = T, sql
        monkeypatch.setitem(sys.modules, "pyspark", pyspark)
        monkeypatch.setitem(sys.modules, "pyspark.sql", sql)
        monkeypatch.setitem(sys.modules, "pyspark.sql.types", T)
        return T


def document(i=0, **overrides) -> Document:
    base = Document(doc_id=f"doc{i}", file_name=f"Deck {TRICKY}.pptx", file_path=f"/Volumes/main/kpi_evidence/reports/inbox/d{i}",
                    size_bytes=1024 + i, format=DocumentFormat.PPTX, status=DocumentStatus.READY, registered_at=T0,
                    updated_at=T0, period_end=date(2026, 5, 31), metadata_proposals={"brand": {"value": "KESTREL", "c": 0.9}},
                    needs_confirmation=["period"])
    return dataclasses.replace(base, **overrides)


def decision(i=0) -> ReviewDecision:
    return ReviewDecision(decision_id=f"dec{i}", result_id="r1", result_version=2, action=ReviewAction.ACCEPT,
                          rationale=TRICKY, decided_by="a@x.com", decision_source=DecisionSource.HUMAN,
                          decided_at=T0, evidence_hash="e", corrected_value=None)


def response_row(record) -> list:
    out = []
    for name, kind, _n, _e in columns(type(record)):
        value = to_row(record)[name]
        if value is None:
            out.append(None)
        elif kind == "TIMESTAMP":
            out.append(value.isoformat() + "Z")
        elif kind == "BOOLEAN":
            out.append("true" if value else "false")
        else:
            out.append(str(value))
    return out


WAREHOUSE_TYPES = {"STRING": "STRING", "JSON": "STRING", "DOUBLE": "DOUBLE", "BIGINT": "LONG",
                   "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP"}


def table_columns(cls):
    return [(n, WAREHOUSE_TYPES[k]) for n, k, _nl, _e in columns(cls)]


def test_warehouse_mode_needs_a_warehouse(db_settings):
    with pytest.raises(ValueError, match="warehouse_id"):
        DatabricksStore(dataclasses.replace(db_settings, warehouse_id=None), client=FakeWorkspaceClient())
    assert DatabricksStore(dataclasses.replace(db_settings, warehouse_id=None), spark=FakeSpark()).mode == "spark"


def test_local_settings_are_rejected(settings):
    with pytest.raises(ValueError, match="databricks"):
        DatabricksStore(settings, spark=FakeSpark())


def test_from_runtime_falls_back_to_the_warehouse(db_settings, monkeypatch):
    monkeypatch.setattr(dbs, "active_spark", lambda: None)
    assert DatabricksStore.from_runtime(db_settings, client=FakeWorkspaceClient()).mode == "warehouse"
    spark = FakeSpark()
    monkeypatch.setattr(dbs, "active_spark", lambda: spark)
    assert DatabricksStore.from_runtime(db_settings).spark is spark
    forced = dataclasses.replace(db_settings, extra={"store_mode": "warehouse"})
    assert DatabricksStore.from_runtime(forced, client=FakeWorkspaceClient()).mode == "warehouse"


def test_module_does_not_import_pyspark():
    import importlib

    import kpi_evidence.store.databricks_store as module

    source = importlib.util.find_spec(module.__name__).origin
    text = open(source, encoding="utf-8").read()
    top_level = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
    assert not any("pyspark" in line for line in top_level)


def test_query_sends_typed_parameters_and_polls(db_settings):
    responses = [running("q1"), running("q1"), ok([("value", "DOUBLE")], [["1.5"]], sid="q1")]
    store, client = warehouse_store(db_settings, lambda sql, kw: responses)
    df = store.query(
        "SELECT :i AS i, :f AS f, :b AS b, :d AS d, :t AS t, :s AS s, :e AS e, :n AS n WHERE 'x:y' = 'x:y'",
        {"i": 3, "f": 0.1, "b": True, "d": date(2026, 5, 31), "t": T0, "s": TRICKY,
         "e": DocumentStatus.READY, "n": None, "unused": 1})
    assert df["value"].tolist() == [1.5]
    req = client.requests[0]
    assert req["warehouse_id"] == "wh-123" and req["wait_timeout"] == "30s"
    assert req["disposition"] == Disposition.INLINE and req["format"] == Format.JSON_ARRAY
    assert req["on_wait_timeout"] == ExecuteStatementRequestOnWaitTimeout.CONTINUE
    params = {p.name: (p.value, p.type) for p in req["parameters"]}
    assert params == {
        "i": ("3", "BIGINT"), "f": ("0.1", "DOUBLE"), "b": ("true", "BOOLEAN"), "d": ("2026-05-31", "DATE"),
        "t": ("2026-06-10T09:30:15.250000Z", "TIMESTAMP"), "s": (TRICKY, "STRING"), "e": ("READY", "STRING"),
        "n": (None, None)}
    assert client.statement_execution.polls == ["q1", "q1"]
    assert store.sleeps == [0.5, 0.75]


def test_query_without_params_sends_none(db_settings):
    store, client = warehouse_store(db_settings)
    store.query("SELECT 1")
    assert "parameters" not in client.requests[0]


def test_result_rows_are_typed_from_the_manifest(db_settings):
    cols = [("s", "STRING"), ("i", "LONG"), ("n", "INT"), ("f", "DOUBLE"), ("dec", "DECIMAL"), ("b", "BOOLEAN"),
            ("d", "DATE"), ("t", "TIMESTAMP"), ("arr", "ARRAY"), ("nul", "NULL")]
    rows = [["a", "9007199254740993", "1", "1.25", "10.50", "true", "2026-05-31", "2026-06-10T09:30:15.250Z",
             '[1, "x"]', None],
            [None, None, None, None, None, None, None, None, None, None]]
    store, _ = warehouse_store(db_settings, lambda sql, kw: ok(cols, rows))
    df = store.query("SELECT ...")
    assert list(df.columns) == [c[0] for c in cols]
    assert str(df["i"].dtype) == "Int64" and df["i"].iloc[0] == 9007199254740993
    assert df["f"].dtype == "float64" and df["dec"].iloc[0] == 10.5
    assert str(df["b"].dtype) == "boolean" and bool(df["b"].iloc[0]) is True
    assert pd.api.types.is_datetime64_any_dtype(df["d"]) and df["d"].iloc[0] == pd.Timestamp("2026-05-31")
    assert df["t"].iloc[0] == pd.Timestamp("2026-06-10 09:30:15.250") and df["t"].dt.tz is None
    assert df["arr"].iloc[0] == [1, "x"]
    records = df.to_dict(orient="records")
    assert pd.isna(records[1]["i"]) and pd.isna(records[1]["t"]) and records[1]["s"] is None


def test_result_chunks_are_followed(db_settings):
    store, client = warehouse_store(db_settings, lambda sql, kw: ok([("v", "LONG")], [["1"], ["2"]], sid="c1", next_chunk=1))
    client.statement_execution.chunks[("c1", 1)] = ResultData(data_array=[["3"]], next_chunk_index=2)
    client.statement_execution.chunks[("c1", 2)] = ResultData(data_array=[["4"]])
    assert store.query("SELECT v")["v"].tolist() == [1, 2, 3, 4]


def test_truncated_inline_results_are_an_error(db_settings):
    store, _ = warehouse_store(db_settings, lambda sql, kw: ok([("v", "LONG")], [["1"]], truncated=True))
    with pytest.raises(StatementError, match="truncated"):
        store.query("SELECT v")


@pytest.mark.parametrize("cls", ALL_TABLES, ids=[c.TABLE for c in ALL_TABLES])
def test_read_round_trips_records_from_json_array_rows(db_settings, cls):
    from test_store_duckdb import make_record

    rec = make_record(cls)
    store, client = warehouse_store(db_settings, lambda sql, kw: ok(table_columns(cls), [response_row(rec)]))
    key = {k: getattr(rec, k) for k in cls.KEY}
    assert store.get(cls, **key) == rec
    assert client.statements[0].startswith(f"SELECT * FROM main.kpi_evidence.{cls.TABLE} WHERE ")


def test_failed_statements_keep_the_server_message(db_settings):
    message = "[TABLE_OR_VIEW_NOT_FOUND] The table or view `main`.`kpi_gov`.`gv_x` cannot be found."
    store, _ = warehouse_store(db_settings, lambda sql, kw: failed(message))
    with pytest.raises(StatementError) as info:
        store.query("SELECT 1")
    assert str(info.value) == message
    assert info.value.getCondition() == "TABLE_OR_VIEW_NOT_FOUND" and info.value.sql_state == "42P01"
    assert info.value.error_code == "BAD_REQUEST" and info.value.statement_id == "s1"


def test_permission_failures_are_permission_errors(db_settings):
    message = "PERMISSION_DENIED: User does not have SELECT on Table 'main.platform_restricted.exfactory_monthly'."
    store, _ = warehouse_store(db_settings, lambda sql, kw: failed(message))
    with pytest.raises(StatementAccessDenied) as info:
        store.runner().run("SELECT SUM(units) AS value FROM main.platform_restricted.exfactory_monthly")
    assert isinstance(info.value, AccessDenied) and isinstance(info.value, PermissionError)
    assert "PERMISSION_DENIED" in str(info.value)


def test_slow_statements_time_out_and_are_canceled(db_settings):
    store, client = warehouse_store(db_settings, lambda sql, kw: [running("slow")], timeout_s=0)
    with pytest.raises(TimeoutError, match="slow"):
        store.query("SELECT 1")
    assert client.statement_execution.canceled == ["slow"]


def test_array_parameters_are_rejected(db_settings):
    store, _ = warehouse_store(db_settings)
    with pytest.raises(TypeError, match="ARRAY"):
        store.query("SELECT * FROM t WHERE id IN (:ids)", {"ids": ["a", "b"]})


def test_runner_contract_in_warehouse_mode(db_settings):
    answers = {"one": ok([("value", "DOUBLE")], [["5783.041"]]),
               "null": ok([("value", "DOUBLE")], [[None]]),
               "two": ok([("value", "DOUBLE")], [["1"], ["2"]]),
               "name": ok([("total", "DOUBLE")], [["1"]])}
    store, client = warehouse_store(db_settings, lambda sql, kw: answers[sql])
    runner = store.runner()
    assert runner.run("one") == 5783.041
    assert client.requests[0]["row_limit"] == 2
    for sql, message in (("null", "NULL"), ("two", "one row"), ("name", "'value'")):
        with pytest.raises(MetricQueryError, match=message):
            runner.run(sql)
    assert runner.calls == 4


def test_insert_new_is_an_insert_only_merge_of_cast_markers(db_settings):
    store, client = warehouse_store(db_settings, lambda sql, kw: merge_metrics(1))
    assert store.insert_new([document()]) == 1
    (sql,) = client.statements
    names = [c[0] for c in columns(Document)]
    assert sql.startswith("MERGE INTO main.kpi_evidence.documents AS t USING (SELECT * FROM VALUES (")
    assert f"AS v({', '.join(names)})) AS s ON t.doc_id = s.doc_id WHEN NOT MATCHED THEN INSERT" in sql
    assert "WHEN MATCHED" not in sql
    assert "CAST(:p0_0 AS STRING)" in sql and "CAST(:p0_3 AS BIGINT)" in sql and "CAST(:p0_6 AS TIMESTAMP)" in sql
    params = {p.name: p.value for p in client.requests[0]["parameters"]}
    assert len(params) == len(names) <= dbs.MAX_MARKERS
    assert all(p.type is None for p in client.requests[0]["parameters"]), "markers are STRING; the SQL casts"
    by_col = {names[int(k.split("_")[1])]: v for k, v in params.items()}
    assert by_col["file_name"] == document().file_name
    assert by_col["format"] == "PPTX" and by_col["size_bytes"] == "1024"
    assert by_col["registered_at"] == "2026-06-10T09:30:15.250000Z"
    assert by_col["period_end"] == "2026-05-31" and by_col["brand"] is None
    assert by_col["metadata_proposals"] == to_row(document())["metadata_proposals"]


def test_upsert_updates_changed_non_key_columns(db_settings):
    sqlglot = pytest.importorskip("sqlglot")
    store, client = warehouse_store(db_settings, lambda sql, kw: merge_metrics(2, inserted=1))
    assert store.upsert([document(0), document(1)]) == 2
    (sql,) = client.statements
    assert "WHEN MATCHED AND (t.file_name IS DISTINCT FROM s.file_name OR " in sql
    assert "THEN UPDATE SET file_name = s.file_name, " in sql and "doc_id = s.doc_id," not in sql
    assert "CAST(:p1_0 AS STRING)" in sql
    assert sqlglot.parse_one(sql, read="databricks").key == "merge"


def test_writes_are_batched_under_the_marker_limit(db_settings):
    store, client = warehouse_store(db_settings, lambda sql, kw: merge_metrics(1))
    per = dbs.MAX_MARKERS // len(columns(Document))
    docs = [document(i) for i in range(per * 2 + 1)]
    assert store.insert_new(docs) == 3
    assert len(client.statements) == 3
    assert all(len(r["parameters"]) <= dbs.MAX_MARKERS for r in client.requests)
    assert [len(r["parameters"]) // len(columns(Document)) for r in client.requests] == [per, per, 1]


def test_counts_fall_back_to_rows_sent_without_metrics(db_settings):
    store, _ = warehouse_store(db_settings)
    assert store.insert_new([document(0), document(1)]) == 2


def test_replace_deletes_then_inserts(db_settings):
    sqlglot = pytest.importorskip("sqlglot")
    store, client = warehouse_store(db_settings, lambda sql, kw: ok([("num_affected_rows", "LONG"),
                                                                    ("num_inserted_rows", "LONG")], [["1", "1"]]))
    value = ExtractedValue(value_id="v1", doc_id="doc0", method=ExtractionMethod.NATIVE_CHART, extractor="x",
                           extractor_version="1", locator={"slide": 1}, slide_or_page=1, context={}, raw_text="1",
                           raw_number=1.0, number_format=None, flags=[], confidence=1.0, created_at=T0)
    assert store.replace(ExtractedValue, {"doc_id": "doc0"}, [value]) == 1
    delete, insert = client.statements
    assert delete == "DELETE FROM main.kpi_evidence.extracted_values WHERE doc_id = :doc_id"
    assert [(p.name, p.value, p.type) for p in client.requests[0]["parameters"]] == [("doc_id", "doc0", "STRING")]
    assert insert.startswith("INSERT INTO main.kpi_evidence.extracted_values (value_id, doc_id, ")
    assert ") SELECT value_id, doc_id, " in insert and insert.endswith(") AS s")
    assert sqlglot.parse_one(insert, read="databricks").key == "insert"
    client.statement_execution.requests.clear()
    assert store.replace(ExtractedValue, {"doc_id": "doc0"}, []) == 0
    assert client.statements == [delete]


def test_blind_insert(db_settings):
    store, client = warehouse_store(db_settings)
    store.insert([decision()])
    assert client.statements[0].startswith("INSERT INTO main.kpi_evidence.review_decisions (decision_id, ")


def _publication() -> Publication:
    from test_store_views import publication

    return publication("pub1")


def atomic(settings: Settings) -> Settings:
    return dataclasses.replace(settings, extra={**settings.extra, "atomic": True})


def test_transaction_sends_one_begin_atomic_block_with_inlined_literals(db_settings):
    sqlglot = pytest.importorskip("sqlglot")
    store, client = warehouse_store(atomic(db_settings))
    with store.transaction():
        store.insert_new([decision()])
        store.execute("UPDATE main.kpi_evidence.published_kpis SET superseded_by = :new WHERE publication_id = :old "
                      "AND superseded_by IS NULL", {"new": "pub1", "old": "pub0", "unused": 1})
        store.insert_new([_publication()])
        assert client.statements == [], "nothing is sent before the block ends"
    (sql,) = client.statements
    assert "parameters" not in client.requests[0]
    assert sql.startswith("BEGIN ATOMIC\n  MERGE INTO main.kpi_evidence.review_decisions AS t USING (SELECT * FROM VALUES (")
    assert sql.endswith(";\nEND;")
    assert "SET superseded_by = 'pub1' WHERE publication_id = 'pub0'" in sql
    assert r"CAST('it\'s \\ fine; -- :not_a_marker' AS STRING)" in sql, "Spark-style escaping, not doubled quotes"
    assert "CAST('2026-06-10T09:30:15.250000Z' AS TIMESTAMP)" in sql and "CAST(NULL AS DOUBLE)" in sql
    assert marker_names(sql, backslash_escapes=True) == []
    inner = sql[len("BEGIN ATOMIC\n"):-len("END;")]
    parts = [p.strip() for p in re.split(r";\n", inner) if p.strip()]
    assert len(parts) == 3
    assert [sqlglot.parse_one(p, read="databricks").key for p in parts] == ["merge", "update", "merge"]


def test_failed_transaction_sends_nothing(db_settings):
    store, client = warehouse_store(atomic(db_settings))
    with pytest.raises(RuntimeError, match="boom"):
        with store.transaction():
            store.insert_new([decision()])
            raise RuntimeError("boom")
    assert client.statements == []
    store.insert_new([decision()])
    assert len(client.statements) == 1


def test_nested_and_empty_transactions(db_settings):
    store, client = warehouse_store(atomic(db_settings))
    with store.transaction():
        pass
    assert client.statements == []
    with store.transaction():
        store.insert_new([decision(0)])
        with store.transaction():
            store.insert_new([decision(1)])
    assert len(client.statements) == 1 and client.statements[0].count("MERGE INTO") == 2


def test_transaction_replace_is_inlined(db_settings):
    store, client = warehouse_store(atomic(db_settings))
    with store.transaction():
        store.replace(ReviewDecision, {"result_id": "r'1", "result_version": 2}, [decision()])
    sql = client.statements[0]
    assert "DELETE FROM main.kpi_evidence.review_decisions WHERE result_id = 'r\\'1' AND result_version = 2;" in sql


def test_reading_a_table_written_in_the_transaction_raises(db_settings):
    store, client = warehouse_store(atomic(db_settings), lambda sql, kw: ok([("n", "LONG")], [["0"]]))
    with pytest.raises(TransactionReadAfterWrite, match="published_kpis"):
        with store.transaction():
            store.count(ReviewDecision)
            store.insert_new([_publication()])
            store.query("SELECT * FROM main.kpi_evidence.published_kpis_current")
    assert client.statements == ["SELECT COUNT(*) AS n FROM main.kpi_evidence.review_decisions"]


def test_spark_query_passes_named_args(db_settings):
    spark = FakeSpark(lambda sql, args: FakeDF(["value"], [(2.0,)]))
    store = DatabricksStore(db_settings, spark=spark)
    df = store.query("SELECT :a AS value WHERE s = :s AND 'x:y' = 'x:y'", {"a": 1, "s": DocumentStatus.READY, "z": 0})
    assert df["value"].tolist() == [2.0]
    assert spark.calls == [("SELECT :a AS value WHERE s = :s AND 'x:y' = 'x:y'", {"a": 1, "s": "READY"})]
    store.query("SELECT 1 AS value")
    assert spark.calls[-1] == ("SELECT 1 AS value", None)


def test_spark_write_uses_an_explicit_schema_and_a_temp_view(db_settings, spark_types):
    T = spark_types
    spark = FakeSpark(lambda sql, args: FakeDF(["num_affected_rows", "num_updated_rows", "num_deleted_rows",
                                                 "num_inserted_rows"], [(1, 0, 0, 1)]) if sql.startswith("MERGE") else FakeDF())
    store = DatabricksStore(db_settings, spark=spark)
    assert store.insert_new([document(0, brand=None, size_bytes=7)]) == 1
    (created,) = spark.created
    fields = {f.name: f for f in created.schema.fields}
    assert [f.name for f in created.schema.fields] == [c[0] for c in columns(Document)]
    assert fields["size_bytes"].dataType == T.LongType() and not fields["size_bytes"].nullable
    assert fields["brand"].dataType == T.StringType() and fields["brand"].nullable
    assert fields["registered_at"].dataType == T.TimestampType()
    assert fields["period_end"].dataType == T.DateType()
    frame = created.data
    assert frame["size_bytes"].tolist() == [7] and frame["size_bytes"].dtype == object
    assert frame["brand"].tolist() == [None]
    assert frame["registered_at"].iloc[0] == pd.Timestamp(T0, tz="UTC")
    assert frame["period_end"].tolist() == [date(2026, 5, 31)]
    (view,) = spark.views
    merge = spark.statements[-1]
    assert merge.startswith(f"MERGE INTO main.kpi_evidence.documents AS t USING {view} AS s ON t.doc_id = s.doc_id")
    assert spark.dropped == [view]


def test_spark_write_drops_the_view_when_the_merge_fails(db_settings, spark_types):
    def responder(sql, args):
        if sql.startswith("MERGE"):
            raise RuntimeError("[DELTA_CONCURRENT_APPEND] ConcurrentAppendException")
        return FakeDF()

    spark = FakeSpark(responder)
    store = DatabricksStore(db_settings, spark=spark)
    with pytest.raises(RuntimeError, match="DELTA_CONCURRENT_APPEND"):
        store.upsert([document()])
    assert spark.dropped == list(spark.views)


def test_spark_transaction_is_atomic_when_enabled(db_settings, spark_types):
    spark = FakeSpark()
    store = DatabricksStore(atomic(db_settings), spark=spark)
    with store.transaction():
        store.insert_new([decision()])
        store.insert_new([_publication()])
    assert len(spark.calls) == 1 and spark.statements[0].startswith("BEGIN ATOMIC")
    assert spark.created == [], "inlined literals, no temp views inside the block"


def test_spark_transaction_can_run_sequentially(db_settings, spark_types):
    spark = FakeSpark()
    store = DatabricksStore(dataclasses.replace(db_settings, extra={"atomic": False}), spark=spark)
    with store.transaction():
        store.insert_new([decision()])
        assert len(spark.calls) == 1, "statements run immediately"
    assert not any(s.startswith("BEGIN ATOMIC") for s in spark.statements)


def test_transactions_run_sequentially_by_default(db_settings, spark_types):
    spark = FakeSpark()
    store = DatabricksStore(db_settings, spark=spark)
    assert store.atomic is False
    with store.transaction():
        store.insert_new([decision()])
        assert len(spark.calls) == 1, "statements run immediately"
        store.upsert([document()])
    assert len(spark.calls) == 2
    assert not any(s.startswith("BEGIN ATOMIC") for s in spark.statements)
    w_store, client = warehouse_store(db_settings)
    with w_store.transaction():
        w_store.insert_new([decision()])
        assert len(client.statements) == 1
    assert not any(s.startswith("BEGIN ATOMIC") for s in client.statements)


def test_stage_writes_never_send_begin_atomic_by_default(db_settings, spark_types):
    from kpi_evidence import intake, reconcile
    from kpi_evidence.contracts import ReconRun, ReconTrial

    spark = FakeSpark()
    store = DatabricksStore(db_settings, spark=spark)
    intake._write_family_changes(store, document(0), [])
    run = ReconRun(**_filled(ReconRun))
    trial = ReconTrial(**{**_filled(ReconTrial), "recon_run_id": run.recon_run_id})
    reconcile._write_run(store, run, [trial])
    written = " ".join(spark.statements)
    assert "main.kpi_evidence.documents" in written and "main.kpi_evidence.recon_runs" in written
    assert not any("BEGIN ATOMIC" in s for s in spark.statements)


def test_atomic_block_with_a_plain_table_runs_statements_one_by_one(db_settings, spark_types):
    store, client = warehouse_store(atomic(db_settings))
    with store.transaction():
        store.insert_new([decision()])
        store.upsert([document()])
        store.execute("UPDATE main.kpi_evidence.kpi_results SET is_current = FALSE WHERE doc_id = :d", {"d": "doc0"})
        assert client.statements == [], "buffered until the block ends"
    assert len(client.statements) == 3
    assert not any(s.startswith("BEGIN ATOMIC") for s in client.statements)
    assert client.statements[0].startswith("MERGE INTO main.kpi_evidence.review_decisions")
    assert client.statements[1].startswith("MERGE INTO main.kpi_evidence.documents")
    assert "parameters" not in client.requests[2] and "doc_id = 'doc0'" in client.statements[2]


def test_atomic_block_needs_every_table_catalog_managed(db_settings):
    store, client = warehouse_store(atomic(db_settings))
    assert store.catalog_managed_tables() == {
        "main.kpi_evidence.review_decisions", "main.kpi_evidence.kpi_results", "main.kpi_evidence.published_kpis",
        "main.kpi_evidence.review_events"}
    merge = ("MERGE INTO main.kpi_evidence.kpi_results AS t USING (SELECT 'r1' AS result_id) AS s "
             "ON t.result_id = s.result_id WHEN MATCHED THEN UPDATE SET t.is_current = FALSE")
    with store.transaction():
        store.insert_new([decision()])
        store.execute(merge)
    (sql,) = client.statements
    assert sql.startswith("BEGIN ATOMIC")
    with store.transaction():
        store.execute("OPTIMIZE main.kpi_evidence.review_decisions")
    assert client.statements[-1] == "OPTIMIZE main.kpi_evidence.review_decisions", "unknown targets are never atomic"


def _filled(cls) -> dict:
    out = {}
    for name, kind, nullable, enum_type in columns(cls):
        if enum_type is not None:
            out[name] = next(iter(enum_type))
        elif kind == "TIMESTAMP":
            out[name] = T0
        elif kind == "DATE":
            out[name] = date(2026, 5, 31)
        elif kind == "JSON":
            out[name] = {}
        elif kind == "BIGINT":
            out[name] = 1
        elif kind == "DOUBLE":
            out[name] = 1.0
        elif kind == "BOOLEAN":
            out[name] = False
        else:
            out[name] = f"{cls.TABLE}-{name}"
    return out


def test_spark_runner_keeps_original_exceptions(db_settings):
    class AnalysisException(Exception):
        def getCondition(self):
            return "TABLE_OR_VIEW_NOT_FOUND"

    def responder(sql, args):
        if "missing" in sql:
            raise AnalysisException("[TABLE_OR_VIEW_NOT_FOUND] The table or view `missing` cannot be found.")
        return FakeDF(["value"], [(Decimal("17220.318"),)])

    runner = DatabricksStore(db_settings, spark=FakeSpark(responder)).runner()
    assert runner.run("SELECT 1 AS value") == 17220.318
    with pytest.raises(AnalysisException) as info:
        runner.run("SELECT x AS value FROM missing")
    assert info.value.getCondition() == "TABLE_OR_VIEW_NOT_FOUND"
    with pytest.raises(MetricQueryError):
        DatabricksStore(db_settings, spark=FakeSpark(lambda s, a: FakeDF(["value"], [(1,), (2,)]))).runner().run("x")


def test_spark_write_dataframe_saves_a_table(db_settings):
    spark = FakeSpark()
    store = DatabricksStore(db_settings, spark=spark)
    frame = pd.DataFrame({"month": ["2026-05"], "trx_units": [1.5]})
    assert store.write_dataframe("main.platform_demo.rx_account_monthly", frame) == 1
    assert spark.statements == ["CREATE SCHEMA IF NOT EXISTS main.platform_demo"]
    ((table, mode, options, data),) = spark.saved
    assert (table, mode, options) == ("main.platform_demo.rx_account_monthly", "overwrite", {"overwriteSchema": "true"})
    assert data is frame


def test_warehouse_write_dataframe_inlines_escaped_literals_in_batches(db_settings, monkeypatch):
    sqlglot = pytest.importorskip("sqlglot")
    store, client = warehouse_store(db_settings)
    monkeypatch.setattr(dbs, "LITERAL_BATCH_CHARS", 400)
    frame = pd.DataFrame({
        "name": [TRICKY, "b", None, "d"], "n": [1, 2, 3, 4], "x": [1.5, float("nan"), 2.5, float("inf")],
        "flag": [True, False, True, False], "day": [date(2026, 5, 31)] * 4,
        "ts": pd.to_datetime(["2026-06-01 10:00:00"] * 4)})
    assert store.write_dataframe("main.platform_demo.t", frame) == 4
    statements = client.statements
    assert statements[0] == "CREATE SCHEMA IF NOT EXISTS main.platform_demo"
    assert statements[1].startswith("CREATE OR REPLACE TABLE main.platform_demo.t AS SELECT * FROM (SELECT * FROM VALUES ")
    assert len(statements) > 3 and all(s.startswith("INSERT INTO main.platform_demo.t SELECT") for s in statements[2:])
    body = " ".join(statements[1:])
    assert r"CAST('it\'s \\ fine; -- :not_a_marker' AS STRING)" in body
    assert "CAST(1 AS BIGINT)" in body and "CAST(1.5 AS DOUBLE)" in body and "CAST(NULL AS DOUBLE)" in body
    assert "CAST(TRUE AS BOOLEAN)" in body and "CAST('2026-05-31' AS DATE)" in body
    assert "CAST('2026-06-01T10:00:00Z' AS TIMESTAMP)" in body
    assert "AS v(name, n, x, flag, day, ts)" in statements[1]
    assert all("parameters" not in r for r in client.requests)
    for s in statements[1:]:
        sqlglot.parse_one(s, read="databricks")


def test_warehouse_write_dataframe_empty_frame_creates_typed_table(db_settings):
    store, client = warehouse_store(db_settings)
    frame = pd.DataFrame({"a": pd.Series([], dtype="int64"), "b": pd.Series([], dtype=object)})
    assert store.write_dataframe("main.platform_demo.empty", frame) == 0
    assert client.statements[-1] == "CREATE OR REPLACE TABLE main.platform_demo.empty (a BIGINT, b STRING)"
    assert store.write_dataframe("main.platform_demo.empty", frame, mode="append") == 0
    with pytest.raises(ValueError):
        store.write_dataframe("main.platform_demo.x; DROP", frame)


def test_ensure_schema_creates_everything_and_only_changes_what_differs(db_settings):
    documents_props = [[f"delta.constraints.{n}", f"({expr})"] for n, expr in check_constraints(Document)]

    def responder(sql, kw):
        if "information_schema.columns" in sql:
            rows = [[cls.TABLE, c[0]] for cls in ALL_TABLES for c in columns(cls)
                    if not (cls.TABLE == "reviewers" and c[0] == "updated_by")]
            return ok([("table_name", "STRING"), ("column_name", "STRING")], rows)
        if sql == "SHOW TBLPROPERTIES main.kpi_evidence.documents":
            return ok([("key", "STRING"), ("value", "STRING")], documents_props)
        if sql.startswith("SHOW TBLPROPERTIES main.kpi_evidence.kpi_results"):
            return failed("[TABLE_OR_VIEW_NOT_FOUND] whatever")
        if sql.startswith("SHOW TBLPROPERTIES"):
            return ok([("key", "STRING"), ("value", "STRING")], [["delta.enableDeletionVectors", "true"]])
        return ok()

    store, client = warehouse_store(db_settings, responder)
    store.ensure_schema()
    sql = client.statements
    assert sql[:2] == ["CREATE SCHEMA IF NOT EXISTS main.kpi_evidence", "CREATE SCHEMA IF NOT EXISTS main.kpi_gov"]
    creates = [s for s in sql if s.startswith("CREATE TABLE IF NOT EXISTS")]
    assert len(creates) == len(ALL_TABLES)
    assert any("main.kpi_evidence.published_kpis" in s and "'delta.feature.catalogManaged' = 'supported'" in s
               for s in creates)
    assert "ALTER TABLE main.kpi_evidence.reviewers ADD COLUMNS (updated_by STRING)" in sql
    assert client.requests[len(ALL_TABLES) + 2]["parameters"][0].value == "kpi_evidence"
    alters = [s for s in sql if s.startswith("ALTER TABLE") and "CONSTRAINT" in s]
    assert not any("main.kpi_evidence.documents " in s for s in alters), "unchanged constraints are left alone"
    assert "ALTER TABLE main.kpi_evidence.kpi_results ADD CONSTRAINT kpi_results_validation_status_valid CHECK " \
           "(validation_status IS NULL OR validation_status IN (" in " ".join(alters)
    assert any(s.startswith("ALTER TABLE main.kpi_evidence.kpi_results DROP CONSTRAINT IF EXISTS ") for s in alters)
    views = [s for s in sql if s.startswith("CREATE OR REPLACE VIEW")]
    assert [v.split()[4] for v in views] == [f"main.kpi_evidence.{v}" for v in
                                             ("published_kpis_current", "kpi_results_current", "review_open",
                                              "report_captures")]
    assert not any("VOLUME" in s for s in sql)


def test_ensure_schema_can_create_the_volume(db_settings):
    store, client = warehouse_store(dataclasses.replace(db_settings, extra={"create_volume": True}))
    store.ensure_schema()
    assert "CREATE VOLUME IF NOT EXISTS main.kpi_evidence.reports" in client.statements
    custom = dataclasses.replace(db_settings, volume_path="/Volumes/cat/sch/vol")
    assert DatabricksStore(custom, client=FakeWorkspaceClient()).volume_name() == "cat.sch.vol"


@pytest.mark.parametrize("value, expected", [
    (None, "NULL"), (float("nan"), "NULL"), (True, "TRUE"), (False, "FALSE"), (3, "3"), (2.5, "2.5"),
    ("plain", "'plain'"), ("it's", r"'it\'s'"), ("a\\b", r"'a\\b'"), ("x\\'; DROP", r"'x\\\'; DROP'"),
    ("line\nbreak", "'line\nbreak'"), (DocumentStatus.READY, "'READY'"),
    (date(2026, 5, 31), "DATE '2026-05-31'"), (T0, "TIMESTAMP '2026-06-10T09:30:15.250000Z'"),
    (datetime(2026, 6, 10, 11, 0, tzinfo=timezone.utc), "TIMESTAMP '2026-06-10T11:00:00Z'"),
    (Decimal("10.50"), "CAST('10.50' AS DECIMAL(38, 2))"),
])
def test_databricks_literal(value, expected):
    assert databricks_literal(value) == expected


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), ["a"], {"a": 1}])
def test_databricks_literal_rejects_unsafe_values(value):
    with pytest.raises((ValueError, TypeError)):
        databricks_literal(value)


def test_databricks_literal_round_trips_through_a_spark_parser():
    sqlglot = pytest.importorskip("sqlglot")
    for text in ("it's", "a\\b", "x\\'; DROP TABLE t; --", "''", "\\"):
        parsed = sqlglot.parse_one(f"SELECT {databricks_literal(text)} AS x", read="databricks")
        assert len(parsed.expressions) == 1, text


def test_inline_params_skips_spark_string_literals():
    sql = r"SELECT 'it\'s :x' AS a, :y AS b, `col:z`, :y -- :w"
    assert inline_params(sql, {"y": "v'1"}) == r"SELECT 'it\'s :x' AS a, 'v\'1' AS b, `col:z`, 'v\'1' -- :w"
    with pytest.raises(KeyError):
        inline_params("SELECT :missing", {})
