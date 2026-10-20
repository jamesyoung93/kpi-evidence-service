"""Reliability: the system's public track record — accuracy, reproducibility,
correct refusals, and user corrections, with trend across recorded runs."""
from __future__ import annotations

import logging

import altair as alt
import pandas as pd
import streamlit as st

from harness import pipeline, services
from harness import semantic_layer as sl

_SERIES = ["pass", "reproducible", "correct refusals", "corrections"]
_SERIES_COLORS = ["#2a78d6", "#B07C0E", "#6B7280", "#e34948"]  # validated set
_REPORT_CACHE_KEY = "_reliability_reports_by_data_version"
_RERUN_REQUEST_KEY = "_reliability_rerun_requested"


def _trend_chart(hist: pd.DataFrame) -> None:
    df = hist.rename(columns={"pass_rate": "pass", "reproducible_rate": "reproducible",
                              "correct_refusal_rate": "correct refusals",
                              "correction_rate": "corrections"})
    df = df.assign(run=range(1, len(df) + 1))
    long = df.melt(["run"], _SERIES, var_name="measure", value_name="rate").dropna()
    st.altair_chart(
        alt.Chart(long).mark_line(point=True, strokeWidth=2).encode(
            x=alt.X("run:O", title="run"),
            y=alt.Y("rate:Q", title=None, axis=alt.Axis(format="%"),
                    scale=alt.Scale(domain=[0, 1])),
            color=alt.Color("measure:N",
                            scale=alt.Scale(domain=_SERIES, range=_SERIES_COLORS),
                            legend=alt.Legend(orient="top", title=None)),
            tooltip=["run:O", "measure:N", alt.Tooltip("rate:Q", format=".0%")],
        ).properties(height=220), width="stretch")


def _rates(result: pd.DataFrame) -> dict[str, float]:
    refusals = result[result["tier"] == "Abstained"]
    feedback = services.feedback_history()
    votes = feedback[feedback["verdict"].isin(["correct", "wrong"])] \
        if len(feedback) else feedback
    return {
        "pass_rate": float(result["pass"].mean()),
        "reproducible_rate": float(result["reproducible"].mean()),
        "correct_refusal_rate": (float(refusals["pass"].mean())
                                 if len(refusals) else 1.0),
        "correction_rate": (float((votes["verdict"] == "wrong").mean())
                            if len(votes) else 0.0),
    }


def _run_and_cache(data_version: str) -> dict:
    result = pipeline.run_golden()
    history = pipeline.eval_history()
    matching = history[history["data_version"] == data_version] if len(history) else history
    recorded_ts = matching.iloc[-1]["ts"] if len(matching) else None
    report = {"result": result, "recorded_ts": recorded_ts, "rates": _rates(result)}
    reports = dict(st.session_state.get(_REPORT_CACHE_KEY, {}))
    reports[data_version] = report
    st.session_state[_REPORT_CACHE_KEY] = reports
    # Keep the original keys during the transition for saved browser sessions
    # and external test harnesses that inspect the current report directly.
    st.session_state["golden"] = result
    st.session_state["golden_ts"] = recorded_ts
    return report


def _previous_run(history: pd.DataFrame, recorded_ts: str | None):
    if recorded_ts is None or len(history) == 0:
        return None
    matches = history.index[history["ts"] == recorded_ts].tolist()
    return history.iloc[matches[-1] - 1] if matches and matches[-1] >= 1 else None


def _scoreboard(report: dict, history: pd.DataFrame) -> None:
    rates = report["rates"]
    previous = _previous_run(history, report.get("recorded_ts"))

    def delta(key: str) -> str | None:
        if previous is None:
            return None
        change = rates[key] - previous[key]
        # A zero-point delta is first-run noise and, for correction rate, can
        # look like a red regression even though nothing changed.
        return None if abs(change) < 0.0005 else f"{change * 100:+.1f}pp"

    st.subheader("Current reliability scorecard")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pass rate", f"{rates['pass_rate'] * 100:.0f}%",
              delta=delta("pass_rate"))
    c2.metric("Reproducible", f"{rates['reproducible_rate'] * 100:.0f}%",
              delta=delta("reproducible_rate"))
    c3.metric("Correct refusals", f"{rates['correct_refusal_rate'] * 100:.0f}%",
              delta=delta("correct_refusal_rate"))
    c4.metric("Correction rate", f"{rates['correction_rate'] * 100:.1f}%",
              delta=delta("correction_rate"), delta_color="inverse",
              help="Share of correctness feedback that flagged an answer as wrong.")


def _request_rerun() -> None:
    st.session_state[_RERUN_REQUEST_KEY] = True


def render() -> None:
    st.title("Reliability")
    st.caption("The system's track record, so you can decide how much to trust it: a "
               "standing question set with independently computed expected answers, "
               "checked automatically once per data version and available to rerun here. "
               "Declining correctly is scored, and every question runs twice and must "
               "produce identical result hashes.")

    data_version = sl.data_version()
    reports = st.session_state.get(_REPORT_CACHE_KEY, {})
    report = reports.get(data_version)
    rerun_requested = bool(st.session_state.pop(_RERUN_REQUEST_KEY, False))
    if report is None or rerun_requested:
        try:
            with st.spinner("Running every question twice…"):
                report = _run_and_cache(data_version)
        except Exception:
            logging.getLogger(__name__).exception("accuracy check failed")
            qualifier = "automatic " if not rerun_requested else ""
            st.error(f"The {qualifier}accuracy check couldn't finish. Check the data feed, "
                     "then run it again.")

    hist = pipeline.eval_history()
    if report is not None:
        _scoreboard(report, hist)
        st.caption(f"Automatically checked on first visit for data version `{data_version}`; "
                   "the report is cached for this session.")

    st.button("Run accuracy check again", type="secondary", on_click=_request_rerun)

    if report is not None:
        display = report["result"].copy()
        for column in ("pass", "reproducible"):
            if column in display:
                display[column] = display[column].map(
                    {True: "✓", False: "Needs review"}).fillna("—")
        st.dataframe(display, width="stretch", hide_index=True)

    if len(hist) >= 3:
        st.markdown("**Trend across recorded runs**")
        _trend_chart(hist)
    elif len(hist) >= 2:
        st.caption("Reliability trend appears after three recorded runs.")

    st.divider()
    st.markdown("**Correction record** — answers users flagged, reviewed against the "
                "governed definitions")
    st.caption("Votes are anonymous session-deduplicated feedback telemetry, not an "
               "authenticated quality measure.")
    fb = services.feedback_history()
    votes = fb[fb["verdict"].isin(["correct", "wrong"])] if len(fb) else fb
    if len(votes):
        st.caption(f"{len(votes)} correctness vote(s) recorded. Raw question text is never "
                   "shown on this public page.")
        safe_columns = [column for column in (
            "ts", "question_hash", "class", "tier", "engine", "result_hash",
            "data_version", "verdict",
        ) if column in votes.columns]
        st.dataframe(votes[safe_columns].tail(20), width="stretch", hide_index=True)
    else:
        st.info("No feedback yet. Use 👍 / 🚩 on any answer to build the record.")

    st.divider()
    stats = st.session_state.get("llm_stats", {"validated": 0, "rejected": 0})
    st.markdown("**Language-model translation, this session**")
    st.caption(f"{stats['validated']} translation(s) validated against the registry · "
               f"{stats['rejected']} rejected and answered by the built-in parser instead. "
               "Every translation is checked before anything runs; a rejected one never "
               "reaches an engine.")
