"""Tile registry, scope controls, materialization, and parser round trips."""
from dataclasses import FrozenInstanceError

import pytest

from harness import semantic_layer as sl
from harness import services, tiles, triage


def test_pharma_tile_catalog_is_immutable_registered_and_multi_class():
    assert [tile.id for tile in tiles.TILE_DEFINITIONS] == [
        "trx", "nrx", "nbrx", "trx_share", "calls", "call_attainment",
        "new_writers", "samples", "commercial_trx", "payer_mix", "whitespace_hcps",
        "top_writers", "incoming_referrals", "active_referrers", "hcp_cohort",
    ]
    assert all(tile.metric in sl.METRICS for tile in tiles.TILE_DEFINITIONS)
    assert {tile.question_class for tile in tiles.TILE_DEFINITIONS} == {
        triage.DESCRIPTIVE, triage.DIAGNOSTIC, triage.RETRIEVAL, triage.COHORT}
    assert tiles.TILES_BY_ID["payer_mix"].breakdown_dimension == "payer_channel"
    assert tiles.TILES_BY_ID["whitespace_hcps"].retrieval_template == "whitespace"
    assert all(hash(tile) for tile in tiles.TILE_DEFINITIONS)
    with pytest.raises(FrozenInstanceError):
        tiles.TILE_DEFINITIONS[0].label = "Changed"


def test_control_mappings_are_complete_and_immutable():
    assert dict(tiles.WINDOW_CONTROLS) == {
        "Latest": None, "R3M": 3, "R6M": 6, "R12M": 12}
    assert dict(tiles.BASIS_CONTROLS) == {
        "MoM": "prior_month", "QoQ": "prior_quarter", "YoY": "yoy"}
    with pytest.raises(TypeError):
        tiles.WINDOW_CONTROLS["R9M"] = 9


@pytest.mark.parametrize("definition", tiles.TILE_DEFINITIONS, ids=lambda tile: tile.id)
def test_every_tile_question_round_trips_all_class_fields(definition):
    question = tiles.canonical_question(definition)
    parsed = triage.parse(question)
    intent = tiles.intent_for(definition)
    assert parsed == intent
    assert intent.question == question
    assert intent.question_class == definition.question_class
    assert intent.metric == definition.metric
    assert intent.filters == tiles.effective_filters(definition)
    if definition.question_class == triage.DESCRIPTIVE:
        assert intent.trend is True
        assert intent.compare_basis == "prior_month"
    elif definition.question_class == triage.DIAGNOSTIC:
        assert intent.dim_breakdown == definition.breakdown_dimension
        assert intent.compare_basis == "prior_month"
    else:
        assert intent.template == definition.retrieval_template
        assert intent.window is None and intent.compare_basis is None


@pytest.mark.parametrize("control,n", [("R3M", 3), ("R6M", 6), ("R12M", 12)])
def test_window_controls_round_trip_to_last_n_intents(control, n):
    intent = tiles.intent_for("trx", window=control)
    assert intent.window.kind == "last_n"
    assert intent.window.months == sl.months()[-n:]


def test_hierarchy_scope_options_are_dynamic_valid_and_round_trip():
    options = tiles.scope_options()
    assert options[0].key == tiles.ALL_SCOPES
    dimensions = {dimension for option in options for dimension, _ in option.filters}
    assert dimensions == set(tiles.SCOPE_DIMENSIONS)
    for dimension in tiles.SCOPE_DIMENSIONS:
        value = tiles.dimension_values(dimension)[0]
        scope = {dimension: value}
        assert tiles.scope_from_key(tiles.scope_key(scope)) == tiles.freeze_filters(scope)
        intent = tiles.intent_for("trx", scope=scope)
        assert intent.filters == tiles.effective_filters("trx", scope=scope)


def test_scope_does_not_drop_catalog_filters_and_legacy_region_alias_works():
    assert tiles.intent_for("trx", region="East").filters == {"region": "East"}
    expected = {"payer_channel": "Commercial", "region": "East"}
    assert tiles.effective_filters("commercial_trx", scope={"region": "East"}) == expected
    assert tiles.intent_for("commercial_trx", scope={"region": "East"}).filters == expected


def test_conflicting_global_scope_keeps_fixed_tile_scope_and_discloses_it():
    spec = tiles.question_spec("commercial_trx")
    scope = {"payer_channel": "Medicaid"}
    assert tiles.effective_spec_filters(spec, scope) == {"payer_channel": "Commercial"}
    notes = tiles.fixed_scope_disclosures(spec, scope)
    assert len(notes) == 1
    assert "Selected Payer Channel (Medicaid)" in notes[0]
    assert "fixed Payer Channel (Commercial)" in notes[0]
    assert tiles.fixed_scope_disclosures(spec, {"payer_channel": "Commercial"}) == ()


