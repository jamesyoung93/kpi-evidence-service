"""Question workspace used on Home.

The KPI band lives in :mod:`views.home`; this module keeps the interrogable
question surface, session history, override disclosure, and shared artifact
renderer in one place.
"""
from __future__ import annotations

import logging
import os
import hashlib
from datetime import datetime

import streamlit as st

from harness import pipeline, runtime_policy, triage, voice
from harness import semantic_layer as sl
from views import common

_BASIS_CODES = {"prior month": "prior_month", "prior quarter": "prior_quarter",
                "same month last year": "yoy"}

SUGGESTIONS = {
    "Find": ["List whitespace HCPs with no activity",
             "Top 15 accounts by TRx"],
    "Measure": ["What is TRx in the West region?",
                "Trend NBRx by month in Cardiology"],
    "Break down": ["Which specialties account for the TRx change?",
                   "Which payer channels account for the NRx change?"],
    "Test an event": ["What was the impact of the speaker program in the East?",
                      "What was the impact of the competitor launch in West Cardiology?"],
}


def _remember(art) -> None:
    history = st.session_state.setdefault("history", [])
    entry_id = f"{art.result_hash}:{art.question}"
    now = datetime.now().strftime("%H:%M")
    for i, e in enumerate(history):
        if e["id"] == entry_id:  # re-asked: refresh, don't duplicate
            e["ts"] = now
            history.append(history.pop(i))
            return
    override = bool(art.resolution and "override" in art.resolution.reason)
    history.append({"id": entry_id, "question": art.question, "tier": art.tier,
                    "headline": art.headline, "ts": now, "hash": art.result_hash,
                    "artifact": art, "override": override})
    del history[:-50]


def _replay(entry_id: str) -> None:
    st.session_state["replay_key"] = entry_id


def _count_translation(art) -> None:
    """Session counters for the Reliability page: validated vs rejected
    language-model translations."""
    tr = art.extras.get("translation", {})
    stats = st.session_state.setdefault("llm_stats", {"validated": 0, "rejected": 0})
    if tr.get("translator") == "llm":
        stats["validated"] += 1
    elif tr.get("fallback_kind") == "rejected":
        stats["rejected"] += 1


def _model_context() -> tuple[str | None, str, bool]:
    """Resolve a policy-approved key/model and whether the quota was exhausted."""

    session_key = st.session_state.get("api_key")
    deployment_key = os.environ.get("ANTHROPIC_API_KEY") \
        if runtime_policy.deployment_llm_enabled() else None
    key = session_key or deployment_key
    used = int(st.session_state.get("_model_calls_used", 0))
    exhausted = bool(key and used >= runtime_policy.session_model_call_limit())
    models = runtime_policy.allowed_models()
    model = st.session_state.get("llm_model")
    if model not in models:
        model = models[0]
    return (None if exhausted else key), model, exhausted


def _history_rail() -> None:
    history = st.session_state.get("history", [])
    st.markdown("**This session**")
    if not history:
        st.caption("Questions you ask this session appear here — click one to bring "
                   "its answer back.")
        return
    for i, e in reversed(list(enumerate(history))):
        label = e["question"] if len(e["question"]) <= 57 else e["question"][:57] + "…"
        st.button(label, key=f"hist_{i}", on_click=_replay, args=(e["id"],), width="stretch")
        meta = f"{e['ts']}{' · override' if e['override'] else ''}"
        st.markdown(f"{common.chip(e['tier'])}<span style='font-size:0.75rem;"
                    f"color:#6B7280'>{meta}</span>", unsafe_allow_html=True)
        artifact = e["artifact"]
        persona = common.active_persona()
        if e["tier"] == "Abstained":
            headline = voice.refusal_presentation(
                artifact, persona=persona).lead
        elif artifact.engine == "cohort":
            headline = voice.cohort_presentation(
                artifact, persona=persona).headline
        elif artifact.resolution is not None:
            intent = artifact.extras.get("intent")
            headline = voice.tile_presentation(
                artifact,
                persona=persona,
                scope=getattr(intent, "filters", {}) if intent is not None else {},
            ).headline
        else:
            headline = voice.humanize_sentence(
                e["headline"].removeprefix("Declined: "), persona)
        st.caption(headline if len(headline) <= 80 else headline[:80] + "…")


def _variant_label(variant: str, metric: str | None) -> str:
    if variant == "governed default":
        return "Use the standard definition"
    if metric in sl.METRICS and variant in sl.METRICS[metric]["variants"]:
        return voice.variant_name(metric, variant)
    owner = next(
        (metric_id for metric_id, definition in sl.METRICS.items()
         if variant in definition["variants"]),
        None,
    )
    return voice.variant_name(owner, variant) if owner else voice.column_name(variant)


