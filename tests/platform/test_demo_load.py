from __future__ import annotations

import pandas as pd
import pytest

from kpi_evidence import platform
from kpi_evidence.settings import Settings


def test_load_counts_and_qualified_names(platform_store, platform_settings):
    s = platform_settings
    counts = platform.load_demo_platform(platform_store, s)
    assert counts == {
        f"{s.platform_schema}.rx_account_monthly": 5760,
        f"{s.platform_schema}.rx_panel_monthly": 1104 + 3 * 48,
        f"{s.platform_schema}.referrals_monthly": 4608,
        f"{s.platform_schema}.accounts": 240,
        f"{s.restricted_schemas[0]}.exfactory_monthly": 24,
    }


def test_npi_and_months_are_strings(platform_store, platform_settings):
    s = platform_settings
    for table in ("rx_account_monthly", "referrals_monthly", "accounts"):
        types = dict(platform_store.query(f"DESCRIBE {s.platform_schema}.{table}")[["column_name", "column_type"]].values)
        assert types["npi"] == "VARCHAR", table
    types = dict(platform_store.query(f"DESCRIBE {s.platform_schema}.rx_account_monthly")[["column_name", "column_type"]].values)
    assert types["month"] == "VARCHAR"
    npi = platform_store.query(f"SELECT npi FROM {s.platform_schema}.accounts ORDER BY account_id LIMIT 1").iloc[0, 0]
    assert npi == "9999000001"


def test_new_writers_first_month_is_null(platform_store, platform_settings):
    df = platform_store.query(
        f"SELECT COUNT(*) AS n, COUNT(new_writers) AS non_null FROM {platform_settings.platform_schema}.rx_account_monthly "
        f"WHERE month = '2024-07'")
    assert int(df.iloc[0, 0]) == 240 and int(df.iloc[0, 1]) == 0


def test_panel_has_two_loads_with_pre_restatement_level(platform_store, platform_settings):
    s = platform_settings
    df = platform_store.query(
        f"SELECT load_id, CAST(MIN(loaded_at) AS DATE) AS loaded, MIN(month) AS first_month, MAX(month) AS last_month, "
        f"COUNT(*) AS n FROM {s.platform_schema}.rx_panel_monthly GROUP BY load_id ORDER BY load_id")
    rows = {r["load_id"]: r for r in df.to_dict(orient="records")}
    assert set(rows) == {"L2024-10", "L2026-06"}
    assert rows["L2024-10"]["n"] == 144 and rows["L2024-10"]["last_month"] == "2024-09"
    assert str(rows["L2024-10"]["loaded"])[:10] == "2024-10-05" and str(rows["L2026-06"]["loaded"])[:10] == "2026-06-05"
    ratio = platform_store.query(
        f"SELECT SUM(CASE WHEN load_id = 'L2026-06' THEN il17_class_trx END) / "
        f"SUM(CASE WHEN load_id = 'L2024-10' THEN il17_class_trx END) AS r "
        f"FROM {s.platform_schema}.rx_panel_monthly WHERE month <= '2024-09'").iloc[0, 0]
    assert ratio == pytest.approx(1.015, rel=1e-6)


def test_exfactory_is_rounded_trx_times_1_04(platform_store, platform_settings):
    s = platform_settings
    df = platform_store.query(
        f"SELECT e.month, e.brand, e.units_shipped, a.trx FROM {s.restricted_schemas[0]}.exfactory_monthly e "
        f"JOIN (SELECT month, SUM(trx_units) AS trx FROM {s.platform_schema}.rx_account_monthly GROUP BY month) a "
        f"USING (month) ORDER BY e.month")
    assert len(df) == 24 and set(df["brand"]) == {platform.DEMO_BRAND}
    assert (df["units_shipped"] == (df["trx"] * 1.04).round(0)).all()


def test_governed_views_created(platform_store, platform_settings):
    names = platform.create_governed_views(platform_store, platform_settings)
    assert names == ["gv_rx_account_monthly", "gv_rx_panel_monthly", "gv_referrals_monthly"]
    assert set(names) == set(platform.GOVERNED_VIEWS) == set(platform.DQ_CHECKS)
    for name in names:
        cols = set(platform_store.query(f"SELECT * FROM {platform_settings.gov_schema}.{name} LIMIT 0").columns)
        assert {"brand", "month", "month_start", "month_end", "region", "district", "territory", "specialty",
                "payer_channel"} <= cols


def test_render_sql_uses_catalog_prefix_on_databricks():
    s = Settings.from_env(backend="databricks", catalog="dev_cat")
    sql = platform.governed_views_sql(s)
    assert "CREATE OR REPLACE VIEW dev_cat.kpi_gov.gv_rx_account_monthly" in sql
    assert "FROM dev_cat.platform_demo.rx_account_monthly" in sql
    assert "${" not in sql
    assert platform.base_table_fq(s, "exfactory_monthly") == "dev_cat.platform_restricted.exfactory_monthly"
    with pytest.raises(ValueError, match="unknown placeholder"):
        platform.render_sql("SELECT * FROM ${nope}.t", s)


def test_load_through_a_databricks_like_store_uses_three_part_names():
    class RecordingStore:
        dialect = "databricks"

        def __init__(self):
            self.statements, self.writes = [], {}

        def execute(self, sql, params=None):
            self.statements.append(sql)

        def execute_script(self, script):
            self.statements.extend(s for s in script.split(";") if s.strip())

        def write_dataframe(self, table, frame, mode="overwrite"):
            assert mode == "overwrite" and isinstance(frame, pd.DataFrame)
            self.writes[table] = frame
            return len(frame)

    s = Settings.from_env(backend="databricks", catalog="main")
    store = RecordingStore()
    counts = platform.load_demo_platform(store, s)
    assert set(counts) == {"main.platform_demo.rx_account_monthly", "main.platform_demo.rx_panel_monthly",
                           "main.platform_demo.referrals_monthly", "main.platform_demo.accounts",
                           "main.platform_restricted.exfactory_monthly"}
    assert "CREATE SCHEMA IF NOT EXISTS main.platform_restricted" in store.statements
    nw = store.writes["main.platform_demo.rx_account_monthly"]["new_writers"]
    assert str(nw.dtype) == "Float64" and nw.isna().sum() == 240
    assert platform.create_governed_views(store, s) == list(platform.GOVERNED_VIEWS)


def test_shared_demo_store_fixture_uses_this_module(demo_store):
    s = demo_store.settings
    n = demo_store.query(f"SELECT COUNT(*) FROM {s.restricted_schemas[0]}.exfactory_monthly").iloc[0, 0]
    assert int(n) == 24
    dup = demo_store.query(
        f"SELECT COUNT(*) - COUNT(DISTINCT month || territory || payer_channel) FROM {s.gov_schema}.gv_rx_panel_monthly")
    assert int(dup.iloc[0, 0]) == 0


def test_missing_demo_file_is_an_explicit_error(tmp_path):
    s = Settings.from_env(local_dir=tmp_path / ".k", extra={"demo_platform_dir": str(tmp_path / "empty")})
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError, match="fact_source_a.csv"):
        platform.demo_platform_frames(s)
