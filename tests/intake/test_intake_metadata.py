from __future__ import annotations

from datetime import date

import pytest
from intake_builders import NOW, later, make_doc, make_page, make_value, trend_values

from kpi_evidence.contracts import (
    Document, DocumentStatus, ExtractionMethod, KpiResult, ReviewEvent, ValidationStatus,
)
from kpi_evidence.ids import stable_id
from kpi_evidence.intake import apply_proposals, confirm_metadata, document_grain, propose_metadata

DECK1 = "Kestrel_Monthly_Brand_Review_2026-06.pptx"


def deck1_inputs(doc_id: str = "d" * 64):
    pages = [make_page(1, "Kestrel Monthly Brand Review - June 2026", "Kestrel Monthly Brand Review - June 2026",
                       doc_id=doc_id),
             make_page(2, "TRx trend", "TRx trend\nSource: account feed. Data through May 2026", doc_id=doc_id)]
    return pages, trend_values(doc_id)


def test_june_deck_with_data_through_may(registry):
    pages, values = deck1_inputs()
    p = propose_metadata(registry, make_doc(DECK1), pages, values)
    assert p["brand"]["value"] == "KESTREL" and p["brand"]["confidence"] == 0.95
    assert p["report_type"]["value"] == "monthly_brand_review" and p["report_type"]["confidence"] == 0.95
    assert p["period_end"]["value"] == "2026-05-31" and p["period_end"]["confidence"] == 0.95
    assert "data through" in p["period_end"]["source"]
    assert p["period_start"]["value"] == "2026-05-01" and p["period_grain"]["value"] == "month"
    assert p["period_label"]["value"] == "May 2026"
    assert p["publication_date"]["value"] == "2026-06-01"
    assert p["publication_date"]["confidence"] == 0.95


def test_executive_summary_quarter(registry):
    pages = [make_page(1, "Kestrel Executive Summary Q2 2026")]
    p = propose_metadata(registry, make_doc("Kestrel_Executive_Summary_Q2-2026.pdf"), pages, [])
    assert p["report_type"]["value"] == "executive_summary" and p["report_type"]["confidence"] == 0.95
    assert (p["period_end"]["value"], p["period_start"]["value"], p["period_grain"]["value"]) == (
        "2026-06-30", "2026-04-01", "quarter")
    assert p["period_end"]["confidence"] == 0.95


def test_quarter_report_data_through_uses_report_default_grain(registry):
    pages = [make_page(1, "Kestrel Executive Summary", "Executive summary\nData through June 2026")]
    p = propose_metadata(registry, make_doc("Kestrel_Executive_Summary.pdf"), pages, [])
    assert (p["period_end"]["value"], p["period_grain"]["value"], p["period_start"]["value"]) == (
        "2026-06-30", "quarter", "2026-04-01")


def test_ambiguous_update_deck_needs_confirmation(registry, mem_store, settings):
    doc = make_doc("kestrel-update.pptx")
    pages = [make_page(1, "Update", "Update\nQ2 priorities"), make_page(2, "Share", "Share 26.2%")]
    p = propose_metadata(registry, doc, pages, [make_value("0.262", category="Share", series_name="Share")])
    assert p["brand"]["value"] == "KESTREL" and p["brand"]["confidence"] == 0.6
    assert p["report_type"]["value"] == "field_update" and p["report_type"]["confidence"] == 0.95
    assert p["period_end"]["value"] is None and p["period_end"]["confidence"] == 0.0
    mem_store.upsert([doc])
    out = apply_proposals(mem_store, registry, settings, doc, p)
    assert out.status == DocumentStatus.AWAITING_CONFIRMATION
    assert out.needs_confirmation == ["brand", "period_end"]
    assert out.brand is None and out.period_end is None and out.report_type == "field_update"
    assert out.report_family is None


def test_no_brand_anywhere(registry):
    p = propose_metadata(registry, make_doc("update.pptx"), [make_page(1, "Monthly update")], [])
    assert p["brand"]["value"] is None and p["brand"]["confidence"] == 0.0


