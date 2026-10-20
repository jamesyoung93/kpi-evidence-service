from copy import deepcopy
from hashlib import file_digest, sha256
from io import BytesIO
import json
from math import ceil, floor, isfinite
from pathlib import Path

from .pdf import ExtractionResult
from ..schema import Target


def measure_bar(image, *, bbox, axis, color, color_tolerance=24, min_coverage=0.6, pixel_uncertainty=2.0):
    """Measure one strip, using two verified axis ticks in full-image fractions."""
    if axis.get("scale", "linear") != "linear" or axis.get("orientation", "vertical") not in {"vertical", "horizontal"}:
        raise ValueError("only calibrated linear horizontal/vertical bars are supported")
    if (len(bbox) != 4 or not all(isfinite(v) and 0 <= v <= 1 for v in bbox)
            or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
        raise ValueError("bbox must be normalized left/top/right/bottom coordinates")
    positions, values = axis["positions"], axis["values"]
    if (len(positions) != 2 or len(values) != 2 or not all(isfinite(v) for v in positions + values)
            or not all(0 <= p <= 1 for p in positions) or positions[0] == positions[1] or values[0] == values[1]):
        raise ValueError("provide two distinct calibrated ticks with finite values")
    if len(color) != 3 or any(type(c) is not int or not 0 <= c <= 255 for c in color):
        raise ValueError("bar color must be three RGB integers")
    if not 0 <= color_tolerance <= 100 or not .1 <= min_coverage <= 1 or not isfinite(pixel_uncertainty) or pixel_uncertainty < 1:
        raise ValueError("invalid color tolerance, coverage or pixel uncertainty (minimum 1 pixel)")
    vertical = axis.get("orientation", "vertical") == "vertical"
    width, height = image.size
    size = height if vertical else width
    slope = (values[1] - values[0]) / ((positions[1] - positions[0]) * size)
    intercept = values[0] - slope * positions[0] * size
    baseline_value = axis.get("baseline", 0.0)
    if not isfinite(baseline_value):
        raise ValueError("baseline must be finite")
    baseline = (baseline_value - intercept) / slope
    left, top, right, bottom = floor(bbox[0]*width), floor(bbox[1]*height), ceil(bbox[2]*width), ceil(bbox[3]*height)
    start, end = (top, bottom) if vertical else (left, right)
    if not start - pixel_uncertainty <= baseline <= end + pixel_uncertainty:
        raise ValueError("bar strip does not include the calibrated baseline")
    with image.crop((left, top, right, bottom)).convert("RGB") as crop:
        pixels = crop.load()
        length, breadth = (crop.height, crop.width) if vertical else (crop.width, crop.height)
        if breadth < 2:
            raise ValueError("bar strip is too narrow to measure")
        hits = []
        for p in range(length):
            count = sum(max(abs(channel - expected) for channel, expected in zip(pixels[q, p] if vertical else pixels[p, q], color))
                        <= color_tolerance for q in range(breadth))
            if count / breadth >= min_coverage:
                hits.append(p + start)
    if not hits:
        raise ValueError("no bar of the configured color found; a missing bar is not inferred as zero")
    if any(b - a != 1 for a, b in zip(hits, hits[1:])):
        raise ValueError("bar is fragmented or multiple bars/marks overlap the strip")
    low, high = hits[0], hits[-1] + 1
    if min(abs(low - baseline), abs(high - baseline)) > pixel_uncertainty:
        raise ValueError("colored region is not anchored to the calibrated baseline")
    endpoint = low if abs(high - baseline) <= abs(low - baseline) else high
    if endpoint <= start + 1 or endpoint >= end - 1:
        raise ValueError("bar endpoint touches the crop boundary and may be clipped")
    if not min(positions) * size <= endpoint <= max(positions) * size:
        raise ValueError("bar endpoint lies outside the calibrated tick range")
    value = slope * endpoint + intercept
    error = abs(slope) * pixel_uncertainty
    return dict(value=value, value_lower=value-error, value_upper=value+error,
                endpoint_pixel=endpoint, baseline_pixel=baseline, pixel_uncertainty=pixel_uncertainty,
                units_per_pixel=abs(slope), image_size=[width, height], bbox=list(bbox), axis=deepcopy(axis), color=list(color))


def extract_bars(pdf_path, report, patterns, *, dpi=200, review=None, strict=True, evidence_dir=None):
    """Infer configured bar estimates; only reviewed estimates become targets."""
    import pdfplumber
    from ..storage import local_path
    source = local_path(pdf_path)
    if not patterns or not 72 <= dpi <= 600:
        raise ValueError("provide bar patterns and DPI between 72 and 600")
    with source.open("rb") as stream:
        source_hash = file_digest(stream, "sha256").hexdigest()
    out = local_path(evidence_dir) if evidence_dir else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    result = ExtractionResult()
    with pdfplumber.open(str(source)) as pdf:
        for index, pattern in enumerate(patterns):
            page = pattern.get("page", 1)
            if type(page) is not int or not 1 <= page <= len(pdf.pages):
                raise ValueError("each bar needs a valid 1-based page")
            for required in ("metric", "as_of", "bbox", "axis", "color"):
                if required not in pattern:
                    raise ValueError(f"bar pattern requires {required}")
        for page_no, page in enumerate(pdf.pages, 1):
            entries = [(i, p) for i, p in enumerate(patterns) if p.get("page", 1) == page_no]
            if not entries:
                continue
            if page.width * page.height * (dpi / 72)**2 > 50_000_000:
                raise ValueError("rendered page exceeds 50 megapixels")
            with page.to_image(resolution=dpi, antialias=True).original as image:
                with BytesIO() as buffer:
                    image.save(buffer, format="PNG")
                    image_hash = sha256(buffer.getvalue()).hexdigest()
                page_file = f"{source_hash[:12]}-p{page_no}-chart.png"
                if out:
                    image.save(out / page_file)
                for index, pattern in entries:
                    evidence = dict(metric=pattern["metric"], page=page_no, source_file=source.name,
                                    source_sha256=source_hash, page_image=page_file if out else "",
                                    page_image_sha256=image_hash, method="chart_estimate", reviewed=False)
                    try:
                        estimate = measure_bar(image, **{key: pattern[key] for key in
                            ("bbox", "axis", "color", "color_tolerance", "min_coverage", "pixel_uncertainty") if key in pattern})
                        evidence.update(estimate, as_of=pattern["as_of"], unit=pattern.get("unit", ""))
                        target = Target(target_id=f"{report}-{source_hash[:12]}-p{page_no}-b{index}", report=report,
                                        metric=pattern["metric"], as_of=pattern["as_of"], value=estimate["value"],
                                        value_lower=estimate["value_lower"], value_upper=estimate["value_upper"],
                                        unit=pattern.get("unit", ""), split=pattern.get("split", "fit"),
                                        stated_rule=pattern.get("stated_rule", ""), source_file=source.name,
                                        page=str(page_no), extraction_method="chart_estimate_reviewed",
                                        source_region=json.dumps(pattern["bbox"]))
                        evidence["target_id"] = target.target_id
                        if review is None or review(image, deepcopy(evidence)) is not True:
                            raise ValueError("chart estimate requires independent review of the page and calibration")
                        evidence.update(reviewed=True, status="accepted")
                        result.targets.append(target)
                    except (ValueError, RuntimeError) as exc:
                        evidence.update(status="needs_review", reason=str(exc))
                        result.issues.append(dict(evidence))
                    result.evidence.append(evidence)
    if strict:
        result.require_complete()
    return result
