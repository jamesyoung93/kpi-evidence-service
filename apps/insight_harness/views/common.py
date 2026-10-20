"""Shared answer rendering: tier chips, the provenance stamp, artifact actions,
decomposition waterfalls, the causal design brief, and scoped-refusal
presentation. Every page renders answers through render_answer()."""
from __future__ import annotations

import html
import math
import re

import altair as alt
import pandas as pd
import streamlit as st

from harness import semantic_layer as sl
from harness import runtime_policy, saved_insights, services, tiles, voice
from harness.provenance import TIER_ABSTAINED

TIER_COLORS = {"Verified": "#0E7C7B", "Directional": "#765100", "Hypothesis": "#70408F",
               TIER_ABSTAINED: "#6B7280"}

# chart palette (validated: in lightness band, >=3:1 on surface, CVD-safe pairs)
C_UP, C_DOWN, C_TOTAL = "#2a78d6", "#B07C0E", "#6B7280"
C_PRIMARY, C_REFERENCE = "#2a78d6", "#6B7280"
C_LABEL = "#52514e"
HOME_PAGE = "Home"
_BASIS_CONTROL = {code: label for label, code in tiles.BASIS_CONTROLS.items()}


def active_persona() -> str:
    """Return the active presentation persona without coupling voice to Streamlit."""

    return str(
        st.session_state.get("home_persona")
        or st.session_state.get("_home_persona_last")
        or "executive"
    )


def format_metric_value(metric: str, variant: str | None, value: float | None, *,
                        places: int = 1) -> str:
    """Format a governed value without losing its unit semantics."""

    if value is None:
        return "—"
    numeric = float(value)
    if sl.metric_kind(metric) == "ratio":
        return f"{numeric:.1%}"
    if variant == "dollars":
        return f"${numeric:,.{places}f}"
    return f"{numeric:,.{places}f}"


def format_artifact_value(art, value: float | None, *, places: int = 1) -> str:
    resolution = art.resolution
    if resolution is None:
        return "—" if value is None else f"{float(value):,.{places}f}"
    return format_metric_value(resolution.metric, resolution.variant, value, places=places)


def format_comparison_delta(art, comparison: dict) -> str | None:
    """Return relative percent for additive metrics and points for ratios."""

    if not comparison.get("available") or art.resolution is None:
        return None
    if sl.metric_kind(art.resolution.metric) == "ratio":
        points = comparison.get("delta_pp")
        return f"{float(points):+.1f} pp" if points is not None else None
    relative = comparison.get("delta_pct")
    return f"{float(relative) * 100:+.1f}%" if relative is not None else None


def format_native_delta(metric: str, variant: str | None, value: float | None) -> str:
    """Format an additive movement in the metric's native unit."""

    if value is None:
        return "—"
    numeric = float(value)
    if sl.metric_kind(metric) == "ratio":
        return f"{numeric * 100:+.1f} pp"
    if variant == "dollars":
        sign = "+" if numeric >= 0 else "-"
        return f"{sign}${abs(numeric):,.1f}"
    return f"{numeric:+,.1f}"


# --------------------------------------------------------------------------- #
# Navigation and question queueing
# --------------------------------------------------------------------------- #
def goto(page: str, **state) -> None:
    """Callback: switch page and set session state in one click."""
    st.session_state.update(state)
    st.session_state["nav"] = page


def queue_question(question: str) -> None:
    """Callback: put a question in the Home explorer and go there."""
    st.session_state["ask_q"] = question
    st.session_state["_ask_q_last"] = question
    st.session_state.pop("replay_key", None)
    st.session_state["nav"] = HOME_PAGE


def queue_question_with_resolution(question: str, source: str | None = None,
                                   variant: str | None = None,
                                   basis: str | None = None) -> None:
    """Open a question without losing the definition that produced it.

    Drill-through surfaces use this callback whenever their artifact was
    evaluated under an explicit source or variant.  Setting governed defaults
    explicitly when an override is absent also prevents an unrelated, stale
    Ask override from changing the reopened answer.
    """

    st.session_state["ask_src"] = source or "governed default"
    st.session_state["_ask_src_last"] = st.session_state["ask_src"]
    st.session_state["ask_var"] = variant or "governed default"
    st.session_state["_ask_var_last"] = st.session_state["ask_var"]
    basis_labels = {
        "prior_month": "prior month",
        "prior_quarter": "prior quarter",
        "yoy": "same month last year",
    }
    if basis in basis_labels:
        st.session_state["ask_basis"] = basis_labels[basis]
        st.session_state["_ask_basis_last"] = basis_labels[basis]
    elif basis is None:
        st.session_state.pop("ask_basis", None)
        st.session_state.pop("_ask_basis_last", None)
    queue_question(question)


