from __future__ import annotations

import pytest

from kpi_evidence import review
from kpi_evidence.contracts import (DecisionSource, ExtractedValue, ExtractionMethod, KpiMapping, KpiResult,
                                    MappingMethod, MappingStatus, Observation, Publication, PublicationLabel,
                                    ReviewAction, ReviewDecision, ReviewEvent)
from kpi_evidence.contracts import ValidationStatus as S
from kpi_evidence.review import DecisionError, StaleResult, decide


def _events(store, kind):
    return store.read(ReviewEvent, "event = :e", {"e": kind})


def test_accept_verified_without_rationale_publishes_matches_platform(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VERIFIED, page=2)
    seeder.recon(s)
    out = decide(store, registry, settings, s.result.result_id, ReviewAction.ACCEPT, "", "rev@example.com",
                 request_id="req-7", decision_id="dec-1", result_version=1)
    pub = out.publication
    assert out.message == "Published as Matches platform."
    assert pub.display_label == PublicationLabel.MATCHES_PLATFORM and pub.review_action == ReviewAction.ACCEPT
    assert pub.value == 5910.0 and pub.platform_value == 5909.8 and pub.approved_by == "rev@example.com"
    assert pub.evidence_uri == s.doc.file_path and pub.evidence_hash == s.doc.doc_id
    assert pub.evidence_locator["page"] == 2 and pub.evidence_locator["shape_id"] == 4
    assert (pub.harness_metric_id, pub.harness_variant_id, pub.harness_source) == ("trx", "units", "source_a")
    assert pub.period_label == "May 2026" and pub.tolerance["rel"] == 0.005
    assert pub.sql_artifact_id == s.result.sql_artifact_id and pub.sql_sha == "sha-artifact"
    decision = store.get(ReviewDecision, decision_id="dec-1")
    assert decision.decision_source == DecisionSource.HUMAN and decision.request_id == "req-7"
    assert decision.evidence_hash == review.evidence_hash(s.result)
    decided, published = _events(store, "decided"), _events(store, "published")
    assert len(decided) == 1 and len(published) == 1
    assert decided[0].request_id == "req-7" and published[0].request_id == "req-7"
    assert decided[0].detail["platform_snapshot"] == {"gv_rx_account_monthly": "v1"}


@pytest.mark.parametrize("status,label", [
    (S.VARIANCE_REVIEW, PublicationLabel.ACCEPTED_VARIANCE),
    (S.INCOMPATIBLE_DEFINITION, PublicationLabel.ACCEPTED_VARIANCE),
    (S.REPORT_CONFLICT, PublicationLabel.ACCEPTED_VARIANCE),
])
def test_accept_variance_needs_rationale_and_publishes_accepted_variance(seeder, store, registry, settings, status, label):
    s = seeder.seed("samples", status, report_value=1060, platform_value=1000)
    with pytest.raises(DecisionError, match="rationale"):
        decide(store, registry, settings, s.result.result_id, "ACCEPT", "  ", "rev@example.com")
    out = decide(store, registry, settings, s.result.result_id, "accept", "Field team counts drop-offs", "rev@example.com")
    assert out.publication.display_label == label
    assert out.publication.rationale == "Field team counts drop-offs"
    assert out.publication.variance_rel == pytest.approx(0.06)


def test_accept_report_only(seeder, store, registry, settings):
    s = seeder.seed("brand_awareness", S.REPORT_ONLY, unit="fraction", report_value=0.62, platform_value=None)
    out = decide(store, registry, settings, s.result.result_id, "ACCEPT", "Survey wave Q2", "rev@example.com")
    assert out.publication.display_label == PublicationLabel.REPORT_ONLY
    assert out.publication.harness_metric_id is None and out.publication.platform_value is None


@pytest.mark.parametrize("status", [S.UNMAPPED_CAPTURED, S.UNCERTAIN_EXTRACTION, S.AMBIGUOUS_MAPPING,
                                    S.MISSING_ACCESS, S.PLATFORM_DATA_ISSUE, S.BLOCKED])
def test_accept_is_refused_where_a_person_must_correct_first(seeder, store, registry, settings, status):
    kpi = None if status == S.UNMAPPED_CAPTURED else "trx"
    s = seeder.seed(kpi, status, platform_value=None)
    with pytest.raises(DecisionError):
        decide(store, registry, settings, s.result.result_id, "ACCEPT", "looks fine", "rev@example.com")
    assert store.count(ReviewDecision) == 0 and store.count(Publication) == 0


def test_actor_is_required(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VERIFIED)
    with pytest.raises(DecisionError):
        decide(store, registry, settings, s.result.result_id, "ACCEPT", "", "  ")


def test_unknown_result_raises_key_error(store, registry, settings):
    with pytest.raises(KeyError):
        decide(store, registry, settings, "nope", "ACCEPT", "x", "rev@example.com")


def test_stale_version_is_refused(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VERIFIED)
    s.result.result_version = 2
    store.upsert([s.result])
    with pytest.raises(StaleResult):
        decide(store, registry, settings, s.result.result_id, "ACCEPT", "", "rev@example.com", result_version=1)
    assert store.count(ReviewDecision) == 0


