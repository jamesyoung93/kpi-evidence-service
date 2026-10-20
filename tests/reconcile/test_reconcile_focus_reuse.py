from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path

import pytest
from recon_testkit import make_doc, make_mapping, make_obs, run

from kpi_evidence import platform as platform_module
from kpi_evidence.compile import compile_kpi
from kpi_evidence.contracts import KpiResult, MappingStatus, ReconRun, ReconTrial, ValidationStatus as VS, to_row
from kpi_evidence.materialize import materialize
from kpi_evidence.reconcile import REUSED_KEY, evaluate_document, is_reused
from kpi_evidence.registry import GRAINS, Registry, parse_kpi

ACCOUNT = "platform_demo.rx_account_monthly"
MARCH, APRIL, MAY = date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31)


class _Counting:
    def __init__(self, inner, calls):
        self.inner, self.calls = inner, calls

    def run(self, sql):
        self.calls.append(sql)
        return self.inner.run(sql)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _count_runner_calls(store, monkeypatch) -> list[str]:
    calls: list[str] = []
    access = store.platform_access
    original = access.runner
    monkeypatch.setattr(access, "runner", lambda: _Counting(original(), calls))
    return calls


def _state(store) -> dict[str, list[str]]:
    return {t.TABLE: sorted(json.dumps(to_row(x), default=str, sort_keys=True) for x in store.read(t))
            for t in (KpiResult, ReconRun, ReconTrial)}


def _pairs(doc):
    return [(make_obs(doc, "TRx", 5783.04, half=0.005), "trx"),
            (make_obs(doc, "Samples", 2625.0, half=0.5), "samples"),
            (make_obs(doc, "Call attainment", 0.9192, unit="fraction", half=0.00005, stated="writers only"),
             "call_attainment")]


def _hint_kpi(value_sql: str, kpi_id: str = "fragile_trx"):
    platform = {"from": ACCOUNT, "value": value_sql, "brand_column": None,
                "date_expr": "CAST(month || '-01' AS DATE)", "scope_columns": {}, "source_views": [ACCOUNT]}
    raw = {"kpi_id": kpi_id, "version": 1, "status": "DRAFT", "label": "Fragile TRx", "definition": "test hint",
           "unit": "count", "aggregation": "sum", "grains": list(GRAINS), "dimensions": [], "synonyms": ["fragile"],
           "tolerance": {"rel": 0.005, "abs": 0.0}, "platform": platform, "definition_source": "hint"}
    return parse_kpi(raw)


def _platform_value(store, kpi, settings, as_of: date) -> float:
    compiled = compile_kpi(kpi, brand="KESTREL", scope={}, grain="month", settings=settings)
    return compiled.spec.compute(store.runner(), compiled.governed_defaults, as_of.isoformat())[0]


def test_one_failing_period_does_not_abort_the_other_periods(demo_store, settings):
    kpi = _hint_kpi("SUM(CASE WHEN month = '2026-04' THEN CAST(error('boom') AS DOUBLE) ELSE trx_units END)")
    registry = Registry(kpis={kpi.kpi_id: kpi}, brands={}, report_types={}, goal_list=[], policy={},
                        directory=Path("."))
    doc = make_doc(demo_store)
    values = {MARCH: _platform_value(demo_store, kpi, settings, MARCH), APRIL: 1000.0,
              MAY: _platform_value(demo_store, kpi, settings, MAY)}
    pairs = [(make_obs(doc, "Fragile", v, period_end=end, key=str(end)), kpi.kpi_id) for end, v in values.items()]
    results = {r.period_end: r for r in run(demo_store, registry, settings, doc, pairs)}
    assert results[MARCH].validation_status == VS.VERIFIED
    assert results[MAY].validation_status == VS.VERIFIED
    assert results[APRIL].validation_status == VS.BLOCKED
    assert "boom" in results[APRIL].explanation["error"]
    assert "aborted" not in json.dumps([r.explanation for r in results.values()])


