from __future__ import annotations

from recon_testkit import NOW, make_doc, make_obs, one, run

from kpi_evidence.conflicts import detect_conflicts
from kpi_evidence.contracts import (
    DecisionSource, DocumentStatus, KpiResult, ReviewAction, ReviewDecision, ReviewEvent, ValidationStatus as VS,
)

DECK = "Kestrel_Monthly_Brand_Review_2026-06.pptx"
PDF = "Kestrel_Executive_Summary_Q2-2026.pdf"


def _two_reports(store, registry, settings, deck_value=5783.04, pdf_value=5910.0, pdf_family="fam-exec"):
    deck, pdf = make_doc(store, DECK, family="fam-monthly"), make_doc(store, PDF, family=pdf_family)
    a = one(store, registry, settings, deck, make_obs(deck, "TRx", deck_value, half=0.005), "trx")
    b = one(store, registry, settings, pdf, make_obs(pdf, "TRx", pdf_value, half=0.5), "trx")
    return deck, pdf, a, b


def _get(store, result):
    return store.get(KpiResult, result_id=result.result_id)


def test_conflict_between_families(demo_store, registry, settings):
    deck, pdf, a, b = _two_reports(demo_store, registry, settings)
    assert (a.validation_status, b.validation_status) == (VS.VERIFIED, VS.VARIANCE_REVIEW)
    flagged = detect_conflicts(demo_store, registry, settings, doc_ids=[pdf.doc_id])
    assert {r.result_id for r in flagged} == {a.result_id, b.result_id}
    a2, b2 = _get(demo_store, a), _get(demo_store, b)
    assert (a2.validation_status, a2.reason_code, a2.result_version) == (VS.REPORT_CONFLICT, "conflicting_reports", 2)
    assert PDF in a2.reason_detail and "5,910" in a2.reason_detail
    assert DECK in b2.reason_detail
    assert a2.explanation["conflict"]["previous"]["validation_status"] == "VERIFIED"
    detect_conflicts(demo_store, registry, settings)
    again = one(demo_store, registry, settings, deck, make_obs(deck, "TRx", 5783.04, half=0.005), "trx", run_id="run-2")
    assert (again.validation_status, again.result_version) == (VS.REPORT_CONFLICT, 2)
    assert _get(demo_store, a).result_version == 2 and _get(demo_store, b).result_version == 2


def test_values_within_tolerance_do_not_conflict(demo_store, registry, settings):
    _, pdf, a, b = _two_reports(demo_store, registry, settings, pdf_value=5790.0)
    assert detect_conflicts(demo_store, registry, settings, doc_ids=[pdf.doc_id]) == []
    assert _get(demo_store, a).validation_status == VS.VERIFIED


def test_same_family_is_supersession_not_conflict(demo_store, registry, settings):
    v1 = make_doc(demo_store, DECK, family="fam-monthly", status=DocumentStatus.SUPERSEDED)
    v2 = make_doc(demo_store, "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx", family="fam-monthly", version=2)
    old = one(demo_store, registry, settings, v1, make_obs(v1, "TRx", 5600.0, half=0.5), "trx")
    new = one(demo_store, registry, settings, v2, make_obs(v2, "TRx", 5783.0, half=0.5), "trx")
    assert old.is_current is False and new.is_current is True
    assert detect_conflicts(demo_store, registry, settings) == []
    v1.status = DocumentStatus.READY
    demo_store.upsert([v1])
    one(demo_store, registry, settings, v1, make_obs(v1, "TRx", 5600.0, half=0.5), "trx", run_id="run-2")
    assert detect_conflicts(demo_store, registry, settings) == []


def test_human_decided_results_get_an_event_instead(demo_store, registry, settings):
    _, pdf, a, b = _two_reports(demo_store, registry, settings)
    demo_store.insert([ReviewDecision(decision_id="d1", result_id=a.result_id, result_version=1,
                                      action=ReviewAction.ACCEPT, rationale="", decided_by="reviewer@example.com",
                                      decision_source=DecisionSource.HUMAN, decided_at=NOW, evidence_hash="e")])
    flagged = detect_conflicts(demo_store, registry, settings, doc_ids=[pdf.doc_id])
    assert [r.result_id for r in flagged] == [b.result_id]
    assert (_get(demo_store, a).validation_status, _get(demo_store, a).result_version) == (VS.VERIFIED, 1)
    events = demo_store.read(ReviewEvent, "event = 'conflict_detected'")
    assert len(events) == 1 and events[0].result_id == a.result_id
    assert events[0].detail["others"][0]["file_name"] == PDF
    detect_conflicts(demo_store, registry, settings)
    assert demo_store.count(ReviewEvent) == 1


def test_conflict_is_lifted_when_the_other_report_is_superseded(demo_store, registry, settings):
    deck, pdf, a, b = _two_reports(demo_store, registry, settings)
    detect_conflicts(demo_store, registry, settings)
    pdf.status = DocumentStatus.SUPERSEDED
    demo_store.upsert([pdf])
    stale = _get(demo_store, b)
    stale.is_current = False
    demo_store.upsert([stale])
    assert detect_conflicts(demo_store, registry, settings, doc_ids=[deck.doc_id]) == []
    restored = _get(demo_store, a)
    assert (restored.validation_status, restored.reason_code, restored.result_version) == (
        VS.VERIFIED, "match_under_governed_default", 3)
    assert "conflict" not in restored.explanation


def test_unsettled_results_are_not_compared(demo_store, registry, settings):
    deck, pdf = make_doc(demo_store, DECK, family="f1"), make_doc(demo_store, PDF, family="f2")
    one(demo_store, registry, settings, deck, make_obs(deck, "TRx", 5783.04, half=0.005), "trx")
    uncertain = make_obs(pdf, "TRx", 5900.0, confidence=0.3)
    scaled = make_obs(pdf, "TRx (000s)", 5.78, half=0.005, key="k")
    results = run(demo_store, registry, settings, pdf, [(uncertain, "trx"), (scaled, "trx")])
    assert [r.reason_code for r in results] == ["low_confidence_value", "scale_mismatch"]
    assert all(r.is_current for r in results)
    assert detect_conflicts(demo_store, registry, settings) == []
