from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import (
    Document, DocumentFormat, DocumentPage, DocumentStatus, ExtractedValue, ExtractionMethod, ReviewEvent,
)
from .ids import json_safe, sha256_bytes, sha256_text, stable_id, utcnow
from .periods import PeriodParse, find_periods, grain_from_bounds, parse_period, period_bounds, snap_month_end
from .registry import Registry, normalize_label
from .settings import Settings
from .store.base import Store
from .store.files import files_for, safe_name

SUPPORTED_FORMATS = frozenset({DocumentFormat.PPTX, DocumentFormat.PPTM, DocumentFormat.XLSX, DocumentFormat.PDF,
                               DocumentFormat.PNG, DocumentFormat.JPEG, DocumentFormat.TIFF})
METADATA_FIELDS = ("brand", "report_type", "period_end", "period_start", "period_label", "period_grain",
                   "publication_date")
DOC_FIELDS = ("brand", "report_type", "period_end", "period_start", "period_label", "publication_date")
DOC_PROPERTIES_KEY = "_document_properties"
EVIDENCE_KEY = "_evidence_text"
_EVIDENCE_CHARS = 3000
AI_CONFIDENCE_CAP = 0.6
_PERIOD_DEPENDENTS = ("period_start", "period_label", "period_grain")
_DATE_FIELDS = ("period_end", "period_start", "publication_date")

_OLE_MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
_REMEDY_ENCRYPTED = ("This file is encrypted (a sensitivity label or password). Remove the sensitivity label "
                     "encryption or export to PDF, then upload it again.")
_REMEDY_LEGACY_PPT = ("This is a legacy PowerPoint 97-2003 (.ppt) file. Open it in PowerPoint, save as .pptx, "
                      "then upload it again.")
_REMEDY_LEGACY_XLS = ("This is a legacy Excel 97-2003 (.xls) workbook. Open it in Excel, save as .xlsx, then "
                      "upload it again.")
_REMEDY_UNKNOWN = ("This file type is not supported ({detail}). Upload a .pptx, .pdf, .xlsx or an image "
                   "(.png, .jpg, .tif).")


def _utf16(name: str) -> bytes:
    return name.encode("utf-16-le")


def sniff(blob: bytes, file_name: str) -> tuple[DocumentFormat, str]:
    """Detect the format from the bytes."""
    ext = Path(file_name or "").suffix.lower()
    if not blob or len(blob) < 4:
        return DocumentFormat.UNKNOWN, "empty or truncated file"
    head = blob[:8]
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return _sniff_zip(blob, ext)
    if b"%PDF-" in blob[:1024]:
        return DocumentFormat.PDF, "PDF document"
    if head == b"\x89PNG\r\n\x1a\n":
        return DocumentFormat.PNG, "PNG image"
    if head[:3] == b"\xFF\xD8\xFF":
        return DocumentFormat.JPEG, "JPEG image"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return DocumentFormat.TIFF, "TIFF image"
    if head == _OLE_MAGIC:
        if _utf16("EncryptedPackage") in blob or _utf16("EncryptionInfo") in blob:
            return DocumentFormat.ENCRYPTED_OOXML, "OLE container with an EncryptedPackage stream (encrypted Office file)"
        if _utf16("PowerPoint Document") in blob:
            return DocumentFormat.LEGACY_PPT, "legacy PowerPoint 97-2003 presentation (OLE compound file)"
        if _utf16("Workbook") in blob or _utf16("Book") in blob:
            return DocumentFormat.LEGACY_PPT, "legacy Excel 97-2003 workbook (OLE compound file)"
        if _utf16("WordDocument") in blob:
            return DocumentFormat.LEGACY_PPT, "legacy Word 97-2003 document (OLE compound file)"
        return DocumentFormat.LEGACY_PPT, "OLE compound file (legacy Office format)"
    return DocumentFormat.UNKNOWN, f"unrecognized file signature {blob[:4].hex()}" + (f" ({ext})" if ext else "")


