from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
from recon_testkit import make_doc, make_obs, one

from kpi_evidence.contracts import ValidationStatus as VS
from kpi_evidence.hints import parse_hints
from kpi_evidence.registry import Registry

MAY = date(2026, 5, 31)
ACCOUNT = "platform_demo.rx_account_monthly"
ROW = {"kpi": "calls_hint", "aliases": "Calls", "table": ACCOUNT, "value_sql": "SUM(calls)", "date_column": "month",
       "date_format": "yyyy-MM", "alternatives": "doubled=SUM(calls) * 2"}


def _hint_registry() -> Registry:
    kpi = parse_hints(pd.DataFrame([ROW])).kpis["calls_hint"]
    return Registry(kpis={kpi.kpi_id: kpi}, brands={}, report_types={}, goal_list=[], policy={}, directory=Path("."))


def _calls(store) -> float:
    return float(store.query(f"SELECT SUM(calls) AS v FROM {ACCOUNT} WHERE month = '2026-05'").iloc[0, 0])


def test_a_verified_hint_row_never_mentions_a_governed_definition(demo_store, settings):
    doc = make_doc(demo_store)
    r = one(demo_store, _hint_registry(), settings, doc,
            make_obs(doc, "Calls", _calls(demo_store), period_end=MAY, half=0.5), "calls_hint")
    assert r.validation_status == VS.VERIFIED
    assert "primary KPI table definition" in r.reason_detail and "governed" not in r.reason_detail


def test_a_hint_alternative_names_the_primary_kpi_table_definition(demo_store, settings):
    doc = make_doc(demo_store)
    r = one(demo_store, _hint_registry(), settings, doc,
            make_obs(doc, "Calls", 2 * _calls(demo_store), period_end=MAY, half=0.5, stated="doubled"), "calls_hint")
    assert r.validation_status == VS.INCOMPATIBLE_DEFINITION and r.reason_code == "matches_alternative_definition"
    assert "the primary KPI table definition gives" in r.reason_detail and "governed" not in r.reason_detail


def test_a_registry_kpi_keeps_the_governed_wording(demo_store, registry, settings):
    doc = make_doc(demo_store)
    r = one(demo_store, registry, settings, doc,
            make_obs(doc, "Calls", _calls(demo_store), period_end=MAY, half=0.5), "calls")
    assert r.validation_status == VS.VERIFIED and "under the governed definition" in r.reason_detail


def test_an_unmapped_number_is_described_without_the_registry(demo_store, registry, settings):
    doc = make_doc(demo_store)
    r = one(demo_store, registry, settings, doc, make_obs(doc, "Mystery metric", 42.0, half=0.5), None)
    assert r.validation_status == VS.UNMAPPED_CAPTURED and "registry" not in r.reason_detail
