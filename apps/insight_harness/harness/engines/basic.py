"""Deterministic engines: descriptive aggregation/trend and retrieval templates."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from .. import semantic_layer as sl
from .. import services
from ..provenance import AnswerArtifact, TIER_ABSTAINED, TIER_VERIFIED, _stable_hash
from ..triage import BASIS_LABELS, Intent
from . import cohort

_BASIS_STEPS = {"prior_month": 1, "prior_quarter": 3, "yoy": 12}


@dataclass(frozen=True)
class TopWritersRecipe:
    """Immutable recipe for the governed Top writers retrieval surface."""

    version: str = "top_writers_nrx_share_v1"
    top_n: int = 15
    min_nrx_ttm: float = cohort.DEFAULT_RECIPE.min_nrx_ttm
    min_market_nrx_ttm: float = cohort.DEFAULT_RECIPE.min_market_nrx_ttm
    selection_metric: str = "nrx_share_ttm"
    numerator: str = "nrx_ttm"
    denominator: str = "market_nrx_ttm"
    tie_breakers: tuple[str, ...] = (
        "nrx_share_ttm DESC", "nrx_ttm DESC", "account_id ASC",
    )
    interpretation: str = "descriptive ranking only; not causal"


DEFAULT_TOP_WRITERS_RECIPE = TopWritersRecipe()


def _comparison_payload(basis: str, current_month: str, current_value: float,
                        source_months: list[str], monthly_values: dict,
                        *, ratio: bool = False) -> dict:
    """Return the stable comparison contract consumed by tiles and other views.

    Reference values are aligned to the current month rather than plotted at
    their historical x-position.  That makes a current/reference overlay a
    like-for-like comparison while retaining the actual reference month in the
    artifact for auditability.
    """
    payload = {
        "basis": basis,
        "basis_label": BASIS_LABELS[basis],
        "available": False,
        "current_month": current_month,
        "current_value": float(current_value),
        "reference_month": None,
        "reference_value": None,
        "delta": None,
        "delta_pct": None,
        "delta_pp": None,
    }
    try:
        reference_month = str(pd.Period(current_month, freq="M")
                              - _BASIS_STEPS[basis])
    except (TypeError, ValueError):
        return payload
    if reference_month not in source_months or reference_month not in monthly_values:
        return payload
    reference_value = float(monthly_values[reference_month])
    delta = float(current_value) - reference_value
    payload.update({
        "available": True,
        "reference_month": reference_month,
        "reference_value": reference_value,
        "delta": delta,
        "delta_pct": delta / reference_value if reference_value else None,
        "delta_pp": delta * 100 if ratio else None,
    })
    return payload


def _window_refusal(intent: Intent, res: sl.Resolution, src_months: list) -> AnswerArtifact:
    """The resolved source has no data for the asked window. Refuse rather
    than report a silently wrong number for a period the source can't cover."""
    src = sl.SOURCES[res.source]["name"]
    art = AnswerArtifact(intent.question, intent.question_class, TIER_ABSTAINED, "abstention",
                         headline=f"Declined: {src} has no data for {intent.window.label}; "
                                  f"it covers {src_months[0]} through {src_months[-1]}.")
    art.resolution = res
    kw = services.PRIMARY_KEYWORD.get(res.metric, "TRx")
    art.extras["reframes"] = [f"Trend {kw} by month", f"What is {kw} in the West region?"]
    return art


def _no_data_refusal(intent: Intent, res: sl.Resolution, detail: str = "") -> AnswerArtifact:
    scope = sl.scope_string(intent.filters)
    suffix = f" {detail}" if detail else ""
    art = AnswerArtifact(
        intent.question, intent.question_class, TIER_ABSTAINED, "abstention",
        headline=(f"Declined: no governed observations exist for {scope} in "
                  f"{sl.SOURCES[res.source]['name']}.{suffix}"),
        resolution=res,
    )
    keyword = services.PRIMARY_KEYWORD.get(res.metric, "TRx")
    art.extras["reframes"] = [f"Trend {keyword} by month",
                              f"What is {keyword} in the West region?"]
    return art


