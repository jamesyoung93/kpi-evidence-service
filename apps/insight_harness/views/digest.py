"""Personalized, deterministic digest of governed monthly signals."""
from __future__ import annotations

import logging
import os
import hashlib
import json
from dataclasses import replace
from typing import Mapping

import altair as alt
import streamlit as st

from harness import digest as digest_service
from harness import (digest_narrator, profiles, runtime_policy, saved_insights,
                     semantic_layer as sl, services, triage, voice)
from harness.digest_store import DigestHistoryStore, InMemoryDigestHistoryStore
from views import common


def _sparkline_chart(item) -> alt.Chart | None:
    """Twelve-point trend with only the flagged month marked."""

    frame = item.candidate.artifact.chart_df
    if frame is None or frame.empty or "month" not in frame.columns:
        return None
    series = next((column for column in frame.columns if column != "month"), None)
    if series is None:
        return None
    plot = frame[["month", series]].dropna().tail(12).rename(
        columns={series: "value"}).copy()
    if plot.empty:
        return None
    plot["month"] = plot["month"].astype(str)
    latest = plot.tail(1)
    base = alt.Chart(plot).encode(
        x=alt.X("month:O", axis=None, sort=None),
        y=alt.Y("value:Q", axis=None, scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("month:O", title="Month"),
                 alt.Tooltip("value:Q", title=series, format=",.2f")],
    )
    line = base.mark_line(color=common.C_PRIMARY, strokeWidth=2)
    marker = alt.Chart(latest).mark_circle(
        color=common.C_PRIMARY, size=70, stroke="white", strokeWidth=1.5,
    ).encode(
        x=alt.X("month:O", axis=None, sort=None),
        y=alt.Y("value:Q", axis=None, scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("month:O", title="Flagged month"),
                 alt.Tooltip("value:Q", title=series, format=",.2f")],
    )
    return alt.layer(line, marker).properties(
        height=86,
        description="Twelve-month digest sparkline; the flagged latest month is marked.",
    )


def presentation_for(item, persona):
    """Create view-only persona copy without mutating the digest item artifact."""

    candidate = item.candidate
    resolution = candidate.artifact.resolution
    presentation = voice.digest_presentation(
        persona,
        kind=candidate.kind,
        metric=candidate.metric,
        scope=candidate.filter_dict,
        facts=candidate.facts.to_dict() if candidate.facts else None,
        value=candidate.artifact.value,
        variant=resolution.variant if resolution else None,
        alternate_label=candidate.fork_label,
        alternate_value=candidate.fork_value,
        event_name=candidate.event_name,
        narration_text=item.headline if item.narration.get("text") else None,
    )
    return presentation


def _voice_presentation_hash(item, persona) -> str:
    return voice.presentation_hash(
        item.fact_hash, persona, presentation_for(item, persona))


def _item_download_json(item, persona) -> str:
    """Export the immutable artifact together with the exact displayed copy."""

    presentation = presentation_for(item, persona)
    payload = item.to_dict()
    payload["voice_presentation"] = {
        "persona": voice.resolve_profile(persona).id,
        "headline": presentation.headline,
        "detail": presentation.detail,
        "chip": presentation.chip,
        "presentation_hash": voice.presentation_hash(
            item.fact_hash, persona, presentation),
    }
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def _digest_download(artifact, persona) -> tuple[str, str]:
    """Return an export envelope and hash for the exact persona-visible digest."""

    display_items = []
    for item in artifact.items:
        presentation = presentation_for(item, persona)
        display_items.append({
            "fact_hash": item.fact_hash,
            "headline": presentation.headline,
            "detail": presentation.detail,
            "chip": presentation.chip,
            "presentation_hash": voice.presentation_hash(
                item.fact_hash, persona, presentation),
        })
    bundle = {
        "persona": voice.resolve_profile(persona).id,
        "items": display_items,
    }
    display_hash = voice.presentation_hash(artifact.fact_hash, persona, bundle)
    payload = artifact.to_dict()
    payload["voice_presentation"] = bundle | {"presentation_hash": display_hash}
    return json.dumps(payload, indent=2, sort_keys=True, default=str), display_hash


