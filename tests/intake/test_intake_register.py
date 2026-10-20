from __future__ import annotations

import io
from pathlib import Path

import pytest
from intake_builders import CT_DOCX, CT_PPTM, CT_PPTX, CT_XLSX, OLE_MAGIC, ole, ooxml, pdf_bytes

from kpi_evidence.contracts import Document, DocumentFormat, DocumentStatus, ReviewEvent
from kpi_evidence.ids import sha256_bytes
from kpi_evidence.intake import register_file, register_inbox, sniff
from kpi_evidence.store.files import files_for


@pytest.mark.parametrize("blob,name,fmt", [
    (ooxml(CT_PPTX), "deck.pptx", DocumentFormat.PPTX),
    (ooxml(CT_PPTM), "deck.pptm", DocumentFormat.PPTM),
    (ooxml(CT_XLSX, "xl/workbook.xml"), "book.xlsx", DocumentFormat.XLSX),
    (ooxml(CT_PPTX), "renamed.pdf", DocumentFormat.PPTX),
    (pdf_bytes(), "report.pdf", DocumentFormat.PDF),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "shot.png", DocumentFormat.PNG),
    (b"\xFF\xD8\xFF\xE0" + b"\x00" * 20, "shot.jpg", DocumentFormat.JPEG),
    (b"II*\x00" + b"\x00" * 20, "scan.tif", DocumentFormat.TIFF),
    (b"MM\x00*" + b"\x00" * 20, "scan.tiff", DocumentFormat.TIFF),
    (ole("EncryptionInfo", "EncryptedPackage"), "locked.pptx", DocumentFormat.ENCRYPTED_OOXML),
    (ole("PowerPoint Document", "Current User"), "old.ppt", DocumentFormat.LEGACY_PPT),
    (ole("Workbook"), "old.xls", DocumentFormat.LEGACY_PPT),
    (ole("Something"), "x.bin", DocumentFormat.LEGACY_PPT),
    (ooxml(CT_DOCX, "word/document.xml"), "memo.docx", DocumentFormat.UNKNOWN),
    (ooxml(None), "plain.zip", DocumentFormat.UNKNOWN),
    (b"PK\x03\x04garbage-not-a-zip", "broken.pptx", DocumentFormat.UNKNOWN),
    (b"hello world, plain text", "notes.txt", DocumentFormat.UNKNOWN),
    (b"", "empty.pptx", DocumentFormat.UNKNOWN),
])
def test_sniff(blob, name, fmt):
    found, detail = sniff(blob, name)
    assert found == fmt
    assert detail


def test_sniff_parts_fallback_without_main_content_type():
    found, detail = sniff(ooxml("application/xml", "ppt/presentation.xml"), "deck.pptx")
    assert found == DocumentFormat.PPTX and "ppt/" in detail


def test_sniff_real_pptx_and_xlsx():
    pptx = pytest.importorskip("pptx")
    buf = io.BytesIO()
    pptx.Presentation().save(buf)
    assert sniff(buf.getvalue(), "real.pptx")[0] == DocumentFormat.PPTX
    openpyxl = pytest.importorskip("openpyxl")
    wb, buf2 = openpyxl.Workbook(), io.BytesIO()
    wb.save(buf2)
    assert sniff(buf2.getvalue(), "real.xlsx")[0] == DocumentFormat.XLSX


def test_sniff_pdf_with_leading_junk():
    assert sniff(b"\x00\x00junk" + pdf_bytes(), "x.pdf")[0] == DocumentFormat.PDF


def test_encrypted_detection_is_utf16_stream_name():
    assert sniff(OLE_MAGIC + b"\x00" * 100 + b"EncryptedPackage", "x.ppt")[0] == DocumentFormat.LEGACY_PPT


def test_register_file_stores_bytes_in_dated_inbox(mem_store, settings):
    blob = ooxml(CT_PPTX)
    doc = register_file(mem_store, settings, blob=blob, file_name="Kestrel Monthly Review (June).pptx",
                        uploaded_by="analyst@example.com")
    sha = sha256_bytes(blob)
    assert doc.doc_id == sha and doc.status == DocumentStatus.REGISTERED and doc.format == DocumentFormat.PPTX
    assert doc.size_bytes == len(blob) and doc.uploaded_by == "analyst@example.com" and doc.error is None
    p = Path(doc.file_path)
    assert p.read_bytes() == blob
    assert p.name == f"{sha[:12]}_Kestrel_Monthly_Review_June_.pptx"
    rel = p.relative_to(Path(settings.resolved_volume_path())).as_posix()
    assert rel.startswith(f"inbox/{doc.registered_at:%Y/%m/%d}/")
    events = mem_store.read(ReviewEvent, "doc_id = :d", {"d": sha})
    assert [e.event for e in events] == ["uploaded"] and events[0].actor == "analyst@example.com"