def clear_replay() -> None:
    st.session_state.pop("replay_key", None)


def saved_insight_store() -> saved_insights.InMemorySavedInsightStore:
    """Return this viewer's isolated store.

    Historical container-global watch files are deliberately not imported:
    anonymous viewers must never inherit another viewer's saved questions.
    """

    return saved_insights.session_store(st.session_state)


def _watch_window(intent) -> str:
    window = getattr(intent, "window", None)
    if window is None:
        return "Latest"
    if window.kind == "last_n":
        match = next((label for label, months in tiles.WINDOW_CONTROLS.items()
                      if months == len(window.months)), None)
        if match:
            return match
    raise ValueError("Only Latest, R3M, R6M, and R12M windows can be watched.")


def _save_watch(art) -> saved_insights.SaveResult:
    intent = art.extras["intent"]
    resolution = art.resolution
    basis_code = intent.compare_basis
    if basis_code is None and art.engine == "decomposition":
        basis_code = "prior_quarter"
    basis = _BASIS_CONTROL.get(basis_code or "prior_month", "MoM")
    if intent.question_class == "Retrieval":
        viz_kind = "count"
    elif intent.question_class == "Diagnostic":
        viz_kind = "table"
    else:
        viz_kind = "line" if intent.trend or art.chart_df is not None else "sparkline"
    label = (
        f"{voice.metric_name(resolution.metric)} · "
        f"{voice.scope_text(intent.filters, persona=active_persona())}"
    )
    insight = saved_insights.create_saved_insight(
        resolution.metric,
        intent.filters,
        label=label,
        source=resolution.source,
        variant=resolution.variant,
        window=_watch_window(intent),
        basis=basis,
        viz_kind=viz_kind,
        question_class=intent.question_class,
        breakdown_dimension=intent.dim_breakdown,
        retrieval_template=intent.template,
    )
    return saved_insight_store().add(insight)


# --------------------------------------------------------------------------- #
# Chips and the stamp
# --------------------------------------------------------------------------- #
def chip(tier: str, *, tooltip: str | None = None) -> str:
    label = html.escape(str(tier))
    title = f' title="{html.escape(tooltip, quote=True)}"' if tooltip else ""
    return (f'<span class="tier-chip"{title} '
            f'style="background:{TIER_COLORS.get(tier, "#333")}">{label}</span>')


def _artifact_window_label(art) -> str:
    """Return the effective window displayed by this exact artifact."""

    intent = art.extras.get("intent")
    # A diagnostic's answer is the actual comparison interval, which may
    # begin before the requested aggregation window because it needs a
    # reference period.
    if art.engine == "decomposition" and art.extras.get("m0") and art.extras.get("m1"):
        return f'{art.extras["m0"]}–{art.extras["m1"]}'

    # Registered causal designs own their pre/post window and treated scope;
    # the user's wording is not a substitute for either effective contract.
    if art.engine == "causal_advisor":
        estimate = art.extras.get("estimate", {})
        months = list(estimate.get("pre", [])) + list(estimate.get("post", []))
        if months:
            return f"{months[0]}–{months[-1]}"

    effective = [str(month) for month in art.extras.get("effective_window", [])]
    if effective:
        span = effective[0] if len(effective) == 1 else f"{effective[0]}–{effective[-1]}"
        requested = [str(month) for month in art.extras.get("requested_window", [])]
        return f"{span} effective" if requested and effective != requested else span

    if art.chart_df is not None and "month" in art.chart_df.columns:
        months = [str(month) for month in art.chart_df["month"].dropna().tolist()]
        if months:
            return months[0] if len(months) == 1 else f"{months[0]}–{months[-1]}"

    window = getattr(intent, "window", None)
    if window is not None:
        partial = re.search(
            r"\(partial: (20\d{2}-\d{2})–(20\d{2}-\d{2}) available here\)",
            art.headline,
        )
        if partial:
            return f"{partial.group(1)}–{partial.group(2)} (partial {window.label})"
        return str(window.label)

    # Older deterministic engines disclose their effective month in the
    # headline rather than a structured field.  Reading the first date keeps
    # the display faithful without changing the artifact or its result hash.
    if match := re.search(r"\b20\d{2}-\d{2}\b", art.headline):
        return match.group(0)
    if art.engine == "retrieval":
        return "current account snapshot"
    return "latest available"


