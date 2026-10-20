from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

from ..contracts import DocumentPage, ExtractedValue, ExtractionMethod
from ..ids import utcnow
from ..store.base import sql_literal
from . import ExtractionOutput, parse_number_parts
from ._common import (
    ASSOCIATION, EXTRACTION, MEANING, NUM, analyze_grid, build_value, cell_number_text, derived_change_flag,
    finalize_values, find_labeled_numbers, flag, is_footnote, is_note_line, issue, number_text, text_cell_kind,
)

ADAPTER_VERSION = "1"
EXTRACTOR = "ai_parse_document"
SUPPORTED_VERSIONS = ("2.0",)
CHART_CONFIDENCE_CAP = 0.5
_VOLUME_FILE = re.compile(r"^/Volumes/[^'\"\\*?\[\]{}\s][^'\"\\*?\[\]{}]*[^'\"\\*?\[\]{}\s/]$")
_TEXT_TYPES = frozenset({"text", "title", "caption", "section_header", "page_header", "page_footer", "footnote"})
_FOOTNOTE_TYPES = frozenset({"footnote", "page_footer"})
_MARKDOWN = re.compile(r"\*\*|__|`")


def _check_volume_path(path: str, *, directory: bool = False) -> str:
    text = str(path).rstrip("/") if directory else str(path)
    if not _VOLUME_FILE.match(text):
        raise ValueError(f"expected an exact /Volumes/... path without quotes or glob characters, got {path!r}")
    return text + "/" if directory else text


def build_ai_parse_sql(volume_paths: Sequence[str], version: str = "2.0", image_output_path: str | None = None, *,
                       description_element_types: str = "figure") -> str:
    """SQL that parses exactly these files: one ``read_files`` per path, ``UNION ALL``."""
    if isinstance(volume_paths, str):
        volume_paths = [volume_paths]
    paths = [_check_volume_path(p) for p in volume_paths]
    if not paths:
        raise ValueError("build_ai_parse_sql needs at least one Volume path")
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f"ai_parse_document version must be one of {SUPPORTED_VERSIONS} (pinned), got {version!r}")
    if description_element_types not in ("", "figure", "*"):
        raise ValueError("description_element_types must be '', 'figure' or '*'")
    options = ["'version'", sql_literal(version)]
    if image_output_path:
        options += ["'imageOutputPath'", sql_literal(_check_volume_path(image_output_path, directory=True))]
    options += ["'descriptionElementTypes'", sql_literal(description_element_types)]
    option_map = "map(" + ", ".join(options) + ")"
    selects = [
        "SELECT path, length, sha2(content, 256) AS content_sha256,\n"
        f"       to_json(ai_parse_document(content, {option_map})) AS parsed_json\n"
        f"FROM read_files({sql_literal(p)}, format => 'binaryFile')"
        for p in dict.fromkeys(paths)
    ]
    return "\nUNION ALL\n".join(selects)


class _HTMLTable(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, int, bool]]] = []
        self._cell: list[str] | None = None
        self._attrs: dict[str, str] = {}
        self._header = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            if not self.rows:
                self.rows.append([])
            self._cell, self._attrs, self._header = [], {k: v or "" for k, v in attrs}, tag == "th"
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            self.rows[-1].append((" ".join("".join(self._cell).split()), _span(self._attrs.get("colspan")),
                                  _span(self._attrs.get("rowspan")), self._header))
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _span(raw: str | None) -> int:
    try:
        return max(1, min(int(raw or 1), 1000))
    except ValueError:
        return 1


def html_table_grid(html: str) -> tuple[list[list[str]], list[tuple[int, int, int]], int | None]:
    """Table HTML -> (cell texts on a rectangular grid, colspans for header composition, leading <th> rows)."""
    parser = _HTMLTable()
    parser.feed(html or "")
    parser.close()
    placed: dict[tuple[int, int], str] = {}
    is_th: dict[int, bool] = {}
    spans: list[tuple[int, int, int]] = []
    for r, row in enumerate(parser.rows):
        c = 0
        is_th[r] = bool(row) and all(cell[3] for cell in row)
        for text, colspan, rowspan, _th in row:
            while (r, c) in placed:
                c += 1
            for dr in range(rowspan):
                for dc in range(colspan):
                    placed[(r + dr, c + dc)] = text if dr == 0 and dc == 0 else ""
            if colspan > 1:
                spans.append((r, c, colspan))
            c += colspan
    nrows = max((r for r, _c in placed), default=-1) + 1
    ncols = max((c for _r, c in placed), default=-1) + 1
    grid = [[placed.get((r, c), "") for c in range(ncols)] for r in range(nrows)]
    hint = 0
    while hint < nrows and is_th.get(hint):
        hint += 1
    return grid, spans, (hint or None)


