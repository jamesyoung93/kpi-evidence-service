from __future__ import annotations

import dataclasses

import pytest

from kpi_evidence import review
from kpi_evidence.contracts import (DecisionSource, DocumentStatus, ExtractionMethod, MappingMethod, MappingStatus,
                                    Publication, PublicationLabel, ReviewAction, ReviewDecision, ReviewEvent)
from kpi_evidence.contracts import ValidationStatus as S
from kpi_evidence.ids import utcnow
from kpi_evidence.review import DecisionError, auto_publish, decide, policy_checks, publish, retract
from kpi_evidence.serving import published_current


def _decision(result, decision_id, actor="rev@example.com"):
    return ReviewDecision(decision_id=decision_id, result_id=result.result_id, result_version=result.result_version,
                          action=ReviewAction.ACCEPT, rationale="ok", decided_by=actor,
                          decision_source=DecisionSource.HUMAN, decided_at=utcnow(),
                          evidence_hash=review.evidence_hash(result))


def test_new_value_for_the_same_natural_key_supersedes_the_previous(seeder, store, registry, settings):
    v1 = seeder.seed("trx", S.VERIFIED, doc=seeder.doc("deck_v1.pptx"), report_value=5910.0)
    v2 = seeder.seed("trx", S.VERIFIED, doc=seeder.doc("deck_v2.pptx"), report_value=5912.0)
    p1 = decide(store, registry, settings, v1.result.result_id, "ACCEPT", "", "rev@example.com").publication
    p2 = decide(store, registry, settings, v2.result.result_id, "ACCEPT", "", "rev@example.com").publication
    assert p1.publication_id != p2.publication_id
    assert store.get(Publication, publication_id=p1.publication_id).superseded_by == p2.publication_id
    current = published_current(store, "KESTREL")
    assert list(current["publication_id"]) == [p2.publication_id] and current.iloc[0]["value"] == 5912.0
    assert store.count(Publication) == 2
    event = store.read(ReviewEvent, "event = 'published' AND result_id = :r", {"r": v2.result.result_id})[0]
    assert event.detail["superseded"] == [p1.publication_id]


def test_different_scope_or_period_does_not_supersede(seeder, store, registry, settings):
    a = seeder.seed("trx", S.VERIFIED)
    b = seeder.seed("trx", S.VERIFIED, scope={"region": "West"}, report_value=1265.7, platform_value=1265.7)
    c = seeder.seed("trx", S.VERIFIED, period_end=a.result.period_end.replace(day=30, month=4))
    for s in (a, b, c):
        decide(store, registry, settings, s.result.result_id, "ACCEPT", "", "rev@example.com")
    assert len(published_current(store)) == 3


def test_publish_is_idempotent_on_publication_id(seeder, store, registry):
    s = seeder.seed("trx", S.VERIFIED)
    d = _decision(s.result, "dec-x")
    first = publish(store, registry, s.result, d)
    second = publish(store, registry, s.result, d)
    assert first.publication_id == second.publication_id and store.count(Publication) == 1
    assert len(store.read(ReviewEvent, "event = 'published'")) == 1


def test_publish_needs_brand_and_kpi(seeder, store, registry):
    s = seeder.seed("trx", S.VERIFIED, brand=None, doc=seeder.doc("nobrand.pptx", brand=None))
    with pytest.raises(DecisionError, match="brand"):
        publish(store, registry, s.result, _decision(s.result, "d1"))
    u = seeder.seed(None, S.UNMAPPED_CAPTURED, mapping_method=None, mapping_status=MappingStatus.NO_MATCH)
    with pytest.raises(DecisionError, match="KPI"):
        publish(store, registry, u.result, _decision(u.result, "d2"))


