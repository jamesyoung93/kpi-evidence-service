from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any

from ..contracts import DocumentPage, ExtractedValue, ExtractionMethod
from ..ids import utcnow
from . import ExtractionOutput
from ._common import (
    EXTRACTION, MEANING, NUM, analyze_grid, build_value, cell_number_text, derived_change_flag, finalize_values,
    find_labeled_numbers, flag, footnote_mark_flag, is_footnote, is_note_line, issue, text_cell_kind,
)

ADAPTER_VERSION = "1"
EXTRACTOR = "pdfplumber"
TABLE_CONFIDENCE = 0.9
TEXT_CONFIDENCE = 0.7
LARGE_IMAGE_FRACTION = 0.15
_LETTERS = re.compile(r"[^\W\d_]{2,}")
_PDF_DATE = re.compile(r"^D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?")
_MARK_CHAR = re.compile(r"[\d*†‡§,]")
_BASE_CHAR = re.compile(r"[\d%)]")


def extractor_versions() -> dict[str, str]:
    import pdfplumber

    return {"pdfplumber": str(pdfplumber.__version__), "pdf_adapter": f"kpi_evidence.extract.pdf/{ADAPTER_VERSION}"}


def _pdf_date(raw: Any) -> str | None:
    if raw is None:
        return None
    text = raw.decode("latin-1", "ignore") if isinstance(raw, bytes) else str(raw)
    m = _PDF_DATE.match(text.strip())
    if not m:
        return text.strip() or None
    parts = [int(p) if p else d for p, d in zip(m.groups(), (0, 1, 1, 0, 0, 0), strict=True)]
    try:
        return datetime(*parts).isoformat()
    except ValueError:
        return text.strip()


def _properties(metadata: dict[str, Any] | None) -> dict[str, Any]:
    meta = metadata or {}
    out = {"title": meta.get("Title"), "subject": meta.get("Subject"), "keywords": meta.get("Keywords"),
           "created": _pdf_date(meta.get("CreationDate")), "modified": _pdf_date(meta.get("ModDate"))}
    return {k: (v.decode("latin-1", "ignore") if isinstance(v, bytes) else v) for k, v in out.items()
            if v not in (None, "", b"")}


def _box(x0: float, top: float, x1: float, bottom: float) -> list[float]:
    return [round(float(x0), 2), round(float(top), 2), round(float(x1) - float(x0), 2), round(float(bottom) - float(top), 2)]


def _inside(line: dict[str, Any], boxes: list[tuple[float, float, float, float]]) -> bool:
    cx, cy = (line["x0"] + line["x1"]) / 2, (line["top"] + line["bottom"]) / 2
    return any(x0 - 1 <= cx <= x1 + 1 and top - 1 <= cy <= bottom + 1 for x0, top, x1, bottom in boxes)


class _PageRun:
    def __init__(self, doc_id: str, run_id: str | None, created_at: datetime, page_number: int, version: str):
        self.doc_id, self.run_id, self.created_at, self.page, self.version = doc_id, run_id, created_at, page_number, version

    def value(self, method: ExtractionMethod, **kw: Any) -> ExtractedValue:
        return build_value(doc_id=self.doc_id, method=method, extractor=EXTRACTOR, extractor_version=self.version,
                           run_id=self.run_id, created_at=self.created_at, **kw)


def _placeholder(run: _PageRun, bbox: list[float], *, title: str | None, detail: str, area: float | None,
                 image_index: int | None = None) -> ExtractedValue:
    context = {"object_kind": "image", "page_title": title, "area_fraction": round(area, 4) if area is not None else None}
    return run.value(ExtractionMethod.IMAGE_PLACEHOLDER, locator={"page": run.page, "image_index": image_index, "bbox": bbox},
                     context=context, raw_text=None, raw_number=None, number_format=None,
                     flags=[flag("IMAGE_NOT_PARSED", EXTRACTION, detail)], confidence=0.0)


def _title(lines: list[dict[str, Any]]) -> str | None:
    sizes = [max((c.get("size") or 0.0) for c in ln.get("chars") or [{}]) for ln in lines]
    biggest = max(sizes, default=0.0)
    for ln, size in zip(lines, sizes, strict=True):
        text = (ln.get("text") or "").strip()
        if text and _LETTERS.search(text) and size >= 0.95 * biggest:
            return text
    return None