def _sniff_zip(blob: bytes, ext: str) -> tuple[DocumentFormat, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = set(archive.namelist())
            types = ""
            if "[Content_Types].xml" in names:
                info = archive.getinfo("[Content_Types].xml")
                if info.file_size <= 5_000_000:
                    types = archive.read(info).decode("utf-8", errors="replace").lower()
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
        return DocumentFormat.UNKNOWN, f"corrupt or unreadable ZIP archive ({type(exc).__name__})"
    if not types:
        return DocumentFormat.UNKNOWN, "ZIP archive without [Content_Types].xml (not an Office document)"
    if "ms-powerpoint.presentation.macroenabled" in types or "ms-powerpoint.slideshow.macroenabled" in types \
            or "ms-powerpoint.template.macroenabled" in types:
        return DocumentFormat.PPTM, "macro-enabled PowerPoint presentation (macros are never run)"
    if "presentationml.presentation.main" in types or "presentationml.slideshow.main" in types \
            or "presentationml.template.main" in types:
        return DocumentFormat.PPTX, "PowerPoint presentation (OOXML)"
    if "spreadsheetml.sheet.main" in types or "ms-excel.sheet.macroenabled" in types \
            or "spreadsheetml.template.main" in types:
        return DocumentFormat.XLSX, "Excel workbook (OOXML)"
    if "wordprocessingml" in types:
        return DocumentFormat.UNKNOWN, "Word document (OOXML)"
    if any(n.startswith("ppt/") for n in names):
        return DocumentFormat.PPTX, "PowerPoint package (detected from ppt/ parts)"
    if any(n.startswith("xl/") for n in names):
        return DocumentFormat.XLSX, "Excel package (detected from xl/ parts)"
    return DocumentFormat.UNKNOWN, "OOXML package of an unsupported type"


def _status_for(fmt: DocumentFormat, detail: str) -> tuple[DocumentStatus, str | None]:
    if fmt in SUPPORTED_FORMATS:
        return DocumentStatus.REGISTERED, None
    if fmt == DocumentFormat.ENCRYPTED_OOXML:
        return DocumentStatus.ENCRYPTED, _REMEDY_ENCRYPTED
    if fmt == DocumentFormat.LEGACY_PPT:
        if "Excel" in detail:
            return DocumentStatus.UNSUPPORTED_FORMAT, _REMEDY_LEGACY_XLS
        if "Word" in detail:
            return DocumentStatus.UNSUPPORTED_FORMAT, _REMEDY_UNKNOWN.format(detail=detail)
        return DocumentStatus.UNSUPPORTED_FORMAT, _REMEDY_LEGACY_PPT
    return DocumentStatus.UNSUPPORTED_FORMAT, _REMEDY_UNKNOWN.format(detail=detail)


def register_file(store: Store, settings: Settings, *, blob: bytes, file_name: str, uploaded_by: str | None = None,
                  source_path: str | None = None) -> Document:
    """Record one file."""
    doc_id = sha256_bytes(blob)
    existing = store.get(Document, doc_id=doc_id)
    if existing is not None:
        return existing
    fmt, detail = sniff(blob, file_name)
    status, error = _status_for(fmt, detail)
    now = utcnow()
    if source_path is None:
        rel = f"inbox/{now:%Y/%m/%d}/{doc_id[:12]}_{safe_name(file_name)}"
        path = files_for(settings).write_bytes(rel, blob)
    else:
        path = str(source_path)
    doc = Document(doc_id=doc_id, file_name=Path(file_name).name or file_name, file_path=path, size_bytes=len(blob),
                   format=fmt, status=status, registered_at=now, updated_at=now, uploaded_by=uploaded_by,
                   sniff_detail=detail, error=error)
    store.insert_new([doc])
    store.insert_new([ReviewEvent(event_id=stable_id("uploaded", doc_id), result_id=None, doc_id=doc_id,
                                  event="uploaded", actor=uploaded_by or "system:intake", event_at=now,
                                  detail={"file_name": doc.file_name, "format": fmt.value, "status": status.value})])
    return store.get(Document, doc_id=doc_id) or doc


_SHA_PREFIX = re.compile(r"^(?P<sha>[0-9a-f]{12})_(?P<name>.+)$")


def _original_name(path: str, blob: bytes) -> str:
    name = PurePosixPath(str(path).replace("\\", "/")).name
    m = _SHA_PREFIX.match(name)
    if m and sha256_bytes(blob).startswith(m["sha"]):
        return m["name"]
    return name


def _inbox_order(path: str) -> tuple[str, str, str]:
    name = PurePosixPath(str(path).replace("\\", "/")).name
    m = _SHA_PREFIX.match(name)
    original = m["name"] if m else name
    return original.lower(), original, str(path)


def register_inbox(store: Store, settings: Settings) -> list[Document]:
    """Register every new file under ``<volume>/inbox/`` in place, ordered by original file name."""
    fs = files_for(settings)
    known = store.read_df(Document, columns_sql="file_path, status, failed_stage")
    registered = {str(r.file_path) for r in known.itertuples()
                  if not (str(r.status) == DocumentStatus.FAILED.value and str(r.failed_stage) == "register")}
    out: dict[str, Document] = {}
    for path in sorted(fs.list_files("inbox"), key=_inbox_order):
        if str(path) in registered:
            continue
        try:
            blob = fs.read_bytes(path)
        except OSError as exc:
            out[path] = _unreadable_document(store, path, exc)
            continue
        doc = register_file(store, settings, blob=blob, file_name=_original_name(path, blob), source_path=path)
        out.setdefault(doc.doc_id, doc)
    return list(out.values())


def _unreadable_document(store: Store, path: str, exc: Exception) -> Document:
    now = utcnow()
    doc = Document(doc_id=sha256_text(f"unreadable:{path}"), file_name=PurePosixPath(path.replace("\\", "/")).name,
                   file_path=path, size_bytes=0, format=DocumentFormat.UNKNOWN, status=DocumentStatus.FAILED,
                   registered_at=now, updated_at=now, failed_stage="register",
                   error=f"could not read the file: {type(exc).__name__}: {exc}"[:2000])
    store.upsert([doc])
    return doc


def _proposal(value: Any, confidence: float, source: str, alternatives: list[dict[str, Any]] | None = None
              ) -> dict[str, Any]:
    if isinstance(value, (date, datetime)):
        value = value.isoformat()[:10]
    return {"value": value, "confidence": round(float(confidence), 3), "source": source,
            "alternatives": json_safe(alternatives or [])}


def _document_properties(doc: Document) -> dict[str, Any]:
    props = (doc.metadata_proposals or {}).get(DOC_PROPERTIES_KEY) or {}
    if not isinstance(props, Mapping):
        return {}
    return {str(k).lower().split(":")[-1]: v for k, v in props.items() if v not in (None, "")}


def _filename_text(file_name: str) -> str:
    stem = Path(file_name or "").stem
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)
    return stem.replace("_", " ").replace(".", " ")


