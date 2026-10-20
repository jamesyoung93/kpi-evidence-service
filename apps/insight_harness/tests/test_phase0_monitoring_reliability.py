"""Focused UI contracts for the Phase 0 monitoring and reliability polish."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from harness import pipeline, services
from harness import semantic_layer as sl


APP = str(Path(__file__).parent.parent / "app.py")


def _app() -> AppTest:
    return AppTest.from_file(APP, default_timeout=90)


def _button(at: AppTest, label: str):
    return next(button for button in at.button if label in (button.label or ""))


def test_monitoring_uses_exact_1_6_default_and_visually_prioritizes_rows(monkeypatch):
    thresholds: list[float] = []
    feed = pd.DataFrame([{
        "month": "2026-06",
        "metric": "TRx",
        "scope": "region=West",
        "latest": 1250.0,
        "trailing_mean": 1000.0,
        "z": 2.4,
        "impact": 250.0,
        "native_delta": 250.0,
        "relative_change": 0.25,
        "impact_score": 0.82,
        "value_format": "number",
        "direction": "up",
        "status": "flagged",
        "metric_id": "trx",
        "dim": "region",
        "value": "West",
    }])

    def fake_anomaly_feed(threshold=2.0):
        thresholds.append(threshold)
        return feed

    monkeypatch.setattr(services, "anomaly_feed", fake_anomaly_feed)
    at = _app().run()
    at.radio(key="nav").set_value("Monitoring").run()

    sensitivity = next(slider for slider in at.slider
                       if slider.label == "Sensitivity (z-score threshold)")
    assert sensitivity.value == 1.6
    assert thresholds[-1] == 1.6
    markup = " ".join(str(item.value) for item in at.markdown)
    assert "border-left-color: #0E7C7B" in markup
    assert "text-align: right" in markup
    assert "Priority 82/100" in markup
    assert "Native movement +250" in markup
    breakdown = _button(at, "Break this down")
    assert breakdown.help == "Open a detailed breakdown of TRx for West."

    sensitivity.set_value(1.7).run()
    assert thresholds[-1] == 1.7


def test_reliability_auto_runs_once_per_data_version_then_explicitly_reruns(monkeypatch):
    calls = {"golden": 0}
    result = pd.DataFrame([
        {"id": "G1", "question": "Measured value", "class": "Descriptive",
         "tier": "Verified", "pass": True, "reproducible": True,
         "detail": "matches"},
        {"id": "G2", "question": "Unsupported forecast", "class": "Refusal",
         "tier": "Abstained", "pass": True, "reproducible": True,
         "detail": "correct refusal"},
    ])
    history = pd.DataFrame([{
        "ts": "2026-07-15T12:00:00+00:00",
        "data_version": sl.data_version(),
        "pass_rate": 1.0,
        "reproducible_rate": 1.0,
        "correct_refusal_rate": 1.0,
        "correction_rate": 0.5,
        "n": 2,
    }])
    feedback = pd.DataFrame([
        {"ts": "one", "question_hash": "safe-1", "question": "secret text",
         "class": "Descriptive", "tier": "Verified", "engine": "basic",
         "result_hash": "r1", "data_version": sl.data_version(),
         "verdict": "correct", "note": "private note"},
        {"ts": "two", "question_hash": "safe-2", "question": "more secret text",
         "class": "Descriptive", "tier": "Verified", "engine": "basic",
         "result_hash": "r2", "data_version": sl.data_version(),
         "verdict": "wrong", "note": "another private note"},
    ])

    def fake_run_golden(record=True):
        calls["golden"] += 1
        return result.copy()

    monkeypatch.setattr(pipeline, "run_golden", fake_run_golden)
    monkeypatch.setattr(pipeline, "eval_history", lambda: history.copy())
    monkeypatch.setattr(services, "feedback_history", lambda: feedback.copy())

    at = _app().run()
    at.radio(key="nav").set_value("Reliability").run()

    assert not at.exception
    assert calls["golden"] == 1
    values = {metric.label: metric.value for metric in at.metric}
    assert values == {
        "Pass rate": "100%",
        "Reproducible": "100%",
        "Correct refusals": "100%",
        "Correction rate": "50.0%",
    }
    cache = at.session_state["_reliability_reports_by_data_version"]
    assert sl.data_version() in cache
    assert "Automatically checked on first visit" in " ".join(
        str(item.value) for item in at.caption)

    feedback_tables = [item.value for item in at.dataframe
                       if "verdict" in item.value.columns]
    assert len(feedback_tables) == 1
    assert "question_hash" in feedback_tables[0].columns
    assert "question" not in feedback_tables[0].columns
    assert "note" not in feedback_tables[0].columns

    at.run()
    assert calls["golden"] == 1
    _button(at, "Run accuracy check again").click().run()
    assert calls["golden"] == 2
