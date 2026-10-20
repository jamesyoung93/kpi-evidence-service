from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from .contracts import Document, DocumentPage, ExtractedValue, ExtractionMethod, Observation
from .ids import scope_key as make_scope_key
from .ids import stable_id, utcnow
from .intake import document_grain
from .periods import (
    GRAIN_MONTHS, PeriodParse, add_months, parse_period, parse_year_label, period_bounds, snap_month_end, strip_periods,
    window_bounds, window_grain,
)
from .registry import DIMENSIONS, Registry, normalize_label

INTERPRETATION_VERSION = "v1"
FINGERPRINT_FIELDS = ("report_type", "page_title", "object_kind", "object_title", "label", "col_header")

_STORED_METHODS = {ExtractionMethod.NATIVE_CHART, ExtractionMethod.EMBEDDED_WORKSHEET, ExtractionMethod.WORKBOOK,
                   ExtractionMethod.REVIEWER_CORRECTION}
_KIND_BY_METHOD = {
    ExtractionMethod.NATIVE_CHART: "chart", ExtractionMethod.NATIVE_TABLE: "table",
    ExtractionMethod.EMBEDDED_WORKSHEET: "worksheet", ExtractionMethod.WORKBOOK: "worksheet",
    ExtractionMethod.SLIDE_TEXT: "slide_text", ExtractionMethod.PDF_TABLE: "pdf_table",
    ExtractionMethod.PDF_TEXT: "pdf_text", ExtractionMethod.AI_PARSE: "ai_parse",
    ExtractionMethod.IMAGE_PLACEHOLDER: "image", ExtractionMethod.REVIEWER_CORRECTION: "correction",
}
_TEXT_KINDS = {"slide_text", "pdf_text"}


def _flag(code: str, dimension: str, detail: str = "") -> dict[str, str]:
    return {"code": code, "dimension": dimension, "detail": detail}


_CURRENCY = {"$": "usd", "us$": "usd", "usd": "usd", "€": "eur", "eur": "eur", "£": "gbp", "gbp": "gbp"}
_SCALE_WORDS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "mn": 1e6, "million": 1e6,
                "b": 1e9, "bn": 1e9, "billion": 1e9}
_NUM_TEXT = re.compile(r"""
    ^(?P<approx>[~≈]|approx\.?\s+|c\.\s*)?
    (?P<sign1>[+\-])?\s*
    (?P<cur1>US\$|\$|€|£|USD|EUR|GBP)?\s*
    (?P<open>\()?\s*
    (?P<sign2>[+\-])?\s*
    (?P<cur2>US\$|\$|€|£|USD|EUR|GBP)?\s*
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)
    \s*(?P<suffix>thousand|million|billion|bn|mn|mm|k|m|b)?\.?
    \s*(?P<pct>%|pts?\.?|pp|bps|x)?
    \s*(?P<cur3>USD|EUR|GBP)?
    \s*(?P<close>\))?
    \s*(?P<pct2>%)?
    \s*(?P<marks>[*†‡¹²³⁴⁵⁶⁷⁸⁹⁰]+)?$
""", re.X | re.I)
_NON_VALUES = {"", "-", "--", "—", "–", "n/a", "na", "nm", "n.m.", "nd", "tbd", "*"}
_DIVISOR = {"percent": 100.0, "points": 100.0, "bps": 10000.0}


@dataclass(frozen=True)
class DisplayNumber:
    """A printed number taken apart: the bare signed number plus what the print says about it."""

    number: float
    scale: float
    decimals: int
    kind: str | None
    currency: str | None
    approximate: bool = False

    @property
    def value(self) -> float:
        return _clean_float(self.number * self.scale / _DIVISOR.get(self.kind or "", 1.0))

    @property
    def unit(self) -> str:
        if self.kind in ("percent", "points", "bps"):
            return "fraction"
        if self.kind == "multiple" or self.currency in ("eur", "gbp"):
            return "unknown"
        return "usd" if self.currency == "usd" else "count"

    @property
    def half_unit(self) -> float:
        return _clean_float(0.5 * 10.0 ** (-self.decimals) * self.scale / _DIVISOR.get(self.kind or "", 1.0))


def _clean_float(x: float) -> float:
    return float(f"{x:.15g}")


def parse_display_number(text: str | None) -> DisplayNumber | None:
    """Parse a printed number ("5.9K", "84.4%", "(1,234)", "$12.3M", "+2.1 pts"); None if it is not one."""
    if text is None:
        return None
    s = str(text).strip().replace("−", "-").replace(" ", " ").replace(" ", " ")
    s = re.sub(r"^[–—](?=\s*[\d$€£(.])", "-", s)
    if s.lower() in _NON_VALUES:
        return None
    m = _NUM_TEXT.match(s)
    if not m or bool(m["open"]) != bool(m["close"]):
        return None
    if (m["sign1"] and m["sign2"]) or (m["open"] and (m["sign1"] or m["sign2"])):
        return None
    if m["pct"] and m["pct2"]:
        return None
    number = float(m["num"].replace(",", ""))
    if m["open"] or "-" in (m["sign1"] or "", m["sign2"] or ""):
        number = -number
    decimals = len(m["num"].split(".", 1)[1]) if "." in m["num"] else 0
    scale = _SCALE_WORDS[m["suffix"].lower()] if m["suffix"] else 1.0
    pct = (m["pct"] or m["pct2"] or "").lower().rstrip(".")
    kind = {"%": "percent", "pt": "points", "pts": "points", "pp": "points", "bps": "bps", "x": "multiple"}.get(pct)
    cur_text = (m["cur1"] or m["cur2"] or m["cur3"] or "").lower()
    return DisplayNumber(number=number, scale=scale, decimals=decimals, kind=kind,
                         currency=_CURRENCY.get(cur_text), approximate=bool(m["approx"]))


@dataclass(frozen=True)
class FormatInfo:
    """What an Excel/OOXML number format code says about a stored number."""

    general: bool
    percent: bool
    currency: str | None
    display_scale: float
    decimals: int | None
    is_date: bool
    literal_percent: bool = False


def classify_format(code: str | None) -> FormatInfo:
    """Classify the first section of a format code (quoted literals and escapes removed)."""
    if code is None or not str(code).strip() or str(code).strip().lower() == "general":
        return FormatInfo(True, False, None, 1.0, None, False)
    raw = str(code)
    is_date = False
    try:
        from openpyxl.styles.numbers import is_date_format

        is_date = bool(is_date_format(raw))
    except Exception:
        is_date = False
    section = _first_section(raw)
    currency = None
    bracket = re.search(r"\[\$([^\]-]*)", section)
    if bracket and bracket.group(1):
        currency = _CURRENCY.get(bracket.group(1).strip().lower(), "unknown")
    stripped = re.sub(r"\[[^\]]*\]", "", re.sub(r'"[^"]*"|\\.', "", section))
    if currency is None:
        currency = next((c for sym, c in (("$", "usd"), ("€", "eur"), ("£", "gbp")) if sym in stripped), None)
    if currency is None and (re.search(r'"[^"]*(?:\$|USD)[^"]*"', section) or "\\$" in section):
        currency = "usd"
    scale = 1.0
    last = max((stripped.rfind(ch) for ch in "0#?"), default=-1)
    if last >= 0:
        commas = len(stripped[last + 1:]) - len(stripped[last + 1:].lstrip(","))
        scale = 1000.0 ** commas
    decimals = None
    dec = re.search(r"\.([0#?]+)", stripped)
    if re.search(r"[0#?]", stripped):
        decimals = len(dec.group(1)) if dec else 0
    literal_percent = "%" not in stripped and bool(re.search(r'"[^"]*%[^"]*"|\\%', section))
    return FormatInfo(False, "%" in stripped, currency, scale, decimals, is_date and "%" not in stripped,
                      literal_percent)


