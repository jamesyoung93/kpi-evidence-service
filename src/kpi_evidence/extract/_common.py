from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..contracts import ExtractedValue, ExtractionMethod
from ..ids import stable_id, utcnow
from . import NumberParts, is_non_value, parse_number_parts, split_reference_mark

EXTRACTION, MEANING, ASSOCIATION = "extraction", "meaning", "association"


def flag(code: str, dimension: str, detail: str) -> dict[str, str]:
    return {"code": code, "dimension": dimension, "detail": detail}


def merge_flags(*groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Concatenate flag lists, keeping the first flag per code (upstream assessments do the same)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or ():
            code = item.get("code")
            if code and code not in seen:
                seen.add(code)
                out.append(dict(item))
    return out


def hidden_slide_flag() -> dict[str, str]:
    return flag("HIDDEN_SLIDE", ASSOCIATION, "The value is on a hidden slide; it is not shown when the deck is presented.")


def footnote_mark_flag(mark: str | None = None) -> dict[str, str]:
    """Flag for a footnote reference split off a number."""
    what = f"The footnote reference {mark!r}" if mark else "A superscript footnote reference"
    return flag("FOOTNOTE_MARK", EXTRACTION, f"{what} after this number was not read as part of the number.")


def cell_number_text(text: str) -> tuple[str, list[dict[str, str]]]:
    """Cell text without a trailing footnote reference, plus its flag."""
    core, mark = split_reference_mark(text)
    return core, ([footnote_mark_flag(mark)] if mark else [])


def issue(code: str, detail: str, *, severity: str = "warning", page: int | None = None,
          part: str | None = None, **extra: Any) -> dict[str, Any]:
    out = {"code": code, "severity": severity, "page": page, "part": part, "detail": detail}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _clean_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if v is not None and v != "" and v != []}


def build_value(*, doc_id: str, method: ExtractionMethod, extractor: str, extractor_version: str,
                locator: dict[str, Any], context: dict[str, Any], raw_text: str | None,
                raw_number: float | None, number_format: str | None, flags: Sequence[dict[str, Any]],
                confidence: float, run_id: str | None, source_record_id: str | None = None,
                created_at: datetime | None = None) -> ExtractedValue:
    """One ``ExtractedValue`` with ``value_id = stable_id(doc_id, method, locator)`` (locator nulls dropped)."""
    loc = _clean_mapping(locator)
    page = loc.get("page")
    return ExtractedValue(
        value_id=stable_id(doc_id, method.value, loc), doc_id=doc_id, method=method, extractor=extractor,
        extractor_version=extractor_version, locator=loc, slide_or_page=int(page) if page is not None else None,
        context=_clean_mapping(context), raw_text=raw_text,
        raw_number=float(raw_number) if raw_number is not None else None, number_format=number_format,
        flags=merge_flags(flags), confidence=float(confidence), created_at=created_at or utcnow(),
        run_id=run_id, source_record_id=source_record_id)


def finalize_values(values: list[ExtractedValue], issues: list[dict[str, Any]]) -> list[ExtractedValue]:
    """Drop exact locator duplicates (they would collapse in the MERGE anyway) and record each as an issue."""
    seen: dict[str, ExtractedValue] = {}
    for v in values:
        if v.value_id in seen:
            issues.append(issue("DUPLICATE_LOCATOR", f"two values share locator {v.locator}; kept the first",
                                page=v.slide_or_page))
            continue
        seen[v.value_id] = v
    return list(seen.values())


def bbox_list(box: dict[str, Any] | None) -> list[float] | None:
    """Upstream ``{"x","y","width","height"}`` -> ``[x, y, w, h]``."""
    if not box:
        return None
    try:
        return [float(box["x"]), float(box["y"]), float(box["width"]), float(box["height"])]
    except (KeyError, TypeError, ValueError):
        return None


