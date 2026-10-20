"""Content-addressed answer artifacts must hash every deterministic result fact."""
from __future__ import annotations

import json

import pandas as pd

from harness.provenance import AnswerArtifact


def _artifact() -> AnswerArtifact:
    return AnswerArtifact(
        question="Trend TRx by month",
        question_class="Descriptive",
        tier="Verified",
        engine="descriptive",
        headline="TRx latest: 100.0",
        value=100.0,
        table=pd.DataFrame({"month": ["2026-05", "2026-06"], "TRx": [90.0, 100.0]}),
        chart_df=pd.DataFrame({"month": ["2026-05", "2026-06"], "TRx": [90.0, 100.0]}),
        resolution={"metric": "trx", "variant": "units", "source": "source_a"},
        caveats=["Monthly synthetic data."],
        divergence=[],
        extras={
            "comparison": {"reference_value": 90.0, "delta": 10.0},
            "translation": {"translator": "rules", "latency_ms": 1},
        },
        data_version="data-v1",
        created_at="2026-07-15T10:00:00+00:00",
    )


def test_runtime_metadata_does_not_change_the_result_hash():
    first = _artifact()
    second = _artifact()
    second.created_at = "2026-07-16T10:00:00+00:00"
    second.extras["translation"] = {"translator": "llm", "latency_ms": 999}
    second.extras["analyst_reviewed"] = "2026-07-16T11:00:00+00:00"
    assert first.result_hash == second.result_hash


def test_every_deterministic_result_axis_changes_the_hash():
    baseline = _artifact().result_hash

    chart_changed = _artifact()
    chart_changed.chart_df.loc[1, "TRx"] = 101.0
    assert chart_changed.result_hash != baseline

    resolution_changed = _artifact()
    resolution_changed.resolution["source"] = "source_b"
    assert resolution_changed.result_hash != baseline

    version_changed = _artifact()
    version_changed.data_version = "data-v2"
    assert version_changed.result_hash != baseline

    caveat_changed = _artifact()
    caveat_changed.caveats.append("Additional limitation.")
    assert caveat_changed.result_hash != baseline

    comparison_changed = _artifact()
    comparison_changed.extras["comparison"]["delta"] = 11.0
    assert comparison_changed.result_hash != baseline


def test_artifact_json_is_portable_strict_json():
    artifact = _artifact()
    artifact.chart_df.loc[0, "TRx"] = float("nan")
    payload = json.loads(artifact.to_json())
    assert payload["chart"][0]["TRx"] is None
    assert payload["result_hash"] == artifact.result_hash