def _raised_marks(chars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_edge: dict[int, list[dict[str, Any]]] = {}
    for ch in chars:
        by_edge.setdefault(int(round(float(ch["x1"]))), []).append(ch)
    marks: list[dict[str, Any]] = []
    marked: set[int] = set()
    for ch in sorted(chars, key=lambda c: float(c["x0"])):
        if not _MARK_CHAR.fullmatch(ch.get("text") or ""):
            continue
        x0, size = float(ch["x0"]), float(ch.get("size") or 0.0)
        near = [p for k in range(int(round(x0)) - 2, int(round(x0)) + 3) for p in by_edge.get(k, [])
                if p is not ch and -1.0 <= x0 - float(p["x1"]) <= max(1.0, 0.3 * float(p.get("size") or 0.0))
                and float(p["top"]) < float(ch["bottom"]) and float(ch["top"]) < float(p["bottom"])]
        for p in sorted(near, key=lambda p: abs(x0 - float(p["x1"]))):
            p_size = float(p.get("size") or 0.0)
            if id(p) in marked:
                chained = abs(p_size - size) < 0.5 and abs(float(p["bottom"]) - float(ch["bottom"])) < 0.5
            else:
                chained = (_BASE_CHAR.fullmatch(p.get("text") or "") is not None and 0 < size <= 0.85 * p_size
                           and float(ch["bottom"]) < float(p["bottom"]) - 0.15 * p_size)
            if chained:
                marks.append(ch)
                marked.add(id(ch))
                break
    return marks


def _mark_after(marks: list[dict[str, Any]], x1: float, top: float, bottom: float) -> bool:
    return any(abs(float(m["x0"]) - x1) <= 2.0 and float(m["bottom"]) > top - 1 and float(m["top"]) < bottom + 1
               for m in marks)


def _mark_inside(marks: list[dict[str, Any]], cell: tuple[float, float, float, float] | None) -> bool:
    if cell is None:
        return False
    x0, top, x1, bottom = (float(v) for v in cell)
    return any(x0 <= (float(m["x0"]) + float(m["x1"])) / 2 <= x1 and top <= (float(m["top"]) + float(m["bottom"])) / 2 <= bottom
               for m in marks)


def _table_values(run: _PageRun, table, ti: int, *, title: str | None, caption: str | None,
                  footnotes: list[str], marks: list[dict[str, Any]] | None = None) -> list[ExtractedValue]:
    rows = table.extract() or []
    texts = [[" ".join((c or "").split()) for c in row] for row in rows]
    classified = [[text_cell_kind(t) for t in row] for row in texts]
    kinds = [[k for k, _p in row] for row in classified]
    layout = analyze_grid(texts, kinds)
    boxes = [list(r.cells) for r in table.rows]
    out = []
    for r, row in enumerate(classified):
        for c, (kind, parts) in enumerate(row):
            if kind != NUM or (r, c) in layout.header_cells:
                continue
            cell = boxes[r][c] if r < len(boxes) and c < len(boxes[r]) else None
            col_header = layout.col_headers[c] if c < len(layout.col_headers) else None
            left = layout.labels_left_of(texts, kinds, r, c)
            raw_text, mark_flags = cell_number_text(texts[r][c])
            if _mark_inside(marks or [], cell):
                mark_flags.append(footnote_mark_flag())
            context = {"object_kind": "pdf_table", "page_title": title, "object_title": caption,
                       "row_label": layout.row_labels[r], "col_header": col_header, "header_row": layout.header_row(),
                       "row_labels": left if len(left) > 1 else None, "nearby_text": [caption] if caption else None,
                       "footnotes": footnotes, "number_source": "text"}
            out.append(run.value(
                ExtractionMethod.PDF_TABLE,
                locator={"page": run.page, "table_index": ti, "row": r, "col": c,
                         "bbox": _box(*cell) if cell else _box(*table.bbox)},
                context=context, raw_text=raw_text, raw_number=parts.number, number_format=None,
                flags=derived_change_flag(col_header, layout.row_labels[r]) + mark_flags, confidence=TABLE_CONFIDENCE))
    return out


def _word_box(words: list[dict[str, Any]], line: dict[str, Any], raw: str) -> list[float] | None:
    target = raw.strip()
    for w in words:
        if (w["top"] >= line["top"] - 1 and w["bottom"] <= line["bottom"] + 1 and w["x0"] >= line["x0"] - 1
                and w["x1"] <= line["x1"] + 1 and w["text"].strip().rstrip(",;:") == target):
            return _box(w["x0"], w["top"], w["x1"], w["bottom"])
    return None


def _neighbor_label(lines: list[dict[str, Any]], usable: list[bool], li: int) -> str | None:
    line = lines[li]
    height = max(line["bottom"] - line["top"], 1.0)
    for j in (li - 1, li + 1):
        if 0 <= j < len(lines) and usable[j]:
            other = lines[j]
            text = (other.get("text") or "").strip()
            close = abs(other["top"] - line["top"]) <= 2.5 * height
            overlap = min(other["x1"], line["x1"]) - max(other["x0"], line["x0"]) > 0
            if text and close and overlap and _LETTERS.search(text) and not find_labeled_numbers(text):
                return text
    return None


def _text_values(run: _PageRun, lines: list[dict[str, Any]], usable: list[bool], words: list[dict[str, Any]], *,
                 title: str | None, footnotes: list[str], marks: list[dict[str, Any]] | None = None) -> list[ExtractedValue]:
    out = []
    for li, line in enumerate(lines):
        if not usable[li]:
            continue
        text = line.get("text") or ""
        for hit in find_labeled_numbers(text):
            flags: list[dict[str, Any]] = []
            label = hit.label
            if hit.label_source == "inherited":
                flags.append(flag("ROW_LABEL_INHERITED", MEANING,
                                  "No text next to this number; the label of the previous number on the line was used."))
            if label is None:
                label = _neighbor_label(lines, usable, li)
                if label is None:
                    continue
                flags.append(flag("LABEL_FROM_ADJACENT_LINE", MEANING,
                                  f"The label {label!r} is on the neighboring line; proximity is not proof."))
            flags += derived_change_flag(hit.label_before, hit.label_after)
            previous = next((lines[j]["text"].strip() for j in range(li - 1, -1, -1) if usable[j]), None)
            context = {"object_kind": "pdf_text", "page_title": title, "text_line": text.strip(), "row_label": label,
                       "label_before": hit.label_before, "label_after": hit.label_after,
                       "nearby_text": [previous] if previous else None, "footnotes": footnotes,
                       "number_source": "text"}
            word = _word_box(words, line, hit.raw_text)
            if word and _mark_after(marks or [], word[0] + word[2], word[1], word[1] + word[3]):
                flags.append(footnote_mark_flag())
            bbox = word or _box(line["x0"], line["top"], line["x1"], line["bottom"])
            out.append(run.value(ExtractionMethod.PDF_TEXT,
                                 locator={"page": run.page, "line": li, "span": [hit.start, hit.end], "bbox": bbox},
                                 context=context, raw_text=hit.raw_text, raw_number=hit.parts.number,
                                 number_format=None, flags=flags, confidence=TEXT_CONFIDENCE))
    return out


def _extract_page(run: _PageRun, page) -> tuple[list[ExtractedValue], DocumentPage]:
    width, height = float(page.width), float(page.height)
    area = width * height or None
    images = list(page.images or [])
    shapes: list[dict[str, Any]] = []
    for ii, im in enumerate(images):
        shapes.append({"shape_id": f"image{ii}", "kind": "picture", "name": im.get("name") or f"Image {ii + 1}",
                       "bbox": _box(im["x0"], im["top"], im["x1"], im["bottom"])})
    has_text = any((c.get("text") or "").strip() for c in page.chars)
    values: list[ExtractedValue] = []
    if not has_text:
        values.append(_placeholder(run, [0.0, 0.0, width, height], title=None, area=1.0,
                                   detail="The page has no text layer (scanned or image-only); numbers need OCR / ai_parse_document."))
        doc_page = DocumentPage(doc_id=run.doc_id, page_number=run.page, title=None, text="", notes=None, hidden=False,
                                image_count=len(images), page_width=width, page_height=height, shapes=shapes,
                                created_at=run.created_at)
        return values, doc_page

    marks = _raised_marks(page.chars)
    mark_ids = frozenset(id(m) for m in marks)
    view = page.filter(lambda obj: id(obj) not in mark_ids) if marks else page
    lines = view.extract_text_lines(return_chars=True)
    tables = view.find_tables()
    table_boxes = [tuple(t.bbox) for t in tables]
    title = _title(lines)
    footnotes = list(dict.fromkeys((ln.get("text") or "").strip() for ln in lines if is_footnote(ln.get("text") or "")))
    in_table = [_inside(ln, table_boxes) for ln in lines]
    for ti, table in enumerate(tables):
        x0, top, x1, bottom = table.bbox
        shapes.append({"shape_id": f"table{ti}", "kind": "table", "name": f"Table {ti + 1}", "bbox": _box(x0, top, x1, bottom)})
        above = [ln for ln, inside in zip(lines, in_table, strict=True) if not inside and ln["bottom"] <= top + 1
                 and top - ln["bottom"] <= 36 and (ln.get("text") or "").strip()]
        caption = above[-1]["text"].strip() if above else None
        values += _table_values(run, table, ti, title=title, caption=caption, footnotes=footnotes, marks=marks)
    usable = [not inside and not is_note_line(ln.get("text") or "") for ln, inside in zip(lines, in_table, strict=True)]
    words = view.extract_words()
    values += _text_values(run, lines, usable, words, title=title, footnotes=footnotes, marks=marks)
    for ii, im in enumerate(images):
        frac = ((im["x1"] - im["x0"]) * (im["bottom"] - im["top"]) / area) if area else None
        if frac is not None and frac >= LARGE_IMAGE_FRACTION:
            values.append(_placeholder(run, _box(im["x0"], im["top"], im["x1"], im["bottom"]), title=title, area=frac,
                                       image_index=ii, detail="A large image on this page may contain numbers; it was not read."))
    doc_page = DocumentPage(doc_id=run.doc_id, page_number=run.page, title=title, text=view.extract_text() or "",
                            notes=None, hidden=False, image_count=len(images), page_width=width, page_height=height,
                            shapes=shapes, created_at=run.created_at)
    return values, doc_page


def extract_pdf_values(doc_id: str, blob: bytes, file_name: str, run_id: str | None = None) -> ExtractionOutput:
    """Digital PDF -> values + one ``DocumentPage`` per page."""
    import pdfplumber

    out = ExtractionOutput(extractor_versions=extractor_versions())
    if b"%PDF" not in blob[:1024]:
        out.issues.append(issue("NOT_A_PDF", f"{file_name}: no %PDF header", severity="error"))
        return out
    try:
        pdf = pdfplumber.open(io.BytesIO(blob))
    except Exception as exc:
        out.issues.append(issue("PDF_OPEN_FAILED", f"{type(exc).__name__}: {exc}", severity="error"))
        return out
    created_at = utcnow()
    version = f"{out.extractor_versions['pdfplumber']}+kpe.{ADAPTER_VERSION}"
    with pdf:
        try:
            out.document_properties = _properties(pdf.metadata)
            pages = list(pdf.pages)
        except Exception as exc:
            out.issues.append(issue("PDF_OPEN_FAILED", f"{type(exc).__name__}: {exc}", severity="error"))
            return out
        out.document_properties["page_count"] = len(pages)
        for number, page in enumerate(pages, 1):
            run = _PageRun(doc_id, run_id, created_at, number, version)
            try:
                values, doc_page = _extract_page(run, page)
            except Exception as exc:
                out.issues.append(issue("PAGE_EXTRACTION_FAILED", f"{type(exc).__name__}: {exc}", page=number))
                values, doc_page = [], DocumentPage(doc_id=doc_id, page_number=number, title=None, text="", notes=None,
                                                    hidden=False, image_count=0, page_width=None, page_height=None,
                                                    shapes=[], created_at=created_at)
            out.values += values
            out.pages.append(doc_page)
            page.flush_cache()
    out.values = finalize_values(out.values, out.issues)
    return out
