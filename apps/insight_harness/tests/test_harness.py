"""Regression tests for pharma resolution, provenance, telemetry, and edge cases."""
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from harness import pipeline, services, triage
from harness import semantic_layer as sl


def test_resolution_clamps_unregistered_source_and_variant_with_disclosure():
    calls = sl.resolve("calls", "source_b", "dollars")
    assert calls.source == "source_a" and calls.variant == "std"
    assert "not registered" in calls.reason
    trx = sl.resolve("trx", "source_b", "bogus")
    assert trx.source == "source_b" and trx.variant == "units"
    assert "override" in trx.reason and "not registered" in trx.reason


def test_trx_variant_comparison_groups_never_compare_currency_to_volume():
    units = sl.resolve("trx", variant="units")
    assert ("variant", "normalized") in units.alternates
    assert ("variant", "dollars") not in units.alternates
    dollars = sl.resolve("trx", variant="dollars")
    assert not any(kind == "variant" for kind, _ in dollars.alternates)


def test_decomposition_source_fork_is_common_window_like_for_like():
    artifact = pipeline.answer("Which regions account for the NRx change?")
    fork = next(item for item in artifact.divergence if item["fork"] == "source: source_b")
    assert fork["common_window"] == ["2026-02", "2026-05"]
    assert artifact.extras["coverage_gaps"]
    source_a = sl.load_fact("source_a")
    source_b = sl.load_fact("source_b")
    expected_base = (source_a[source_a["month"] == "2026-05"]["nrx"].sum()
                     - source_a[source_a["month"] == "2026-02"]["nrx"].sum())
    expected_alt = (source_b[source_b["month"] == "2026-05"]["nrx"].sum()
                    - source_b[source_b["month"] == "2026-02"]["nrx"].sum())
    assert fork["base_value"] == pytest.approx(expected_base)
    assert fork["value"] == pytest.approx(expected_alt)


def test_event_overlap_uses_actual_comparison_window_metric_and_scope():
    national = pipeline.answer("Which regions account for the TRx change?")
    assert {event["id"] for event in national.extras["overlapping_events"]} == {
        "competitor_launch"
    }
    east = pipeline.answer("Which regions account for the TRx change in the East region?")
    assert "overlapping_events" not in east.extras
    calls = pipeline.answer("Which regions account for the calls change?")
    assert "overlapping_events" not in calls.extras


def test_causal_repeated_scope_language_is_allowed_without_narrowing():
    artifact = pipeline.answer(
        "What was the impact of the formulary win in the South for Medicare Part D?")
    assert artifact.tier == "Hypothesis"
    assert artifact.extras["effective_scope"] == {
        "region": "South", "payer_channel": "Medicare Part D"
    }


def test_artifact_json_is_complete_for_every_engine():
    questions = (
        "What is TRx in the West region?",
        "Which specialties account for the TRx change?",
        "List whitespace HCPs with no activity",
        "What was the impact of the speaker program?",
        "Forecast TRx for next quarter",
    )
    for question in questions:
        payload = json.loads(pipeline.answer(question).to_json())
        for key in ("question", "tier", "engine", "code", "resolution", "caveats",
                    "divergence", "extras", "data_version", "result_hash"):
            assert key in payload, f"{key} missing for {question!r}"
        assert "intent" in payload["extras"]


def test_ratio_and_causal_reproduction_recipes_match_executed_math():
    share = pipeline.answer("What is TRx market share in the West region?")
    assert "aggregate_metric" in share.code
    assert "['trx_units'].sum()" not in share.code
    causal = pipeline.answer("What was the impact of the competitor launch in West Cardiology?")
    assert "treated_growth" in causal.code and "control_growth" in causal.code
    assert "did_pct = treated_growth - control_growth" in causal.code
    assert str(causal.extras["effective_scope"]) in causal.code


def test_feedback_defaults_to_question_hash_and_omits_raw_text(monkeypatch):
    monkeypatch.delenv("INSIGHT_HARNESS_LOG_RAW_QUESTIONS", raising=False)
    artifact = pipeline.answer("What is TRx in the West region?")
    services.log_feedback(artifact, "correct")
    record = json.loads(services.FEEDBACK_LOG.read_text().strip())
    assert "question" not in record
    assert len(record["question_hash"]) == 16


def test_feedback_question_hash_is_keyed_hmac(monkeypatch):
    monkeypatch.setenv("INSIGHT_HARNESS_TELEMETRY_HASH_KEY", "test-only-secret")
    artifact = pipeline.answer("What is TRx in the West region?")
    services.log_feedback(artifact, "correct")
    record = json.loads(services.FEEDBACK_LOG.read_text(encoding="utf-8"))
    expected = hmac.new(b"test-only-secret", artifact.question.encode("utf-8"),
                        hashlib.sha256).hexdigest()[:16]
    assert record["question_hash"] == expected
    assert record["question_hash"] != hashlib.sha256(
        artifact.question.encode("utf-8")).hexdigest()[:16]


