from __future__ import annotations

import math

import pytest

from kpi_evidence.contracts import ReasonCode, ValidationStatus
from kpi_evidence.platform import classify_error
from kpi_evidence.registry import lint
from kpi_evidence.store.base import AccessDenied, MetricQueryError

JUNE, MAY = "2026-06-30", "2026-05-31"


def _outcome(outcomes, kpi_id, grain="month", variant="default", as_of=JUNE):
    matches = [o for k, o in outcomes.items() if k.startswith(f"{kpi_id}|{grain}|{variant}|{as_of}|")]
    assert len(matches) == 1, f"no case {kpi_id} {grain} {variant} {as_of}"
    return matches[0]


def _value(outcomes, *args, **kwargs) -> float:
    outcome = _outcome(outcomes, *args, **kwargs)
    assert outcome.error is None, f"{args} raised {outcome.error!r}"
    return outcome.value


def test_registry_lints_clean(platform_registry):
    assert lint(platform_registry) == []


def test_every_case_computes_or_fails_for_a_known_reason(all_cases, duckdb_outcomes):
    unexpected = []
    for case in all_cases:
        outcome = duckdb_outcomes[case.key]
        if outcome.error is None:
            if not math.isfinite(outcome.value):
                unexpected.append((case.key, "non-finite"))
            continue
        status, code, _ = classify_error(outcome.error)
        if case.kpi_id == "exfactory_units":
            expected = (ValidationStatus.MISSING_ACCESS, ReasonCode.PERMISSION_DENIED)
        elif case.uses_panel and case.as_of == JUNE and case.grain == "month":
            expected = (ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.NO_DATA_FOR_PERIOD)
        else:
            expected = None
        if (status, code) != expected:
            unexpected.append((case.key, status.value, code, str(outcome.error)[:120]))
    assert not unexpected, unexpected
    assert len({c.kpi_id for c in all_cases}) == 13
    assert len(all_cases) >= 150


@pytest.mark.parametrize("kpi_id, variant, expected, tol", [
    ("trx", "default", 5783.041, 5e-4),
    ("trx_share", "default", 0.0917, 5e-5),
    ("trx_share", "basket=il17_class", 0.2621, 5e-5),
    ("call_attainment", "default", 0.8442, 5e-5),
    ("call_attainment", "eligibility=writers_only", 0.9192, 5e-5),
    ("call_attainment", "aggregation=mean_of_ratios", 0.8576, 5e-5),
    ("referrals_in", "default", 147.0, 1e-9),
    ("referrals_in", "projection=projected", 183.75, 1e-9),
    ("speaker_attendance", "default", 64.0, 1e-9),
    ("speaker_attendance", "measure=distinct_hcps", 47.0, 1e-9),
    ("nrx", "default", 1122.263, 5e-4),
    ("nbrx", "default", 782.454, 5e-4),
    ("calls", "default", 1163.0, 1e-9),
    ("call_plan", "default", 1377.6, 1e-6),
    ("samples", "default", 2476.0, 1e-9),
    ("new_writers", "default", 2.0, 1e-9),
    ("active_referrers", "default", 129.0, 1e-9),
])
def test_june_2026_reference_values(duckdb_outcomes, kpi_id, variant, expected, tol):
    assert _value(duckdb_outcomes, kpi_id, "month", variant) == pytest.approx(expected, abs=tol)


def test_q2_trx_and_panel_lag_trap(duckdb_outcomes, platform_store, platform_settings):
    account_q2 = _value(duckdb_outcomes, "trx", "quarter")
    panel_q2 = _value(duckdb_outcomes, "trx", "quarter", "source=panel")
    assert account_q2 == pytest.approx(17220.318, abs=5e-4)
    assert panel_q2 == pytest.approx(11783.067, abs=5e-4)
    assert round(panel_q2 / account_q2 - 1, 3) == -0.316
    march = platform_store.query(
        f"SELECT SUM(trx_units) AS v FROM {platform_settings.gov_schema}.gv_rx_panel_monthly "
        f"WHERE month = '2026-03'").iloc[0, 0]
    panel_q_may = _value(duckdb_outcomes, "trx", "quarter", "source=panel", as_of=MAY)
    assert panel_q_may == pytest.approx(panel_q2 + march, rel=1e-12)


