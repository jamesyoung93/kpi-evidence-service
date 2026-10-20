from __future__ import annotations

from statistics import mean, pstdev
from math import isfinite

from .schema import Target, Residual
from .hypotheses import RESIDUAL_HINTS


def rel_var(computed: float, gold: float) -> float:
    if not isfinite(computed) or not isfinite(gold):
        raise ValueError("computed and target values must be finite")
    if gold == 0:
        return 0.0 if computed == 0 else float("inf")
    return (computed - gold) / abs(gold)


def split_targets(targets: list[Target], metric: str):
    if not targets or not any(t.split == "fit" for t in targets):
        raise ValueError("at least one fit target is required")
    if any(t.metric != metric or t.report != targets[0].report for t in targets):
        raise ValueError("targets must belong to one report and the requested metric")
    if len({t.target_id for t in targets}) != len(targets):
        raise ValueError("target IDs must be unique within a reconciliation")
    return ([t for t in targets if t.split == "fit"],
            [t for t in targets if t.split == "holdout"])


def score(rows: list[dict]) -> float:
    """Mean absolute relative variance across rows (each row has 'rel')."""
    return mean(abs(r["rel"]) for r in rows) if rows else 0.0


def evaluate(runner, spec, params: dict[str, str], targets: list[Target]) -> list[dict]:
    rows = []
    for t in targets:
        v, components = spec.compute(runner, params, t.as_of)
        rows.append({"target_id": t.target_id, "as_of": t.as_of, "split": t.split,
                     "gold": t.value, "computed": v, "rel": rel_var(v, t.value),
                     "unit": t.unit, "source_file": t.source_file, "page": t.page,
                     "confidence": t.confidence, "extraction_method": t.extraction_method,
                     "source_region": t.source_region,
                     "value_lower": t.value_lower, "value_upper": t.value_upper,
                     **({"components": components} if components else {})})
    return rows


def classify_residual(rows: list[dict], tolerance: float) -> str:
    """Describe the shape of what is left so the judge (or a human) can reason about it."""
    rels = [r["rel"] for r in rows]
    if any(not isfinite(x) for x in rels):
        return "noisy"
    if not rels or mean(abs(x) for x in rels) <= tolerance:
        return "within_tolerance"
    m, s = mean(rels), pstdev(rels)
    ordered = [r["rel"] for r in sorted(rows, key=lambda r: r["as_of"])]
    trend = ordered[-1] - ordered[0] if len(ordered) > 1 else 0.0
    if s < 0.25 * abs(m):
        return "level_shift"
    if abs(trend) > 2 * s:
        return "drift"
    return "noisy"


def summarize_result(final, tolerance: float) -> tuple[str, Residual]:
    pattern = classify_residual([r for r in final.per_target if r["split"] == "fit"], tolerance)
    causes = RESIDUAL_HINTS.get(pattern, [])
    if pattern == "within_tolerance":
        status, note = "reconciled", "Within tolerance."
    else:
        status = "explained_residual" if pattern == "level_shift" else "unresolved"
        note = (f"Remaining gap is a {pattern.replace('_', ' ')}. "
                f"Possible causes to review: {', '.join(causes)}.")
    if final.holdout_variance is not None and final.holdout_variance > tolerance:
        status = "unresolved"
        note += " Holdout is outside tolerance; review before trusting this definition."
    return status, Residual(final.fit_variance, final.holdout_variance, pattern, causes, note)
