from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..contracts import DocumentPage, ExtractedValue

__all__ = [
    "ExtractionOutput", "NumberParts", "SCALE_SUFFIXES", "extract_document", "is_non_value",
    "parse_number_parts", "parse_number_text", "split_reference_mark",
]


@dataclass
class ExtractionOutput:
    """Everything one extraction route produced for one document."""

    values: list[ExtractedValue] = field(default_factory=list)
    pages: list[DocumentPage] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    extractor_versions: dict[str, str] = field(default_factory=dict)
    document_properties: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """True when the route could not read the document at all."""
        return not self.values and not self.pages and any(i.get("severity") == "error" for i in self.issues)

    @property
    def error_text(self) -> str | None:
        """One line summarizing error issues (for ``documents.error``), or None."""
        errors = [f"{i.get('code')}: {i.get('detail')}" for i in self.issues if i.get("severity") == "error"]
        return "; ".join(errors) or None


SCALE_SUFFIXES: dict[str, float] = {"": 1.0, "K": 1e3, "M": 1e6, "MM": 1e6, "MN": 1e6, "B": 1e9, "BN": 1e9}

_NON_VALUES = frozenset({"", "-", "--", "–", "—", "n/a", "na", "nm", "n.m.", "nc", "n.c.", "*", "tbd", "none"})
_SPACES = re.compile(r"[    ]")
_FOOTNOTE_MARKS = re.compile(r"[*†‡¹²³⁰⁴-⁹]+$")
_REFERENCE_MARK = re.compile(r"(?<=[\d%A-Za-z)])\s*[(\[]\d{1,2}(?:,\s?\d{1,2})*[)\]]\s*$")
_SUFFIX_WORDS = {"THOUSAND": "K", "MILLION": "M", "BILLION": "B"}

_CELL_NUMBER = re.compile(
    r"(?P<sign>[+\-])?\s*(?P<cur>US\$|[$€£¥])?\s*(?P<open>\()?\s*(?P<sign2>[+\-])?\s*"
    r"(?P<cur2>US\$|[$€£¥])?\s*"
    r"(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)\s*"
    r"(?P<suffix>thousand|million|billion|bn|mm|mn|[kmb])?\s*(?P<unit>%|pp|pts?|bps|x)?\s*(?P<close>\))?\s*"
    r"(?P<unit2>%)?",
    re.IGNORECASE)


@dataclass(frozen=True)
class NumberParts:
    """A printed number taken apart."""

    number: float
    kind: str
    suffix: str
    multiplier: float
    currency: str | None
    decimals: int
    explicit_sign: bool
    accounting_negative: bool


def is_non_value(text: str | None) -> bool:
    """True for printed placeholders that mean 'no value' (``-``, ``n/a``, ``NM``...)."""
    return text is None or _clean(text).lower() in _NON_VALUES


def _clean(text: str) -> str:
    text = _SPACES.sub(" ", str(text)).strip()
    text = text.replace("−", "-").replace("–", "-") if len(text) > 1 else text
    text = _FOOTNOTE_MARKS.sub("", text).strip()
    return _REFERENCE_MARK.sub("", text).strip()


def split_reference_mark(text: str | None) -> tuple[str, str | None]:
    """Split a trailing footnote reference such as "(1)" off a printed number."""
    text = text or ""
    m = _REFERENCE_MARK.search(text)
    if m is None or parse_number_parts(text[:m.start()]) is None:
        return text, None
    return text[:m.start()].rstrip(), m.group().strip()


def parse_number_parts(text: str | None) -> NumberParts | None:
    """Parse a whole printed cell/token such as ``"5.9K"``, ``"84.4%"``, ``"(1,234)"``, ``"$12.3M"``."""
    if text is None or is_non_value(text):
        return None
    cleaned = _clean(text)
    m = _CELL_NUMBER.fullmatch(cleaned)
    if not m:
        return None
    if bool(m["open"]) != bool(m["close"]):
        return None
    if (m["sign"] and m["sign2"]) or (m["open"] and (m["sign"] or m["sign2"])) or (m["cur"] and m["cur2"]):
        return None
    if m["unit"] and m["unit2"]:
        return None
    number = float(m["num"].replace(",", ""))
    if not math.isfinite(number):
        return None
    negative = bool(m["open"]) or "-" in (m["sign"] or "", m["sign2"] or "")
    unit = (m["unit"] or m["unit2"] or "").lower()
    currency = m["cur"] or m["cur2"]
    kind = {"%": "percent", "pp": "points", "pt": "points", "pts": "points", "bps": "bps", "x": "multiple"}.get(unit)
    if kind is None:
        kind = "currency" if currency else "number"
    suffix = (m["suffix"] or "").upper()
    suffix = _SUFFIX_WORDS.get(suffix, suffix)
    decimals = len(m["num"].split(".", 1)[1]) if "." in m["num"] else 0
    return NumberParts(
        number=-number if negative else number, kind=kind, suffix=suffix, multiplier=SCALE_SUFFIXES[suffix],
        currency=currency.replace("US", "") if currency else None, decimals=decimals,
        explicit_sign=bool(m["sign"] or m["sign2"]), accounting_negative=bool(m["open"]))


def parse_number_text(text: str | None) -> tuple[float, str] | None:
    """``(bare_number, kind)`` for a printed number, else None."""
    parts = parse_number_parts(text)
    return None if parts is None else (parts.number, parts.kind)


def extract_document(settings, doc, blob: bytes, *, run_id: str | None = None) -> ExtractionOutput:
    """Route one registered document to its extractor (see ``kpi_evidence.extract.router``)."""
    from .router import extract_document as _route

    return _route(settings, doc, blob, run_id=run_id)
