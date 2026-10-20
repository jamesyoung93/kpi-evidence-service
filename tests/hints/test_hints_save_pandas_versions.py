from __future__ import annotations

import math

import pandas as pd
import pytest

from kpi_evidence.hints import COLUMNS, load_kpi_table, save_kpi_table


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "KPI": ["TRx", "Brand awareness"],
        "Table": ["main.platform_demo.rx_account_monthly", None],
        "value_sql": ["  SUM(trx_units)  ", math.nan],
        "priority": [1, pd.NA],
        "tolerance": [0.005, None],
    })


@pytest.mark.parametrize("suffix", [".csv", ".xlsx"])
def test_save_kpi_table_writes_blanks_and_text_on_any_pandas(tmp_path, suffix):
    path = tmp_path / f"kpis{suffix}"
    assert save_kpi_table(_frame(), path) == str(path)
    back = load_kpi_table(path)
    assert list(back.columns) == ["kpi", "table", "value_sql", "tolerance", "priority"]
    assert back["kpi"].tolist() == ["TRx", "Brand awareness"]
    assert back["table"].tolist() == ["main.platform_demo.rx_account_monthly", ""]
    assert back["value_sql"].tolist() == ["SUM(trx_units)", ""]
    assert back["priority"].tolist() == ["1", ""]


def test_save_kpi_table_without_rows_writes_every_column(tmp_path):
    path = tmp_path / "empty.csv"
    save_kpi_table(pd.DataFrame({"kpi": []}), path)
    assert path.read_text(encoding="utf-8").strip() == "kpi"
    save_kpi_table(pd.DataFrame(), path)
    assert path.read_text(encoding="utf-8").strip() == ",".join(COLUMNS)
