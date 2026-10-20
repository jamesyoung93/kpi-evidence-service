"""Governed Top writers recipe, parser, tile, and translator contracts."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from harness import pipeline, semantic_layer as sl, tile_runtime, tiles, triage
from harness.engines import basic, cohort
from harness.llm_translator import TranslationError, _validate


def _translation_payload(**overrides) -> str:
    payload = {
        "question_class": triage.RETRIEVAL,
        "metric": "nrx",
        "filters": {},
        "trend": False,
        "dim_breakdown": None,
        "event_id": None,
        "template": "top_writers",
        "window": None,
        "compare_basis": None,
        "basket_id": None,
        "reason": "",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_top_writers_tile_round_trips_through_rules_and_llm_validator():
    question = "Top 15 HCP writers by trailing-12-month NRx share"
    assert tiles.canonical_question("top_writers") == question
    spec = tiles.question_spec("top_writers")
    assert (spec.metric, spec.retrieval_template) == ("nrx", "top_writers")

    intent = triage.parse(question)
    assert intent == tiles.intent_for("top_writers")
    assert (intent.question_class, intent.metric, intent.template) == (
        triage.RETRIEVAL, "nrx", "top_writers")

    scoped = tiles.intent_for("top_writers", scope={"region": "West"})
    assert scoped.question == question + " in West"
    assert scoped.filters == {"region": "West"}

    translated, meta = _validate(question, _translation_payload())
    assert meta["validated"] is True
    assert (translated.question_class, translated.metric, translated.template) == (
        triage.RETRIEVAL, "nrx", "top_writers")
    with pytest.raises(TranslationError, match="requires metric 'nrx'"):
        _validate(question, _translation_payload(metric="trx"))


def test_top_writers_recipe_applies_floors_stable_ranking_and_full_disclosure():
    recipe = basic.DEFAULT_TOP_WRITERS_RECIPE
    assert recipe.top_n == 15
    assert recipe.min_nrx_ttm == cohort.DEFAULT_RECIPE.min_nrx_ttm == 24
    assert recipe.min_market_nrx_ttm == \
        cohort.DEFAULT_RECIPE.min_market_nrx_ttm == 120

    artifact = tile_runtime.evaluate_tile("top_writers").artifact
    assert artifact.tier == "Verified" and artifact.engine == "retrieval"
    assert artifact.resolution.metric == "nrx"
    assert "governed Top writers account-grain retrieval" in artifact.resolution.reason

    expected_columns = [
        "rank", "account_id", "npi", "name", "specialty", "territory",
        "district", "region", "payer_channel", "nrx_ttm",
        "market_nrx_ttm", "nrx_share_ttm", "decile",
    ]
    assert artifact.table.columns.tolist() == expected_columns
    assert artifact.table["rank"].tolist() == list(range(1, 16))
    assert artifact.table["npi"].str.fullmatch(r"9999\d{6}").all()
    assert (artifact.table["nrx_ttm"] >= recipe.min_nrx_ttm).all()
    assert (artifact.table["market_nrx_ttm"] >= recipe.min_market_nrx_ttm).all()

    eligible = sl.load_accounts()
    eligible = eligible[
        (eligible["nrx_ttm"] >= recipe.min_nrx_ttm)
        & (eligible["market_nrx_ttm"] >= recipe.min_market_nrx_ttm)
        & eligible["nrx_share_ttm"].notna()
    ]
    expected = eligible.sort_values(
        ["nrx_share_ttm", "nrx_ttm", "account_id"],
        ascending=[False, False, True], kind="mergesort",
    ).head(recipe.top_n)
    assert artifact.table["account_id"].tolist() == expected["account_id"].tolist()
    pd.testing.assert_series_equal(
        artifact.table["nrx_share_ttm"],
        expected["nrx_share_ttm"].reset_index(drop=True),
        check_names=False,
    )

    recipe_extra = artifact.extras["recipe"]
    assert recipe_extra["min_nrx_ttm"] == 24
    assert recipe_extra["min_market_nrx_ttm"] == 120
    assert recipe_extra["tie_breakers"] == (
        "nrx_share_ttm DESC", "nrx_ttm DESC", "account_id ASC")
    assert artifact.extras["column_roles"] == {
        "numerator": "nrx_ttm",
        "denominator": "market_nrx_ttm",
        "share": "nrx_share_ttm",
    }
    assert "NRx TTM ≥ 24" in artifact.headline
    assert "market NRx TTM ≥ 120" in artifact.headline
    assert "descriptive ranking only, not causal" in artifact.headline
    assert "hcp.nrx_ttm >= 24" in artifact.code
    assert "hcp.market_nrx_ttm >= 120" in artifact.code
    assert "no causal inference" in artifact.code.lower()
    assert any("must not be interpreted causally" in caveat
               for caveat in artifact.caveats)


def test_generic_top_accounts_ask_keeps_legacy_nrx_volume_ranking():
    question = "Top 15 accounts by NRx"
    intent = triage.parse(question)
    assert (intent.metric, intent.template) == ("nrx", "top_accounts")

    artifact = pipeline.answer(question)
    expected_ids = sl.load_accounts().sort_values(
        "nrx_ttm", ascending=False).head(15)["account_id"].tolist()
    assert artifact.table["account_id"].tolist() == expected_ids
    assert "nrx_share_ttm" not in artifact.table
    assert artifact.headline == "Top 15 HCP accounts by trailing-twelve-month NRx"
