from __future__ import annotations

import io
import json
from pathlib import Path

from kpi_evidence.extract.ai_parse import parse_ai_document_output
from kpi_evidence.extract.pdf import extract_pdf_values
from kpi_evidence.extract.pptx import sniff_problem
from kpi_evidence.ids import sha256_bytes

BRITISH = ("neighbour", "labelled", "colour", "summarise", "recognise")


def _details(values):
    return [f["detail"] for v in values for f in v.flags if f["code"] == "LABEL_FROM_ADJACENT_LINE"]


def _american(texts):
    return texts and not [t for t in texts if any(word in t.lower() for word in BRITISH)]


def test_encrypted_package_remedy():
    message = sniff_problem(b"\xd0\xcf\x11\xe0" + b"\x00" * 60)
    assert "sensitivity-labeled" in message and _american([message])


def test_pptx_adjacent_label_note(deck_output):
    details = _details(deck_output.values)
    assert _american(details) and all("neighboring" in d for d in details)


def test_pdf_adjacent_label_note():
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(72, height - 72, "Highlights")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, height - 120, "184")
    pdf.drawString(72, height - 134, "Referrals (projected)")
    pdf.showPage()
    pdf.save()
    blob = buf.getvalue()
    details = _details(extract_pdf_values(sha256_bytes(blob), blob, "highlights.pdf").values)
    assert _american(details) and all("neighboring" in d for d in details)


def test_ai_parse_adjacent_label_note():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "ai_parse_v2_field_update.json").read_text(encoding="utf-8"))
    details = _details(parse_ai_document_output("a" * 64, fixture).values)
    assert _american(details) and all("neighboring" in d for d in details)
