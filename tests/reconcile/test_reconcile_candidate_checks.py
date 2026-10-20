from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from recon_testkit import make_doc, make_obs, one

from kpi_evidence.compile import RESOLVE_CHECK, WINDOW_CHECK, compile_kpi
from kpi_evidence.contracts import ReconTrial, ValidationStatus as VS
from kpi_evidence.registry import GRAINS, Registry, parse_kpi

JUNE = date(2026, 6, 30)
FULL = "platform_demo.rx_account_monthly"
LAGGED = "platform_demo.rx_lagged_candidate"
NO_CALLS = "platform_demo.rx_without_calls"
Q2 = "'2026-04', '2026-05', '2026-06'"


@pytest.fixture()
def store(demo_store):
    demo_store.execute(f"CREATE TABLE {LAGGED} AS SELECT * FROM {FULL} WHERE month <= '2026-05'")
    demo_store.execute(f"CREATE TABLE {NO_CALLS} AS SELECT * EXCLUDE (calls) FROM {FULL} WHERE month <= '2026-05'")
    return demo_store


def _hint(tables: list[str], value: str = "SUM(calls)", alternatives: dict | None = None):
    names = ["primary"] + [f"alt_{i}" for i in range(1, len(tables))]
    platform = {"value": value, "brand_column": None, "date_expr": "CAST(month || '-01' AS DATE)",
                "source_views": list(tables), "params": {}}
    if len(tables) == 1:
        platform["from"] = tables[0]
    else:
        platform["view"] = "{source}"
        platform["params"]["source"] = {"hypothesis": "source", "default": "primary",
                                        "options": dict(zip(names, tables)), "describe": dict(zip(names, tables)),
                                        "permitted": ["primary"]}
    if alternatives:
        platform["value"] = "{measure}"
        platform["params"]["measure"] = {"hypothesis": "measure", "default": "primary",
                                         "options": {"primary": value, **alternatives}, "permitted": ["primary"]}
    raw = {"kpi_id": "calls_hint", "version": 0, "status": "DRAFT", "label": "Calls (hint)", "unit": "count",
           "aggregation": "sum", "grains": list(GRAINS), "synonyms": ["calls"], "tolerance": {"rel": 0.005},
           "platform": platform, "definition_source": "hint"}
    return parse_kpi(raw)


def _registry(kpi) -> Registry:
    return Registry(kpis={kpi.kpi_id: kpi}, brands={}, report_types={}, goal_list=[], policy={}, directory=Path("."))


def _sum(store, months: str, column: str = "calls") -> float:
    return float(store.query(f"SELECT SUM({column}) AS v FROM {FULL} WHERE month IN ({months})").iloc[0, 0])


def _quarter(store, kpi, value: float):
    doc = make_doc(store)
    return one(store, _registry(kpi), store.settings, doc,
               make_obs(doc, "Calls", value, period_end=JUNE, grain="quarter", half=0.5), kpi.kpi_id)


def _trials(store, result) -> list[ReconTrial]:
    return store.read(ReconTrial, "recon_run_id = :r", {"r": result.recon_run_id}, order_by="seq")


def test_a_candidate_table_missing_a_month_never_reproduces_the_quarter(store):
    partial = _sum(store, "'2026-04', '2026-05'")
    r = _quarter(store, _hint([FULL, LAGGED]), partial)
    assert r.validation_status == VS.VARIANCE_REVIEW and "alternative" not in r.explanation
    assert r.platform_value == pytest.approx(_sum(store, Q2))
    assert r.explanation["incomplete_alternatives"] == {f"source=alt_1 ({LAGGED})": "1 month"}
    assert f"The alternative source=alt_1 ({LAGGED}) was not compared" in r.reason_detail
    lagged = [t for t in _trials(store, r) if t.choices.get("source") == "alt_1"]
    assert lagged and all(t.value is None and WINDOW_CHECK in t.error for t in lagged)


def test_a_measure_alternative_on_a_complete_table_still_reproduces_the_quarter(store):
    kpi = _hint([FULL, LAGGED], alternatives={"doubled": "SUM(calls) * 2"})
    r = _quarter(store, kpi, 2 * _sum(store, Q2))
    assert r.validation_status == VS.INCOMPATIBLE_DEFINITION
    assert r.explanation["matched_choices"] == {"source": "primary", "measure": "doubled"}


def test_a_missing_column_is_reported_before_the_missing_month(store):
    r = _quarter(store, _hint([NO_CALLS]), _sum(store, Q2))
    assert r.validation_status == VS.PLATFORM_DATA_ISSUE and r.reason_code == "platform_object_missing"
    assert "calls" in r.explanation["error"] and "no data for" not in r.reason_detail


def test_the_alternative_reason_names_the_missing_column(store):
    r = _quarter(store, _hint([NO_CALLS, FULL]), _sum(store, Q2))
    assert r.validation_status == VS.INCOMPATIBLE_DEFINITION and r.explanation["alternative"] == "source=alt_1"
    assert r.explanation["primary_reason_code"] == "platform_object_missing"
    assert "could not run (" in r.reason_detail and "calls" in r.reason_detail.split(";")[0]
    assert "no data for" not in r.reason_detail


def test_a_ratio_with_a_missing_column_is_reported_before_the_missing_month(store):
    platform = {"from": NO_CALLS, "numerator": "SUM(trx_units)", "denominator": "SUM(calls)", "brand_column": None,
                "date_expr": "CAST(month || '-01' AS DATE)", "source_views": [NO_CALLS]}
    kpi = parse_kpi({"kpi_id": "trx_per_call", "version": 0, "status": "DRAFT", "label": "TRx per call",
                     "unit": "fraction", "aggregation": "ratio_of_sums", "grains": list(GRAINS),
                     "synonyms": ["trx per call"], "tolerance": {"abs": 0.0005}, "platform": platform,
                     "definition_source": "hint"})
    doc = make_doc(store)
    r = one(store, _registry(kpi), store.settings, doc,
            make_obs(doc, "TRx per call", 0.5, period_end=JUNE, grain="quarter", unit="fraction"), kpi.kpi_id)
    assert r.validation_status == VS.PLATFORM_DATA_ISSUE and r.reason_code == "platform_object_missing"
    assert "calls" in r.explanation["error"]


def test_the_resolve_check_runs_first_and_is_not_exported(settings):
    compiled = compile_kpi(_hint([FULL, LAGGED]), brand="KESTREL", scope={}, grain="quarter", settings=settings)
    checks = list(compiled.trial_spec().checks)
    assert checks[0] == RESOLVE_CHECK and WINDOW_CHECK in checks and "period_has_rows" in checks
    assert RESOLVE_CHECK not in compiled.checked_spec().checks
    sql = compiled.trial_spec().checks[RESOLVE_CHECK]
    assert sql.startswith("SELECT COUNT(*) AS value FROM (") and sql.endswith(") AS q WHERE 1 = 0")