def test_not_current_result_is_stale(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VERIFIED, is_current=False)
    with pytest.raises(StaleResult):
        decide(store, registry, settings, s.result.result_id, "ACCEPT", "", "rev@example.com")


def test_double_click_is_a_no_op(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VERIFIED)
    first = decide(store, registry, settings, s.result.result_id, "ACCEPT", "", "rev@example.com", decision_id="form-1")
    second = decide(store, registry, settings, s.result.result_id, "ACCEPT", "", "rev@example.com", decision_id="form-1")
    assert second.replayed and not first.replayed
    assert second.publication.publication_id == first.publication.publication_id
    assert store.count(ReviewDecision) == 1 and store.count(Publication) == 1
    assert len(_events(store, "decided")) == 1 and len(_events(store, "published")) == 1


def test_reject_never_publishes(seeder, store, registry, settings):
    s = seeder.seed("samples", S.VARIANCE_REVIEW)
    with pytest.raises(DecisionError):
        decide(store, registry, settings, s.result.result_id, "REJECT", "", "rev@example.com")
    out = decide(store, registry, settings, s.result.result_id, "REJECT", "Not a KPI value", "rev@example.com")
    assert out.publication is None and out.retracted == []
    assert store.count(Publication) == 0


def test_reject_after_accept_retracts_the_publication(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VERIFIED)
    accepted = decide(store, registry, settings, s.result.result_id, "ACCEPT", "", "rev@example.com")
    out = decide(store, registry, settings, s.result.result_id, "REJECT", "Deck was a draft", "lead@example.com",
                 request_id="req-9", decision_id="rej-1")
    assert [p.publication_id for p in out.retracted] == [accepted.publication.publication_id]
    pub = store.get(Publication, publication_id=accepted.publication.publication_id)
    assert pub.retracted is True
    assert store.count(Publication) == 1
    retracted = _events(store, "retracted")
    assert len(retracted) == 1 and retracted[0].request_id == "req-9"
    replay = decide(store, registry, settings, s.result.result_id, "REJECT", "Deck was a draft", "lead@example.com",
                    decision_id="rej-1")
    assert replay.replayed and [p.publication_id for p in replay.retracted] == [pub.publication_id]


def test_correct_value_rechecks_and_auto_publishes_when_platform_agrees(seeder, store, registry, settings, fake_siblings):
    s = seeder.seed("trx", S.VARIANCE_REVIEW, report_value=5190.0, platform_value=5909.8,
                    context={"hidden": False}, value_flags=[{"code": "LOW_OCR", "dimension": "extraction", "detail": ""},
                                                            {"code": "HIDDEN_X", "dimension": "meaning", "detail": ""}])
    fake_siblings.recheck_status = S.VERIFIED
    out = decide(store, registry, settings, s.result.result_id, "CORRECT", "Typo: digits swapped", "rev@example.com",
                 corrected_value=5910.0, decision_id="cor-1", request_id="req-c")
    new_obs = store.get(Observation, observation_id=out.new_result.observation_id)
    new_val = store.get(ExtractedValue, value_id=new_obs.value_id)
    assert new_val.method == ExtractionMethod.REVIEWER_CORRECTION and new_val.raw_number == 5910.0
    assert new_val.context["value_corrected"] is True and new_val.context["corrected_by"] == "rev@example.com"
    assert new_val.slide_or_page == s.value.slide_or_page
    assert {f["code"] for f in new_val.flags} == {"HIDDEN_X", "REVIEWER_CORRECTION"}
    assert new_obs.observation_id != s.obs.observation_id and new_obs.value == 5910.0
    for name in ("period_start", "period_end", "period_grain", "scope_key", "fingerprint", "label", "brand"):
        assert getattr(new_obs, name) == getattr(s.obs, name)
    mapping = store.get(KpiMapping, observation_id=new_obs.observation_id, registry_version=registry.version)
    assert mapping.method == MappingMethod.REVIEWER and mapping.status == MappingStatus.ACCEPTED
    assert mapping.kpi_id == "trx" and mapping.decided_by == "rev@example.com"
    assert fake_siblings.remember_calls == []
    assert fake_siblings.recheck_calls == [(new_obs.observation_id, "trx", "review-cor-1")]
    assert out.new_result.validation_status == S.VERIFIED
    assert out.publication is not None and out.publication.display_label == PublicationLabel.MATCHES_PLATFORM
    assert out.publication.review_action == ReviewAction.CORRECT
    assert out.publication.approved_by == "system:auto-policy@v1"
    assert "corrected by rev@example.com" in out.publication.rationale
    assert "now Matches platform" in out.message
    original = store.get(KpiResult, result_id=s.result.result_id)
    assert original.is_current is False
    decision = store.get(ReviewDecision, decision_id="cor-1")
    assert decision.corrected_value == 5910.0 and decision.action == ReviewAction.CORRECT
    again = decide(store, registry, settings, s.result.result_id, "CORRECT", "Typo: digits swapped", "rev@example.com",
                   corrected_value=5910.0, decision_id="cor-1")
    assert again.replayed and again.new_result.result_id == out.new_result.result_id
    assert len(fake_siblings.recheck_calls) == 1


