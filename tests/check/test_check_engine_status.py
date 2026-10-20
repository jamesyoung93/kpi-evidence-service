from __future__ import annotations

import warnings

import pandas as pd
import pytest

from check_standins import NOTEBOOK_KPI_ROWS, REPORTS, FakeSparkSession, install_standins
from kpi_evidence.check import ReportCheck
from kpi_evidence.contracts import Document

PDF = REPORTS / "Kestrel_Executive_Summary_Q2-2026.pdf"
UPDATE = REPORTS / "kestrel-update.pptx"


def _ai_issues(result) -> list[dict]:
    issues = result.issues
    return issues[issues["category"] == "AI"].to_dict(orient="records") if "category" in issues else []


def test_failed_ai_calls_are_an_issue_and_part_of_the_summary(monkeypatch):
    install_standins(monkeypatch)
    check = ReportCheck(load_demo_platform=True, ai_mode="ai_query")
    check.add_reports(PDF)
    result = check.run(materialize=False)
    ai = result.summary["ai"]
    assert result.summary["ai_mode"] == "ai_query" and ai["mode"] == "ai_query"
    assert ai["outcome"] == "failed" and ai["requests"] > 0 and ai["failed"] == ai["requests"]
    assert "Databricks" in ai["error"]
    [issue] = _ai_issues(result)
    assert issue["severity"] == "warning" and issue["item"] == "ai_query"
    assert issue["message"].startswith(f"{ai['failed']} of {ai['requests']} AI request(s) failed with ai_mode ai_query")
    check.close()


def test_ai_off_is_recorded_without_an_issue(monkeypatch):
    install_standins(monkeypatch)
    check = ReportCheck(load_demo_platform=True)
    check.add_reports(PDF)
    result = check.run(materialize=False)
    assert result.summary["ai_mode"] == "off"
    assert result.summary["ai"] == {"mode": "off", "outcome": "off", "requests": 0, "failed": 0, "error": None}
    assert _ai_issues(result) == []
    check.close()


def test_awaiting_confirmation_lists_only_fields_the_defaults_do_not_fill(monkeypatch):
    install_standins(monkeypatch)
    check = ReportCheck(load_demo_platform=True)
    check.add_reports(UPDATE)
    check.run(materialize=False)
    assert check.default_brand() == "KESTREL"
    doc = check.store.get(Document, doc_id=next(iter(check._entries)))
    assert doc.status.value == "AWAITING_CONFIRMATION" and doc.needs_confirmation == ["period_end"]
    assert check.documents["needs_confirmation"].tolist() == [["period_end"]]
    check.confirm(UPDATE.name, period="May 2026")
    check.run(materialize=False)
    doc = check.store.get(Document, doc_id=doc.doc_id)
    assert doc.status.value == "PROCESSED" and doc.brand == "KESTREL"
    check.close()


def test_a_closed_check_says_so_everywhere(tmp_path):
    check = ReportCheck(state_path=tmp_path / "state.duckdb")
    assert check.history().empty
    check.close()
    assert (tmp_path / "state.duckdb").is_file()
    for call in (check.history, check.save_state, lambda: check.documents, lambda: check.add_reports(PDF),
                 check.run):
        with pytest.raises(RuntimeError, match="this check is closed; create a new ReportCheck"):
            call()


def _rows(table: str) -> pd.DataFrame:
    return pd.DataFrame([{**row, "table": table if row["table"] else ""} for row in NOTEBOOK_KPI_ROWS])


def test_two_part_table_names_warn_which_catalog_they_resolve_against(tmp_path, monkeypatch):
    install_standins(monkeypatch, hints=False)
    check = ReportCheck(FakeSparkSession(tmp_path), catalog="main")
    with pytest.warns(UserWarning, match="without a catalog"):
        check.set_kpis(_rows("platform_demo.rx_account_monthly"))
    [message] = check._kpi_messages
    assert message["category"] == "KPI table" and "platform_demo.rx_account_monthly" in message["message"]
    assert "the session's current catalog (" in message["message"] and "catalog setting (main)" in message["message"]
    check.add_reports(PDF)
    result = check.run(materialize=False)
    assert any("without a catalog" in str(m) for m in result.issues["message"])


def test_three_part_names_and_local_checks_do_not_warn(tmp_path, monkeypatch):
    install_standins(monkeypatch, hints=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        spark_check = ReportCheck(FakeSparkSession(tmp_path), catalog="main")
        spark_check.set_kpis(_rows("main.platform_demo.rx_account_monthly"))
        local_check = ReportCheck(load_demo_platform=True)
        local_check.set_kpis(_rows("platform_demo.rx_account_monthly"))
    assert not [w for w in caught if "without a catalog" in str(w.message)]
    assert spark_check._kpi_messages == [] and local_check._kpi_messages == []
    local_check.close()
