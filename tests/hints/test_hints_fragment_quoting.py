from __future__ import annotations

import pandas as pd
import pytest

from kpi_evidence.hints import parse_hints, sql_fragment_problems

BS = chr(92)


@pytest.mark.parametrize("text", [
    f"'{BS}'' UPDATE '{BS}''",
    f"region = '{BS}'' OR TRUE OR region = '{BS}''",
    f"region = 'O{BS}'Brien'",
    "region = 'O''Brien'",
    'label = "say ""hi"""',
])
def test_fragments_that_spark_and_duckdb_read_differently_are_rejected(text):
    assert sql_fragment_problems(text)


@pytest.mark.parametrize("text", [
    "COALESCE(region, '') <> ''",
    "channel IN ('', 'RETAIL')",
    "channel = 'CALL CENTER' AND status <> 'DELETE'",
])
def test_plain_literals_stay_valid(text):
    assert sql_fragment_problems(text) == []


def test_a_kpi_table_row_with_a_backslash_escape_is_skipped_with_a_message():
    row = {"kpi": "trx", "table": "platform_demo.rx_account_monthly", "value_sql": "SUM(trx_units)",
           "date_column": "month", "date_format": "yyyy-MM", "filters": f"region = '{BS}'' OR TRUE OR region = '{BS}''"}
    hints = parse_hints(pd.DataFrame([row]))
    assert "trx" not in hints.kpis
    assert any("backslashes" in m for m in hints.errors["message"])
