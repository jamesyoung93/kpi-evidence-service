from __future__ import annotations

import pandas as pd
import pytest

from kpi_evidence import platform
from kpi_evidence.compile import compile_kpi, sql_relations
from kpi_evidence.registry import parse_kpi
from kpi_evidence.settings import Settings, default_sql_dir

REAL = "real_src.rx_account_monthly"


@pytest.fixture()
def ported_sql(tmp_path, monkeypatch):
    text = (default_sql_dir() / "governed_views.sql").read_text(encoding="utf-8")
    folder = tmp_path / "sql"
    folder.mkdir()
    (folder / "governed_views.sql").write_text(text.replace("${platform}.rx_account_monthly", REAL), encoding="utf-8")
    monkeypatch.setattr(platform, "default_sql_dir", lambda: folder)
    return folder


def test_bundled_views_map_to_their_demo_tables(platform_settings):
    s = platform_settings
    assert platform.governed_view_sources(s) == {
        "gv_rx_account_monthly": (f"{s.platform_schema}.rx_account_monthly",),
        "gv_rx_panel_monthly": (f"{s.platform_schema}.rx_panel_monthly",),
        "gv_referrals_monthly": (f"{s.platform_schema}.referrals_monthly",),
    }


def test_a_ported_view_is_pinned_by_the_table_it_reads(mem_store, ported_sql):
    s = mem_store.settings
    platform.load_demo_platform(mem_store, s)
    mem_store.execute("CREATE SCHEMA IF NOT EXISTS real_src")
    mem_store.execute(f"CREATE TABLE {REAL} AS SELECT * FROM {s.platform_schema}.rx_account_monthly")
    platform.create_governed_views(mem_store, s)
    before = platform.platform_snapshot(mem_store, s, ["gv_rx_account_monthly"])
    assert list(before) == [REAL] and platform.is_available(before)
    mem_store.execute(f"UPDATE {s.platform_schema}.rx_account_monthly SET trx_units = trx_units * 2")
    assert platform.platform_snapshot(mem_store, s, ["gv_rx_account_monthly"]) == before
    mem_store.execute(f"UPDATE {REAL} SET trx_units = trx_units * 1.25")
    after = platform.platform_snapshot(mem_store, s, [f"{s.gov_schema}.gv_rx_account_monthly"])
    assert list(after) == [REAL] and after[REAL] != before[REAL]


class _HistoryStore:
    dialect = "databricks"

    def __init__(self):
        self.queries: list[str] = []

    def query(self, sql, params=None):
        self.queries.append(sql)
        return pd.DataFrame({"version": [5]})


def test_a_ported_view_on_databricks_reads_the_history_of_the_real_table(ported_sql):
    s = Settings.from_env(backend="databricks", catalog="main")
    store = _HistoryStore()
    snap = platform.platform_snapshot(store, s, ["gv_rx_account_monthly", "main.kpi_gov.gv_rx_account_monthly"])
    assert snap == {REAL: "delta_version:5"}
    assert store.queries == [f"DESCRIBE HISTORY {REAL} LIMIT 1"]


def test_a_governed_view_missing_from_the_script_is_unavailable(platform_store, platform_settings, tmp_path,
                                                                monkeypatch):
    folder = tmp_path / "sql"
    folder.mkdir()
    text = (default_sql_dir() / "governed_views.sql").read_text(encoding="utf-8")
    (folder / "governed_views.sql").write_text(text.split("CREATE OR REPLACE VIEW ${gov}.gv_referrals_monthly")[0],
                                               encoding="utf-8")
    monkeypatch.setattr(platform, "default_sql_dir", lambda: folder)
    snap = platform.platform_snapshot(platform_store, platform_settings, ["gv_referrals_monthly"])
    assert not platform.is_available(snap) and snap["gv_referrals_monthly"].startswith("unavailable")


@pytest.mark.parametrize("sql, expected", [
    ("SELECT SUM(x) AS value FROM a.b WHERE y IN (SELECT y FROM c.d) AND z = 'FROM e.f'", ["a.b", "c.d"]),
    ("SELECT EXTRACT(YEAR FROM d) FROM s.t JOIN s.u ON TRUE -- FROM s.v", ["s.t", "s.u"]),
    ("SELECT 1 FROM (SELECT * FROM s.t) x WHERE a IS DISTINCT FROM b", ["s.t"]),
    ("SELECT 'it''s FROM x.y' AS v FROM s.t", ["s.t"]),
])
def test_sql_relations(sql, expected):
    assert sql_relations(sql) == expected


def _hint(value: str, where: str | None = None) -> dict:
    block = {"from": "platform_demo.rx_account_monthly", "value": value, "brand_column": None,
             "date_expr": "CAST(month || '-01' AS DATE)", "source_views": ["platform_demo.rx_account_monthly"]}
    if where:
        block["where"] = where
    return {"kpi_id": "trx_adj", "version": 0, "status": "DRAFT", "label": "TRx adjusted", "unit": "count",
            "aggregation": "sum", "grains": ["month"], "platform": block}


@pytest.mark.parametrize("raw, extra", [
    (_hint("SUM(trx_units) * (SELECT MAX(factor) FROM platform_demo.trx_adjust)"), "platform_demo.trx_adjust"),
    (_hint("SUM(trx_units)", "account_id NOT IN (SELECT account_id FROM main.crm.excluded)"), "main.crm.excluded"),
])
def test_compiled_relations_include_tables_read_in_subqueries(raw, extra, platform_settings):
    compiled = compile_kpi(parse_kpi(raw), brand="KESTREL", scope={}, grain="month", settings=platform_settings)
    assert sorted(compiled.relations()) == sorted(["platform_demo.rx_account_monthly", extra])


def test_compiled_relations_cover_every_declared_source(platform_registry, platform_settings):
    compiled = compile_kpi(platform_registry.get("trx"), brand="KESTREL", scope={}, grain="month",
                           settings=platform_settings)
    gov = platform_settings.gov_schema
    assert set(compiled.relations()) == {f"{gov}.gv_rx_account_monthly", f"{gov}.gv_rx_panel_monthly"}
