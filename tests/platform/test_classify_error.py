from __future__ import annotations

import duckdb
import pytest

from kpi_evidence.contracts import ReasonCode, ValidationStatus
from kpi_evidence.platform import StatementFailed, classify_error
from kpi_evidence.store.base import AccessDenied, MetricQueryError

MISSING_ACCESS = (ValidationStatus.MISSING_ACCESS, ReasonCode.PERMISSION_DENIED)
OBJECT_MISSING = (ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.PLATFORM_OBJECT_MISSING)
NO_DATA = (ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.NO_DATA_FOR_PERIOD)
NULL_RESULT = (ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.NULL_RESULT)
BLOCKED = (ValidationStatus.BLOCKED, ReasonCode.EXECUTION_ERROR)


class FakeAnalysisException(Exception):
    def __init__(self, message, condition=None, sqlstate=None, legacy=False):
        super().__init__(message)
        self._condition, self._sqlstate, self._legacy = condition, sqlstate, legacy

    def getCondition(self):
        if self._legacy:
            raise AttributeError("getCondition")
        return self._condition

    def getErrorClass(self):
        return self._condition

    def getSqlState(self):
        return self._sqlstate


def _duckdb_error(sql: str) -> Exception:
    con = duckdb.connect()
    con.execute("CREATE TABLE t (a INTEGER)")
    try:
        con.execute(sql)
    except Exception as exc:
        return exc
    raise AssertionError("expected an error")


@pytest.mark.parametrize("exc, expected", [
    (AccessDenied("PERMISSION_DENIED: the pipeline identity lacks SELECT on objects in schema 'platform_restricted'"),
     MISSING_ACCESS),
    (FakeAnalysisException("[INSUFFICIENT_PERMISSIONS] Insufficient privileges: User does not have SELECT on Table "
                           "'main.platform_restricted.exfactory_monthly'.", "INSUFFICIENT_PERMISSIONS", "42501"),
     MISSING_ACCESS),
    (FakeAnalysisException("something about privileges", None, "42501"), MISSING_ACCESS),
    (RuntimeError("PERMISSION_DENIED: User does not have USE SCHEMA on Schema 'main.platform_restricted'"), MISSING_ACCESS),
    (StatementFailed("PERMISSION_DENIED: no access", error_code="PERMISSION_DENIED"), MISSING_ACCESS),
    (FakeAnalysisException("[TABLE_OR_VIEW_NOT_FOUND] The table or view `kpi_gov`.`gv_x` cannot be found. SQLSTATE: 42P01",
                           "TABLE_OR_VIEW_NOT_FOUND", "42P01"), OBJECT_MISSING),
    (FakeAnalysisException("[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column with name `trx` cannot be resolved.",
                           "UNRESOLVED_COLUMN.WITH_SUGGESTION", "42703", legacy=True), OBJECT_MISSING),
    (FakeAnalysisException("analysis failed", "SCHEMA_NOT_FOUND", None), OBJECT_MISSING),
    (StatementFailed("BAD_REQUEST: [TABLE_OR_VIEW_NOT_FOUND] The table or view `main`.`kpi_gov`.`gv_x` cannot be found.",
                     error_code="BAD_REQUEST", sql_state="42P01"), OBJECT_MISSING),
    (_duckdb_error("SELECT * FROM nope.missing"), OBJECT_MISSING),
    (_duckdb_error("SELECT b FROM t"), OBJECT_MISSING),
    (ValueError("quality check 'period_has_rows' failed: 1 violations"), NO_DATA),
    (MetricQueryError("metric query returned NULL; explicitly use COALESCE if zero is intended"), NULL_RESULT),
    (ValueError("metric query returned NULL; explicitly use COALESCE if zero is intended"), NULL_RESULT),
    (ValueError("trx_share@abc: ratio denominator is zero at 2026-06-30"), NO_DATA),
    (_duckdb_error("SELECT CAST('x' AS INTEGER)"), BLOCKED),
    (KeyError("period_start"), BLOCKED),
    (MetricQueryError("metric query must return exactly one row and one column named 'value'"), BLOCKED),
    (TimeoutError("statement exceeded 300s"), BLOCKED),
])
def test_classification(exc, expected):
    status, code, detail = classify_error(exc)
    assert (status, code) == expected
    assert detail and len(detail) <= 500 and type(exc).__name__ in detail


def test_databricks_sdk_permission_error():
    from databricks.sdk.errors import NotFound, PermissionDenied

    assert classify_error(PermissionDenied("User is not authorized", error_code="PERMISSION_DENIED"))[:2] == MISSING_ACCESS
    assert classify_error(NotFound("Warehouse abc not found"))[:2] == BLOCKED


def test_cause_chain_is_inspected():
    try:
        try:
            raise AccessDenied("PERMISSION_DENIED: nope")
        except AccessDenied as inner:
            raise RuntimeError("evaluation failed") from inner
    except RuntimeError as outer:
        assert classify_error(outer)[:2] == MISSING_ACCESS


def test_detail_drops_jvm_stack_and_keeps_condition():
    exc = FakeAnalysisException("[TABLE_OR_VIEW_NOT_FOUND] The table or view `a`.`b` cannot be found.\n\tat org.apache"
                                ".spark.sql.Foo(Foo.scala:1)\n\tat more", "TABLE_OR_VIEW_NOT_FOUND", "42P01")
    detail = classify_error(exc)[2]
    assert "org.apache" not in detail and "[TABLE_OR_VIEW_NOT_FOUND]" in detail


def test_real_reconloop_errors(platform_registry, platform_settings, platform_store, case_tools):
    tools = case_tools
    panel_june = tools["KpiCase"]("nrx", "month", "source=panel", (("source", "panel"),), tools["JUNE"])
    out = tools["evaluate_case"](platform_registry, platform_settings, platform_store.runner(), panel_june)
    assert classify_error(out.error)[:2] == NO_DATA
    exfactory = tools["KpiCase"]("exfactory_units", "month", "default", (), tools["JUNE"])
    out = tools["evaluate_case"](platform_registry, platform_settings, platform_store.runner(), exfactory)
    assert classify_error(out.error)[:2] == MISSING_ACCESS