def _first_page_title(pages: list[DocumentPage]) -> str | None:
    for p in sorted(pages or [], key=lambda p: p.page_number)[:2]:
        if p.title and p.title.strip():
            return p.title.strip()
    return None


def _body_text(page: DocumentPage) -> str:
    text, title = page.text or "", (page.title or "").strip()
    if not title:
        return text
    lines = [line for line in text.splitlines() if line.strip() != title]
    body = "\n".join(lines)
    if body == text and text.startswith(title):
        body = text[len(title):]
    return body


def _phrase_hits(text: str | None, phrases: Mapping[str, str]) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    if not text:
        return out
    norm = " " + normalize_label(text) + " "
    for phrase, target in phrases.items():
        if not phrase:
            continue
        for m in re.finditer(rf"(?<= ){re.escape(phrase)}(?= )", norm):
            out.setdefault(target, []).append(m.span())
    return out


def _brands_in(text: str | None, aliases: Mapping[str, str]) -> set[str]:
    return set(_phrase_hits(text, aliases))


def _report_types_in(text: str | None, registry: Registry) -> set[str]:
    keywords = {normalize_label(k): rt for rt, spec in registry.report_types.items()
                for k in (spec.get("keywords") or []) + [spec.get("display_name", ""), rt.replace("_", " ")] if k}
    hits = [(s, e, rt) for rt, spans in _phrase_hits(text, keywords).items() for s, e in spans]
    keep = {rt for s, e, rt in hits
            if not any(s2 <= s and e <= e2 and (e2 - s2) > (e - s) and rt2 != rt for s2, e2, rt2 in hits)}
    return keep


def _vote(groups: dict[str, set[str]]) -> tuple[str | None, float, str, list[dict[str, Any]]]:
    support: dict[str, list[str]] = {}
    for source, values in groups.items():
        for v in sorted(values):
            support.setdefault(v, []).append(source)
    if not support:
        return None, 0.0, "none", []
    ranked = sorted(support, key=lambda v: (-len(support[v]), v))
    best = ranked[0]
    alternatives = [{"value": v, "source": "+".join(support[v])} for v in ranked[1:]]
    if alternatives:
        return best, 0.5, "+".join(support[best]), alternatives
    return best, (0.95 if len(support[best]) >= 2 else 0.6), "+".join(support[best]), []