def _first_section(code: str) -> str:
    out, quote = [], False
    for ch in code:
        if ch == '"':
            quote = not quote
        elif ch == ";" and not quote:
            break
        out.append(ch)
    return "".join(out)


_PERCENT_CUE = re.compile(r"(?:^|[\s(\[/])%(?=$|[\s)\]/:,])|\bpercent(?:age)?\b|\bpct\b|\(\s*pp\s*\)|\bpts\b", re.I)
_CURRENCY_CUE = re.compile(r"\(\s*(?:US\$|\$|usd)\s*\)|\busd\b|(?<![\w$])\$(?!\d)", re.I)
_SCALE_CUE = re.compile(
    r"\(\s*(?:in\s+)?(?:US\$|\$|usd|€|£)?\s*(?P<s>'?000s?|k|thousands?|m|mm|mn|millions?|b|bn|billions?)\s*\)"
    r"|\bin\s+(?P<s2>thousands|millions|billions)\b"
    r"|(?:US\$|\$|usd)\s?(?P<s3>k|mm|mn|m|bn|b)\b", re.I)


def _scale_cue(texts: Iterable[str | None]) -> tuple[float, str] | None:
    for t in texts:
        if not t:
            continue
        m = _SCALE_CUE.search(t)
        if m:
            word = (m["s"] or m["s2"] or m["s3"]).lower().lstrip("'")
            factor = 1e3 if word.startswith("000") else _SCALE_WORDS.get(word.rstrip("s"), _SCALE_WORDS.get(word))
            if factor:
                return factor, m.group(0)
    return None


def _cue(rx: re.Pattern[str], texts: Iterable[str | None]) -> str | None:
    for t in texts:
        if t:
            m = rx.search(t)
            if m:
                return m.group(0).strip()
    return None


@dataclass
class ValueReading:
    value: float | None
    unit: str | None
    half_unit: float
    flags: list[dict[str, str]] = field(default_factory=list)


_CAPTION_CUE = re.compile(
    _SCALE_CUE.pattern + r"|\(\s*(?:%|percent|pct)\s*\)|\bin\s+(?:%|percent)(?!\w)|\(\s*(?:US\$|\$|usd)\s*\)", re.I)
_MEASURE_SPLIT = re.compile(r"\s+(?:and|plus)\s+|\s*[&+;|]\s*|\s+/\s+", re.I)
_UNIT_CUES = (_SCALE_CUE, _PERCENT_CUE, _CURRENCY_CUE)
_STOP_WORDS = frozenset({"a", "an", "the", "of", "in", "for", "by", "per", "on", "at", "to"})


def _strip_unit_cues(text: str | None) -> str:
    out = str(text or "")
    for rx in _UNIT_CUES:
        out = rx.sub(" ", out)
    return out


def _measure_words(text: str | None) -> set[str]:
    t = strip_periods(_strip_unit_cues(text), include_bare=True)
    return set(normalize_label(_BY_DIMENSION.sub(" ", t)).split()) - _STOP_WORDS


def _title_cue_segment(title: str, own: str | None, single: bool) -> str:
    parts = [p for p in _MEASURE_SPLIT.split(title) if _measure_words(p)]
    if single or len(parts) <= 1:
        return title
    own_words = _measure_words(own)
    fitting = [(len(_measure_words(p)), p) for p in parts if _measure_words(p) <= own_words]
    return max(fitting, key=lambda item: item[0])[1] if fitting else ""


def _container_cues(kind: str, ctx: Mapping[str, Any]) -> list[str]:
    title = str(ctx.get("object_title") or "")
    if not title:
        return []
    if kind == "chart":
        single = int(ctx.get("series_count") or 1) <= 1
        return [_title_cue_segment(title, ctx.get("series_name"), single)]
    if kind in ("table", "worksheet", "pdf_table"):
        segment = _title_cue_segment(title, ctx.get("row_label"), False)
        return [" ".join(m.group(0) for m in _CAPTION_CUE.finditer(segment))]
    return []


def read_value(v: ExtractedValue, ctx: Mapping[str, Any]) -> ValueReading:
    """Scale and unit for one extracted value (see module docstring)."""
    if v.method == ExtractionMethod.IMAGE_PLACEHOLDER:
        return ValueReading(None, None, 0.0)
    kind = _object_kind(v, ctx)
    cue_texts = [ctx.get("col_header"), ctx.get("axis_title"), ctx.get("series_name"), ctx.get("row_label"),
                 *_container_cues(kind, ctx)]
    source = ctx.get("number_source") or ("stored" if v.method in _STORED_METHODS else "text")
    if source == "stored":
        return _read_stored(v, cue_texts)
    return _read_text(v, cue_texts)


def _read_stored(v: ExtractedValue, cue_texts: list[str | None]) -> ValueReading:
    if v.raw_number is None:
        return ValueReading(None, None, 0.0, [_flag("VALUE_NOT_NUMERIC", "extraction",
                                                   f"stored value is not a number: {v.raw_text!r}")])
    fmt = classify_format(v.number_format)
    value = float(v.raw_number)
    flags: list[dict[str, str]] = []
    if fmt.is_date:
        return ValueReading(value, "unknown", 0.0, [_flag("VALUE_IS_DATE", "extraction",
                                                          f"number format {v.number_format!r} is a date format")])
    if fmt.percent:
        return ValueReading(value, "fraction", 0.0)
    if fmt.literal_percent:
        return ValueReading(_clean_float(value / 100.0), "fraction", 0.0, [_flag(
            "UNIT_FROM_FORMAT_LITERAL", "interpretation",
            f"number format {v.number_format!r} prints a percent sign; "
            f"stored {v.raw_number} read as percentage points")])
    unit = {"usd": "usd", None: "count"}.get(fmt.currency, "unknown")
    unscaled = fmt.display_scale == 1.0
    pct = _cue(_PERCENT_CUE, cue_texts) if fmt.currency is None and unscaled else None
    if pct:
        unit = "fraction"
        if abs(value) > 1.5:
            value = _clean_float(value / 100.0)
            flags.append(_flag("PERCENT_POINTS_ASSUMED", "interpretation",
                               f"stored {v.raw_number} under '{pct}' read as percentage points"))
        flags.append(_flag("UNIT_FROM_TEXT_CUE", "interpretation", f"percent cue '{pct}' in labels"))
    else:
        if fmt.currency is None and _cue(_CURRENCY_CUE, cue_texts):
            unit = "usd"
            flags.append(_flag("UNIT_FROM_TEXT_CUE", "interpretation", "currency cue in labels"))
        scale = _scale_cue(cue_texts) if unscaled else None
        if scale:
            value = _clean_float(value * scale[0])
            flags.append(_flag("SCALE_FROM_HEADER", "interpretation", f"x{scale[0]:g} from '{scale[1]}'"))
    return ValueReading(value, unit, 0.0, flags)


