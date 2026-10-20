from __future__ import annotations

from datetime import date

import pytest

from kpi_evidence.contracts import ReconRun
from kpi_evidence.contracts import ValidationStatus as S
from kpi_evidence.review import PRIOR_DECISION_LABEL, decide, evidence


def test_evidence_assembles_every_part(seeder, store, registry):
    doc = seeder.doc()
    page = seeder.page(doc, 2, title="TRx trend")
    s = seeder.seed("trx", S.VERIFIED, doc=doc, page=2)
    run = seeder.recon(s)
    seeder.proposal(s, "trx")
    ev = evidence(store, registry, s.result.result_id)
    assert ev.result.result_id == s.result.result_id
    assert ev.observation.observation_id == s.obs.observation_id
    assert ev.value.value_id == s.value.value_id and ev.document.doc_id == doc.doc_id
    assert ev.page is not None and ev.page.title == page.title and ev.page.shapes[0]["kind"] == "chart"
    assert ev.kpi["kpi_id"] == "trx" and ev.kpi["label"] == registry.kpis["trx"].label
    assert ev.kpi["definition_hash"] == registry.kpis["trx"].definition_hash()
    assert ev.kpi["tolerance"]["rel"] == 0.005 and "measure" in ev.kpi["params"]
    assert ev.recon_run["recon_run_id"] == run.recon_run_id
    assert ev.recon_run["start_choices"] == {"measure": "units", "source": "account_feed"}
    assert ev.recon_run["phase_a_outcome"] == "verified"
    assert list(ev.trials["seq"]) == [1, 2]
    first, second = ev.trials.iloc[0], ev.trials.iloc[1]
    assert first["choices_text"] == "measure=units, source=account_feed" and bool(first["is_start_choice"])
    assert first["rel_diff"] == pytest.approx((5909.8 - 5910.0) / 5910.0)
    assert second["error"] == "boom" and second["sql"] == "SELECT 2 AS value"
    assert ev.sql_artifact.sql_sha == "sha-artifact"
    assert len(ev.platform_series) == 2 and list(ev.platform_series["value"]) == [5700.0, 5909.8]
    assert ev.mapping.kpi_id == "trx" and len(ev.mapping_proposals) == 1
    assert ev.status_info.label == "Matches platform"
    assert ev.prior_decisions.empty
    story = ev.story()
    assert story == ("Report says 5,910 (slide 2, May 2026). Platform says 5,909.8 for Total prescriptions (TRx). "
                     "Difference +0.0%. Matches platform.")


def test_evidence_for_unknown_result_raises(store, registry):
    with pytest.raises(KeyError):
        evidence(store, registry, "missing")


def test_evidence_without_run_or_kpi(seeder, store, registry):
    s = seeder.seed(None, S.UNMAPPED_CAPTURED, platform_value=None, mapping_method=None)
    ev = evidence(store, registry, s.result.result_id)
    assert ev.kpi is None and ev.recon_run is None and ev.trials.empty and ev.platform_series.empty
    assert ev.sql_artifact is None and ev.prior_decisions.empty


def test_kpi_comes_from_registry_table_when_no_registry(seeder, store, registry):
    store.upsert(registry.registry_rows())
    s = seeder.seed("trx", S.VERIFIED)
    ev = evidence(store, None, s.result.result_id)
    assert ev.kpi["label"] == registry.kpis["trx"].label and ev.kpi["spec"]["kpi_id"] == "trx"


def test_prior_decisions_from_other_documents_are_informative_and_not_valid(seeder, store, registry, settings):
    may = seeder.seed("trx", S.VERIFIED, doc=seeder.doc("may.pptx"), fingerprint="fp-trend")
    seeder.recon(may)
    decide(store, registry, settings, may.result.result_id, "ACCEPT", "", "rev@example.com", decision_id="d-may")
    june = seeder.seed("trx", S.VARIANCE_REVIEW, doc=seeder.doc("june.pptx"), fingerprint="fp-trend",
                       period_end=date(2026, 6, 30))
    seeder.recon(june)
    unrelated = seeder.seed("trx", S.VERIFIED, doc=seeder.doc("other.pptx"), fingerprint="fp-other")
    decide(store, registry, settings, unrelated.result.result_id, "ACCEPT", "", "rev@example.com")
    prior = evidence(store, registry, june.result.result_id).prior_decisions
    assert list(prior["decision_id"]) == ["d-may"]
    row = prior.iloc[0]
    assert bool(row["informative"]) and row["label"] == PRIOR_DECISION_LABEL
    assert bool(row["still_valid"]) is False and bool(row["file_changed"]) is True
    assert "source file" in row["changes"] and row["file_name"] == "may.pptx" and row["action"] == "ACCEPT"


def test_prior_decision_on_an_earlier_version_tracks_platform_changes(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VARIANCE_REVIEW, fingerprint="fp-v")
    run = seeder.recon(s, snapshot={"gv_rx_account_monthly": "v1"})
    decide(store, registry, settings, s.result.result_id, "ACCEPT", "known gap", "rev@example.com", decision_id="d-v1")
    s.result.result_version = 2
    store.upsert([s.result])
    prior = evidence(store, registry, s.result.result_id).prior_decisions
    assert list(prior["decision_id"]) == ["d-v1"]
    assert bool(prior.iloc[0]["still_valid"]) is True and bool(prior.iloc[0]["same_document"]) is True
    run.platform_snapshot = {"gv_rx_account_monthly": "v2"}
    store.upsert([run])
    prior = evidence(store, registry, s.result.result_id).prior_decisions
    row = prior.iloc[0]
    assert bool(row["still_valid"]) is False and bool(row["platform_changed"]) is True
    assert row["changes"] == "platform data"


def test_prior_decision_flags_a_changed_definition(seeder, store, registry, settings):
    may = seeder.seed("trx", S.VERIFIED, doc=seeder.doc("may.pptx"), fingerprint="fp-def")
    decide(store, registry, settings, may.result.result_id, "ACCEPT", "", "rev@example.com")
    may.result.definition_hash = "old-definition"
    store.upsert([may.result])
    june = seeder.seed("trx", S.VERIFIED, doc=seeder.doc("june.pptx"), fingerprint="fp-def")
    row = evidence(store, registry, june.result.result_id).prior_decisions.iloc[0]
    assert bool(row["definition_changed"]) is True and "KPI definition" in row["changes"]


def test_decisions_and_publications_of_the_result_are_attached(seeder, store, registry, settings):
    s = seeder.seed("trx", S.VERIFIED)
    decide(store, registry, settings, s.result.result_id, "ACCEPT", "", "rev@example.com")
    ev = evidence(store, registry, s.result.result_id)
    assert len(ev.decisions) == 1 and len(ev.publications) == 1
    assert store.count(ReconRun) == 0
