from __future__ import annotations

import io

from kpi_evidence.contracts import ExtractionMethod
from kpi_evidence.extract.pdf import extract_pdf_values
from kpi_evidence.ids import sha256_bytes


def _extract(blob):
    return extract_pdf_values(sha256_bytes(blob), blob, "Kestrel_Executive_Summary_Q2-2026.pdf", run_id="r1")


def test_table_cells_with_bbox(pdf_bytes):
    out = _extract(pdf_bytes)
    cells = {(v.locator["row"], v.locator["col"]): v for v in out.values if v.method == ExtractionMethod.PDF_TABLE}
    assert set(cells) == {(1, 1), (1, 2), (2, 1), (2, 2)}
    trx = cells[(1, 1)]
    assert (trx.raw_text, trx.raw_number, trx.confidence) == ("17,220", 17220.0, 0.9)
    assert trx.context["row_label"] == "TRx" and trx.context["col_header"] == "Q2 2026"
    assert trx.context["object_kind"] == "pdf_table" and trx.context["header_row"] == ["KPI", "Q2 2026", "vs PY"]
    assert trx.locator["page"] == 1 and trx.locator["table_index"] == 0
    x, y, w, h = trx.locator["bbox"]
    assert 0 < x < 612 and 0 < y < 792 and 0 < w < 200 and 0 < h < 40
    assert cells[(1, 2)].raw_number == -31.6 and "DERIVED_CHANGE" in [f["code"] for f in cells[(1, 2)].flags]
    assert cells[(2, 2)].raw_text == "+0.4pp"


def test_labeled_text_line_outside_table(pdf_bytes):
    text = [v for v in _extract(pdf_bytes).values if v.method == ExtractionMethod.PDF_TEXT]
    assert [(v.raw_text, v.context["row_label"]) for v in text] == [("84.4%", "Call attainment")]
    v = text[0]
    assert v.confidence == 0.7 and v.context["text_line"] == "Call attainment: 84.4% of plan"
    assert v.context["footnotes"] == ["Data through June 2026"]
    assert len(v.locator["bbox"]) == 4 and v.locator["bbox"][2] < 100


def test_image_only_page_is_a_placeholder(pdf_bytes):
    out = _extract(pdf_bytes)
    placeholders = [v for v in out.values if v.method == ExtractionMethod.IMAGE_PLACEHOLDER]
    assert [v.slide_or_page for v in placeholders] == [2]
    assert placeholders[0].raw_number is None and placeholders[0].confidence == 0.0
    assert [f["code"] for f in placeholders[0].flags] == ["IMAGE_NOT_PARSED"]


def test_pages_and_properties(pdf_bytes):
    out = _extract(pdf_bytes)
    first, second = out.pages
    assert first.title == "Kestrel Executive Summary Q2 2026" and "Call attainment" in first.text
    assert (first.page_width, first.page_height) == (612.0, 792.0)
    assert [s["kind"] for s in first.shapes] == ["table"]
    assert second.text == "" and second.image_count == 1 and second.title is None
    assert out.document_properties["title"] == "Kestrel Executive Summary" and out.document_properties["page_count"] == 2
    assert out.issues == [] and len({v.value_id for v in out.values}) == len(out.values)


def test_scan_only_pdf():
    from extract_testkit import png_bytes
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawImage(ImageReader(io.BytesIO(png_bytes())), 50, 50, 400, 300)
    pdf.showPage()
    pdf.save()
    out = _extract(buf.getvalue())
    assert [v.method for v in out.values] == [ExtractionMethod.IMAGE_PLACEHOLDER] and len(out.pages) == 1


def test_bad_pdf_is_an_error_issue():
    assert _extract(b"no header").issues[0]["code"] == "NOT_A_PDF"
    broken = _extract(b"%PDF-1.4\n garbage")
    assert broken.failed and broken.issues[0]["code"] in {"PDF_OPEN_FAILED", "PAGE_EXTRACTION_FAILED"}
