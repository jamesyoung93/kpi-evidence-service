from __future__ import annotations

import pandas as pd

from kpi_evidence import platform
from kpi_evidence.settings import Settings


def test_dimension_values(platform_store, platform_settings):
    dims = platform.dimension_values(platform_store, platform_settings)
    assert set(dims) == {"region", "district", "territory", "specialty", "payer_channel"}
    assert dims["region"] == {"East", "North", "South", "West"}
    assert len(dims["district"]) == 8 and "West District 1" in dims["district"]
    assert len(dims["territory"]) == 24 and "N-PCP-01" in dims["territory"]
    assert dims["specialty"] == {"Primary Care", "Cardiology", "Endocrinology"}
    assert dims["payer_channel"] == {"Cash", "Commercial", "Medicaid", "Medicare Part D"}
    assert all(isinstance(v, str) for values in dims.values() for v in values)


def test_snapshot_is_deterministic_and_maps_views_to_base_tables(platform_store, platform_settings):
    s = platform_settings
    views = ["gv_rx_account_monthly", "gv_rx_panel_monthly", "gv_referrals_monthly"]
    snap = platform.platform_snapshot(platform_store, s, views)
    assert set(snap) == {f"{s.platform_schema}.rx_account_monthly", f"{s.platform_schema}.rx_panel_monthly",
                         f"{s.platform_schema}.referrals_monthly"}
    assert all(v.startswith("content:") and len(v) == len("content:") + 16 for v in snap.values())
    assert platform.platform_snapshot(platform_store, s, list(reversed(views))) == snap
    assert platform.platform_snapshot(platform_store, s, views + views) == snap


def test_snapshot_changes_when_data_changes(fresh_platform):
    s = fresh_platform.settings
    before = platform.platform_snapshot(fresh_platform, s, ["gv_rx_account_monthly", "gv_referrals_monthly"])
    fresh_platform.execute(f"UPDATE {s.platform_schema}.rx_account_monthly SET trx_units = trx_units + 1 "
                           f"WHERE account_id = 'HCP0002' AND month = '2026-06'")
    after = platform.platform_snapshot(fresh_platform, s, ["gv_rx_account_monthly", "gv_referrals_monthly"])
    key = f"{s.platform_schema}.rx_account_monthly"
    assert before[key] != after[key]
    assert before[f"{s.platform_schema}.referrals_monthly"] == after[f"{s.platform_schema}.referrals_monthly"]


def test_snapshot_records_restricted_and_unknown_objects(platform_store, platform_settings):
    s = platform_settings
    snap = platform.platform_snapshot(platform_store, s, ["exfactory_monthly", "gv_does_not_exist"])
    assert snap[f"{s.restricted_schemas[0]}.exfactory_monthly"].startswith("unavailable: permission_denied")
    assert snap["gv_does_not_exist"].startswith("unavailable: unknown_object")


def test_snapshot_reports_a_missing_table(fresh_platform):
    s = fresh_platform.settings
    fresh_platform.execute(f"DROP TABLE {s.platform_schema}.referrals_monthly")
    snap = platform.platform_snapshot(fresh_platform, s, ["gv_referrals_monthly"])
    assert snap[f"{s.platform_schema}.referrals_monthly"].startswith("unavailable: platform_object_missing")


class _FakeDatabricksStore:
    dialect = "databricks"

    def __init__(self, versions: dict[str, object]):
        self.versions = versions
        self.queries: list[str] = []

    def query(self, sql, params=None):
        self.queries.append(sql)
        table = sql.removeprefix("DESCRIBE HISTORY ").removesuffix(" LIMIT 1")
        outcome = self.versions[table]
        if isinstance(outcome, Exception):
            raise outcome
        return pd.DataFrame({"version": [outcome], "operation": ["WRITE"]})


def test_snapshot_on_databricks_uses_describe_history():
    s = Settings.from_env(backend="databricks", catalog="main")
    store = _FakeDatabricksStore({
        "main.platform_demo.rx_account_monthly": 12,
        "main.platform_demo.rx_panel_monthly": 7,
        "main.platform_restricted.exfactory_monthly": RuntimeError(
            "[INSUFFICIENT_PERMISSIONS] Insufficient privileges: User does not have SELECT on Table "
            "'main.platform_restricted.exfactory_monthly'. SQLSTATE: 42501"),
    })
    snap = platform.platform_snapshot(store, s, ["gv_rx_account_monthly", "gv_rx_panel_monthly", "exfactory_monthly"])
    assert snap["main.platform_demo.rx_account_monthly"] == "delta_version:12"
    assert snap["main.platform_demo.rx_panel_monthly"] == "delta_version:7"
    assert snap["main.platform_restricted.exfactory_monthly"].startswith("unavailable: permission_denied")
    assert store.queries[0].startswith("DESCRIBE HISTORY main.platform_demo.")


def test_snapshot_hashes_dotted_kpi_table_names_directly(fresh_platform):
    s = fresh_platform.settings
    name = f"{s.platform_schema}.referrals_monthly"
    before = platform.platform_snapshot(fresh_platform, s, [name, name])
    assert list(before) == [name] and before[name].startswith("content:")
    assert platform.is_available(before)
    fresh_platform.execute(f"UPDATE {name} SET region = 'Elsewhere' WHERE month = '2026-06'")
    after = platform.platform_snapshot(fresh_platform, s, [name])
    assert after[name] != before[name]


def test_snapshot_marks_unreadable_dotted_names_unavailable(platform_store, platform_settings):
    s = platform_settings
    restricted = f"{s.restricted_schemas[0]}.exfactory_monthly"
    missing = f"{s.platform_schema}.no_such_table"
    snap = platform.platform_snapshot(platform_store, s, [restricted, missing])
    assert snap[restricted].startswith("unavailable: permission_denied")
    assert snap[missing].startswith("unavailable: platform_object_missing")
    assert not platform.is_available(snap) and not platform.is_available({})


def test_snapshot_on_databricks_reads_the_history_of_dotted_names():
    s = Settings.from_env(backend="databricks", catalog="main")
    store = _FakeDatabricksStore({"main.sales.rx": 4, "main.sales.rx_view": RuntimeError(
        "[DELTA_TABLE_NOT_FOUND] DESCRIBE HISTORY is only supported for Delta tables; main.sales.rx_view does not exist")})
    snap = platform.platform_snapshot(store, s, ["main.sales.rx", "main.sales.rx_view"])
    assert snap["main.sales.rx"] == "delta_version:4"
    assert snap["main.sales.rx_view"].startswith("unavailable")
    assert store.queries == ["DESCRIBE HISTORY main.sales.rx LIMIT 1", "DESCRIBE HISTORY main.sales.rx_view LIMIT 1"]
