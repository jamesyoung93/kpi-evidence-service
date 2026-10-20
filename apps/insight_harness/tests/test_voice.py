"""Pure contracts for the persona voice presentation layer."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pandas as pd

from harness import digest_narrator, pipeline, profiles, semantic_layer as sl, voice


SOUTH_ATTENDANCE = {
    "latest": 13.0,
    "trailing_mean": 7.7,
    "trailing_min": 6.0,
    "trailing_max": 9.0,
    "absolute_change": 5.3,
    "relative_change": 5.3 / 7.7,
    "low_base": False,
}


def _numbers(value: str) -> frozenset[str]:
    return frozenset(
        token.replace(",", "")
        for token in re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?", value)
    )


def test_voice_profiles_cover_the_operational_personas_as_immutable_data():
    assert tuple(voice.VOICE_PROFILES) == tuple(profiles.PERSONAS_BY_ID)
    assert voice.resolve_profile("Market Access").id == "market_access"
    assert voice.resolve_profile(profiles.PERSONAS_BY_ID["sales_rep"]).id == "sales_rep"


def test_scope_copy_has_pronouns_human_values_and_code_second_territories():
    rep = profiles.PERSONAS_BY_ID["sales_rep"]
    assert voice.scope_text(rep.default_scope, rep) == "your territory"
    assert voice.scope_text({}, "executive", opener=True) == "Across the brand"
    territory = voice.scope_text({"territory": "E-CAR-01"}, "executive")
    assert territory == "East Cardiology 1 (E-CAR-01)"
    assert "territory=" not in territory
    assert voice.scope_text({"region": "North"}, "executive") == "North"


def test_supplied_south_attendance_fixtures_and_five_voice_determinism():
    rendered = {
        persona_id: voice.digest_presentation(
            persona_id,
            kind="movement",
            metric="speaker_attendance",
            scope={"region": "South"},
            facts=SOUTH_ATTENDANCE,
            value=13.0,
            variant="attendees",
        )
        for persona_id in profiles.PERSONAS_BY_ID
    }
    assert rendered["executive"].headline == (
        "Speaker attendance in South is running well above normal — "
        "13/month vs a typical 8."
    )
    assert rendered["sales_rep"].headline == (
        "Speaker programs near you are filling up — 13 attendees vs a typical 8 last month."
    )
    assert rendered["district_manager"].headline == (
        "Your region's speaker attendance jumped to 13 vs a typical 8 — "
        "break it down by territory."
    )
    assert len({item.headline for item in rendered.values()}) == 5
    assert {_numbers(item.headline) for item in rendered.values()} == {frozenset({"13", "8"})}
    for persona_id, first in rendered.items():
        second = voice.digest_presentation(
            persona_id, kind="movement", metric="speaker_attendance",
            scope={"region": "South"}, facts=SOUTH_ATTENDANCE,
            value=13.0, variant="attendees",
        )
        assert second == first
        assert "7.7" in first.detail


def test_share_fork_uses_definition_names_and_points_not_relative_percent():
    copies = {
        persona_id: voice.digest_presentation(
            persona_id,
            kind="divergence",
            metric="trx_share",
            scope={"region": "North"},
            facts=None,
            value=0.097,
            variant="brand_market",
            alternate_label="TRx share · IL-17 class",
            alternate_value=0.278,
        )
        for persona_id in profiles.PERSONAS_BY_ID
    }
    assert copies["executive"].headline == (
        "North share reads two ways: 9.7% of all advanced therapy, 27.8% within "
        "the IL-17 class — align the definition before it reaches a slide."
    )
    assert copies["district_manager"].headline == (
        "A definition question, not a field one — your script volumes are unaffected."
    )
    for copy in copies.values():
        combined = f"{copy.headline} {copy.detail}"
        assert "18.1 pp" in combined
        assert "186.3%" not in combined
        assert "governed" not in combined.casefold()
        assert "alternate" not in combined.casefold()
        assert _numbers(combined) >= {"9.7", "27.8", "18.1"}


def test_additive_definition_fork_uses_reporting_units_not_share_language():
    copies = {
        persona_id: voice.digest_presentation(
            persona_id,
            kind="divergence",
            metric="trx",
            scope={"payer_channel": "Medicaid"},
            value=1465.642523,
            variant="units",
            alternate_label="TRx normalized equivalents",
            alternate_value=1392.360403,
        )
        for persona_id in profiles.PERSONAS_BY_ID
    }
    for copy in copies.values():
        combined = f"{copy.headline} {copy.detail}".casefold()
        assert "share reads" not in combined
        assert re.search(r"reporting[- ]unit", combined)
        assert "73.3 units" in combined
        assert _numbers(combined) >= {"1465.6", "1392.4", "73.3"}


def test_movement_subjects_scope_fallback_and_display_labels_are_grammatical():
    down = SOUTH_ATTENDANCE | {
        "latest": 90.0, "trailing_mean": 100.0, "absolute_change": -10.0,
    }
    for metric in ("samples", "calls", "new_writers", "active_referrers",
                   "referrals_in"):
        for persona_id in profiles.PERSONAS_BY_ID:
            headline = voice.digest_presentation(
                persona_id, kind="movement", metric=metric, scope={},
                facts=down, variant=sl.default_variant(metric),
            ).headline
            assert not re.search(
                r"\b(?:samples|details|new writers|active referrers|incoming referrals) "
                r"(?:is|was)\b", headline, re.I)

    scoped = voice.humanize_sentence(
        "territory=E-CAR-01 TRx was 405.2.", "executive")
    assert scoped == "East Cardiology 1 (E-CAR-01) TRx was 405.2."
    for label in ("TRx", "NRx", "NBRx", "TRx market share",
                  "Call-plan attainment"):
        assert voice.column_name(label) == label


def test_table_humanizer_copies_data_and_registers_display_names():
    source = pd.DataFrame({
        "gap_rank": [1],
        "metric": ["speaker_attendance_per_hcp_90d"],
        "territory": ["E-CAR-01"],
        "payer_channel": ["Medicaid"],
    })
    baseline = source.copy(deep=True)
    rendered = voice.humanize_table(source)
    pd.testing.assert_frame_equal(source, baseline)
    assert list(rendered.columns) == ["Gap rank", "Metric", "Territory", "Payer channel"]
    assert rendered.loc[0, "Metric"] == "Speaker touches per HCP (90d)"
    assert rendered.loc[0, "Territory"] == "East Cardiology 1 (E-CAR-01)"
    assert not any("_" in column for column in rendered.columns)


def test_artifact_hash_and_payload_are_unchanged_by_every_presentation_adapter():
    artifact = pipeline.answer(
        "Compare the activity mix of top 20 HCPs by NRx share with matched peers"
    )
    before_hash = artifact.result_hash
    before_json = artifact.to_json()
    before_table = artifact.table.copy(deep=True)

    for persona_id in profiles.PERSONAS_BY_ID:
        voice.cohort_presentation(artifact, persona_id)
        voice.tile_presentation(artifact, persona=persona_id)
        voice.humanize_table(artifact.table, persona_id)

    assert artifact.result_hash == before_hash
    assert artifact.to_json() == before_json
    pd.testing.assert_frame_equal(artifact.table, before_table)


def test_cohort_hero_leads_with_the_finding_and_demotes_methodology():
    artifact = pipeline.answer(
        "Compare the activity mix of top 20 HCPs by NRx share with matched peers"
    )
    rendered = voice.cohort_presentation(artifact, "executive")
    assert "vs" in rendered.hero
    assert "matched pairs" not in rendered.hero
    assert rendered.method_chip == "19 matched pairs"
    assert "speaker_attendance_per_hcp_90d" not in rendered.hero
    assert voice.metric_name("referrals_in_per_covered_hcp_90d") == (
        "Incoming referrals per covered HCP (90d)")
    assert voice.metric_name("active_referrers_per_covered_hcp_90d") == (
        "Active referrers per covered HCP (90d)")
    for persona_id in profiles.PERSONAS_BY_ID:
        copy = voice.cohort_presentation(artifact, persona_id)
        assert not digest_narrator._CAUSAL.search(copy.headline)

    compact = voice.cohort_display_table(artifact.table, compact=True)
    assert compact is not None
    assert not {"Metric", "Value format", "Gap rank", "Relative difference",
                "Top HCPs observed", "Peers observed"} & set(compact.columns)
    percent_rows = compact[compact["Activity"] == "Call-plan attainment"]
    assert percent_rows["Top HCPs"].str.endswith("%").all()


def test_zero_refusal_monitoring_and_relevance_patterns_are_persona_aware():
    assert voice.zero_state("sales_rep") == (
        "No untouched high-value HCPs right now — coverage is holding."
    )
    executive = voice.monitoring_presentation(
        "samples", {}, latest=90, trailing_mean=100, absolute_change=-10,
        persona="executive",
    )
    rep = voice.monitoring_presentation(
        "samples", {}, latest=90, trailing_mean=100, absolute_change=-10,
        persona="sales_rep",
    )
    assert executive.headline != rep.headline
    assert "Across the brand" in executive.headline
    assert voice.persona_relevance("executive", "divergence", "trx_share") > 0
    assert voice.persona_relevance("sales_rep", "divergence", "trx_share") < 0
    assert voice.persona_relevance("district_manager", "movement", "calls") > 0

    generic = voice.tile_presentation(
        "executive", metric="nrx", is_zero=True, template="top_writers")
    assert "No records for NRx" in generic.headline
    assert "untouched high-value" not in generic.headline.casefold()


def test_descriptive_tiles_change_voice_keep_facts_and_use_default_scope_pronoun():
    artifact = pipeline.answer("What were TRx in E-CAR-01?")
    before = artifact.to_json()
    copies = {
        persona_id: voice.tile_presentation(artifact, persona=persona_id)
        for persona_id in profiles.PERSONAS_BY_ID
    }
    assert len({copy.headline for copy in copies.values()}) == 5
    assert "your territory" in copies["sales_rep"].headline.casefold()
    expected = voice.format_value(
        artifact.resolution.metric, artifact.value, artifact.resolution.variant)
    assert all(expected in copy.headline for copy in copies.values())
    assert all("2026-06" not in copy.headline for copy in copies.values())
    assert artifact.to_json() == before

    # Every registered metric/variant is joined to a singular business subject,
    # so persona frames cannot produce "units is" or equivalent collisions.
    for metric, definition in sl.METRICS.items():
        for variant in definition["variants"]:
            fake = SimpleNamespace(
                resolution=SimpleNamespace(metric=metric, variant=variant),
                engine="descriptive", extras={"intent": SimpleNamespace(filters={})},
                headline="", value=1.0, table=None,
            )
            for persona_id in profiles.PERSONAS_BY_ID:
                headline = voice.tile_presentation(fake, persona=persona_id).headline
                assert not re.search(
                    r"\b(?:units|dollars|equivalents|details|samples|writers|"
                    r"referrers|referrals)\s+is\b", headline, re.I)


def test_display_copy_hash_is_stable_persona_specific_and_fact_bound():
    facts = SOUTH_ATTENDANCE
    hashes = {}
    for persona_id in profiles.PERSONAS_BY_ID:
        copy = voice.digest_presentation(
            persona_id, kind="movement", metric="speaker_attendance",
            scope={"region": "South"}, facts=facts, variant="attendees")
        first = voice.presentation_hash("fact-123", persona_id, copy)
        second = voice.presentation_hash("fact-123", persona_id, copy)
        assert first == second
        hashes[persona_id] = first
    assert len(set(hashes.values())) == 5
