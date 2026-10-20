"""Diagnostic decomposition across governed pharma dimensions.

Deterministic arithmetic on additive metrics — this is the checkable answer to
most of what people mean by 'why'. Contributions across each dimension sum
exactly to the total delta; the engine ranks dimensions by concentration.
"""
from __future__ import annotations

import pandas as pd

from .. import semantic_layer as sl
from ..provenance import AnswerArtifact, TIER_ABSTAINED, TIER_VERIFIED
from ..triage import BASIS_LABELS, Intent

_BASIS_STEPS = {"prior_month": 1, "prior_quarter": 3, "yoy": 12}


def _contributions(df: pd.DataFrame, col: str, dim: str, m0: str, m1: str) -> pd.DataFrame:
    a = df[df["month"] == m0].groupby(dim)[col].sum()
    b = df[df["month"] == m1].groupby(dim)[col].sum()
    out = pd.DataFrame({"period_start": a, "period_end": b}).fillna(0.0)
    out["delta"] = out["period_end"] - out["period_start"]
    total = out["delta"].sum()
    # A zero net change can contain real offsetting movements, but none has a
    # meaningful share of zero. Preserve that distinction as undefined.
    out["share_of_change"] = out["delta"] / total if total != 0 else float("nan")
    return out.sort_values("delta").reset_index().rename(columns={dim: "value"}).assign(dimension=dim)


