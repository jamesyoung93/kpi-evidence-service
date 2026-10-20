"""Causal Studio: design, assumption checks, estimate, sensitivity, and review."""
from __future__ import annotations

import logging

import streamlit as st

from harness import pipeline, triage, voice
from harness import semantic_layer as sl
from harness.engines.causal_advisor import DESIGN_METRICS
from views import common


def render() -> None:
    st.title("Causal Studio")
    st.caption("Attribution is a designed analysis, not a narrative. The studio matches your "
               "question to a registered event, proposes a study design, computes the assumption "
               "checks, and keeps the result Hypothesis-tier. Authenticated analyst sign-off "
               "is recorded as provenance; it does not promote the tier.")

    # widget-keyed state is dropped when another page renders; restore the
    # selection so the studio keeps its context across navigation
    if "studio_event" not in st.session_state and "_studio_event_last" in st.session_state:
        st.session_state["studio_event"] = st.session_state["_studio_event_last"]
    eid = st.selectbox("Registered event", list(sl.EVENTS), key="studio_event",
                       format_func=lambda k: sl.EVENTS[k]["name"])
    st.session_state["_studio_event_last"] = eid
    with st.expander("Event registry"):
        for ev in sl.EVENTS.values():
            scope = voice.scope_text(ev["scope"], default_scope={})
            metrics = ", ".join(voice.metric_name(item) for item in ev["metrics"])
            st.markdown(f"- **{ev['name']}** — from {ev['start']}, scope "
                        f"{scope}, "
                        f"metrics: {metrics}. {voice.humanize_sentence(ev['notes'])}")

    autorun = st.session_state.pop("studio_autorun", False)
    metric = st.session_state.pop("studio_metric", None)
    if st.button("Propose a design", type="primary") or autorun:
        ev = sl.EVENTS[eid]
        registered = [candidate for candidate in ev.get("metrics", ())
                      if candidate in DESIGN_METRICS]
        metric = metric if metric in registered else registered[0]
        question = f"What was the impact of {ev['name']} on {sl.METRICS[metric]['label']}?"
        intent = triage.Intent(question, triage.CAUSAL, metric, {}, event_id=eid)
        try:
            with st.spinner("Computing the design and its checks…"):
                st.session_state["studio_art"] = pipeline.answer_intent(intent)
        except Exception:
            logging.getLogger(__name__).exception("design proposal failed: %s", eid)
            st.error("This design couldn't be computed, so nothing is shown rather than "
                     "an unchecked estimate. Pick another event, or check the data feed.")

    art = st.session_state.get("studio_art")
    if art is not None and art.extras["intent"].event_id == eid:
        common.render_answer(art, key="studio")
    else:
        st.info("Pick a registered event and propose a design — the studio computes the "
                "assumption checks, the estimate, and its sensitivity.")
