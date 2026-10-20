"""Pharma windows, dimensions, ratios, sources, causal scopes, and governance."""
import json

import pandas as pd
import pytest

from harness import pipeline, services, triage
from harness import semantic_layer as sl
from harness.llm_translator import TranslationError, _validate
from harness.engines import decomposition


def _raw(**overrides):
    payload = {
        "question_class": "Descriptive", "metric": "trx", "filters": {},
        "trend": False, "dim_breakdown": None, "event_id": None,
        "template": None, "window": None, "compare_basis": None,
        "basket_id": None, "reason": "",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_quarter_window_aggregates_raw_trx():
    artifact = pipeline.answer("What was TRx in Q1 2026 in the West region?")
    frame = sl.load_fact("source_a")
    expected = frame[(frame["region"] == "West")
                     & frame["month"].isin(["2026-01", "2026-02", "2026-03"])]["trx_units"].sum()
    assert artifact.value == pytest.approx(expected)
    assert "Q1 2026" in artifact.headline


def test_trend_window_and_yoy_reference_are_structured():
    artifact = pipeline.answer("Trend TRx last 6 months vs same month last year")
    assert artifact.chart_df["month"].tolist() == sl.months()[-6:]
    comparison = artifact.extras["comparison"]
    assert comparison["basis"] == "yoy" and comparison["available"] is True
    assert comparison["reference_month"] == "2025-06"


def test_lagged_source_refuses_uncovered_month_and_discloses_partial_quarter():
    refused = pipeline.answer("What was TRx in June 2026?", source="source_b")
    assert refused.tier == "Abstained"
    assert "Projected retail panel" in refused.headline
    partial = pipeline.answer("What was TRx in Q2 2026?", source="source_b")
    assert partial.tier == "Verified" and "partial" in partial.headline
    assert any("clamped" in caveat for caveat in partial.caveats)


@pytest.mark.parametrize("question", [
    "What is TRx in E-CAR-01 and Endocrinology?",
    "Trend TRx in E-CAR-01 and Endocrinology",
    "Which payer channels account for the TRx change in E-CAR-01 and Endocrinology?",
])
def test_structurally_empty_scopes_abstain_without_crashing(question):
    artifact = pipeline.answer(question)
    assert artifact.tier == "Abstained"
    assert "no governed observations" in artifact.headline


def test_market_share_is_weighted_ratio_and_formatted_as_percent():
    artifact = pipeline.answer("What is TRx market share in West Cardiology?")
    frame = sl.load_fact("source_a")
    current = frame[(frame["month"] == frame["month"].max())
                    & (frame["region"] == "West")
                    & (frame["specialty"] == "Cardiology")]
    expected = current["trx_units"].sum() / current["market_trx"].sum()
    assert artifact.value == pytest.approx(expected)
    assert f"{expected * 100:.1f}%" in artifact.headline
    assert "aggregate_metric" in artifact.code


def test_call_attainment_is_ratio_of_sums_not_sum_of_row_ratios():
    artifact = pipeline.answer("What is call-plan attainment in the East region?")
    frame = sl.load_fact("source_a")
    current = frame[(frame["month"] == frame["month"].max())
                    & (frame["region"] == "East")]
    expected = current["calls"].sum() / current["call_plan"].sum()
    assert artifact.value == pytest.approx(expected)
    assert "%" in artifact.headline


def test_zero_ratio_denominator_is_undefined_not_zero():
    frame = pd.DataFrame({"calls": [0, 0], "call_plan": [0, 0]})
    value = sl.aggregate_metric(frame, "call_attainment", "actual_plan")
    assert pd.isna(value)


def test_ratio_comparison_reports_percentage_points():
    artifact = pipeline.answer("What was TRx market share in May 2026 vs prior month?")
    comparison = artifact.extras["comparison"]
    assert comparison["delta_pp"] == pytest.approx(comparison["delta"] * 100)
    assert " pp " in artifact.headline


def test_ratio_decomposition_is_a_scoped_refusal():
    artifact = pipeline.answer("Which specialties account for the TRx market share change?")
    assert artifact.tier == "Abstained"
    assert "ratio metric" in artifact.headline


@pytest.mark.parametrize("phrase,dimension", [
    ("Which payer channels account for the NRx change?", "payer_channel"),
    ("Which specialties account for the TRx change?", "specialty"),
    ("Which territories account for the TRx change?", "territory"),
    ("Which districts account for the TRx change?", "district"),
    ("Which regions account for the TRx change?", "region"),
])
def test_named_breakdown_dimension_is_parsed_and_honored(phrase, dimension):
    intent = triage.parse(phrase)
    assert intent.dim_breakdown == dimension
    artifact = pipeline.answer_intent(intent)
    assert set(artifact.extras["tables"]) == {dimension}
    assert set(artifact.table["dimension"]) == {dimension}


def test_post_filter_cardinality_removes_fully_pinned_breakdown():
    intent = triage.Intent("q", triage.DIAGNOSTIC, "trx",
                           {"payer_channel": "Commercial"},
                           dim_breakdown="payer_channel")
    artifact = pipeline.answer_intent(intent)
    assert artifact.extras["tables"] == {}
    assert "no finer breakdown" in artifact.extras["note"]


def test_multi_value_pharma_filter_is_deterministic():
    intent = triage.parse("What is TRx in the East and West regions?")
    assert intent.filters == {"region": ["East", "West"]}
    artifact = pipeline.answer_intent(intent)
    assert "region in [East, West]" in artifact.headline


def test_source_divergence_uses_common_window_and_separates_coverage():
    artifact = pipeline.answer("What was TRx in Q2 2026?")
    source_fork = next(fork for fork in artifact.divergence
                       if fork["fork"] == "source: source_b")
    assert source_fork["common_window"] == ["2026-04", "2026-05"]
    assert abs(source_fork["rel_diff"]) < 0.05  # projection bias, not a missing-month -33%
    assert artifact.extras["coverage_gaps"]
    assert any("common period" in caveat for caveat in artifact.caveats)


def test_diagnostic_missing_source_anchor_abstains_and_partial_window_is_explicit():
    missing = pipeline.answer(
        "Which regions account for the TRx change in June 2026?", source="source_b")
    assert missing.tier == "Abstained"
    assert missing.extras["requested_window"] == ["2026-06"]
    assert missing.extras["effective_window"] == []

    partial = pipeline.answer(
        "Which regions account for the TRx change in Q2 2026?", source="source_b")
    assert partial.tier == "Verified"
    assert partial.extras["requested_window"] == ["2026-04", "2026-05", "2026-06"]
    assert partial.extras["effective_window"] == ["2026-04", "2026-05"]
    assert "effective source window" in partial.headline
    assert any("does not cover the full requested window" in text for text in partial.caveats)


def test_decomposition_zero_denominators_are_explicitly_undefined(monkeypatch):
    rows = []
    for month, east, west in (("2026-01", 0, 0), ("2026-02", 1, 0)):
        for region, value in (("East", east), ("West", west)):
            rows.append({"month": month, "region": region, "territory": f"{region}-01",
                         "district": f"{region} District", "specialty": "Cardiology",
                         "payer_channel": "Commercial", "new_writers": value})
    frame = pd.DataFrame(rows)
    monkeypatch.setattr(sl, "load_fact", lambda source: frame)
    intent = triage.Intent(
        "Which regions account for the new writers change in February 2026?",
        triage.DIAGNOSTIC, "new_writers", dim_breakdown="region",
        window=triage.Window("month", ["2026-02"], "2026-02"),
        compare_basis="prior_month")
    artifact = decomposition.decompose(intent, sl.resolve("new_writers"))
    assert "percentage change unavailable: zero baseline" in artifact.headline

    frame.loc[frame["month"] == "2026-01", "new_writers"] = [1, 1]
    frame.loc[frame["month"] == "2026-02", "new_writers"] = [2, 0]
    offsetting = decomposition.decompose(intent, sl.resolve("new_writers"))
    assert "movements offset" in offsetting.headline
    assert offsetting.table["share_of_change"].isna().all()


def test_ratio_watch_and_anomaly_feeds_never_sum_ratios():
    feed = services.watch_feed([{"metric": "call_attainment", "filters": {"region": "West"}}])
    assert len(feed) == 1 and 0 <= feed.iloc[0]["latest"] <= 2
    anomalies = services.anomaly_feed(0.0)
    assert set(anomalies["metric_id"]).issubset(sl.METRICS)
    assert not ({"revenue", "units", "new_customers"} & set(anomalies["metric_id"]))
    assert anomalies["impact_score"].between(0, 1).all()
    assert anomalies["impact_score"].is_monotonic_decreasing
    ratios = anomalies[anomalies["metric_id"].isin(["trx_share", "call_attainment"])]
    assert len(ratios) and (ratios["value_format"] == "percent").all()


def test_watch_empty_scope_is_no_data_not_false_zero():
    feed = services.watch_feed([{"metric": "trx", "filters": {"region": "Atlantis"}}])
    assert len(feed) == 1
    assert feed.iloc[0]["status"] == "no_data"
    assert pd.isna(feed.iloc[0]["latest"])


def test_panel_caveats_include_registered_restatement_limitation():
    artifact = pipeline.answer("What was TRx in Q1 2025?", source="source_b")
    assert any("restated" in caveat.lower() for caveat in artifact.caveats)


def test_account_retrieval_ranks_requested_rx_metric_with_truthful_provenance():
    nrx = pipeline.answer("Top 15 accounts by NRx")
    assert nrx.tier == "Verified"
    assert nrx.table["nrx_ttm"].is_monotonic_decreasing
    assert (nrx.resolution.metric, nrx.resolution.source, nrx.resolution.variant) == (
        "nrx", "source_a", "units")

    clamped = pipeline.answer("Top 15 accounts by TRx", source="source_b", variant="dollars")
    assert clamped.tier == "Verified"
    assert (clamped.resolution.source, clamped.resolution.variant) == ("source_a", "units")
    assert "account-grain retrieval" in clamped.resolution.reason


def test_account_retrieval_refuses_unregistered_metric_instead_of_substitution():
    artifact = pipeline.answer("Top 15 accounts by calls")
    assert artifact.tier == "Abstained"
    assert "no different metric was substituted" in artifact.headline


@pytest.mark.parametrize("question,event_id,effect", [
    ("What was the impact of the speaker program?", "speaker_launch", 0.08),
    ("What was the impact of the formulary win in South Medicare?", "formulary_win", 0.10),
    ("What was the impact of the competitor launch in West Cardiology?",
     "competitor_launch", -0.22),
])
def test_registered_pharma_causal_designs_use_exact_scopes(question, event_id, effect):
    artifact = pipeline.answer(question)
    event = sl.EVENTS[event_id]
    assert artifact.tier == "Hypothesis"
    assert artifact.extras["effective_scope"] == event["scope"]
    assert artifact.extras["control_scope"] == event["control_scope"]
    assert artifact.value == pytest.approx(effect, abs=0.02)
    windows = artifact.extras["source_sensitivity_windows"]
    if len(windows) > 1:
        assert len({json.dumps(window, sort_keys=True) for window in windows.values()}) == 1
    assert "treated_growth" in artifact.code and "control_growth" in artifact.code


def test_causal_design_refuses_unregistered_narrowing_and_metric_substitution():
    narrowed = pipeline.answer(
        "What was the impact of the competitor launch in West Cardiology for Commercial?")
    assert narrowed.tier == "Abstained"
    assert "narrow" in narrowed.headline
    wrong_metric = pipeline.answer("What was the impact of the speaker program on calls?")
    assert wrong_metric.tier == "Abstained"
    assert "no registered design" in wrong_metric.headline


def test_llm_causal_no_metric_defaults_to_matched_event_metric():
    raw = _raw(question_class="Causal", metric=None, event_id="formulary_win")
    intent, _ = _validate("What was the impact of the formulary win?", raw)
    assert intent.metric == sl.EVENTS["formulary_win"]["default_metric"] == "trx"


def test_llm_requested_breakdown_dimension_is_validated():
    intent, _ = _validate("q", _raw(question_class="Diagnostic",
                                     dim_breakdown="payer_channel"))
    assert intent.dim_breakdown == "payer_channel"
    with pytest.raises(TranslationError):
        _validate("q", _raw(question_class="Diagnostic", dim_breakdown="segment"))
    with pytest.raises(TranslationError):
        _validate("q", _raw(question_class="Descriptive", dim_breakdown="region"))


def test_llm_bounded_contract_rejects_missing_extra_and_cross_class_fields():
    missing = json.loads(_raw())
    missing.pop("reason")
    with pytest.raises(TranslationError):
        _validate("q", json.dumps(missing))
    extra = json.loads(_raw()) | {"answer": "invented"}
    with pytest.raises(TranslationError):
        _validate("q", json.dumps(extra))
    with pytest.raises(TranslationError):
        _validate("q", _raw(question_class="Retrieval", template=None))
    with pytest.raises(TranslationError):
        _validate("q", _raw(question_class="Descriptive", template="whitespace"))
    with pytest.raises(TranslationError):
        _validate("q", _raw(event_id="speaker_launch"))
    with pytest.raises(TranslationError):
        _validate("q", _raw(question_class="Causal", event_id="speaker_launch",
                             metric="calls"))
    with pytest.raises(TranslationError):
        _validate("q", _raw(trend="false"))
    with pytest.raises(TranslationError):
        _validate("q", _raw(reason={"text": "not a string"}))


def test_llm_filters_and_windows_are_registry_validated():
    intent, _ = _validate("q", _raw(filters={"region": ["West", "East"]},
                                     window={"kind": "quarter", "q": 1, "year": 2026}))
    assert intent.filters == {"region": ["East", "West"]}
    assert intent.window.months == ["2026-01", "2026-02", "2026-03"]
    with pytest.raises(TranslationError):
        _validate("q", _raw(filters={"region": "Atlantis"}))


def test_llm_cohort_and_explicit_basket_contracts_are_registry_validated():
    cohort_intent, _ = _validate(
        "Compare top HCPs by NRx share with matched peers",
        _raw(question_class="Cohort comparison", metric="nrx"),
    )
    assert cohort_intent.question_class == triage.COHORT
    assert cohort_intent.metric == "nrx"

    basket_intent, _ = _validate(
        "What is TRx share in the IL-17 class?",
        _raw(metric="trx_share", basket_id="il17_class"),
    )
    assert basket_intent.basket_id == "il17_class"
    with pytest.raises(TranslationError):
        _validate("q", _raw(metric="trx", basket_id="il17_class"))
    with pytest.raises(TranslationError):
        _validate("q", _raw(metric="trx_share", basket_id="invented"))


def test_governance_write_is_atomic_and_audits_actor_before_after():
    assert sl.set_governance(materiality_rel=0.08, actor="test-admin")
    assert sl.CONFIG_PATH.exists()
    record = json.loads(sl.GOVERNANCE_LOG.read_text().splitlines()[0])
    assert record["actor"] == "test-admin"
    assert record["before"] == {}
    assert record["after"]["materiality_rel"] == 0.08
    assert "_audit_history" not in record["before"]
    assert "_audit_history" not in record["after"]
    assert not list(sl.CONFIG_PATH.parent.glob("*.tmp"))


def test_governance_embedded_audit_survives_missing_jsonl_mirror():
    sl.set_governance(materiality_rel=0.08, actor="first-admin")
    sl.set_governance(materiality_rel=0.09, actor="second-admin")
    config = json.loads(sl.CONFIG_PATH.read_text(encoding="utf-8"))
    assert len(config["_audit_history"]) == 2
    sl.GOVERNANCE_LOG.unlink()
    history = sl.governance_log()
    assert [record["actor"] for record in history] == ["first-admin", "second-admin"]
    assert len({record["change_id"] for record in history}) == 2


def test_run_golden_history_is_recorded():
    pipeline.run_golden(record=True)
    pipeline.run_golden(record=True)
    history = pipeline.eval_history()
    assert len(history) == 2
    assert (history["pass_rate"] == 1.0).all()
    assert (history["reproducible_rate"] == 1.0).all()