def decompose(intent: Intent, res: sl.Resolution) -> AnswerArtifact:
    if sl.metric_kind(res.metric) == "ratio":
        art = AnswerArtifact(
            intent.question, intent.question_class, TIER_ABSTAINED, "abstention",
            headline=(f"Declined: {sl.METRICS[res.metric]['label']} is a ratio metric; "
                      "additive contribution decomposition would misstate the result."),
            resolution=res,
        )
        art.extras["reframes"] = [f"Trend {sl.METRICS[res.metric]['label']} by month",
                                  "Which specialties account for the TRx change?"]
        return art
    df = sl.apply_filters(sl.load_fact(res.source), intent.filters)
    if df.empty:
        art = AnswerArtifact(
            intent.question, intent.question_class, TIER_ABSTAINED, "abstention",
            headline=(f"Declined: no governed observations exist for "
                      f"{sl.scope_string(intent.filters)} in "
                      f"{sl.SOURCES[res.source]['name']}."),
            resolution=res,
        )
        art.extras["reframes"] = [f"Trend {sl.METRICS[res.metric]['label']} by month"]
        return art
    col = sl.column_for(res.metric, res.variant)
    ms = sorted(df["month"].unique())
    if not ms:
        raise AssertionError("non-empty governed frame must contain a month")

    # Anchor on the requested period. A missing single-month anchor is no data,
    # not permission to answer a different month. For a multi-month request we
    # may use the available portion only when the requested/effective windows
    # are explicitly carried in the artifact and caveats.
    requested_window = list(intent.window.months) if intent.window else None
    effective_window = ([m for m in requested_window if m in ms]
                        if requested_window else None)
    coverage_note = None
    if requested_window:
        if not effective_window or (len(requested_window) == 1
                                    and requested_window[-1] not in ms):
            art = AnswerArtifact(
                intent.question, intent.question_class, TIER_ABSTAINED, "abstention",
                headline=(f"Declined: {sl.SOURCES[res.source]['name']} has no data for "
                          f"the requested anchor {requested_window[-1]}; it covers "
                          f"{ms[0]} through {ms[-1]}."),
                resolution=res,
            )
            art.extras["requested_window"] = requested_window
            art.extras["effective_window"] = effective_window or []
            art.extras["reframes"] = [f"Which regions account for the "
                                      f"{sl.METRICS[res.metric]['label']} change?"]
            return art
        anchor = effective_window[-1]
        if effective_window != requested_window:
            coverage_note = (
                f"requested {intent.window.label} ({requested_window[0]}–"
                f"{requested_window[-1]}); effective source window "
                f"{effective_window[0]}–{effective_window[-1]}"
            )
    else:
        anchor = ms[-1]
    basis = intent.compare_basis or "prior_quarter"
    steps = _BASIS_STEPS[basis]
    i1 = ms.index(anchor)
    i0 = max(0, i1 - steps)
    m0, m1 = ms[i0], anchor
    basis_label = BASIS_LABELS[basis] + \
        (" — clamped to available history" if i1 - steps < 0 else "")

    total0 = df[df["month"] == m0][col].sum()
    total1 = df[df["month"] == m1][col].sum()
    delta = total1 - total0

    # Honor the dimension explicitly requested. Otherwise, include only
    # dimensions with actual post-filter cardinality greater than one.
    candidates = [intent.dim_breakdown] if intent.dim_breakdown else list(sl.DIMENSIONS)
    dims = [dimension for dimension in candidates
            if dimension in sl.DIMENSIONS and df[dimension].nunique(dropna=False) > 1]
    tables = {dim: _contributions(df, col, dim, m0, m1) for dim in dims}
    label = sl.METRICS[res.metric]["variants"][res.variant]["label"]
    pct_text = (f"{delta / total0 * 100:+.1f}%" if total0
                else "percentage change unavailable: zero baseline")
    coverage_text = f"; {coverage_note}" if coverage_note else ""
    move = (f"{label} moved {delta:+,.1f} from {m0} to {m1} "
            f"({pct_text}, {basis_label}{coverage_text})")

    if tables:
        # rank dimensions by concentration: largest single |contribution| / |total delta|
        conc = {dim: (t["delta"].abs().max() / abs(delta) if delta
                      else t["delta"].abs().max())
                for dim, t in tables.items()}
        lead_dim = max(conc, key=conc.get)
        lead = tables[lead_dim].iloc[tables[lead_dim]["delta"].abs().idxmax()]
        if delta:
            headline = (f"{move}. Largest single contributor: {lead_dim} = {lead['value']} "
                        f"({lead['delta']:+,.1f}, "
                        f"{lead['share_of_change']*100:.0f}% of the change).")
        else:
            headline = (f"{move}. Net change is zero because movements offset. "
                        f"Largest absolute movement: {lead_dim} = {lead['value']} "
                        f"({lead['delta']:+,.1f}); share of net change is unavailable.")
        note = ("Decomposition shows where the change sits, not what caused it. "
                "When a registered event overlaps this window, the Causal Studio "
                "can test attribution.")
        table = pd.concat(tables.values(), ignore_index=True) \
            [["dimension", "value", "period_start", "period_end", "delta", "share_of_change"]]
    else:
        # every dimension is pinned to a single value: the total move IS the answer
        lead_dim = None
        headline = f"{move}."
        note = ("Every dimension in this scope is pinned to a single value, so there is "
                "no finer breakdown to show — the total move above is the whole story. "
                "Widen the scope to decompose it.")
        table = None

    code = (f"df = load('{res.source}'); df = filter(df, {intent.filters})\n"
            f"for dim in {list(tables)}:\n"
            f"    delta[dim] = pivot(df, '{col}', dim, '{m1}') - pivot(df, '{col}', dim, '{m0}')\n"
            f"# comparison basis: {basis_label}; contributions per dimension sum to the total delta")

    art = AnswerArtifact(intent.question, intent.question_class, TIER_VERIFIED, "decomposition",
                         headline=headline, value=float(delta), code=code, table=table)
    art.resolution = res
    art.extras = {"m0": m0, "m1": m1, "lead_dim": lead_dim, "tables": tables, "note": note}
    if requested_window:
        art.extras["requested_window"] = requested_window
        art.extras["effective_window"] = effective_window
    if coverage_note:
        art.caveats.append(
            f"{sl.SOURCES[res.source]['name']} does not cover the full requested window: "
            f"{coverage_note}.")
    # link registered events that overlap this window, cover this metric, and
    # don't contradict the active filters
    overlapping = [{"id": eid, "name": ev["name"]}
                   for eid, ev in sl.EVENTS.items()
                   if m0 <= ev["start"] <= m1
                   and res.metric in ev.get("metrics", [])
                   and _scope_compatible(ev["scope"], intent.filters)]
    if overlapping:
        art.extras["overlapping_events"] = overlapping
    return art


def _scope_compatible(event_scope: dict, filters: dict) -> bool:
    """An event is offered only when the analysis filters don't exclude it."""
    for dim, val in event_scope.items():
        if dim in filters:
            asked = filters[dim] if isinstance(filters[dim], (list, tuple)) else [filters[dim]]
            event_values = val if isinstance(val, (list, tuple)) else [val]
            if not set(asked).intersection(event_values):
                return False
    return True
