from __future__ import annotations

from kpi_evidence import platform
from kpi_evidence.contracts import DqResult


def _by_key(results):
    return {(r.object_name, r.check_name): r for r in results}


def test_demo_platform_passes_every_hard_check(fresh_platform, registry):
    s = fresh_platform.settings
    results = platform.run_dq_checks(fresh_platform, registry, s, "run-dq-1")
    hard = [r for r in results if r.severity == "hard"]
    assert len(hard) == 9 and all(r.passed for r in hard), [(r.object_name, r.check_name, r.detail) for r in hard]
    fresh = [r for r in results if r.check_name == "fresh"]
    assert len(fresh) == 3 and all(r.passed for r in fresh)
    by = _by_key(results)
    assert "expected >= 2026-05" in by[("gv_rx_panel_monthly", "fresh")].detail
    assert "23 months 2024-07..2026-05" in by[("gv_rx_panel_monthly", "complete_months")].detail
    assert platform.dq_status_by_view(fresh_platform, s, registry) == {
        "gv_rx_account_monthly": "pass", "gv_rx_panel_monthly": "pass", "gv_referrals_monthly": "pass",
        "exfactory_monthly": "not_checked"}


def test_panel_base_table_duplicates_are_informational(fresh_platform, registry):
    s = fresh_platform.settings
    results = platform.run_dq_checks(fresh_platform, registry, s, "run-dq-2")
    base = _by_key(results)[(f"{s.platform_schema}.rx_panel_monthly", "unique_grain")]
    assert base.severity == "soft" and not base.passed and base.violations == 144
    assert "informational" in base.detail and "2024-0" in base.detail
    assert platform.dq_status_by_view(fresh_platform, s, registry)["gv_rx_panel_monthly"] == "pass"
    assert f"{s.platform_schema}.rx_panel_monthly" not in platform.dq_status_by_view(fresh_platform, s, registry)


def test_results_are_written_and_rerun_replaces(fresh_platform, registry):
    s = fresh_platform.settings
    first = platform.run_dq_checks(fresh_platform, registry, s, "run-dq-3")
    platform.run_dq_checks(fresh_platform, registry, s, "run-dq-3")
    cid = platform.dq_check_run_id("run-dq-3")
    assert all(r.check_run_id == cid for r in first)
    assert fresh_platform.count(DqResult, "check_run_id = :c", {"c": cid}) == len(first) == 13


def test_duplicate_grain_fails_the_view_and_names_blocked_kpis(fresh_platform, registry):
    s = fresh_platform.settings
    fresh_platform.execute(
        f"INSERT INTO {s.platform_schema}.rx_account_monthly SELECT * FROM {s.platform_schema}.rx_account_monthly "
        f"WHERE account_id = 'HCP0001' AND month = '2026-06'")
    results = _by_key(platform.run_dq_checks(fresh_platform, registry, s, "run-dq-4"))
    bad = results[("gv_rx_account_monthly", "unique_grain")]
    assert not bad.passed and bad.violations == 1
    assert "account_id=HCP0001" in bad.detail and "Blocks KPIs:" in bad.detail and "trx" in bad.detail
    status = platform.dq_status_by_view(fresh_platform, s, registry)
    assert status["gv_rx_account_monthly"] == "fail" and status["gv_referrals_monthly"] == "pass"


def test_latest_run_decides(fresh_platform, registry):
    s = fresh_platform.settings
    fresh_platform.execute(f"DELETE FROM {s.platform_schema}.referrals_monthly WHERE month = '2025-03'")
    gap = _by_key(platform.run_dq_checks(fresh_platform, registry, s, "run-a"))[("gv_referrals_monthly", "complete_months")]
    assert not gap.passed and gap.violations == 1 and "missing 2025-03" in gap.detail
    assert platform.dq_status_by_view(fresh_platform, s, registry)["gv_referrals_monthly"] == "fail"
    platform.load_demo_platform(fresh_platform, s)
    later = platform.evaluate_dq_checks(fresh_platform, s, platform.dq_check_run_id("run-b"), registry=registry)
    for r in later:
        r.checked_at = gap.checked_at.replace(year=gap.checked_at.year + 1)
    fresh_platform.insert(later)
    assert platform.dq_status_by_view(fresh_platform, s, registry)["gv_referrals_monthly"] == "pass"


def test_null_keys_fail(fresh_platform, registry):
    s = fresh_platform.settings
    fresh_platform.execute(f"UPDATE {s.platform_schema}.referrals_monthly SET account_id = NULL WHERE month = '2026-01' "
                           f"AND account_id = 'HCP0001'")
    r = _by_key(platform.run_dq_checks(fresh_platform, registry, s, "run-dq-5"))[("gv_referrals_monthly", "not_null_keys")]
    assert not r.passed and r.violations == 1


def test_freshness_is_soft(fresh_platform, registry):
    s = fresh_platform.settings
    results = _by_key(platform.run_dq_checks(fresh_platform, registry, s, "run-dq-6", reference_month="2026-09-15"))
    stale = results[("gv_rx_account_monthly", "fresh")]
    assert stale.severity == "soft" and not stale.passed and stale.violations == 3
    assert results[("gv_rx_panel_monthly", "fresh")].violations == 3
    assert platform.dq_status_by_view(fresh_platform, s, registry)["gv_rx_account_monthly"] == "pass"


def test_missing_view_is_a_failed_row_not_an_exception(fresh_platform, registry):
    s = fresh_platform.settings
    fresh_platform.execute(f"DROP VIEW {s.gov_schema}.gv_referrals_monthly")
    results = platform.run_dq_checks(fresh_platform, registry, s, "run-dq-7")
    rows = [r for r in results if r.object_name == "gv_referrals_monthly"]
    assert len(rows) == 4 and not any(r.passed for r in rows)
    assert all("check could not run (PLATFORM_DATA_ISSUE/platform_object_missing)" in r.detail for r in rows)
    status = platform.dq_status_by_view(fresh_platform, s, registry)
    assert status["gv_referrals_monthly"] == "fail" and status["gv_rx_account_monthly"] == "pass"


def test_no_run_yet_means_not_checked(mem_store, registry):
    status = platform.dq_status_by_view(mem_store, mem_store.settings, registry)
    assert set(status.values()) == {"not_checked"}
    assert {"gv_rx_account_monthly", "gv_rx_panel_monthly", "gv_referrals_monthly", "exfactory_monthly"} <= set(status)


def test_registry_defaults_from_settings(fresh_platform, registry):
    platform.run_dq_checks(fresh_platform, registry, fresh_platform.settings, "run-dq-8")
    assert platform.dq_status_by_view(fresh_platform, fresh_platform.settings)["exfactory_monthly"] == "not_checked"


def test_every_registry_source_view_is_governed_or_direct(registry):
    for kpi in registry.kpis.values():
        if kpi.platform is not None:
            for view in kpi.platform.source_views:
                assert view in platform.DQ_CHECKS or view in platform.DIRECT_SOURCES, (kpi.kpi_id, view)
