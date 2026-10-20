from __future__ import annotations

from recon_testkit import make_doc, make_mapping, make_obs, one

from kpi_evidence.contracts import MappingMethod, MappingStatus, ValidationStatus as VS


def _ambiguous(obs, *kpi_ids):
    candidates = [{"kpi_id": k, "method": "MODEL", "confidence": 0.7} for k in kpi_ids]
    return make_mapping(obs, kpi_ids[0] if kpi_ids else None, status=MappingStatus.AMBIGUOUS,
                        method=MappingMethod.MODEL, candidates=candidates, reason="model_proposal_only")


def test_a_single_candidate_with_another_unit_is_not_offered_as_a_choice(mem_store, registry, settings):
    doc = make_doc(mem_store)
    obs = make_obs(doc, "TRx lift from Speaker programs", 0.08, unit="percent")
    r = one(mem_store, registry, settings, doc, obs, _ambiguous(obs, "trx"))
    assert r.validation_status == VS.AMBIGUOUS_MAPPING
    assert "one of" not in r.reason_detail
    assert r.reason_detail == ("'TRx lift from Speaker programs' may not be trx: the report gives it as 'fraction' but "
                               "Total prescriptions (TRx) is measured as 'count'. Confirm whether it is this KPI.")


def test_a_single_candidate_with_the_same_unit_asks_for_a_confirmation(mem_store, registry, settings):
    doc = make_doc(mem_store)
    obs = make_obs(doc, "Rx volume", 6200.0, unit="count")
    r = one(mem_store, registry, settings, doc, obs, _ambiguous(obs, "trx"))
    assert r.reason_detail == "'Rx volume' may not be trx. Confirm whether it is this KPI."


def test_several_candidates_are_listed_as_a_choice(mem_store, registry, settings):
    doc = make_doc(mem_store)
    obs = make_obs(doc, "Scripts", 6200.0, unit="count")
    r = one(mem_store, registry, settings, doc, obs, _ambiguous(obs, "trx", "nrx"))
    assert r.reason_detail == "'Scripts' could be one of: nrx, trx. Confirm which KPI it is."