@pytest.mark.parametrize("status,platform,corrected,label", [
    (S.VERIFIED, 1.0, False, PublicationLabel.MATCHES_PLATFORM),
    (S.VERIFIED, 1.0, True, PublicationLabel.MATCHES_PLATFORM),
    (S.VARIANCE_REVIEW, 1.0, False, PublicationLabel.ACCEPTED_VARIANCE),
    (S.VARIANCE_REVIEW, 1.0, True, PublicationLabel.CORRECTED),
    (S.INCOMPATIBLE_DEFINITION, 1.0, False, PublicationLabel.ACCEPTED_VARIANCE),
    (S.REPORT_ONLY, None, False, PublicationLabel.REPORT_ONLY),
    (S.REPORT_CONFLICT, None, False, PublicationLabel.REPORT_ONLY),
    (S.REPORT_CONFLICT, 1.0, False, PublicationLabel.ACCEPTED_VARIANCE),
])
def test_display_label(seeder, status, platform, corrected, label):
    s = seeder.seed("trx", status, platform_value=platform)
    assert review.display_label(s.result, corrected) == label


def test_display_label_refuses_blocked_statuses(seeder):
    s = seeder.seed("trx", S.MISSING_ACCESS, platform_value=None)
    with pytest.raises(DecisionError):
        review.display_label(s.result)


def test_retract_marks_the_same_row_and_logs_an_event(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VERIFIED)
    pub = decide(store, registry, settings, s.result.result_id, "ACCEPT", "", "rev@example.com").publication
    with pytest.raises(DecisionError):
        retract(store, pub.publication_id, "lead@example.com", "")
    out = retract(store, pub.publication_id, "lead@example.com", "Deck withdrawn", request_id="req-r")
    assert out.retracted is True and store.count(Publication) == 1
    assert published_current(store).empty
    again = retract(store, pub.publication_id, "lead@example.com", "Deck withdrawn")
    assert again.retracted is True
    events = store.read(ReviewEvent, "event = 'retracted'")
    assert len(events) == 1 and events[0].request_id == "req-r" and events[0].detail["rationale"] == "Deck withdrawn"
    with pytest.raises(KeyError):
        retract(store, "missing", "lead@example.com", "x")


def test_published_current_without_the_view_matches_the_view(seeder, store, registry, settings):
    a = seeder.seed("trx", S.VERIFIED, doc=seeder.doc("v1.pptx"))
    b = seeder.seed("trx", S.VERIFIED, doc=seeder.doc("v2.pptx"), report_value=5911.0)
    c = seeder.seed("nrx", S.VERIFIED)
    for s in (a, b, c):
        decide(store, registry, settings, s.result.result_id, "ACCEPT", "", "rev@example.com")
    with_view = published_current(store, "KESTREL")
    store.execute(f"DROP VIEW IF EXISTS {store.fq('published_kpis_current')}")
    without = published_current(store, "KESTREL")
    assert sorted(with_view["publication_id"]) == sorted(without["publication_id"])
    assert len(without) == 2


def _auto(store, registry, settings, *results):
    return auto_publish(store, registry, settings, [r.result for r in results], "run-1")


def test_auto_publish_verified_structured_synonym_dq_pass(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VERIFIED, method=ExtractionMethod.NATIVE_TABLE)
    pubs = _auto(store, registry, settings, s)
    assert len(pubs) == 1 and pubs[0].display_label == PublicationLabel.MATCHES_PLATFORM
    decision = store.get(ReviewDecision, decision_id=pubs[0].decision_id)
    assert decision.decided_by == "system:auto-policy@v1" and decision.decision_source == DecisionSource.AUTO_POLICY
    assert decision.policy_version == "v1" and decision.request_id == "run-1"
    assert pubs[0].approved_by == "system:auto-policy@v1"
    assert _auto(store, registry, settings, s) == []
    assert store.count(Publication) == 1 and store.count(ReviewDecision) == 1


@pytest.mark.parametrize("kwargs,reason", [
    ({"method": ExtractionMethod.PDF_TEXT}, "extraction_not_structured"),
    ({"method": ExtractionMethod.SLIDE_TEXT}, "extraction_not_structured"),
    ({"method": ExtractionMethod.AI_PARSE}, "extraction_not_structured"),
    ({"mapping_method": MappingMethod.MODEL}, "mapping_method_not_allowed"),
    ({"mapping_method": MappingMethod.SYNONYM, "mapping_status": MappingStatus.AMBIGUOUS}, "mapping_method_not_allowed"),
    ({"dq_status": "fail"}, "dq_not_passed"),
    ({"dq_status": "not_checked"}, "dq_not_passed"),
    ({"context": {"hidden": True}}, "hidden_slide"),
    ({"value_flags": [{"code": "HIDDEN_SLIDE", "dimension": "association", "detail": ""}]}, "hidden_slide"),
    ({"is_current": False}, "not_current"),
])
def test_auto_publish_conditions(seeder, store, registry, settings, kwargs, reason):
    s = seeder.seed("trx", S.VERIFIED, **kwargs)
    check = policy_checks(store, registry, [s.result])[0]
    assert not check.eligible and reason in check.reasons
    assert _auto(store, registry, settings, s) == []
    assert store.count(Publication) == 0