def test_panel_month_without_data_is_a_platform_data_issue(duckdb_outcomes):
    outcome = _outcome(duckdb_outcomes, "trx", "month", "source=panel")
    assert classify_error(outcome.error)[:2] == (ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.NO_DATA_FOR_PERIOD)
    assert "period_has_rows" in str(outcome.error)
    assert _value(duckdb_outcomes, "trx", "month", "source=panel", as_of=MAY) > 0


def test_exfactory_is_missing_access(duckdb_outcomes):
    outcome = _outcome(duckdb_outcomes, "exfactory_units")
    assert isinstance(outcome.error, AccessDenied)
    status, code, detail = classify_error(outcome.error)
    assert (status, code) == (ValidationStatus.MISSING_ACCESS, ReasonCode.PERMISSION_DENIED)
    assert "platform_restricted" in detail


def test_exfactory_sql_runs_once_access_is_granted(duckdb_unrestricted_outcomes):
    june = _value(duckdb_unrestricted_outcomes, "exfactory_units")
    trx_june = _value(duckdb_unrestricted_outcomes, "trx")
    assert june == round(trx_june * 1.04)
    q2 = _value(duckdb_unrestricted_outcomes, "exfactory_units", "quarter")
    assert q2 > 3 * june * 0.9


def test_new_writers_warm_up_month_is_null_not_zero(platform_registry, platform_settings, platform_store, case_tools):
    case = case_tools["KpiCase"]("new_writers", "month", "default", (), "2024-07-31")
    outcome = case_tools["evaluate_case"](platform_registry, platform_settings, platform_store.runner(), case)
    assert isinstance(outcome.error, MetricQueryError)
    assert classify_error(outcome.error)[:2] == (ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.NULL_RESULT)


def test_west_region_scope(platform_registry, platform_settings, platform_store, case_tools):
    case = case_tools["KpiCase"]("trx", "month", "default", (("measure", "units"), ("source", "account_feed")),
                                 JUNE, scope=(("region", "West"),))
    outcome = case_tools["evaluate_case"](platform_registry, platform_settings, platform_store.runner(), case)
    assert outcome.value == pytest.approx(1265.7, abs=0.05)


def test_ratio_components_are_recorded(duckdb_outcomes):
    outcome = _outcome(duckdb_outcomes, "trx_share")
    assert set(outcome.components) >= {"numerator", "denominator", "queries"}
    assert outcome.components["numerator"] == pytest.approx(5783.041, abs=5e-4)


def test_restated_panel_months_are_not_double_counted(platform_store, platform_settings, duckdb_outcomes, case_tools,
                                                      platform_registry):
    s = platform_settings
    view = platform_store.query(
        f"SELECT SUM(trx_units) AS v, COUNT(DISTINCT load_id) AS loads FROM {s.gov_schema}.gv_rx_panel_monthly "
        f"WHERE month = '2024-07'").iloc[0]
    base = platform_store.query(
        f"SELECT SUM(trx_units) AS v FROM {s.platform_schema}.rx_panel_monthly WHERE month = '2024-07'").iloc[0]
    current = platform_store.query(
        f"SELECT SUM(trx_units) AS v FROM {s.platform_schema}.rx_panel_monthly "
        f"WHERE month = '2024-07' AND load_id = 'L2026-06'").iloc[0]
    assert int(view["loads"]) == 1
    assert view["v"] == pytest.approx(current["v"], rel=1e-12)
    assert base["v"] == pytest.approx(current["v"] * (1 + 1 / 1.015), rel=1e-6)
    case = case_tools["KpiCase"]("trx", "month", "source=panel", (("measure", "units"), ("source", "panel")), "2024-07-31")
    outcome = case_tools["evaluate_case"](platform_registry, s, platform_store.runner(), case)
    assert outcome.value == pytest.approx(current["v"], rel=1e-12)
