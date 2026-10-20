from math import isfinite

from .compare import evaluate, rel_var, split_targets


def compare_vintages(spec, targets, runner, *, parameter, before, after, choices=None,
                     absolute_tolerance=0.0):
    """Hold the metric definition fixed and compare two declared vintage options."""
    split_targets(targets, spec.name)
    p = spec.params.get(parameter)
    if p is None or p.hypothesis not in {"data_vintage", "alignment_vintage", "restatement", "late_data"}:
        raise ValueError("revision parameter must declare a vintage, restatement, or late_data hypothesis")
    if before == after or before not in p.options or after not in p.options:
        raise ValueError("before and after must be distinct declared options")
    if not isfinite(absolute_tolerance) or absolute_tolerance < 0:
        raise ValueError("absolute_tolerance must be finite and nonnegative")
    base = spec.initial_choices(choices)
    old_params, new_params = base | {parameter: before}, base | {parameter: after}
    old_rows = evaluate(runner, spec, old_params, targets)
    new_rows = evaluate(runner, spec, new_params, targets)
    evidence = []
    for t, old, new in zip(targets, old_rows, new_rows):
        delta = new["computed"] - old["computed"]
        relative = rel_var(new["computed"], old["computed"])
        components = {}
        for key in ("numerator", "denominator"):
            if key in old.get("components", {}):
                previous, current = old["components"][key], new["components"][key]
                components[key] = dict(before=previous, after=current, delta=current - previous)
        changed = abs(delta) > absolute_tolerance or any(abs(c["delta"]) > absolute_tolerance for c in components.values())
        evidence.append(dict(target_id=t.target_id, report=t.report, metric=t.metric, as_of=t.as_of,
                             split=t.split, unit=t.unit, parameter=parameter, before_option=before, after_option=after,
                             before=old["computed"], after=new["computed"], delta=delta,
                             relative_change=relative if isfinite(relative) else None,
                             status="revised" if changed else "unchanged", components=components,
                             before_params=old_params, after_params=new_params,
                             before_queries=spec.render_components(old_params, t.as_of),
                             after_queries=spec.render_components(new_params, t.as_of)))
    return evidence


def write_revisions(evidence, out_dir):
    import json
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "revisions.json").write_text(json.dumps(evidence, indent=2, allow_nan=False), encoding="utf-8")
    lines = ["# Source vintage comparison", "",
             "The definition is held fixed except for the named vintage parameter. Differences do not establish a cause.", "",
             "| Report | Metric | Period | Before | After | Delta | Status |", "|---|---|---|---|---|---|---|"]
    for e in evidence:
        lines.append(f"| {e['report']} | {e['metric']} | {e['as_of']} | {e['before']:.8g} | {e['after']:.8g} | {e['delta']:+.8g} | {e['status']} |")
    (out / "revisions.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def revisions_config(path, *, metric, parameter, before, after, reports=None, records_dir=None, out_dir=None):
    import json
    from pathlib import Path
    from .batch import select_pairs
    from .config import load_config, build_runner
    from .record import record_filename
    cfg, specs, targets = load_config(path)
    groups = select_pairs(specs, targets, reports=reports, metrics=[metric])
    runner = build_runner(cfg, path)
    evidence = []
    for report, name, ts in groups:
        choices = cfg.get("start", {}).get(name)
        if records_dir:
            record = json.loads((Path(records_dir) / record_filename(report, name)).read_text(encoding="utf-8"))
            if (record["report"], record["metric"]) != (report, name):
                raise ValueError("record identity does not match the selected report and metric")
            choices = record["final_params"]
        evidence.extend(compare_vintages(specs[name], ts, runner, parameter=parameter, before=before,
                                         after=after, choices=choices))
    if out_dir:
        write_revisions(evidence, out_dir)
    return evidence
