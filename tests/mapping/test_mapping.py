from __future__ import annotations

import pytest

from kpi_evidence.ai import LocalHeuristicProvider, OffProvider
from kpi_evidence.contracts import KpiMapping, MappingMethod, MappingProposal, MappingStatus
from kpi_evidence.ids import stable_id, utcnow
from kpi_evidence.mapping import (
    DECIDED_BY, fingerprint, label_is_uninformative, longest_synonym_hits, map_observations, mask_text,
    meaning_flags, remember_mapping, synonym_phrases, units_compatible,
)
from mapping_testkit import MEANING_FLAG, FakeProvider, make_obs


def _map(store, registry, settings, observations, provider=None):
    return map_observations(store, registry, settings, observations, provider or OffProvider())


def _one(store, registry, settings, obs, provider=None) -> KpiMapping:
    (m,) = _map(store, registry, settings, [obs], provider)
    return m


def test_units_compatible_families():
    assert units_compatible("fraction", "fraction")
    assert not units_compatible("fraction", "count")
    assert not units_compatible("count", "fraction")
    assert units_compatible("usd", "count") and units_compatible("count", "usd")
    assert units_compatible(None, "fraction") and units_compatible("unknown", "count")


def test_longest_whole_phrase_wins(registry):
    phrases = synonym_phrases(registry.approved())
    assert {p.kpi_id for p in longest_synonym_hits("TRx share", phrases)} == {"trx_share"}
    assert {p.kpi_id for p in longest_synonym_hits("New to brand prescriptions", phrases)} == {"nbrx"}
    assert {p.kpi_id for p in longest_synonym_hits("Call plan attainment", phrases)} == {"call_attainment"}
    assert longest_synonym_hits("atrx nrxtra", phrases) == []
    assert {p.kpi_id for p in longest_synonym_hits("Referral", phrases)} == {"referrals_in"}
    assert {p.kpi_id for p in longest_synonym_hits("Sample units", phrases)} == {"samples"}


def test_meaning_flag_detection():
    assert meaning_flags([MEANING_FLAG, {"code": "MISSING_CHART_POINTS", "dimension": "extraction"}]) == [
        "NEARBY_LABEL_UNVERIFIED"]
    assert meaning_flags(["TABLE_HEADER_UNCONFIRMED", "DERIVED_CHANGE"]) == ["TABLE_HEADER_UNCONFIRMED"]


def test_uninformative_labels():
    assert label_is_uninformative("May 2026")
    assert label_is_uninformative("Q2-2026 YTD")
    assert label_is_uninformative("Dec-25")
    assert label_is_uninformative("West region", scope={"region": "West"})
    assert not label_is_uninformative("TRx")
    assert mask_text("TRx May 2026") == mask_text("TRx Jun 2027") == "trx <month> #"


def test_fingerprint_prefers_stored_value_and_has_fallback():
    assert fingerprint(make_obs("TRx", fp="abc")) == "abc"
    a = fingerprint({"label": "TRx May 2026", "label_context": "Brand review · TRx"})
    b = fingerprint({"label": "TRx Jun 2026", "label_context": "Brand review · TRx"})
    assert a == b and len(a) == 32


@pytest.mark.parametrize("label, unit, expected", [
    ("TRx share", "fraction", "trx_share"),
    ("TRx", "count", "trx"),
    ("New to brand prescriptions", "count", "nbrx"),
    ("Ex-factory units", "count", "exfactory_units"),
    ("IL-17 class share", "fraction", "trx_share"),
    ("47 HCPs attended", "count", "speaker_attendance"),
])
def test_unique_synonym_is_accepted(mem_store, registry, settings, label, unit, expected):
    m = _one(mem_store, registry, settings, make_obs(label, unit=unit))
    assert (m.status, m.method, m.kpi_id) == (MappingStatus.ACCEPTED, MappingMethod.SYNONYM, expected)
    assert m.kpi_version == registry.kpis[expected].version
    assert m.registry_version == registry.version and m.decided_by == DECIDED_BY


