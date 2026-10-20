from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta
from typing import Any

from kpi_evidence.contracts import (
    Document, DocumentFormat, DocumentPage, DocumentStatus, ExtractedValue, ExtractionMethod,
)
from kpi_evidence.ids import stable_id

NOW = datetime(2026, 6, 5, 12, 0, 0)
CT_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
CT_PPTM = "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
CT_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
CT_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
OLE_MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"


def ooxml(content_type: str | None, main_part: str = "ppt/presentation.xml", extra: bytes = b"") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if content_type is not None:
            z.writestr("[Content_Types].xml",
                       '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
                       f'package/2006/content-types"><Override PartName="/{main_part}" ContentType="{content_type}"/>'
                       "</Types>")
        z.writestr(main_part, b"<x/>" + extra)
    return buf.getvalue()


def ole(*stream_names: str) -> bytes:
    body = b"".join(name.encode("utf-16-le") + b"\x00" * 16 for name in stream_names)
    return OLE_MAGIC + b"\x00" * 504 + body


def pdf_bytes(tag: str = "") -> bytes:
    return b"%PDF-1.7\n%" + tag.encode() + b"\n1 0 obj <<>> endobj\ntrailer <<>>\n%%EOF"


def make_page(page_number: int, title: str | None, text: str = "", *, doc_id: str = "d" * 64) -> DocumentPage:
    return DocumentPage(doc_id=doc_id, page_number=page_number, title=title, text=text or (title or ""), notes=None,
                        hidden=False, image_count=0, page_width=None, page_height=None, shapes=[], created_at=NOW)


def make_value(raw_text: str, *, doc_id: str = "d" * 64, method: ExtractionMethod = ExtractionMethod.NATIVE_CHART,
               **context: Any) -> ExtractedValue:
    loc = {"page": 2, "n": stable_id(raw_text, sorted((k, str(v)) for k, v in context.items()))}
    return ExtractedValue(value_id=stable_id(doc_id, method.value, loc), doc_id=doc_id, method=method, extractor="test",
                          extractor_version="0", locator=loc, slide_or_page=2, context=context, raw_text=raw_text,
                          raw_number=None, number_format=None, flags=[], confidence=1.0, created_at=NOW)


def trend_values(doc_id: str = "d" * 64, months=("Dec-25", "Jan-26", "Feb-26", "Mar-26", "Apr-26", "May-26")):
    return [make_value(str(5700 + i), doc_id=doc_id, category=m, series_name="TRx") for i, m in enumerate(months)]


def make_doc(file_name: str, *, doc_id: str | None = None, registered_at: datetime | None = None,
             status: DocumentStatus = DocumentStatus.EXTRACTED, props: dict | None = None, **fields: Any) -> Document:
    doc_id = doc_id or stable_id("doc", file_name, length=64)
    return Document(doc_id=doc_id, file_name=file_name, file_path=f"/vol/inbox/{file_name}", size_bytes=10,
                    format=DocumentFormat.PDF if file_name.endswith(".pdf") else DocumentFormat.PPTX, status=status,
                    registered_at=registered_at or NOW, updated_at=registered_at or NOW,
                    metadata_proposals={"_document_properties": props} if props else {}, **fields)


def later(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)
