from __future__ import annotations

from datetime import date

import pytest
from recon_testkit import MONTH_ENDS, make_doc, make_mapping, make_obs, one, run, store_value

from kpi_evidence.compile import compile_kpi
from kpi_evidence.contracts import (
    ExtractionMethod, KpiResult, MappingMethod, MappingStatus, MatchBasis, ReconRun, ReconTrial, ValidationStatus as VS,
)
from kpi_evidence.reconcile import (
    fallback_classify_error, normalize_unit, recheck_observation, scale_factor,
)
from kpi_evidence.store.base import AccessDenied


def platform_value(store, registry, settings, kpi_id, as_of, *, grain="month", choices=None, scope=None):
    compiled = compile_kpi(registry.kpis[kpi_id], brand="KESTREL", scope=scope or {}, grain=grain, settings=settings)
    return compiled.spec.compute(store.runner(), choices or compiled.governed_defaults, as_of.isoformat())[0]


def test_verified_exact(demo_store, registry, settings):
    doc = make_doc(demo_store)
    r = one(demo_store, registry, settings, doc, make_obs(doc, "TRx", 5783.040615), "trx")
    assert r.validation_status == VS.VERIFIED
    assert r.reason_code == "match_under_governed_default"
    assert r.match_basis == MatchBasis.EXACT
    assert r.platform_value == pytest.approx(5783.040615)
    assert r.definition_choices == {"measure": "units", "source": "account_feed"}
    assert r.result_version == 1 and r.is_current and r.recon_run_id and r.sql_artifact_id
    assert "matches the platform" in r.reason_detail


def test_verified_within_rounding_and_tolerance(demo_store, registry, settings):
    doc = make_doc(demo_store)
    rounded = make_obs(doc, "TRx", 5783.0, half=0.5, key="rounded")
    tolerant = make_obs(doc, "TRx", 5800.0, half=0.5, key="tolerant")
    share = make_obs(doc, "TRx share", 0.092, unit="fraction", half=0.0005)
    results = run(demo_store, registry, settings, doc, [(rounded, "trx"), (tolerant, "trx"), (share, "trx_share")])
    assert [(r.validation_status, r.match_basis) for r in results] == [
        (VS.VERIFIED, MatchBasis.WITHIN_ROUNDING), (VS.VERIFIED, MatchBasis.WITHIN_TOLERANCE),
        (VS.VERIFIED, MatchBasis.WITHIN_ROUNDING)]
    assert results[1].tolerance["applied"] == pytest.approx(29.0)
    assert results[2].platform_value == pytest.approx(0.091731, abs=1e-6)


def test_stated_permitted_variant_verified(demo_store, registry, settings):
    doc = make_doc(demo_store)
    obs = make_obs(doc, "IL-17 class share", 0.2621, unit="fraction", half=0.00005, stated="IL-17 class share")
    r = one(demo_store, registry, settings, doc, obs, "trx_share")
    assert r.validation_status == VS.VERIFIED
    assert r.reason_code == "match_under_stated_permitted_variant"
    assert r.definition_choices["basket"] == "il17_class"
    assert r.platform_value == pytest.approx(0.26208, abs=1e-5)


def _with_assumed(registry, report_type, assumed):
    import copy

    out = copy.deepcopy(registry)
    out.report_types[report_type] = {**out.report_types[report_type], "assumed_definitions": assumed}
    return out


def test_report_type_definition_verifies_unstated_value(demo_store, registry, settings):
    doc = make_doc(demo_store)
    obs = make_obs(doc, "TRx share", 0.2621, unit="fraction", half=0.00005)
    plain = one(demo_store, registry, settings, doc, obs, "trx_share", run_id="plain")
    assert plain.validation_status == VS.INCOMPATIBLE_DEFINITION
    assumed = _with_assumed(registry, "monthly_brand_review", {"trx_share": {"basket": "il17_class"}})
    r = one(demo_store, assumed, settings, doc, obs, "trx_share", run_id="assumed")
    assert r.validation_status == VS.VERIFIED
    assert r.reason_code == "match_under_report_type_definition"
    assert r.definition_choices["basket"] == "il17_class"
    assert "the definition this report type uses" in r.reason_detail


