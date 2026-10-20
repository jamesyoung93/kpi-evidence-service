from __future__ import annotations

import dataclasses
import math
import threading
import typing
from datetime import date, datetime, timedelta

import duckdb
import pytest

from kpi_evidence.contracts import (
    ALL_TABLES,
    Document,
    DocumentFormat,
    DocumentStatus,
    ExtractedValue,
    ExtractionMethod,
    KpiResult,
    Observation,
    Reviewer,
    columns,
)
from kpi_evidence.store.base import AccessDenied, MetricQueryError
from kpi_evidence.store.duckdb_store import DuckDBStore, to_duckdb_params

TRICKY = "it's \\ a :colon -- not a comment"


def _base_annotation(hint):
    args = typing.get_args(hint)
    if type(None) in args:
        return next(a for a in args if a is not type(None))
    return hint


def field_value(cls, name: str, variant: int = 0):
    hint = _base_annotation(typing.get_type_hints(cls)[name])
    kind = next(c for c in columns(cls) if c[0] == name)
    _n, sql_kind, _nullable, enum_type = kind
    if enum_type is not None:
        members = list(enum_type)
        return members[variant % len(members)]
    if sql_kind == "JSON":
        origin = typing.get_origin(hint) or hint
        if origin is list:
            return [{"code": "FLAG", "detail": TRICKY, "n": variant}, 1, 2.5, None]
        return {"k": TRICKY, "n": [1, 2.5 + variant, None], "nested": {"ok": True}}
    if sql_kind == "STRING":
        return f"{name}-{variant} {TRICKY}"
    if sql_kind == "DOUBLE":
        return 1.25 + variant
    if sql_kind == "BIGINT":
        return 3 + variant
    if sql_kind == "BOOLEAN":
        return variant % 2 == 0
    if sql_kind == "DATE":
        return date(2026, 5, 31) - timedelta(days=variant)
    if sql_kind == "TIMESTAMP":
        return datetime(2026, 6, 1, 12, 30, 15, 123456) + timedelta(hours=variant)
    raise AssertionError(sql_kind)


def make_record(cls, *, key_variant: int = 0, variant: int = 0, fill_optional: bool = True):
    values = {}
    for name, _kind, nullable, _enum in columns(cls):
        if name in cls.KEY:
            values[name] = field_value(cls, name, key_variant)
        elif nullable and not fill_optional:
            values[name] = None
        else:
            values[name] = field_value(cls, name, variant)
    return cls(**values)


def key_of(rec):
    return {k: getattr(rec, k) for k in type(rec).KEY}


def first_non_key(cls) -> str:
    return next(c[0] for c in columns(cls) if c[0] not in cls.KEY)


TABLES = pytest.mark.parametrize("cls", ALL_TABLES, ids=[c.TABLE for c in ALL_TABLES])


@TABLES
def test_round_trip_every_table(mem_store, cls):
    rec = make_record(cls)
    assert mem_store.insert_new([rec]) == 1
    assert mem_store.insert_new([rec]) == 0, "insert_new is idempotent"
    assert mem_store.get(cls, **key_of(rec)) == rec
    assert mem_store.read(cls) == [rec]

    assert mem_store.upsert([rec]) == 0, "an unchanged upsert touches nothing"
    changed = dataclasses.replace(rec, **{first_non_key(cls): field_value(cls, first_non_key(cls), 1)})
    assert changed != rec
    assert mem_store.upsert([changed]) == 1
    assert mem_store.get(cls, **key_of(rec)) == changed

    other = make_record(cls, key_variant=1)
    assert mem_store.upsert([other]) == 1
    assert mem_store.count(cls) == 2

    third = make_record(cls, key_variant=2)
    assert mem_store.insert([third]) == 1
    assert {tuple(key_of(r).values()) for r in mem_store.read(cls)} == {
        tuple(key_of(r).values()) for r in (rec, other, third)}


@TABLES
def test_round_trip_nullable_fields_as_null(mem_store, cls):
    rec = make_record(cls, fill_optional=False)
    assert mem_store.upsert([rec]) == 1
    assert mem_store.get(cls, **key_of(rec)) == rec


@TABLES
def test_enum_and_json_types_come_back_typed(mem_store, cls):
    rec = make_record(cls)
    mem_store.insert_new([rec])
    got = mem_store.get(cls, **key_of(rec))
    for name, kind, _nullable, enum_type in columns(cls):
        value = getattr(got, name)
        if enum_type is not None:
            assert type(value) is enum_type
        elif kind == "JSON":
            assert isinstance(value, (dict, list))
        elif kind == "DATE":
            assert type(value) is date
        elif kind == "TIMESTAMP":
            assert type(value) is datetime


