"""Round-2 contracts: baskets, incomplete referrals, and matched cohorts."""
from dataclasses import FrozenInstanceError

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from harness import baskets, pipeline, referrals, semantic_layer as sl, services, triage
from harness.engines import cohort


def _render_cohort_answer() -> None:
    import streamlit as st

    from harness import pipeline as answer_pipeline
    from views import common as answer_common

    artifact = answer_pipeline.answer(
        "Compare the activity mix of top 20 HCPs by NRx share with matched peers")
    st.session_state["_cohort_artifact"] = artifact
    answer_common.render_answer(artifact, key="cohort_contract")


def test_market_basket_registry_is_governed_immutable_and_reconciled():
    assert set(baskets.BASKETS) == {"il17_class", "advanced_therapy"}
    assert baskets.registry_fingerprint() == baskets.registry_fingerprint()
    with pytest.raises(TypeError):
        baskets.BASKETS["new"] = baskets.BASKETS["il17_class"]
    with pytest.raises(FrozenInstanceError):
        baskets.BASKETS["il17_class"].label = "changed"

    frame = sl.load_fact("source_a")
    latest = frame[frame["month"] == frame["month"].max()]
    for basket in baskets.BASKETS.values():
        members = sum(latest[member.column].sum() for member in basket.members)
        assert members == pytest.approx(latest[basket.denominator_column].sum(), rel=1e-9)
        scoped = baskets.reconciliation_for_scope(
            basket.id, {"region": "West"}, [str(frame["month"].max())])
        assert scoped["reconciled"] is True


def test_adaptive_basket_default_and_override_are_disclosed_and_hash_material():
    accounts = sl.load_accounts()
    recent = accounts.loc[accounts["adoption_stage"] == "recent_adopter"].iloc[0]
    scope = {"account_id": recent["account_id"]}
    adaptive = baskets.answer_basket_share(filters=scope)
    broad = baskets.answer_basket_share(
        filters=scope, basket_override="advanced_therapy")

    resolution = adaptive.extras["basket_resolution"]
    assert resolution["basket_id"] == "il17_class"
    assert resolution["adoption_stage"] == "recent_adopter"
    assert "adaptive default" in adaptive.headline
    assert broad.extras["basket_resolution"]["overridden"] is True
    assert "Basket override" in broad.headline
    assert adaptive.value > broad.value
    assert adaptive.result_hash != broad.result_hash


def test_basket_share_is_ratio_of_sums_and_reproducible():
    first = baskets.answer_basket_share(
        filters={"region": "West"}, adoption_stage="established")
    second = baskets.answer_basket_share(
        filters={"region": "West"}, adoption_stage="established")
    frame = sl.load_fact("source_a")
    current = frame[(frame["region"] == "West")
                    & (frame["month"] == frame["month"].max())]
    expected = current["trx_units"].sum() / current["advanced_therapy_trx"].sum()
    assert first.value == pytest.approx(expected)
    assert first.result_hash == second.result_hash
    assert first.extras["basket_reconciliation"]["reconciled"] is True
    with pytest.raises(ValueError, match="unregistered market basket"):
        baskets.resolve_basket("established", "invented")


def test_explicit_basket_wording_routes_through_ask_without_changing_ordinary_share():
    explicit_intent = triage.parse("What is TRx share in the IL-17 class?")
    assert (explicit_intent.metric, explicit_intent.basket_id) == (
        "trx_share", "il17_class")
    explicit = pipeline.answer("What is TRx share in the IL-17 class?")
    ordinary = pipeline.answer("What is TRx market share?")
    assert explicit.engine == "basket_share"
    assert explicit.resolution.variant == "il17_class"
    assert explicit.extras["basket_resolution"]["overridden"] is True
    assert "Basket selected" in explicit.headline or "Basket override" in explicit.headline
    assert ordinary.engine == "descriptive"
    assert ordinary.resolution.variant == "brand_market"


def test_account_grain_basket_members_reconcile_after_csv_round_trip():
    account_id = sl.load_accounts().iloc[0]["account_id"]
    artifact = baskets.answer_basket_share(
        filters={"account_id": account_id}, basket_override="il17_class")
    assert artifact.tier == "Verified"
    assert artifact.extras["basket_reconciliation"]["absolute_error"] <= 1e-6


def test_referral_feed_has_exact_coverage_and_preserves_unknowns():
    report = referrals.coverage()
    assert (report.observed_hcps, report.eligible_hcps, report.rate) == (192, 240, 0.8)
    assert report.projected is False
    fact = sl.load_fact("referral")
    assert not fact.duplicated(sl.SOURCES["referral"]["grain"]).any()
    assert (fact["active_referrers"] <= fact["referrals_in"]).all()
    assert fact["npi"].astype(str).str.fullmatch(r"9999\d{6}").all()

    accounts = sl.load_accounts()
    covered_id = fact.loc[fact["referrals_in"] == 0, "account_id"].iloc[0]
    uncovered_id = next(iter(set(accounts["account_id"]) - set(fact["account_id"])))
    activity = referrals.account_activity(
        account_ids=[covered_id, uncovered_id], months=sl.months("referral")[-3:])
    covered = activity.set_index("account_id").loc[covered_id]
    uncovered = activity.set_index("account_id").loc[uncovered_id]
    assert bool(covered["referral_covered"]) is True
    assert pd.notna(covered["referrals_in"])
    assert bool(uncovered["referral_covered"]) is False
    assert pd.isna(uncovered["referrals_in"])