def test_register_file_is_idempotent(mem_store, settings):
    blob = pdf_bytes("same")
    first = register_file(mem_store, settings, blob=blob, file_name="a.pdf")
    mem_store.upsert([Document(**{**first.__dict__, "status": DocumentStatus.PROCESSED})])
    second = register_file(mem_store, settings, blob=blob, file_name="renamed.pdf", uploaded_by="other@example.com")
    assert second.doc_id == first.doc_id
    assert second.status == DocumentStatus.PROCESSED and second.file_name == "a.pdf"
    assert mem_store.count(Document) == 1
    assert mem_store.count(ReviewEvent) == 1


def test_register_encrypted_file_has_remedy(mem_store, settings):
    doc = register_file(mem_store, settings, blob=ole("EncryptedPackage"), file_name="locked.pptx")
    assert doc.status == DocumentStatus.ENCRYPTED and doc.format == DocumentFormat.ENCRYPTED_OOXML
    assert "remove the sensitivity label encryption or export to pdf" in doc.error.lower()
    assert Path(doc.file_path).exists()


def test_register_legacy_ppt_has_remedy(mem_store, settings):
    doc = register_file(mem_store, settings, blob=ole("PowerPoint Document"), file_name="old.ppt")
    assert doc.status == DocumentStatus.UNSUPPORTED_FORMAT and doc.format == DocumentFormat.LEGACY_PPT
    assert "save as .pptx" in doc.error


def test_register_legacy_xls_remedy(mem_store, settings):
    doc = register_file(mem_store, settings, blob=ole("Workbook"), file_name="old.xls")
    assert doc.status == DocumentStatus.UNSUPPORTED_FORMAT and "save as .xlsx" in doc.error


def test_register_unknown_file(mem_store, settings):
    doc = register_file(mem_store, settings, blob=b"just some text", file_name="notes.txt")
    assert doc.status == DocumentStatus.UNSUPPORTED_FORMAT and doc.format == DocumentFormat.UNKNOWN
    assert ".pptx" in doc.error and doc.sniff_detail


def test_register_in_place(mem_store, settings, tmp_path):
    src = tmp_path / "elsewhere" / "Kestrel_Executive_Summary_Q2-2026.pdf"
    src.parent.mkdir()
    src.write_bytes(pdf_bytes("inplace"))
    doc = register_file(mem_store, settings, blob=src.read_bytes(), file_name=src.name, source_path=str(src))
    assert doc.file_path == str(src) and doc.format == DocumentFormat.PDF
    assert not Path(settings.resolved_volume_path(), "inbox").exists()


def test_register_persists_and_reads_back(mem_store, settings):
    doc = register_file(mem_store, settings, blob=pdf_bytes("rt"), file_name="r.pdf")
    back = mem_store.get(Document, doc_id=doc.doc_id)
    assert back.format == DocumentFormat.PDF and back.status == DocumentStatus.REGISTERED
    assert back.metadata_proposals == {} and back.needs_confirmation == []


def test_register_inbox_registers_dropped_files_in_place(mem_store, settings):
    fs = files_for(settings)
    a = fs.write_bytes("inbox/2026/06/05/Kestrel_Monthly_Brand_Review_2026-06.pptx", ooxml(CT_PPTX))
    b = fs.write_bytes("inbox/dropped/scan.pdf", pdf_bytes("scan"))
    docs = register_inbox(mem_store, settings)
    assert sorted(d.file_path for d in docs) == sorted([a, b])
    by_path = {d.file_path: d for d in docs}
    assert by_path[a].file_name == "Kestrel_Monthly_Brand_Review_2026-06.pptx"
    assert by_path[b].format == DocumentFormat.PDF
    assert register_inbox(mem_store, settings) == []
    assert mem_store.count(Document) == 2


def test_register_inbox_skips_files_written_by_register_file(mem_store, settings):
    doc = register_file(mem_store, settings, blob=pdf_bytes("up"), file_name="upload.pdf")
    assert register_inbox(mem_store, settings) == []
    assert mem_store.count(Document) == 1 and mem_store.get(Document, doc_id=doc.doc_id) is not None


def test_register_inbox_strips_own_sha_prefix_on_a_fresh_store(mem_store, settings):
    blob = pdf_bytes("fresh")
    fs = files_for(settings)
    path = fs.write_bytes(f"inbox/2026/06/05/{sha256_bytes(blob)[:12]}_Kestrel_Field_Update_scan.pdf", blob)
    [doc] = register_inbox(mem_store, settings)
    assert doc.file_path == path and doc.file_name == "Kestrel_Field_Update_scan.pdf"


def test_register_inbox_empty(mem_store, settings):
    assert register_inbox(mem_store, settings) == []


def test_register_inbox_orders_files_by_original_name(mem_store, settings):
    fs = files_for(settings)
    second = pdf_bytes("deck v2")
    first = pdf_bytes("deck v1")
    late = fs.write_bytes("inbox/2026/06/01/Brand_Review_v2.pdf", second)
    early = fs.write_bytes(f"inbox/2026/06/09/{sha256_bytes(first)[:12]}_Brand_Review.pdf", first)
    docs = register_inbox(mem_store, settings)
    assert [d.file_path for d in docs] == [early, late]
    assert [d.file_name for d in docs] == ["Brand_Review.pdf", "Brand_Review_v2.pdf"]