def _resolved_context(persona: str | None, scope: Mapping | None) -> tuple[str, dict]:
    selected_persona = persona or st.session_state.get("home_persona") \
        or st.session_state.get("_home_persona_last") \
        or st.session_state.get("persona") or st.session_state.get("persona_id") \
        or "executive"
    try:
        definition = profiles.persona_definition(str(selected_persona))
    except ValueError:
        definition = next((item for item in profiles.PERSONAS
                           if item.label.lower() == str(selected_persona).lower()), None)
    label = definition.label if definition else str(selected_persona)
    if scope is not None:
        selected_scope = dict(scope)
    elif isinstance(st.session_state.get("digest_scope"), Mapping):
        selected_scope = dict(st.session_state["digest_scope"])
    elif definition:
        selected_scope = dict(definition.digest_scope)
    else:
        selected_scope = dict(st.session_state.get("persona_scope", {}) or {})
    return label, selected_scope


def _session_history_store() -> InMemoryDigestHistoryStore:
    key = "_digest_session_history_store"
    store = st.session_state.get(key)
    if not isinstance(store, InMemoryDigestHistoryStore):
        store = InMemoryDigestHistoryStore()
        st.session_state[key] = store
    return store


def _narrated(item, api_key: str, model: str, persona):
    cache = st.session_state.setdefault("_digest_narration_cache", {})
    credential_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:8]
    presentation = presentation_for(item, persona)
    persona_id = voice.resolve_profile(persona).id
    # Wording is intentionally outside the fact hash, so the phrasing cache
    # must key on the pre-narration presentation contract to avoid serving
    # stale copy after a template-only release.
    key = (item.presentation_hash, persona_id, presentation.headline,
           model, credential_fingerprint)
    if key not in cache:
        allowed, policy_reason = digest_narrator.rewrite_policy(item)
        if not allowed:
            # This is a local safety decision, not a model call. In particular,
            # registered-event items must never consume the user's UI quota.
            cache[key] = item.with_narration(presentation.headline, {
                "narrator": "template", "validated": True,
                "fallback_kind": "policy", "fallback_reason": policy_reason,
            })
        else:
            used = int(st.session_state.get("_model_calls_used", 0) or 0)
            limit = runtime_policy.session_model_call_limit()
            if used >= limit:
                cache[key] = item.with_narration(presentation.headline, {
                    "narrator": "template", "validated": True,
                    "fallback_kind": "quota",
                    "fallback_reason": "session model-call limit reached",
                })
            else:
                st.session_state["_model_calls_used"] = used + 1
                cache[key] = digest_narrator.rewrite_item(
                    item, api_key=api_key, model=model,
                    template_headline=presentation.headline,
                    scope_label=voice.scope_text(item.candidate.filter_dict, persona),
                    metric_label=voice.metric_subject(item.candidate.metric),
                    persona=voice.resolve_profile(persona).label,
                )
    return cache[key]


def _queue_breakdown(item) -> None:
    """Reopen a digest item with its exact saved-question controls."""

    context = dict(item.fact_payload()["drillthrough_context"])
    st.session_state["_digest_drillthrough_context"] = context
    common.queue_question_with_resolution(
        item.breakdown_question, context["source"], context["variant"],
        digest_service._basis_code(context["basis"]),
    )


def _descriptive_watch_inputs(insights) -> tuple[list[dict], int]:
    """Return watch relevance inputs the movement scanner can evaluate truthfully."""

    eligible = [insight for insight in insights
                if insight.watched and not insight.is_stale
                and insight.question_class == triage.DESCRIPTIVE]
    skipped = sum(1 for insight in insights
                  if insight.watched and not insight.is_stale
                  and insight.question_class != triage.DESCRIPTIVE)
    return ([{
        "metric": insight.metric,
        "filters": insight.filters,
        "source": insight.source,
        "variant": insight.variant,
        "window": insight.window,
        "basis": insight.basis,
        "question_class": insight.question_class,
    } for insight in eligible], skipped)