def test_feedback_raw_question_is_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("INSIGHT_HARNESS_LOG_RAW_QUESTIONS", "true")
    artifact = pipeline.answer("What is TRx in the West region?")
    services.log_feedback(artifact, "wrong", "test")
    record = json.loads(services.FEEDBACK_LOG.read_text().strip())
    assert record["question"] == artifact.question


def test_feedback_appends_are_thread_and_process_lock_safe():
    artifact = pipeline.answer("What is TRx in the West region?")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: services.log_feedback(artifact, "correct", str(index)),
                      range(20)))
    lines = services.FEEDBACK_LOG.read_text().splitlines()
    assert len(lines) == 20
    assert len({json.loads(line)["note"] for line in lines}) == 20


def test_feedback_history_skips_a_corrupt_line():
    good = {"ts": "2026-07-15T00:00:00+00:00", "question_hash": "abc",
            "class": "Descriptive", "tier": "Verified", "engine": "descriptive",
            "result_hash": "result", "data_version": "version",
            "verdict": "correct", "note": ""}
    services.FEEDBACK_LOG.write_text(
        json.dumps(good) + "\n" + '{"ts":' + "\n" + json.dumps(good | {"verdict": "wrong"}) + "\n")
    history = services.feedback_history()
    assert len(history) == 2
    assert set(history["verdict"]) == {"correct", "wrong"}


def test_eval_history_concurrent_appends_are_atomic_and_corruption_tolerant():
    pipeline.EVAL_HISTORY.write_text('{"ts":\n', encoding="utf-8", newline="\n")
    result = pd.DataFrame([
        {"tier": "Verified", "pass": True, "reproducible": True},
        {"tier": "Abstained", "pass": True, "reproducible": True},
    ])
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: pipeline._record_run(result), range(20)))

    history = pipeline.eval_history()
    assert len(history) == 20
    assert (history["pass_rate"] == 1.0).all()
    assert len(pipeline.EVAL_HISTORY.read_text(encoding="utf-8").splitlines()) == 21


def test_parser_is_pharma_only_and_supports_common_synonyms():
    assert triage.parse("How many prescriptions were written?").metric == "trx"
    assert triage.parse("What is call plan attainment?").metric == "call_attainment"
    assert triage.parse("Show new-to-brand volume").metric == "nbrx"
    legacy = triage.parse("What is revenue for Enterprise?")
    assert legacy.question_class == triage.OUT_OF_SCOPE


def test_source_a_account_grain_and_source_b_panel_grain_are_honest():
    source_a, source_b = sl.load_fact("source_a"), sl.load_fact("source_b")
    assert "account_id" in source_a and "account_id" not in source_b
    assert not source_a.duplicated(sl.SOURCES["source_a"]["grain"]).any()
    assert not source_b.duplicated(sl.SOURCES["source_b"]["grain"]).any()
    for field_metric in ("calls", "call_plan", "samples", "speaker_attendance", "new_writers"):
        assert sl.METRICS[field_metric]["sources"] == ["source_a"]


def test_accounts_reconcile_to_fact_and_whitespace_is_true_no_activity():
    accounts, frame = sl.load_accounts(), sl.load_fact("source_a")
    last12 = sl.months()[-12:]
    expected = frame[frame["month"].isin(last12)].groupby("account_id")["trx_units"].sum()
    actual = accounts.set_index("account_id")["trx_ttm"]
    assert (actual - expected).abs().max() < 0.0011
    whitespace = pipeline.answer("List whitespace HCPs with no activity").table
    assert len(whitespace) > 0
    assert (whitespace["calls_90d"] == 0).all()
    assert (whitespace["months_since_activity"] >= 3).all()


def test_new_writer_metric_is_derived_from_first_observed_positive_trx():
    frame = sl.load_fact("source_a").sort_values(["account_id", "month"])
    warmup = frame["month"].min()
    observed = frame[(frame["trx_units"] > 0) & (frame["month"] != warmup)] \
        .groupby("account_id").head(1).index
    # Incumbents first observed in the warm-up month are not new writers later.
    incumbents = set(frame[(frame["trx_units"] > 0) & (frame["month"] == warmup)]["account_id"])
    observed = [idx for idx in observed if frame.loc[idx, "account_id"] not in incumbents]
    expected = pd.Series(0.0, index=frame.index)
    expected.loc[frame["month"] == warmup] = float("nan")
    expected.loc[observed] = 1
    pd.testing.assert_series_equal(frame["new_writers"], expected, check_names=False)
