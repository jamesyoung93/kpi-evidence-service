"""Phase 4 contract: isolated, deterministic, diverse, grounded digests."""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from harness import digest, digest_narrator, profiles, semantic_layer as sl, voice
from harness.digest_store import (DigestHistoryStore, InMemoryDigestHistoryStore,
                                  history_fingerprint)
from harness import triage
from views import digest as digest_view


REPO = Path(__file__).parent.parent
APP = str(REPO / "app.py")


def _build(tmp_path=None, *, persona="Executive", scope=None, watches=(),
           record=False, store=None, owner_namespace=None):
    if store is None:
        store = (DigestHistoryStore(tmp_path / "digest_history.jsonl") if tmp_path
                 else InMemoryDigestHistoryStore())
    return digest.build_digest(
        persona=persona, scope=scope or {}, watches=list(watches), store=store,
        record=record, owner_namespace=owner_namespace)


def _movement_item():
    scan = digest.scan_candidates(persona="Executive", scope={}, watches=[])
    candidate = next(item for item in scan.candidates
                     if item.kind == "anomaly" and item.facts is not None
                     and item.facts.trailing_std)
    return digest._make_item(candidate, 1.0, 1.0)


def _event_item():
    scan = digest.scan_candidates(persona="Executive", scope={}, watches=[])
    candidate = next(item for item in scan.candidates if item.kind == "event")
    return digest._make_item(candidate, 1.0, 1.0)


def _share_divergence_item():
    scan = digest.scan_candidates(persona="Executive", scope={}, watches=[])
    candidate = next(item for item in scan.candidates
                     if item.kind == "divergence" and item.metric == "trx_share")
    return digest._make_item(candidate, 1.0, 1.0)


def _response(item, *, text=None, metric=None, scope=None, direction=None,
              causal=False):
    return {
        "text": text or digest_view.presentation_for(item, "Executive").headline,
        "metric": metric or item.candidate.metric,
        "scope": scope or item.fact_payload()["scope"],
        "direction": direction or digest_narrator.expected_direction(item),
        "causal": causal,
    }


def test_digest_is_normalized_semantically_diverse_and_artifact_grounded():
    artifact = _build()
    assert 0 < len(artifact.items) <= 3
    families = [item.candidate.family for item in artifact.items]
    assert len(set(families)) == len(families)
    assert sum(item.candidate.metric in {"trx", "nrx", "nbrx"}
               for item in artifact.items) <= 1
    for item in artifact.items:
        assert 0.0 <= item.candidate.impact_score <= 1.0
        payload = json.loads(item.to_json())
        assert payload["underlying_answer_hash"] == item.candidate.artifact.result_hash
        assert payload["underlying_answer"]["result_hash"] == \
            item.candidate.artifact.result_hash
        assert payload["fact_hash"] == item.fact_hash == item.result_hash
        assert payload["presentation_hash"] == item.presentation_hash


def test_scanner_explicitly_covers_all_four_signal_paths():
    watch = {"metric": "trx", "filters": {}, "source": "source_b",
             "variant": "normalized", "window": "R6M", "basis": "YoY"}
    scan = digest.scan_candidates(persona="Executive", scope={}, watches=[watch])
    kinds = {candidate.kind for candidate in scan.candidates}
    assert {"anomaly", "watch", "divergence", "event"} <= kinds
    assert all(candidate.artifact.result_hash for candidate in scan.candidates)