def test_referral_answers_compute_scoped_completeness_and_are_monitorable():
    artifact = pipeline.answer("What are incoming referrals in the West region?")
    assert artifact.tier == "Verified"
    assert artifact.resolution.source == "referral"
    assert artifact.extras["source_completeness"] == {
        "source": "referral", "entity": "account_id", "observed": 48,
        "expected": 60, "coverage": 0.8, "target": 0.8,
        "projected": False, "filters": {"region": "West"},
    }
    assert any("unknown, not zero" in caveat for caveat in artifact.caveats)
    metric_ids = set(services.anomaly_feed(0.0)["metric_id"])
    assert {"referrals_in", "active_referrers"} <= metric_ids


def test_new_writer_warmup_is_undefined_and_aggregation_preserves_missingness():
    frame = sl.load_fact("source_a")
    warmup = sl.months("source_a")[0]
    warmup_rows = frame[frame["month"] == warmup]
    assert warmup_rows["new_writers"].isna().all()
    assert pd.isna(sl.aggregate_metric(warmup_rows, "new_writers", "strict"))
    assert pd.isna(sl.monthly_metric(frame, "new_writers", "strict").loc[warmup])
    artifact = pipeline.answer("Trend new writers over the last 12 months")
    assert any("non-comparable warm-up" in caveat for caveat in artifact.caveats)


def test_synthetic_npis_are_stable_10_digit_demo_identifiers():
    accounts = sl.load_accounts()
    npis = accounts["npi"].astype(str)
    assert npis.str.fullmatch(r"9999\d{6}").all()
    assert npis.is_unique
    joined = sl.load_fact("source_a")[["account_id", "npi"]].drop_duplicates()
    assert dict(joined.astype({"npi": str}).values) == dict(
        accounts[["account_id", "npi"]].astype({"npi": str}).values)


def test_cohort_recipe_matches_exact_peers_and_is_fully_reproducible():
    first = cohort.compare_top_hcps()
    second = cohort.compare_top_hcps()
    assert first.tier == "Directional"
    assert first.result_hash == second.result_hash
    assert first.extras["input_hash"] == second.extras["input_hash"]
    assert first.extras["recipe_hash"] == cohort.recipe_fingerprint()
    assert first.extras["recipe"]["version"] == cohort.COHORT_RECIPE_VERSION

    selected = first.extras["selection"]
    pairs = first.extras["peer_matches"]
    assert len(selected) == cohort.DEFAULT_RECIPE.top_n
    assert selected["nrx_share_ttm"].is_monotonic_decreasing
    assert (selected["nrx_ttm"] >= cohort.DEFAULT_RECIPE.min_nrx_ttm).all()
    assert (selected["market_nrx_ttm"] >= cohort.DEFAULT_RECIPE.min_market_nrx_ttm).all()
    assert pairs["peer_account_id"].is_unique
    assert not set(pairs["peer_account_id"]) & set(selected["account_id"])
    assert (pairs["region"].notna() & pairs["specialty"].notna()).all()

    accounts = sl.load_accounts().copy()
    accounts["decile_band"] = accounts["decile"].map(
        lambda value: cohort._decile_band(int(value), cohort.DEFAULT_RECIPE))
    lookup = accounts.set_index("account_id")
    for pair in pairs.itertuples(index=False):
        top, peer = lookup.loc[pair.top_account_id], lookup.loc[pair.peer_account_id]
        assert (top["region"], top["specialty"], top["decile_band"]) == (
            peer["region"], peer["specialty"], peer["decile_band"])
    assert any("cannot establish" in caveat for caveat in first.caveats)
    assert any("uncovered HCPs" in caveat for caveat in first.caveats)


def test_money_question_routes_through_builtin_ask_to_cohort_engine():
    question = "Compare the activity mix of top 20 HCPs by NRx share with matched peers"
    intent = triage.parse(question)
    assert intent.question_class == triage.COHORT
    artifact = pipeline.answer(question)
    assert artifact.engine == "cohort" and artifact.tier == "Directional"
    assert artifact.question == question
    assert isinstance(artifact.resolution, sl.Resolution)
    assert artifact.resolution.source == "source_a"


def test_cohort_answer_renders_grouped_bars_without_monthly_line_assumptions():
    at = AppTest.from_function(_render_cohort_answer, default_timeout=60).run()
    assert not at.exception
    artifact = at.session_state["_cohort_artifact"]
    assert artifact.result_hash == "7874ccd5961a"
    assert artifact.chart_df is not None and "month" not in artifact.chart_df
    assert len(at.get("vega_lite_chart") or at.get("arrow_vega_lite_chart")) == 1
    markup = " ".join(str(item.value) for item in at.markdown)
    assert ("Speaker touches per HCP: 5.0 for top HCPs vs 0.6 among matched peers"
            in markup)
    assert "19 matched pairs" in markup
    primary_tables = [item.value for item in at.dataframe
                      if "Activity" in item.value.columns]
    assert len(primary_tables) == 1
    assert {"Metric", "Value format", "Gap rank"}.isdisjoint(
        primary_tables[0].columns)


def test_cohort_recipe_change_changes_recipe_and_result_hashes():
    baseline = cohort.compare_top_hcps()
    smaller = cohort.compare_top_hcps(recipe=cohort.CohortRecipe(top_n=10))
    assert smaller.extras["selected_count"] == 10
    assert smaller.extras["recipe_hash"] != baseline.extras["recipe_hash"]
    assert smaller.result_hash != baseline.result_hash