def number_text(value: float | int) -> str:
    """Stored number as plain text without float noise (``5810.0 -> "5810"``)."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value) if isinstance(value, float) else str(value)


def serial_to_iso(serial: float, date_system: str = "1900") -> str:
    """Excel/OOXML date serial -> ISO date (time dropped)."""
    if date_system == "1904":
        moment = datetime(1904, 1, 1) + timedelta(days=float(serial))
    elif serial < 61:
        moment = datetime(1899, 12, 31) + timedelta(days=float(serial))
    else:
        moment = datetime(1899, 12, 30) + timedelta(days=float(serial))
    return moment.date().isoformat()


def iso_date_text(value: Any) -> str:
    """Worksheet dates arrive as ISO datetimes; keep the date part when the time is midnight."""
    text = str(value)
    return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}T00:00:00", text) else text


_CORE_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}
_CORE_FIELDS = (("title", "dc:title"), ("subject", "dc:subject"), ("keywords", "cp:keywords"),
                ("description", "dc:description"), ("category", "cp:category"),
                ("created", "dcterms:created"), ("modified", "dcterms:modified"))


def core_properties(blob: bytes) -> dict[str, str]:
    """``docProps/core.xml`` title/subject/keywords/created/modified of an OOXML package ({} if absent)."""
    from defusedxml import ElementTree as ET

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        if "docProps/core.xml" not in archive.namelist():
            return {}
        root = ET.fromstring(archive.read("docProps/core.xml"), forbid_dtd=True)
    out: dict[str, str] = {}
    for key, path in _CORE_FIELDS:
        node = root.find(path, _CORE_NS)
        if node is not None and node.text and node.text.strip():
            out[key] = node.text.strip()
    return out


EMPTY, NUM, LABEL, NA = "empty", "num", "label", "na"


def is_year_like(parts: NumberParts | None, text: str) -> bool:
    """A bare 4-digit integer 1990-2100 printed without separators reads as a year, not a count."""
    return bool(parts and parts.kind == "number" and not parts.suffix and parts.decimals == 0
                and not parts.explicit_sign and not parts.accounting_negative
                and re.fullmatch(r"\s*\d{4}\s*", text or "") and 1990 <= parts.number <= 2100)


def text_cell_kind(text: str | None) -> tuple[str, NumberParts | None]:
    """Classify a printed cell: empty, na (a printed placeholder), num (one parseable number) or label."""
    if text is None or not str(text).strip():
        return EMPTY, None
    if is_non_value(text):
        return NA, None
    parts = parse_number_parts(text)
    if parts is None or is_year_like(parts, text):
        return LABEL, None
    return NUM, parts


_WINDOW_HEADER = re.compile(r"\s*\d{1,2}\s?[Mm]\s*")


def _is_window_header(texts: list[list[str]], kinds: list[list[str]], r: int, c: int) -> bool:
    if kinds[r][c] != NUM or not _WINDOW_HEADER.fullmatch(texts[r][c] or ""):
        return False
    below = [parse_number_parts(texts[i][c]) for i in range(r + 1, len(kinds))
             if c < len(kinds[i]) and kinds[i][c] == NUM]
    return bool(below) and not any(p is None or p.suffix for p in below)


def _is_header_row(texts: list[list[str]], kinds: list[list[str]], r: int) -> bool:
    row = kinds[r]
    if LABEL not in row or NA in row:
        return False
    windows = [c for c, k in enumerate(row) if k == NUM]
    if not all(_is_window_header(texts, kinds, r, c) for c in windows):
        return False
    above = texts[r - 1] if r else []
    return not windows or r == 0 or any(not (above[c] if c < len(above) else "").strip() for c in windows)


@dataclass
class GridLayout:
    header_rows: int
    col_headers: list[str | None]
    row_labels: list[str | None]
    header_cells: frozenset[tuple[int, int]] = frozenset()

    def header_row(self) -> list[str] | None:
        if not self.header_rows:
            return None
        return [h or "" for h in self.col_headers]

    def labels_left_of(self, texts: list[list[str]], kinds: list[list[str]], r: int, c: int) -> list[str]:
        return [texts[r][j] for j in range(min(c, len(texts[r]))) if kinds[r][j] == LABEL]


def analyze_grid(texts: list[list[str]], kinds: list[list[str]], *, spans: Iterable[tuple[int, int, int]] = (),
                 header_hint: int | None = None, max_header_rows: int = 3) -> GridLayout:
    """Detect header rows, compose column headers and pick row labels."""
    nrows = len(texts)
    ncols = max((len(r) for r in texts), default=0)
    if header_hint is not None:
        h = max(0, min(header_hint, nrows))
    else:
        h = 0
        while h < min(max_header_rows, nrows) and _is_header_row(texts, kinds, h):
            h += 1
        if h == nrows:
            h = min(1, nrows)
    header_cells = frozenset((r, c) for r in range(h) for c in range(len(kinds[r]))
                             if _is_window_header(texts, kinds, r, c))
    filled = [[(texts[r][c] if c < len(texts[r]) else "") or "" for c in range(ncols)] for r in range(h)]
    for r, c, n in spans:
        if r < h and 0 <= c < ncols and n > 1:
            for k in range(1, n):
                if c + k < ncols and not filled[r][c + k].strip():
                    filled[r][c + k] = filled[r][c]
    col_headers: list[str | None] = []
    for c in range(ncols):
        parts: list[str] = []
        for r in range(h):
            text = " ".join(filled[r][c].split())
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        col_headers.append(" / ".join(parts) or None)
    row_labels: list[str | None] = []
    for r in range(nrows):
        label = next((" ".join(texts[r][c].split()) for c in range(len(texts[r])) if kinds[r][c] == LABEL), None)
        row_labels.append(label)
    return GridLayout(header_rows=h, col_headers=col_headers, row_labels=row_labels, header_cells=header_cells)


_CHANGE = re.compile(
    r"(?:\bvs\.?(?=\s|$)|\bversus\b|\byoy\b|\by/y\b|\bmom\b|\bm/m\b|\bqoq\b|\bq/q\b|\bchg\b|\bchange\b|"
    r"\bgrowth\b|\bdelta\b|Δ|\bvariance\b)", re.I)


def derived_change_flag(*texts: str | None) -> list[dict[str, str]]:
    """``DERIVED_CHANGE`` when a header/series/label says the number is a change ("vs PY", "% chg", "YoY")."""
    for text in texts:
        if text and _CHANGE.search(text):
            return [flag("DERIVED_CHANGE", MEANING, f"Label marks a change or comparison value: {text!r}")]
    return []


_FOOTNOTE = re.compile(
    r"^\s*(?:[*†‡]|source[s]?\b|note[s]?\b|footnote)|\bdata\s+(?:through|thru|as\s+of)\b|"
    r"\bsource[s]?\s*:", re.I)


def is_footnote(line: str) -> bool:
    """Source/caveat lines ("Source: ...", "Data through May 2026", "* excludes ...")."""
    return bool(_FOOTNOTE.search(line or ""))


def is_note_line(line: str) -> bool:
    """True when the whole line is a note, not a KPI line with a note in parentheses."""
    return is_footnote(re.sub(r"\([^()]*\)", " ", line or ""))


_MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
           r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_PERIOD_BEFORE = re.compile(
    rf"(?:\b{_MONTHS}\.?(?:\s+\d{{1,2}}(?:st|nd|rd|th)?,?)?|\bq[1-4]|\bh[12]|\bfy|\bcy|\bweek|\bwk)"
    r"[\s'’\-./]*$", re.I)
_TO_DATE_BEFORE = re.compile(r"\b(?:ytd|mat)(?P<sep>[\s'’\-./]*)$", re.I)
_MONTH_AFTER = re.compile(rf"^[\s,'’\-./]*{_MONTHS}\b", re.I)
_TOKEN = re.compile(
    r"(?<![\w.,/\-])(?<!\d:)"
    r"(?P<tok>"
    r"(?:[+\-](?=(?:US\$|[$€£¥])?\(?\d))?"
    r"(?:US\$|[$€£¥])?\(?"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:\s?(?:(?i:thousand|million|billion|bn|mm|mn)|[KMBkmb])(?![A-Za-z]))?"
    r"(?:\s?(?:%|(?i:pp|pts?|bps)|x)(?![A-Za-z]))?"
    r"\)?[¹²³⁰⁴-⁹]*)"
    r"(?![\w%]|[.,/:]\d|-\d)")
_REFERENCE_TOKEN = re.compile(r"[(\[]\d{1,2}[)\]]")
_SEPARATOR = re.compile(r"\s*[|•·;]\s*|\t|\s{3,}")
_REFERENCE_BEFORE = re.compile(
    r"\b(?:page|slide|p\.|pg\.?|no\.|figure|fig\.|table|chart|exhibit|appendix|section|step|phase|version|"
    r"wave|round|tier|top|bottom)\s*#?\s*$|#\s*$|\b(?:page|slide|p\.|pg\.?)\s*\d+\s*(?:of|/)\s*$", re.I)
_REFERENCE_AFTER = re.compile(r"^\s*of\s+\d+\b", re.I)
_LETTERS = re.compile(r"[^\W\d_]{2,}")
_LABEL_STRIP = " \t:=,;.-–—"


@dataclass(frozen=True)
class TextNumber:
    """One number found in a text line, with the text on either side of it."""

    raw_text: str
    parts: NumberParts
    start: int
    end: int
    label_before: str | None
    label_after: str | None
    label: str | None
    label_source: str | None


def _normalize_line(line: str) -> str:
    line = re.sub(r"[    ]", " ", line)
    return re.sub(r"(?<![\w.])[−–](?=[$€£¥(]?\d)", "-", line)


def _clean_label(text: str) -> str | None:
    text = " ".join(text.split()).strip(_LABEL_STRIP)
    if text.count("(") != text.count(")"):
        text = text.strip("()").strip(_LABEL_STRIP)
    elif text.startswith("(") and text.endswith(")") and text.count("(") == 1:
        text = text[1:-1].strip(_LABEL_STRIP)
    return text if _LETTERS.search(text) else None


def _is_date_or_reference(line: str, start: int, end: int, raw: str, parts: NumberParts) -> bool:
    bare = (parts.kind == "number" and not parts.suffix and parts.decimals == 0 and not parts.explicit_sign
            and not parts.accounting_negative and "," not in raw)
    if not bare:
        return False
    if is_year_like(parts, raw):
        return True
    before, after = line[:start], line[end:]
    if _REFERENCE_BEFORE.search(before) or _REFERENCE_AFTER.match(after):
        return True
    to_date = _TO_DATE_BEFORE.search(before)
    if to_date and (len(raw) <= 2 or re.search(r"['’]", to_date["sep"])):
        return True
    return bool(_PERIOD_BEFORE.search(before) or _MONTH_AFTER.match(after))


def _is_reference_mark(norm: str, previous_end: int | None, start: int, end: int) -> bool:
    if previous_end is None:
        return False
    open_at = start if norm[start] in "([" else start - 1
    close_at = end - 1 if norm[end - 1] in ")]" else end
    if open_at < previous_end or close_at >= len(norm):
        return False
    return not norm[previous_end:open_at].strip() and bool(_REFERENCE_TOKEN.fullmatch(norm[open_at:close_at + 1]))


def find_numbers(line: str) -> list[tuple[int, int, str, NumberParts]]:
    """Every parseable number token in a line, excluding dates, years and identifiers such as IL-17 or Q2."""
    norm = _normalize_line(line)
    found: list[tuple[int, int, str, NumberParts]] = []
    previous_end: int | None = None
    for m in _TOKEN.finditer(norm):
        start, end = m.span("tok")
        raw = m["tok"]
        if raw.endswith(")") and "(" not in raw:
            raw, end = raw[:-1], end - 1
        if raw.startswith("(") and ")" not in raw:
            raw, start = raw[1:], start + 1
        parts = parse_number_parts(raw)
        if parts is None or _is_date_or_reference(norm, start, end, raw.rstrip("¹²³⁰⁴⁵⁶⁷⁸⁹"), parts):
            continue
        if _is_reference_mark(norm, previous_end, start, end):
            continue
        found.append((start, end, line[start:end], parts))
        previous_end = end
    return found


def find_labeled_numbers(line: str) -> list[TextNumber]:
    """Numbers in a line with their adjacent text ("Brand awareness: 62%", "TRx 5,910", "+8% TRx")."""
    numbers = find_numbers(line)
    seps = [m.span() for m in _SEPARATOR.finditer(line)]
    out: list[TextNumber] = []
    previous: tuple[int, str | None] = (-1, None)
    for i, (start, end, raw, parts) in enumerate(numbers):
        segment = sum(1 for _s, e in seps if e <= start)
        left = max([numbers[i - 1][1] if i else 0] + [e for _s, e in seps if e <= start])
        right = min([numbers[i + 1][0] if i + 1 < len(numbers) else len(line)]
                    + [s for s, _e in seps if s >= end])
        before = _clean_label(line[left:start])
        after = _clean_label(line[end:right])
        inherited = previous[1] if previous[0] == segment else None
        if before:
            label, source = before, "before"
        elif after:
            label, source = after, "after"
        elif inherited:
            label, source = inherited, "inherited"
        else:
            label, source = None, None
        previous = (segment, before or inherited)
        out.append(TextNumber(raw, parts, start, end, before, after, label, source))
    return out
