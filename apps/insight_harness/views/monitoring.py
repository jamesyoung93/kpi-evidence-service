"""Monitoring: movements ranked by business impact, each one click away from
its breakdown."""
from __future__ import annotations

import logging

import streamlit as st

from harness import saved_insights, services, tile_runtime, tiles, voice
from harness import semantic_layer as sl
from views import common


_SURFACED_ROW_STYLES = """
<style>
div[class*="st-key-monitoring_flag_"] {
    border: 1px solid rgba(107, 114, 128, 0.22);
    border-left-width: 5px !important;
    border-radius: 0.5rem;
    padding: 0.7rem 0.85rem;
    margin-bottom: 0.45rem;
}
div[class*="st-key-monitoring_flag_up_"] {
    border-left-color: #0E7C7B !important;
}
div[class*="st-key-monitoring_flag_down_"] {
    border-left-color: #D05A5A !important;
}
div[class*="st-key-monitoring_flag_flat_"] {
    border-left-color: #2A78D6 !important;
}
.monitoring-priority {
    text-align: right;
    line-height: 1.3;
}
.monitoring-priority strong {
    display: block;
    font-size: 1rem;
}
.monitoring-priority span {
    color: #6B7280;
    font-size: 0.82rem;
}
</style>
"""


def _movement_summary(row, persona: str) -> str:
    presentation = voice.monitoring_presentation(
        row.metric_id,
        {row.dim: row.value},
        latest=float(row.latest),
        trailing_mean=float(row.trailing_mean),
        trailing_min=float(getattr(row, "typical_min", row.trailing_mean)),
        trailing_max=float(getattr(row, "typical_max", row.trailing_mean)),
        absolute_change=float(row.native_delta),
        low_base=bool(getattr(row, "low_base", False)),
        persona=persona,
    )
    return presentation.headline


def _remove_saved(store: saved_insights.InMemorySavedInsightStore,
                  insight_id: str) -> None:
    store.remove(insight_id)


def _human_scope(raw_scope: str, persona: str) -> str:
    """Adapt the feed's machine scope without changing its data contract."""

    return voice.humanize_sentence(raw_scope, persona=persona)


def _humanize_related(items, persona: str) -> str:
    rendered = []
    for item in tuple(items or ()):
        metric, separator, raw_scope = str(item).partition(" · ")
        rendered.append(
            f"{voice.metric_name(metric)} · {_human_scope(raw_scope, persona)}"
            if separator else voice.humanize_sentence(item, persona=persona))
    return "; ".join(rendered)


def _watched_section(persona: str) -> None:
    st.subheader("Watched")
    store = common.saved_insight_store()
    watched = tuple(insight for insight in store.all() if insight.watched)
    if not watched:
        st.caption("Nothing watched yet. Pin a metric with the 👁 Watch action under "
                   "any measured answer.")
        return
    for i, insight in enumerate(watched):
        metric_label = voice.metric_name(insight.spec.metric)
        scope_label = voice.scope_text(dict(insight.spec.filters), persona=persona)
        insight_label = f"{metric_label} · {scope_label}"
        c1, c2, c3, c4, c5 = st.columns([2.6, 2.6, 1.1, 1.6, 1.0])
        c1.markdown(f"**{insight_label}**")
        if insight.is_stale:
            c1.caption("saved definition is no longer registered")
            c2.caption(voice.humanize_sentence(insight.stale_reason, persona))
            c3.caption("—")
        else:
            try:
                evaluation = tile_runtime.evaluate_saved(insight)
                art = evaluation.artifact
                source = sl.SOURCES[art.resolution.source]["name"]
                variant = voice.variant_name(
                    art.resolution.metric, art.resolution.variant)
                c1.caption(f"{source} · {variant}")
                presentation = voice.tile_presentation(
                    art, persona=persona, scope=dict(insight.spec.filters))
                c2.caption(presentation.headline)
                material = sum(1 for fork in art.divergence if fork.get("material"))
                if material:
                    c3.markdown(
                        common.chip(
                            "Two answers exist",
                            tooltip=voice.chip_tooltip("Two answers exist"),
                        ),
                        unsafe_allow_html=True,
                    )
                else:
                    c3.caption(art.tier.lower())
                filters = dict(insight.spec.filters)
                question = services.breakdown_question(
                    insight.spec.metric,
                    filters,
                    tiles.WINDOW_CONTROLS[insight.spec.window],
                    tiles.BASIS_CONTROLS[insight.spec.basis],
                )
                c4.button(
                    f"Break down {metric_label}", key=f"watch_{insight.id}",
                    on_click=common.queue_question_with_resolution,
                    args=(question, insight.source, insight.variant),
                    kwargs={"basis": tiles.BASIS_CONTROLS[insight.spec.basis]})
            except Exception:
                logging.getLogger(__name__).exception(
                    "saved insight evaluation failed: %s", insight.id)
                c2.caption("This saved insight could not be evaluated.")
                c3.caption("—")
        c5.button(f"Remove {metric_label}", key=f"unwatch_{insight.id}", on_click=_remove_saved,
                  args=(store, insight.id))