def test_correct_kpi_of_unmapped_value_remembers_mapping_then_accept(seeder, store, registry, settings, fake_siblings):
    s = seeder.seed(None, S.UNMAPPED_CAPTURED, mapping_method=None, mapping_status=MappingStatus.NO_MATCH,
                    unit="fraction", report_value=0.62, platform_value=None, label="Aided awareness",
                    fingerprint="fp-mr-table")
    with pytest.raises(DecisionError, match="Correct"):
        decide(store, registry, settings, s.result.result_id, "ACCEPT", "it's awareness", "rev@example.com")
    with pytest.raises(DecisionError, match="Choose the KPI"):
        decide(store, registry, settings, s.result.result_id, "CORRECT", "value check", "rev@example.com",
               corrected_value=0.62)
    fake_siblings.recheck_status = S.REPORT_ONLY
    fake_siblings.recheck_platform = None
    out = decide(store, registry, settings, s.result.result_id, "CORRECT", "Market research awareness",
                 "rev@example.com", corrected_kpi_id="brand_awareness", decision_id="cor-2")
    assert len(fake_siblings.remember_calls) == 1
    obs_id, kpi_id, actor, decision_id = fake_siblings.remember_calls[0]
    assert (kpi_id, actor, decision_id) == ("brand_awareness", "rev@example.com", "cor-2")
    assert out.new_result.validation_status == S.REPORT_ONLY
    assert out.publication is None and "waits for a decision" in out.message
    new_val = store.get(ExtractedValue, value_id=store.get(Observation, observation_id=obs_id).value_id)
    assert new_val.context["value_corrected"] is False and new_val.raw_number == 0.62
    accepted = decide(store, registry, settings, out.new_result.result_id, "ACCEPT", "Survey wave", "rev@example.com")
    assert accepted.publication.display_label == PublicationLabel.REPORT_ONLY
    assert accepted.publication.review_action == ReviewAction.CORRECT


def test_corrected_value_with_remaining_variance_publishes_as_corrected(seeder, store, registry, settings, fake_siblings):
    s = seeder.seed("samples", S.VARIANCE_REVIEW, report_value=1600, platform_value=1000)
    fake_siblings.recheck_status = S.VARIANCE_REVIEW
    fake_siblings.recheck_platform = 1000.0
    out = decide(store, registry, settings, s.result.result_id, "CORRECT", "Deck showed cumulative", "rev@example.com",
                 corrected_value=1060.0)
    assert out.publication is None and out.new_result.validation_status == S.VARIANCE_REVIEW
    accepted = decide(store, registry, settings, out.new_result.result_id, "ACCEPT", "Drop-offs counted",
                      "rev@example.com")
    assert accepted.publication.display_label == PublicationLabel.CORRECTED
    assert accepted.publication.value == 1060.0


def test_correct_retracts_the_original_publication(seeder, store, registry, settings, fake_siblings):
    s = seeder.seed("trx", S.VERIFIED)
    first = decide(store, registry, settings, s.result.result_id, "ACCEPT", "", "rev@example.com")
    fake_siblings.recheck_status = S.VARIANCE_REVIEW
    out = decide(store, registry, settings, s.result.result_id, "CORRECT", "Wrong month", "lead@example.com",
                 corrected_value=5800.0)
    assert [p.publication_id for p in out.retracted] == [first.publication.publication_id]
    assert store.get(Publication, publication_id=first.publication.publication_id).retracted is True


def test_correct_validation(seeder, store, registry, settings, fake_siblings):
    s = seeder.seed("trx", S.VARIANCE_REVIEW)
    with pytest.raises(DecisionError, match="corrected value"):
        decide(store, registry, settings, s.result.result_id, "CORRECT", "x", "rev@example.com")
    with pytest.raises(DecisionError, match="Unknown KPI"):
        decide(store, registry, settings, s.result.result_id, "CORRECT", "x", "rev@example.com", corrected_kpi_id="nope")
    with pytest.raises(DecisionError, match="finite"):
        decide(store, registry, settings, s.result.result_id, "CORRECT", "x", "rev@example.com",
               corrected_value=float("nan"))
    assert fake_siblings.recheck_calls == [] and store.count(ReviewDecision) == 0


def test_failed_recheck_records_no_decision(seeder, store, registry, settings, fake_siblings):
    s = seeder.seed("trx", S.VARIANCE_REVIEW)
    fake_siblings.raise_on_recheck = RuntimeError("warehouse down")
    with pytest.raises(RuntimeError):
        decide(store, registry, settings, s.result.result_id, "CORRECT", "x", "rev@example.com", corrected_value=1.0)
    assert store.count(ReviewDecision) == 0
    assert store.get(KpiResult, result_id=s.result.result_id).is_current is True