def test_stated_text_wins_over_report_type_definition(demo_store, registry, settings):
    doc = make_doc(demo_store)
    assumed = _with_assumed(registry, "monthly_brand_review", {"trx_share": {"basket": "il17_class"}})
    obs = make_obs(doc, "IL-17 class share", 0.2621, unit="fraction", half=0.00005, stated="IL-17 class share")
    r = one(demo_store, assumed, settings, doc, obs, "trx_share")
    assert r.validation_status == VS.VERIFIED
    assert r.reason_code == "match_under_stated_permitted_variant"


def test_report_type_definition_for_another_report_type_is_ignored(demo_store, registry, settings):
    doc = make_doc(demo_store)
    assumed = _with_assumed(registry, "executive_summary", {"trx_share": {"basket": "il17_class"}})
    obs = make_obs(doc, "TRx share", 0.092, unit="fraction", half=0.0005)
    r = one(demo_store, assumed, settings, doc, obs, "trx_share")
    assert r.validation_status == VS.VERIFIED
    assert r.reason_code == "match_under_governed_default"


def test_scope_binding_region(demo_store, registry, settings):
    doc = make_doc(demo_store)
    obs = make_obs(doc, "TRx", 1265.7, half=0.05, scope={"region": "West"})
    r = one(demo_store, registry, settings, doc, obs, "trx")
    assert r.validation_status == VS.VERIFIED and r.scope_key == '{"region":"West"}'


def test_alternative_definition_is_incompatible_never_verified(demo_store, registry, settings):
    doc = make_doc(demo_store)
    obs = make_obs(doc, "Call attainment", 0.9192, unit="fraction", half=0.00005, stated="writers only")
    r = one(demo_store, registry, settings, doc, obs, "call_attainment")
    assert r.validation_status == VS.INCOMPATIBLE_DEFINITION
    assert r.reason_code == "matches_alternative_definition"
    assert "eligibility=writers_only" in r.explanation["alternative"]
    assert r.explanation["differing_options"]["eligibility"]["to"] == "writers_only"
    assert r.explanation["alternative_value"] == pytest.approx(0.91916, abs=1e-5)
    assert r.platform_value == pytest.approx(0.84422, abs=1e-5)
    assert r.definition_choices == {"aggregation": "ratio_of_sums", "eligibility": "all_hcps"}
    assert "eligibility=writers_only" in r.reason_detail


def test_alternative_found_by_search_without_stated_rule(demo_store, registry, settings):
    doc = make_doc(demo_store)
    obs = make_obs(doc, "Speaker attendance", 47.0, half=0.5)
    r = one(demo_store, registry, settings, doc, obs, "speaker_attendance")
    assert r.validation_status == VS.INCOMPATIBLE_DEFINITION
    assert r.explanation["matched_choices"] == {"measure": "distinct_hcps"}


def test_unexplained_variance(demo_store, registry, settings):
    doc = make_doc(demo_store)
    r = one(demo_store, registry, settings, doc, make_obs(doc, "Samples", 2625.0, half=0.5), "samples")
    assert r.validation_status == VS.VARIANCE_REVIEW
    assert r.reason_code == "single_period_gap_unexplained"
    assert r.reason_detail == "Report 6.0% above the platform (platform 2,476; tolerance 0.5%)."
    assert r.rel_diff == pytest.approx(0.0602, abs=1e-4)
    assert r.match_basis is None


def test_systematic_gap_over_six_months_with_holdouts(demo_store, registry, settings):
    doc = make_doc(demo_store)
    pairs = []
    for end in MONTH_ENDS:
        value = round(platform_value(demo_store, registry, settings, "samples", end) * 1.06)
        pairs.append((make_obs(doc, "Samples", float(value), period_end=end, half=0.5), "samples"))
    results = run(demo_store, registry, settings, doc, pairs)
    assert {r.validation_status for r in results} == {VS.VARIANCE_REVIEW}
    assert {r.reason_code for r in results} == {"systematic_gap"}
    run_row = demo_store.get(ReconRun, recon_run_id=results[0].recon_run_id)
    splits = {t["as_of"]: t["split"] for t in run_row.record["targets"]}
    assert [splits[e.isoformat()] for e in MONTH_ENDS] == ["fit"] * 4 + ["holdout"] * 2