def render() -> None:
    persona = common.active_persona()
    st.title("Monitoring")
    st.markdown(_SURFACED_ROW_STYLES, unsafe_allow_html=True)
    st.caption("Movements worth your attention, ranked by business impact rather than "
               "statistical significance.")
    z = st.slider("Sensitivity (z-score threshold)", 1.0, 3.5, 1.6, 0.1)
    st.caption("Sensitivity applies to system-surfaced movements; watched cards use "
               "their saved comparison basis.")

    _watched_section(persona)
    st.divider()
    st.subheader("Surfaced by the system")
    try:
        with st.spinner("Scanning for movements…"):
            feed = services.anomaly_feed(z)
    except Exception:
        logging.getLogger(__name__).exception("anomaly feed failed")
        st.error("The monitoring feed couldn't be computed. Check the data feed, "
                 "then reload this page.")
        return

    if len(feed) == 0:
        st.info("Nothing exceeds the threshold this month. Lower the sensitivity to see "
                "smaller movements.")
        return

    with st.expander("How ranking works"):
        st.caption(
            "Each row is one distinct movement story. Priority score v2 is a unitless "
            "0–100 ranking: 45% standardized movement + 20% relative movement + 35% "
            "business scale. Business scale compares native movement with that metric's "
            "national monthly volume; low bases suppress the relative and scale terms."
        )
    for row in feed.itertuples():
        direction = row.direction if row.direction in {"up", "down", "flat"} else "flat"
        scope_label = voice.scope_text({row.dim: row.value}, persona=persona)
        with st.container(key=f"monitoring_flag_{direction}_{row.Index}"):
            c1, c2, c3, c4 = st.columns([2.4, 2.4, 1.4, 1.8])
            c1.markdown(f"**{voice.metric_name(row.metric_id)}** · {scope_label}")
            variant = sl.default_variant(row.metric_id)
            c2.caption(_movement_summary(row, persona))
            movement = common.format_native_delta(row.metric_id, variant, row.native_delta)
            priority = f"Priority {row.impact_score * 100:.0f}/100"
            c3.markdown(
                f'<div class="monitoring-priority" '
                f'aria-label="{priority}; native movement {movement}">'
                f"<strong>{priority}</strong><span>Native movement {movement}</span></div>",
                unsafe_allow_html=True,
            )
            c4.button(
                "Break this down", key=f"mon_{row.Index}",
                help=f"Open a detailed breakdown of {voice.metric_name(row.metric_id)} "
                     f"for {scope_label}.",
                on_click=common.queue_question,
                args=(services.breakdown_question(row.metric_id, {row.dim: row.value}),),
            )
            with st.expander("Why this surfaced"):
                components = getattr(row, "priority_components", {}) or {}
                st.caption(
                    f"Standardized movement: {abs(float(row.z)):.2f}σ · "
                    f"priority: {row.impact_score * 100:.1f}/100"
                )
                if components:
                    st.caption(
                        "Bounded components · standardized "
                        f"{float(components.get('standardized', 0)):.3f} · relative "
                        f"{float(components.get('relative', 0)):.3f} · business scale "
                        f"{float(components.get('business_scale', 0)):.3f}"
                    )
                st.caption(f"Formula: {services.PRIORITY_SCORE_FORMULA}")
                if bool(getattr(row, "low_base", False)):
                    reason = getattr(row, "low_base_reason", "registered low-base floor")
                    st.caption(
                        f"Low-base guard active ({reason}); percentage and scale inflation "
                        "do not contribute to priority."
                    )
                also_visible = tuple(getattr(row, "also_visible_as", ()) or ())
                if also_visible:
                    st.caption("Also visible as: " +
                               _humanize_related(also_visible, persona))
    with st.expander("All flagged movements, as a table"):
        display = feed.assign(
            scope=[
                voice.scope_text({row.dim: row.value}, persona=persona)
                for row in feed.itertuples()
            ],
            latest_display=[
                common.format_metric_value(row.metric_id, sl.default_variant(row.metric_id),
                                           row.latest)
                for row in feed.itertuples()
            ],
            trailing_display=[
                common.format_metric_value(row.metric_id, sl.default_variant(row.metric_id),
                                           row.trailing_mean)
                for row in feed.itertuples()
            ],
            native_movement=[
                common.format_native_delta(row.metric_id, sl.default_variant(row.metric_id),
                                           row.native_delta)
                for row in feed.itertuples()
            ],
            priority_score=(feed["impact_score"] * 100).round(1),
            also_visible_as=[
                _humanize_related(value, persona)
                if isinstance(value, (list, tuple)) else str(value or "")
                for value in feed.get("also_visible_as", [""] * len(feed))
            ],
        )
        st.dataframe(
            voice.humanize_table(display[["month", "metric", "scope", "latest_display", "trailing_display",
                     "native_movement", "z", "priority_score", "direction",
                     "also_visible_as"]].rename(
                columns={"latest_display": "latest", "trailing_display": "trailing",
                         "native_movement": "movement", "priority_score": "priority (0–100)"})),
            width="stretch", hide_index=True,
        )
