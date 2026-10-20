from __future__ import annotations

import pandas as pd
import pytest

from check_standins import REPORTS, install_standins
from kpi_evidence import conflicts as conflicts_module
from kpi_evidence.check import ReportCheck
from kpi_evidence.contracts import Document, DocumentStatus

DECK = REPORTS / "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx"
SCOPES = "region=region; district=district; territory=territory; specialty=specialty; payer_channel=payer_channel"
FEED_ROW = {"kpi": "trx", "aliases": "TRx; Total prescriptions", "table": "platform_demo.trx_feed",
            "value_sql": "SUM(trx_units)", "date_column": "month", "date_format": "yyyy-MM",
            "scope_columns": SCOPES, "brand": "Kestrel"}


@pytest.fixture()
def standins(monkeypatch):
    install_standins(monkeypatch)


def _trx_monthly(result) -> pd.DataFrame:
    rows = result.comparisons
    return rows[(rows["kpi_id"] == "trx") & (rows["period_grain"] == "month")].set_index("result_id")


def test_rerun_after_a_platform_restatement_recomputes_the_verdicts(standins):
    check = ReportCheck(load_demo_platform=True)
    check.add_reports(DECK)
    first = _trx_monthly(check.run())
    assert len(first) and set(first["validation_status"]) == {"VERIFIED"}
    check.platform.execute("UPDATE platform_demo.rx_account_monthly SET trx_units = trx_units * 1.25")
    result = check.run()
    second = _trx_monthly(result)
    assert set(second.index) == set(first.index)
    assert "VERIFIED" not in set(second["validation_status"])
    assert not set(second.index) & result.call["reused_ids"]
    assert (second["platform_value"] > first.loc[second.index, "platform_value"] * 1.2).all()


def test_unchanged_rerun_reuses_saved_results_unless_reuse_is_off(standins):
    on = ReportCheck(load_demo_platform=True)
    on.add_reports(DECK)
    first = on.run()
    again = on.run()
    assert again.call["reused_ids"] and again.call["reused_ids"] <= set(first.comparisons["result_id"])
    assert dict(zip(again.comparisons["result_id"], again.comparisons["validation_status"])) == dict(
        zip(first.comparisons["result_id"], first.comparisons["validation_status"]))

    off = ReportCheck(load_demo_platform=True, reuse=False)
    off.add_reports(DECK)
    off.run()
    assert off.run().call["reused_ids"] == set()


def test_rerun_after_a_missing_table_is_created_computes_it(monkeypatch):
    install_standins(monkeypatch, hints=False)
    check = ReportCheck(load_demo_platform=True)
    check.set_kpis(pd.DataFrame([FEED_ROW]))
    check.add_reports(DECK)
    first = check.run(focus=["trx"])
    assert set(first.comparisons["validation_status"]) == {"PLATFORM_DATA_ISSUE"}
    check.platform.execute("CREATE TABLE platform_demo.trx_feed AS SELECT * FROM platform_demo.rx_account_monthly")
    second = check.run(focus=["trx"])
    assert set(second.comparisons["validation_status"]) == {"VERIFIED"}


def test_a_document_that_failed_processing_is_retried_on_the_next_run(standins, monkeypatch):
    real = conflicts_module.detect_conflicts
    calls = {"n": 0}

    def fails_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ConcurrentAppendException: files were added by a concurrent update")
        return real(*args, **kwargs)

    monkeypatch.setattr(conflicts_module, "detect_conflicts", fails_once)
    check = ReportCheck(load_demo_platform=True)
    check.add_reports(DECK)
    check.run()
    doc_id = next(iter(check._entries))
    assert check.store.get(Document, doc_id=doc_id).status == DocumentStatus.FAILED
    check.run()
    assert check.store.get(Document, doc_id=doc_id).status == DocumentStatus.PROCESSED
    assert calls["n"] == 2
