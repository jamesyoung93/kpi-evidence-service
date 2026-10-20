from __future__ import annotations

import pandas as pd

from streamlit.testing.v1 import AppTest


def test_scoreboard_suppresses_zero_deltas(monkeypatch):
    from views import reliability

    monkeypatch.setattr(reliability.st, "subheader", lambda *args, **kwargs: None)
    calls = []

    class Metric:
        def metric(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(reliability.st, "columns", lambda n: [Metric() for _ in range(n)])
    rates = {
        "pass_rate": 1.0,
        "reproducible_rate": 1.0,
        "correct_refusal_rate": 1.0,
        "correction_rate": 0.0,
    }
    history = pd.DataFrame([{**rates, "ts": "before"}, {**rates, "ts": "now"}])
    reliability._scoreboard({"rates": rates, "recorded_ts": "now"}, history)
    assert len(calls) == 4
    assert all(call[1]["delta"] is None for call in calls)
    assert calls[-1][1]["delta_color"] == "inverse"


def test_reliability_uses_text_statuses_and_waits_for_three_runs():
    source = __import__("inspect").getsource(
        __import__("views.reliability", fromlist=["render"]).render)
    assert 'False: "Needs review"' in source
    assert 'True: "✓"' in source
    assert "if len(hist) >= 3" in source