def _format_value(value: float, res: sl.Resolution) -> str:
    fmt = sl.METRICS[res.metric]["variants"][res.variant].get("format", "number")
    if fmt == "percent":
        return f"{value * 100:,.1f}%"
    if fmt == "currency":
        return f"${value:,.0f}"
    return f"{value:,.1f}"


def _append_comparison(headline: str, comparison: dict, res: sl.Resolution) -> str:
    if not comparison.get("available"):
        return headline
    prior = comparison["reference_value"]
    fmt = sl.METRICS[res.metric]["variants"][res.variant].get("format", "number")
    if fmt == "percent":
        return (f"{headline} ({comparison['delta_pp']:+.1f} pp "
                f"{comparison['basis_label']}, {comparison['reference_month']}: "
                f"{_format_value(prior, res)})")
    if prior:
        return (f"{headline} ({comparison['delta_pct'] * 100:+.1f}% "
                f"{comparison['basis_label']}, {comparison['reference_month']}: "
                f"{_format_value(prior, res)})")
    return headline


def descriptive(intent: Intent, res: sl.Resolution) -> AnswerArtifact:
    df = sl.load_fact(res.source)
    col = sl.column_for(res.metric, res.variant)
    fdf = sl.apply_filters(df, intent.filters)
    if fdf.empty:
        return _no_data_refusal(intent, res)
    label = sl.METRICS[res.metric]["variants"][res.variant]["label"]
    scope = sl.scope_string(intent.filters)
    src_months = sorted(df["month"].unique())
    metric_series = sl.monthly_metric(fdf, res.metric, res.variant)
    if metric_series.empty or metric_series.notna().sum() == 0:
        return _no_data_refusal(intent, res, "The registered ratio denominator is zero.")
    monthly_values = metric_series.to_dict()
    caveats: list[str] = []

    # a window is validated against the default calendar at parse time; the
    # resolved source may cover less, so re-clamp here and disclose the clamp
    w = intent.window
    wmonths, wlabel = None, None
    if w:
        wmonths = [m for m in w.months if m in src_months]
        if not wmonths:
            return _window_refusal(intent, res, src_months)
        wlabel = w.label
        if len(wmonths) < len(w.months):
            wlabel = f"{w.label} (partial: {wmonths[0]}–{wmonths[-1]} available here)"
            caveats.append(f"{sl.SOURCES[res.source]['name']} covers {src_months[0]} through "
                           f"{src_months[-1]}, so the window was clamped to the months it has.")

    if intent.trend:
        out = metric_series.rename(label).reset_index()
        if wmonths:
            out = out[out["month"].isin(wmonths)].reset_index(drop=True)
        latest = out.iloc[-1]
        comparison = None
        if intent.compare_basis:
            comparisons = [
                _comparison_payload(intent.compare_basis, month, value,
                                    src_months, monthly_values,
                                    ratio=sl.metric_kind(res.metric) == "ratio")
                for month, value in out[["month", label]].itertuples(index=False, name=None)
            ]
            reference_label = f"{label} · {BASIS_LABELS[intent.compare_basis]}"
            out[reference_label] = [
                c["reference_value"] if c["available"] else float("nan")
                for c in comparisons
            ]
            comparison = comparisons[-1]
            available = [i for i, c in enumerate(comparisons) if c["available"]]
            if not available:
                caveats.append(
                    f"The requested {BASIS_LABELS[intent.compare_basis]} reference series "
                    "predates the available history for this window, so the latest-point "
                    "comparison is unavailable."
                )
            elif len(available) < len(comparisons):
                caveats.append(
                    f"The {BASIS_LABELS[intent.compare_basis]} reference series begins at "
                    f"{out.iloc[available[0]]['month']}; earlier reference months predate "
                    "the available history."
                )
        code = (f"df = load('{res.source}')\n"
                f"df = filter(df, {intent.filters})\n"
                + f"trend = monthly_metric(df, '{res.metric}', '{res.variant}')"
                + (f"\nreference = align_prior(trend, basis="
                   f"'{intent.compare_basis}')" if intent.compare_basis else "")
                + (f"\ntrend = trend[trend.index.isin({wmonths})]"
                   if wmonths else ""))
        art = AnswerArtifact(intent.question, intent.question_class, TIER_VERIFIED, "descriptive",
                             headline=f"{label} ({scope}), {wlabel + ', ' if wlabel else ''}"
                                      f"latest month {latest['month']}: "
                                      f"{_format_value(float(latest[label]), res)}",
                             value=float(latest[label]), chart_df=out, table=out, code=code)
        if comparison is not None:
            art.extras["comparison"] = comparison
    elif w:
        val = sl.aggregate_metric(fdf[fdf["month"].isin(wmonths)], res.metric, res.variant)
        if pd.isna(val):
            return _no_data_refusal(intent, res, "The registered ratio denominator is zero.")
        headline = f"{label} ({scope}), {wlabel}: {_format_value(val, res)}"
        comparison = None
        if intent.compare_basis:
            if len(wmonths) == 1:
                comparison = _comparison_payload(intent.compare_basis, wmonths[0], val,
                                                 src_months, monthly_values,
                                                 ratio=sl.metric_kind(res.metric) == "ratio")
                prior = comparison["reference_value"]
                if comparison["available"] and (prior or sl.metric_kind(res.metric) == "ratio"):
                    headline = _append_comparison(headline, comparison, res)
                elif comparison["available"]:
                    caveats.append("The requested comparison value is zero, so a percentage "
                                   "change cannot be computed.")
                else:
                    caveats.append("The requested comparison month predates the available "
                                   "history, so the comparison is omitted.")
            else:
                caveats.append("A comparison basis applies to a single anchor month; this "
                               "answer aggregates a multi-month window, so the requested "
                               "comparison is omitted.")
        code = (f"df = load('{res.source}')\n"
                f"df = filter(df, {intent.filters})\n"
                f"value = aggregate_metric(df[df.month.isin({wmonths})], "
                f"'{res.metric}', '{res.variant}')")
        if comparison is not None:
            code += (
                f"\nreference = aggregate_metric(df[df.month == "
                f"'{comparison['reference_month']}'], '{res.metric}', '{res.variant}')\n"
                "delta = value - reference"
                if comparison["available"]
                else f"\n# {intent.compare_basis} reference is unavailable"
            )
        art = AnswerArtifact(intent.question, intent.question_class, TIER_VERIFIED, "descriptive",
                             headline=headline, value=val, code=code)
        if comparison is not None:
            art.extras["comparison"] = comparison
    else:
        ms = sorted(fdf["month"].unique())
        if not ms:
            return _no_data_refusal(intent, res)
        latest_month = ms[-1]
        val = sl.aggregate_metric(fdf.loc[fdf["month"] == latest_month],
                                  res.metric, res.variant)
        if pd.isna(val):
            return _no_data_refusal(intent, res, "The registered ratio denominator is zero.")
        headline = f"{label} ({scope}), {latest_month}: {_format_value(val, res)}"
        comparison = None
        if intent.compare_basis:
            comparison = _comparison_payload(intent.compare_basis, latest_month, val,
                                             src_months, monthly_values,
                                             ratio=sl.metric_kind(res.metric) == "ratio")
            prior = comparison["reference_value"]
            if comparison["available"] and (prior or sl.metric_kind(res.metric) == "ratio"):
                headline = _append_comparison(headline, comparison, res)
            elif comparison["available"]:
                caveats.append("The requested comparison value is zero, so a percentage "
                               "change cannot be computed.")
            else:
                caveats.append("The requested comparison month predates the available "
                               "history, so the comparison is omitted.")
        code = (f"df = load('{res.source}')\n"
                f"df = filter(df, {intent.filters})\n"
                f"value = aggregate_metric(df[df.month == '{latest_month}'], "
                f"'{res.metric}', '{res.variant}')")
        if comparison is not None:
            code += (
                f"\nreference = aggregate_metric(df[df.month == "
                f"'{comparison['reference_month']}'], '{res.metric}', '{res.variant}')\n"
                "delta = value - reference"
                if comparison["available"]
                else f"\n# {intent.compare_basis} reference is unavailable"
            )
        art = AnswerArtifact(intent.question, intent.question_class, TIER_VERIFIED, "descriptive",
                             headline=headline, value=val, code=code)
        if comparison is not None:
            art.extras["comparison"] = comparison
    art.resolution = res
    art.caveats.extend(caveats)
    return art


