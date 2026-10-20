from __future__ import annotations

import copy
import json

from recon_testkit import make_doc, make_obs, one, run

from kpi_evidence.compile import compile_kpi
from kpi_evidence.contracts import KpiResult, ReconRun, ReconTrial, ValidationStatus as VS, to_row
from kpi_evidence.reconcile import TrialSink, recording


def _trials(store, recon_run_id):
    return store.read(ReconTrial, "recon_run_id = :r", {"r": recon_run_id}, order_by="seq")


def test_sink_survives_deepcopy(registry, settings):
    compiled = compile_kpi(registry.kpis["trx"], brand="KESTREL", scope={}, grain="month", settings=settings)
    rspec = recording(compiled.spec)
    clone = copy.deepcopy(rspec)
    assert clone.sink is rspec.sink and isinstance(clone.sink, TrialSink)


def test_every_compute_call_is_recorded(demo_store, registry, settings):
    doc = make_doc(demo_store)
    obs = make_obs(doc, "Call attainment", 0.9192, unit="fraction", half=0.00005, stated="writers only")
    r = one(demo_store, registry, settings, doc, obs, "call_attainment")
    run_row = demo_store.get(ReconRun, recon_run_id=r.recon_run_id)
    trials = _trials(demo_store, r.recon_run_id)
    attempts = sum(len(rec["iterations"]) for rec in run_row.record["phase_a"]) + len(run_row.record["phase_b"]["iterations"])
    assert len(trials) > attempts
    assert {t.phase for t in trials} == {"phase_a", "phase_b", "grid"}
    assert [t.seq for t in trials] == list(range(len(trials)))
    grid = {json.dumps(t.choices, sort_keys=True) for t in trials if t.phase == "grid"}
    assert len(grid) == 4
    assert all(t.sql and "{as_of}" not in t.sql and t.sql_sha for t in trials)
    assert all(t.as_of.isoformat() == "2026-06-30" for t in trials)
    assert run_row.start_choices == {"aggregation": "ratio_of_sums", "eligibility": "all_hcps"}
    assert run_row.final_choices["eligibility"] == "writers_only"
    assert run_row.phase_a_outcome == "not_verified" and run_row.phase_b_outcome == "reconciled"
    assert run_row.judge == "stated_rule" and run_row.policy["max_changes"] == 3
    assert run_row.reconloop_version == "0.5.0" and run_row.bindings["brand"] == "KESTREL"
    assert run_row.record["stated_other"] == {"eligibility": "writers_only"}


def test_trial_errors_are_kept(demo_store, registry, settings):
    doc = make_doc(demo_store)
    obs = make_obs(doc, "TRx share", 0.5, unit="fraction", half=0.0005)
    r = one(demo_store, registry, settings, doc, obs, "trx_share")
    assert r.validation_status == VS.VARIANCE_REVIEW
    trials = _trials(demo_store, r.recon_run_id)
    assert all((t.value is None) == (t.error is not None) for t in trials)
    failed = [t for t in trials if t.error]
    assert failed and all(t.choices["source"] == "panel" and "quality check" in t.error for t in failed)
    assert {t.phase for t in failed} >= {"phase_b", "grid"}
    assert any(t.value is not None for t in trials if t.phase == "grid")
    errors = [t for t in _trials(demo_store, one(demo_store, registry, settings, doc,
                                                 make_obs(doc, "Ex-factory", 6000.0, key="x"),
                                                 "exfactory_units").recon_run_id)]
    assert errors and errors[0].error.startswith("AccessDenied")


def test_idempotent_rerun(demo_store, registry, settings):
    doc = make_doc(demo_store)
    pairs = [(make_obs(doc, "TRx", 5783.04, half=0.005), "trx"),
             (make_obs(doc, "Samples", 2625.0, half=0.5), "samples"),
             (make_obs(doc, "Call attainment", 0.9192, unit="fraction", half=0.00005, stated="writers only"),
              "call_attainment"),
             (make_obs(doc, "Ex-factory", 6000.0), "exfactory_units"),
             (make_obs(doc, "Awareness", 0.62, unit="fraction"), "brand_awareness")]
    first = run(demo_store, registry, settings, doc, pairs)
    snapshot = {t.TABLE: sorted(json.dumps(to_row(x), default=str, sort_keys=True) for x in demo_store.read(t))
                for t in (KpiResult, ReconRun, ReconTrial)}
    second = run(demo_store, registry, settings, doc, pairs, run_id="run-2")
    assert [(r.result_id, r.result_version) for r in first] == [(r.result_id, r.result_version) for r in second]
    assert all(r.result_version == 1 for r in second)
    again = {t.TABLE: sorted(json.dumps(to_row(x), default=str, sort_keys=True) for x in demo_store.read(t))
             for t in (KpiResult, ReconRun, ReconTrial)}
    assert again == snapshot


def test_result_version_bumps_on_changed_input(demo_store, registry, settings):
    doc = make_doc(demo_store)
    obs = make_obs(doc, "Samples", 2476.0)
    v1 = one(demo_store, registry, settings, doc, obs, "samples")
    assert (v1.validation_status, v1.result_version) == (VS.VERIFIED, 1)
    changed = make_obs(doc, "Samples", 2625.0)
    assert changed.observation_id == obs.observation_id
    v2 = one(demo_store, registry, settings, doc, changed, "samples", run_id="run-2")
    assert v2.result_id == v1.result_id and v2.result_version == 2
    assert v2.validation_status == VS.VARIANCE_REVIEW
    stored = demo_store.get(KpiResult, result_id=v1.result_id)
    assert stored.result_version == 2 and stored.run_id == "run-2"
    assert demo_store.count(KpiResult) == 1


def test_results_no_longer_produced_become_not_current(demo_store, registry, settings):
    doc = make_doc(demo_store)
    a, b = make_obs(doc, "TRx", 5783.04, half=0.005), make_obs(doc, "Samples", 2476.0)
    run(demo_store, registry, settings, doc, [(a, "trx"), (b, "samples")])
    run(demo_store, registry, settings, doc, [(a, "trx")], run_id="run-2")
    current = {r.kpi_id: r.is_current for r in demo_store.read(KpiResult)}
    assert current == {"trx": True, "samples": False}