def test_tie_between_kpis_is_ambiguous(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("TRx and NRx", unit="count"))
    assert m.status == MappingStatus.AMBIGUOUS and m.kpi_id is None
    assert m.reason.startswith("multiple_candidates")
    assert {c["kpi_id"] for c in m.candidates} == {"trx", "nrx"}
    assert mem_store.count(MappingProposal, "observation_id = :o", {"o": m.observation_id}) == 2


def test_tie_is_ambiguous_even_when_only_one_kpi_has_a_compatible_unit(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("NRx share", unit="fraction"))
    assert m.status == MappingStatus.AMBIGUOUS and m.kpi_id is None


def test_label_unit_mismatch_needs_a_person(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("Call attainment", unit="count"))
    assert m.status == MappingStatus.AMBIGUOUS and m.kpi_id == "call_attainment"
    assert m.reason.startswith("unit_mismatch")
    m2 = _one(mem_store, registry, settings, make_obs("TRx", unit="fraction"))
    assert m2.status == MappingStatus.AMBIGUOUS and m2.reason.startswith("unit_mismatch")


@pytest.mark.parametrize("unit", [None, "unknown", "usd"])
def test_unknown_or_amount_units_pass_the_gate(mem_store, registry, settings, unit):
    m = _one(mem_store, registry, settings, make_obs("TRx", unit=unit))
    assert m.status == MappingStatus.ACCEPTED and m.kpi_id == "trx"


def test_context_only_match_with_wrong_unit_is_no_match(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("Kestrel", unit="fraction", context="TRx trend · Kestrel"))
    assert m.status == MappingStatus.NO_MATCH and m.reason.startswith("no_registry_match")


def test_model_candidates_are_narrowed_by_unit(mem_store, registry, settings):
    provider = FakeProvider()
    _one(mem_store, registry, settings, make_obs("Gross widgets", unit="fraction"), provider)
    (request,) = provider.calls[0]
    assert request.candidates and all(registry.kpis[k].unit == "fraction" for k in request.candidates)


def test_derived_change_is_skipped(mem_store, registry, settings):
    provider = FakeProvider()
    obs = make_obs("TRx vs PY %", unit="fraction", flags=[{"code": "DERIVED_CHANGE", "dimension": "interpretation"}])
    m = _one(mem_store, registry, settings, obs, provider)
    assert m.status == MappingStatus.NO_MATCH and m.reason.startswith("derived_change")
    assert provider.calls == []
    assert mem_store.count(MappingProposal, "observation_id = :o", {"o": obs.observation_id}) == 0


def test_image_not_parsed_is_no_value(mem_store, registry, settings):
    obs = make_obs("", unit=None, value=None, flags=[{"code": "IMAGE_NOT_PARSED", "dimension": "extraction"}])
    m = _one(mem_store, registry, settings, obs, FakeProvider())
    assert m.status == MappingStatus.NO_MATCH and m.reason.startswith("no_value")


def test_period_only_label_uses_label_context(mem_store, registry, settings):
    m = _one(mem_store, registry, settings,
             make_obs("May 2026", context="Monthly brand review · Total prescriptions · May 2026"))
    assert m.status == MappingStatus.ACCEPTED and m.kpi_id == "trx"
    assert m.candidates[0]["source"] == "label_context"


def test_scope_only_label_uses_label_context(mem_store, registry, settings):
    m = _one(mem_store, registry, settings,
             make_obs("West", scope={"region": "West"}, context="NRx by region · West"))
    assert m.status == MappingStatus.ACCEPTED and m.kpi_id == "nrx"


def test_brand_only_label_uses_label_context(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("Kestrel", context="Brand review > TRx trend > Kestrel"))
    assert m.status == MappingStatus.ACCEPTED and m.kpi_id == "trx"