def test_title_and_filename_agree_without_footer(registry):
    pages = [make_page(1, "Kestrel brand review May 2026")]
    p = propose_metadata(registry, make_doc("Kestrel_Brand_Review_2026-05.pptx"), pages, [])
    assert p["period_end"]["value"] == "2026-05-31" and p["period_end"]["confidence"] == 0.95
    assert p["period_end"]["source"] == "title+filename"


def test_single_weak_period_signal(registry):
    p = propose_metadata(registry, make_doc("Kestrel_MBR_2026-05.pptx"), [make_page(1, None, "")], [])
    assert p["period_end"]["value"] == "2026-05-31" and p["period_end"]["confidence"] == 0.6


def test_conflicting_title_and_values_without_footer(registry):
    pages = [make_page(1, "Kestrel Monthly Brand Review June 2026")]
    p = propose_metadata(registry, make_doc(DECK1), pages, trend_values())
    assert p["period_end"]["confidence"] == 0.5
    assert p["period_end"]["value"] == "2026-05-31"
    assert {a["value"] for a in p["period_end"]["alternatives"]} == {"2026-06-30"}
    assert p["publication_date"]["value"] == "2026-06-01"


def test_conflicting_data_through_lines(registry):
    pages = [make_page(2, "A", "Data through May 2026"), make_page(3, "B", "Data through April 2026")]
    p = propose_metadata(registry, make_doc(DECK1), pages, [])
    assert p["period_end"]["confidence"] == 0.5 and p["period_end"]["alternatives"]


def test_values_beyond_data_through_lower_confidence(registry):
    pages = [make_page(2, "A", "Data through April 2026")]
    p = propose_metadata(registry, make_doc(DECK1), pages, trend_values())
    assert p["period_end"]["value"] == "2026-04-30" and p["period_end"]["confidence"] == 0.5


def test_weak_through_phrase_is_not_a_measurement_signal(registry):
    pages = [make_page(1, "Kestrel brand review May 2026"), make_page(3, "Plan", "Launch plan through Dec 2026")]
    p = propose_metadata(registry, make_doc("Kestrel_Brand_Review_2026-05.pptx"), pages, [])
    assert p["period_end"]["value"] == "2026-05-31" and p["period_end"]["confidence"] == 0.95


def test_document_properties_title_and_dates(registry):
    doc = make_doc("deck.pptx", props={"title": "Kestrel Monthly Brand Review", "dc:subject": "Data through May 2026",
                                       "modified": "2026-06-03T09:00:00Z"})
    pages = [make_page(1, "Kestrel Monthly Brand Review")]
    p = propose_metadata(registry, doc, pages, [])
    assert p["brand"]["value"] == "KESTREL" and p["brand"]["confidence"] == 0.6
    assert p["report_type"]["value"] == "monthly_brand_review"
    assert p["period_end"]["value"] == "2026-05-31" and p["period_end"]["confidence"] == 0.95
    assert p["publication_date"] == {"value": "2026-06-03", "confidence": 0.6, "source": "document_properties",
                                     "alternatives": []}


def test_brand_from_body_text_counts_as_second_source(registry):
    pages = [make_page(1, "Update"), make_page(2, "Share", "Kestrel share of market")]
    p = propose_metadata(registry, make_doc("kestrel-update.pptx"), pages, [])
    assert p["brand"]["confidence"] == 0.95 and "page_text" in p["brand"]["source"]


def test_longest_report_type_keyword_wins(registry):
    pages = [make_page(1, "Kestrel leadership update Q2 2026")]
    p = propose_metadata(registry, make_doc("Kestrel_Leadership_Update_Q2-2026.pdf"), pages, [])
    assert p["report_type"]["value"] == "executive_summary" and p["report_type"]["confidence"] == 0.95


def test_camel_case_file_name(registry):
    p = propose_metadata(registry, make_doc("KestrelMonthlyBrandReview.pptx"), [], [])
    assert p["brand"]["value"] == "KESTREL" and p["report_type"]["value"] == "monthly_brand_review"


def test_footnotes_in_values_count(registry):
    values = [make_value("5910", method=ExtractionMethod.NATIVE_TABLE, row_label="TRx", col_header="Value",
                         footnotes=["Data through May 2026"])]
    p = propose_metadata(registry, make_doc(DECK1), [make_page(1, "Kestrel Monthly Brand Review June 2026")], values)
    assert p["period_end"]["value"] == "2026-05-31" and p["period_end"]["confidence"] == 0.95


