from __future__ import annotations

from datetime import date

from recon_testkit import make_doc, make_obs, one

from kpi_evidence.conflicts import detect_conflicts
from kpi_evidence.contracts import KpiResult, ValidationStatus as VS


class _CountingStore:
    def __init__(self, store):
        self._store = store
        self.result_ids: list[str] = []

    def read(self, model, *args, **kwargs):
        rows = self._store.read(model, *args, **kwargs)
        if model is KpiResult:
            self.result_ids += [r.result_id for r in rows]
        return rows

    def __getattr__(self, name):
        return getattr(self._store, name)


def test_detection_for_one_document_skips_unrelated_results(demo_store, registry, settings):
    deck = make_doc(demo_store, "Kestrel_Monthly_Brand_Review_2026-06.pptx", family="fam-monthly")
    pdf = make_doc(demo_store, "Kestrel_Executive_Summary_Q2-2026.pdf", family="fam-exec")
    other = make_doc(demo_store, "Kestrel_Monthly_Brand_Review_2026-03.pptx", family="fam-march")
    a = one(demo_store, registry, settings, deck, make_obs(deck, "TRx", 5783.04, half=0.005), "trx")
    b = one(demo_store, registry, settings, pdf, make_obs(pdf, "TRx", 5910.0, half=0.5), "trx")
    unrelated = [one(demo_store, registry, settings, other,
                     make_obs(other, "TRx", 6000.0, period_end=date(2026, 3, 31), half=0.5), "trx"),
                 one(demo_store, registry, settings, other, make_obs(other, "Calls", 900.0, half=0.5), "calls")]

    counting = _CountingStore(demo_store)
    flagged = detect_conflicts(counting, registry, settings, doc_ids=[pdf.doc_id])

    assert {r.result_id for r in flagged} == {a.result_id, b.result_id}
    assert demo_store.get(KpiResult, result_id=a.result_id).validation_status == VS.REPORT_CONFLICT
    assert not {r.result_id for r in unrelated} & set(counting.result_ids)


def test_a_document_without_results_reads_nothing_else(demo_store, registry, settings):
    deck = make_doc(demo_store, "Kestrel_Monthly_Brand_Review_2026-06.pptx", family="fam-monthly")
    empty = make_doc(demo_store, "Kestrel_Executive_Summary_Q2-2026.pdf", family="fam-exec")
    one(demo_store, registry, settings, deck, make_obs(deck, "TRx", 5783.04, half=0.005), "trx")
    counting = _CountingStore(demo_store)
    assert detect_conflicts(counting, registry, settings, doc_ids=[empty.doc_id]) == []
    assert counting.result_ids == []