def test_informative_label_never_borrows_a_kpi_from_context(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("Market", context="Brand review > TRx trend > Market"))
    assert m.status == MappingStatus.NO_MATCH and m.reason == "no_registry_match"


def test_memory_beats_synonym(mem_store, registry, settings):
    obs = make_obs("TRx May 2026", fp="fp-memory")
    mem = remember_mapping(mem_store, obs, "nrx", "reviewer@example.com", decision_id="dec-1")
    assert mem.memory_id == stable_id("fp-memory", "trx <month> #")
    next_month = make_obs("TRx Jun 2026", fp="fp-memory")
    m = _one(mem_store, registry, settings, next_month)
    assert (m.status, m.method, m.kpi_id) == (MappingStatus.ACCEPTED, MappingMethod.MEMORY, "nrx")


def test_inactive_or_unknown_memory_is_ignored(mem_store, registry, settings):
    obs = make_obs("TRx", fp="fp-x")
    mem = remember_mapping(mem_store, obs, "retired_kpi", "reviewer@example.com")
    m = _one(mem_store, registry, settings, obs)
    assert m.method == MappingMethod.SYNONYM and m.kpi_id == "trx"
    mem.kpi_id, mem.active = "nrx", False
    mem_store.upsert([mem])
    assert _one(mem_store, registry, settings, obs).kpi_id == "trx"


def test_meaning_flag_blocks_auto_accept(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("TRx", flags=[MEANING_FLAG]))
    assert m.status == MappingStatus.AMBIGUOUS and m.kpi_id == "trx"
    assert m.reason.startswith("meaning_flagged") and "NEARBY_LABEL_UNVERIFIED" in m.reason


def test_extraction_flags_do_not_block_mapping(mem_store, registry, settings):
    obs = make_obs("TRx", flags=[{"code": "MISSING_CHART_POINTS", "dimension": "extraction"}])
    assert _one(mem_store, registry, settings, obs).status == MappingStatus.ACCEPTED


def test_memory_confirms_a_meaning_flagged_label(mem_store, registry, settings):
    obs = make_obs("TRx", flags=[MEANING_FLAG], fp="fp-flagged")
    remember_mapping(mem_store, obs, "trx", "reviewer@example.com")
    m = _one(mem_store, registry, settings, obs)
    assert (m.status, m.method) == (MappingStatus.ACCEPTED, MappingMethod.MEMORY)


def test_memory_with_incompatible_unit_is_not_accepted(mem_store, registry, settings):
    obs = make_obs("Attainment", unit="count", fp="fp-unit")
    remember_mapping(mem_store, obs, "call_attainment", "reviewer@example.com")
    m = _one(mem_store, registry, settings, obs)
    assert m.status == MappingStatus.AMBIGUOUS and m.reason.startswith("unit_mismatch")


def test_model_proposal_is_never_accepted(mem_store, registry, settings):
    observations = [make_obs("Gross widgets", obs_id="o1"), make_obs("Something else", unit="fraction", obs_id="o2"),
                    make_obs("Kestrel", context="Brand page", obs_id="o3")]
    mappings = _map(mem_store, registry, settings, observations, FakeProvider(confidence=0.99))
    assert all(m.status == MappingStatus.AMBIGUOUS for m in mappings)
    for m in mappings:
        assert m.method == MappingMethod.MODEL and m.reason.startswith("model_proposal_only")
        assert m.candidates[0]["method"] == "MODEL" and m.candidates[0]["kpi_id"] == m.kpi_id
    rows = mem_store.read(MappingProposal, "observation_id = :o", {"o": "o1"})
    assert len(rows) == 1 and rows[0].model == "fake-model" and rows[0].prompt_sha == "sha-fake"
    assert rows[0].rationale == "Fake proposal."
    assert rows[0].proposal_id == stable_id("o1", "MODEL", rows[0].kpi_id, registry.version)