def _ready(mem_store, registry, settings, file_name=DECK1, *, doc_id, minutes=0):
    doc = make_doc(file_name, doc_id=doc_id, registered_at=later(minutes))
    mem_store.upsert([doc])
    pages, values = deck1_inputs(doc_id)
    return apply_proposals(mem_store, registry, settings, doc, propose_metadata(registry, doc, pages, values))


def test_apply_ready_sets_fields_and_family(mem_store, registry, settings):
    doc = _ready(mem_store, registry, settings, doc_id="a" * 64)
    assert doc.status == DocumentStatus.READY and doc.needs_confirmation == []
    assert (doc.brand, doc.report_type, doc.period_end, doc.period_start, doc.period_label) == (
        "KESTREL", "monthly_brand_review", date(2026, 5, 31), date(2026, 5, 1), "May 2026")
    assert doc.publication_date == date(2026, 6, 1)
    assert doc.report_family == stable_id("KESTREL", "monthly_brand_review", "2026-05-31")
    assert doc.version == 1 and doc.supersedes_doc_id is None
    stored = mem_store.get(Document, doc_id=doc.doc_id)
    assert stored.status == DocumentStatus.READY and stored.metadata_proposals["period_end"]["value"] == "2026-05-31"
    assert document_grain(stored, registry) == "month"


def test_apply_keeps_document_properties(mem_store, registry, settings):
    doc = make_doc(DECK1, props={"title": "Kestrel Monthly Brand Review"})
    mem_store.upsert([doc])
    out = apply_proposals(mem_store, registry, settings, doc, propose_metadata(registry, doc, *deck1_inputs()))
    assert out.metadata_proposals["_document_properties"] == {"title": "Kestrel Monthly Brand Review"}


def _result(doc_id: str) -> KpiResult:
    return KpiResult(result_id=stable_id("r", doc_id), result_version=1, observation_id=stable_id("o", doc_id),
                     doc_id=doc_id, brand="KESTREL", kpi_id="trx", kpi_version=1, definition_hash="h", scope_key="{}",
                     period_start=date(2026, 5, 1), period_end=date(2026, 5, 31), period_grain="month",
                     report_value=5910.0, unit="count", platform_value=5783.0, abs_diff=None, rel_diff=None,
                     tolerance={}, match_basis=None, validation_status=ValidationStatus.VARIANCE_REVIEW,
                     reason_code="unresolved", reason_detail="", explanation={}, definition_choices={},
                     extraction_method=None, recon_run_id=None, sql_artifact_id=None, dq_status=None,
                     is_current=True, created_at=NOW)


def test_newer_version_supersedes_previous(mem_store, registry, settings):
    v1 = _ready(mem_store, registry, settings, doc_id="a" * 64)
    mem_store.upsert([Document(**{**v1.__dict__, "status": DocumentStatus.PROCESSED})])
    mem_store.upsert([_result(v1.doc_id)])
    v2 = _ready(mem_store, registry, settings, "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx", doc_id="b" * 64,
                minutes=30)
    assert v2.status == DocumentStatus.READY and v2.version == 2
    assert v2.report_family == v1.report_family and v2.supersedes_doc_id == v1.doc_id
    old = mem_store.get(Document, doc_id=v1.doc_id)
    assert old.status == DocumentStatus.SUPERSEDED and old.superseded_by_doc_id == v2.doc_id
    [res] = mem_store.read(KpiResult, "doc_id = :d", {"d": v1.doc_id})
    assert res.is_current is False


def test_reapplying_is_idempotent(mem_store, registry, settings):
    _ready(mem_store, registry, settings, doc_id="a" * 64)
    v2 = _ready(mem_store, registry, settings, doc_id="b" * 64, minutes=30)
    again = _ready(mem_store, registry, settings, doc_id="b" * 64, minutes=30)
    assert (again.version, again.status, again.supersedes_doc_id) == (v2.version, v2.status, v2.supersedes_doc_id)
    assert mem_store.count(Document) == 2