def test_non_descriptive_watches_are_excluded_instead_of_reinterpreted():
    unsupported = [
        {"metric": "trx", "filters": {}, "source": "source_b",
         "variant": "normalized", "window": "R6M", "basis": "YoY",
         "question_class": triage.DIAGNOSTIC},
        {"metric": "trx", "filters": {}, "source": "source_b",
         "variant": "normalized", "window": "R6M", "basis": "YoY",
         "question_class": triage.RETRIEVAL},
    ]
    scan = digest.scan_candidates(persona="Executive", scope={}, watches=unsupported)
    assert not [candidate for candidate in scan.candidates if candidate.kind == "watch"]
    assert _build(watches=unsupported).input_fingerprint == _build().input_fingerprint

    common = {"watched": True, "is_stale": False, "metric": "trx", "filters": {},
              "source": "source_b", "variant": "normalized", "window": "R6M",
              "basis": "YoY"}
    inputs, skipped = digest_view._descriptive_watch_inputs([
        SimpleNamespace(**common, question_class=triage.DESCRIPTIVE),
        SimpleNamespace(**common, question_class=triage.DIAGNOSTIC),
        SimpleNamespace(**common, question_class=triage.RETRIEVAL),
    ])
    assert len(inputs) == 1
    assert inputs[0]["question_class"] == triage.DESCRIPTIVE
    assert skipped == 2


def test_persona_scope_is_a_strict_lexicographic_tier():
    scan = digest.scan_candidates(persona="Executive", scope={}, watches=[])
    first = scan.candidates[0]
    second = next(item for item in scan.candidates if item.family != first.family)
    scoped = replace(first, scope_rank=2, impact_score=0.0001, scope_priority=0.0)
    national = replace(second, scope_rank=1, impact_score=1.0, scope_priority=1.0)
    ranked = digest.rank_candidates([national, scoped], limit=2)
    assert ranked[0][0] == scoped


def test_mapping_scope_is_preserved_and_ranked_ahead_of_national():
    fact = sl.load_fact(next(iter(sl.SOURCES)))
    dimension = "territory" if "territory" in sl.DIMENSIONS else sl.DIMENSIONS[0]
    value = str(sorted(fact[dimension].dropna().unique())[0])
    artifact = _build(persona="Sales Rep", scope={dimension: value})
    assert artifact.scope == {dimension: value}
    assert artifact.items
    assert all(item.candidate.scope_rank == 2 for item in artifact.items)


def test_digest_history_is_idempotent_and_does_not_rotate_on_rerun(tmp_path):
    store = DigestHistoryStore(tmp_path / "digest_history.jsonl")
    first = _build(tmp_path, store=store, record=True, owner_namespace="viewer-a")
    second = _build(tmp_path, store=store, record=True, owner_namespace="viewer-a")
    assert first.digest_key == second.digest_key
    assert first.result_hash == second.result_hash
    assert [item.candidate.semantic_key for item in first.items] == [
        item.candidate.semantic_key for item in second.items]
    assert len(store.load()) == 1


def test_history_isolated_by_owner_scope_and_watch_fingerprint(tmp_path):
    store = DigestHistoryStore(tmp_path / "digest_history.jsonl")
    base = {"persona": "Executive", "data_version": "v1",
            "governance_fingerprint": "g1", "item_keys": ["a"]}
    records = [
        base | {"digest_key": "match", "owner_namespace": "user-a",
                "scope": {"region": "East"}, "input_fingerprint": "watch-a"},
        base | {"digest_key": "other-owner", "owner_namespace": "user-b",
                "scope": {"region": "East"}, "input_fingerprint": "watch-a"},
        base | {"digest_key": "other-scope", "owner_namespace": "user-a",
                "scope": {"region": "West"}, "input_fingerprint": "watch-a"},
        base | {"digest_key": "other-watch", "owner_namespace": "user-a",
                "scope": {"region": "East"}, "input_fingerprint": "watch-b"},
    ]
    for record in records:
        store.record_once(record)
    recent = store.recent(
        persona="Executive", scope={"region": "East"},
        input_fingerprint="watch-a", owner_namespace="user-a")
    assert [record["digest_key"] for record in recent] == ["match"]

    first, second = InMemoryDigestHistoryStore(), InMemoryDigestHistoryStore()
    first.record_once(records[0])
    assert len(first.load()) == 1
    assert second.load() == []


def test_record_once_is_concurrency_safe(tmp_path):
    path = tmp_path / "digest_history.jsonl"
    record = {"digest_key": "one", "persona": "Executive", "scope": {},
              "input_fingerprint": "w", "owner_namespace": "u", "item_keys": ["a"]}

    def write_once(_):
        return DigestHistoryStore(path).record_once(record)

    with ThreadPoolExecutor(max_workers=8) as pool:
        stored = list(pool.map(write_once, range(24)))
    assert all(item == record for item in stored)
    assert DigestHistoryStore(path).load() == [record]


