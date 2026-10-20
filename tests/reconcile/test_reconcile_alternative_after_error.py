from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path

import pytest
from recon_testkit import MONTH_ENDS, make_doc, make_obs, one, run

from kpi_evidence.contracts import ReconRun, ReconTrial, ValidationStatus as VS
from kpi_evidence.eval import truth
from kpi_evidence.registry import GRAINS, Registry, parse_kpi

MAY = date(2026, 5, 31)
PANEL = "platform_demo.rx_panel_monthly"
ACCOUNT = "platform_demo.rx_account_monthly"


def _kpi(tables: list[str], kpi_id: str = "calls_hint"):
    names = ["primary"] + [f"alt_{i}" for i in range(1, len(tables))]
    platform = {"value": "SUM(calls)", "brand_column": None, "date_expr": "CAST(month || '-01' AS DATE)",
                "scope_columns": {"region": "region"}, "source_views": list(tables)}
    if len(tables) == 1:
        platform["from"] = tables[0]
    else:
        platform["view"] = "{source}"
        platform["params"] = {"source": {"hypothesis": "source", "default": "primary",
                                         "options": dict(zip(names, tables)), "describe": dict(zip(names, tables)),
                                         "permitted": ["primary"]}}
    raw = {"kpi_id": kpi_id, "version": 1, "status": "DRAFT", "label": "Calls (hint)", "definition": "test hint",
           "unit": "count", "aggregation": "sum", "grains": list(GRAINS), "dimensions": ["region"],
           "synonyms": ["calls"], "tolerance": {"rel": 0.005, "abs": 0.0}, "platform": platform,
           "definition_source": "hint"}
    return parse_kpi(raw)


def _registry(kpi) -> Registry:
    return Registry(kpis={kpi.kpi_id: kpi}, brands={}, report_types={}, goal_list=[], policy={}, directory=Path("."))


def _trials(store, result):
    return store.read(ReconTrial, "recon_run_id = :r", {"r": result.recon_run_id}, order_by="seq")


def test_primary_missing_column_alternative_table_matches(demo_store, settings):
    kpi = _kpi([PANEL, ACCOUNT])
    doc = make_doc(demo_store)
    calls_may = truth.calls(MAY)
    r = one(demo_store, _registry(kpi), settings, doc,
            make_obs(doc, "Calls", float(calls_may), period_end=MAY, half=0.5), kpi.kpi_id)
    assert r.validation_status == VS.INCOMPATIBLE_DEFINITION
    assert r.reason_code == "matches_alternative_definition"
    ex = r.explanation
    assert ex["alternative"] == "source=alt_1"
    assert ex["matched_choices"] == {"source": "alt_1"}
    assert ex["alternative_value"] == pytest.approx(calls_may)
    assert ex["primary_status"] == "PLATFORM_DATA_ISSUE" and ex["primary_reason_code"] == "platform_object_missing"
    assert "calls" in ex["primary_error"]
    assert ACCOUNT in ex["alternative_sql"] and PANEL not in ex["alternative_sql"]
    assert r.reason_detail.startswith("The primary definition could not run (")
    assert f"the alternative source=alt_1 ({ACCOUNT}) reproduces the report: {calls_may:,.0f}." in r.reason_detail
    assert r.platform_value is None and r.sql_artifact_id is None
    assert r.definition_choices == {"source": "primary"}
    trials = _trials(demo_store, r)
    assert [t.phase for t in trials if t.phase == "phase_a"] and all(t.error for t in trials if t.phase == "phase_a")
    grid = [t for t in trials if t.phase == "grid"]
    assert {t.choices["source"] for t in grid} == {"primary", "alt_1"}
    assert any(t.choices["source"] == "primary" and t.error for t in grid)
    assert any(t.choices["source"] == "alt_1" and t.value == pytest.approx(calls_may) for t in grid)
    run_row = demo_store.get(ReconRun, recon_run_id=r.recon_run_id)
    assert run_row.phase_a_outcome == "error"
    assert run_row.record["error_grid"][MAY.isoformat()]["candidates"] == 2
    assert run_row.record["verdicts"][r.observation_id]["reason_code"] == "matches_alternative_definition"


def test_every_period_of_a_group_is_explained_including_holdouts(demo_store, settings):
    kpi = _kpi([PANEL, ACCOUNT])
    doc = make_doc(demo_store)
    pairs = [(make_obs(doc, "Calls", float(truth.calls(end)), period_end=end, half=0.5), kpi.kpi_id)
             for end in MONTH_ENDS]
    results = run(demo_store, _registry(kpi), settings, doc, pairs)
    assert {r.reason_code for r in results} == {"matches_alternative_definition"}
    assert [r.explanation["alternative_value"] for r in results] == pytest.approx(
        [truth.calls(end) for end in MONTH_ENDS])


def test_access_denied_primary_with_readable_alternative(demo_store, settings):
    demo_store.execute(f"CREATE TABLE platform_restricted.rx_copy AS SELECT * FROM {ACCOUNT}")
    kpi = _kpi(["platform_restricted.rx_copy", ACCOUNT])
    doc = make_doc(demo_store)
    r = one(demo_store, _registry(kpi), settings, doc,
            make_obs(doc, "Calls", float(truth.calls(MAY)), period_end=MAY, half=0.5), kpi.kpi_id)
    assert r.reason_code == "matches_alternative_definition"
    assert r.explanation["primary_status"] == "MISSING_ACCESS"
    assert "PERMISSION_DENIED" in r.reason_detail


def test_no_alternative_match_keeps_the_error_classification(demo_store, settings):
    kpi = _kpi([PANEL, ACCOUNT])
    doc = make_doc(demo_store)
    r = one(demo_store, _registry(kpi), settings, doc,
            make_obs(doc, "Calls", float(truth.calls(MAY)) * 1.10, period_end=MAY, half=0.5), kpi.kpi_id)
    assert r.validation_status == VS.PLATFORM_DATA_ISSUE
    assert r.reason_code == "platform_object_missing"
    grid = [t for t in _trials(demo_store, r) if t.phase == "grid"]
    assert any(t.choices["source"] == "alt_1" and t.value is not None for t in grid)
    assert demo_store.get(ReconRun, recon_run_id=r.recon_run_id).record["error_grid"]


def test_grid_budget_overrun_keeps_the_error_classification(demo_store, settings):
    tight = dataclasses.replace(settings, grid_max_candidates=1)
    kpi = _kpi([PANEL, ACCOUNT])
    doc = make_doc(demo_store)
    r = one(demo_store, _registry(kpi), tight, doc,
            make_obs(doc, "Calls", float(truth.calls(MAY)), period_end=MAY, half=0.5), kpi.kpi_id)
    assert r.validation_status == VS.PLATFORM_DATA_ISSUE
    record = demo_store.get(ReconRun, recon_run_id=r.recon_run_id).record
    assert record["error_grid"][MAY.isoformat()]["grid_skipped"] == "budget"


def test_kpi_without_alternatives_is_unchanged(demo_store, settings):
    kpi = _kpi([PANEL])
    doc = make_doc(demo_store)
    r = one(demo_store, _registry(kpi), settings, doc,
            make_obs(doc, "Calls", float(truth.calls(MAY)), period_end=MAY, half=0.5), kpi.kpi_id)
    assert r.validation_status == VS.PLATFORM_DATA_ISSUE and r.reason_code == "platform_object_missing"
    assert not [t for t in _trials(demo_store, r) if t.phase == "grid"]
    assert "error_grid" not in demo_store.get(ReconRun, recon_run_id=r.recon_run_id).record