def test_older_document_processed_after_newer_is_superseded(mem_store, registry, settings):
    _ready(mem_store, registry, settings, doc_id="b" * 64, minutes=30)
    old = _ready(mem_store, registry, settings, doc_id="a" * 64, minutes=0)
    assert old.status == DocumentStatus.SUPERSEDED and old.superseded_by_doc_id == "b" * 64
    assert old.version == 1
    assert mem_store.get(Document, doc_id="b" * 64).status == DocumentStatus.READY


def test_different_period_is_a_different_family(mem_store, registry, settings):
    may = _ready(mem_store, registry, settings, doc_id="a" * 64)
    doc = make_doc("Kestrel_Monthly_Brand_Review_2026-07.pptx", doc_id="c" * 64, registered_at=later(60))
    mem_store.upsert([doc])
    pages = [make_page(1, "Kestrel Monthly Brand Review - July 2026"), make_page(2, "x", "Data through June 2026")]
    june = apply_proposals(mem_store, registry, settings, doc, propose_metadata(registry, doc, pages, []))
    assert june.report_family != may.report_family and june.version == 1
    assert mem_store.get(Document, doc_id=may.doc_id).status == DocumentStatus.READY


def test_awaiting_document_is_not_superseded(mem_store, registry, settings):
    waiting = make_doc(DECK1, doc_id="a" * 64, status=DocumentStatus.AWAITING_CONFIRMATION,
                       brand="KESTREL", report_type="monthly_brand_review", period_end=date(2026, 5, 31),
                       report_family=stable_id("KESTREL", "monthly_brand_review", "2026-05-31"))
    mem_store.upsert([waiting])
    newer = _ready(mem_store, registry, settings, doc_id="b" * 64, minutes=30)
    assert newer.version == 2
    assert mem_store.get(Document, doc_id="a" * 64).status == DocumentStatus.AWAITING_CONFIRMATION


def test_threshold_comes_from_settings(mem_store, registry, settings):
    settings.metadata_confidence_threshold = 0.99
    doc = _ready(mem_store, registry, settings, doc_id="a" * 64)
    assert doc.status == DocumentStatus.AWAITING_CONFIRMATION
    assert set(doc.needs_confirmation) == {"brand", "report_type", "period_end"}


class FakeProvider:
    model = "fake-model"

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer or {}, error, []

    def propose_metadata(self, text, fields):
        self.calls.append({"text": text, "fields": fields})
        if self.error:
            raise self.error
        return self.answer


def test_ai_fills_missing_fields_only_and_is_capped(mem_store, registry, settings):
    doc = make_doc("kestrel-update.pptx")
    mem_store.upsert([doc])
    proposals = propose_metadata(registry, doc, [make_page(1, "Update")], [])
    provider = FakeProvider({"period_end": {"value": "2026-05-15", "confidence": 0.99},
                             "report_type": {"value": "executive_summary", "confidence": 0.99},
                             "brand": {"value": "Kestrel", "confidence": 0.99}})
    out = apply_proposals(mem_store, registry, settings, doc, proposals, ai_provider=provider)
    p = out.metadata_proposals
    assert p["period_end"] == {"value": "2026-05-31", "confidence": 0.6, "source": "ai:fake-model", "alternatives": []}
    assert p["report_type"]["value"] == "field_update"
    assert p["brand"]["source"] == "filename"
    assert provider.calls[0]["fields"] == ["period_end", "publication_date"]
    assert "File name: kestrel-update.pptx" in provider.calls[0]["text"] and "Title: Update" in provider.calls[0]["text"]
    assert out.status == DocumentStatus.AWAITING_CONFIRMATION and "period_end" in out.needs_confirmation


def test_ai_failure_is_recorded_not_raised(mem_store, registry, settings):
    doc = make_doc("update.pptx")
    mem_store.upsert([doc])
    out = apply_proposals(mem_store, registry, settings, doc, propose_metadata(registry, doc, [], []),
                          ai_provider=FakeProvider(error=RuntimeError("endpoint down")))
    assert "endpoint down" in out.metadata_proposals["_ai_error"]
    assert out.status == DocumentStatus.AWAITING_CONFIRMATION