def test_store_skips_corrupt_lines_and_fingerprint_ignores_timestamp(tmp_path):
    path = tmp_path / "digest_history.jsonl"
    valid = {"digest_key": "one", "owner_namespace": "u", "persona": "Executive",
             "scope": {}, "input_fingerprint": "w", "data_version": "v1",
             "governance_fingerprint": "g1", "item_keys": ["a", "b"],
             "created_at": "first"}
    path.write_text(json.dumps(valid) + "\n" + '{"digest_key":' + "\n", encoding="utf-8")
    store = DigestHistoryStore(path)
    assert store.load() == [valid]
    assert history_fingerprint([valid]) == \
        history_fingerprint([valid | {"created_at": "later"}])


def test_watch_execution_context_survives_into_item_and_breakdown(monkeypatch):
    watch = {"metric": "trx", "filters": {"region": "West"},
             "source": "source_b", "variant": "normalized",
             "window": "R6M", "basis": "YoY"}
    scan = digest.scan_candidates(persona="Executive", scope={}, watches=[watch])
    candidate = next(item for item in scan.candidates if item.kind == "watch")
    item = digest._make_item(candidate, 1.0, 1.0)
    payload = item.fact_payload()
    context = payload["drillthrough_context"]
    assert context == {"source": "source_b", "variant": "normalized",
                       "window": "R6M", "basis": "yoy"}
    method = payload["ranking_method"]
    assert method["id"] == digest.MOVEMENT_RANKING_METHOD
    assert method["latest_periods"] == 1
    assert method["baseline_periods"] == 6
    assert method["uses_drillthrough_window"] is False
    assert method["uses_drillthrough_basis"] is False
    assert method["weights"] == {
        "standardized": 0.45, "relative": 0.20, "business_scale": 0.35}
    assert "national monthly volume" in method["business_scale_definition"]
    assert "suppressed" in method["low_base_guard"]
    assert "last 6 months" in item.breakdown_question
    assert "same month last year" in item.breakdown_question

    queued = {}
    monkeypatch.setattr(digest_view, "st", SimpleNamespace(session_state={}))
    monkeypatch.setattr(
        digest_view.common, "queue_question_with_resolution",
        lambda question, source, variant, basis: queued.update(
            question=question, source=source, variant=variant, basis=basis))
    digest_view._queue_breakdown(item)
    assert digest_view.st.session_state["_digest_drillthrough_context"] == context
    assert queued == {"question": item.breakdown_question, "source": "source_b",
                      "variant": "normalized", "basis": "yoy"}

    alternate_context = replace(candidate, window="R12M", basis="prior_month")
    assert alternate_context.semantic_key == candidate.semantic_key
    alternate_item = digest._make_item(alternate_context, 1.0, 1.0)
    assert alternate_item.fact_hash == item.fact_hash
    assert alternate_item.presentation_hash != item.presentation_hash


def test_fact_and_presentation_hashes_have_distinct_contracts():
    artifact = _build()
    item = artifact.items[0]
    narrated = item.with_narration("Validated alternate wording", {
        "narrator": "language_model", "validated": True, "model": "model-a"})
    assert narrated.fact_hash == item.fact_hash
    assert narrated.presentation_hash != item.presentation_hash
    presented = replace(artifact, items=(narrated, *artifact.items[1:]))
    assert presented.fact_hash == artifact.fact_hash
    assert presented.presentation_hash != artifact.presentation_hash
    assert json.loads(presented.to_json())["presentation_hash"] == presented.presentation_hash

    copy_mutations = (
        replace(item, template_headline=item.template_headline + " Copy edit."),
        replace(item, impact_text=item.impact_text + " Copy edit."),
        replace(item, breakdown_question=item.breakdown_question + " Copy edit?"),
    )
    for mutated in copy_mutations:
        assert mutated.fact_hash == item.fact_hash
        assert mutated.presentation_hash != item.presentation_hash


