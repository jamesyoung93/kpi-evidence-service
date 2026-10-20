from __future__ import annotations

import pandas as pd
import pytest

from check_standins import NOTEBOOK_KPI_ROWS, REPORTS, FakeSparkSession

pytest.importorskip("kpi_evidence.hints")
pytest.importorskip("kpi_evidence.report")

from kpi_evidence.check import ReportCheck, example_kpi_table_path

COMPARISON_COLUMNS = [
    "report_file", "source_type", "page", "object", "label_as_printed", "value_as_printed", "report_value", "unit",
    "period_label", "period_start", "period_end", "period_grain", "scope", "kpi_id", "kpi_label", "mapping_method",
    "definition_source", "definition", "platform_tables", "sql", "platform_value", "abs_diff", "rel_diff",
    "tolerance", "match_basis", "flag", "status", "status_label", "reason", "reused", "matched_alternative",
    "alternative_value", "metadata_confirmed", "result_id", "observation_id", "doc_id"]


@pytest.fixture(scope="module")
def demo_result():
    check = ReportCheck.demo()
    return check, check.run()


def test_example_kpi_table_is_found():
    path = example_kpi_table_path()
    assert path is not None and path.name == "kpi_table_example.csv"


def test_demo_comparison_report(demo_result):
    check, result = demo_result
    assert list(result.comparisons.columns) == COMPARISON_COLUMNS
    assert {"GOOD", "REVIEW"} <= set(result.comparisons["flag"])
    assert {"hint", "registry"} <= set(result.comparisons["definition_source"])
    assert "verdict" in result.kpis.columns and "validated" in set(result.kpis["verdict"])
    waiting = result.issues[result.issues["category"] == "awaiting confirmation"]
    assert "kestrel-update.pptx" in set(waiting["report_file"])
    assert check._kpi_source.endswith("kpi_table_example.csv")
    assert result.summary.get("run_id")


def test_demo_rerun_gives_the_same_result_ids(demo_result):
    check, result = demo_result
    again = check.run()
    assert set(again.comparisons["result_id"]) == set(result.comparisons["result_id"])
    assert len(again.issues) == len(result.issues)


def test_demo_html_is_written(demo_result, tmp_path):
    _check, result = demo_result
    path = result.save_html(str(tmp_path / "kpi_check.html"))
    text = open(path, encoding="utf-8").read()
    assert "<html" in text.lower() and "kestrel" in text.lower()


def test_notebook_mode_with_real_hints(tmp_path):
    spark = FakeSparkSession(tmp_path, governed_views=False)
    check = ReportCheck(spark)
    check.add_reports(REPORTS / "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx")
    table = check.set_kpis(pd.DataFrame(NOTEBOOK_KPI_ROWS))
    assert set(table["kpi_id"]) >= {"trx", "nrx", "calls", "brand_awareness"}
    result = check.run(materialize=False)
    rows = result.comparisons
    verified = rows[rows["status"] == "VERIFIED"]
    assert {"trx", "nrx"} <= set(verified["kpi_id"])
    assert set(verified["definition_source"]) == {"hint"}
    assert verified["sql"].str.contains("main.platform_demo.rx_account_monthly", regex=False).all()
    assert any("FROM main.platform_demo.rx_account_monthly" in s for s in spark.statements)


def test_invalid_kpi_rows_are_reported_not_dropped(tmp_path):
    spark = FakeSparkSession(tmp_path, governed_views=False)
    check = ReportCheck(spark, brand="Kestrel")
    rows = [NOTEBOOK_KPI_ROWS[0], dict(NOTEBOOK_KPI_ROWS[1], kpi="Bad NRx", value_sql="SUM(nrx); DROP TABLE x")]
    table = check.set_kpis(pd.DataFrame(rows))
    assert list(table["used"]) == [True, False]
    check.add_reports(REPORTS / "Kestrel_Executive_Summary_Q2-2026.pdf")
    result = check.run(materialize=False)
    assert (result.issues["category"].astype(str).str.contains("KPI table")).any()