def test_non_finite_doubles_and_json_are_scrubbed(mem_store):
    rec = make_record(KpiResult)
    rec = dataclasses.replace(rec, platform_value=math.inf, abs_diff=-math.inf, rel_diff=math.nan,
                              tolerance={"rel": math.nan, "abs": [math.inf, 1.0]})
    mem_store.upsert([rec])
    got = mem_store.get(KpiResult, result_id=rec.result_id)
    assert got.platform_value is None and got.abs_diff is None and got.rel_diff is None
    assert got.tolerance == {"rel": None, "abs": [None, 1.0]}


def test_upsert_counts_only_new_and_changed_rows(mem_store):
    a, b, c = (make_record(Reviewer, key_variant=i) for i in range(3))
    assert mem_store.upsert([a, b]) == 2
    b2 = dataclasses.replace(b, role="admin")
    assert mem_store.upsert([a, b2, c]) == 2
    assert mem_store.get(Reviewer, **key_of(b)).role == "admin"


def test_duplicate_keys_in_one_write_keep_the_last(mem_store):
    a = make_record(Reviewer)
    a2 = dataclasses.replace(a, role="registry_owner")
    assert mem_store.insert_new([a, a2]) == 1
    assert mem_store.get(Reviewer, **key_of(a)).role == "registry_owner"


@pytest.mark.parametrize("method", ["insert", "insert_new", "upsert"])
def test_not_null_is_enforced(mem_store, method):
    bad = dataclasses.replace(make_record(Document), file_name=None)
    with pytest.raises(duckdb.ConstraintException):
        getattr(mem_store, method)([bad])
    assert mem_store.count(Document) == 0


@pytest.mark.parametrize("method", ["insert", "insert_new", "upsert"])
def test_check_constraint_rejects_unknown_enum_values(mem_store, method):
    bad = dataclasses.replace(make_record(Document), status="NOT_A_STATUS")
    with pytest.raises(duckdb.ConstraintException):
        getattr(mem_store, method)([bad])


def test_values_that_do_not_fit_the_column_type_fail_loudly(mem_store):
    bad = dataclasses.replace(make_record(Document), size_bytes="lots")
    with pytest.raises(TypeError, match="size_bytes"):
        mem_store.upsert([bad])


def _value(doc_id: str, i: int) -> ExtractedValue:
    rec = make_record(ExtractedValue, key_variant=i)
    return dataclasses.replace(rec, value_id=f"{doc_id}-{i}", doc_id=doc_id, method=ExtractionMethod.NATIVE_TABLE)


def test_replace_deletes_matching_rows_then_inserts(mem_store):
    mem_store.insert([_value("A", 1), _value("A", 2), _value("B", 1)])
    assert mem_store.replace(ExtractedValue, {"doc_id": "A"}, [_value("A", 3)]) == 1
    ids = {v.value_id for v in mem_store.read(ExtractedValue)}
    assert ids == {"A-3", "B-1"}


def test_replace_with_no_records_deletes(mem_store):
    mem_store.insert([_value("A", 1), _value("B", 1)])
    assert mem_store.replace(ExtractedValue, {"doc_id": "A"}, []) == 0
    assert [v.value_id for v in mem_store.read(ExtractedValue)] == ["B-1"]


def test_replace_with_multi_column_match(mem_store):
    mem_store.insert([_value("A", 1), _value("B", 1)])
    mem_store.replace(ExtractedValue, {"doc_id": "A", "method": ExtractionMethod.NATIVE_TABLE}, [])
    assert [v.value_id for v in mem_store.read(ExtractedValue)] == ["B-1"]


def test_replace_requires_a_filter(mem_store):
    with pytest.raises(ValueError):
        mem_store.replace(ExtractedValue, {}, [_value("A", 1)])
    with pytest.raises(ValueError):
        mem_store.replace(ExtractedValue, {"doc_id; DROP TABLE x": "A"}, [])


@pytest.fixture(params=["memory", "file"])
def any_store(request, settings, tmp_path):
    store = DuckDBStore(settings, ":memory:" if request.param == "memory" else tmp_path / "tx.duckdb")
    store.ensure_schema()
    return store


def test_transaction_commits(any_store):
    with any_store.transaction():
        any_store.insert_new([make_record(Reviewer)])
        any_store.upsert([make_record(Reviewer, key_variant=1)])
    assert any_store.count(Reviewer) == 2