def _propose_brand(registry: Registry, name_text: str, titles: list[str], page_texts: list[str]) -> dict[str, Any]:
    aliases = registry.brand_aliases()
    groups = {"filename": _brands_in(name_text, aliases),
              "title": set().union(*[_brands_in(t, aliases) for t in titles]) if titles else set(),
              "page_text": set().union(*[_brands_in(t, aliases) for t in page_texts]) if page_texts else set()}
    value, conf, source, alts = _vote({k: v for k, v in groups.items() if v})
    return _proposal(value, conf, source, alts)


def _propose_report_type(registry: Registry, name_text: str, titles: list[str]) -> dict[str, Any]:
    groups = {"filename": _report_types_in(name_text, registry),
              "title": set().union(*[_report_types_in(t, registry) for t in titles]) if titles else set()}
    value, conf, source, alts = _vote({k: v for k, v in groups.items() if v})
    return _proposal(value, conf, source, alts)


_STRONG_THROUGH = re.compile(
    r"(?:data\s+(?:through|thru|as\s+of)|as\s+of|(?:month|period|quarter)s?[\s-]+end(?:ing|ed))[\s:,\-–—(]*$", re.I)


def _explicit_periods(texts: Iterable[tuple[str, str]], fs: int) -> list[tuple[PeriodParse, str]]:
    out: list[tuple[PeriodParse, str]] = []
    for source, text in texts:
        if not text:
            continue
        for p in find_periods(text, fiscal_year_start_month=fs):
            prefix = str(text)[max(0, p.span[0] - 30):p.span[0]]
            if p.kind == "through" and _STRONG_THROUGH.search(prefix):
                out.append((p, source))
    return out


def _first_period(text: str | None, fs: int) -> PeriodParse | None:
    for p in find_periods(text, fiscal_year_start_month=fs):
        if p.kind != "through":
            return p
    return None


def dominant_value_period(values: Iterable[ExtractedValue], fs: int = 1) -> tuple[PeriodParse | None, int]:
    """Most common period end among the values' own labels (category, column, series, row); ties -> latest."""
    counts: Counter[date] = Counter()
    sample: dict[date, PeriodParse] = {}
    for v in values:
        if v.method == ExtractionMethod.IMAGE_PLACEHOLDER:
            continue
        ctx = v.context or {}
        for key in ("category", "col_header", "series_name", "row_label"):
            p = parse_period(ctx.get(key), fiscal_year_start_month=fs)
            if p is not None:
                counts[p.end] += 1
                sample.setdefault(p.end, p)
                break
    if not counts:
        return None, 0
    best = max(counts, key=lambda e: (counts[e], e))
    return sample[best], counts[best]


def _period_fields(p: PeriodParse, grain: str, fs: int) -> dict[str, Any]:
    start, end = period_bounds(p.end, grain, fiscal_year_start_month=fs) if grain != p.grain else (p.start, p.end)
    return {"period_end": end, "period_start": start, "period_grain": grain, "period_label": p.label}


def _propose_period(name_text: str, titles: list[tuple[str, str]], page_texts: list[tuple[str, str]],
                    values: list[ExtractedValue], fs: int, default_grain: str,
                    prop_date: date | None = None) -> dict[str, dict[str, Any]]:
    explicit = _explicit_periods([*titles, *page_texts], fs)
    title_p = next((p for _s, t in titles if (p := _first_period(t, fs)) is not None), None)
    file_p = _first_period(name_text, fs)
    values_p, _n = dominant_value_period(values, fs)

    def grain_of(p: PeriodParse) -> str:
        return default_grain if p.kind in ("through", "date", "serial") else p.grain

    def alt(p: PeriodParse, source: str) -> dict[str, Any]:
        return {"value": p.end.isoformat(), "label": p.label, "source": source}

    chosen: PeriodParse | None = None
    source, conf, alternatives = "none", 0.0, []
    if explicit:
        ends = Counter(p.end for p, _s in explicit)
        best_end = max(ends, key=lambda e: (ends[e], e))
        chosen, src = next((p, s) for p, s in explicit if p.end == best_end)
        source, conf = f"{src}: data through {chosen.label}", 0.95
        others = {p.end: (p, s) for p, s in explicit if p.end != best_end}
        alternatives = [alt(p, s) for p, s in others.values()]
        if others:
            conf = 0.5
        if values_p is not None and values_p.end > chosen.end:
            conf = 0.5
            alternatives.append(alt(values_p, "values"))
    else:
        groups = [(name, p) for name, p in (("title", title_p), ("filename", file_p), ("values", values_p)) if p]
        if groups:
            if len({p.end for _name, p in groups}) == 1:
                chosen = groups[0][1]
                conf = 0.95 if len(groups) >= 2 else 0.6
                source = "+".join(n for n, _p in groups)
            else:
                pick = next(((n, p) for n, p in groups if n == "values"), groups[0])
                source, chosen, conf = pick[0], pick[1], 0.5
                alternatives = [alt(p, n) for n, p in groups if p.end != chosen.end]
    out: dict[str, dict[str, Any]] = {}
    if chosen is None:
        for f in ("period_end", *_PERIOD_DEPENDENTS):
            out[f] = _proposal(None, 0.0, "none")
    else:
        fields = _period_fields(chosen, grain_of(chosen), fs)
        out["period_end"] = _proposal(fields["period_end"], conf, source, alternatives)
        for f in _PERIOD_DEPENDENTS:
            out[f] = _proposal(fields[f], conf, source)
    out["publication_date"] = _propose_publication(title_p, file_p, chosen, prop_date)
    return out