def _top_writers_retrieval(
        intent: Intent, res: sl.Resolution, accounts: pd.DataFrame,
        recipe: TopWritersRecipe = DEFAULT_TOP_WRITERS_RECIPE) -> AnswerArtifact:
    """Rank eligible HCPs by governed trailing-12-month NRx share."""

    recipe_payload = asdict(recipe)
    recipe_hash = _stable_hash(recipe_payload)
    required = {
        "account_id", "npi", "name", "specialty", "territory", "district",
        "region", "payer_channel", recipe.numerator, recipe.denominator,
        recipe.selection_metric, "decile",
    }
    missing = required - set(accounts.columns)
    if missing:
        art = AnswerArtifact(
            intent.question, intent.question_class, TIER_ABSTAINED, "abstention",
            headline=("Declined: governed Top writers inputs are unavailable: "
                      + ", ".join(sorted(missing)) + "."),
            resolution=res,
        )
        art.extras.update({"recipe": recipe_payload, "recipe_hash": recipe_hash})
        return art

    eligible = accounts[
        (accounts[recipe.numerator] >= recipe.min_nrx_ttm)
        & (accounts[recipe.denominator] >= recipe.min_market_nrx_ttm)
        & accounts[recipe.selection_metric].notna()
    ].copy()
    scope = sl.scope_string(intent.filters)
    common_extras = {
        "recipe": recipe_payload,
        "recipe_hash": recipe_hash,
        "scope": dict(intent.filters),
        "scoped_account_count": int(len(accounts)),
        "eligible_account_count": int(len(eligible)),
        "excluded_account_count": int(len(accounts) - len(eligible)),
        "column_roles": {
            "numerator": recipe.numerator,
            "denominator": recipe.denominator,
            "share": recipe.selection_metric,
        },
        "interpretation": recipe.interpretation,
    }
    floor_text = (
        f"NRx TTM ≥ {recipe.min_nrx_ttm:g} and market NRx TTM ≥ "
        f"{recipe.min_market_nrx_ttm:g}"
    )
    if eligible.empty:
        art = AnswerArtifact(
            intent.question, intent.question_class, TIER_ABSTAINED, "abstention",
            headline=(f"Declined: no HCPs in {scope} meet the governed Top writers "
                      f"floors ({floor_text})."),
            resolution=res,
        )
        art.caveats.append(
            "This governed recipe is a descriptive ranking only and cannot support "
            "causal claims about why an HCP has higher NRx share.")
        art.extras.update(common_extras)
        return art

    out = eligible.sort_values(
        [recipe.selection_metric, recipe.numerator, "account_id"],
        ascending=[False, False, True], kind="mergesort",
    ).head(recipe.top_n).copy()
    out.insert(0, "rank", range(1, len(out) + 1))
    out["npi"] = out["npi"].astype(str)
    out = out[[
        "rank", "account_id", "npi", "name", "specialty", "territory",
        "district", "region", "payer_channel", recipe.numerator,
        recipe.denominator, recipe.selection_metric, "decile",
    ]].reset_index(drop=True)
    code = (
        "hcp = filter(load('accounts'), filters)\n"
        f"eligible = hcp[(hcp.nrx_ttm >= {recipe.min_nrx_ttm:g}) & "
        f"(hcp.market_nrx_ttm >= {recipe.min_market_nrx_ttm:g}) & "
        "hcp.nrx_share_ttm.notna()]\n"
        "out = stable_sort(eligible, nrx_share_ttm DESC, nrx_ttm DESC, "
        f"account_id ASC).head({recipe.top_n})\n"
        "# Descriptive ranking only; no causal inference."
    )
    art = AnswerArtifact(
        intent.question, intent.question_class, TIER_VERIFIED, "retrieval",
        headline=(f"Top {len(out)} of {len(eligible)} eligible HCP writers in {scope} "
                  "by trailing-12-month NRx share "
                  f"({floor_text}); descriptive ranking only, not causal."),
        table=out, code=code, resolution=res,
    )
    art.caveats.extend([
        "Descriptive ranking only: this result does not establish why an HCP has "
        "higher NRx share and must not be interpreted causally.",
        f"Eligibility floors: {floor_text}; top {recipe.top_n} requested.",
        "NRx share is trailing-12-month brand NRx (numerator) divided by "
        "trailing-12-month market NRx (denominator).",
        "Ties resolve deterministically by NRx share descending, NRx TTM "
        "descending, then account ID ascending.",
    ])
    art.extras.update(common_extras)
    art.extras["selected_count"] = int(len(out))
    return art


