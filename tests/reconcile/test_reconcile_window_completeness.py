from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from recon_testkit import make_doc, make_obs, one

from kpi_evidence.compile import WINDOW_CHECK, compile_kpi
from kpi_evidence.contracts import ValidationStatus as VS
from kpi_evidence.registry import GRAINS, Registry, parse_kpi

JUNE, MAY = date(2026, 6, 30), date(2026, 5, 31)
FULL = "platform_demo.rx_account_monthly"
LAGGED = "platform_demo.rx_lagged"


@pytest.fixture()
def lagged(demo_store):
    demo_store.execute(f"CREATE TABLE {LAGGED} AS SELECT * FROM {FULL} WHERE month <= '2026-05'")
    demo_store.execute(f"CREATE VIEW {demo_store.settings.gov_schema}.gv_lagged AS SELECT * FROM "
                       f"{demo_store.settings.gov_schema}.gv_rx_account_monthly WHERE month <= '2026-05'")
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
                                        "options": dict(zip(names, tables)), "permitted": ["primary"]}
    if alternatives:
        platform["value"] = "{measure}"
        platform["params"]["measure"] = {"hypothesis": "measure", "default": "primary",
                                         "options": {"primary": value, **alternatives}, "permitted": ["primary"]}
    raw = {"kpi_id": "calls_hint", "version": 0, "status": "DRAFT", "label": "Calls (hint)", "unit": "count",
           "aggregation": "sum", "grains": list(GRAINS), "synonyms": ["calls"], "tolerance": {"rel": 0.005},
           "platform": platform, "definition_source": "hint"}
    return parse_kpi(raw)


def _governed(view: str):
    raw = {"kpi_id": "calls_gov", "version": 1, "status": "APPROVED", "approved_by": "owner@example.com",
           "label": "Calls", "unit": "count", "aggregation": "sum", "grains": list(GRAINS), "synonyms": ["calls"],
           "tolerance": {"rel": 0.005}, "platform": {"view": view, "value": "SUM(calls)"}}
    return parse_kpi(raw)


def _registry(kpi) -> Registry:
    return Registry(kpis={kpi.kpi_id: kpi}, brands={}, report_types={}, goal_list=[], policy={}, directory=Path("."))


def _sum(store, months: str) -> float:
    return float(store.query(f"SELECT SUM(calls) AS v FROM {FULL} WHERE month IN ({months})").iloc[0, 0])


Q2 = "'2026-04', '2026-05', '2026-06'"


@pytest.mark.parametrize("kpi_factory", [lambda: _hint([LAGGED]), lambda: _governed("gv_lagged")],
                         ids=["table with a date column", "governed view"])
def test_a_quarter_missing_a_month_is_a_platform_data_issue(lagged, settings, kpi_factory):
    kpi = kpi_factory()
    doc = make_doc(lagged)
    partial = _sum(lagged, "'2026-04', '2026-05'")
    r = one(lagged, _registry(kpi), settings, doc,
            make_obs(doc, "Calls", partial, period_end=JUNE, grain="quarter", half=0.5), kpi.kpi_id)
    assert r.validation_status == VS.PLATFORM_DATA_ISSUE and r.reason_code == "no_data_for_period"
    assert r.reason_detail.startswith("The platform source has no data for 1 month of the period ending 2026-06-30")
    assert r.explanation["error"] == "the platform source has no data for 1 month of the period"
    assert r.platform_value is None


def test_a_complete_quarter_and_a_single_month_still_compare(lagged, settings):
    doc = make_doc(lagged)
    full = one(lagged, _registry(_hint([FULL])), settings, doc,
               make_obs(doc, "Calls", _sum(lagged, Q2), period_end=JUNE, grain="quarter", half=0.5), "calls_hint")
    assert full.validation_status == VS.VERIFIED
    month = one(lagged, _registry(_hint([LAGGED])), settings, doc,
                make_obs(doc, "Calls", _sum(lagged, "'2026-05'"), period_end=MAY, half=0.5, key="m"), "calls_hint")
    assert month.validation_status == VS.VERIFIED


def test_a_complete_candidate_table_explains_an_incomplete_primary(lagged, settings):
    doc = make_doc(lagged)
    r = one(lagged, _registry(_hint([LAGGED, FULL])), settings, doc,
            make_obs(doc, "Calls", _sum(lagged, Q2), period_end=JUNE, grain="quarter", half=0.5), "calls_hint")
    assert r.validation_status == VS.INCOMPATIBLE_DEFINITION and r.explanation["alternative"] == "source=alt_1"
    assert "could not run (the platform source has no data for 1 month of the period)" in r.reason_detail


def test_an_alternative_on_the_same_incomplete_table_explains_nothing(lagged, settings):
    doc = make_doc(lagged)
    kpi = _hint([LAGGED], alternatives={"doubled": "SUM(calls) * 2"})
    r = one(lagged, _registry(kpi), settings, doc,
            make_obs(doc, "Calls", 2 * _sum(lagged, "'2026-04', '2026-05'"), period_end=JUNE, grain="quarter",
                     half=0.5), "calls_hint")
    assert r.validation_status == VS.PLATFORM_DATA_ISSUE


@pytest.mark.parametrize("grain, months", [("quarter", "3"), ("r6m", "6"), ("r12m", "12"),
                                           ("ytd", "month(DATE '{as_of}')"), ("month", None)])
def test_window_checks_are_compiled_for_multi_month_grains(settings, grain, months):
    for kpi in (_hint([FULL]), _governed("gv_rx_account_monthly")):
        compiled = compile_kpi(kpi, brand="KESTREL", scope={}, grain=grain, settings=settings)
        if months is None:
            assert compiled.window_checks == {}
            continue
        sql = compiled.window_checks[WINDOW_CHECK]
        assert sql.startswith(f"SELECT {months} - COUNT(DISTINCT ")
        assert WINDOW_CHECK in compiled.checked_spec().checks and WINDOW_CHECK not in compiled.spec.checks


def test_a_lagging_panel_missing_a_month_does_not_reproduce_the_quarter(demo_store, registry, settings):
    doc = make_doc(demo_store, "Kestrel_Executive_Summary_Q2-2026.pdf", family="fam-exec")
    panel_q2 = float(demo_store.query(f"SELECT SUM(trx_units) AS v FROM {settings.gov_schema}.gv_rx_panel_monthly "
                                      "WHERE month IN ('2026-04', '2026-05')").iloc[0, 0])
    r = one(demo_store, registry, settings, doc,
            make_obs(doc, "TRx (retail panel)", round(panel_q2), period_end=JUNE, grain="quarter", half=0.5,
                     stated="retail panel"), "trx")
    assert r.validation_status == VS.VARIANCE_REVIEW and "alternative" not in r.explanation
    [(name, months)] = r.explanation["incomplete_alternatives"].items()
    assert name.startswith("source=panel (Projected retail panel") and months == "1 month"
    assert f"The alternative {name} was not compared: it has no data for 1 month of the period." in r.reason_detail
