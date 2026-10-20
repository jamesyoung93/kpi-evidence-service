from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from kpi_evidence.check import ReportCheck

DECK = Path(__file__).resolve().parents[2] / "samples" / "reports" / "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx"
SCOPES = "region=region; district=district; territory=territory; specialty=specialty; payer_channel=payer_channel"
BASE = {"kpi": "trx", "aliases": "TRx; Total prescriptions", "table": "platform_demo.rx_account_monthly",
        "date_column": "month", "date_format": "yyyy-MM", "unit": "count", "tolerance": "0.5%", "brand": "Kestrel",
        "scope_columns": SCOPES}
CASES = {
    "scalar subquery in value_sql": (
        {"value_sql": "SUM(trx_units) * (SELECT MAX(factor) FROM platform_demo.trx_adjust)"},
        "CREATE OR REPLACE TABLE platform_demo.trx_adjust AS SELECT CAST(1.0 AS DOUBLE) AS factor",
        "UPDATE platform_demo.trx_adjust SET factor = 2.0"),
    "subquery in filters": (
        {"value_sql": "SUM(trx_units)",
         "filters": "account_id NOT IN (SELECT account_id FROM platform_demo.excluded_accounts)"},
        "CREATE OR REPLACE TABLE platform_demo.excluded_accounts AS SELECT 'none' AS account_id",
        "INSERT INTO platform_demo.excluded_accounts SELECT DISTINCT account_id FROM platform_demo.rx_account_monthly "
        "LIMIT 200"),
}


def _session(state: Path, row: dict, setup: str | None, change: str | None):
    with ReportCheck(load_demo_platform=True, state_path=str(state)) as check:
        check._bootstrap(check._effective_registry())
        if setup:
            check.store.execute(setup)
        if change:
            check.store.execute(change)
        check.set_kpis(pd.DataFrame([row]))
        check.add_reports(DECK)
        return check.run(focus=["trx"])


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_change_in_a_subquery_table_is_never_hidden_by_reuse(tmp_path, case):
    extra, setup, change = CASES[case]
    row = {**BASE, **extra}
    state = tmp_path / "state.duckdb"
    first = _session(state, row, setup, None)
    assert set(first.comparisons["status"]) == {"VERIFIED"}
    same = _session(state, row, None, None)
    assert same.comparisons["reused"].all()
    changed = _session(state, row, None, change)
    assert not changed.comparisons["reused"].any()
    assert "VERIFIED" not in set(changed.comparisons["status"])