def _propose_publication(title_p: PeriodParse | None, file_p: PeriodParse | None, measured: PeriodParse | None,
                         prop_date: date | None) -> dict[str, Any]:
    if prop_date is not None and measured is not None and prop_date < measured.end:
        prop_date = None
    candidates = [(n, p) for n, p in (("title", title_p), ("filename", file_p)) if p is not None and p.grain == "month"]
    if measured is not None:
        candidates = [(n, p) for n, p in candidates if p.end != measured.end]
    if not candidates:
        return _proposal(prop_date, 0.6 if prop_date else 0.0, "document_properties" if prop_date else "none")
    starts = {p.start for _n, p in candidates}
    first = candidates[0][1]
    source = "+".join(n for n, _p in candidates)
    value = first.start
    if prop_date is not None and first.start <= prop_date <= first.end:
        value, source = prop_date, f"{source}+document_properties"
    if len(starts) == 1:
        return _proposal(value, 0.95 if len(candidates) >= 2 else 0.6, source)
    return _proposal(value, 0.5, candidates[0][0],
                     [{"value": p.start.isoformat(), "source": n} for n, p in candidates[1:]])


def _date_from_props(props: Mapping[str, Any]) -> date | None:
    for key in ("modified", "created"):
        raw = props.get(key)
        if isinstance(raw, datetime):
            return raw.date()
        if isinstance(raw, date):
            return raw
        if isinstance(raw, str) and re.match(r"\d{4}-\d{2}-\d{2}", raw):
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                continue
    return None


def propose_metadata(registry: Registry, doc: Document, pages: list[DocumentPage],
                     values: list[ExtractedValue]) -> dict[str, dict[str, Any]]:
    """Deterministic proposals ``{field: {value, confidence, source, alternatives}}`` (dates as ISO text)."""
    props = _document_properties(doc)
    name_text = _filename_text(doc.file_name)
    first_title = _first_page_title(pages)
    titles = [(src, str(t)) for src, t in (("document_title", props.get("title")), ("first_page_title", first_title)) if t]
    title_texts = [t for _src, t in titles] + ([str(props["subject"])] if props.get("subject") else [])
    footnotes: list[str] = []
    for v in values or []:
        for f in (v.context or {}).get("footnotes") or []:
            if f and f not in footnotes:
                footnotes.append(str(f))
    page_texts = [t for t in (_body_text(p) for p in pages or []) if t]
    brand = _propose_brand(registry, name_text, title_texts, page_texts + footnotes)
    fs = int(registry.brands.get(brand["value"] or "", {}).get("fiscal_year_start_month", 1))
    report_type = _propose_report_type(registry, name_text, title_texts)
    default_grain = str(registry.report_types.get(report_type["value"] or "", {}).get("default_grain", "month"))
    other_texts = ([("page_text", t) for t in page_texts] + [("footnote", f) for f in footnotes]
                   + [("document_properties", str(props[k])) for k in ("subject", "keywords", "description")
                      if props.get(k)])
    period = _propose_period(name_text, titles, other_texts, list(values or []), fs, default_grain,
                             _date_from_props(props))
    evidence = "\n".join([f"File name: {doc.file_name}", *[f"Title: {t}" for t in title_texts],
                          *[p.text for p in sorted(pages or [], key=lambda p: p.page_number)[:3] if p.text],
                          *footnotes[:5]])
    return {"brand": brand, "report_type": report_type, **period, EVIDENCE_KEY: evidence[:_EVIDENCE_CHARS]}


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        p = parse_period(text)
        if p is None:
            raise ValueError(f"not a date or period: {value!r}") from None
        return p.end


