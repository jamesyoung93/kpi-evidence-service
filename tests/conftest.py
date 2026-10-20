from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from kpi_evidence.registry import load_registry
from kpi_evidence.settings import Settings
from kpi_evidence.store.duckdb_store import DuckDBStore

ROOT = Path(__file__).resolve().parents[1]
PLATFORM_DATA = ROOT / "apps" / "insight_harness" / "data"


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings.from_env(local_dir=tmp_path / ".kpi_evidence", ai_mode="local", registry_dir=ROOT / "registry")


@pytest.fixture(scope="session")
def registry():
    return load_registry(ROOT / "registry")


@pytest.fixture()
def mem_store(settings) -> DuckDBStore:
    store = DuckDBStore(settings, ":memory:")
    store.ensure_schema()
    return store


def _load_demo_platform_fallback(store: DuckDBStore) -> None:
    a = pd.read_csv(PLATFORM_DATA / "fact_source_a.csv", dtype={"npi": str})
    b = pd.read_csv(PLATFORM_DATA / "fact_source_b.csv")
    b["load_id"], b["loaded_at"] = "L2026-06", pd.Timestamp("2026-06-05")
    r = pd.read_csv(PLATFORM_DATA / "fact_referral.csv", dtype={"npi": str})
    s = store.settings
    store.register_frame("rx_account_monthly", a, f"{s.platform_schema}.rx_account_monthly")
    store.register_frame("rx_panel_monthly", b, f"{s.platform_schema}.rx_panel_monthly")
    store.register_frame("referrals_monthly", r, f"{s.platform_schema}.referrals_monthly")
    sql = (ROOT / "src" / "sql" / "governed_views.sql").read_text(encoding="utf-8")
    sql = sql.replace("${gov}", s.gov_schema).replace("${platform}", s.platform_schema)
    store.execute_script(sql)


@pytest.fixture()
def demo_store(mem_store) -> DuckDBStore:
    try:
        from kpi_evidence import platform
        platform.load_demo_platform(mem_store, mem_store.settings)
        platform.create_governed_views(mem_store, mem_store.settings)
    except (ImportError, AttributeError):
        _load_demo_platform_fallback(mem_store)
    return mem_store