def hero_figure_content(art, persona: str | None = None) -> tuple[str, str, str]:
    """Build the value, quiet metadata, and body for an answer hero.

    This pure adapter is deliberately separate from ``render_hero_figure`` so
    tiles and other stamped-answer surfaces can reuse the visual component
    without creating a second computation path.
    """

    persona = persona or "executive"
    intent = art.extras.get("intent")
    resolution = art.resolution
    effective_scope = art.extras.get("effective_scope")
    scope_filters = (
        effective_scope if effective_scope is not None
        else (getattr(intent, "filters", {}) if intent else {})
    )
    if resolution is None:
        metric = f"{art.question_class} request"
        scope = "Scope check"
        presentation = None
    else:
        metric = voice.variant_name(resolution.metric, resolution.variant)
        scope = voice.scope_text(scope_filters, persona=persona)
        presentation = voice.tile_presentation(
            art, persona=persona, scope=scope_filters)
    label = " · ".join((metric, scope, _artifact_window_label(art)))

    if art.tier == TIER_ABSTAINED:
        value = "No reliable answer"
        body = ""
    elif art.engine == "causal_advisor":
        effect = art.extras.get("estimate", {}).get("did_pct", art.value)
        value = "—" if effect is None else f"{float(effect) * 100:+.1f}%"
        body = presentation.headline
    elif art.engine == "retrieval":
        value = f"{len(art.table):,} records" if art.table is not None else "Result set"
        body = presentation.headline
    elif art.engine == "cohort":
        presentation = voice.cohort_presentation(art, persona=persona)
        value = presentation.hero
        label = presentation.label
        body = presentation.headline
    elif art.engine == "decomposition" and resolution is not None:
        value = format_native_delta(resolution.metric, resolution.variant, art.value)
        body = presentation.headline
    else:
        value = format_artifact_value(art, art.value)
        body = (presentation.headline if presentation is not None
                else voice.humanize_sentence(art.headline, persona))
    return value, label, body


def render_hero_figure(value: str, label: str, body: str, tier: str,
                       question_class: str) -> None:
    """Render the reusable answer/tile hero with escaped presentation text."""

    safe_value = html.escape(str(value))
    safe_label = html.escape(str(label))
    safe_body = html.escape(str(body))
    safe_class = html.escape(str(question_class))
    safe_heading = html.escape(f"{label}: {value}", quote=True)
    body_html = (
        f'<div class="answer-hero-body" style="font-size:1rem;line-height:1.5;'
        f'color:#3F3F46;max-width:78ch;margin-top:8px;">{safe_body}</div>'
        if safe_body else ""
    )
    st.markdown(
        f'<section class="answer-hero" aria-label="{safe_heading}" '
        'style="padding:2px 0 10px;">'
        '<div class="answer-hero-label" style="display:flex;align-items:center;'
        'gap:8px;flex-wrap:wrap;margin-bottom:8px;">'
        f'<span style="color:#6B7280;font-size:.78rem;letter-spacing:.02em;">'
        f'{safe_label}</span><span style="flex:1 1 16px;"></span>{chip(tier)}'
        f'<span class="question-chip" style="border:1px solid #D1D5DB;border-radius:12px;'
        f'padding:2px 10px;color:#52525B;font-size:.76rem;font-weight:600;">'
        f'{safe_class}</span></div>'
        f'<h3 class="answer-hero-value" style="font-size:44px;line-height:1.05;'
        f'font-weight:600;letter-spacing:-.025em;color:#18181B;margin:0;">'
        f'{safe_value}</h3>{body_html}</section>',
        unsafe_allow_html=True,
    )