def _read_text(v: ExtractedValue, cue_texts: list[str | None]) -> ValueReading:
    dn = parse_display_number(v.raw_text)
    if dn is None:
        if v.raw_number is None:
            return ValueReading(None, None, 0.0, [_flag("VALUE_NOT_NUMERIC", "extraction",
                                                       f"printed value is not a number: {v.raw_text!r}")])
        return ValueReading(float(v.raw_number), "count", 0.0,
                            [_flag("NUMBER_TEXT_UNPARSED", "extraction",
                                   f"could not read {v.raw_text!r}; used the extractor's number")])
    flags: list[dict[str, str]] = []
    if v.raw_number is not None and abs(float(v.raw_number) - dn.number) > 1e-9 * max(1.0, abs(dn.number)):
        flags.append(_flag("RAW_NUMBER_MISMATCH", "extraction",
                           f"extractor number {v.raw_number} differs from printed {v.raw_text!r}; printed text used"))
    if dn.approximate:
        flags.append(_flag("APPROXIMATE_VALUE", "interpretation", f"printed as approximate: {v.raw_text!r}"))
    if dn.kind is None:
        pct = _cue(_PERCENT_CUE, cue_texts)
        if pct:
            dn = replace(dn, kind="points" if re.search(r"pp|pts", pct, re.I) else "percent")
            flags.append(_flag("UNIT_FROM_TEXT_CUE", "interpretation", f"percent cue '{pct}' in labels"))
        else:
            if dn.currency is None and _cue(_CURRENCY_CUE, cue_texts):
                dn = replace(dn, currency="usd")
                flags.append(_flag("UNIT_FROM_TEXT_CUE", "interpretation", "currency cue in labels"))
            scale = _scale_cue(cue_texts) if dn.scale == 1.0 else None
            if scale:
                dn = replace(dn, scale=scale[0])
                flags.append(_flag("SCALE_FROM_HEADER", "interpretation", f"x{scale[0]:g} from '{scale[1]}'"))
    return ValueReading(dn.value, dn.unit, dn.half_unit, flags)


_CHANGE_CUE = re.compile(
    r"\bvs\.?(?=\s|$)|\bversus\b|%\s*chg|\bchg\b|\bchange\b|\bgrowth\b|\by/?o/?y\b|\bm/?o/?m\b|\bq/?o/?q\b"
    r"|\bdelta\b|Δ|\bvar\b|\bvar\.|\bvariance\b|\bdiff(?:erence)?\b|\+/-|\bpp\b|\bpts\b|\bbps\b", re.I)
_TITLE_CHANGE_CUE = re.compile(r"%\s*chg|\bchange\b|\bgrowth\b|\by/?o/?y\b|\bm/?o/?m\b|\bq/?o/?q\b|\bdelta\b|Δ", re.I)
_GENERIC = re.compile(
    r"^(?:series\s*\d*|total|totals|grand total|overall|actuals?|act|values?|data|amount|count|number|n|#"
    r"|py|ly|cy|ty|ya|cur|prior(?:\s+(?:year|period))?|current(?:\s+(?:year|period))?|this\s+year|last\s+year"
    r"|previous\s+year|year\s+ago|[\d\s.,%'’-]*)$", re.I)
_NON_ACTUAL = re.compile(r"\b(?:plan|target|goal|forecast|fcst|budget|quota|objective)\b", re.I)
_YEAR_ONLY = re.compile(r"^\s*(?:FY|CY)?\s*((?:19|20)\d{2})\s*$", re.I)
_EDGE = re.compile(r"^[\s:;,\-–—=|•·()\[\]/]+|[\s:;,\-–—=|•·(\[/]+$")
_TRAILING_WORDS = re.compile(
    r"(?:\s+(?:is|was|were|are|at|of|reached|totaled|totalled|about|for|in|the|a|an|across|within|by|on|from|to"
    r"|with|vs\.?))+$", re.I)
_LEADING_WORDS = re.compile(r"^(?:(?:in|of|for|the|a|an|at|on|across|within)\s+)+", re.I)
_BY_DIMENSION = re.compile(r"\b(?:by|per|across)\s+(?:region|district|territory|specialty|payer(?:\s+channel)?|channel"
                           r"|segment|month|quarter|week|year)s?\b", re.I)
_CAUSAL_ACTIVE = re.compile(r"^(?P<subject>.+?)\s+(?:drove|drive|drives|generated|delivered|contributed|added"
                            r"|led\s+to|resulted\s+in|caused)\s*$", re.I)
_CAUSAL_PASSIVE = re.compile(r"^(?P<metric>.+?)\s+(?:driven\s+by|generated\s+by|due\s+to|attributable\s+to"
                             r"|from)\s+(?P<subject>.+)$", re.I)
_NUMBER_TOKEN = re.compile(r"(?<![\w-])[+\-−]?\s*[$€£]?\s*\(?\d[\d,]*(?:\.\d+)?\)?(?:\s*(?:[KMBkmb]|mm|bn))?\s*%?(?!-?[A-Za-z])")


def _is_generic(text: str) -> bool:
    return bool(_GENERIC.match(text.strip()))


_QUALIFIER_WORDS = re.compile(
    r"\b(?:vs|versus|v|py|ly|cy|ya|prior|previous|last|this|year|ago|period|plan|target|goal|forecast|fcst|budget"
    r"|quota|objective|chg|change|growth|yoy|mom|qoq|delta|var|variance|diff|difference|pp|pts|bps|index)\b"
    r"|[%Δ+/.\-]", re.I)