def _bbox(element: dict[str, Any]) -> tuple[int | None, list[float] | None]:
    boxes = element.get("bbox") or []
    if isinstance(boxes, dict):
        boxes = [boxes]
    if not boxes:
        return None, None
    first = boxes[0] or {}
    page_id = first.get("page_id")
    coord = first.get("coord") or []
    box = None
    if len(coord) == 4:
        x0, y0, x1, y1 = (float(v) for v in coord)
        box = [x0, y0, x1 - x0, y1 - y0]
    return (int(page_id) if page_id is not None else None), box


def _chart_series(content: Any) -> tuple[dict[str, Any], list[tuple[int, str | None, int, str | None, Any]]]:
    obj = content
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return {}, []
    if isinstance(obj, list):
        obj = {"series": obj}
    if not isinstance(obj, dict):
        return {}, []

    def title_of(axis: Any) -> str | None:
        return axis.get("title") if isinstance(axis, dict) else (axis if isinstance(axis, str) else None)

    x_axis = obj.get("x_axis") or obj.get("xAxis") or obj.get("category_axis") or {}
    y_axis = obj.get("y_axis") or obj.get("yAxis") or obj.get("value_axis") or {}
    categories = (obj.get("categories") or obj.get("labels") or obj.get("x")
                  or (x_axis.get("labels") or x_axis.get("categories") if isinstance(x_axis, dict) else None) or [])
    meta = {"title": obj.get("title"), "chart_type": obj.get("type") or obj.get("chart_type"),
            "axis_title": title_of(y_axis), "category_axis_title": title_of(x_axis)}
    points: list[tuple[int, str | None, int, str | None, Any]] = []
    for si, series in enumerate(obj.get("series") or obj.get("datasets") or []):
        if not isinstance(series, dict):
            continue
        name = series.get("name") or series.get("label") or series.get("series_name")
        raw = series.get("values") or series.get("data") or series.get("points") or series.get("y") or []
        for pi, item in enumerate(raw if isinstance(raw, list) else []):
            if isinstance(item, dict):
                category = item.get("x", item.get("label", item.get("category", item.get("name"))))
                value = item.get("y", item.get("value"))
            else:
                category = categories[pi] if pi < len(categories) else None
                value = item
            points.append((si, str(name) if name is not None else None, pi,
                           str(category) if category is not None else None, value))
    return meta, points


class _Builder:
    def __init__(self, doc_id: str, run_id: str | None, version: str, created_at: datetime):
        self.doc_id, self.run_id, self.version, self.created_at = doc_id, run_id, version, created_at
        self.parsed_flag = flag("AI_PARSED", EXTRACTION,
                                f"Read by ai_parse_document v{version} (model-derived); not corroborated by a text layer.")

    def value(self, method: ExtractionMethod, *, confidence: float, flags: list[dict[str, Any]], **kw: Any) -> ExtractedValue:
        return build_value(doc_id=self.doc_id, method=method, extractor=EXTRACTOR,
                           extractor_version=f"{self.version}+kpe.{ADAPTER_VERSION}", run_id=self.run_id,
                           created_at=self.created_at, flags=flags, confidence=confidence, **kw)

    def placeholder(self, page: int | None, element_id: Any, bbox: list[float] | None, detail: str,
                    context: dict[str, Any]) -> ExtractedValue:
        return self.value(ExtractionMethod.IMAGE_PLACEHOLDER, confidence=0.0,
                          flags=[flag("IMAGE_NOT_PARSED", EXTRACTION, detail)],
                          locator={"page": page, "element_id": element_id, "bbox": bbox},
                          context={"object_kind": "image", "bbox_unit": "px" if bbox else None, **context},
                          raw_text=None, raw_number=None, number_format=None)


