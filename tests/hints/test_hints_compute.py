from __future__ import annotations

from datetime import date

import pytest
from hints_testkit import ACCOUNT, EXAMPLE_CSV, JUNE, MAY, compute, row, table

from kpi_evidence.eval import truth
from kpi_evidence.hints import load_kpi_table, parse_hints
from kpi_evidence.store.base import AccessDenied


@pytest.fixture(scope="module")
def hints():
    return parse_hints(load_kpi_table(EXAMPLE_CSV))


def test_trx_june_and_quarter(demo_store, settings, hints):
    kpi = hints.kpis["trx"]
    assert compute(demo_store, settings, kpi, JUNE) == pytest.approx(5783.041, abs=1e-3)
    assert compute(demo_store, settings, kpi, JUNE, grain="quarter") == pytest.approx(17220.318, abs=1e-3)
    assert compute(demo_store, settings, kpi, JUNE, grain="ytd") == pytest.approx(truth.trx(JUNE, "ytd"), rel=1e-12)
    assert compute(demo_store, settings, kpi, JUNE, scope={"region": "West"}) == pytest.approx(1265.7, abs=0.05)


def test_trx_panel_candidate(demo_store, settings, hints):
    value = compute(demo_store, settings, hints.kpis["trx"], JUNE, grain="quarter", choices={"source": "alt_1"})
    assert value == pytest.approx(truth.trx(JUNE, "quarter", source="panel"), rel=1e-9)
    assert value == pytest.approx(11783.07, abs=0.01)


def test_call_attainment_primary_and_alternative(demo_store, settings, hints):
    kpi = hints.kpis["call_attainment"]
    assert compute(demo_store, settings, kpi, JUNE) == pytest.approx(0.8442, abs=1e-4)
    writers = compute(demo_store, settings, kpi, JUNE, choices={"measure": "writers_only"})
    assert writers == pytest.approx(truth.call_attainment(JUNE, eligibility="writers_only"), rel=1e-9)
    assert writers == pytest.approx(0.9192, abs=1e-4)


@pytest.mark.parametrize("kpi_id, as_of", [("nrx", MAY), ("nbrx", MAY), ("samples", MAY), ("new_writers", MAY),
                                           ("referrals_in", MAY), ("active_referrers", MAY), ("trx_share", JUNE)])
def test_simple_hints_match_truth(demo_store, settings, hints, kpi_id, as_of):
    assert compute(demo_store, settings, hints.kpis[kpi_id], as_of) == pytest.approx(truth.kpi_value(kpi_id, as_of),
                                                                                     rel=1e-9)


def test_calls_primary_table_fails_and_second_candidate_works(demo_store, settings, hints):
    kpi = hints.kpis["calls"]
    with pytest.raises(Exception, match="calls"):
        compute(demo_store, settings, kpi, MAY)
    assert compute(demo_store, settings, kpi, MAY, choices={"source": "alt_1"}) == pytest.approx(truth.calls(MAY))


def test_restricted_table_is_access_denied(demo_store, settings, hints):
    with pytest.raises(AccessDenied):
        compute(demo_store, settings, hints.kpis["exfactory_units"], MAY)


def test_yyyymm_and_date_formats(demo_store, settings):
    demo_store.execute(f"CREATE TABLE platform_demo.rx_ym AS SELECT CAST(REPLACE(month, '-', '') AS INTEGER) AS ym, "
                       f"CAST(month || '-01' AS DATE) AS month_start, trx_units FROM {ACCOUNT}")
    hints = parse_hints(table(
        row(kpi="trx_ym", table="platform_demo.rx_ym", date_column="ym", date_format="yyyyMM"),
        row(kpi="trx_day1", table="platform_demo.rx_ym", date_column="month_start", date_format="date")))
    assert hints.errors.empty
    for kpi_id in ("trx_ym", "trx_day1"):
        kpi = hints.kpis[kpi_id]
        assert compute(demo_store, settings, kpi, JUNE) == pytest.approx(5783.041, abs=1e-3)
        assert compute(demo_store, settings, kpi, JUNE, grain="quarter") == pytest.approx(17220.318, abs=1e-3)


def test_month_end_dates_window(demo_store, settings):
    demo_store.execute(f"CREATE TABLE platform_demo.rx_me AS SELECT last_day(CAST(month || '-01' AS DATE)) AS month_end, "
                       f"trx_units FROM {ACCOUNT}")
    kpi = parse_hints(table(row(kpi="trx_me", table="platform_demo.rx_me", date_column="month_end",
                                date_format="date"))).kpis["trx_me"]
    assert compute(demo_store, settings, kpi, JUNE) == pytest.approx(5783.041, abs=1e-3)


def test_hint_kpi_reconciles_verified_through_the_engine(demo_store, settings, hints):
    from pathlib import Path

    from kpi_evidence.contracts import (
        Document, DocumentFormat, DocumentStatus, KpiMapping, MappingMethod, MappingStatus, Observation,
    )
    from kpi_evidence.ids import scope_key, stable_id, utcnow
    from kpi_evidence.reconcile import evaluate_document
    from kpi_evidence.registry import Registry

    now = utcnow()
    doc = Document(doc_id="d" * 64, file_name="deck.pptx", file_path="/x/deck.pptx", size_bytes=1,
                   format=DocumentFormat.PPTX, status=DocumentStatus.READY, registered_at=now, updated_at=now,
                   brand="KESTREL", report_type="monthly_brand_review", period_end=JUNE)
    demo_store.upsert([doc])
    obs = Observation(observation_id=stable_id("o", 1), value_id=stable_id("v", 1), doc_id=doc.doc_id,
                      interpretation_version="t", label="TRx", label_context="TRx", value=5783.0, unit="count",
                      display_half_unit=0.5, period_start=date(2026, 6, 1), period_end=JUNE, period_grain="month",
                      period_label="Jun 2026", period_source="category", period_confidence=1.0, scope={},
                      scope_key=scope_key({}), brand="KESTREL", fingerprint="f", stated_rule="", flags=[],
                      confidence=1.0, created_at=now)
    reg = Registry(kpis={"trx": hints.kpis["trx"]}, brands={}, report_types={}, goal_list=[], policy={},
                   directory=Path("."))
    mapping = KpiMapping(observation_id=obs.observation_id, registry_version=reg.version, status=MappingStatus.ACCEPTED,
                         method=MappingMethod.SYNONYM, kpi_id="trx", kpi_version=1, candidates=[], reason="synonym",
                         decided_by="system:test", decided_at=now)
    [result] = evaluate_document(demo_store, reg, settings, doc, [obs], [mapping], {}, "run-h")
    assert result.validation_status.value == "VERIFIED"
    assert result.reason_code == "match_under_governed_default"
    assert result.definition_choices == {"source": "primary"}
    assert result.platform_value == pytest.approx(5783.041, abs=1e-3)