def test_persona_copy_is_view_only_and_preserves_the_item_artifact():
    item = _movement_item()
    before = item.to_json()
    fact_hash = item.fact_hash
    presentation_hash = item.presentation_hash

    copies = [digest_view.presentation_for(item, persona) for persona in (
        "Sales Rep", "District Manager", "Brand Marketing", "Market Access", "Executive",
    )]

    assert len({copy.headline for copy in copies}) == 5
    assert item.to_json() == before
    assert item.fact_hash == fact_hash
    assert item.presentation_hash == presentation_hash


def test_structured_narrator_accepts_only_a_fully_grounded_response():
    item = _movement_item()
    valid, reason = digest_narrator.validate_rewrite(item, _response(item))
    assert valid, reason


def test_narrator_rejects_machine_copy_even_when_numeric_facts_match():
    movement = _movement_item()
    human = digest_view.presentation_for(movement, "Executive").headline
    for suffix, expected_reason in (
        (" governed artifact.", "methodology"),
        (" gap_rank.", "machine identifier"),
    ):
        valid, reason = digest_narrator.validate_rewrite(
            movement, _response(movement, text=human + suffix))
        assert not valid
        assert expected_reason in reason

    fork = _share_divergence_item()
    scope = voice.scope_text(fork.candidate.filter_dict, "executive",
                             default_scope={})
    metric = voice.metric_name(fork.candidate.metric)
    attacks = (
        f"{scope} {metric} was 9.7% above 27.8%; the definitions are different.",
        f"{scope} {metric} differs by 9.7% vs 27.8% relative difference.",
    )
    for text in attacks:
        valid, reason = digest_narrator.validate_rewrite(
            fork, _response(fork, text=text), persona="executive")
        assert not valid
        assert "passive" in reason or "relative percentage" in reason


def test_each_persona_canonical_template_validates_for_both_sample_directions():
    scan = digest.scan_candidates(persona="Executive", scope={}, watches=[])
    sample_candidates = [
        candidate for candidate in scan.candidates
        if candidate.metric == "samples" and candidate.facts is not None
    ]
    directions = {
        "above": next(candidate for candidate in sample_candidates
                      if candidate.facts.absolute_change > 0),
        "below": next(candidate for candidate in sample_candidates
                      if candidate.facts.absolute_change < 0),
    }
    for candidate in directions.values():
        item = digest._make_item(candidate, 1.0, 1.0)
        for persona in voice.VOICE_PROFILES:
            presentation = digest_view.presentation_for(item, persona)
            scope_label = voice.scope_text(
                candidate.filter_dict, persona, default_scope={})
            metric_label = voice.metric_subject(candidate.metric)
            valid, reason = digest_narrator.validate_rewrite(
                item,
                _response(item, text=presentation.headline),
                template_headline=presentation.headline,
                scope_label=scope_label,
                metric_label=metric_label,
                persona=persona,
            )
            assert valid, f"{persona}/{candidate.filter_dict}: {reason}"


def test_default_scope_pronouns_validate_on_the_direct_narrator_path():
    for persona_id in ("sales_rep", "district_manager"):
        persona = profiles.PERSONAS_BY_ID[persona_id]
        expected_scope = dict(persona.default_scope)
        scan = digest.scan_candidates(
            persona=persona.label, scope=expected_scope, watches=[])
        candidate = next(
            candidate for candidate in scan.candidates
            if candidate.facts is not None
            and candidate.filter_dict == expected_scope
            and candidate.kind != "event"
            and candidate.event_id is None)
        item = digest._make_item(candidate, 1.0, 1.0)
        presentation = digest_view.presentation_for(item, persona_id)
        valid, reason = digest_narrator.validate_rewrite(
            item, _response(item, text=presentation.headline), persona=persona_id)
        assert valid, reason
        assert voice.scope_text(expected_scope, persona_id) in presentation.headline


