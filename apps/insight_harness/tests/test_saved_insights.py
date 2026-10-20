"""Versioned migration, complete identity, session isolation, and evaluation."""
from dataclasses import replace

import pytest

from harness import pipeline, saved_insights, tile_runtime, tiles, triage


def test_legacy_watch_migrates_with_declarative_defaults_and_round_trips():
    legacy = {
        "metric": "trx", "filters": {"region": "West"},
        "label": "TRx · region=West", "added": "2026-07-15T10:00:00+00:00"}
    saved = saved_insights.migrate_legacy_watch(legacy)
    assert saved.schema_version == saved_insights.SCHEMA_VERSION
    assert saved.spec.metric == "trx"
    assert saved.spec.filters == (("region", "West"),)
    assert saved.spec.window == "Latest" and saved.spec.basis == "MoM"
    assert saved.spec.question_class == triage.DESCRIPTIVE
    assert saved.watched and not saved.is_stale
    assert saved.added_at == legacy["added"]
    assert saved_insights.saved_insight_from_record(saved.to_record()) == saved


def test_schema_v2_nested_record_and_malformed_version_migrate_tolerantly():
    record = {
        "schema_version": 2, "label": "Panel TRx", "watched": True,
        "intent_spec": {
            "metric": "trx", "filters": {"region": ["West", "West"]},
            "source": "source_b", "variant": "normalized", "window": "R6M",
            "basis": "YoY", "viz_kind": "line", "default_personas": ["executive"]}}
    saved = saved_insights.saved_insight_from_record(record)
    assert saved.spec.filters == (("region", ("West",)),)
    assert saved.spec.source == "source_b" and saved.spec.variant == "normalized"
    assert saved.spec.window == "R6M" and saved.spec.basis == "YoY"
    malformed = dict(record, schema_version="not-an-int")
    assert saved_insights.saved_insight_from_record(malformed).metric == "trx"


def test_complete_identity_includes_class_controls_and_display_but_not_label():
    store = saved_insights.InMemorySavedInsightStore()
    base = saved_insights.create_saved_insight("trx", {"region": "West"}, label="First")
    duplicate = store.add(base)
    assert duplicate.added
    relabeled = saved_insights.create_saved_insight(
        "trx", {"region": "West"}, label="Different label")
    assert not store.add(relabeled).added
    distinct = [
        replace(base, id="source", spec=replace(base.spec, source="source_b")),
        replace(base, id="variant", spec=replace(base.spec, variant="normalized")),
        replace(base, id="window", spec=replace(base.spec, window="R3M")),
        replace(base, id="basis", spec=replace(base.spec, basis="YoY")),
        replace(base, id="viz", spec=replace(base.spec, viz_kind="line")),
        replace(base, id="persona", spec=replace(base.spec, default_personas=("executive",))),
        replace(base, id="diagnostic", spec=replace(
            base.spec, question_class=triage.DIAGNOSTIC,
            breakdown_dimension="payer_channel")),
        replace(base, id="watch-mode", watched=False),
    ]
    assert all(store.add(insight).added for insight in distinct)
    assert len(store) == 1 + len(distinct)


def test_stale_legacy_watch_remains_visible_removable_and_cannot_execute():
    stale = saved_insights.migrate_legacy_watch(
        {"metric": "retired_metric", "filters": {}, "label": "Old watch"})
    assert stale.is_stale and "unregistered metric" in stale.stale_reason
    store = saved_insights.InMemorySavedInsightStore([stale])
    with pytest.raises(tile_runtime.StaleInsightError, match="unregistered metric"):
        tile_runtime.evaluate_saved(stale)
    assert store.remove(stale.id) and len(store) == 0


def test_obsolete_recorded_stale_reason_is_recomputed_and_cleared():
    valid = saved_insights.create_saved_insight("trx").to_record()
    valid["stale_reason"] = "metric used to be retired"
    restored = saved_insights.saved_insight_from_record(valid)
    assert not restored.is_stale


def test_session_store_instances_do_not_leak_between_session_mappings():
    session_a, session_b = {}, {}
    store_a = saved_insights.session_store(session_a)
    store_b = saved_insights.session_store(session_b)
    store_a.save("trx", {"region": "East"})
    assert len(store_a) == 1 and len(store_b) == 0
    assert saved_insights.session_store(session_a) is store_a


def test_catalog_save_snapshots_effective_scope_controls_and_class():
    territory = tiles.dimension_values("territory")[0]
    saved = saved_insights.save_catalog_tile(
        "payer_mix", window="R3M", basis="YoY", scope={"territory": territory},
        source="source_b", variant="normalized", viz_kind="table")
    assert saved.catalog_tile_id == "payer_mix"
    expected_filters = tiles.freeze_filters(
        tiles.effective_filters("payer_mix", scope={"territory": territory}))
    assert saved.spec.filters == expected_filters
    assert saved.spec.window == "R3M" and saved.spec.basis == "YoY"
    assert saved.spec.source == "source_b" and saved.spec.variant == "normalized"
    assert saved.spec.question_class == triage.DIAGNOSTIC
    assert saved.spec.breakdown_dimension == "payer_channel"


@pytest.mark.parametrize("tile_id", ["trx", "payer_mix", "whitespace_hcps"])
def test_saved_evaluation_matches_direct_answer_intent_for_every_class(tile_id):
    saved = saved_insights.save_catalog_tile(
        tile_id, scope={"region": "West"}, window="R3M", basis="YoY")
    evaluated = tile_runtime.evaluate_saved(saved)
    direct = pipeline.answer_intent(
        evaluated.intent, source=saved.spec.source, variant=saved.spec.variant)
    assert evaluated.saved_insight_id == saved.id
    assert evaluated.result_hash == direct.result_hash
    assert evaluated.artifact.resolution == direct.resolution
    assert evaluated.artifact.data_version == direct.data_version