def test_multi_period_group_verified_with_holdouts(demo_store, registry, settings):
    doc = make_doc(demo_store)
    pairs = [(make_obs(doc, "TRx", platform_value(demo_store, registry, settings, "trx", end), period_end=end), "trx")
             for end in MONTH_ENDS]
    results = run(demo_store, registry, settings, doc, pairs)
    assert [r.validation_status for r in results] == [VS.VERIFIED] * 6
    assert len({r.recon_run_id for r in results}) == 1
    run_row = demo_store.get(ReconRun, recon_run_id=results[0].recon_run_id)
    assert run_row.phase_a_outcome == "verified" and run_row.phase_b_outcome is None
    assert sum(t["split"] == "holdout" for t in run_row.record["targets"]) == 2
    four = run(demo_store, registry, settings, doc, pairs[:4], run_id="run-4")
    run4 = demo_store.get(ReconRun, recon_run_id=four[0].recon_run_id)
    assert [t["split"] for t in sorted(run4.record["targets"], key=lambda t: t["as_of"])] == ["fit"] * 3 + ["holdout"]


def test_unit_mismatch_before_platform(demo_store, registry, settings):
    doc = make_doc(demo_store)
    r = one(demo_store, registry, settings, doc, make_obs(doc, "TRx share", 9.17, unit="count", half=0.005), "trx_share")
    assert (r.validation_status, r.reason_code) == (VS.INCOMPATIBLE_DEFINITION, "unit_mismatch")
    assert r.recon_run_id is None and r.platform_value is None


def test_scale_mismatch_thousands(demo_store, registry, settings):
    doc = make_doc(demo_store)
    r = one(demo_store, registry, settings, doc, make_obs(doc, "TRx (000s)", 5.78, half=0.005), "trx")
    assert (r.validation_status, r.reason_code) == (VS.INCOMPATIBLE_DEFINITION, "scale_mismatch")
    assert r.explanation["scale_factor"] == 1000.0
    assert "1,000x" in r.reason_detail


def test_scale_factor_helper():
    assert scale_factor(5783.04, 5.78) == 1000.0
    assert scale_factor(0.0917, 9.17) == 0.01
    assert scale_factor(5783.0, 5800.0) is None
    assert scale_factor(10.0, 0.0) is None
    assert normalize_unit("%") == "fraction" and normalize_unit("unknown") is None


def test_missing_access(demo_store, registry, settings):
    doc = make_doc(demo_store)
    r = one(demo_store, registry, settings, doc, make_obs(doc, "Ex-factory units", 6014.0, half=0.5), "exfactory_units")
    assert (r.validation_status, r.reason_code) == (VS.MISSING_ACCESS, "permission_denied")
    trials = demo_store.read(ReconTrial, "recon_run_id = :r", {"r": r.recon_run_id})
    assert trials and all(t.error and "PERMISSION_DENIED" in t.error for t in trials)
    run_row = demo_store.get(ReconRun, recon_run_id=r.recon_run_id)
    assert run_row.phase_a_outcome == "error" and "PERMISSION_DENIED" in run_row.error


def test_null_platform_value_is_data_issue(demo_store, registry, settings):
    doc = make_doc(demo_store)
    r = one(demo_store, registry, settings, doc, make_obs(doc, "New writers", 12.0, period_end=date(2024, 7, 31)),
            "new_writers")
    assert r.validation_status == VS.PLATFORM_DATA_ISSUE
    assert r.reason_code in ("null_result", "no_data_for_period")


def test_one_bad_period_does_not_sink_the_group(demo_store, registry, settings):
    doc = make_doc(demo_store)
    pairs = [(make_obs(doc, "New writers", 12.0, period_end=date(2024, 7, 31)), "new_writers"),
             (make_obs(doc, "New writers", 0.0, period_end=date(2024, 8, 31)), "new_writers")]
    bad, good = run(demo_store, registry, settings, doc, pairs)
    assert bad.validation_status == VS.PLATFORM_DATA_ISSUE
    assert good.validation_status == VS.VERIFIED and good.match_basis == MatchBasis.EXACT