def _render_why(rendered) -> None:
    candidate = rendered.candidate
    score_label = "Definition-fork impact" if candidate.kind == "divergence" \
        else "Priority score v2"
    st.markdown(
        f"- **{score_label}:** {candidate.impact_score:.3f}\n"
        f"- **Novelty factor:** {rendered.novelty_factor:.3f}\n"
        f"- **Ranking score:** {rendered.score:.3f}\n"
        f"- **Ranking method:** {rendered.ranking_method['description']}\n"
        f"- **Metric family:** {voice.column_name(candidate.family)}\n"
        f"- **Resolution:** "
        f"{voice.humanize_sentence(candidate.artifact.resolution.reason)}"
    )
    if candidate.facts and candidate.kind != "divergence":
        facts = candidate.facts
        st.caption(
            f"Standardized movement: {abs(facts.z):.2f}σ · bounded components: "
            f"standardized {facts.standardized_term:.3f}, relative "
            f"{facts.relative_term:.3f}, business scale "
            f"{facts.business_scale_term:.3f}."
        )
        st.caption(f"Formula: {services.PRIORITY_SCORE_FORMULA}")
        if facts.low_base:
            st.caption(
                f"Low-base guard active ({facts.low_base_reason}); relative and "
                "business-scale terms were suppressed."
            )
    if candidate.event_name:
        st.caption(f"Registered event flag: {candidate.event_name}. This is context, "
                   "not a causal attribution.")
    if candidate.fork_label:
        st.caption(f"Material alternate: {candidate.fork_label}.")
    context = rendered.fact_payload()["drillthrough_context"]
    if context["window"] or context["basis"]:
        st.caption(
            "Saved window/comparison controls are preserved for breakdown navigation "
            "only; they do not alter the standardized ranking computation."
        )


@st.dialog("Explore digest signal", width="large")
def show_digest_dialog(rendered, persona="Executive") -> None:
    """Expand a digest story through the canonical full artifact renderer."""

    presentation = presentation_for(rendered, persona)
    st.markdown(common.chip(
        presentation.chip, tooltip=voice.chip_tooltip(presentation.chip)),
        unsafe_allow_html=True)
    st.subheader(presentation.headline)
    st.caption(presentation.detail)
    common.render_answer(
        rendered.candidate.artifact,
        key=f"digest_dialog_{rendered.result_hash}",
        allow_expand=False,
        persona=persona,
    )
    with st.expander("Why this surfaced", expanded=True):
        _render_why(rendered)
    with st.expander("Evidence"):
        display_hash = _voice_presentation_hash(rendered, persona)
        st.caption(
            f"Evidence stamp · fact hash: `{rendered.fact_hash}` · displayed-copy hash: "
            f"`{display_hash}`"
        )


def _render_item(rendered, index: int, persona="Executive") -> None:
    candidate = rendered.candidate
    presentation = presentation_for(rendered, persona)
    display_hash = _voice_presentation_hash(rendered, persona)
    expand = False
    with st.container(border=True):
        st.markdown(common.chip(candidate.artifact.tier)
                    + f"&nbsp; {common.chip(presentation.chip, tooltip=voice.chip_tooltip(presentation.chip))}",
                    unsafe_allow_html=True)
        st.subheader(presentation.headline)
        st.caption(presentation.detail)
        sparkline = _sparkline_chart(rendered)
        if sparkline is not None:
            st.altair_chart(sparkline, width="stretch")
        narration = rendered.narration
        if narration.get("narrator") == "language_model":
            latency = f" · {narration['latency_ms']} ms" \
                if narration.get("latency_ms") is not None else ""
            st.caption(f"language model (validated){latency}")
        elif narration.get("fallback_kind"):
            if narration.get("fallback_kind") == "policy":
                st.caption("Event wording was kept unchanged to avoid implying causation.")
            else:
                st.caption("Templated phrasing retained because the optional rewrite was not "
                           "validated.")

        action1, action2, action3 = st.columns([1.3, 1.2, 1.0])
        action1.button("Break this down", key=f"digest_break_{index}_{rendered.result_hash}",
                       on_click=_queue_breakdown, args=(rendered,),
                       width="stretch")
        action2.download_button(
            "Download details", data=_item_download_json(rendered, persona),
            mime="application/json",
            file_name=f"digest_item_{display_hash}.json",
            key=f"digest_download_{index}_{rendered.result_hash}",
        )
        expand = action3.button(
            "Expand", key=f"digest_expand_{index}_{rendered.result_hash}",
            width="stretch", help="Open the complete governed artifact and full-size chart.")
        with st.expander("Why this surfaced"):
            _render_why(rendered)
        with st.expander("Evidence"):
            st.caption(
                f"Fact hash: `{rendered.fact_hash}` · displayed-copy hash: "
                f"`{display_hash}` · answer hash: "
                f"`{candidate.artifact.result_hash}`"
            )
    if expand:
        show_digest_dialog(rendered, persona)