def _has_metric_words(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", _QUALIFIER_WORDS.sub(" ", text)))


def _tidy(text: str | None) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = _EDGE.sub("", t)
    t = re.sub(r"\(\s*\)", "", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass(frozen=True)
class _ScopeIndex:
    entries: tuple[tuple[str, str, str], ...]

    @classmethod
    def build(cls, dimension_values: Mapping[str, Iterable[str]] | None) -> "_ScopeIndex":
        entries = []
        for dim, values in (dimension_values or {}).items():
            for value in values or ():
                norm = normalize_label(str(value))
                if norm:
                    entries.append((norm, str(dim), str(value)))
        entries.sort(key=lambda e: (-len(e[0]), e[1], e[0]))
        return cls(tuple(entries))


_DIM_WORDS = {"region": ("region", "regions", "regional", "area"), "district": ("district",),
              "territory": ("territory", "terr"), "specialty": ("specialty", "specialists", "specialty group"),
              "payer_channel": ("channel", "payer", "payers", "segment")}


def find_scope(text: str | None, index: _ScopeIndex, *, require_cue: bool = False) -> tuple[dict[str, list[str]], str]:
    """Dimension values named in ``text`` -> ({dimension: [values]}, text with those words removed)."""
    if not text or not index.entries:
        return {}, str(text or "")
    norm = " " + normalize_label(text) + " "
    found: dict[str, list[str]] = {}
    remove: list[str] = []
    for value_norm, dim, canonical in index.entries:
        needle = f" {value_norm} "
        if needle not in norm:
            continue
        words = _DIM_WORDS.get(dim, ())
        cue_phrases = [f" {value_norm} {w} " for w in words] + [f" {w} {value_norm} " for w in words]
        cue = next((p for p in cue_phrases if p in norm), None)
        has_own_word = any(f" {w} " in needle for w in words)
        if require_cue and cue is None and not has_own_word:
            continue
        found.setdefault(dim, [])
        if canonical not in found[dim]:
            found[dim].append(canonical)
        norm = norm.replace(cue or needle, " ")
        remove.append((cue or needle).strip())
    cleaned = str(text)
    for phrase in remove:
        pattern = r"[\s\-_]+".join(re.escape(w) for w in phrase.split())
        cleaned = re.sub(rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])", " ", cleaned, flags=re.I)
    return found, cleaned


def _clean_label(text: str | None, index: _ScopeIndex, fs: int) -> str:
    if not text:
        return ""
    _scope, without_scope = find_scope(text, index)
    t = _tidy(strip_periods(without_scope, fiscal_year_start_month=fs, include_bare=True))
    t = _BY_DIMENSION.sub(" ", t)
    t = _LEADING_WORDS.sub("", _TRAILING_WORDS.sub("", _tidy(t)).strip())
    t = _tidy(t)
    return "" if not t or _is_generic(t) else t


_SEGMENT_BREAK = re.compile(r"[|•·;\t]")


def _number_at(line: str, raw: str | None, span: Any = None) -> int:
    if not raw:
        return -1
    if (isinstance(span, (list, tuple)) and len(span) == 2 and all(isinstance(x, int) for x in span)
            and line[span[0]:span[1]] == raw):
        return span[0]
    first = idx = line.find(raw)
    while idx >= 0:
        end = idx + len(raw)
        clean_start = idx == 0 or not (line[idx - 1].isdigit() or line[idx - 1] in ".,")
        if clean_start and (end >= len(line) or not line[end].isdigit()):
            return idx
        idx = line.find(raw, idx + 1)
    return first


def _text_segments(line: str, raw: str | None, span: Any = None) -> tuple[str, str]:
    idx = _number_at(line, raw, span)
    if idx < 0:
        return _NUMBER_TOKEN.sub(" ", line), ""
    before, after = line[:idx], line[idx + len(raw):]
    numbers = list(_NUMBER_TOKEN.finditer(before))
    near_before = _SEGMENT_BREAK.split(before[numbers[-1].end():] if numbers else before)[-1]
    nxt = _NUMBER_TOKEN.search(after)
    near_after = after[:nxt.start()] if nxt else after
    near_after = re.split(r"[.;|•]", near_after, maxsplit=1)[0]
    if not re.search(r"[A-Za-z]", near_before) and not re.search(r"[A-Za-z]", near_after):
        near_before = _NUMBER_TOKEN.sub(" ", before)
    return near_before, near_after


def _text_parts(v: ExtractedValue, ctx: Mapping[str, Any], line: str) -> tuple[str, str]:
    before, after = ctx.get("label_before"), ctx.get("label_after")
    if before or after:
        return str(before or ""), str(after or "")
    return _text_segments(line, v.raw_text, (v.locator or {}).get("span"))


def _line_before(line: str, v: ExtractedValue) -> str:
    idx = _number_at(line, v.raw_text, (v.locator or {}).get("span"))
    return _NUMBER_TOKEN.sub(" ", line[:idx]) if idx > 0 else ""


def _object_kind(v: ExtractedValue, ctx: Mapping[str, Any]) -> str:
    return str(ctx.get("object_kind") or _KIND_BY_METHOD.get(v.method, "unknown"))


@dataclass
class _LabelResult:
    label: str
    flags: list[dict[str, str]] = field(default_factory=list)


def _row_label_cells(ctx: Mapping[str, Any]) -> list[str]:
    cells = [str(c) for c in (ctx.get("row_labels") or []) if c is not None and str(c).strip()]
    if cells:
        return cells
    return [str(ctx["row_label"])] if ctx.get("row_label") else []


def _names_metric(text: str | None, metrics: Iterable[str]) -> bool:
    norm = f" {normalize_label(_strip_unit_cues(text))} "
    return any(f" {m} " in norm for m in metrics)


def _metric_row_label(ctx: Mapping[str, Any], index: _ScopeIndex, fs: int, metrics: Iterable[str]) -> str | None:
    cells = _row_label_cells(ctx)
    if len(cells) <= 1:
        return ctx.get("row_label")
    cleaned = [(c, _clean_label(c, index, fs)) for c in cells]
    named = next((c for c, cl in cleaned if cl and _names_metric(cl, metrics)), None)
    return named or next((c for c, cl in cleaned if cl), ctx.get("row_label"))


def build_label(v: ExtractedValue, ctx: Mapping[str, Any], index: _ScopeIndex, fs: int = 1,
                metrics: Iterable[str] = ()) -> _LabelResult:
    """The metric phrase used for mapping, plus DERIVED_CHANGE / NON_ACTUAL_SERIES signals."""
    kind = _object_kind(v, ctx)
    title = _clean_label(ctx.get("object_title"), index, fs)
    axis = _clean_label(ctx.get("axis_title"), index, fs)
    page = _clean_label(ctx.get("page_title"), index, fs)
    flags: list[dict[str, str]] = []
    change: str | None = None

    def qualify(base: str, qualifier: str | None) -> str:
        """Attach a change/plan qualifier to the metric, unless the qualifier names the metric itself."""
        nonlocal change
        if not qualifier:
            return base
        q = _tidy(qualifier)
        if _CHANGE_CUE.search(q):
            change = q
        elif _NON_ACTUAL.search(q):
            flags.append(_flag("NON_ACTUAL_SERIES", "meaning", f"'{q}' is a plan/target/forecast, not an actual"))
        else:
            return base
        return q if (_has_metric_words(q) or not base) else _tidy(f"{base} {q}")

    label = ""
    if kind == "chart":
        fallback = title or axis or page
        series = ctx.get("series_name")
        s_clean = _clean_label(series, index, fs)
        if s_clean and not _CHANGE_CUE.search(s_clean) and not _NON_ACTUAL.search(s_clean):
            label = s_clean
        else:
            label = qualify(fallback, s_clean or None)
        if not change and _TITLE_CHANGE_CUE.search(str(ctx.get("object_title") or "")):
            change = _TITLE_CHANGE_CUE.search(str(ctx.get("object_title"))).group(0)
    elif kind in _TEXT_KINDS or (kind in ("ai_parse", "correction") and ctx.get("text_line")
                                  and not (ctx.get("row_label") or ctx.get("col_header"))):
        line = str(ctx.get("text_line") or v.raw_text or "")
        near_before, near_after = _text_parts(v, ctx, line)
        b, a = _clean_label(near_before, index, fs), _clean_label(near_after, index, fs)
        if not b and not a:
            b = _clean_label(_line_before(line, v), index, fs)
        causal = _CAUSAL_ACTIVE.match(b) if b else None
        passive = _CAUSAL_PASSIVE.match(a) if (a and not b) else None
        if causal or passive:
            subject = (causal or passive)["subject"]
            metric = (a if causal else passive["metric"]) or "lift"
            label = _tidy(f"{metric} from {subject}")
            flags.append(_flag("CAUSAL_LANGUAGE", "interpretation", f"causal wording in '{_tidy(line)}'"))
        elif b:
            label = b
            if a and _CHANGE_CUE.search(a):
                label, change = _tidy(f"{b} {a}"), a
        else:
            label = a
            if a and _CHANGE_CUE.search(a):
                change = a
        if not change and v.raw_text and re.search(r"(?:pp|pts?|bps)\s*\)?\s*$", str(v.raw_text), re.I):
            change = str(v.raw_text)
        label = label or title or page
    elif kind == "image":
        label = title or page or "image"
    else:
        row = _clean_label(_metric_row_label(ctx, index, fs, metrics), index, fs)
        col_raw = ctx.get("col_header")
        col = _clean_label(col_raw, index, fs)
        if row and not _CHANGE_CUE.search(row):
            label = row
            if col and (_CHANGE_CUE.search(col) or _NON_ACTUAL.search(col)):
                label = qualify(row, col_raw)
        elif row:
            label, change = row, row
        elif col and not _CHANGE_CUE.search(col):
            label = col
        else:
            label = qualify(title or page, col_raw if col else None)
    if not change:
        for key in ("col_header", "series_name", "row_label", "axis_title"):
            if _CHANGE_CUE.search(str(ctx.get(key) or "")):
                change = str(ctx.get(key))
                break
    if change:
        flags.append(_flag("DERIVED_CHANGE", "meaning", f"change/comparison value: '{_tidy(change)}'"))
    label = _tidy(label)
    if not label:
        flags.append(_flag("LABEL_MISSING", "meaning", "no metric phrase found next to the value"))
    return _LabelResult(label, flags)


@dataclass
class PeriodResolution:
    start: date | None
    end: date | None
    grain: str | None
    label: str | None
    source: str
    confidence: float
    flags: list[dict[str, str]] = field(default_factory=list)


_VALUE_LEVEL = ("category", "series", "object_title", "col_header", "row_label", "text_line")
_CTX_KEY = {"category": "category", "series": "series_name", "object_title": "object_title",
            "col_header": "col_header", "row_label": "row_label", "text_line": "text_line", "page_title": "page_title"}


def _category_is_date(ctx: Mapping[str, Any]) -> bool:
    if ctx.get("category_is_date"):
        return True
    fmt = ctx.get("category_number_format")
    return bool(fmt) and classify_format(fmt).is_date


_BARE_KINDS = ("month", "quarter", "half")
_CONTAINER_SOURCES = ("object_title",)
_YEAR_TOKEN = re.compile(r"(?<![\d.,])(?:19|20)\d{2}(?![\d.,%])")
_PRIOR = re.compile(
    r"(?<![A-Za-z])(?:(?P<year>py|ly|ya|(?:prior|last|previous|prev\.?)\s+(?:year|yr)|year\s+ago)"
    r"|(?P<quarter>pq|(?:prior|last|previous|prev\.?)\s+(?:quarter|qtr)|quarter\s+ago)"
    r"|(?P<month>pm|(?:prior|last|previous|prev\.?)\s+(?:month|mo)|month\s+ago))(?![A-Za-z])", re.I)
_PRIOR_PERIOD = re.compile(r"\s*(?:(?:prior|previous|prev\.?)(?:\s+period)?|last\s+period)\s*", re.I)


def _outer_levels(ctx: Mapping[str, Any]) -> list[str]:
    levels = [str(x).strip() for x in (ctx.get("category_levels") or []) if x is not None and str(x).strip()]
    return levels[:-1]


def _with_year(text: str, p: PeriodParse, year_text: Any, fs: int) -> PeriodParse | None:
    m = _YEAR_ONLY.match(str(year_text or ""))
    if not m:
        return None
    printed = str(year_text).strip()
    label = f"{text} {printed}"
    if p.kind == "month":
        month, year = p.end.month, int(m.group(1))
        if printed[:2].lower() == "fy" and fs != 1 and month >= fs:
            year -= 1
        return PeriodParse(date(year, month, 1), snap_month_end(date(year, month, 1)), "month", label, 0.9, "month")
    composed = parse_period(label, fiscal_year_start_month=fs)
    if composed is None or composed.kind != p.kind:
        return None
    return replace(composed, label=label, confidence=0.9)


def _category_period(text: str | None, ctx: Mapping[str, Any], reference: date | None, fs: int) -> PeriodParse | None:
    outer = _outer_levels(ctx)
    p = None
    if text:
        p = parse_period(text, reference=reference, fiscal_year_start_month=fs,
                         excel_serial=_category_is_date(ctx), date_system=str(ctx.get("date_system") or "1900"))
        p = p or parse_year_label(text, reference=reference)
    if p is not None and p.kind in _BARE_KINDS and p.confidence <= 0.7:
        for year_text in [*reversed(outer), *(ctx.get(k) for k in ("series_name", "col_header", "object_title"))]:
            composed = _with_year(str(text), p, year_text, fs)
            if composed is not None:
                return composed
    if p is None:
        for level in reversed(outer):
            q = parse_period(level, reference=reference, fiscal_year_start_month=fs)
            q = q or parse_year_label(level, reference=reference)
            if q is not None:
                return q
    return p


def _parse_source(name: str, text: str | None, ctx: Mapping[str, Any], reference: date | None,
                  fs: int) -> PeriodParse | None:
    if name == "category":
        return _category_period(text, ctx, reference, fs)
    if not text:
        return None
    p = parse_period(text, reference=reference, fiscal_year_start_month=fs)
    if p is None and name in ("series", "col_header", "row_label"):
        p = next((y for y in (parse_year_label(part, reference=reference) for part in str(text).split(" / "))
                  if y is not None), None)
    return p


def _unread_year(ctx: Mapping[str, Any]) -> str | None:
    for text in (ctx.get("category"), *_outer_levels(ctx), ctx.get("series_name"), ctx.get("col_header"),
                 ctx.get("row_label")):
        if text and _YEAR_TOKEN.search(str(text)):
            return str(text)
    return None


def _prior_column(ctx: Mapping[str, Any], source: str, current_end: date | None) -> tuple[str, str] | None:
    for key in ("col_header", "series_name"):
        text = ctx.get(key)
        for part in str(text or "").split(" / "):
            if not part.strip() or _CHANGE_CUE.search(part):
                continue
            m = _PRIOR.search(part)
            if m is None and not _PRIOR_PERIOD.fullmatch(part):
                continue
            printed = parse_period(part) if _CTX_KEY.get(source) == key else None
            if printed is not None and (current_end is None or printed.end < current_end):
                continue
            return str(text), (m.lastgroup if m else "period")
    return None


def _prior_months(unit: str, grain: str | None) -> int | None:
    if unit == "year":
        return 12
    if unit == "quarter":
        return 3 if grain in ("quarter", "qtd", "r3m") else None
    if unit == "month":
        return 1 if grain in ("month", "r3m", "r6m", "r12m", "ytd") else None
    return GRAIN_MONTHS.get(grain or "") or (3 if grain == "qtd" else None)


def _shift_back(start: date, end: date, grain: str | None, months: int, fs: int) -> tuple[date, date]:
    new_end = snap_month_end(add_months(end, -months))
    if grain == "ytd":
        return period_bounds(new_end, "ytd", fiscal_year_start_month=fs)[0], new_end
    return add_months(start, -months), new_end


def resolve_period(ctx: Mapping[str, Any], doc: Document, fs: int = 1, doc_grain: str | None = None) -> PeriodResolution:
    """Measurement period for one value, with its source, confidence and conflict flags."""
    doc_grain = doc_grain or (document_grain(doc) if doc.period_end is not None else None)
    reference = doc.period_end or doc.publication_date
    parsed: list[tuple[str, PeriodParse]] = []
    title_through = None
    for name in _VALUE_LEVEL:
        p = _parse_source(name, ctx.get(_CTX_KEY[name]), ctx, reference, fs)
        if p is not None and name in _CONTAINER_SOURCES and p.kind == "through":
            title_through = p
        elif p is not None:
            parsed.append((name, p))
    page = _parse_source("page_title", ctx.get("page_title"), ctx, reference, fs)
    if page is not None and _is_publication_month(page, doc):
        page = None
    footnote = None
    for text in ctx.get("footnotes") or []:
        p = parse_period(text, reference=reference, fiscal_year_start_month=fs)
        if p is not None and (footnote is None or (p.kind == "through" and footnote.kind != "through")):
            footnote = p
    flags: list[dict[str, str]] = []

    chosen = 0
    for i, (name, p) in enumerate(parsed[1:], start=1):
        if name in _CONTAINER_SOURCES:
            continue
        if parsed[chosen][1].contains(p) and not parsed[chosen][1].same_period(p) and p.confidence >= 0.6:
            chosen = i
    unread = None if parsed else _unread_year(ctx)
    if parsed:
        source, primary = parsed[chosen]
    elif unread is not None:
        return PeriodResolution(None, None, None, None, "none", 0.0, [_flag(
            "PERIOD_UNKNOWN", "association",
            f"'{unread}' names a year that is not a complete period on its own; confirm the period")])
    elif title_through is not None:
        source, primary = "object_title", title_through
    elif page is not None:
        source, primary = "page_title", page
    elif footnote is not None:
        source, primary = "footnote", footnote
    elif doc.period_end is not None:
        grain = doc_grain or "month"
        start = doc.period_start or period_bounds(doc.period_end, grain, fiscal_year_start_month=fs)[0]
        source, primary = "document", PeriodParse(start, doc.period_end, grain, doc.period_label or "", 0.6, "document")
    else:
        return PeriodResolution(None, None, None, None, "none", 0.0,
                                [_flag("PERIOD_UNKNOWN", "association", "no period in the value's context or document")])

    start, end, grain, label, conf = primary.start, primary.end, primary.grain, primary.label, primary.confidence
    if primary.kind in ("month", "date", "through", "serial"):
        own = _CTX_KEY.get(source)
        window = None
        for key in ("series_name", "col_header", "row_label", "object_title", "axis_title"):
            if key != own:
                window = window_grain(ctx.get(key))
                if window:
                    break
        if window is None and primary.kind == "through" and source in ("footnote", "page_title", "object_title"):
            if doc.period_end == end and doc_grain:
                window = doc_grain
        if window and window != "month":
            start, end, grain = window_bounds(end, window, fiscal_year_start_month=fs)
            label = f"{label} ({grain})"
    base = PeriodParse(start, end, grain, label or "", conf, primary.kind)
    prior = _prior_column(ctx, source, snap_month_end(doc.period_end) if doc.period_end else None)
    if prior is not None:
        text, unit = prior
        months = _prior_months(unit, grain)
        if months is None:
            return PeriodResolution(None, None, None, None, "none", 0.0, [_flag(
                "PERIOD_UNKNOWN", "association",
                f"'{text}' names a prior period, but which period it means for a {grain} value is unclear; "
                f"confirm the period")])
        start, end = _shift_back(start, end, grain, months, fs)
        if unit == "year":
            label = f"{label} (prior year)".strip()
            flags.append(_flag("PRIOR_YEAR_SHIFTED", "interpretation",
                               f"'{text}' column: period shifted back 12 months"))
        else:
            label = f"{label} (prior {unit})".strip()
            flags.append(_flag("PRIOR_PERIOD_SHIFTED", "interpretation",
                               f"'{text}' column: period shifted back {months} month{'s' if months != 1 else ''}"))
    conflicts = []
    for i, (name, other) in enumerate(parsed):
        if i != chosen and not (other.contains(base) or base.contains(other)):
            conflicts.append(f"{name} says {other.label}")
    for name, cutoff in (("object_title", title_through), ("footnote", footnote)):
        if cutoff is not None and cutoff.kind == "through" and base.end > cutoff.end:
            conflicts.append(f"{name} says data through {cutoff.label}")
    if conflicts:
        conf = round(conf * 0.5, 3)
        flags.append(_flag("PERIOD_CONFLICT", "association",
                           f"{source} gives {primary.label}; " + "; ".join(conflicts)))
    return PeriodResolution(start, end, grain, label or None, source, conf, flags)


def _is_publication_month(p: PeriodParse, doc: Document) -> bool:
    pub = doc.publication_date
    if pub is None or doc.period_end is None or p.grain != "month":
        return False
    return (p.end.year, p.end.month) == (pub.year, pub.month) and p.end != doc.period_end


def period_factor(period_confidence: float) -> float:
    """Multiplier applied to the extraction confidence (documented in the module docstring)."""
    if period_confidence <= 0.0 or period_confidence >= 0.9:
        return 1.0
    return 0.9 if period_confidence >= 0.6 else 0.5


_SCOPE_SOURCES = (("category", False), ("row_label", False), ("series_name", False), ("col_header", False),
                  ("text_label", False), ("object_title", True), ("page_title", True))


def resolve_scope(ctx: Mapping[str, Any], index: _ScopeIndex, text_label: str | None = None
                  ) -> tuple[dict[str, str], list[dict[str, str]]]:
    """{dimension: value} from the value's context; the highest-precedence source wins per dimension."""
    scope: dict[str, str] = {}
    origin: dict[str, str] = {}
    flags: list[dict[str, str]] = []
    for key, require_cue in _SCOPE_SOURCES:
        for text in _scope_texts(ctx, key, text_label):
            found, _rest = find_scope(text, index, require_cue=require_cue)
            for dim, values in found.items():
                if dim not in DIMENSIONS:
                    continue
                if len(values) > 1:
                    flags.append(_flag("SCOPE_AMBIGUOUS", "association", f"{key} names several {dim} values: {values}"))
                    continue
                if dim not in scope:
                    scope[dim], origin[dim] = values[0], key
                elif scope[dim] != values[0]:
                    flags.append(_flag("SCOPE_CONFLICT", "association",
                                       f"{origin[dim]} says {dim}={scope[dim]} but {key} says {values[0]}"))
    return scope, flags


def _scope_texts(ctx: Mapping[str, Any], key: str, text_label: str | None) -> list[str | None]:
    if key == "text_label":
        return [text_label]
    if key == "category":
        return [ctx.get("category"), *reversed(_outer_levels(ctx))]
    if key == "row_label":
        return [*_row_label_cells(ctx)] or [None]
    return [ctx.get(key)]


@dataclass(frozen=True)
class _Vocabulary:
    metrics: frozenset[str] = frozenset()
    brand: frozenset[str] = frozenset()

    @classmethod
    def build(cls, registry: Registry | None, brand: str | None) -> "_Vocabulary":
        metrics: set[str] = set()
        for kpi in (registry.kpis.values() if registry is not None else ()):
            for text in (kpi.kpi_id, kpi.label, *kpi.synonyms):
                norm = normalize_label(str(text or ""))
                if len(norm) > 1:
                    metrics.add(norm)
        names = [brand or ""]
        info = (registry.brands.get(brand or "") if registry is not None else None) or {}
        names += [str(info.get("display_name") or ""), *(str(a) for a in info.get("aliases") or [])]
        return cls(frozenset(metrics), frozenset(n for n in (normalize_label(x) for x in names) if n))


_SCOPE_KINDS = ("table", "worksheet", "pdf_table", "ai_parse")
_TOTAL_WORDS = frozenset(
    "total totals grand overall all national nation nationwide us usa united states company brand portfolio combined"
    .split())
_NEUTRAL_WORDS = _TOTAL_WORDS | _STOP_WORDS | frozenset(
    "region regions regional area district districts territory territories terr specialty specialties channel channels"
    " segment segments payer payers latest current this reporting reported report last prior previous prev full fiscal"
    " calendar rolling trailing moving month months monthly mo mos quarter quarters quarterly qtr year years yearly"
    " annual week weeks weekly period periods date to end ending ended mtd qtd ytd mat ttm ltm ago avg average value"
    " values result results metric metrics kpi kpis measure measures figure figures number numbers amount count counts"
    " data actual actuals act level levels unit units volume rate share score index pct percent percentage series and"
    " vs versus with from".split())
_BY_SCOPE = re.compile(r"\b(?:by|per|across)\s+(?:region|district|territor(?:y|ies)|specialt(?:y|ies)"
                       r"|payer(?:\s+channel)?|channel|segment)s?\b", re.I)


def _scope_residual(text: str, label: str, index: _ScopeIndex, vocab: _Vocabulary, fs: int) -> str:
    _found, rest = find_scope(text, index)
    rest = strip_periods(_strip_unit_cues(rest), fiscal_year_start_month=fs, include_bare=True)
    if _CHANGE_CUE.search(rest) or _NON_ACTUAL.search(rest) or _PRIOR.search(rest):
        return ""
    norm = f" {normalize_label(rest)} "
    phrases = sorted({normalize_label(label), *vocab.metrics, *vocab.brand} - {""}, key=len, reverse=True)
    for phrase in phrases:
        while f" {phrase} " in norm:
            norm = norm.replace(f" {phrase} ", " ")
    words = [w for w in norm.split() if len(w) > 1 and not any(ch.isdigit() for ch in w) and w not in _NEUTRAL_WORDS]
    return " ".join(words)


def _is_total(text: str, vocab: _Vocabulary) -> bool:
    norm = f" {normalize_label(text)} "
    for phrase in vocab.brand:
        norm = norm.replace(f" {phrase} ", " brand ")
    words = set(norm.split())
    return bool(words & _TOTAL_WORDS) and words <= _NEUTRAL_WORDS


def _scope_unknown_flags(ctx: Mapping[str, Any], kind: str, label: str, scope: Mapping[str, str],
                         index: _ScopeIndex, vocab: _Vocabulary, fs: int) -> list[dict[str, str]]:
    if kind == "chart":
        candidates = [ctx.get("category"), *_outer_levels(ctx)]
    elif kind in _SCOPE_KINDS:
        candidates = [*_row_label_cells(ctx), *str(ctx.get("col_header") or "").split(" / ")]
    else:
        return []
    unknown: list[str] = []
    explicit_total = False
    for text in (str(c).strip() for c in candidates if c is not None):
        if not text:
            continue
        if _scope_residual(text, label, index, vocab, fs):
            unknown.append(text)
        elif _is_total(text, vocab):
            explicit_total = True
    if unknown:
        names = ", ".join(f"'{u}'" for u in unknown)
        return [_flag("SCOPE_UNKNOWN", "association",
                      f"{names} is not a known region, territory or segment value, so the value is not compared "
                      f"with the brand total; confirm the scope")]
    by = _BY_SCOPE.search(str(ctx.get("object_title") or ""))
    if by and not scope and not explicit_total:
        return [_flag("SCOPE_UNKNOWN", "association",
                      f"the title '{ctx.get('object_title')}' breaks values down {by.group(0)}, but no scope was found "
                      f"for this value; confirm the scope")]
    return []


_MONTH_WORD = re.compile(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
                         r"|sep(?:t(?:ember)?|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?", re.I)


def mask_text(text: str | None) -> str:
    """Lowercase text with month names, quarter/half/fiscal tokens, years and digits masked."""
    t = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    t = _MONTH_WORD.sub("<m>", t)
    t = re.sub(r"(?<![a-z0-9])(?:fy|cy)\s*['’]?\s*\d{2,4}", "<fy>", t)
    t = re.sub(r"(?<![a-z0-9])(?:q[1-4]|[1-4]q|h[12]|[12]h)(?![a-z])", "<q>", t)
    t = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", "<y>", t)
    return re.sub(r"\d+(?:[.,]\d+)*", "#", t)


def fingerprint_parts(report_type: str | None = None, page_title: str | None = None, object_kind: str | None = None,
                      object_title: str | None = None, label: str | None = None,
                      col_header: str | None = None) -> tuple[str, ...]:
    """The masked layout parts a fingerprint hashes (stable across months: digits and dates masked)."""
    return (str(report_type or ""), mask_text(page_title), str(object_kind or ""), mask_text(object_title),
            mask_text(label), mask_text(col_header))


def fingerprint(source: Any = None, /, **parts: Any) -> str:
    """Layout fingerprint: sha over ``fingerprint_parts``."""
    values: dict[str, Any] = {}
    if source is not None:
        existing = source.get("fingerprint") if isinstance(source, Mapping) else getattr(source, "fingerprint", None)
        if existing and not parts:
            return str(existing)
        for name in FINGERPRINT_FIELDS:
            values[name] = source.get(name) if isinstance(source, Mapping) else getattr(source, name, None)
    values.update({k: v for k, v in parts.items() if k in FINGERPRINT_FIELDS})
    return stable_id("fingerprint-v1", *fingerprint_parts(**values))


_DEFINITION_CUE = re.compile(
    r"\b(?:only|excl(?:\.|udes?|uding)?|incl(?:\.|udes?|uding)?|based\s+on|defined|definition|basket|class|market"
    r"|source|projected|panel|normali[sz]ed|writers?|distinct|unique|average|mean|eligible|share\s+of|methodology"
    r"|note|per\s+hcp)\b|=", re.I)


def _registry_keywords(registry: Registry | None) -> tuple[str, ...]:
    words: set[str] = set()
    for kpi in (registry.kpis.values() if registry is not None else ()):
        if kpi.platform is None:
            continue
        for param in kpi.platform.params.values():
            for phrases in param.keywords.values():
                words.update(normalize_label(p) for p in phrases if p)
    return tuple(sorted(w for w in words if w))


def stated_rule_text(ctx: Mapping[str, Any], kind: str, keywords: tuple[str, ...] = ()) -> str:
    """Report text that may state the definition: titles, labels, footnotes, and definitional nearby text."""
    parts: list[str] = [ctx.get("object_title"), ctx.get("axis_title"), ctx.get("series_name"),
                        ctx.get("row_label"), ctx.get("col_header")]
    if kind in _TEXT_KINDS or ctx.get("text_line"):
        parts.append(ctx.get("text_line"))
    parts.extend(ctx.get("footnotes") or [])
    for text in ctx.get("nearby_text") or []:
        norm = " " + normalize_label(text) + " "
        if _DEFINITION_CUE.search(str(text)) or any(f" {k} " in norm for k in keywords):
            parts.append(text)
    seen: list[str] = []
    for p in parts:
        t = re.sub(r"\s+", " ", str(p or "")).strip()
        if t and t not in seen:
            seen.append(t)
    return " | ".join(seen)


def _merge_flags(*groups: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for f in group or []:
            item = dict(f) if isinstance(f, Mapping) else {"code": str(f), "dimension": "extraction", "detail": ""}
            key = (str(item.get("code")), str(item.get("detail", "")))
            if key not in seen:
                seen.add(key)
                out.append(item)
    return out


def _label_context(ctx: Mapping[str, Any]) -> str:
    parts = [ctx.get("page_title"), ctx.get("object_title"), ctx.get("series_name") or ctx.get("row_label"),
             ctx.get("col_header"), ctx.get("category"), ctx.get("text_line")]
    seen: list[str] = []
    for p in parts:
        t = re.sub(r"\s+", " ", str(p or "")).strip()
        if t and t not in seen:
            seen.append(t)
    return " > ".join(seen)[:1000]


def interpret_value(doc: Document, v: ExtractedValue, *, index: _ScopeIndex, fs: int = 1,
                    keywords: tuple[str, ...] = (), interpretation_version: str = INTERPRETATION_VERSION,
                    pages_by_number: Mapping[int, DocumentPage] | None = None,
                    doc_grain: str | None = None, vocabulary: _Vocabulary | None = None) -> Observation:
    """One extracted value -> one observation (pure; no store access)."""
    vocab = vocabulary or _Vocabulary.build(None, doc.brand)
    ctx = dict(v.context or {})
    page = (pages_by_number or {}).get(v.slide_or_page or -1)
    if page is not None:
        if not ctx.get("page_title") and page.title:
            ctx["page_title"] = page.title
        if page.hidden and not ctx.get("hidden"):
            ctx["hidden"] = True
    kind = _object_kind(v, ctx)
    reading = read_value(v, ctx)
    label = build_label(v, ctx, index, fs, vocab.metrics)
    period = resolve_period(ctx, doc, fs, doc_grain)
    text_label = None
    if kind in _TEXT_KINDS:
        near_before, near_after = _text_parts(v, ctx, str(ctx.get("text_line") or v.raw_text or ""))
        text_label = f"{near_before} {near_after}"
    scope, scope_flags = resolve_scope(ctx, index, text_label)
    scope_flags += _scope_unknown_flags(ctx, kind, label.label, scope, index, vocab, fs)
    extra: list[dict[str, str]] = []
    if v.method == ExtractionMethod.IMAGE_PLACEHOLDER and not any(f.get("code") == "IMAGE_NOT_PARSED" for f in v.flags or []):
        extra.append(_flag("IMAGE_NOT_PARSED", "extraction", "image may hold data; no value was read"))
    if ctx.get("hidden") and not any(f.get("code") == "HIDDEN_SLIDE" for f in v.flags or []):
        extra.append(_flag("HIDDEN_SLIDE", "extraction", "value is on a hidden slide"))
    flags = _merge_flags(v.flags or [], extra, reading.flags, label.flags, period.flags, scope_flags)
    confidence = 0.0 if v.method == ExtractionMethod.IMAGE_PLACEHOLDER else float(v.confidence)
    fp = fingerprint(report_type=doc.report_type, page_title=ctx.get("page_title"), object_kind=kind,
                     object_title=ctx.get("object_title"), label=label.label, col_header=ctx.get("col_header"))
    return Observation(
        observation_id=stable_id(v.value_id, interpretation_version), value_id=v.value_id, doc_id=v.doc_id,
        interpretation_version=interpretation_version, label=label.label, label_context=_label_context(ctx),
        value=reading.value, unit=reading.unit if reading.value is not None else None,
        display_half_unit=float(reading.half_unit), period_start=period.start, period_end=period.end,
        period_grain=period.grain, period_label=period.label, period_source=period.source,
        period_confidence=float(period.confidence), scope=scope, scope_key=make_scope_key(scope), brand=doc.brand,
        fingerprint=fp, stated_rule=stated_rule_text(ctx, kind, keywords), flags=flags,
        confidence=round(confidence * period_factor(period.confidence), 4), created_at=utcnow(), run_id=None)


_ROW_GROUP_METHODS = frozenset({ExtractionMethod.NATIVE_TABLE, ExtractionMethod.PDF_TABLE, ExtractionMethod.WORKBOOK,
                                ExtractionMethod.EMBEDDED_WORKSHEET})
_CELL_KEYS = ("row", "col", "cell", "bbox")


def _label_kind(text: str, index: _ScopeIndex, vocab: _Vocabulary, fs: int) -> str:
    if find_scope(text, index)[0]:
        return "scope"
    if _names_metric(text, vocab.metrics):
        return "metric"
    if parse_period(text, fiscal_year_start_month=fs) is not None or parse_year_label(text) is not None:
        return "period"
    return "other"


def _fill_row_groups(values: list[ExtractedValue], index: _ScopeIndex, vocab: _Vocabulary,
                     fs: int) -> list[ExtractedValue]:
    tables: dict[tuple[Any, ...], dict[int, list[int]]] = {}
    for i, v in enumerate(values):
        loc = v.locator or {}
        if v.method not in _ROW_GROUP_METHODS or not isinstance(loc.get("row"), int):
            continue
        if not (v.context or {}).get("row_label"):
            continue
        key = (v.method, v.slide_or_page, tuple(sorted((k, str(x)) for k, x in loc.items() if k not in _CELL_KEYS)))
        tables.setdefault(key, {}).setdefault(loc["row"], []).append(i)
    out = list(values)
    for rows in tables.values():
        previous: tuple[int, list[str]] | None = None
        for r in sorted(rows):
            cells = _row_label_cells(out[rows[r][0]].context or {})
            if previous is not None and previous[0] == r - 1 and len(cells) < len(previous[1]):
                above = previous[1]
                k = len(above) - len(cells)
                kinds = [_label_kind(c, index, vocab, fs) for c in (*above, *cells)]
                above_kinds, own_kinds = kinds[:len(above)], kinds[len(above):]
                if above_kinds[k:] == own_kinds and above_kinds[:len(cells)] != own_kinds:
                    cells = [*above[:k], *cells]
                    for i in rows[r]:
                        out[i] = replace(out[i], context={**(out[i].context or {}), "row_labels": cells})
            previous = (r, cells)
    return out


def interpret(doc: Document, values: list[ExtractedValue], pages: list[DocumentPage], registry: Registry,
              settings: Any, dimension_values: Mapping[str, Iterable[str]] | None,
              interpretation_version: str = INTERPRETATION_VERSION) -> list[Observation]:
    """Interpret every extracted value of one document."""
    index = _ScopeIndex.build(dimension_values)
    fs = int((registry.brands.get(doc.brand or "", {}) if registry is not None else {}).get("fiscal_year_start_month", 1))
    keywords = _registry_keywords(registry)
    pages_by_number = {p.page_number: p for p in pages or []}
    doc_grain = document_grain(doc, registry)
    vocab = _Vocabulary.build(registry, doc.brand)
    return [interpret_value(doc, v, index=index, fs=fs, keywords=keywords, doc_grain=doc_grain,
                            interpretation_version=interpretation_version, pages_by_number=pages_by_number,
                            vocabulary=vocab)
            for v in _fill_row_groups(list(values or []), index, vocab, fs)]
