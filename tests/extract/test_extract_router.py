from __future__ import annotations

import io

import pytest
from extract_testkit import make_doc, png_bytes

from kpi_evidence.contracts import DocumentFormat, DocumentPage, ExtractedValue, ExtractionMethod
from kpi_evidence.extract import ExtractionOutput, extract_document
from kpi_evidence.extract import router


def test_routes_each_supported_format(settings, deck_bytes, workbook_bytes, pdf_bytes):
    cases = [(deck_bytes, "deck.pptx", DocumentFormat.PPTX, "pptx", ExtractionMethod.NATIVE_CHART),
             (deck_bytes, "deck.pptm", DocumentFormat.PPTM, "pptx", ExtractionMethod.NATIVE_CHART),
             (workbook_bytes, "kpis.xlsx", DocumentFormat.XLSX, "xlsx", ExtractionMethod.WORKBOOK),
             (pdf_bytes, "summary.pdf", DocumentFormat.PDF, "pdf", ExtractionMethod.PDF_TABLE)]
    for blob, name, fmt, route, method in cases:
        out = extract_document(settings, make_doc(blob, name, fmt), blob, run_id="run-9")
        assert out.extractor_versions["route"] == route
        assert method in {v.method for v in out.values} and out.pages
        assert {v.run_id for v in out.values} == {"run-9"}
        assert not [i for i in out.issues if i["code"] == "CONTENT_HASH_MISMATCH"]


@pytest.mark.parametrize("fmt, pil_format", [(DocumentFormat.PNG, "PNG"), (DocumentFormat.JPEG, "JPEG"),
                                             (DocumentFormat.TIFF, "TIFF")])
def test_images_become_one_placeholder_and_one_page(settings, fmt, pil_format):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (10, 20, 30)).save(buf, format=pil_format)
    blob = buf.getvalue()
    out = extract_document(settings, make_doc(blob, f"shot.{pil_format.lower()}", fmt), blob)
    assert len(out.values) == 1 and len(out.pages) == 1
    value = out.values[0]
    assert value.method == ExtractionMethod.IMAGE_PLACEHOLDER and value.raw_number is None and value.confidence == 0.0
    assert value.locator == {"page": 1, "bbox": [0.0, 0.0, 640.0, 480.0]}
    assert [f["code"] for f in value.flags] == ["IMAGE_NOT_PARSED"]
    assert (out.pages[0].page_width, out.pages[0].page_height, out.pages[0].image_count) == (640.0, 480.0, 1)


def test_unreadable_image_still_gets_a_placeholder(settings):
    blob = b"\x89PNG\r\n\x1a\n broken"
    out = extract_document(settings, make_doc(blob, "x.png", DocumentFormat.PNG), blob)
    assert len(out.values) == 1 and out.issues[0]["code"] == "IMAGE_UNREADABLE" and not out.failed


@pytest.mark.parametrize("fmt, code, needle", [
    (DocumentFormat.ENCRYPTED_OOXML, "ENCRYPTED", "sensitivity label"),
    (DocumentFormat.LEGACY_PPT, "UNSUPPORTED_FORMAT", "save as .pptx"),
    (DocumentFormat.UNKNOWN, "UNSUPPORTED_FORMAT", "cannot be extracted"),
])
def test_unsupported_formats_are_explicit(settings, fmt, code, needle):
    blob = b"\xd0\xcf\x11\xe0" + b"\0" * 32
    out = extract_document(settings, make_doc(blob, "old.ppt", fmt), blob)
    assert out.failed and out.values == [] and out.pages == []
    assert out.issues[0]["code"] == code and needle in out.issues[0]["detail"]
    assert code in out.error_text


def test_route_exception_becomes_an_issue(settings, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("library bug")

    monkeypatch.setattr(router, "extract_image_values", boom)
    blob = png_bytes()
    out = extract_document(settings, make_doc(blob, "x.png", DocumentFormat.PNG), blob)
    assert out.failed and out.issues[0]["code"] == "EXTRACTION_FAILED" and "library bug" in out.issues[0]["detail"]


def test_hash_mismatch_is_a_warning(settings, pdf_bytes):
    doc = make_doc(b"other bytes", "summary.pdf", DocumentFormat.PDF)
    out = extract_document(settings, doc, pdf_bytes)
    assert out.values and any(i["code"] == "CONTENT_HASH_MISMATCH" and i["severity"] == "warning" for i in out.issues)


def test_output_writes_through_the_store(settings, mem_store, pdf_bytes):
    doc = make_doc(pdf_bytes, "summary.pdf", DocumentFormat.PDF)
    out: ExtractionOutput = extract_document(settings, doc, pdf_bytes)
    mem_store.replace(ExtractedValue, {"doc_id": doc.doc_id}, out.values)
    mem_store.replace(DocumentPage, {"doc_id": doc.doc_id}, out.pages)
    mem_store.replace(ExtractedValue, {"doc_id": doc.doc_id}, out.values)
    assert mem_store.count(ExtractedValue, "doc_id = :d", {"d": doc.doc_id}) == len(out.values)
    back = mem_store.read(ExtractedValue, "doc_id = :d AND method = :m", {"d": doc.doc_id, "m": "PDF_TEXT"})
    assert back[0].context["text_line"] == "Call attainment: 84.4% of plan"