def render(persona: str | None = None, scope: Mapping | None = None,
           store: DigestHistoryStore | None = None) -> None:
    st.title("Daily digest")
    st.caption(
        "The three developments most worth your attention in the current monthly data."
    )
    persona, scope = _resolved_context(persona, scope)
    st.markdown(f"**View:** {persona} · **Scope:** {voice.scope_text(scope, persona)}")

    insight_store = saved_insights.session_store(st.session_state)
    watches, skipped_watch_classes = _descriptive_watch_inputs(insight_store.all())
    if skipped_watch_classes:
        st.caption(f"{skipped_watch_classes} watched diagnostic/retrieval item(s) are excluded "
                   "from movement ranking; they remain available in Monitoring.")
    try:
        with st.spinner("Reviewing monthly signals…"):
            artifact = digest_service.build_digest(
                persona=persona, scope=scope, watches=watches,
                store=store or _session_history_store(),
            )
    except Exception:
        logging.getLogger(__name__).exception("digest computation failed")
        st.error("The digest could not be computed, so no ranking is shown.")
        return

    st.caption(f"Reviewed {artifact.scanned_series} monthly series across "
               f"{artifact.metric_families} metric families.")
    with st.expander("How ranking works"):
        st.caption(
            "The ranking compares the latest month with the prior six-month norm, then "
            "balances standardized movement, relative movement, and business scale. "
            "Low-base safeguards prevent small counts from being overstated."
        )
        st.caption(f"Formula: {services.PRIORITY_SCORE_FORMULA}")
    with st.expander("Digest evidence"):
        st.caption(f"Data `{artifact.data_version}` · governance "
                   f"`{artifact.governance_fingerprint}` · digest `{artifact.result_hash}`")

    session_key = st.session_state.get("api_key")
    deployment_key = os.environ.get("ANTHROPIC_API_KEY") \
        if runtime_policy.deployment_llm_enabled() else None
    api_key = session_key or deployment_key
    use_model = st.checkbox(
        "Use optional language-model phrasing",
        value=False, disabled=not bool(api_key), key="digest_use_model",
        help="Ranking and facts stay deterministic; only the validated wording can change.",
    )
    if not api_key:
        st.caption("Templated phrasing is active. Add a session API credential to enable "
                   "optional validated rewriting.")
    allowed_models = runtime_policy.allowed_models()
    requested_model = st.session_state.get("llm_model") or digest_narrator.DEFAULT_MODEL
    model = requested_model if requested_model in allowed_models else allowed_models[0]
    if requested_model != model:
        st.caption(f"Requested model is not allowed; using `{model}`.")

    if not artifact.items:
        st.info("No signal has enough monthly history to rank yet.")
        return
    rendered_items = tuple(
        _narrated(item, api_key, model, persona) if use_model and api_key else item
        for item in artifact.items)
    presented = replace(artifact, items=rendered_items)
    digest_download, digest_display_hash = _digest_download(presented, persona)
    st.download_button(
        "Download complete digest", data=digest_download,
        mime="application/json",
        file_name=f"digest_{digest_display_hash}.json",
        key=f"digest_complete_{digest_display_hash}",
    )
    for index, item in enumerate(rendered_items, start=1):
        _render_item(item, index, persona)