def _typed(field_name: str, value: Any) -> Any:
    if field_name in _DATE_FIELDS:
        return _as_date(value)
    return None if value in (None, "") else str(value)


def document_grain(doc: Document, registry: Registry | None = None) -> str | None:
    """The document's measurement grain."""
    if doc.period_end is None:
        return None
    proposals = doc.metadata_proposals or {}
    item, end_item = proposals.get("period_grain"), proposals.get("period_end")
    if isinstance(item, Mapping) and item.get("value"):
        end_value = end_item.get("value") if isinstance(end_item, Mapping) else None
        if end_value in (None, doc.period_end.isoformat()):
            return str(item["value"])
    fs = int((registry.brands.get(doc.brand or "", {}) if registry is not None else {}).get("fiscal_year_start_month", 1))
    derived = grain_from_bounds(doc.period_start, doc.period_end, fiscal_year_start_month=fs)
    if derived:
        return derived
    if registry is not None and doc.report_type in registry.report_types:
        return str(registry.report_types[doc.report_type].get("default_grain", "month"))
    return "month"


def _required_fields(registry: Registry) -> list[str]:
    meta = (registry.policy or {}).get("metadata") or {}
    return list(meta.get("required_fields") or ["brand", "report_type", "period_end"])


def _ai_fill(ai_provider: Any, registry: Registry, doc: Document, proposals: dict[str, Any]) -> dict[str, Any]:
    missing = [f for f in ("brand", "report_type", "period_end", "publication_date")
               if (proposals.get(f) or {}).get("value") is None]
    if ai_provider is None or not missing or not hasattr(ai_provider, "propose_metadata"):
        return proposals
    out = dict(proposals)
    text = str(proposals.get(EVIDENCE_KEY) or f"File name: {doc.file_name}")
    try:
        raw = ai_provider.propose_metadata(text, missing) or {}
    except Exception as exc:
        out["_ai_error"] = f"{type(exc).__name__}: {exc}"[:500]
        return out
    model = str(getattr(ai_provider, "model", None) or getattr(ai_provider, "name", None) or type(ai_provider).__name__)
    aliases = registry.brand_aliases()
    for f in missing:
        item = raw.get(f) if isinstance(raw, Mapping) else None
        if item is None:
            continue
        value = item.get("value") if isinstance(item, Mapping) else item
        conf = min(float(item.get("confidence", AI_CONFIDENCE_CAP)) if isinstance(item, Mapping) else AI_CONFIDENCE_CAP,
                   AI_CONFIDENCE_CAP)
        try:
            if f == "brand":
                value = value if value in registry.brands else aliases.get(normalize_label(str(value)))
            elif f == "report_type":
                value = value if value in registry.report_types else None
            else:
                value = _as_date(value)
                value = snap_month_end(value) if f == "period_end" and value else value
        except ValueError:
            value = None
        if value is None:
            continue
        out[f] = _proposal(value, conf, f"ai:{model}")
        if f == "period_end":
            grain = str(registry.report_types.get((out.get("report_type") or {}).get("value") or "", {})
                        .get("default_grain", "month"))
            start, _end = period_bounds(value, grain)
            for dep, dep_value in (("period_start", start), ("period_grain", grain), ("period_label", None)):
                if (out.get(dep) or {}).get("value") is None:
                    out[dep] = _proposal(dep_value, conf, f"ai:{model}")
    return out


_FILE_VERSION = re.compile(r"(?:^|[\s_\-.(])(?:v|ver|version|rev|r)\s*(\d{1,3})(?=$|[\s_\-.)])", re.I)
_FILE_REVISED = re.compile(r"(?:^|[\s_\-.(])revised(?=$|[\s_\-.)])", re.I)


def file_name_version(file_name: str) -> int:
    """Version token in a file name ("_v2" -> 2, "rev3" -> 3, "revised" -> 2); 1 when there is none."""
    stem = Path(file_name or "").stem
    m = _FILE_VERSION.search(stem)
    if m:
        return int(m.group(1))
    return 2 if _FILE_REVISED.search(stem) else 1


def _order_key(d: Document) -> tuple[int, datetime, str, str]:
    return (file_name_version(d.file_name), d.registered_at, d.file_name, d.doc_id)


_NOT_CURRENT = {DocumentStatus.SUPERSEDED, DocumentStatus.AWAITING_CONFIRMATION, DocumentStatus.UNSUPPORTED_FORMAT,
                DocumentStatus.ENCRYPTED}


