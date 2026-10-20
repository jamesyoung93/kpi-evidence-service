from __future__ import annotations

import io
import traceback
from typing import Any

from ..contracts import DocumentFormat, DocumentPage, ExtractionMethod
from ..ids import sha256_bytes, utcnow
from . import ExtractionOutput
from ._common import EXTRACTION, build_value, flag, issue

_IMAGE_FORMATS = (DocumentFormat.PNG, DocumentFormat.JPEG, DocumentFormat.TIFF)
_REMEDIES = {
    DocumentFormat.ENCRYPTED_OOXML: ("ENCRYPTED", "The file is an encrypted Office package: remove the sensitivity "
                                                  "label encryption or export to PDF, then upload again."),
    DocumentFormat.LEGACY_PPT: ("UNSUPPORTED_FORMAT", "Legacy binary PowerPoint (.ppt): save as .pptx and upload again."),
}


def extract_image_values(doc_id: str, blob: bytes, file_name: str, run_id: str | None = None) -> ExtractionOutput:
    """A standalone screenshot/scan: one placeholder value for the whole image and one page (size in pixels)."""
    out = ExtractionOutput(extractor_versions={"image_adapter": "kpi_evidence.extract.router/1"})
    width = height = None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(blob)) as image:
            width, height = (float(v) for v in image.size)
            out.document_properties = {"pixel_width": int(width), "pixel_height": int(height),
                                       "image_format": image.format, "frames": int(getattr(image, "n_frames", 1))}
    except Exception as exc:
        out.issues.append(issue("IMAGE_UNREADABLE", f"{type(exc).__name__}: {exc}", page=1))
    out.document_properties["page_count"] = 1
    created_at = utcnow()
    bbox = [0.0, 0.0, width, height] if width and height else None
    out.values.append(build_value(
        doc_id=doc_id, method=ExtractionMethod.IMAGE_PLACEHOLDER, extractor="image", extractor_version="kpe.1",
        locator={"page": 1, "bbox": bbox},
        context={"object_kind": "image", "media_sha256": sha256_bytes(blob), "bbox_unit": "px" if bbox else None,
                 "area_fraction": 1.0},
        raw_text=None, raw_number=None, number_format=None,
        flags=[flag("IMAGE_NOT_PARSED", EXTRACTION, "Screenshot or scan: numbers need OCR / ai_parse_document.")],
        confidence=0.0, run_id=run_id, created_at=created_at))
    out.pages.append(DocumentPage(doc_id=doc_id, page_number=1, title=None, text="", notes=None, hidden=False,
                                  image_count=1, page_width=width, page_height=height,
                                  shapes=[{"shape_id": "image", "kind": "picture", "name": file_name, "bbox": bbox}],
                                  created_at=created_at))
    return out


def _route(fmt: DocumentFormat):
    if fmt in (DocumentFormat.PPTX, DocumentFormat.PPTM):
        from .pptx import extract_pptx_values

        return "pptx", extract_pptx_values
    if fmt == DocumentFormat.XLSX:
        from .xlsx import extract_xlsx_values

        return "xlsx", extract_xlsx_values
    if fmt == DocumentFormat.PDF:
        from .pdf import extract_pdf_values

        return "pdf", extract_pdf_values
    if fmt in _IMAGE_FORMATS:
        return "image", extract_image_values
    return None, None


def extract_document(settings: Any, doc: Any, blob: bytes, *, run_id: str | None = None) -> ExtractionOutput:
    """Extract one document's values and pages."""
    try:
        fmt = DocumentFormat(getattr(doc.format, "value", doc.format))
    except ValueError:
        fmt = DocumentFormat.UNKNOWN
    name, route = _route(fmt)
    if route is None:
        code, remedy = _REMEDIES.get(fmt, ("UNSUPPORTED_FORMAT", f"Format {fmt.value} cannot be extracted. Upload a "
                                                                   ".pptx, .xlsx, .pdf, .png, .jpg or .tif file."))
        return ExtractionOutput(issues=[issue(code, remedy, severity="error", format=fmt.value)],
                                extractor_versions={"route": "none"})
    try:
        out = route(doc.doc_id, blob, doc.file_name, run_id)
    except Exception as exc:
        out = ExtractionOutput(issues=[issue("EXTRACTION_FAILED", f"{type(exc).__name__}: {exc}", severity="error",
                                             trace=traceback.format_exc(limit=3))])
    out.extractor_versions["route"] = name
    if sha256_bytes(blob) != doc.doc_id:
        out.issues.append(issue("CONTENT_HASH_MISMATCH", "the bytes passed in do not hash to doc_id; values are keyed by doc_id"))
    return out