def render_workspace() -> None:
    """Render the question box and history rail without a page title."""
    col_main, col_rail = st.columns([5, 2], gap="large")

    with col_main:
        # widget-keyed state is dropped when another page renders; restore the
        # question so the Ask context survives a navigation round-trip
        if "ask_q" not in st.session_state and "_ask_q_last" in st.session_state:
            st.session_state["ask_q"] = st.session_state["_ask_q_last"]
        q = st.text_input("Question", key="ask_q", on_change=common.clear_replay,
                          placeholder="e.g., Which specialties account for the TRx change?")
        st.session_state["_ask_q_last"] = q

        history = st.session_state.get("history", [])
        with st.expander("Try asking", expanded=not history and not q):
            cols = st.columns(len(SUGGESTIONS))
            for col, (group, questions) in zip(cols, SUGGESTIONS.items()):
                col.markdown(f"**{group}**")
                for s in questions:
                    col.button(s, key=f"sugg_{s}", on_click=common.queue_question,
                               args=(s,), width="stretch")

        # answer-affecting inputs survive navigation (widget-keyed state is
        # dropped when another page renders) and exit replay mode when changed
        for k in ("ask_src", "ask_var", "ask_basis"):
            if k not in st.session_state and f"_{k}_last" in st.session_state:
                st.session_state[k] = st.session_state[f"_{k}_last"]

        with st.expander("Override the governed defaults (disclosed as a user override)"):
            oc1, oc2 = st.columns(2)
            src = oc1.selectbox("Source", ["governed default"] + list(sl.SOURCES),
                                key="ask_src", on_change=common.clear_replay,
                                format_func=lambda s: "Use the standard source"
                                if s == "governed default"
                                else sl.SOURCES[s]["name"])
            variants = sorted({v for metric in sl.METRICS.values() for v in metric["variants"]})
            parsed_metric = triage.parse(q).metric if q else None
            var = oc2.selectbox(
                "Metric definition", ["governed default"] + variants,
                key="ask_var", on_change=common.clear_replay,
                format_func=lambda value: _variant_label(value, parsed_metric))

        basis = None
        if q and triage.parse(q).question_class == triage.DIAGNOSTIC:
            pick_basis = st.selectbox(
                "Compare against", ["as asked (default: prior quarter)"] + list(_BASIS_CODES),
                key="ask_basis", on_change=common.clear_replay,
                help="The comparison month the breakdown measures the change from. "
                     "The choice is disclosed in the headline.")
            basis = _BASIS_CODES.get(pick_basis)
        for k in ("ask_src", "ask_var", "ask_basis"):
            if k in st.session_state:
                st.session_state[f"_{k}_last"] = st.session_state[k]

        replay_id = st.session_state.get("replay_key")
        entry = next((e for e in history if e["id"] == replay_id), None) if replay_id else None
        if entry is not None:
            st.caption(f"From this session's history — asked at {entry['ts']}. "
                       "Edit the question above to ask something new.")
            common.render_answer(entry["artifact"], key="replay")
        elif q:
            api_key, model, exhausted = _model_context()
            if exhausted:
                st.warning("This session's model-call allowance is exhausted. The governed "
                           "built-in parser is handling questions without an external call.")
            # governance settings are part of "identical question + config":
            # a config change must invalidate the memoized answer
            gov = (sl.materiality(),
                   tuple((m, sl.default_variant(m)) for m in sl.METRICS))
            credential_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:8] \
                if api_key else None
            params = (q, src, var, basis, credential_fingerprint,
                      model, gov, sl.data_version())
            cached = st.session_state.get("_answer_cache")
            if cached and cached[0] == params:
                art = cached[1]  # same request: don't recompute (or re-call the LLM)
            else:
                try:
                    with st.spinner("Computing…"):
                        art = pipeline.answer(q,
                                              None if src == "governed default" else src,
                                              None if var == "governed default" else var,
                                              api_key=api_key,
                                              model=model,
                                              basis=basis)
                    if api_key:
                        st.session_state["_model_calls_used"] = \
                            int(st.session_state.get("_model_calls_used", 0)) + 1
                    st.session_state["_answer_cache"] = (params, art)
                    _count_translation(art)
                except Exception:
                    question_hash = hashlib.sha256(q.encode()).hexdigest()[:12]
                    logging.getLogger(__name__).exception(
                        "answer computation failed: question_hash=%s", question_hash)
                    art = None
                    st.error("This answer couldn't be computed, so nothing is shown rather "
                             "than an unverified number. Rephrase the question, or pick a "
                             "suggestion above.")
            if art is not None:
                _remember(art)
                common.render_answer(art)
        else:
            st.info("Ask a question about a governed metric, or pick a suggestion above.")

    with col_rail:
        _history_rail()


def render() -> None:
    """Standalone compatibility renderer; the application routes to Home."""
    st.title("Home")
    st.caption("Ask about governed metrics in plain language. Every answer carries its own "
               "provenance; a question that can't be answered reliably gets a scoped refusal "
               "with a suggested reframe.")
    render_workspace()