def test_provider_without_method_is_ignored(mem_store, registry, settings):
    doc = make_doc("update.pptx")
    mem_store.upsert([doc])
    out = apply_proposals(mem_store, registry, settings, doc, propose_metadata(registry, doc, [], []),
                          ai_provider=object())
    assert "_ai_error" not in out.metadata_proposals


def _awaiting(mem_store, registry, settings, doc_id="a" * 64, minutes=0):
    doc = make_doc("kestrel-update.pptx", doc_id=doc_id, registered_at=later(minutes))
    mem_store.upsert([doc])
    proposals = propose_metadata(registry, doc, [make_page(1, "Update")], [])
    return apply_proposals(mem_store, registry, settings, doc, proposals)


def test_confirm_metadata(mem_store, registry, settings):
    doc = _awaiting(mem_store, registry, settings)
    assert doc.status == DocumentStatus.AWAITING_CONFIRMATION
    out = confirm_metadata(mem_store, registry, settings, doc.doc_id,
                           {"brand": "Kestrel", "period_end": "2026-05-15"}, "reviewer@example.com")
    assert out.status == DocumentStatus.READY and out.needs_confirmation == []
    assert out.confirmed_by == "reviewer@example.com" and out.confirmed_at is not None
    assert (out.brand, out.report_type, out.period_end, out.period_start) == (
        "KESTREL", "field_update", date(2026, 5, 31), date(2026, 5, 1))
    assert out.period_label == "May 2026"
    assert out.report_family == stable_id("KESTREL", "field_update", "2026-05-31") and out.version == 1
    assert out.metadata_proposals["brand"]["source"] == "confirmed:reviewer@example.com"
    assert document_grain(out, registry) == "month"
    [event] = mem_store.read(ReviewEvent, "event = 'confirmed_metadata'")
    assert event.doc_id == doc.doc_id and event.actor == "reviewer@example.com"
    assert event.detail["fields"]["period_end"] == "2026-05-15" and event.detail["version"] == 1
    assert mem_store.get(Document, doc_id=doc.doc_id).status == DocumentStatus.READY


def test_confirm_accepts_period_text_and_grain(mem_store, registry, settings):
    doc = _awaiting(mem_store, registry, settings)
    out = confirm_metadata(mem_store, registry, settings, doc.doc_id,
                           {"brand": "KESTREL", "period_end": "Q2 2026", "period_grain": "quarter"}, "r@example.com")
    assert (out.period_start, out.period_end) == (date(2026, 4, 1), date(2026, 6, 30))
    assert document_grain(out, registry) == "quarter"


def test_confirm_versions_against_existing_family(mem_store, registry, settings):
    first = _awaiting(mem_store, registry, settings, doc_id="a" * 64)
    confirm_metadata(mem_store, registry, settings, first.doc_id, {"brand": "KESTREL", "period_end": "2026-05-31"},
                     "r@example.com")
    second = _awaiting(mem_store, registry, settings, doc_id="b" * 64, minutes=10)
    out = confirm_metadata(mem_store, registry, settings, second.doc_id,
                           {"brand": "KESTREL", "period_end": "2026-05-31"}, "r@example.com")
    assert out.version == 2 and out.supersedes_doc_id == first.doc_id
    assert mem_store.get(Document, doc_id=first.doc_id).status == DocumentStatus.SUPERSEDED


def test_confirm_before_extraction_keeps_registered(mem_store, registry, settings):
    doc = make_doc("x.pptx", status=DocumentStatus.REGISTERED)
    mem_store.upsert([doc])
    out = confirm_metadata(mem_store, registry, settings, doc.doc_id,
                           {"brand": "KESTREL", "report_type": "monthly_brand_review", "period_end": "2026-05-31"},
                           "r@example.com")
    assert out.status == DocumentStatus.REGISTERED and out.confirmed_by == "r@example.com"


