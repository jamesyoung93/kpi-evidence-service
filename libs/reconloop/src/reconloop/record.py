from __future__ import annotations

import json
from math import isfinite
from pathlib import Path
from urllib.parse import quote

from .schema import ReconRecord, CompoundChange, change_items


def record_filename(report: str, metric: str) -> str:
    def component(value):
        return quote(value, safe="").replace("_", "%5F").replace(".", "%2E")
    if all(v and v.isascii() and v.replace("_", "").isalnum() and "__" not in v
           and not v.startswith("_") and not v.endswith("_") for v in (report, metric)):
        return f"{report}__{metric}.json"
    return f"encoded-{component(report)}__{component(metric)}.json"


def _json_safe(value):
    if isinstance(value, float) and not isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def write(records: list[ReconRecord], out_dir: str | Path) -> Path:
    from .storage import local_path
    out = local_path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for r in records:
        (out / record_filename(r.report, r.metric)).write_text(
            json.dumps(_json_safe(r.to_dict()), indent=2, allow_nan=False), encoding="utf-8")
    md = out / "report.md"
    md.write_text(to_markdown(records), encoding="utf-8")
    return md


def to_markdown(records: list[ReconRecord]) -> str:
    lines = ["# Reconciliation report", ""]
    for report in sorted({r.report for r in records}):
        members = [r for r in records if r.report == report]
        counts = {status: sum(r.status == status for r in members)
                  for status in ("reconciled", "explained_residual", "unresolved")}
        lines.append(f"**{report}**: {len(members)} metrics; " + ", ".join(f"{n} {s}" for s, n in counts.items()) + ".")
    lines.append("")
    coupled = {(r.report, r.coupling["group"]): r.coupling for r in records if r.coupling}
    for (report, name), audit in coupled.items():
        lines += [f"## Coupled group: {report} / {name}", "",
                  f"Search: {len(audit['fit_candidates'])} fit candidates; {audit['selection']}.",
                  f"Joint proposal accepted: {audit['accepted']}. {audit['reject_reason']}", "",
                  "| Identity | Period | Split | Left | Converted right | Passed |", "|---|---|---|---|---|---|"]
        for e in audit["final_identities"]:
            lines.append(f"| {e['name']} | {e['as_of']} | {e['split']} | {e['left_value']:.8g} | {e['right_value']:.8g} | {e['passed']} |")
        lines.append("")
    lines += ["| Report | Metric | Status | Fit variance | Holdout | Changes |", "|---|---|---|---|---|---|"]
    for r in records:
        n = sum(1 for i in r.iterations if i.accepted)
        h = f"{r.residual.holdout_variance:.2%}" if r.residual.holdout_variance is not None else "–"
        lines.append(f"| {r.report} | {r.metric} | {r.status} | {r.residual.fit_variance:.2%} | {h} | {n} |")
    for r in records:
        lines += ["", f"## {r.report} · {r.metric}", "",
                  f"Status: **{r.status}**. {r.residual.note}", "",
                  f"Start: `{r.start_params}`  ", f"Final: `{r.final_params}`", "", "| Step | Change | Reason | Fit | Holdout | Outcome |",
                  "|---|---|---|---|---|---|"]
        for i in r.iterations:
            if i.change is None:
                label = "start" if i.step == 0 else "unchanged member in joint proposal"
                outcome = "–" if i.step == 0 else ("accepted" if i.accepted else f"rejected: {i.reject_reason}")
                lines.append(f"| {i.step} | {label} | – | {i.fit_variance:.2%} | {fmt(i.holdout_variance)} | {outcome} |")
                continue
            outcome = "accepted" if i.accepted else f"rejected: {i.reject_reason}"
            changes = "; ".join(f"{c.param}: {c.old} → {c.new}" for c in change_items(i.change))
            reasons = "; ".join(f"{c.hypothesis}: {c.rationale}" for c in change_items(i.change))
            if isinstance(i.change, CompoundChange):
                reasons += f"; Joint reason: {i.change.rationale}"
            lines.append(f"| {i.step} | {changes} | {reasons} | "
                         f"{i.fit_variance:.2%} | {fmt(i.holdout_variance)} | {outcome} |")
        final = next(i for i in reversed(r.iterations) if i.accepted is not False)
        if r.lifecycle:
            lines += ["", "Lifecycle:"]
            for event in r.lifecycle:
                lines.append(f"- {event['action']} at {event['at']}: {event.get('reason', '')}")
        if r.diagnostics:
            d = r.diagnostics
            ratio = d["fit_ratio"]
            lines += ["", "Post-close diagnostics:", f"- Fit ratio pattern: {ratio['status']} ({ratio['periods']} periods)."]
            if "computed_over_target" in ratio:
                lines.append(f"- Computed / target: {ratio['computed_over_target']:.8g}; coefficient of variation: {ratio['coefficient_of_variation']:.2%}.")
            lines.append(f"- {ratio['interpretation']}")
            if "holdout_factor_check" in d:
                lines.append(f"- Holdout supports the fit-derived factor: {d['holdout_factor_check']['consistent']}.")
            if "vintages" in d:
                lines.append(f"- Declared vintage comparison: {sum(e['status'] == 'revised' for e in d['vintages'])} of {len(d['vintages'])} periods revised.")
            if "vintage_error" in d:
                lines.append(f"- Vintage diagnostic failed: {d['vintage_error']}")
            if "breakdown_error" in d:
                lines.append(f"- Breakdown diagnostic failed: {d['breakdown_error']}")
            for part in d.get("breakdowns", []):
                ratio = part["fit_ratio"]
                lines.append(f"- {part['option']} vs {part['reference_metric']}: {ratio['status']}; computed / target {ratio.get('computed_over_target', 'unavailable')}.")
        component_rows = [row for row in final.per_target if row.get("components")]
        if component_rows:
            lines += ["", "| Period | Split | Numerator | Denominator | Scale | Ratio |",
                      "|---|---|---|---|---|---|"]
            for row in component_rows:
                c = row["components"]
                lines.append(f"| {row['as_of']} | {row['split']} | {c['numerator']:.8g} | {c['denominator']:.8g} | {c['scale']:g} | {row['computed']:.8g} |")
        lines += ["", "Final query (first as-of date):", "", "```sql", r.final_query, "```"]
    return "\n".join(lines) + "\n"


def fmt(x):
    return "–" if x is None else f"{x:.2%}"
