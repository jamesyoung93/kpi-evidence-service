from __future__ import annotations

from check_standins import REPORTS
from kpi_evidence import platform
from kpi_evidence.check import ReportCheck
from kpi_evidence.settings import default_sql_dir

DECK = REPORTS / "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx"
REAL = "real_src.rx_account_monthly"


def _trx_monthly(result):
    rows = result.comparisons
    return rows[(rows["kpi_id"] == "trx") & (rows["period_grain"] == "month")]


def test_restating_the_real_table_behind_a_ported_view_recomputes(tmp_path, monkeypatch):
    folder = tmp_path / "sql"
    folder.mkdir()
    text = (default_sql_dir() / "governed_views.sql").read_text(encoding="utf-8")
    (folder / "governed_views.sql").write_text(text.replace("${platform}.rx_account_monthly", REAL), encoding="utf-8")
    monkeypatch.setattr(platform, "default_sql_dir", lambda: folder)
    state = tmp_path / "state.duckdb"

    with ReportCheck(state_path=str(state), registry="bundled", brand="KESTREL") as check:
        check._ensure_state()
        platform.load_demo_platform(check.store, check.settings)
        check.store.execute("CREATE SCHEMA IF NOT EXISTS real_src")
        check.store.execute(f"CREATE TABLE {REAL} AS SELECT * FROM {check.settings.platform_schema}.rx_account_monthly")
        platform.create_governed_views(check.store, check.settings)
        check.add_reports(DECK)
        first = _trx_monthly(check.run())
    assert len(first) and set(first["status"]) == {"VERIFIED"}

    with ReportCheck(state_path=str(state), registry="bundled", brand="KESTREL") as check:
        check.store.execute(f"UPDATE {REAL} SET trx_units = trx_units * 1.25")
        check.add_reports(DECK)
        second = _trx_monthly(check.run())
    assert not second["reused"].any()
    assert "VERIFIED" not in set(second["status"])