def render_stamp(art) -> None:
    s = art.stamp()
    tr = art.extras.get("translation", {})
    if tr.get("translator") == "llm":
        tr_label = "language model (validated)"
        if tr.get("latency_ms") is not None:
            tr_label += f", {tr['latency_ms']} ms"
    elif tr.get("fallback_reason"):
        tr_label = "built-in parser (language-model fallback"
        tr_label += f", {tr['latency_ms']} ms)" if tr.get("latency_ms") is not None else ")"
    else:
        tr_label = "built-in parser"
    reviewed = art.extras.get("analyst_reviewed") or \
        st.session_state.get("reviewed", {}).get(art.result_hash)
    reviewed_str = f" · analyst-reviewed {reviewed[:10]}" if reviewed else ""
    st.markdown(f'<div class="stamp">translator: {tr_label} · engine: {s["engine"]} · '
                f'result hash: {s["result_hash"]} · '
                f'data version: {s["data_version"]} · {s["created_at"]}{reviewed_str}</div>',
                unsafe_allow_html=True)
    if tr.get("fallback_reason"):
        st.caption(f"ℹ️ {tr['fallback_reason']}")


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def visible_y_domain(chart_df: pd.DataFrame | None, *,
                     padding_ratio: float = 0.08) -> list[float] | None:
    """Return a padded y-domain containing every finite visible series value.

    An explicit non-zero domain keeps small but meaningful movement legible in
    both compact tiles and expanded Ask answers.  The padding also handles a
    constant series without clipping zero, negative, or ratio-valued data.
    """

    if chart_df is None or chart_df.empty:
        return None
    columns = [column for column in chart_df.columns if column != "month"]
    if not columns:
        return None
    numeric = chart_df[columns].apply(pd.to_numeric, errors="coerce")
    values = [float(value) for value in numeric.to_numpy().ravel()
              if pd.notna(value) and math.isfinite(float(value))]
    if not values:
        return None
    low, high = min(values), max(values)
    if math.isclose(low, high, rel_tol=0.0, abs_tol=0.0):
        pad = abs(low) * padding_ratio if low else 1.0
    else:
        pad = max((high - low) * padding_ratio,
                  max(abs(low), abs(high), 1.0) * 1e-9)
    return [low - pad, high + pad]


def _line_chart_spec(chart_df: pd.DataFrame) -> alt.Chart:
    """Build the governed answer chart separately for visual-contract tests."""

    series = [c for c in chart_df.columns if c != "month"]
    long = chart_df.melt("month", var_name="series", value_name="value").dropna()
    color = alt.Color("series:N",
                      scale=alt.Scale(domain=series, range=[C_PRIMARY, C_REFERENCE][:len(series)]),
                      legend=None if len(series) == 1 else alt.Legend(orient="top", title=None))
    dash = alt.StrokeDash(
        "series:N", legend=None,
        scale=alt.Scale(domain=series, range=[[1, 0], [6, 4]][:len(series)]),
    ) if len(series) > 1 else alt.value([1, 0])
    return alt.Chart(long).mark_line(point=False, strokeWidth=2).encode(
        x=alt.X("month:O", title=None),
        y=alt.Y(
            "value:Q", title=None,
            scale=alt.Scale(
                domain=visible_y_domain(chart_df), zero=False, nice=False)),
        color=color,
        strokeDash=dash,
        tooltip=[alt.Tooltip("month:O"), alt.Tooltip("series:N"),
                 alt.Tooltip("value:Q", format=",.1f")],
    ).properties(
        height=260,
        description=("Monthly current and comparison series on an explicit visible-data "
                     "domain; the comparison line is dashed."),
    )


def line_chart(chart_df: pd.DataFrame) -> None:
    st.altair_chart(_line_chart_spec(chart_df), width="stretch")