@pytest.mark.parametrize("fields,message", [
    ({"brand": "Falcon", "period_end": "2026-05-31"}, "unknown brand"),
    ({"brand": "KESTREL", "report_type": "weekly_blog", "period_end": "2026-05-31"}, "unknown report type"),
    ({"brand": "KESTREL", "shade": "red"}, "unknown metadata fields"),
    ({"brand": "KESTREL"}, "required fields still missing"),
    ({"brand": "KESTREL", "period_end": "soon"}, "not a date"),
    ({"brand": "KESTREL", "period_end": "2026-05-31", "period_grain": "fortnight"}, "unknown period grain"),
])
def test_confirm_rejects_bad_input(mem_store, registry, settings, fields, message):
    doc = _awaiting(mem_store, registry, settings)
    with pytest.raises(ValueError, match=message):
        confirm_metadata(mem_store, registry, settings, doc.doc_id, fields, "r@example.com")


def test_confirm_unknown_or_encrypted(mem_store, registry, settings):
    with pytest.raises(KeyError):
        confirm_metadata(mem_store, registry, settings, "0" * 64, {}, "r@example.com")
    doc = make_doc("x.pptx", status=DocumentStatus.ENCRYPTED, error="encrypted")
    mem_store.upsert([doc])
    with pytest.raises(ValueError, match="ENCRYPTED"):
        confirm_metadata(mem_store, registry, settings, doc.doc_id, {"brand": "KESTREL"}, "r@example.com")


def test_document_grain_fallbacks(registry):
    q = make_doc("x.pdf", period_start=date(2026, 4, 1), period_end=date(2026, 6, 30))
    assert document_grain(q, registry) == "quarter"
    y = make_doc("x.pdf", period_start=date(2026, 1, 1), period_end=date(2026, 5, 31))
    assert document_grain(y, registry) == "ytd"
    d = make_doc("x.pdf", report_type="executive_summary", period_end=date(2026, 6, 30))
    assert document_grain(d, registry) == "quarter"
    assert document_grain(make_doc("x.pdf"), registry) is None


def test_same_second_batch_uses_file_name_version(mem_store, registry, settings):
    v2 = _ready(mem_store, registry, settings, "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx", doc_id="0" * 64)
    assert v2.version == 1
    v1 = _ready(mem_store, registry, settings, DECK1, doc_id="f" * 64)
    assert v1.status == DocumentStatus.SUPERSEDED and v1.version == 1
    current = mem_store.get(Document, doc_id="0" * 64)
    assert current.status == DocumentStatus.READY
    assert current.version == 2 and current.supersedes_doc_id == "f" * 64


def test_renumbering_keeps_results_current(mem_store, registry, settings):
    _ready(mem_store, registry, settings, "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx", doc_id="0" * 64)
    mem_store.upsert([_result("0" * 64)])
    _ready(mem_store, registry, settings, DECK1, doc_id="f" * 64)
    [res] = mem_store.read(KpiResult, "doc_id = :d", {"d": "0" * 64})
    assert res.is_current is True


def test_file_name_version():
    from kpi_evidence.intake import file_name_version

    assert file_name_version("Kestrel_Monthly_Brand_Review_2026-06_v2.pptx") == 2
    assert file_name_version("deck rev3.pptx") == 3
    assert file_name_version("Kestrel_Monthly_Brand_Review_2026-06.pptx") == 1
    assert file_name_version("review.pptx") == 1


def test_publication_date_made_precise_by_document_properties(registry):
    doc = make_doc(DECK1, props={"modified": "2026-06-10T09:00:00Z"})
    p = propose_metadata(registry, doc, *deck1_inputs())
    assert p["publication_date"]["value"] == "2026-06-10" and p["publication_date"]["confidence"] == 0.95


def test_property_date_before_measurement_end_is_ignored(registry):
    doc = make_doc("Kestrel_Executive_Summary_Q2-2026.pdf", props={"created": "2000-01-01T00:00:00"})
    p = propose_metadata(registry, doc, [make_page(1, "Kestrel Executive Summary Q2 2026")], [])
    assert p["publication_date"]["value"] is None


def test_evidence_text_is_kept_for_optional_ai(registry):
    p = propose_metadata(registry, make_doc(DECK1), *deck1_inputs())
    assert p["_evidence_text"].startswith(f"File name: {DECK1}")
    assert "Data through May 2026" in p["_evidence_text"]