def _assign_family(store: Store, doc: Document) -> list[Document]:
    if not (doc.brand and doc.report_type and doc.period_end):
        doc.report_family, doc.version = None, 1
        return []
    family = stable_id(doc.brand, doc.report_type, doc.period_end.isoformat())
    doc.report_family = family
    members = [m for m in store.read(Document, "report_family = :f AND doc_id <> :d", {"f": family, "d": doc.doc_id})]
    earlier = sorted((m for m in members if _order_key(m) < _order_key(doc)), key=_order_key)
    later = sorted((m for m in members if _order_key(m) > _order_key(doc)), key=_order_key)
    doc.version = 1 + len(earlier)
    doc.supersedes_doc_id = earlier[-1].doc_id if earlier else None
    changed: list[Document] = []
    now = utcnow()
    ordered = [*earlier, doc, *later]
    for i, m in enumerate(ordered[len(earlier) + 1:], start=len(earlier) + 1):
        version, previous = i + 1, ordered[i - 1].doc_id
        if (m.version, m.supersedes_doc_id) != (version, previous):
            m.version, m.supersedes_doc_id, m.updated_at = version, previous, now
            changed.append(m)
    if doc.status in _NOT_CURRENT:
        return changed
    newer_current = [m for m in later if m.status not in _NOT_CURRENT]
    if newer_current:
        doc.status = DocumentStatus.SUPERSEDED
        doc.superseded_by_doc_id = newer_current[0].doc_id
        return changed
    for m in earlier:
        if m.status in _NOT_CURRENT:
            continue
        m.status = DocumentStatus.SUPERSEDED
        m.superseded_by_doc_id = doc.doc_id
        m.updated_at = now
        changed.append(m)
    return changed


def _retire_results(store: Store, doc_ids: Iterable[str]) -> None:
    for doc_id in doc_ids:
        store.execute(f"UPDATE {store.fq('kpi_results')} SET is_current = FALSE "
                      f"WHERE doc_id = :d AND is_current = TRUE", {"d": doc_id})


def _superseded_ids(doc: Document, changed: list[Document]) -> list[str]:
    return [d.doc_id for d in [doc, *changed] if d.status == DocumentStatus.SUPERSEDED]


def _write_family_changes(store: Store, doc: Document, changed: list[Document]) -> None:
    with store.transaction():
        store.upsert([doc, *changed])
        _retire_results(store, _superseded_ids(doc, changed))


def apply_proposals(store: Store, registry: Registry, settings: Settings, doc: Document,
                    proposals: dict[str, Any], ai_provider: Any = None) -> Document:
    """Fill the document from proposals and decide READY or AWAITING_CONFIRMATION (with family/version)."""
    proposals = _ai_fill(ai_provider, registry, doc, dict(proposals or {}))
    props = (doc.metadata_proposals or {}).get(DOC_PROPERTIES_KEY)
    doc.metadata_proposals = {**proposals, **({DOC_PROPERTIES_KEY: props} if props else {})}
    doc.updated_at = utcnow()
    if doc.confirmed_by:
        if doc.status not in _NOT_CURRENT:
            doc.status = DocumentStatus.READY
        changed = _assign_family(store, doc)
        _write_family_changes(store, doc, changed)
        return doc
    threshold = float(settings.metadata_confidence_threshold)
    required = _required_fields(registry)

    def confident(name: str) -> bool:
        item = proposals.get(name) or {}
        return item.get("value") is not None and float(item.get("confidence") or 0.0) >= threshold

    for name in DOC_FIELDS:
        item = proposals.get(name) or {}
        gate = "period_end" if name in _PERIOD_DEPENDENTS else name
        if gate in required and not confident(gate):
            setattr(doc, name, None)
        else:
            setattr(doc, name, _typed(name, item.get("value")))
    doc.needs_confirmation = [f for f in required if not confident(f)]
    if doc.status not in (DocumentStatus.UNSUPPORTED_FORMAT, DocumentStatus.ENCRYPTED, DocumentStatus.SUPERSEDED):
        doc.status = DocumentStatus.AWAITING_CONFIRMATION if doc.needs_confirmation else DocumentStatus.READY
    changed = _assign_family(store, doc)
    _write_family_changes(store, doc, changed)
    return doc


