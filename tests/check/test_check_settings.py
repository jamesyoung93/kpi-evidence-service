from __future__ import annotations

from pathlib import Path

import pytest

from check_standins import ROOT, FakeSparkSession
from kpi_evidence import platform_access
from kpi_evidence.check import ReportCheck, empty_registry, with_brands, brand_id_for
from kpi_evidence.registry import load_registry
from kpi_evidence.settings import Settings
from kpi_evidence.store.databricks_store import DatabricksStore
from kpi_evidence.store.duckdb_store import DuckDBStore


def test_local_default_is_in_memory_with_a_temp_folder_and_ai_off(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    check = ReportCheck()
    s = check.settings
    assert check.mode == "local" and s.backend == "local" and s.platform_backend is None
    assert s.ai_mode == "off"
    assert isinstance(check.store, DuckDBStore) and check.store.path == ":memory:"
    assert check.platform is check.store.platform_access
    assert Path(s.local_dir).resolve() != (tmp_path / ".kpi_evidence").resolve()
    assert s.volume_path is None
    assert not (tmp_path / ".kpi_evidence").exists()


def test_registry_auto_is_bundled_only_in_local_demo_mode():
    assert ReportCheck(load_demo_platform=True)._base_registry.kpis
    plain = ReportCheck()._base_registry
    assert plain.kpis == {} and plain.brands == {}
    assert plain.policy["metadata"]["required_fields"] == ["brand", "period_end"]
    assert "monthly_brand_review" in plain.report_types


def test_registry_arguments(tmp_path):
    bundled = load_registry(ROOT / "registry")
    assert ReportCheck(registry="bundled")._base_registry.kpis.keys() == bundled.kpis.keys()
    assert ReportCheck(registry=None)._base_registry.kpis == {}
    assert ReportCheck(registry="none", load_demo_platform=True)._base_registry.kpis == {}
    assert ReportCheck(registry=ROOT / "registry")._base_registry.kpis.keys() == bundled.kpis.keys()
    assert ReportCheck(registry=bundled)._base_registry is bundled


def test_spark_mode_keeps_state_in_memory_and_runs_sql_through_spark(tmp_path):
    spark = FakeSparkSession(tmp_path)
    check = ReportCheck(spark, catalog="main", gov_schema="kpi_gov")
    s = check.settings
    assert check.mode == "spark" and s.backend == "local" and s.platform_backend == "databricks"
    assert isinstance(check.store, DuckDBStore) and check.store.path == ":memory:"
    assert isinstance(check.platform, platform_access.DatabricksPlatform)
    assert check.store.platform_access is check.platform
    assert check._base_registry.kpis == {}
    assert check.governed_views() == ["gv_rx_account_monthly", "gv_rx_panel_monthly", "gv_referrals_monthly"]
    assert all(sql.startswith("SELECT 1 AS ok FROM main.kpi_gov.") for sql in spark.statements)
    assert "notebook Spark session" in check.platform_description()


def test_spark_ai_query_is_opt_in(tmp_path):
    spark = FakeSparkSession(tmp_path)
    assert ReportCheck(spark).settings.ai_mode == "off"
    assert ReportCheck(spark, ai_mode="ai_query").settings.ai_mode == "ai_query"


def test_spark_persist_uses_delta_state(tmp_path):
    spark = FakeSparkSession(tmp_path)
    check = ReportCheck(spark, persist=True, catalog="main")
    assert check.settings.backend == "databricks"
    assert isinstance(check.store, DatabricksStore) and check.store.mode == "spark"
    assert check.store.platform_access is check.platform


def test_local_persist_uses_a_duckdb_file(tmp_path):
    settings = Settings.from_env(local_dir=tmp_path / "state")
    check = ReportCheck(settings=settings, persist=True)
    assert Path(check.store.path) == settings.local_db_path()


def test_warehouse_mode(monkeypatch, tmp_path):
    calls = {}

    def fake_warehouse(settings, *, client=None, warehouse_id=None):
        calls["warehouse_id"] = warehouse_id
        calls["platform_backend"] = settings.platform_backend
        return platform_access.StorePlatform(DuckDBStore(Settings.from_env(local_dir=tmp_path), ":memory:"))

    monkeypatch.setattr(platform_access, "warehouse_platform", fake_warehouse)
    check = ReportCheck(warehouse_id="wh-123", catalog="corp")
    assert check.mode == "warehouse" and calls == {"warehouse_id": "wh-123", "platform_backend": "databricks"}
    assert check.settings.warehouse_id == "wh-123" and check.settings.catalog == "corp"
    assert "SQL warehouse wh-123" in check.platform_description()


def test_demo_platform_only_locally(tmp_path):
    with pytest.raises(ValueError, match="local DuckDB only"):
        ReportCheck(FakeSparkSession(tmp_path), load_demo_platform=True)


def test_explicit_settings_are_kept_but_state_is_isolated(tmp_path):
    settings = Settings.from_env(local_dir=tmp_path / "mine", metadata_confidence_threshold=0.9, ai_mode="local",
                                 volume_path=str(tmp_path / "vol"))
    check = ReportCheck(settings=settings)
    assert check.settings.metadata_confidence_threshold == 0.9 and check.settings.ai_mode == "local"
    assert Path(check.settings.local_dir) != tmp_path / "mine" and check.settings.volume_path is None


def test_brand_helpers():
    reg = empty_registry(Settings.from_env())
    reg2 = with_brands(reg, ["Skyrizi", "skyrizi", None])
    assert list(reg2.brands) == ["SKYRIZI"] and reg2.brands["SKYRIZI"]["aliases"] == ["Skyrizi"]
    assert brand_id_for(reg2, "skyrizi") == "SKYRIZI" and brand_id_for(reg2, "Other Brand") == "OTHER_BRAND"
    bundled = load_registry(ROOT / "registry")
    assert with_brands(bundled, ["kestrel"]) is bundled
    assert brand_id_for(bundled, "Kestrel") == "KESTREL"


def test_default_brand_order():
    check = ReportCheck(load_demo_platform=True)
    assert check.default_brand() == "KESTREL"
    assert ReportCheck(brand="Skyrizi").default_brand() == "Skyrizi"
    assert ReportCheck().default_brand() is None