def test_auto_publish_skips_hidden_page_and_superseded_document(seeder, store, registry, settings):
    doc = seeder.doc("hidden.pptx")
    seeder.page(doc, 5, hidden=True)
    hidden = seeder.seed("trx", S.VERIFIED, doc=doc, page=5)
    old = seeder.doc("old.pptx", status=DocumentStatus.SUPERSEDED, superseded_by="newer")
    superseded = seeder.seed("trx", S.VERIFIED, doc=old)
    checks = {c.result_id: c for c in policy_checks(store, registry, [hidden.result, superseded.result])}
    assert "hidden_slide" in checks[hidden.result.result_id].reasons
    assert "document_superseded" in checks[superseded.result.result_id].reasons
    assert _auto(store, registry, settings, hidden, superseded) == []


def test_auto_publish_skips_decided_results(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VERIFIED)
    decide(store, registry, settings, s.result.result_id, "REJECT", "draft deck", "rev@example.com")
    check = policy_checks(store, registry, [s.result])[0]
    assert "already_decided" in check.reasons and _auto(store, registry, settings, s) == []


def test_auto_publish_never_takes_review_statuses(seeder, store, registry, settings):
    seeded = [seeder.seed("samples", st) for st in (S.VARIANCE_REVIEW, S.INCOMPATIBLE_DEFINITION, S.REPORT_CONFLICT,
                                                    S.UNCERTAIN_EXTRACTION, S.MISSING_ACCESS)]
    assert _auto(store, registry, settings, *seeded) == []
    assert all(c.reasons == ["status_needs_person"] for c in policy_checks(store, registry, [s.result for s in seeded]))


def test_auto_publish_respects_a_disabled_policy(seeder, store, registry, settings):
    off = dataclasses.replace(registry, policy={**registry.policy,
                                                "auto_publish": {**registry.policy["auto_publish"], "verified": False}})
    s = seeder.seed("trx", S.VERIFIED)
    assert auto_publish(store, off, settings, [s.result], "run") == []
    assert "policy_disabled" in policy_checks(store, off, [s.result])[0].reasons


def test_policy_version_names_the_decider(seeder, store, registry, settings):
    v2 = dataclasses.replace(registry, policy={**registry.policy, "version": "v2"})
    s = seeder.seed("trx", S.VERIFIED)
    pub = auto_publish(store, v2, settings, [s.result], "run")[0]
    assert pub.approved_by == "system:auto-policy@v2"


def test_report_only_needs_an_earlier_human_accept_of_the_layout(seeder, store, registry, settings):
    kwargs = dict(unit="fraction", report_value=0.62, platform_value=None, dq_status="not_checked",
                  method=ExtractionMethod.NATIVE_TABLE, fingerprint="fp-mr", label="Brand awareness")
    first = seeder.seed("brand_awareness", S.REPORT_ONLY, doc=seeder.doc("may.pptx"), **kwargs)
    assert _auto(store, registry, settings, first) == []
    assert "no_prior_human_accept" in policy_checks(store, registry, [first.result])[0].reasons
    decide(store, registry, settings, first.result.result_id, "ACCEPT", "Survey wave 2", "rev@example.com")
    nxt = seeder.seed("brand_awareness", S.REPORT_ONLY, doc=seeder.doc("june.pptx"),
                      period_end=first.result.period_end.replace(month=6, day=30), **kwargs)
    pubs = _auto(store, registry, settings, nxt)
    assert len(pubs) == 1 and pubs[0].display_label == PublicationLabel.REPORT_ONLY
    other_layout = seeder.seed("brand_awareness", S.REPORT_ONLY, doc=seeder.doc("other.pptx"),
                               **{**kwargs, "fingerprint": "fp-other"})
    assert _auto(store, registry, settings, other_layout) == []