def test_dq_failure_skips_platform(demo_store, registry, settings):
    doc = make_doc(demo_store)
    r = one(demo_store, registry, settings, doc, make_obs(doc, "TRx", 5783.04), "trx",
            dq={"kpi_gov.gv_rx_account_monthly": "fail", "gv_referrals_monthly": "pass"})
    assert (r.validation_status, r.reason_code, r.dq_status) == (VS.PLATFORM_DATA_ISSUE, "dq_failed", "fail")
    assert r.recon_run_id is None
    assert demo_store.count(ReconTrial) == 0
    ok = one(demo_store, registry, settings, doc, make_obs(doc, "Referrals", 147.0, key="r"), "referrals_in",
             dq={"gv_referrals_monthly": "pass"})
    assert ok.validation_status == VS.VERIFIED and ok.dq_status == "pass"


def test_group_crash_becomes_blocked(demo_store, registry, settings, monkeypatch):
    import kpi_evidence.reconcile as rec

    real = rec.compile_kpi

    def boom(kpi, **kw):
        if kpi.kpi_id == "samples":
            raise RuntimeError("registry template exploded")
        return real(kpi, **kw)

    monkeypatch.setattr(rec, "compile_kpi", boom)
    doc = make_doc(demo_store)
    results = run(demo_store, registry, settings, doc, [(make_obs(doc, "Samples", 2476.0), "samples"),
                                                        (make_obs(doc, "TRx", 5783.040615), "trx")])
    assert results[0].validation_status == VS.BLOCKED and "exploded" in results[0].reason_detail
    assert results[1].validation_status == VS.VERIFIED


def test_compile_errors_are_incompatible(demo_store, registry, settings):
    doc = make_doc(demo_store)
    scoped = make_obs(doc, "Ex-factory units", 600.0, scope={"region": "West"})
    weekly = make_obs(doc, "TRx", 1400.0, grain="week")
    a, b = run(demo_store, registry, settings, doc, [(scoped, "exfactory_units"), (weekly, "trx")])
    assert (a.validation_status, a.reason_code) == (VS.INCOMPATIBLE_DEFINITION, "scope_unsupported")
    assert (b.validation_status, b.reason_code) == (VS.INCOMPATIBLE_DEFINITION, "window_unsupported")


def test_fallback_classifier():
    assert fallback_classify_error(AccessDenied("PERMISSION_DENIED: x"))[0] == VS.MISSING_ACCESS
    assert fallback_classify_error(ValueError("quality check 'p' failed: 1 violations"))[1] == "no_data_for_period"
    assert fallback_classify_error(RuntimeError("Catalog Error: Table with name x does not exist!"))[1] == \
        "platform_object_missing"
    assert fallback_classify_error(ZeroDivisionError("odd"))[0] == VS.BLOCKED


def test_report_only_and_causal(demo_store, registry, settings):
    doc = make_doc(demo_store)
    awareness = make_obs(doc, "Brand awareness", 0.62, unit="fraction", half=0.005)
    lift = make_obs(doc, "Speaker program lift", 0.08, unit="fraction", half=0.005)
    a, b = run(demo_store, registry, settings, doc, [(awareness, "brand_awareness"), (lift, "speaker_program_lift")])
    assert (a.validation_status, a.reason_code) == (VS.REPORT_ONLY, "no_platform_definition")
    assert (b.validation_status, b.reason_code) == (VS.REPORT_ONLY, "causal_claim")
    assert a.kpi_id == "brand_awareness" and a.platform_value is None


def test_unmapped_and_missing_mapping_are_captured(demo_store, registry, settings):
    doc = make_doc(demo_store)
    vs_py = make_obs(doc, "vs PY %", 0.041, unit="fraction", flags=[{"code": "DERIVED_CHANGE"}])
    orphan = make_obs(doc, "Mystery number", 12.0)
    unclear = make_obs(doc, "Blurry", None, flags=[{"code": "IMAGE_NOT_PARSED", "dimension": "value"}])
    a, b, c = run(demo_store, registry, settings, doc, [
        (vs_py, make_mapping(vs_py, None, status=MappingStatus.NO_MATCH)), (orphan, None),
        (unclear, make_mapping(unclear, None, status=MappingStatus.NO_MATCH))])
    assert (a.validation_status, a.reason_code, a.kpi_id) == (VS.UNMAPPED_CAPTURED, "no_registry_match", None)
    assert b.validation_status == VS.UNMAPPED_CAPTURED
    assert (c.validation_status, c.reason_code) == (VS.UNCERTAIN_EXTRACTION, "image_not_parsed")