def test_transaction_rolls_back_every_write(any_store):
    any_store.insert_new([make_record(Reviewer)])
    with pytest.raises(RuntimeError, match="boom"):
        with any_store.transaction():
            any_store.upsert([dataclasses.replace(make_record(Reviewer), role="changed")])
            any_store.insert_new([make_record(Reviewer, key_variant=1)])
            any_store.replace(ExtractedValue, {"doc_id": "A"}, [_value("A", 1)])
            raise RuntimeError("boom")
    assert any_store.count(Reviewer) == 1
    assert any_store.get(Reviewer, **key_of(make_record(Reviewer))).role != "changed"
    assert any_store.count(ExtractedValue) == 0


def test_nested_transaction_joins_the_outer_one(any_store):
    with pytest.raises(ValueError):
        with any_store.transaction():
            any_store.insert_new([make_record(Reviewer)])
            with any_store.transaction():
                any_store.insert_new([make_record(Reviewer, key_variant=1)])
            raise ValueError("outer fails")
    assert any_store.count(Reviewer) == 0


def test_constraint_failure_inside_transaction_rolls_back(any_store):
    with pytest.raises(duckdb.ConstraintException):
        with any_store.transaction():
            any_store.insert_new([make_record(Reviewer)])
            any_store.insert_new([dataclasses.replace(make_record(Document), status="BAD")])
    assert any_store.count(Reviewer) == 0


def test_other_threads_do_not_join_a_transaction(settings, tmp_path):
    store = DuckDBStore(settings, tmp_path / "threads.duckdb")
    store.ensure_schema()
    seen = {}

    def count_from_other_thread(label):
        seen[label] = store.count(Reviewer)

    with store.transaction():
        store.insert_new([make_record(Reviewer)])
        t = threading.Thread(target=count_from_other_thread, args=("during",))
        t.start()
        t.join(timeout=30)
    count_from_other_thread("after")
    assert seen == {"during": 0, "after": 1}


def test_two_stores_on_one_file_see_each_others_writes(settings, tmp_path):
    path = tmp_path / "shared.duckdb"
    a, b = DuckDBStore(settings, path), DuckDBStore(settings, path)
    a.ensure_schema()
    b.ensure_schema()
    rec = make_record(Reviewer)
    assert a.insert_new([rec]) == 1
    assert b.get(Reviewer, **key_of(rec)) == rec
    assert b.upsert([dataclasses.replace(rec, role="admin")]) == 1
    assert a.get(Reviewer, **key_of(rec)).role == "admin"
    assert b.insert_new([rec]) == 0


def test_reader_does_not_see_uncommitted_writes_of_another_store(settings, tmp_path):
    path = tmp_path / "shared.duckdb"
    writer, reader = DuckDBStore(settings, path), DuckDBStore(settings, path)
    writer.ensure_schema()
    with writer.transaction():
        writer.insert_new([make_record(Reviewer)])
        assert reader.count(Reviewer) == 0
    assert reader.count(Reviewer) == 1


def test_read_only_store_reads_after_writer_closed(settings, tmp_path):
    path = tmp_path / "ro.duckdb"
    writer = DuckDBStore(settings, path)
    writer.ensure_schema()
    writer.insert_new([make_record(Reviewer)])
    reader = DuckDBStore(settings, path, read_only=True)
    assert reader.count(Reviewer) == 1
    with pytest.raises(duckdb.Error):
        reader.insert_new([make_record(Reviewer, key_variant=1)])


def test_ensure_schema_adds_contract_columns_an_older_file_lacks(settings, tmp_path):
    path = tmp_path / "old.duckdb"
    store = DuckDBStore(settings, path)
    store.ensure_schema()
    store.execute("ALTER TABLE kpi_evidence.reviewers DROP COLUMN updated_by")
    store.ensure_schema()
    cols = set(store.query("SELECT * FROM kpi_evidence.reviewers LIMIT 0").columns)
    assert cols == {c[0] for c in columns(Reviewer)}
    rec = make_record(Reviewer)
    store.upsert([rec])
    assert store.get(Reviewer, **key_of(rec)) == rec


@pytest.fixture()
def runner_store(mem_store):
    for schema in ("platform_restricted", "platform_restricted_archive", "platform_demo"):
        mem_store.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        mem_store.execute(f"CREATE TABLE {schema}.t AS SELECT 1.5 AS x UNION ALL SELECT 2.5")
    return mem_store


