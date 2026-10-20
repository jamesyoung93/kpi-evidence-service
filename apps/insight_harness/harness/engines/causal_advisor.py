"""Registered quasi-experimental designs for the synthetic pharma events.

The advisor never upgrades a design proposal into a causal fact.  It executes
the event's registered treated/control scopes, computes assumption checks, and
returns a Hypothesis-tier artifact for analyst review.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import semantic_layer as sl
from ..provenance import AnswerArtifact, TIER_ABSTAINED, TIER_HYPOTHESIS
from ..triage import Intent

PRE_WINDOW = 6
DESIGN_METRICS = ("trx", "nrx", "nbrx")


def _monthly(df: pd.DataFrame, col: str, scope: dict) -> pd.Series:
    scoped = sl.apply_filters(df, scope)
    return scoped.groupby("month")[col].sum().sort_index()


def _did(treated: pd.Series, control: pd.Series, start: str,
         months_limit: set[str] | None = None) -> dict:
    months = sorted(set(treated.index) & set(control.index))
    if months_limit is not None:
        months = [month for month in months if month in months_limit]
    pre = [month for month in months if month < start][-PRE_WINDOW:]
    post = [month for month in months if month >= start]
    if len(pre) < 2 or not post:
        raise ValueError("the registered design lacks enough aligned pre/post history")

    t_pre, t_post = treated[pre].mean(), treated[post].mean()
    c_pre, c_post = control[pre].mean(), control[post].mean()
    if not t_pre or not c_pre:
        raise ValueError("the registered design has a zero pre-period denominator")
    treated_growth = (t_post - t_pre) / t_pre
    control_growth = (c_post - c_pre) / c_pre
    did_pct = treated_growth - control_growth
    did_abs = did_pct * t_pre

    x = np.arange(len(pre))
    slope_t = np.polyfit(x, treated[pre].values, 1)[0] / t_pre
    slope_c = np.polyfit(x, control[pre].values, 1)[0] / c_pre
    return {
        "pre": pre, "post": post, "did_abs": float(did_abs),
        "did_pct": float(did_pct), "treated_growth_pct": float(treated_growth),
        "control_growth_pct": float(control_growth),
        "pretrend_gap_pp_per_month": float((slope_t - slope_c) * 100),
        "t_pre": float(t_pre), "t_post": float(t_post),
        "c_pre": float(c_pre), "c_post": float(c_post),
    }


def _refusal(intent: Intent, res: sl.Resolution, headline: str,
             reframes: list[str]) -> AnswerArtifact:
    art = AnswerArtifact(intent.question, intent.question_class, TIER_ABSTAINED,
                         "abstention", headline="Declined: " + headline,
                         resolution=res)
    art.extras["reframes"] = reframes
    return art


def _filters_preserve_registered_scope(df: pd.DataFrame, event_scope: dict,
                                       filters: dict) -> tuple[bool, str | None]:
    """Allow repeated/implied scope wording, refuse exclusions or narrowing."""
    registered = sl.apply_filters(df, event_scope)
    asked = sl.apply_filters(registered, filters)
    if asked.empty:
        return False, "the requested filters exclude the registered treated population"
    if len(asked) != len(registered):
        return False, ("the requested filters narrow the treated population beyond the "
                       "registered causal design")
    return True, None


def _source_series(metric: str, variant: str, source: str, event: dict) -> tuple[pd.Series, pd.Series]:
    frame = sl.load_fact(source)
    column = sl.column_for(metric, variant)
    return (_monthly(frame, column, event["scope"]),
            _monthly(frame, column, event["control_scope"]))


def propose(intent: Intent, res: sl.Resolution) -> AnswerArtifact:
    event = sl.EVENTS[intent.event_id]
    registered = [metric for metric in event.get("metrics", []) if metric in DESIGN_METRICS]
    default_metric = event.get("default_metric", registered[0] if registered else "trx")
    if res.metric not in registered:
        return _refusal(
            intent, res,
            f"'{event['name']}' has no registered design for "
            f"{sl.METRICS[res.metric]['label']}; substituting another metric would "
            "misstate the question.",
            [f"What was the impact of {event['name']} on "
             f"{sl.METRICS[default_metric]['label']}?"],
        )

    metric = res.metric
    frame = sl.load_fact(res.source)
    compatible, reason = _filters_preserve_registered_scope(frame, event["scope"],
                                                            intent.filters)
    if not compatible:
        return _refusal(
            intent, res, f"{reason}. The governed design covers "
            f"{sl.scope_string(event['scope'])}.",
            [f"What was the impact of {event['name']} on "
             f"{sl.METRICS[metric]['label']}?"],
        )

    column = sl.column_for(metric, res.variant)
    treated = _monthly(frame, column, event["scope"])
    control = _monthly(frame, column, event["control_scope"])
    try:
        estimate = _did(treated, control, event["start"])
    except ValueError as exc:
        return _refusal(intent, res, str(exc) + ".",
                        [f"Trend {sl.METRICS[metric]['label']} by month"])

    # Variant sensitivity uses the exact same source and months as the primary
    # estimate. Effects are dimensionless growth gaps, so currency and volume
    # variants can be compared without comparing their levels.
    variant_sensitivity: dict[str, float] = {}
    for variant in sl.METRICS[metric]["variants"]:
        alt_column = sl.column_for(metric, variant)
        alt = _did(_monthly(frame, alt_column, event["scope"]),
                   _monthly(frame, alt_column, event["control_scope"]),
                   event["start"])
        variant_sensitivity[variant] = alt["did_pct"]

    # Source sensitivity is explicitly aligned to the intersection of source
    # calendars. A one-month panel lag can never masquerade as a source fork.
    source_series: dict[str, tuple[pd.Series, pd.Series]] = {}
    for source in sl.METRICS[metric]["sources"]:
        try:
            source_series[source] = _source_series(metric, res.variant, source, event)
        except (KeyError, OSError):
            continue
    aligned_months = set.intersection(*(
        set(treated_series.index) & set(control_series.index)
        for treated_series, control_series in source_series.values())) \
        if source_series else set()
    source_sensitivity: dict[str, float] = {}
    source_windows: dict[str, dict] = {}
    for source, (treated_series, control_series) in source_series.items():
        try:
            result = _did(treated_series, control_series, event["start"], aligned_months)
        except ValueError:
            continue
        source_sensitivity[source] = result["did_pct"]
        source_windows[source] = {"pre": result["pre"], "post": result["post"]}

    pretrend_ok = abs(estimate["pretrend_gap_pp_per_month"]) < 0.5
    short_post = len(estimate["post"]) < 4
    threshold = sl.materiality()
    checks = [
        {"check": "Parallel pre-trends (computed)",
         "result": (f"slope gap {estimate['pretrend_gap_pp_per_month']:+.2f} pp/month "
                    f"over {len(estimate['pre'])} pre months"),
         "status": "pass" if pretrend_ok else "flag"},
        {"check": "Post-period length",
         "result": f"{len(estimate['post'])} months since event",
         "status": "flag" if short_post else "pass"},
        {"check": "Sensitivity to metric variant (computed)",
         "result": " | ".join(f"{key}: {value * 100:+.1f}%"
                              for key, value in variant_sensitivity.items()),
         "status": ("pass" if max(variant_sensitivity.values())
                    - min(variant_sensitivity.values()) < threshold else "flag")},
        {"check": "Concurrent-event review",
         "result": event["notes"], "status": "manual"},
    ]
    if len(source_sensitivity) > 1:
        checks.insert(3, {
            "check": "Sensitivity to source (common window)",
            "result": " | ".join(
                f"{sl.SOURCES[source]['name']}: {value * 100:+.1f}%"
                for source, value in source_sensitivity.items()),
            "status": ("pass" if max(source_sensitivity.values())
                       - min(source_sensitivity.values()) < threshold else "flag"),
        })

    chart = pd.DataFrame({"month": treated.index, "treated": treated.values}).merge(
        pd.DataFrame({"month": control.index,
                      "control (avg-scaled)": control.values
                      * (estimate["t_pre"] / estimate["c_pre"])}), on="month")
    headline = (f"Design proposal: difference-in-differences for '{event['name']}' on "
                f"{sl.METRICS[metric]['label']} — Hypothesis, pending analyst review")
    code = (
        f"treated = monthly(df, '{column}', {event['scope']})\n"
        f"control = monthly(df, '{column}', {event['control_scope']})\n"
        "treated_growth = (mean(post(treated)) - mean(pre(treated))) / mean(pre(treated))\n"
        "control_growth = (mean(post(control)) - mean(pre(control))) / mean(pre(control))\n"
        "did_pct = treated_growth - control_growth\n"
        f"# pre={estimate['pre']}; post={estimate['post']}; source={res.source}; "
        f"variant={res.variant}"
    )

    art = AnswerArtifact(intent.question, intent.question_class, TIER_HYPOTHESIS,
                         "causal_advisor", headline=headline,
                         value=estimate["did_pct"], code=code, chart_df=chart,
                         resolution=res)
    art.extras = {
        "event": event, "estimate": estimate, "checks": checks,
        "sensitivity": variant_sensitivity,
        "source_sensitivity": source_sensitivity,
        "source_sensitivity_windows": source_windows,
        "effective_scope": event["scope"], "control_scope": event["control_scope"],
        "note": ("Causal estimates answer a narrow, registered version of 'why'. "
                 "Analyst sign-off records review in provenance; the estimate remains "
                 "Hypothesis-tier."),
    }
    if short_post:
        art.caveats.append("Short post-period: estimate will move as more months arrive.")
    return art