def test_low_confidence_model_proposal_stays_no_match(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("Gross widgets"), FakeProvider(confidence=0.3))
    assert m.status == MappingStatus.NO_MATCH and m.kpi_id is None
    assert m.candidates[0]["note"] == "below_floor"


def test_model_proposal_outside_candidates_is_rejected(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("Gross widgets"), FakeProvider(kpi_id="trx_share"))
    assert m.status == MappingStatus.NO_MATCH and "model proposal failed" in m.reason
    (row,) = mem_store.read(MappingProposal, "observation_id = :o", {"o": m.observation_id})
    assert row.kpi_id is None and "outside the candidate set" in row.rationale


def test_provider_exception_becomes_recorded_error(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("Gross widgets"), FakeProvider(raise_exc=RuntimeError("boom")))
    assert m.status == MappingStatus.NO_MATCH
    (row,) = mem_store.read(MappingProposal, "observation_id = :o", {"o": m.observation_id})
    assert row.method == MappingMethod.MODEL and row.kpi_id is None and "boom" in row.rationale


def test_model_fills_suggestion_for_a_tie_without_accepting(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("TRx and NRx"), FakeProvider(kpi_id="nrx", confidence=0.9))
    assert m.status == MappingStatus.AMBIGUOUS and m.kpi_id == "nrx"
    assert m.reason.startswith("multiple_candidates")


def test_off_provider_makes_no_proposals(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("Gross widgets"), OffProvider())
    assert m.status == MappingStatus.NO_MATCH and m.reason == "no_registry_match" and m.candidates == []
    assert mem_store.count(MappingProposal) == 0


def test_local_heuristic_proposal_goes_to_review(mem_store, registry, settings):
    m = _one(mem_store, registry, settings, make_obs("Referals recieved"), LocalHeuristicProvider())
    assert m.status == MappingStatus.AMBIGUOUS and m.method == MappingMethod.MODEL and m.kpi_id == "referrals_in"
    assert m.candidates[0]["confidence"] <= 0.75


def test_rerun_is_idempotent(mem_store, registry, settings):
    observations = [make_obs("TRx", obs_id="a"), make_obs("TRx and NRx", obs_id="b"), make_obs("Gross", obs_id="c")]
    provider = FakeProvider(confidence=0.9)
    first = _map(mem_store, registry, settings, observations, provider)
    n_props, n_maps = mem_store.count(MappingProposal), mem_store.count(KpiMapping)
    second = _map(mem_store, registry, settings, observations, provider)
    assert (mem_store.count(MappingProposal), mem_store.count(KpiMapping)) == (n_props, n_maps) == (5, 3)
    assert [m.decided_at for m in first] == [m.decided_at for m in second]
    assert [(m.status, m.kpi_id) for m in first] == [(m.status, m.kpi_id) for m in second]


def test_reviewer_mapping_is_never_overwritten(mem_store, registry, settings):
    obs = make_obs("TRx", obs_id="r1")
    _one(mem_store, registry, settings, obs)
    human = KpiMapping(observation_id="r1", registry_version=registry.version, status=MappingStatus.ACCEPTED,
                       method=MappingMethod.REVIEWER, kpi_id="nrx", kpi_version=1, candidates=[],
                       reason="corrected by reviewer", decided_by="reviewer@example.com", decided_at=utcnow())
    mem_store.upsert([human])
    m = _one(mem_store, registry, settings, obs)
    assert (m.method, m.kpi_id) == (MappingMethod.REVIEWER, "nrx")
    stored = mem_store.get(KpiMapping, observation_id="r1", registry_version=registry.version)
    assert stored.kpi_id == "nrx"


def test_results_follow_input_order_and_dedupe(mem_store, registry, settings):
    a, b = make_obs("TRx", obs_id="x1"), make_obs("NRx", obs_id="x2")
    out = _map(mem_store, registry, settings, [a, b, a])
    assert [m.observation_id for m in out] == ["x1", "x2"]
    assert _map(mem_store, registry, settings, []) == []
