from __future__ import annotations

import json

import pytest
from hints_testkit import run_samples

pytestmark = pytest.mark.slow

V2 = "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx"
EXEC = "Kestrel_Executive_Summary_Q2-2026.pdf"
UPDATE = "kestrel-update.pptx"


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory):
    from kpi_evidence.settings import Settings
    from hints_testkit import ROOT

    settings = Settings.from_env(local_dir=tmp_path_factory.mktemp("hints_e2e") / ".kpi_evidence", ai_mode="off",
                                 registry_dir=ROOT / "registry")
    return run_samples(settings)


def _status(run, file_name, label, *, scope="{}", period=None):
    r = run.results
    rows = r[(r.file_name == file_name) & (r.label == label) & (r.scope_key == scope)]
    if period is not None:
        rows = rows[rows.period_end.astype(str).str.startswith(period)]
    assert len(rows) == 1, rows.to_dict("records")
    return rows.iloc[0]


@pytest.mark.parametrize("file_name, label, scope, period, status, reason", [
    (V2, "TRx", "{}", "2025-12-31", "VERIFIED", "match_under_governed_default"),
    (EXEC, "TRx (retail panel)", "{}", None, "VARIANCE_REVIEW", "single_period_gap_unexplained"),
    (V2, "NBRx", "{}", None, "VERIFIED", "match_under_governed_default"),
    (V2, "NRx", '{"region":"West"}', None, "VERIFIED", "match_under_governed_default"),
    (V2, "TRx share", "{}", None, "VERIFIED", "match_under_governed_default"),
    (V2, "IL-17 class share", "{}", None, "VARIANCE_REVIEW", "single_period_gap_unexplained"),
    (V2, "Call attainment", '{"region":"North"}', None, "VERIFIED", "match_under_governed_default"),
    (V2, "Call attainment (writers only)", "{}", None, "INCOMPATIBLE_DEFINITION", "matches_alternative_definition"),
    (V2, "Calls (details delivered)", "{}", None, "INCOMPATIBLE_DEFINITION", "matches_alternative_definition"),
    (EXEC, "Calls", "{}", None, "INCOMPATIBLE_DEFINITION", "matches_alternative_definition"),
    (UPDATE, "Calls", "{}", None, "INCOMPATIBLE_DEFINITION", "matches_alternative_definition"),
    (V2, "Samples", "{}", None, "VERIFIED", "match_under_governed_default"),
    (V2, "New writers", "{}", None, "VERIFIED", "match_under_governed_default"),
    (V2, "Referrals (projected)", "{}", None, "VARIANCE_REVIEW", "single_period_gap_unexplained"),
    (V2, "Ex-factory units", "{}", None, "MISSING_ACCESS", "permission_denied"),
    (V2, "Brand awareness (aided)", "{}", None, "REPORT_ONLY", "no_platform_definition"),
    (V2, "Share of voice", "{}", None, "REPORT_ONLY", "no_platform_definition"),
    (V2, "12-month persistency", "{}", None, "REPORT_ONLY", "no_platform_definition"),
    (V2, "HCPs attended speaker programs", "{}", None, "INCOMPATIBLE_DEFINITION", "matches_alternative_definition"),
])
def test_statuses(sample_run, file_name, label, scope, period, status, reason):
    row = _status(sample_run, file_name, label, scope=scope, period=period)
    assert (row.validation_status, row.reason_code) == (status, reason), row.reason_detail


def test_hint_definitions_were_used(sample_run):
    hints, registry = sample_run.hints, sample_run.registry
    r = sample_run.results
    for kpi_id in ("trx", "calls", "call_attainment", "referrals_in", "exfactory_units", "brand_awareness"):
        hashes = set(r[r.kpi_id == kpi_id].definition_hash)
        assert hashes == {hints.kpis[kpi_id].definition_hash()}, kpi_id
    assert registry.kpis["speaker_attendance"].raw.get("definition_source") is None


def test_calls_name_the_candidate_that_works(sample_run):
    row = _status(sample_run, V2, "Calls (details delivered)")
    explanation = json.loads(row.explanation)
    assert explanation["matched_choices"] == {"source": "alt_1"}
    assert explanation["primary_status"] == "PLATFORM_DATA_ISSUE"
    assert "platform_demo.rx_account_monthly" in row.reason_detail and row.reason_detail.startswith(
        "The primary definition could not run")


def test_alternatives_explain_reports(sample_run):
    attainment = json.loads(_status(sample_run, V2, "Call attainment (writers only)").explanation)
    assert attainment["matched_choices"]["measure"] == "writers_only"


def test_a_panel_missing_a_month_of_the_quarter_does_not_explain_it(sample_run):
    row = _status(sample_run, EXEC, "TRx (retail panel)")
    panel = json.loads(row.explanation)
    assert "matched_choices" not in panel
    assert panel["incomplete_alternatives"] == {"source=alt_1 (platform_demo.rx_panel_monthly)": "1 month"}
    assert "source=alt_1 (platform_demo.rx_panel_monthly) was not compared" in row.reason_detail


def test_kpi_without_report_values_is_not_compared(sample_run):
    assert "active_referrers" in sample_run.hints.kpis
    assert "active_referrers" not in set(sample_run.results.kpi_id.dropna())