def _normalize_confirmed(registry: Registry, name: str, value: Any) -> Any:
    if name == "brand" and value is not None:
        if value in registry.brands:
            return value
        brand = registry.brand_aliases().get(normalize_label(str(value)))
        if brand is None:
            raise ValueError(f"unknown brand {value!r}; add it on the Setup page first")
        return brand
    if name == "report_type" and value is not None and value not in registry.report_types:
        raise ValueError(f"unknown report type {value!r}")
    if name == "period_grain" and value is not None and str(value) not in ("month", "quarter", "r3m", "r6m", "r12m", "ytd"):
        raise ValueError(f"unknown period grain {value!r}")
    return _typed(name, value)


def confirm_metadata(store: Store, registry: Registry, settings: Settings, doc_id: str, fields: dict[str, Any],
                     actor: str) -> Document:
    """A person confirms (or corrects) metadata: READY, family/version recomputed, ReviewEvent written."""
    doc = store.get(Document, doc_id=doc_id)
    if doc is None:
        raise KeyError(f"unknown document {doc_id}")
    if doc.status in (DocumentStatus.UNSUPPORTED_FORMAT, DocumentStatus.ENCRYPTED):
        raise ValueError(f"document {doc_id} is {doc.status.value}; {doc.error or 'it cannot be processed'}")
    unknown = set(fields or {}) - set(METADATA_FIELDS)
    if unknown:
        raise ValueError(f"unknown metadata fields: {sorted(unknown)}")
    if not actor:
        raise ValueError("actor is required")
    fields = dict(fields or {})
    previous = {f: getattr(doc, f) for f in DOC_FIELDS} | {"period_grain": document_grain(doc, registry)}
    proposals = dict(doc.metadata_proposals or {})
    for name in DOC_FIELDS:
        if name in fields:
            setattr(doc, name, _normalize_confirmed(registry, name, fields[name]))
        elif getattr(doc, name) is None and (proposals.get(name) or {}).get("value") is not None:
            setattr(doc, name, _normalize_confirmed(registry, name, proposals[name]["value"]))
    grain = _normalize_confirmed(registry, "period_grain", fields.get("period_grain"))
    if doc.period_end is not None:
        doc.period_end = snap_month_end(doc.period_end)
        explicit_end = "period_end" in fields
        fs = int(registry.brands.get(doc.brand or "", {}).get("fiscal_year_start_month", 1))
        if grain is None:
            proposed = (proposals.get("period_grain") or {}).get("value")
            grain = proposed or str(registry.report_types.get(doc.report_type or "", {}).get("default_grain", "month"))
        if "period_start" not in fields and (doc.period_start is None or explicit_end or "period_grain" in fields):
            doc.period_start = period_bounds(doc.period_end, grain, fiscal_year_start_month=fs)[0]
        if "period_label" not in fields and (doc.period_label is None or explicit_end or "period_grain" in fields):
            doc.period_label = doc.period_end.strftime("%B %Y") if grain == "month" else (
                f"{grain} ending {doc.period_end:%B %Y}")
    missing = [f for f in _required_fields(registry) if getattr(doc, f, None) in (None, "")]
    if missing:
        raise ValueError(f"cannot confirm: required fields still missing: {missing}")
    now = utcnow()
    final = {f: getattr(doc, f) for f in DOC_FIELDS} | {"period_grain": grain}
    for name, value in final.items():
        if value is not None:
            proposals[name] = {**_proposal(value, 1.0, f"confirmed:{actor}"),
                               "alternatives": [] if name not in proposals else
                               [{"value": proposals[name].get("value"), "source": proposals[name].get("source")}]}
    doc.metadata_proposals = proposals
    doc.confirmed_by, doc.confirmed_at = actor, now
    doc.needs_confirmation = []
    doc.updated_at = now
    not_extracted = doc.status == DocumentStatus.REGISTERED or (
        doc.status == DocumentStatus.FAILED and doc.failed_stage in ("extract", "register"))
    if not not_extracted:
        doc.status = DocumentStatus.READY
        doc.superseded_by_doc_id = None
        doc.failed_stage, doc.error = None, None
    changed = _assign_family(store, doc)
    event = ReviewEvent(
        event_id=stable_id("confirmed_metadata", doc_id, actor, now.isoformat()), result_id=None, doc_id=doc_id,
        event="confirmed_metadata", actor=actor, event_at=now,
        detail=json_safe({"fields": {k: _jsonable(v) for k, v in fields.items()},
                          "previous": {k: _jsonable(v) for k, v in previous.items()},
                          "report_family": doc.report_family, "version": doc.version}))
    with store.transaction():
        store.upsert([doc, *changed])
        _retire_results(store, _superseded_ids(doc, changed))
        store.insert_new([event])
    return doc


def _jsonable(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (date, datetime)) else value