def test_diagnostic_and_retrieval_canonical_questions_preserve_scope():
    specialty = tiles.dimension_values("specialty")[0]
    diagnostic = tiles.intent_for("payer_mix", window="R3M", basis="YoY",
                                  scope={"specialty": specialty})
    assert diagnostic.question_class == triage.DIAGNOSTIC
    assert diagnostic.dim_breakdown == "payer_channel"
    assert diagnostic.filters == {"specialty": specialty}
    assert diagnostic.window.months == sl.months()[-3:]
    assert diagnostic.compare_basis == "yoy"
    retrieval = tiles.intent_for("whitespace_hcps", scope={"specialty": specialty})
    assert retrieval.question_class == triage.RETRIEVAL
    assert retrieval.template == "whitespace"
    assert retrieval.filters == {"specialty": specialty}


def test_tile_breakdown_keeps_scope_window_and_basis_in_the_question():
    question = services.breakdown_question(
        "trx", {"region": "West"}, window_n=3, basis="yoy")
    intent = triage.parse(question)
    assert intent.question_class == triage.DIAGNOSTIC
    assert intent.filters == {"region": "West"}
    assert intent.window.months == sl.months()[-3:]
    assert intent.compare_basis == "yoy"


def test_materialization_applies_only_compatible_meaningful_overrides():
    applied = tiles.materialize_spec(
        "trx", window="R3M", basis="YoY", source="source_b", variant="normalized")
    assert applied.spec.source == "source_b"
    assert applied.spec.variant == "normalized"
    assert applied.spec.window == "R3M" and applied.spec.basis == "YoY"
    assert applied.disclosures == ()

    share = tiles.materialize_spec("trx_share", variant="dollars")
    assert share.spec.variant is None
    assert "retained its governed variant" in share.disclosures[0]
    writer = tiles.materialize_spec("new_writers", source="source_b")
    assert writer.spec.source is None
    assert "retained its governed source" in writer.disclosures[0]
    retrieval = tiles.materialize_spec(
        "whitespace_hcps", window="R12M", basis="YoY",
        source="source_b", variant="dollars")
    assert retrieval.spec.window == tiles.TILES_BY_ID["whitespace_hcps"].window
    assert retrieval.spec.source is None and retrieval.spec.variant is None
    assert len(retrieval.disclosures) == 2


def test_cache_key_is_exact_spec_scope_governance_and_data_identity():
    governance = (0.02, (("trx", "units"),))
    materialized = tiles.materialize_spec(
        "trx", window="R3M", basis="YoY", source="source_a", variant="units")
    base = tiles.cache_key_for_spec(
        materialized.spec, scope={"region": "East"}, governance=governance,
        data_version="data-v1")
    assert hash(base)
    assert base.spec == materialized.spec
    assert base.effective_scope == (("region", "East"),)
    changed = [
        tiles.cache_key_for_spec(materialized.spec, scope={"region": "South"},
                                 governance=governance, data_version="data-v1"),
        tiles.cache_key_for_spec(materialized.spec, scope={"region": "East"},
                                 governance=(0.10, ()), data_version="data-v1"),
        tiles.cache_key_for_spec(materialized.spec, scope={"region": "East"},
                                 governance=governance, data_version="data-v2"),
        tiles.cache_key_for_spec(
            tiles.materialize_spec("trx", window="R6M").spec,
            scope={"region": "East"}, governance=governance, data_version="data-v1"),
    ]
    assert all(key != base for key in changed)


def test_default_cache_governance_includes_market_basket_registry():
    from harness import baskets

    governance = tiles.governance_cache_key()
    assert governance[-1] == baskets.registry_fingerprint()


def test_filter_normalization_dedupes_behavior_equivalent_values():
    assert tiles.freeze_filters({"region": ["East", "East", "West"]}) == (
        ("region", ("East", "West")),)


@pytest.mark.parametrize("kwargs", [
    {"window": "R9M"}, {"basis": "WoW"}, {"region": "Atlantis"}])
def test_invalid_controls_are_rejected(kwargs):
    with pytest.raises(ValueError):
        tiles.intent_for("trx", **kwargs)


def test_unknown_tile_id_is_rejected():
    with pytest.raises(ValueError, match="unknown tile id"):
        tiles.canonical_question("not-a-tile")
