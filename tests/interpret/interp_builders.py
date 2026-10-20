from __future__ import annotations

from datetime import date, datetime
from typing import Any

from kpi_evidence.contracts import (
    Document, DocumentFormat, DocumentPage, DocumentStatus, ExtractedValue, ExtractionMethod,
)
from kpi_evidence.ids import stable_id

NOW = datetime(2026, 6, 5, 12, 0, 0)
DIMS = {
    "region": {"East", "North", "South", "West"},
    "district": {"West District 1", "West District 2", "East District 1"},
    "specialty": {"Cardiology", "Endocrinology", "Primary Care"},
    "payer_channel": {"Cash", "Commercial", "Medicaid", "Medicare Part D"},
}
_CONFIDENCE = {ExtractionMethod.SLIDE_TEXT: 0.7, ExtractionMethod.PDF_TEXT: 0.7, ExtractionMethod.PDF_TABLE: 0.9,
               ExtractionMethod.IMAGE_PLACEHOLDER: 0.0}


def make_doc(**overrides: Any) -> Document:
    values = dict(doc_id="d" * 64, file_name="Kestrel_Monthly_Brand_Review_2026-06.pptx", file_path="/tmp/x.pptx",
                  size_bytes=1000, format=DocumentFormat.PPTX, status=DocumentStatus.READY, registered_at=NOW,
                  updated_at=NOW, brand="KESTREL", report_type="monthly_brand_review", period_label="May 2026",
                  period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
                  publication_date=date(2026, 6, 1))
    overrides.pop("period_grain", None)
    values.update(overrides)
    return Document(**values)


def make_value(raw_text: str | None, *, raw_number: float | None = None, method: ExtractionMethod = ExtractionMethod.NATIVE_TABLE,
               number_format: str | None = None, page: int = 2, confidence: float | None = None,
               flags: list[dict[str, str]] | None = None, doc_id: str = "d" * 64, locator: dict | None = None,
               **context: Any) -> ExtractedValue:
    loc = {"page": page, **(locator or {})}
    if not locator:
        loc["n"] = stable_id(raw_text, sorted((k, str(v)) for k, v in context.items()))
    ctx = {k: v for k, v in context.items() if v is not None}
    return ExtractedValue(
        value_id=stable_id(doc_id, method.value, loc), doc_id=doc_id, method=method, extractor="test",
        extractor_version="0", locator=loc, slide_or_page=page, context=ctx, raw_text=raw_text, raw_number=raw_number,
        number_format=number_format, flags=list(flags or []),
        confidence=_CONFIDENCE.get(method, 1.0) if confidence is None else confidence, created_at=NOW)


def chart_point(raw_number: float, *, category: str, series_name: str = "TRx", number_format: str | None = "#,##0",
                **context: Any) -> ExtractedValue:
    context.setdefault("object_kind", "chart")
    context.setdefault("number_source", "stored")
    return make_value(f"{raw_number}", raw_number=raw_number, method=ExtractionMethod.NATIVE_CHART,
                      number_format=number_format, category=category, series_name=series_name, **context)


def table_cell(raw_text: str, *, row_label: str, col_header: str | None = None, **context: Any) -> ExtractedValue:
    context.setdefault("object_kind", "table")
    context.setdefault("number_source", "text")
    return make_value(raw_text, method=ExtractionMethod.NATIVE_TABLE, row_label=row_label, col_header=col_header,
                      **context)


def text_value(raw_text: str, line: str, **context: Any) -> ExtractedValue:
    context.setdefault("object_kind", "slide_text")
    context.setdefault("number_source", "text")
    return make_value(raw_text, method=ExtractionMethod.SLIDE_TEXT, text_line=line, **context)


def make_page(page_number: int, title: str | None, text: str = "", *, hidden: bool = False,
              doc_id: str = "d" * 64) -> DocumentPage:
    return DocumentPage(doc_id=doc_id, page_number=page_number, title=title, text=text or (title or ""), notes=None,
                        hidden=hidden, image_count=0, page_width=12192000.0, page_height=6858000.0, shapes=[],
                        created_at=NOW)


def codes(obs) -> set[str]:
    return {f["code"] for f in obs.flags}
