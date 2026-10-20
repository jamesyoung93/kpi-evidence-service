"""Registry schema, pipeline-only execution, and Ask parity for every tile."""
import pytest

from harness import pipeline, tile_runtime, tiles, triage


def test_catalog_definitions_carry_the_complete_saved_question_schema():
    for definition in tiles.TILE_DEFINITIONS:
        spec = tiles.question_spec(definition)
        assert (spec.metric, spec.filters, spec.source, spec.variant) == (
            definition.metric, definition.filters, definition.source, definition.variant)
        assert (spec.window, spec.basis, spec.viz_kind) == (
            definition.window, definition.basis, definition.viz_kind)
        assert spec.default_personas == definition.default_personas
        assert spec.question_class == definition.question_class
        assert spec.breakdown_dimension == definition.breakdown_dimension
        assert spec.retrieval_template == definition.retrieval_template
        assert spec.default_personas
        assert tiles.spec_errors(spec) == ()


@pytest.mark.parametrize("spec,error", [
    (tiles.SavedQuestionSpec("retired"), "unregistered metric"),
    (tiles.SavedQuestionSpec("calls", source="source_b"), "not registered"),
    (tiles.SavedQuestionSpec("trx", variant="gross"), "not registered"),
    (tiles.SavedQuestionSpec("trx", window="R9M"), "unknown window"),
    (tiles.SavedQuestionSpec("trx", basis="WoW"), "unknown comparison"),
    (tiles.SavedQuestionSpec("trx", viz_kind="pie"), "unknown visualization"),
    (tiles.SavedQuestionSpec("trx", default_personas=("wizard",)), "unknown default"),
    (tiles.SavedQuestionSpec("trx", filters=(("region", "Atlantis"),)),
     "unregistered region"),
    (tiles.SavedQuestionSpec("trx", question_class=triage.DIAGNOSTIC),
     "breakdown dimension"),
    (tiles.SavedQuestionSpec("trx", question_class=triage.RETRIEVAL),
     "retrieval template"),
])
def test_invalid_saved_question_specs_are_rejected(spec, error):
    with pytest.raises(ValueError, match=error):
        tiles.require_valid_spec(spec)


def test_saved_specs_round_trip_descriptive_diagnostic_and_retrieval_fields():
    territory = tiles.dimension_values("territory")[0]
    specs = (
        tiles.SavedQuestionSpec(
            "trx", tiles.freeze_filters({"territory": territory}), source="source_b",
            variant="normalized", window="R3M", basis="YoY", viz_kind="line",
            default_personas=("executive",)),
        tiles.question_spec("payer_mix", window="R6M", basis="QoQ"),
        tiles.question_spec("whitespace_hcps"),
    )
    intents = tuple(tiles.intent_for_spec(spec) for spec in specs)
    assert intents[0].question_class == triage.DESCRIPTIVE
    assert intents[0].filters == {"territory": territory}
    assert intents[0].window.months and intents[0].compare_basis == "yoy"
    assert intents[1].question_class == triage.DIAGNOSTIC
    assert intents[1].dim_breakdown == "payer_channel"
    assert intents[2].question_class == triage.RETRIEVAL
    assert intents[2].template == "whitespace"


def test_runtime_executes_strictly_through_answer_intent(monkeypatch):
    real_answer_intent = pipeline.answer_intent
    calls = []

    def forbidden(*args, **kwargs):
        raise AssertionError("tile runtime must not call pipeline.answer")

    def spy(intent, source=None, variant=None, translation=None):
        calls.append((intent, source, variant, translation))
        return real_answer_intent(intent, source, variant, translation)

    monkeypatch.setattr(pipeline, "answer", forbidden)
    monkeypatch.setattr(pipeline, "answer_intent", spy)
    evaluation = tile_runtime.evaluate_tile(
        "trx", window="R3M", basis="YoY", scope={"region": "West"},
        source="source_a", variant="units")
    assert len(calls) == 1
    intent, source, variant, translation = calls[0]
    assert intent is evaluation.intent
    assert source == "source_a" and variant == "units" and translation is None
    assert evaluation.result_hash == evaluation.artifact.result_hash


@pytest.mark.parametrize("definition", tiles.TILE_DEFINITIONS, ids=lambda tile: tile.id)
def test_every_tile_artifact_hash_matches_the_same_question_in_ask(definition):
    evaluation = tile_runtime.evaluate_tile(definition, scope={"region": "East"})
    direct = pipeline.answer(
        evaluation.canonical_question,
        source=evaluation.spec.source,
        variant=evaluation.spec.variant,
    )
    assert evaluation.result_hash == direct.result_hash
    assert evaluation.artifact.resolution == direct.resolution
    assert evaluation.artifact.data_version == direct.data_version


def test_definition_defaults_are_not_erased_and_cache_spec_matches_execution():
    definition = tiles.TileDefinition(
        "normalized_panel_trx", "Normalized panel TRx", "trx",
        source="source_b", variant="normalized", window="R6M", basis="QoQ",
        viz_kind="line", default_personas=("executive",))
    materialized = tiles.materialize_spec(definition)
    key = tiles.cache_key_for_spec(materialized.spec, scope={"region": "East"})
    evaluation = tile_runtime.evaluate_tile(definition, scope={"region": "East"})
    assert key.spec == evaluation.spec == materialized.spec
    assert evaluation.artifact.resolution.source == "source_b"
    assert evaluation.artifact.resolution.variant == "normalized"


def test_incompatible_overrides_fall_back_with_disclosure_and_do_not_raise():
    share = tile_runtime.evaluate_tile("trx_share", variant="dollars")
    assert share.artifact.resolution.variant == "brand_market"
    assert "retained its governed variant" in share.override_disclosures[0]
    writer = tile_runtime.evaluate_tile("new_writers", source="source_b")
    assert writer.artifact.resolution.source == "source_a"
    assert "retained its governed source" in writer.override_disclosures[0]