def test_narrator_rejects_polarity_unit_scope_metric_and_causal_attacks():
    item = _movement_item()
    direction = digest_narrator.expected_direction(item)
    opposite = "below" if direction == "above" else "above"
    polarity_text = item.template_headline.replace(direction, opposite, 1)
    attacks = [
        _response(item, text=polarity_text),
        _response(item, text=item.template_headline.replace("%", "$", 1)),
        _response(item, scope="region=Elsewhere"),
        _response(item, metric="not_the_metric"),
        _response(item, text=item.template_headline + " It was caused by the event."),
        _response(item, text=item.template_headline + " Forecast 999."),
    ]
    attacks.extend(
        _response(item, text=item.template_headline + phrase)
        for phrase in (
            " The program boosted the movement.",
            " The launch triggered the movement.",
            " The campaign generated the movement.",
            " The event contributed to the movement.",
            " This happened as a result of the program.",
        )
    )
    for attack in attacks:
        assert not digest_narrator.validate_rewrite(item, attack)[0]


def test_registered_event_rewrites_are_policy_rejected_without_using_ui_quota(monkeypatch):
    item = _event_item()
    valid, reason = digest_narrator.validate_rewrite(item, _response(item))
    assert not valid
    assert "registered-event" in reason

    invoked = []
    client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **_: invoked.append(True)))
    monkeypatch.setitem(sys.modules, "anthropic",
                        SimpleNamespace(Anthropic=lambda **_: client))
    direct = digest_narrator.rewrite_item(item, api_key="test-key", model="model-a")
    assert direct.narration["fallback_kind"] == "policy"
    assert not invoked

    state = {"_model_calls_used": 7}
    monkeypatch.setattr(digest_view, "st", SimpleNamespace(session_state=state))
    rendered = digest_view._narrated(item, "test-key", "model-a", "Executive")
    assert rendered.narration["fallback_kind"] == "policy"
    assert state["_model_calls_used"] == 7
    assert not invoked


def test_view_narrator_cache_and_validation_use_the_persona_voice_template(monkeypatch):
    item = _movement_item()
    calls = []

    def rewrite(candidate, **kwargs):
        calls.append(kwargs)
        return candidate.with_narration(kwargs["template_headline"], {
            "narrator": "template", "validated": True,
        })

    state = {"_model_calls_used": 0}
    monkeypatch.setattr(digest_view, "st", SimpleNamespace(session_state=state))
    monkeypatch.setattr(digest_narrator, "rewrite_item", rewrite)

    rep = digest_view._narrated(item, "test-key", "model-a", "Sales Rep")
    executive = digest_view._narrated(item, "test-key", "model-a", "Executive")
    repeat = digest_view._narrated(item, "test-key", "model-a", "Sales Rep")

    assert len(calls) == 2
    assert repeat is rep
    assert rep.headline != executive.headline
    for persona, call in zip(("Sales Rep", "Executive"), calls):
        expected = digest_view.presentation_for(item, persona)
        assert call["template_headline"] == expected.headline
        assert call["persona"] == persona
        assert call["scope_label"] == voice.scope_text(item.candidate.filter_dict, persona)
        assert "=" not in call["scope_label"]
        assert call["metric_label"] == voice.metric_subject(item.candidate.metric)


def test_persona_download_copy_and_hash_match_the_exact_rendered_item():
    item = _movement_item()
    before_json = item.to_json()
    before_fact = item.fact_hash
    persona = "Sales Rep"
    payload = json.loads(digest_view._item_download_json(item, persona))
    rendered = digest_view.presentation_for(item, persona)
    display = payload["voice_presentation"]
    assert display["headline"] == rendered.headline
    assert display["detail"] == rendered.detail
    assert display["chip"] == rendered.chip
    assert display["presentation_hash"] == voice.presentation_hash(
        item.fact_hash, persona, rendered)
    assert item.to_json() == before_json
    assert item.fact_hash == before_fact

    hashes = {
        voice.presentation_hash(
            item.fact_hash, persona_id,
            digest_view.presentation_for(item, persona_id))
        for persona_id in voice.VOICE_PROFILES
    }
    assert len(hashes) == 5