def _element_confidence(element: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    conf = element.get("confidence")
    try:
        value = float(conf)
    except (TypeError, ValueError):
        return 0.0, [flag("AI_CONFIDENCE_MISSING", EXTRACTION, "ai_parse_document returned no confidence for this element.")]
    return max(0.0, min(1.0, value)), []


def parse_ai_document_output(doc_id: str, parsed_json: dict[str, Any] | str | None, *,
                             run_id: str | None = None) -> ExtractionOutput:
    """``ai_parse_document`` v2.0 output -> ``AI_PARSE`` values (+ placeholders for unreadable parts) and pages."""
    out = ExtractionOutput()
    parsed = json.loads(parsed_json) if isinstance(parsed_json, str) else (parsed_json or {})
    metadata = parsed.get("metadata") or {}
    version = str(metadata.get("version") or "2.0")
    out.extractor_versions = {"ai_parse_document": version, "ai_parse_adapter": f"kpi_evidence.extract.ai_parse/{ADAPTER_VERSION}"}
    document = parsed.get("document")
    if not isinstance(document, dict):
        out.issues.append(issue("AI_PARSE_EMPTY", "ai_parse_document output has no 'document' object", severity="error"))
        return out
    if not version.startswith("2."):
        out.issues.append(issue("AI_PARSE_SCHEMA_VERSION", f"output schema {version} is not 2.x; fields may differ"))
    builder = _Builder(doc_id, run_id, version, utcnow())
    elements = [e for e in document.get("elements") or [] if isinstance(e, dict)]
    placed = [(e, *_bbox(e)) for e in elements]
    page_ids = sorted({int(p["id"]) for p in document.get("pages") or [] if isinstance(p, dict) and p.get("id") is not None}
                      | {pid for _e, pid, _b in placed if pid is not None})

    titles: dict[int, str] = {}
    footnotes: dict[int, list[str]] = {}
    for e, pid, _b in placed:
        content = e.get("content") if isinstance(e.get("content"), str) else ""
        if pid is None or not content:
            continue
        if e.get("type") in ("title", "section_header") and pid not in titles:
            titles[pid] = content.strip().split("\n")[0]
        if e.get("type") in _FOOTNOTE_TYPES or any(is_footnote(line) for line in content.split("\n")):
            footnotes.setdefault(pid, []).extend(line.strip() for line in content.split("\n")
                                                 if line.strip() and (e.get("type") in _FOOTNOTE_TYPES or is_footnote(line)))

    for e, pid, box in placed:
        page = pid + 1 if pid is not None else None
        kind = e.get("type")
        ctx = {"page_title": titles.get(pid), "footnotes": list(dict.fromkeys(footnotes.get(pid, []))),
               "element_type": kind, "bbox_unit": "px" if box else None}
        if kind == "table":
            out.values += _table_element(builder, e, page, box, ctx)
        elif kind in _TEXT_TYPES:
            out.values += _text_element(builder, e, page, box, ctx)
        elif kind == "figure":
            out.values += _figure_element(builder, e, page, box, ctx, out.issues)

    for err in parsed.get("error_status") or []:
        pid = err.get("page_id") if isinstance(err, dict) else None
        message = (err or {}).get("error_message") if isinstance(err, dict) else str(err)
        page = int(pid) + 1 if pid is not None else None
        out.issues.append(issue("AI_PARSE_PAGE_ERROR", str(message), page=page))
        out.values.append(builder.placeholder(page, None, None, f"ai_parse_document failed on this page: {message}", {}))

    for pid in page_ids:
        on_page = [(e, b) for e, p, b in placed if p == pid]
        text = "\n".join(_element_text(e) for e, _b in on_page if e.get("type") != "page_number" and _element_text(e))
        out.pages.append(DocumentPage(
            doc_id=doc_id, page_number=pid + 1, title=titles.get(pid), text=text, notes=None, hidden=False,
            image_count=sum(1 for e, _b in on_page if e.get("type") == "figure"), page_width=None, page_height=None,
            shapes=[{"shape_id": str(e.get("id")), "kind": e.get("type"), "name": e.get("type"), "bbox": b}
                    for e, b in on_page], created_at=builder.created_at))
    out.document_properties = {"page_count": len(page_ids), "ai_parse_id": metadata.get("id")}
    out.values = finalize_values(out.values, out.issues)
    return out


def _element_text(element: dict[str, Any]) -> str:
    content = element.get("content")
    if not isinstance(content, str):
        return ""
    if element.get("type") == "table":
        grid, _s, _h = html_table_grid(content)
        return "\n".join("\t".join(row) for row in grid)
    return content.strip()


def _table_element(b: _Builder, e: dict[str, Any], page: int | None, box: list[float] | None,
                   ctx: dict[str, Any]) -> list[ExtractedValue]:
    grid, spans, hint = html_table_grid(e.get("content") or "")
    classified = [[text_cell_kind(t) for t in row] for row in grid]
    kinds = [[k for k, _p in row] for row in classified]
    layout = analyze_grid(grid, kinds, spans=spans, header_hint=hint)
    conf, conf_flags = _element_confidence(e)
    out = []
    for r, row in enumerate(classified):
        for c, (kind, parts) in enumerate(row):
            if kind != NUM or (r, c) in layout.header_cells:
                continue
            col_header = layout.col_headers[c] if c < len(layout.col_headers) else None
            raw_text, mark_flags = cell_number_text(grid[r][c])
            context = {**ctx, "object_kind": "ai_parse", "row_label": layout.row_labels[r], "col_header": col_header,
                       "header_row": layout.header_row(), "number_source": "text"}
            out.append(b.value(ExtractionMethod.AI_PARSE, confidence=conf,
                               flags=[b.parsed_flag, *conf_flags, *derived_change_flag(col_header, layout.row_labels[r]),
                                      *mark_flags],
                               locator={"page": page, "element_id": e.get("id"), "row": r, "col": c, "bbox": box},
                               context=context, raw_text=raw_text, raw_number=parts.number, number_format=None))
    return out


def _text_element(b: _Builder, e: dict[str, Any], page: int | None, box: list[float] | None,
                  ctx: dict[str, Any]) -> list[ExtractedValue]:
    content = e.get("content")
    if not isinstance(content, str) or e.get("type") in _FOOTNOTE_TYPES:
        return []
    conf, conf_flags = _element_confidence(e)
    lines = [_MARKDOWN.sub("", line).lstrip("# ").strip() for line in content.split("\n")]
    out = []
    for li, line in enumerate(lines):
        if not line or is_note_line(line):
            continue
        for hit in find_labeled_numbers(line):
            flags = [b.parsed_flag, *conf_flags]
            label = hit.label
            if hit.label_source == "inherited":
                flags.append(flag("ROW_LABEL_INHERITED", MEANING, "The label of the previous number on the line was used."))
            if label is None:
                neighbor = next((lines[j] for j in (li - 1, li + 1) if 0 <= j < len(lines) and lines[j]
                                  and not find_labeled_numbers(lines[j]) and re.search(r"[^\W\d_]{2,}", lines[j])), None)
                if neighbor is None:
                    continue
                label = neighbor
                flags.append(flag("LABEL_FROM_ADJACENT_LINE", MEANING, f"The label {neighbor!r} is on the neighboring line."))
            flags += derived_change_flag(hit.label_before, hit.label_after)
            context = {**ctx, "object_kind": "ai_parse", "text_line": line, "row_label": label,
                       "label_before": hit.label_before, "label_after": hit.label_after, "number_source": "text"}
            out.append(b.value(ExtractionMethod.AI_PARSE, confidence=conf, flags=flags,
                               locator={"page": page, "element_id": e.get("id"), "line": li, "span": [hit.start, hit.end],
                                        "bbox": box},
                               context=context, raw_text=hit.raw_text, raw_number=hit.parts.number, number_format=None))
    return out


def _figure_element(b: _Builder, e: dict[str, Any], page: int | None, box: list[float] | None, ctx: dict[str, Any],
                    issues: list[dict[str, Any]]) -> list[ExtractedValue]:
    meta, points = _chart_series(e.get("content"))
    description = e.get("description")
    conf, conf_flags = _element_confidence(e)
    out = []
    skipped = 0
    for si, name, pi, category, raw in points:
        if raw is None or raw == "":
            continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            number, text = float(raw), number_text(raw)
        else:
            parts = parse_number_parts(str(raw))
            if parts is None:
                skipped += 1
                continue
            number, text = parts.number, str(raw)
        context = {**ctx, "object_kind": "ai_parse", "object_title": meta.get("title"), "series_name": name,
                   "category": category, "axis_title": meta.get("axis_title"), "chart_type": meta.get("chart_type"),
                   "figure_description": description, "number_source": "text"}
        flags = [b.parsed_flag, *conf_flags,
                 flag("AI_CHART_READ", EXTRACTION, "Chart value read from the rendered image by ai_parse_document; uncertain."),
                 *derived_change_flag(name)]
        out.append(b.value(ExtractionMethod.AI_PARSE, confidence=min(conf, CHART_CONFIDENCE_CAP), flags=flags,
                           locator={"page": page, "element_id": e.get("id"), "series_index": si, "point_index": pi,
                                    "bbox": box},
                           context=context, raw_text=text, raw_number=number, number_format=None))
    if skipped:
        issues.append(issue("AI_CHART_VALUE_UNREADABLE", f"{skipped} chart value(s) on element {e.get('id')} are not numbers",
                            page=page, severity="info"))
    if not out:
        out.append(b.placeholder(page, e.get("id"), box, "ai_parse_document returned no chart data for this figure.",
                                 {**ctx, "figure_description": description}))
    return out


def run_ai_parse(store, settings, doc, *, version: str = "2.0", run_id: str | None = None) -> ExtractionOutput:
    """Run ``ai_parse_document`` on one registered document through ``store.query`` (Databricks only)."""
    target = getattr(store, "platform_access", store)
    if getattr(target, "dialect", None) != "databricks":
        raise NotImplementedError(
            "ai_parse_document runs only on Databricks (store.dialect == 'databricks'). In local mode, scans, images "
            "and image-only slides stay IMAGE_PLACEHOLDER values and are reviewed as UNCERTAIN_EXTRACTION.")
    out = ExtractionOutput(extractor_versions={"ai_parse_document": version})
    if getattr(settings, "ai_mode", "off") == "off":
        out.issues.append(issue("AI_PARSE_DISABLED", "settings.ai_mode is 'off'; ai_parse_document was not called",
                                severity="info"))
        return out
    try:
        sql = build_ai_parse_sql([doc.file_path], version, (settings.extra or {}).get("ai_parse_image_output_path"))
    except ValueError as exc:
        out.issues.append(issue("AI_PARSE_FAILED", str(exc), severity="error"))
        return out
    try:
        frame = target.query(sql)
    except Exception as exc:
        out.issues.append(issue("AI_PARSE_FAILED", f"{type(exc).__name__}: {exc}", severity="error"))
        return out
    if frame is None or len(frame) == 0:
        out.issues.append(issue("AI_PARSE_FAILED", f"no row returned for {doc.file_path}", severity="error"))
        return out
    row = frame.iloc[0]
    try:
        parsed = json.loads(row["parsed_json"]) if isinstance(row["parsed_json"], str) else row["parsed_json"]
    except (KeyError, ValueError) as exc:
        out.issues.append(issue("AI_PARSE_FAILED", f"unreadable parsed_json: {exc}", severity="error"))
        return out
    result = parse_ai_document_output(doc.doc_id, parsed, run_id=run_id)
    sha = row.get("content_sha256") if hasattr(row, "get") else None
    if sha and str(sha) != doc.doc_id:
        result.issues.append(issue("AI_PARSE_CONTENT_MISMATCH", f"file sha256 {sha} differs from doc_id {doc.doc_id}"))
    return result


def needs_ai_parse(output: ExtractionOutput) -> bool:
    """True when some page or image could not be read natively (an ``IMAGE_PLACEHOLDER`` value exists)."""
    return any(v.method == ExtractionMethod.IMAGE_PLACEHOLDER for v in output.values)


def merge_ai_parse(base: ExtractionOutput, ai: ExtractionOutput) -> ExtractionOutput:
    """Replace placeholders with ai_parse values, page by page; native values are never replaced."""
    placeholder_pages = {v.slide_or_page for v in base.values if v.method == ExtractionMethod.IMAGE_PLACEHOLDER}
    ai_values = [v for v in ai.values if v.slide_or_page in placeholder_pages]
    read_pages = {v.slide_or_page for v in ai_values if v.method == ExtractionMethod.AI_PARSE}
    is_deck = "pptx_extract" in base.extractor_versions
    merged: list[ExtractedValue] = []
    for v in base.values:
        if v.method == ExtractionMethod.IMAGE_PLACEHOLDER and v.slide_or_page in read_pages:
            continue
        merged.append(v)
    for v in ai_values:
        if v.method == ExtractionMethod.IMAGE_PLACEHOLDER and v.slide_or_page not in read_pages:
            continue
        if is_deck:
            v.flags = v.flags + [flag("AI_PAGE_MAPPING_UNVERIFIED", ASSOCIATION,
                                      "ai_parse_document page numbers are assumed to equal slide numbers (unverified).")]
        merged.append(v)
    known = {p.page_number for p in base.pages}
    issues = list(base.issues) + [{**i, "severity": "warning"} if i.get("severity") == "error" else i for i in ai.issues]
    return ExtractionOutput(
        values=merged, pages=list(base.pages) + [p for p in ai.pages if p.page_number not in known], issues=issues,
        extractor_versions={**base.extractor_versions, **ai.extractor_versions},
        document_properties={**ai.document_properties, **base.document_properties})
