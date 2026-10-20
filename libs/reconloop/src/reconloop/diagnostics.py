from math import isfinite
from statistics import mean, pstdev


def constant_ratio(rows, *, min_periods=3, max_cv=0.01, tolerance=0.005):
    """Describe computed/target ratios; a stable ratio does not establish its cause."""
    if type(min_periods) is not int or min_periods < 2 or not isfinite(max_cv) or max_cv < 0:
        raise ValueError("ratio diagnostics need >=2 periods and a finite nonnegative CV limit")
    usable, excluded = [], []
    for row in rows:
        gold, computed = row["gold"], row["computed"]
        ratio = computed / gold if gold else float("nan")
        if not isfinite(ratio) or ratio <= 0:
            excluded.append(row["target_id"])
        else:
            usable.append(dict(target_id=row["target_id"], as_of=row["as_of"], ratio=ratio))
    result = dict(status="insufficient", periods=len({r["as_of"] for r in usable}),
                  ratios=usable, excluded=excluded, interpretation="No conversion is applied.")
    if result["periods"] < min_periods:
        return result
    scale = max(r["ratio"] for r in usable)
    normalized = [r["ratio"] / scale for r in usable]
    center = mean(normalized)
    factor = center * scale
    cv = pstdev(normalized) / center
    stable = cv <= max_cv and max(abs(x - center) / center for x in normalized) <= 3 * max_cv
    result.update(status="near_identity" if stable and abs(factor - 1) <= tolerance else "constant" if stable else "variable",
                  computed_over_target=factor, target_over_computed=1 / factor, coefficient_of_variation=cv)
    if result["status"] == "constant":
        result["interpretation"] = ("A stable multiplicative gap is consistent with a unit/measure, grain, coverage or "
                                    "restatement difference. Verify an external definition or conversion; the ratio alone "
                                    "does not establish the cause and is not permission to rescale targets.")
    return result


def diagnose_record(record):
    final = next(i for i in reversed(record.iterations) if i.accepted is not False)
    fit = [r for r in final.per_target if r["split"] == "fit"]
    hold = [r for r in final.per_target if r["split"] == "holdout"]
    diagnostic = dict(fit_ratio=constant_ratio(fit, tolerance=record.tolerance), components=[],
                      boundary="post_close_only; do not feed holdout evidence back into tuning")
    ratio = diagnostic["fit_ratio"]
    if ratio["status"] == "constant" and hold:
        factor = ratio["computed_over_target"]
        checks = [dict(target_id=r["target_id"], as_of=r["as_of"],
                       relative_difference=abs(r["computed"] / r["gold"] / factor - 1) if r["gold"] else None) for r in hold]
        diagnostic["holdout_factor_check"] = dict(rows=checks, consistent=all(
            r["relative_difference"] is not None and isfinite(r["relative_difference"]) and r["relative_difference"] <= .01
            for r in checks))
    initial = {r["target_id"]: r for r in record.iterations[0].per_target}
    for row in final.per_target:
        if not row.get("components"):
            continue
        before, after = initial[row["target_id"]].get("components", {}), row["components"]
        diagnostic["components"].append(dict(target_id=row["target_id"], as_of=row["as_of"], split=row["split"],
            **{name: dict(before=before.get(name), after=after[name],
                         factor=after[name] / before[name] if before.get(name) else None)
               for name in ("numerator", "denominator")}))
    record.diagnostics = diagnostic
    return diagnostic


def compare_breakdowns(spec, references, runner, *, parameter, choices, max_queries=1000):
    """Compare declared population options against their own independent targets."""
    from .compare import evaluate
    if type(max_queries) is not int or max_queries < 1:
        raise ValueError("breakdown max_queries must be a positive integer")
    if parameter not in spec.params or not references or set(references) - spec.params[parameter].options.keys():
        raise ValueError("breakdown options must belong to a declared parameter")
    queries = sum(len(ts) for ts in references.values()) * (len(spec.checks) + (2 if spec.ratio else 1))
    if queries > max_queries:
        raise ValueError("breakdown query budget exceeded")
    evidence = []
    for option, ts in references.items():
        if not ts or len({(t.report, t.metric) for t in ts}) != 1:
            raise ValueError("each breakdown needs targets for one report/metric")
        if any(t.unit and spec.unit and t.unit != spec.unit for t in ts):
            raise ValueError("breakdown units must agree with the source metric")
        params = spec.initial_choices(choices) | {parameter: option}
        rows = evaluate(runner, spec, params, ts)
        evidence.append(dict(option=option, reference_metric=ts[0].metric, params=params, rows=rows,
                             fit_ratio=constant_ratio([r for r in rows if r["split"] == "fit"], tolerance=spec.tolerance)))
    return evidence


def post_close(record, spec, targets, runner, config=None, *, reference_targets=None):
    """Optionally compare declared vintages after close; hold all other choices fixed."""
    diagnostic = diagnose_record(record)
    declaration = (config or {}).get("vintages", {}).get(record.metric)
    if declaration and record.status != "reconciled":
        from .revisions import compare_vintages
        try:
            diagnostic["vintages"] = compare_vintages(spec, targets, runner, choices=record.final_params, **declaration)
        except Exception as exc:
            diagnostic["vintage_error"] = str(exc)
    breakdown = (config or {}).get("breakdowns", {}).get(record.metric)
    if breakdown and record.status != "reconciled":
        try:
            references = {option: [t for t in (reference_targets or targets) if t.report == record.report and t.metric == metric]
                          for option, metric in breakdown["metrics"].items()}
            diagnostic["breakdowns"] = compare_breakdowns(spec, references, runner, choices=record.final_params,
                parameter=breakdown["parameter"], max_queries=breakdown.get("max_queries", 1000))
        except Exception as exc:
            diagnostic["breakdown_error"] = str(exc)
    return diagnostic