def test_complete_digest_export_hash_covers_all_displayed_copy():
    artifact = _build(persona="Executive")
    exported, display_hash = digest_view._digest_download(artifact, "Executive")
    payload = json.loads(exported)
    bundle = payload["voice_presentation"]
    assert bundle["presentation_hash"] == display_hash
    assert len(bundle["items"]) == len(artifact.items)
    for item, displayed in zip(artifact.items, bundle["items"]):
        rendered = digest_view.presentation_for(item, "Executive")
        assert displayed["headline"] == rendered.headline
        assert displayed["detail"] == rendered.detail
        assert displayed["chip"] == rendered.chip

    changed = dict(bundle)
    changed["items"] = [dict(item) for item in bundle["items"]]
    changed["items"][0]["headline"] += " Copy edit."
    changed.pop("presentation_hash")
    assert voice.presentation_hash(
        artifact.fact_hash, "Executive", changed) != display_hash

def test_narrator_success_and_rejection_fallback(tmp_path, monkeypatch):
    item = _movement_item()
    response = SimpleNamespace(content=[SimpleNamespace(
        type="text", text=json.dumps(_response(item)))])
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: response))
    monkeypatch.setitem(sys.modules, "anthropic",
                        SimpleNamespace(Anthropic=lambda **_: client))
    rendered = digest_narrator.rewrite_item(item, api_key="test-key", model="model-a")
    assert rendered.narration["narrator"] == "language_model"
    assert rendered.fact_hash == item.fact_hash
    assert rendered.presentation_hash != item.presentation_hash

    bad = SimpleNamespace(content=[SimpleNamespace(
        type="text", text=json.dumps(_response(
            item, text=item.template_headline + " caused by launch")))])
    client.messages.create = lambda **_: bad
    fallback = digest_narrator.rewrite_item(item, api_key="test-key", model="model-a")
    assert fallback.headline == digest_view.presentation_for(item, "Executive").headline
    assert fallback.narration["fallback_kind"] == "rejected"


def test_narrator_timeout_or_error_falls_back_without_exposing_exception(monkeypatch):
    item = _movement_item()

    def fail(**_):
        raise TimeoutError("secret provider diagnostic")

    client = SimpleNamespace(messages=SimpleNamespace(create=fail))
    monkeypatch.setitem(sys.modules, "anthropic",
                        SimpleNamespace(Anthropic=lambda **_: client))
    fallback = digest_narrator.rewrite_item(item, api_key="test-key")
    assert fallback.headline == digest_view.presentation_for(item, "Executive").headline
    assert fallback.narration["fallback_kind"] == "unavailable"
    assert "secret provider diagnostic" not in fallback.narration["fallback_reason"]


def test_router_digest_view_uses_session_history_drills_through_and_downloads():
    at = AppTest.from_file(APP, default_timeout=180).run()
    at.radio(key="nav").set_value("Digest").run()
    assert not at.exception
    assert isinstance(at.session_state["_digest_session_history_store"],
                      InMemoryDigestHistoryStore)
    labels = [button.label for button in at.get("download_button")]
    assert labels.count("Download details") == 3
    assert "Download complete digest" in labels
    assert not at.code
    evidence = [expander for expander in at.expander if expander.label == "Evidence"]
    assert len(evidence) == 3
    assert all(any("Fact hash:" in str(caption.value)
                   for caption in expander.caption)
               for expander in evidence)
    next(button for button in at.button if button.label == "Break this down").click().run()
    assert at.session_state["nav"] == "Home"
    assert at.session_state["ask_src"] in sl.SOURCES
    assert at.session_state["ask_var"] != "governed default"
    assert at.session_state["ask_q"]


def test_share_divergence_headline_formats_ratio_values_as_percentages():
    candidate = next(
        candidate for candidate in digest.scan_candidates(persona="Executive").candidates
        if candidate.kind == "divergence" and candidate.metric == "trx_share"
    )
    headline, _ = digest._headline(candidate)
    assert headline.count("%") >= 3  # raw artifact contract remains unchanged
    assert "governed 0.1 vs" not in headline