def waterfall(t: pd.DataFrame, m0: str, m1: str) -> alt.Chart:
    """Start level → per-group deltas → end level, as a bridge: levels are
    ticks (not zero-anchored bars, so the zoomed axis misrepresents nothing),
    deltas are floating bars, and labels carry the sign so polarity never
    rides on color alone."""
    start, end = float(t["period_start"].sum()), float(t["period_end"].sum())
    rows = [{"label": m0, "y0": start, "y1": start, "kind": "total", "amount": start}]
    run = start
    for r in t.sort_values("delta", ascending=False).itertuples():
        rows.append({"label": str(r.value), "y0": run, "y1": run + float(r.delta),
                     "kind": "up" if r.delta >= 0 else "down", "amount": float(r.delta)})
        run += float(r.delta)
    rows.append({"label": m1, "y0": end, "y1": end, "kind": "total", "amount": end})
    df = pd.DataFrame(rows)
    df["ymax"] = df[["y0", "y1"]].max(axis=1)
    df["text"] = [f"{r.amount:+,.0f}" if r.kind != "total" else f"{r.amount:,.0f}"
                  for r in df.itertuples()]
    connectors = pd.DataFrame([
        {"x": rows[i]["label"], "x2": rows[i + 1]["label"], "y": rows[i]["y1"]}
        for i in range(len(rows) - 1)])

    # sort=None keeps the band domain in data order; a custom sort array is
    # dropped by Vega-Lite when layers share an x scale with an x2 channel
    yscale = alt.Scale(zero=False, nice=True)
    base = alt.Chart(df).encode(
        x=alt.X("label:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)))
    bars = base.transform_filter(alt.datum.kind != "total").mark_bar(
        size=24, cornerRadius=2).encode(
        y=alt.Y("y0:Q", title=None, scale=yscale), y2="y1:Q",
        color=alt.Color("kind:N", legend=None,
                        scale=alt.Scale(domain=["up", "down"], range=[C_UP, C_DOWN])),
        tooltip=[alt.Tooltip("label:N", title="group"),
                 alt.Tooltip("text:N", title="change")],
    )
    levels = base.transform_filter(alt.datum.kind == "total").mark_tick(
        size=46, thickness=3, color=C_TOTAL).encode(
        y=alt.Y("y1:Q", title=None, scale=yscale),
        tooltip=[alt.Tooltip("label:N", title="period"),
                 alt.Tooltip("text:N", title="level")],
    )
    links = alt.Chart(connectors).mark_rule(
        strokeDash=[3, 3], color=C_TOTAL, opacity=0.6).encode(
        x=alt.X("x:N", sort=None), x2="x2:N", y=alt.Y("y:Q", scale=yscale))
    labels = base.mark_text(dy=-10, fontSize=11, color=C_LABEL).encode(
        y=alt.Y("ymax:Q", scale=yscale), text="text:N")
    return (links + bars + levels + labels).properties(height=280)


# --------------------------------------------------------------------------- #
# Engine-specific answer bodies
# --------------------------------------------------------------------------- #
def _decomposition_body(art, key: str) -> None:
    st.caption(art.extras["note"])
    m0, m1 = art.extras["m0"], art.extras["m1"]
    tables = art.extras["tables"]
    if tables:  # empty when every dimension in scope is pinned to one value
        lead = art.extras["lead_dim"]
        dims = sorted(tables, key=lambda d: d != lead)  # lead dimension first
        tabs = st.tabs([f"by {voice.column_name(d).lower()}" for d in dims])
        for tab, dim in zip(tabs, dims):
            with tab:
                st.altair_chart(waterfall(tables[dim], m0, m1), width="stretch")
                with st.expander("Contribution detail"):
                    st.dataframe(
                        voice.humanize_table(tables[dim]),
                        width="stretch", hide_index=True)
    for ev in art.extras.get("overlapping_events", []):
        c1, c2 = st.columns([3, 2])
        c1.info(f"A registered event overlaps this window: **{ev['name']}**. "
                "Attribution needs a designed test, not a read of this chart.")
        c2.button("Test attribution in Causal Studio", key=f"{key}_studio_{ev['id']}",
                  on_click=goto, args=("Causal Studio",),
                  kwargs={"studio_event": ev["id"], "studio_autorun": True,
                          "studio_metric": art.resolution.metric})


def _signoff(art) -> None:
    credential = st.session_state.get("_governance_admin_credential")
    if not runtime_policy.valid_governance_token(credential):
        st.toast("Analyst sign-off requires administrator authentication.")
        return
    ts = services.log_feedback(art, "analyst_reviewed", "authenticated-admin")
    st.session_state.setdefault("reviewed", {})[art.result_hash] = ts
    art.extras["analyst_reviewed"] = ts


def _causal_brief(art, key: str, persona: str | None = None) -> None:
    est, ev, sens = art.extras["estimate"], art.extras["event"], art.extras["sensitivity"]
    def render_scope(values: dict) -> str:
        return voice.scope_text(values, persona=persona or active_persona())

    scope = render_scope(art.extras.get("analysis_scope") or ev["scope"])
    controls = render_scope(art.extras.get("control_scope")
                            or ev.get("control_scope")
                            or ev.get("candidate_controls", {}))

    st.caption(f"Question: {art.question}")
    st.markdown(
        f"**Design** — difference-in-differences around **{ev['name']}** (from {ev['start']}). "
        f"Treated scope: {scope}. Control group: {controls}. "
        f"Pre-period: {len(est['pre'])} months · post-period: {len(est['post'])} months.")

    st.markdown("**Assumption checks — computed from the data**")
    checks = pd.DataFrame(art.extras["checks"])
    checks["status"] = checks["status"].map(
        {"pass": "✅ pass", "flag": "⚠️ flag", "manual": "👤 needs review"})
    st.dataframe(voice.humanize_table(checks), width="stretch", hide_index=True)

    c1, c2 = st.columns(2)
    c1.metric("Estimated effect (control-adjusted)", f"{est['did_pct']*100:+.1f}%")
    c2.metric("Treated pre/post read", f"{est['treated_growth_pct']*100:+.1f}%",
              help="What the change looks like with no control group. The difference "
                   "between the two numbers is what the comparison corrects.")
    if art.chart_df is not None:
        line_chart(art.chart_df)

    sens_bits = [f"{sl.METRICS[art.resolution.metric]['variants'][v]['label']} "
                 f"{p*100:+.1f}%" for v, p in sens.items()]
    sens_bits += [f"{sl.SOURCES[s]['name']} {p*100:+.1f}%"
                  for s, p in art.extras.get("source_sensitivity", {}).items()
                  if s != art.resolution.source]
    st.caption("Sensitivity — the estimate under each registered alternate: "
               + " · ".join(sens_bits))
    if art.extras.get("note"):
        st.caption(art.extras["note"])

    reviewed = st.session_state.get("reviewed", {}).get(art.result_hash) or \
        art.extras.get("analyst_reviewed")
    if reviewed:
        # rehydrate recomputed artifacts so the JSON download matches the badge
        art.extras.setdefault("analyst_reviewed", reviewed)
        st.success(f"Analyst-reviewed on {reviewed[:10]}. The sign-off is recorded with "
                   "the answer's provenance.")
    else:
        authorized = runtime_policy.valid_governance_token(
            st.session_state.get("_governance_admin_credential"))
        if authorized:
            st.button("Mark as analyst-reviewed", key=f"{key}_signoff",
                      on_click=_signoff, args=(art,),
                      help="Records your authenticated sign-off against this exact result "
                           "hash and data version.")
        else:
            st.info("Analyst sign-off is locked. Authenticate with the server-configured "
                    "administrator token on the Semantic Layer page to review this result.")


def _cohort_body(art, key: str, persona: str | None = None) -> None:
    """Render mixed-unit cohort gaps as independent-scale grouped small multiples."""

    presentation = voice.cohort_presentation(
        art, persona=persona or active_persona())
    st.markdown(
        '<span class="question-chip" style="border:1px solid #D1D5DB;'
        'border-radius:12px;padding:2px 10px;color:#52525B;font-size:.76rem;'
        f'font-weight:600;">{html.escape(presentation.method_chip)}</span>',
        unsafe_allow_html=True,
    )
    chart = art.chart_df
    if chart is not None and not chart.empty and {
            "label", "cohort", "value"}.issubset(chart.columns):
        plot = chart.copy()
        if "metric" in plot:
            plot["label"] = plot["metric"].map(voice.column_name)
        plot["cohort"] = plot["cohort"].map({
            "top_hcps": "Top HCPs", "matched_peers": "Matched peers",
        }).fillna(plot["cohort"])
        plot["display"] = [
            f"{float(row.value):.1%}" if row.value_format == "percent"
            else f"{float(row.value):,.1f}"
            for row in plot.itertuples()
        ]
        bars = alt.Chart(plot).mark_bar(cornerRadiusTopLeft=3,
                                        cornerRadiusTopRight=3).encode(
            x=alt.X("cohort:N", title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("value:Q", title=None, scale=alt.Scale(zero=True)),
            color=alt.Color(
                "cohort:N", title=None,
                scale=alt.Scale(domain=["Top HCPs", "Matched peers"],
                                range=[C_PRIMARY, C_REFERENCE]),
                legend=alt.Legend(orient="top"),
            ),
            tooltip=[alt.Tooltip("label:N", title="Activity"),
                     alt.Tooltip("cohort:N", title="Cohort"),
                     alt.Tooltip("display:N", title="Value")],
        ).properties(width=210, height=150)
        st.altair_chart(
            bars.facet(
                facet=alt.Facet("label:N", title=None,
                                header=alt.Header(labelFontSize=11, labelLimit=220)),
                columns=2,
            ).resolve_scale(y="independent"),
            width="stretch",
        )

    if art.table is not None:
        display = voice.cohort_display_table(art.table, persona)
        st.dataframe(
            display, width="stretch", hide_index=True)

    selection = art.extras.get("selection")
    matches = art.extras.get("peer_matches")
    if isinstance(selection, pd.DataFrame):
        with st.expander("Who was compared"):
            st.dataframe(
                voice.humanize_table(selection), width="stretch", hide_index=True)
    if isinstance(matches, pd.DataFrame):
        with st.expander("How peers were matched"):
            st.dataframe(
                voice.humanize_table(matches), width="stretch", hide_index=True)
            recipe = art.extras.get("recipe")
            if recipe:
                st.caption("Complete matching recipe")
                st.json(recipe)
    st.button(
        "Design a causal follow-up",
        key=f"{key}_causal_followup",
        on_click=goto,
        args=("Causal Studio",),
        kwargs={"studio_metric": "nrx"},
        help=("Open Causal Studio to register or select an event-based design. "
              "The cohort gap itself remains correlational."),
    )


# --------------------------------------------------------------------------- #
# Cross-cutting blocks: divergence, caveats, provenance, actions
# --------------------------------------------------------------------------- #
def _divergence_block(art, persona: str | None = None) -> None:
    material = [d for d in art.divergence if d["material"]]
    if not material:
        return
    persona = persona or active_persona()
    with st.expander("Why two answers exist"):
        st.caption("The answer above uses the governed default. These registered alternates "
                   "move it materially; escalate unresolved forks to metric governance.")
        for fork in material:
            presentation = voice.definition_fork_presentation(
                art, fork, persona=persona)
            note = f" · {fork['note']}" if fork.get("note") else ""
            st.markdown(f"- {presentation.detail}{note}")


def _caveats_block(art) -> None:
    if not art.caveats:
        return
    with st.expander(f"⚠ Caveats ({len(art.caveats)})"):
        for c in art.caveats:
            st.markdown(f'<div class="caveat">{c}</div>', unsafe_allow_html=True)


def _provenance_block(art) -> None:
    with st.expander("Provenance — how this number was produced"):
        r = art.resolution
        if r:
            st.markdown(f"- **Metric:** {sl.METRICS[r.metric]['label']} · **variant:** "
                        f"{sl.METRICS[r.metric]['variants'][r.variant]['label']} · **source:** "
                        f"{sl.SOURCES[r.source]['name']}\n- **Resolution:** {r.reason}")
        tr = art.extras.get("translation", {})
        if tr.get("translator") == "llm":
            st.markdown("**Translation (language-model output, registry-validated "
                        "before execution):**")
            st.code(tr.get("raw", ""), language="json")
        st.code(art.code, language="python")


def _actions_row(art, key: str, *, allow_expand: bool = True,
                 persona: str | None = None) -> None:
    slug = art.question_class.lower().replace(" ", "_")
    # Keep the receipt controls usable in the narrow main pane created by an
    # open sidebar.  A trailing spacer made every real control collapse and
    # wrap vertically; export gets the dominant share instead.
    c1, c2, c3, c4, c5 = st.columns(
        [3.0, 1.0, 1.0, 1.0, 1.2], gap=None, vertical_alignment="center")
    with c1:
        # ``key`` was added to st.popover after our supported Streamlit 1.50
        # floor.  The surrounding answer controls already carry hash-derived
        # keys, and one popover is rendered per card, so no explicit key is
        # needed here.
        # Streamlit supplies the disclosure chevron, so the visible control is
        # still “Download ▾” without duplicating the glyph for screen readers.
        with st.popover("Download", help="Export this exact stamped answer."):
            st.download_button(
                "Answer JSON", data=art.to_json(),
                file_name=f"answer_{slug}_{art.result_hash}.json",
                mime="application/json", key=f"{key}_json", width="stretch",
                help="Download the complete answer, provenance, and result hash.",
            )
            if art.table is not None:
                st.download_button(
                    "Table CSV", data=art.table.to_csv(index=False),
                    file_name=f"answer_{slug}_{art.result_hash}.csv",
                    mime="text/csv", key=f"{key}_csv", width="stretch",
                    help="Download the rows shown with this answer.",
                )
    votes = st.session_state.setdefault("_feedback_votes", {})
    already_voted = art.result_hash in votes
    if c2.button("👍", key=f"{key}_up", disabled=already_voted,
                 help="Mark this answer correct"):
        services.log_feedback(art, "correct")
        votes[art.result_hash] = "correct"
        st.toast("Logged.")
    if c3.button("🚩", key=f"{key}_down", disabled=already_voted,
                 help="Flag this number as wrong"):
        services.log_feedback(art, "wrong")
        votes[art.result_hash] = "wrong"
        st.toast("Logged — flagged answers are reviewed.")
    # A saved insight must reproduce the original bounded question class.
    # Causal designs are intentionally not watchable: SavedQuestionSpec does
    # not model event/design identity, so presenting one as a saved trend would
    # be false provenance.
    watchable = art.resolution is not None and art.extras.get("intent") is not None \
        and art.engine in ("descriptive", "decomposition", "retrieval")
    if watchable:
        if c4.button("👁", key=f"{key}_watch",
                     help="Pin this metric and scope to the Watched list in Monitoring."):
            try:
                result = _save_watch(art)
                st.toast("Watching — see Monitoring." if result.added
                         else "Already on the watchlist.")
            except ValueError as exc:
                st.toast(str(exc))
    expandable = (art.chart_df is not None or art.table is not None
                  or art.engine in {"causal_advisor", "decomposition", "cohort"})
    if allow_expand and expandable:
        if c5.button("Expand", key=f"{key}_expand",
                     help="Open the complete artifact in a large dialog."):
            show_answer_dialog(art, key, persona=persona)


def _refusal(art, key: str, persona: str | None = None) -> None:
    presentation = voice.refusal_presentation(
        art, persona=persona or active_persona())
    lead = html.escape(presentation.lead)
    reason = html.escape(presentation.detail)
    question = html.escape(str(art.question))
    st.markdown(
        f'<div class="refusal"><b>{lead}</b><br>'
        f'<span style="font-size:.88rem;color:#52525B">{reason}</span>'
        '<div style="margin-top:.45rem;font-size:.75rem;color:#71717A">'
        f'Scoped refusal · “{question}”</div></div>',
        unsafe_allow_html=True,
    )
    reframes = art.extras.get("reframes", [])
    if reframes:
        st.caption("Reliable ways to ask this:")
        cols = st.columns(len(reframes) + 1)
        for i, r in enumerate(reframes):
            cols[i].button(r, key=f"{key}_reframe_{i}", on_click=queue_question, args=(r,))
    render_stamp(art)


# --------------------------------------------------------------------------- #
# The single answer renderer
# --------------------------------------------------------------------------- #
def render_answer(art, key: str = "ans", *, allow_expand: bool = True,
                  persona: str | None = None) -> None:
    persona = persona or active_persona()
    key = f"{key}_{art.result_hash}"
    with st.container(border=True):
        value, label, body = hero_figure_content(art, persona)
        render_hero_figure(value, label, body, art.tier, art.question_class)

        if art.tier == TIER_ABSTAINED:
            _refusal(art, key, persona)
            _actions_row(
                art, key, allow_expand=allow_expand, persona=persona)
            return

        if art.engine == "causal_advisor":
            _causal_brief(art, key, persona)
        elif art.engine == "decomposition":
            _decomposition_body(art, key)
        elif art.engine == "cohort":
            _cohort_body(art, key, persona)
        else:
            if art.chart_df is not None:
                line_chart(art.chart_df)
            if art.table is not None and art.engine != "descriptive":
                st.dataframe(voice.humanize_table(art.table, persona), width="stretch",
                             height=min(320, 45 + 35 * len(art.table)))

        _divergence_block(art, persona)
        _caveats_block(art)
        _provenance_block(art)
        render_stamp(art)
        _actions_row(art, key, allow_expand=allow_expand, persona=persona)


@st.dialog("Explore governed answer", width="large")
def show_answer_dialog(art, key: str = "answer_dialog",
                       persona: str | None = None) -> None:
    """Render the canonical artifact at full width without a recursive expand action."""

    render_answer(
        art, key=f"{key}_dialog", allow_expand=False,
        persona=persona or active_persona())