def test_auto_publish_failure_is_recorded_not_raised(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VERIFIED)
    check = policy_checks(store, registry, [s.result])[0]
    assert check.eligible
    s.result.report_value = None
    assert auto_publish(store, registry, settings, [s.result], "run-f") == []
    failed = store.read(ReviewEvent, "event = 'publish_failed'")
    assert len(failed) == 1 and "no value" in failed[0].detail["error"]
    assert store.count(ReviewDecision) == 0


def test_auto_publish_keeps_the_most_precise_value_per_key(seeder, store, registry, settings):
    rounded = seeder.seed("trx", S.VERIFIED, method=ExtractionMethod.NATIVE_TABLE, report_value=5700.0,
                          platform_value=5736.4, half_unit=50.0, fingerprint="fp-table")
    precise = seeder.seed("trx", S.VERIFIED, method=ExtractionMethod.NATIVE_CHART, report_value=5736.363,
                          platform_value=5736.363, half_unit=0.0, fingerprint="fp-chart")
    pubs = _auto(store, registry, settings, rounded, precise)
    assert [p.result_id for p in pubs] == [precise.result.result_id]
    checks = {c.result_id: c for c in policy_checks(store, registry, [rounded.result])}
    assert checks[rounded.result.result_id].reasons == ["already_published"]
    assert store.count(Publication) == 1


def test_batch_duplicates_are_explained(seeder, store, registry):
    a = seeder.seed("trx", S.VERIFIED, report_value=5700.0, half_unit=50.0)
    b = seeder.seed("trx", S.VERIFIED, report_value=5736.0, half_unit=0.5)
    checks = {c.result_id: c for c in policy_checks(store, registry, [a.result, b.result])}
    assert checks[b.result.result_id].eligible
    assert checks[a.result.result_id].reasons == ["another_value_for_same_key"]


def test_unchanged_value_in_a_new_report_version_is_not_republished(seeder, store, registry, settings):
    v1 = seeder.seed("trx", S.VERIFIED, doc=seeder.doc("deck_v1.pptx"), report_value=5910.0, half_unit=0.5)
    assert len(_auto(store, registry, settings, v1)) == 1
    same = seeder.seed("trx", S.VERIFIED, doc=seeder.doc("deck_v2.pptx"), report_value=5910.0, half_unit=0.5)
    assert _auto(store, registry, settings, same) == []
    changed = seeder.seed("trx", S.VERIFIED, doc=seeder.doc("deck_v3.pptx"), report_value=5912.0, half_unit=0.5)
    pubs = _auto(store, registry, settings, changed)
    assert len(pubs) == 1 and published_current(store)["publication_id"].tolist() == [pubs[0].publication_id]


def test_stated_definition_variant_is_never_auto_published_or_linked_to_the_default(seeder, store, registry, settings):
    governed = seeder.seed("trx_share", S.VERIFIED, unit="fraction", report_value=0.091, platform_value=0.0917,
                           definition_choices={"basket": "advanced_therapy", "source": "account_feed"})
    il17 = seeder.seed("trx_share", S.VERIFIED, unit="fraction", report_value=0.261, platform_value=0.2621,
                       label="IL-17 class share", fingerprint="fp-il17",
                       definition_choices={"basket": "il17_class", "source": "account_feed"})
    pubs = _auto(store, registry, settings, governed, il17)
    assert [p.result_id for p in pubs] == [governed.result.result_id]
    assert pubs[0].harness_variant_id == "brand_market"
    assert "non_default_definition" in policy_checks(store, registry, [il17.result])[0].reasons
    assert review.definition_variant(il17.result, registry.kpis["trx_share"]) == {"basket": "il17_class"}
    pub = decide(store, registry, settings, il17.result.result_id, "ACCEPT", "", "rev@example.com").publication
    assert pub.harness_metric_id is None and pub.harness_variant_id is None