@pytest.mark.parametrize("sql", [
    "SELECT SUM(x) AS value FROM platform_restricted.t",
    "SELECT SUM(x) AS value FROM PLATFORM_RESTRICTED . t",
    'SELECT SUM(x) AS value FROM "platform_restricted".t',
    "SELECT SUM(x) AS value FROM memory.platform_restricted.t",
    "WITH a AS (SELECT x FROM platform_demo.t UNION ALL SELECT x FROM platform_restricted.t) SELECT SUM(x) AS value FROM a",
])
def test_runner_denies_restricted_schemas(runner_store, sql):
    runner = runner_store.runner()
    with pytest.raises(AccessDenied, match="PERMISSION_DENIED") as info:
        runner.run(sql)
    assert isinstance(info.value, PermissionError)
    assert "platform_restricted" in str(info.value)
    assert runner.calls == 0


def test_runner_allows_similarly_named_schemas(runner_store):
    runner = runner_store.runner()
    assert runner.run("SELECT SUM(x) AS value FROM platform_restricted_archive.t") == 4.0
    assert runner.run("SELECT SUM(x) AS value FROM platform_demo.t") == 4.0
    assert runner.calls == 2


@pytest.mark.parametrize("sql, message", [
    ("SELECT x AS value FROM platform_demo.t", "exactly one row"),
    ("SELECT SUM(x) AS total FROM platform_demo.t", "named 'value'"),
    ("SELECT CAST(NULL AS DOUBLE) AS value", "NULL"),
    ("SELECT CAST('inf' AS DOUBLE) AS value", "finite"),
])
def test_runner_enforces_the_one_value_contract(runner_store, sql, message):
    with pytest.raises(MetricQueryError, match=message):
        runner_store.runner().run(sql)


def test_runner_keeps_engine_error_messages(runner_store):
    with pytest.raises(duckdb.CatalogException, match="does_not_exist"):
        runner_store.runner().run("SELECT SUM(x) AS value FROM platform_demo.does_not_exist")


@pytest.mark.parametrize("sql, expected", [
    ("SELECT x::INT FROM t WHERE a = :a", "SELECT x::INT FROM t WHERE a = $a"),
    ("SELECT :a::DOUBLE", "SELECT $a::DOUBLE"),
    ("SELECT CAST(:a AS DATE), (:b)", "SELECT CAST($a AS DATE), ($b)"),
    ("WHERE decided_by = 'system:auto-policy@v1' AND x = :x", "WHERE decided_by = 'system:auto-policy@v1' AND x = $x"),
    ("SELECT 'it''s :not' AS t, :a", "SELECT 'it''s :not' AS t, $a"),
    ('SELECT "col:name" FROM t WHERE y = :y', 'SELECT "col:name" FROM t WHERE y = $y'),
    ("SELECT 1 -- :comment\nWHERE z = :z", "SELECT 1 -- :comment\nWHERE z = $z"),
    ("SELECT /* :block */ :a", "SELECT /* :block */ $a"),
    ("SELECT TIMESTAMP '2026-06-01 10:30:00', :a", "SELECT TIMESTAMP '2026-06-01 10:30:00', $a"),
    ("SELECT list[1:2], list[a:b] WHERE a = :a", "SELECT list[1:2], list[a:b] WHERE a = $a"),
    ("SELECT $1, $named, :a, :a", "SELECT $1, $named, $a, $a"),
    (":first", "$first"),
])
def test_to_duckdb_params_edge_cases(sql, expected):
    assert to_duckdb_params(sql) == expected


def test_query_binds_typed_params_and_ignores_unused_ones(mem_store):
    df = mem_store.query(
        "SELECT :d AS d, :n AS n, :s AS s, 'a:b' AS lit, :n + 1 AS n1",
        {"d": date(2026, 5, 31), "n": 2, "s": TRICKY, "unused": 1, "status": DocumentStatus.READY})
    row = df.iloc[0]
    assert row["n"] == 2 and row["n1"] == 3 and row["s"] == TRICKY and row["lit"] == "a:b"
    assert str(row["d"])[:10] == "2026-05-31"


def test_missing_params_still_fail(mem_store):
    with pytest.raises(duckdb.Error):
        mem_store.query("SELECT :missing AS v", {})


def test_enum_params_bind_as_their_value(mem_store):
    doc = dataclasses.replace(make_record(Document), status=DocumentStatus.READY, format=DocumentFormat.PDF)
    mem_store.upsert([doc])
    assert mem_store.read(Document, "status = :s", {"s": DocumentStatus.READY}) == [doc]
    assert mem_store.count(Observation) == 0
