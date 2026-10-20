from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from hashlib import file_digest
from math import isfinite
from pathlib import Path
import json
import re

from ..schema import Target

NUMBER = r"[+\-−]?\s*[$€£]?\s*\(?\s*[+\-−]?\s*[$€£]?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*[KMBkmb]?\s*%?\s*\)?"


def parse_number(raw: str, *, scale: float = 1.0, percent: str = "fraction") -> float:
    """Parse 1.2M, (1,234), -$20, or 12.5%; never guess OCR digit corrections."""
    text = re.sub(r"\s+", "", raw).replace("−", "-")
    match = re.fullmatch(
        r"(?P<sign>[+-]?)(?:[$€£])?(?P<open>\()?"
        r"(?P<inner>[+-]?)(?:[$€£])?"
        r"(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
        r"(?P<suffix>[KMBkmb]?)(?P<pct>%?)(?P<close>\)?)", text)
    if not match or bool(match["open"]) != bool(match["close"]):
        raise ValueError(f"unrecognized number: {raw!r}")
    if percent not in {"fraction", "points"} or not isfinite(scale) or scale <= 0:
        raise ValueError("scale must be positive and finite; percent must be 'fraction' or 'points'")
    if (match["sign"] and match["inner"]) or (match["open"] and (match["sign"] or match["inner"])):
        raise ValueError("ambiguous numeric sign")
    value = float(match["number"].replace(",", ""))
    value *= {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[match["suffix"].upper()]
    if match["open"] or "-" in (match["sign"], match["inner"]):
        value = -value
    if match["pct"] and percent == "fraction":
        value /= 100
    value *= scale
    if not isfinite(value):
        raise ValueError("extracted value must be finite")
    return value


@dataclass
class TextResult:
    text: str
    confidence: float = 1.0
    tokens: list[tuple[int, int, float]] = field(default_factory=list)

    def confidence_at(self, span: tuple[int, int]) -> float:
        scores = [c for start, end, c in self.tokens if start < span[1] and end > span[0]]
        return min(scores) if scores else self.confidence


@dataclass
class ExtractionResult:
    targets: list[Target] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)

    def require_complete(self):
        if self.issues:
            raise ExtractionError(self)
        return self.targets

    def write(self, out_dir: str | Path):
        from .csv_targets import write_targets
        from ..storage import local_path
        out = local_path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        write_targets(self.targets, out / "targets.csv")
        (out / "extraction.json").write_text(json.dumps({
            "complete": not self.issues, "targets": [asdict(t) for t in self.targets],
            "evidence": self.evidence, "issues": self.issues,
        }, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")


class ExtractionError(ValueError):
    def __init__(self, result: ExtractionResult):
        self.result = result
        first = result.issues[0]
        super().__init__(f"PDF extraction needs review ({len(result.issues)} issue(s)): "
                         f"{first.get('metric', '')} page {first.get('page', '')}: {first['reason']}")


def tesseract_read(image, *, lang="eng", psm=6, timeout=60) -> TextResult:
    try:
        import pytesseract
        from PIL import ImageOps
    except ImportError as exc:
        raise RuntimeError('Install reconloop[ocr] and the Tesseract executable for OCR.') from exc
    try:
        gray = ImageOps.autocontrast(ImageOps.grayscale(image))
        try:
            data = pytesseract.image_to_data(gray, lang=lang, config=f"--psm {int(psm)}",
                                            output_type=pytesseract.Output.DICT, timeout=timeout)
        finally:
            gray.close()
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError("Tesseract is not installed or not on PATH; install it to use OCR.") from exc
    chunks, tokens, length, previous = [], [], 0, None
    for i, word in enumerate(data["text"]):
        if not word.strip():
            continue
        line = tuple(data[k][i] for k in ("block_num", "par_num", "line_num"))
        separator = ("\n" if line != previous else " ") if chunks else ""
        chunks.append(separator + word)
        length += len(separator)
        confidence = max(0.0, min(1.0, float(data["conf"][i]) / 100))
        tokens.append((length, length + len(word), confidence))
        length += len(word)
        previous = line
    return TextResult("".join(chunks), min((t[2] for t in tokens), default=0.0), tokens)


def _region(page, bbox):
    if bbox is None:
        return page
    if (len(bbox) != 4 or not all(isfinite(v) and 0 <= v <= 1 for v in bbox)
            or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
        raise ValueError("bbox must be [left, top, right, bottom] fractions between 0 and 1")
    x0, top, x1, bottom = page.bbox
    return page.crop((x0 + bbox[0] * (x1 - x0), top + bbox[1] * (bottom - top),
                      x0 + bbox[2] * (x1 - x0), top + bbox[3] * (bottom - top)))


class _MissingFigure(ValueError):
    ...


def _one_value(text, pattern):
    labels = list(re.finditer(pattern["label"], text, re.I))
    if not labels:
        raise _MissingFigure("label not found")
    if len(labels) != 1:
        raise ValueError(f"expected one label, found {len(labels)}; narrow the tile bbox")
    if pattern.get("value_pattern"):
        matches = list(re.finditer(pattern["value_pattern"], text, re.I))
        if not matches:
            raise _MissingFigure("value_pattern found no figure")
        if len(matches) != 1 or "value" not in matches[0].re.groupindex:
            raise ValueError("value_pattern must match once and contain a named 'value' group")
        match = matches[0]
    else:
        expression = re.compile(r"[\s:=]{0,40}(?P<value>" + NUMBER + r")(?![\w.,%+−-])")
        match = expression.match(text, labels[0].end())
        if not match:
            raise _MissingFigure("no value immediately after label; set value_pattern or a tighter bbox")
    return match.group("value").strip(), match.span("value"), labels[0].span()


def _one_date(text, pattern):
    matches = list(re.finditer(pattern["as_of"], text))
    if not matches:
        raise _MissingFigure("as_of pattern found no date")
    if len(matches) != 1 or not matches[0].lastindex:
        raise ValueError("as_of pattern must match exactly one date in a capture group")
    match = matches[0]
    raw = match.group("date") if "date" in match.re.groupindex else match.group(1)
    value = datetime.strptime(raw, pattern.get("date_format", "%Y-%m-%d")).date().isoformat()
    return value, match.span("date" if "date" in match.re.groupindex else 1), raw


def extract_pdf(pdf_path: str | Path, report: str, patterns: list[dict], *,
                ocr: str = "auto", dpi: int = 300, min_confidence: float = 0.8,
                split: str = "fit", lang: str = "eng", psm: int = 6, timeout: int = 60,
                strict: bool = True, ocr_reader=None, evidence_dir: str | Path | None = None) -> ExtractionResult:
    """Extract every configured metric on every selected page, preferring digital text."""
    if ocr not in {"auto", "always", "never"}:
        raise ValueError("ocr must be auto, always, or never")
    if not patterns or not 72 <= dpi <= 600 or not 0 <= min_confidence <= 1:
        raise ValueError("provide patterns, dpi between 72 and 600, and confidence between 0 and 1")
    if psm not in range(3, 14) or timeout <= 0:
        raise ValueError("psm must be 3..13 and timeout must be positive")
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError('Install reconloop[pdf] for PDF extraction, or reconloop[ocr] for OCR.') from exc
    source = Path(pdf_path)
    with source.open("rb") as stream:
        source_id = file_digest(stream, "sha256").hexdigest()[:12]
    result = ExtractionResult()
    reader = ocr_reader or (lambda image: tesseract_read(image, lang=lang, psm=psm, timeout=timeout))
    with pdfplumber.open(str(source)) as pdf:
        for index, pattern in enumerate(patterns):
            if not pattern.get("metric") or not pattern.get("label"):
                raise ValueError("each PDF pattern needs a metric and label")
            if not pattern.get("as_of") and not pattern.get("as_of_value"):
                raise ValueError("each PDF pattern needs as_of (regex) or as_of_value (ISO date)")
            pages = pattern.get("pages", list(range(1, len(pdf.pages) + 1)))
            if not pages or len(set(pages)) != len(pages) or any(type(p) is not int or not 1 <= p <= len(pdf.pages) for p in pages):
                raise ValueError("pages must be distinct 1-based page numbers in this PDF")
        for pno, page in enumerate(pdf.pages, 1):
            cache = {}

            def read(bbox, method):
                key = (tuple(bbox) if bbox else None, method)
                if key not in cache:
                    region = _region(page, bbox)
                    crop_file = ""
                    if method == "text":
                        value = TextResult(region.extract_text() or "")
                    else:
                        if page.width * page.height * (dpi / 72) ** 2 > 50_000_000:
                            raise ValueError("page exceeds 50 megapixels at this DPI; lower dpi")
                        image = region.to_image(resolution=dpi, antialias=True).original
                        try:
                            if evidence_dir:
                                out = Path(evidence_dir)
                                out.mkdir(parents=True, exist_ok=True)
                                crop_file = f"{source_id}-p{pno}-crop{len(cache)}.png"
                                image.save(out / crop_file)
                            value = reader(image)
                        finally:
                            image.close()
                    cache[key] = value, crop_file
                return cache[key]

            for index, pattern in enumerate(patterns):
                if pno not in pattern.get("pages", range(1, len(pdf.pages) + 1)):
                    continue
                evidence = dict(source_file=source.name, page=pno, metric=pattern["metric"],
                                bbox=pattern.get("bbox"), date_bbox=pattern.get("date_bbox"))
                try:
                    method = "ocr" if ocr == "always" else "text"
                    text, crop = read(pattern.get("bbox"), method)
                    evidence.update(method=method, crop=crop)
                    try:
                        raw, span, label_span = _one_value(text.text, pattern)
                    except _MissingFigure:
                        if ocr != "auto":
                            raise
                        method = "ocr"
                        text, crop = read(pattern.get("bbox"), method)
                        evidence.update(method=method, crop=crop)
                        raw, span, label_span = _one_value(text.text, pattern)
                    evidence.update(raw_value=raw)
                    confidence = min(text.confidence_at(span), text.confidence_at(label_span))
                    date_method, date_crop = "fixed", ""
                    raw_date = pattern.get("as_of_value", "")
                    as_of = raw_date
                    if not as_of:
                        date_method = "ocr" if ocr == "always" else "text"
                        date_text, date_crop = read(pattern.get("date_bbox"), date_method)
                        evidence.update(date_method=date_method, date_crop=date_crop)
                        try:
                            as_of, date_span, raw_date = _one_date(date_text.text, pattern)
                        except _MissingFigure:
                            if ocr != "auto":
                                raise
                            date_method = "ocr"
                            date_text, date_crop = read(pattern.get("date_bbox"), date_method)
                            evidence.update(date_method=date_method, date_crop=date_crop)
                            as_of, date_span, raw_date = _one_date(date_text.text, pattern)
                        confidence = min(confidence, date_text.confidence_at(date_span))
                    value = parse_number(raw, scale=pattern.get("scale", 1.0), percent=pattern.get("percent", "fraction"))
                    evidence.update(method=method, date_method=date_method, raw_value=raw, raw_date=raw_date,
                                    value=value, as_of=as_of, confidence=confidence, crop=crop, date_crop=date_crop)
                    if not isfinite(confidence) or confidence < min_confidence:
                        raise ValueError(f"confidence {confidence:.0%} below threshold {min_confidence:.0%}")
                    target = Target(target_id=f"{report}-{source_id}-p{pno}-m{index}", report=report,
                                    metric=pattern["metric"], as_of=as_of, value=value,
                                    unit=pattern.get("unit", ""), stated_rule=pattern.get("stated_rule", ""),
                                    source_file=source.name, page=str(pno), split=pattern.get("split", split),
                                    confidence=confidence, extraction_method=method,
                                    source_region=json.dumps(pattern.get("bbox")) if pattern.get("bbox") else "")
                    result.targets.append(target)
                    evidence.update(target_id=target.target_id, status="accepted")
                except (ValueError, RuntimeError) as exc:
                    evidence.update(status="needs_review", reason=str(exc))
                    result.issues.append(dict(evidence))
                result.evidence.append(evidence)
    if strict:
        result.require_complete()
    return result