def retrieval(intent: Intent, res: sl.Resolution) -> AnswerArtifact:
    supported_account_metrics = {"trx": "trx_ttm", "nrx": "nrx_ttm", "nbrx": "nbrx_ttm"}
    valid_template = intent.template in {"whitespace", "top_accounts", "top_writers"}
    metric_supported = intent.metric in supported_account_metrics
    recipe_metric_supported = (
        intent.metric == "trx" if intent.template == "whitespace"
        else intent.metric == "nrx" if intent.template == "top_writers"
        else metric_supported
    )
    if not valid_template or not metric_supported or not recipe_metric_supported:
        if intent.template == "whitespace":
            detail = "the whitespace definition is governed on trailing TRx"
        elif intent.template == "top_writers":
            detail = "the Top writers definition is governed on trailing NRx share"
        elif not valid_template:
            detail = "the requested account recipe is not registered"
        else:
            detail = "account ranking is registered only for TRx, NRx, and NBRx"
        art = AnswerArtifact(
            intent.question, intent.question_class, TIER_ABSTAINED, "abstention",
            headline=f"Declined: {detail}; no different metric was substituted.",
            resolution=res,
        )
        art.extras["reframes"] = ["Top 15 accounts by TRx",
                                  "List whitespace HCPs with no activity"]
        return art

    acc = sl.apply_filters(sl.load_accounts(), intent.filters)
    if acc.empty:
        return _no_data_refusal(intent, res)

    if intent.template == "top_writers":
        art = _top_writers_retrieval(intent, res, acc)
    elif intent.template == "whitespace":
        out = acc[(acc["decile"] >= 8) & (acc["months_since_rx"] >= 3)
                  & (acc["months_since_activity"] >= 3) & (acc["calls_90d"] == 0)] \
            .sort_values("trx_ttm", ascending=False) \
            [["account_id", "npi", "name", "specialty", "territory", "district", "region",
              "payer_channel", "trx_ttm", "nrx_ttm", "nbrx_ttm", "decile",
              "months_since_rx", "months_since_activity", "calls_90d",
              "call_plan_90d"]].reset_index(drop=True)
        code = ("hcp = load('accounts')  # prescriber-grain source only\n"
                f"acc = filter(acc, {intent.filters})\n"
                "out = hcp[(hcp.decile >= 8) & (hcp.months_since_rx >= 3) "
                "& (hcp.months_since_activity >= 3) & (hcp.calls_90d == 0)]")
        headline = (f"{len(out)} whitespace HCP accounts: decile 8+ by trailing TRx "
                    "with no prescription or field activity in 3+ months")
    else:
        ranking_column = supported_account_metrics[intent.metric]
        out = acc.sort_values(ranking_column, ascending=False).head(15) \
            [["account_id", "npi", "name", "specialty", "territory", "region", "trx_ttm",
              "nrx_ttm", "nbrx_ttm", "decile", "calls_90d", "call_plan_90d"]] \
            .reset_index(drop=True)
        code = ("hcp = load('accounts')\n"
                f"acc = filter(acc, {intent.filters})\n"
                f"out = hcp.nlargest(15, '{ranking_column}')")
        headline = (f"Top {len(out)} HCP accounts by trailing-twelve-month "
                    f"{sl.METRICS[intent.metric]['label']}")
    if intent.template != "top_writers":
        art = AnswerArtifact(intent.question, intent.question_class, TIER_VERIFIED, "retrieval",
                             headline=headline, table=out, code=code)
        art.resolution = res
    # computed from the source registry's grain metadata
    with_grain = [s["name"] for s in sl.SOURCES.values() if s.get("account_grain")]
    without_grain = [s["name"] for s in sl.SOURCES.values() if not s.get("account_grain")]
    if without_grain:
        art.caveats.append(f"Account-level data exists in {', '.join(with_grain)} only; "
                           f"{', '.join(without_grain)} has no account grain.")
    return art