def test_focus_reconciles_only_focus_observations(demo_store, registry, settings):
    doc = make_doc(demo_store)
    pairs = _pairs(doc)
    observations = [obs for obs, _ in pairs]
    mappings = [make_mapping(obs, kpi) for obs, kpi in pairs]
    results = evaluate_document(demo_store, registry, settings, doc, observations, mappings, {}, "run-1",
                                focus_kpis={"trx"})
    assert [r.kpi_id for r in results] == ["trx"]
    assert {r.kpi_id for r in demo_store.read(KpiResult)} == {"trx"}


def test_focused_rerun_keeps_the_other_results_current(demo_store, registry, settings):
    doc = make_doc(demo_store)
    pairs = _pairs(doc)
    run(demo_store, registry, settings, doc, pairs)
    observations = [obs for obs, _ in pairs]
    mappings = [make_mapping(obs, kpi) for obs, kpi in pairs]
    evaluate_document(demo_store, registry, settings, doc, observations, mappings, {}, "run-2", focus_kpis={"trx"})
    current = {r.kpi_id: r.is_current for r in demo_store.read(KpiResult)}
    assert current == {"trx": True, "samples": True, "call_attainment": True}


def test_focused_rerun_retires_results_of_vanished_observations(demo_store, registry, settings):
    doc = make_doc(demo_store)
    pairs = _pairs(doc)
    run(demo_store, registry, settings, doc, pairs)
    trx_obs = pairs[0][0]
    evaluate_document(demo_store, registry, settings, doc, [trx_obs], [make_mapping(trx_obs, "trx")], {}, "run-2",
                      focus_kpis={"trx"})
    current = {r.kpi_id: r.is_current for r in demo_store.read(KpiResult)}
    assert current == {"trx": True, "samples": False, "call_attainment": False}


def test_ambiguous_match_with_a_focus_candidate_is_included(demo_store, registry, settings):
    doc = make_doc(demo_store)
    obs = make_obs(doc, "Rx", 5783.04)
    mapping = make_mapping(obs, None, status=MappingStatus.AMBIGUOUS, reason="multiple_candidates",
                           candidates=[{"kpi_id": "trx", "confidence": 0.5}, {"kpi_id": "nrx", "confidence": 0.5}])
    results = evaluate_document(demo_store, registry, settings, doc, [obs], [mapping], {}, "run-1",
                                focus_kpis={"nrx"})
    assert len(results) == 1 and results[0].validation_status == VS.AMBIGUOUS_MAPPING
    none = evaluate_document(demo_store, registry, settings, doc, [obs], [mapping], {}, "run-2",
                             focus_kpis={"samples"})
    assert none == []


def test_unchanged_inputs_reuse_stored_results_without_platform_sql(demo_store, registry, settings, monkeypatch):
    doc = make_doc(demo_store)
    first = run(demo_store, registry, settings, doc, _pairs(doc))
    before = _state(demo_store)
    calls = _count_runner_calls(demo_store, monkeypatch)
    second = run(demo_store, registry, settings, doc, _pairs(doc), run_id="run-2")
    assert calls == []
    assert all(is_reused(r) and r.explanation[REUSED_KEY] == "run-1" for r in second)
    assert [(r.result_id, r.result_version, r.platform_value, r.validation_status) for r in first] == \
        [(r.result_id, r.result_version, r.platform_value, r.validation_status) for r in second]
    assert _state(demo_store) == before


def test_reuse_can_be_turned_off(demo_store, registry, settings, monkeypatch):
    doc = make_doc(demo_store)
    run(demo_store, registry, settings, doc, _pairs(doc))
    calls = _count_runner_calls(demo_store, monkeypatch)
    off = dataclasses.replace(settings, extra={**settings.extra, "reuse_results": False})
    second = run(demo_store, registry, off, doc, _pairs(doc), run_id="run-2")
    assert calls and not any(is_reused(r) for r in second)


