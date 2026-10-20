"""Round-2 contracts for commercially credible ranking and digest cards."""
from __future__ import annotations

from harness import digest, profiles, services
from views import digest as digest_view


def test_priority_v2_weights_business_scale_and_guards_low_bases():
    full = services.priority_components_v2(
        z=4.0, relative_change=0.50, business_delta=100.0,
        national_monthly_volume=1000.0,
    )
    assert full == {
        "standardized": 1.0,
        "relative": 1.0,
        "business_scale": 1.0,
        "business_scale_share": 0.1,
        "low_base_guard": False,
        "score": 1.0,
    }

    guarded = services.priority_components_v2(
        z=4.0, relative_change=8.0, business_delta=100.0,
        national_monthly_volume=1.0, low_base=True,
    )
    assert guarded["standardized"] == 1.0
    assert guarded["relative"] == 0.0
    assert guarded["business_scale"] == 0.0
    assert guarded["score"] == 0.45


def test_anomaly_feed_demotes_tiny_counts_and_clusters_duplicate_stories():
    feed = services.anomaly_feed(1.6)
    writers = feed[feed["metric_id"] == "new_writers"]
    samples = feed[feed["metric_id"] == "samples"]
    assert len(writers) == 1
    assert bool(writers.iloc[0]["low_base"])
    assert writers.iloc[0]["cluster_size"] == 2
    assert "New writers · region=South" in writers.iloc[0]["also_visible_as"]
    assert writers.iloc[0]["impact_score"] < samples["impact_score"].max()

    assert "call_attainment" not in set(feed["metric_id"])
    details = feed[feed["metric_id"] == "calls"]
    assert len(details)
    assert all(any("Call-plan attainment" in label for label in labels)
               for labels in details["also_visible_as"])


def test_digest_is_scope_diverse_plain_language_and_sparkline_is_endpoint_only():
    artifact = digest.build_digest(
        persona="Executive", scope={}, watches=[], record=False)
    scopes = [item.candidate.filters for item in artifact.items]
    assert len(scopes) == len(set(scopes))
    assert all("σ" not in item.template_headline for item in artifact.items)
    assert {item.category_label for item in artifact.items} <= {
        "Movement", "Watched", "Definition fork", "Event overlap"}

    item = next(item for item in artifact.items if item.candidate.facts is not None)
    spec = digest_view._sparkline_chart(item).to_dict()
    assert spec["layer"][0]["mark"]["type"] == "line"
    assert "point" not in spec["layer"][0]["mark"]
    assert spec["layer"][1]["mark"]["type"] == "circle"
    assert spec["layer"][0]["encoding"]["y"]["scale"]["zero"] is False


def test_low_base_digest_headline_uses_count_range_not_percentage_or_sigma():
    scan = digest.scan_candidates(persona="Executive", scope={}, watches=[])
    candidate = next(
        item for item in scan.candidates
        if item.metric == "new_writers" and item.facts and item.facts.low_base)
    rendered = digest._make_item(candidate, 1.0, 1.0)
    assert "typical" in rendered.template_headline
    assert "%" not in rendered.template_headline
    assert "σ" not in rendered.template_headline
    assert "percentage suppressed" in rendered.impact_text


def test_persona_ordering_keeps_an_in_scope_material_definition_question():
    persona = profiles.PERSONAS_BY_ID["sales_rep"]
    scan = digest.scan_candidates(
        persona=persona.label, scope=dict(persona.default_scope), watches=[])
    highest_scope = max(candidate.scope_rank for candidate in scan.candidates)
    protected = [candidate for candidate in scan.candidates
                 if candidate.kind == "divergence"
                 and candidate.scope_rank == highest_scope]
    assert protected, "fixture needs an in-scope material definition question"

    ranked = digest.rank_candidates(
        scan.candidates, limit=3, persona=persona.label)
    assert any(candidate.kind == "divergence"
               and candidate.scope_rank == highest_scope
               for candidate, _novelty, _score in ranked)
    for candidate, novelty, score in ranked:
        assert score == digest._candidate_score(candidate, novelty)

    single = digest.rank_candidates(
        scan.candidates, limit=1, persona=persona.label)
    assert len(single) == 1
    assert single[0][0].kind == "divergence"
    assert single[0][0].scope_rank == highest_scope
