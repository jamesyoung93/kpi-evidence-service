from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from harness import pipeline, profiles, tile_runtime, tiles, triage


ROOT = Path(__file__).resolve().parents[1]


def test_round2_capabilities_are_registered_as_saved_question_tiles():
    required = {"top_writers", "incoming_referrals", "active_referrers", "hcp_cohort"}
    assert required <= set(tiles.TILES_BY_ID)
    assert "incoming_referrals" in profiles.PERSONAS_BY_ID[
        "district_manager"].default_tile_ids
    assert {"incoming_referrals", "active_referrers"} <= set(
        profiles.PERSONAS_BY_ID["market_access"].default_tile_ids)
    assert "hcp_cohort" in profiles.PERSONAS_BY_ID["executive"].default_tile_ids

    cohort_spec = tiles.question_spec("hcp_cohort")
    assert cohort_spec.question_class == triage.COHORT
    assert tiles.intent_for_spec(cohort_spec).question_class == triage.COHORT


def test_referral_and_cohort_tiles_execute_through_the_governed_pipeline():
    referral = tile_runtime.evaluate_tile("incoming_referrals", window="R3M")
    assert referral.artifact.resolution.source == "referral"
    assert referral.artifact.extras["source_completeness"]["projected"] is False

    cohort = tile_runtime.evaluate_tile("hcp_cohort")
    assert cohort.artifact.engine == "cohort"
    assert cohort.artifact.tier == "Directional"
    assert cohort.artifact.resolution.source == "source_a"
    assert cohort.artifact.extras["matched_count"] > 0


def test_explicit_basket_question_is_disclosed_and_hash_material():
    class_share = pipeline.answer("What is TRx share in the IL-17 class?")
    broad_share = pipeline.answer("What is TRx share in the advanced-therapy market?")
    assert class_share.resolution.variant == "il17_class"
    assert broad_share.resolution.variant == "advanced_therapy"
    assert class_share.extras["basket_resolution"]["overridden"] is True
    assert class_share.result_hash != broad_share.result_hash


def test_exact_money_question_renders_and_offers_causal_handoff():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    at.text_input(key="ask_q").set_value(
        "Compare the activity mix of top 20 HCPs by NRx share with matched peers"
    ).run()
    assert not at.exception
    assert any(button.label == "Design a causal follow-up" for button in at.button)
    assert len(at.dataframe) >= 3