def test_ambiguous_mapping(demo_store, registry, settings):
    doc = make_doc(demo_store)
    share = make_obs(doc, "share", 0.262, unit="fraction")
    model = make_obs(doc, "Reach", 0.7, unit="fraction")
    cands = [{"kpi_id": "trx_share", "method": "SYNONYM"}, {"kpi_id": "share_of_voice", "method": "SYNONYM"}]
    a, b = run(demo_store, registry, settings, doc, [
        (share, make_mapping(share, None, status=MappingStatus.AMBIGUOUS, candidates=cands, reason="tie")),
        (model, make_mapping(model, "call_attainment", status=MappingStatus.AMBIGUOUS, method=MappingMethod.MODEL))])
    assert (a.validation_status, a.reason_code) == (VS.AMBIGUOUS_MAPPING, "multiple_candidates")
    assert "share_of_voice" in a.reason_detail and a.kpi_id is None
    assert (b.validation_status, b.reason_code, b.kpi_id) == (VS.AMBIGUOUS_MAPPING, "model_proposal_only",
                                                              "call_attainment")


def test_uncertain_extraction_gate(demo_store, registry, settings):
    doc = make_doc(demo_store)
    low = make_obs(doc, "TRx", 5783.0, confidence=0.4, key="low")
    image = make_obs(doc, "TRx", None, key="image")
    store_value(demo_store, image, ExtractionMethod.IMAGE_PLACEHOLDER)
    no_period = make_obs(doc, "TRx", 5783.0, period_end=None, grain="unknown", key="np")
    results = run(demo_store, registry, settings, doc, [(low, "trx"), (image, "trx"), (no_period, "trx")])
    assert [(r.validation_status, r.reason_code) for r in results] == [
        (VS.UNCERTAIN_EXTRACTION, "low_confidence_value"), (VS.UNCERTAIN_EXTRACTION, "image_not_parsed"),
        (VS.UNCERTAIN_EXTRACTION, "period_unknown")]
    assert results[1].extraction_method == ExtractionMethod.IMAGE_PLACEHOLDER


class _BrokenProvider:
    name = "gateway"

    def __init__(self):
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        raise TimeoutError("model endpoint timed out")


class _HeuristicProvider:
    name = "local"

    def complete(self, prompt: str) -> str:
        raise AssertionError("the local heuristic provider is not a judge")


def test_llm_judge_failure_falls_back_to_stated_rule(demo_store, registry, settings):
    provider = _BrokenProvider()
    doc = make_doc(demo_store)
    obs = make_obs(doc, "Call attainment", 0.9192, unit="fraction", half=0.00005, stated="writers only")
    r = one(demo_store, registry, settings, doc, obs, "call_attainment", provider=provider)
    assert r.validation_status == VS.INCOMPATIBLE_DEFINITION and provider.calls >= 1
    run_row = demo_store.get(ReconRun, recon_run_id=r.recon_run_id)
    assert run_row.judge == "llm:gateway+stated_rule"
    assert run_row.record["judge_calls"][0]["error"].startswith("TimeoutError")


def test_providers_without_a_model_use_the_deterministic_judge(demo_store, registry, settings):
    doc = make_doc(demo_store)
    obs = make_obs(doc, "Call attainment", 0.9192, unit="fraction", half=0.00005, stated="writers only")
    r = one(demo_store, registry, settings, doc, obs, "call_attainment", provider=_HeuristicProvider())
    assert demo_store.get(ReconRun, recon_run_id=r.recon_run_id).judge == "stated_rule"
    settings.ai_mode = "off"
    r2 = one(demo_store, registry, settings, doc, obs, "call_attainment", provider=_BrokenProvider(), run_id="r2")
    assert demo_store.get(ReconRun, recon_run_id=r2.recon_run_id).judge == "stated_rule"


def test_recheck_observation_with_corrected_kpi(demo_store, registry, settings):
    doc = make_doc(demo_store)
    obs = make_obs(doc, "Scripts", 5783.04, half=0.005)
    first = one(demo_store, registry, settings, doc, obs, make_mapping(obs, None, status=MappingStatus.NO_MATCH))
    assert first.validation_status == VS.UNMAPPED_CAPTURED
    fixed = recheck_observation(demo_store, registry, settings, obs, "trx", "run-review")
    assert fixed.validation_status == VS.VERIFIED and fixed.kpi_id == "trx"
    assert demo_store.get(KpiResult, result_id=first.result_id).is_current