def test_platform_data_change_recomputes_the_affected_kpis(demo_store, registry, settings, monkeypatch):
    doc = make_doc(demo_store)
    pairs = [(make_obs(doc, "TRx", 5783.04, half=0.005), "trx"),
             (make_obs(doc, "Referrals", 168.0), "referrals_in")]
    first = {r.kpi_id: r for r in run(demo_store, registry, settings, doc, pairs)}
    s = demo_store.settings
    demo_store.execute(f"UPDATE {s.platform_schema}.rx_account_monthly SET trx_units = trx_units + 100 "
                       f"WHERE month = '2026-06'")
    calls = _count_runner_calls(demo_store, monkeypatch)
    second = {r.kpi_id: r for r in run(demo_store, registry, settings, doc, pairs, run_id="run-2")}
    assert calls and not is_reused(second["trx"]) and is_reused(second["referrals_in"])
    assert second["trx"].platform_value > first["trx"].platform_value
    assert second["trx"].result_version == 2 and second["trx"].recon_run_id != first["trx"].recon_run_id


def test_unavailable_snapshot_never_reuses(demo_store, registry, settings, monkeypatch):
    doc = make_doc(demo_store)
    monkeypatch.setattr(platform_module, "platform_snapshot",
                        lambda store, settings, views: {"main.sales.view": "unavailable: not a Delta table"})
    run(demo_store, registry, settings, doc, _pairs(doc))
    calls = _count_runner_calls(demo_store, monkeypatch)
    second = run(demo_store, registry, settings, doc, _pairs(doc), run_id="run-2")
    assert calls and not any(is_reused(r) for r in second)


def test_changed_definition_recomputes_only_that_kpi(demo_store, registry, settings, monkeypatch):
    doc = make_doc(demo_store)
    pairs = _pairs(doc)
    run(demo_store, registry, settings, doc, pairs)
    samples = registry.kpis["samples"]
    looser = dataclasses.replace(samples, raw={**samples.raw, "tolerance": {"rel": 0.2, "abs": 0.0}},
                                 tolerance=dataclasses.replace(samples.tolerance, rel=0.2))
    changed = dataclasses.replace(registry, kpis={**registry.kpis, "samples": looser})
    calls = _count_runner_calls(demo_store, monkeypatch)
    second = {r.kpi_id: r for r in run(demo_store, changed, settings, doc, pairs, run_id="run-2")}
    assert calls and not is_reused(second["samples"])
    assert is_reused(second["trx"]) and is_reused(second["call_attainment"])
    assert second["samples"].validation_status == VS.VERIFIED


def test_materialize_skips_series_when_platform_data_is_unchanged(demo_store, registry, settings, monkeypatch):
    doc = make_doc(demo_store)
    results = run(demo_store, registry, settings, doc, [(make_obs(doc, "TRx", 5783.04, half=0.005), "trx")])
    materialize(demo_store, registry, settings, results, "run-1")
    calls = _count_runner_calls(demo_store, monkeypatch)
    again = run(demo_store, registry, settings, doc, [(make_obs(doc, "TRx", 5783.04, half=0.005), "trx")],
                run_id="run-2")
    materialize(demo_store, registry, settings, again, "run-2")
    assert calls == []
    s = demo_store.settings
    demo_store.execute(f"UPDATE {s.platform_schema}.rx_account_monthly SET trx_units = trx_units + 1 "
                       f"WHERE month = '2024-07'")
    changed = run(demo_store, registry, settings, doc, [(make_obs(doc, "TRx", 5783.04, half=0.005), "trx")],
                  run_id="run-3")
    materialize(demo_store, registry, settings, changed, "run-3")
    assert len(calls) > 20
    july = demo_store.read(KpiResult, "kpi_id = 'trx'")[0]
    assert july.platform_value == pytest.approx(5783.040615)
