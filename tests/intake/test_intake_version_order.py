from __future__ import annotations

from datetime import timedelta

import pytest
from intake_builders import NOW, make_doc, make_page, trend_values

from kpi_evidence.contracts import Document, DocumentStatus
from kpi_evidence.intake import apply_proposals, file_name_version, propose_metadata

V9 = "Kestrel_Monthly_Brand_Review_2026-06_v9.pptx"
V10 = "Kestrel_Monthly_Brand_Review_2026-06_v10.pptx"


def _apply(mem_store, registry, settings, file_name: str, doc_id: str, seconds: int) -> Document:
    doc = make_doc(file_name, doc_id=doc_id, registered_at=NOW + timedelta(seconds=seconds))
    mem_store.upsert([doc])
    pages = [make_page(1, "Kestrel Monthly Brand Review - June 2026", doc_id=doc_id),
             make_page(2, "TRx trend", "TRx trend\nData through May 2026", doc_id=doc_id)]
    proposals = propose_metadata(registry, doc, pages, trend_values(doc_id))
    return apply_proposals(mem_store, registry, settings, doc, proposals)


@pytest.mark.parametrize("gap", [0, 1, 3600])
def test_higher_file_name_version_is_current_whatever_the_registration_time(mem_store, registry, settings, gap):
    _apply(mem_store, registry, settings, V10, "a" * 64, 0)
    _apply(mem_store, registry, settings, V9, "b" * 64, gap)
    v10 = mem_store.get(Document, doc_id="a" * 64)
    v9 = mem_store.get(Document, doc_id="b" * 64)
    assert v10.status == DocumentStatus.READY and v10.version == 2 and v10.supersedes_doc_id == v9.doc_id
    assert v9.status == DocumentStatus.SUPERSEDED and v9.superseded_by_doc_id == v10.doc_id and v9.version == 1


def test_revised_outranks_a_file_without_a_token(mem_store, registry, settings):
    _apply(mem_store, registry, settings, "Kestrel_Monthly_Brand_Review_2026-06_revised.pptx", "a" * 64, 0)
    _apply(mem_store, registry, settings, "Kestrel_Monthly_Brand_Review_2026-06.pptx", "b" * 64, 5)
    assert mem_store.get(Document, doc_id="a" * 64).status == DocumentStatus.READY
    assert mem_store.get(Document, doc_id="b" * 64).status == DocumentStatus.SUPERSEDED


def test_equal_tokens_fall_back_to_registration_time(mem_store, registry, settings):
    _apply(mem_store, registry, settings, "Kestrel_Monthly_Brand_Review_2026-06 v2.pptx", "a" * 64, 5)
    _apply(mem_store, registry, settings, "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx", "b" * 64, 0)
    assert mem_store.get(Document, doc_id="a" * 64).status == DocumentStatus.READY
    assert mem_store.get(Document, doc_id="b" * 64).status == DocumentStatus.SUPERSEDED


@pytest.mark.parametrize("name, version", [
    (V10, 10), ("deck rev3.pptx", 3), ("deck_revised.pptx", 2), ("Deck (Revised).pptx", 2),
    ("unrevised deck.pptx", 1), ("deck_v4_revised.pptx", 4), ("review.pptx", 1), ("Kestrel_r3m_review.pptx", 1),
])
def test_file_name_version_tokens(name, version):
    assert file_name_version(name) == version
