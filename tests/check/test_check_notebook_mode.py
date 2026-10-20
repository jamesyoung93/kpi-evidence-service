from __future__ import annotations

import pandas as pd
import pytest

from check_standins import NOTEBOOK_KPI_ROWS, REPORTS, FakeSparkSession, install_standins
from kpi_evidence.check import ReportCheck
from kpi_evidence.contracts import Document
from kpi_evidence.store.duckdb_store import DuckDBStore

DECK = REPORTS / "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx"


def _platform_sql(spark: FakeSparkSession) -> list[str]:
    return [s for s in spark.statements if "platform_demo." in s]


@pytest.fixture(scope="module")
def notebook_run(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        install_standins(mp)
        spark = FakeSparkSession(tmp_path_factory.mktemp("spark_gov"))
        check = ReportCheck(spark)
        check.add_reports(DECK)
        table = check.set_kpis(pd.DataFrame(NOTEBOOK_KPI_ROWS))
        result = check.run()
        yield spark, check, table, result


def test_state_in_memory_and_registry_empty_plus_hints(notebook_run):
    _spark, check, table, result = notebook_run
    assert isinstance(check.store, DuckDBStore) and check.store.path == ":memory:"
    assert check._base_registry.kpis == {}
    assert sorted(result.call["registry"].kpis) == ["brand_awareness", "calls", "nrx", "trx"]
    assert check.default_brand() == "KESTREL"
    assert list(table["kpi_id"]) == ["trx", "nrx", "calls", "brand_awareness"]
    assert result.call["hints"] is check._hints


def test_kpi_sql_runs_through_spark_on_the_hinted_tables(notebook_run):
    spark, _check, _table, result = notebook_run
    sql = _platform_sql(spark)
    assert any("FROM main.platform_demo.rx_account_monthly" in s for s in sql)
    assert all("kpi_evidence." not in s for s in spark.statements)
    verified = result.comparisons[result.comparisons["validation_status"] == "VERIFIED"]
    assert {"trx", "nrx", "calls"} <= set(verified["kpi_id"])
    assert (verified["kpi_id"] == "trx").sum() >= 6
    report_only = result.comparisons[result.comparisons["validation_status"] == "REPORT_ONLY"]
    assert "brand_awareness" in set(report_only["kpi_id"])


def test_governed_views_present_run_dq_and_scope_detection(notebook_run):
    spark, check, _table, result = notebook_run
    assert check.store.query("SELECT COUNT(*) AS n FROM kpi_evidence.dq_results").iloc[0, 0] > 0
    assert any("SELECT DISTINCT region, district" in s for s in spark.statements)
    nrx = result.comparisons[(result.comparisons["kpi_id"] == "nrx")
                             & (result.comparisons["validation_status"] == "VERIFIED")]
    scopes = set(nrx["scope_key"])
    assert '{"region":"East"}' in scopes and '{"region":"West"}' in scopes
    assert "governed views in main.kpi_gov" in result.call["platform_description"]


def test_without_governed_views_scope_values_come_from_the_kpi_table(tmp_path, monkeypatch):
    install_standins(monkeypatch)
    spark = FakeSparkSession(tmp_path, governed_views=False)
    check = ReportCheck(spark)
    check.add_reports(DECK)
    check.set_kpis(pd.DataFrame(NOTEBOOK_KPI_ROWS))
    result = check.run(materialize=False)
    assert check.governed_views() == []
    assert check.store.query("SELECT COUNT(*) AS n FROM kpi_evidence.dq_results").iloc[0, 0] == 0
    assert any(s.startswith("SELECT DISTINCT region AS v FROM main.platform_demo.rx_account_monthly")
               for s in spark.statements)
    nrx = result.comparisons[(result.comparisons["kpi_id"] == "nrx")
                             & (result.comparisons["validation_status"] == "VERIFIED")]
    assert '{"region":"North"}' in set(nrx["scope_key"])
    assert "no governed views in main.kpi_gov" in result.call["platform_description"]


def test_missing_table_and_denied_schema_are_explicit(tmp_path, monkeypatch):
    install_standins(monkeypatch)
    rows = [dict(NOTEBOOK_KPI_ROWS[0], table="main.platform_demo.no_such_table", scope_columns=""),
            {"kpi": "Ex-factory units", "aliases": "ex-factory units", "table": "main.platform_restricted.exfactory_monthly",
             "value_sql": "SUM(units_shipped)", "date_column": "month", "date_format": "yyyy-MM", "unit": "count",
             "brand": "KESTREL"}]
    spark = FakeSparkSession(tmp_path, governed_views=False)
    check = ReportCheck(spark)
    check.add_reports(DECK)
    check.set_kpis(pd.DataFrame(rows))
    result = check.run(materialize=False)
    by_kpi = result.comparisons.groupby("kpi_id")["validation_status"].agg(set).to_dict()
    assert "PLATFORM_DATA_ISSUE" in by_kpi["trx"] and "VERIFIED" not in by_kpi["trx"]
    assert by_kpi["ex_factory_units"] == {"MISSING_ACCESS"}


def test_scope_value_errors_and_dimension_argument(tmp_path, monkeypatch):
    install_standins(monkeypatch)
    rows = [dict(NOTEBOOK_KPI_ROWS[0], scope_columns="region=no_such_column")]
    spark = FakeSparkSession(tmp_path, governed_views=False)
    check = ReportCheck(spark, dimension_values={"region": ["Atlantis"]})
    check.add_reports(DECK)
    check.set_kpis(pd.DataFrame(rows))
    messages: list[dict] = []
    dims = check._collect_dimension_values([], messages)
    assert dims == {"region": {"Atlantis"}}
    assert messages and messages[0]["category"] == "scope values" and "region" in messages[0]["message"]
    result = check.run(materialize=False)
    assert "scope values" in set(result.issues["category"])


def test_metadata_without_brand_waits_for_a_brand(tmp_path, monkeypatch):
    install_standins(monkeypatch)
    spark = FakeSparkSession(tmp_path, governed_views=False)
    check = ReportCheck(spark)
    check.add_reports(REPORTS / "Kestrel_Executive_Summary_Q2-2026.pdf")
    result = check.run(materialize=False)
    doc = check.store.get(Document, doc_id=next(iter(check._entries)))
    assert doc.status.value == "AWAITING_CONFIRMATION" and "brand" in doc.needs_confirmation
    assert result.comparisons.empty
    check.confirm("Kestrel_Executive_Summary_Q2-2026.pdf", brand="Kestrel")
    check.run(materialize=False)
    doc = check.store.get(Document, doc_id=doc.doc_id)
    assert doc.status.value == "PROCESSED" and doc.brand == "KESTREL"
