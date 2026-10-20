from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from recon_testkit import make_doc, make_obs, run

from kpi_evidence.contracts import KpiResult, PlatformKpiValue, SqlArtifact, ValidationStatus as VS
from kpi_evidence.ids import canonical_json, sha256_text
from kpi_evidence.materialize import complete_period_ends, materialize, window_start


def _reconcile_and_materialize(store, registry, settings, pairs, run_id="run-1"):
    doc = make_doc(store)
    results = run(store, registry, settings, doc, [(make_obs(doc, *a, **k), kpi) for a, k, kpi in pairs], run_id=run_id)
    return results, materialize(store, registry, settings, results, run_id)


def _series(store, kpi_id, grain="month"):
    return store.read(PlatformKpiValue, "kpi_id = :k AND period_grain = :g", {"k": kpi_id, "g": grain},
                      order_by="period_end")


def test_artifacts_exports_and_series(demo_store, registry, settings):
    results, artifacts = _reconcile_and_materialize(demo_store, registry, settings, [
        (("TRx", 5783.04), {"half": 0.005}, "trx"),
        (("TRx share", 0.092), {"unit": "fraction", "half": 0.0005}, "trx_share"),
        (("Call attainment", 0.9192), {"unit": "fraction", "half": 0.00005, "stated": "writers only"}, "call_attainment"),
        (("Awareness", 0.62), {"unit": "fraction"}, "brand_awareness"),
    ])
    assert [r.validation_status for r in results] == [VS.VERIFIED, VS.VERIFIED, VS.INCOMPATIBLE_DEFINITION,
                                                      VS.REPORT_ONLY]
    by_kpi = {a.kpi_id: a for a in artifacts}
    assert set(by_kpi) == {"trx", "trx_share", "call_attainment"}
    assert demo_store.count(SqlArtifact) == 3
    for r in results[:3]:
        assert r.sql_artifact_id == by_kpi[r.kpi_id].artifact_id
        assert demo_store.get(KpiResult, result_id=r.result_id).sql_artifact_id == r.sql_artifact_id

    trx = by_kpi["trx"]
    assert "{as_of}" in trx.value_sql_template and "0001-01-01" not in trx.value_sql_template
    assert "month_end = DATE '{as_of}'" in trx.value_sql_template and "'KESTREL'" in trx.value_sql_template
    assert set(trx.components_sql) == {"value"} and "period_has_rows" in trx.checks_sql
    assert "CREATE OR REPLACE FUNCTION kpi_gov.kpi_trx_v1(as_of DATE)" in trx.function_sql
    assert "RETURNS DOUBLE" in trx.function_sql and "month_end = as_of" in trx.function_sql
    assert "{as_of}" not in trx.function_sql
    sql = trx.value_sql_template.replace("{as_of}", "2026-06-30")
    assert demo_store.runner().run(sql) == pytest.approx(results[0].platform_value)

    share = by_kpi["trx_share"]
    assert set(share.components_sql) == {"numerator", "denominator"}
    assert "NULLIF((" in share.function_sql and "zero denominator returns NULL" in share.function_sql

    path = Path(trx.export_path)
    bindings_hash = sha256_text(canonical_json(trx.bindings))
    assert path.exists() and path.parent.name == "trx" and path.parent.parent.name == "sql"
    assert path.name == f"{trx.definition_hash[:12]}_{bindings_hash[:8]}.sql"
    text = path.read_text(encoding="utf-8")
    for needle in ("-- KPI: trx", "-- Definition:", "-- Choices: measure=units", "-- Bindings:", "-- Generated at:",
                   "CREATE OR REPLACE FUNCTION"):
        assert needle in text

    series = _series(demo_store, "trx")
    assert len(series) == 24 and series[0].period_end == date(2024, 7, 31) and series[-1].period_end == date(2026, 6, 30)
    assert series[-1].value == pytest.approx(5783.040615) and series[-1].error is None
    assert series[-1].sql_artifact_id == trx.artifact_id and series[-1].period_start == date(2026, 6, 1)
    share_rows = _series(demo_store, "trx_share")
    assert share_rows[-1].components["denominator"] > share_rows[-1].components["numerator"] > 0
    ca_rows = _series(demo_store, "call_attainment")
    assert ca_rows[-1].value == pytest.approx(0.84422, abs=1e-5)


def test_period_errors_are_stored_not_raised(demo_store, registry, settings):
    _, artifacts = _reconcile_and_materialize(demo_store, registry, settings, [
        (("New writers", 0.0), {"period_end": date(2024, 8, 31)}, "new_writers")])
    assert len(artifacts) == 1
    rows = {r.period_end: r for r in _series(demo_store, "new_writers")}
    assert rows[date(2024, 7, 31)].value is None and "NULL" in rows[date(2024, 7, 31)].error
    assert rows[date(2024, 8, 31)].value == 0.0 and rows[date(2024, 8, 31)].error is None


def test_quarter_series_only_complete_quarters(demo_store, registry, settings):
    results, _ = _reconcile_and_materialize(demo_store, registry, settings, [
        (("TRx Q2", 17220.3), {"grain": "quarter", "half": 0.05}, "trx")])
    assert results[0].validation_status == VS.VERIFIED
    rows = _series(demo_store, "trx", "quarter")
    assert [r.period_end for r in rows][:2] == [date(2024, 9, 30), date(2024, 12, 31)] and len(rows) == 8
    assert rows[-1].value == pytest.approx(17220.317854) and rows[-1].period_start == date(2026, 4, 1)


def test_materialize_is_idempotent(demo_store, registry, settings):
    results, first = _reconcile_and_materialize(demo_store, registry, settings, [
        (("TRx", 5783.04), {"half": 0.005}, "trx")])
    before = {(r.period_end, r.computed_at) for r in _series(demo_store, "trx")}
    second = materialize(demo_store, registry, settings, results, "run-2")
    assert [(a.artifact_id, a.sql_sha, a.created_at) for a in first] == \
        [(a.artifact_id, a.sql_sha, a.created_at) for a in second]
    assert {(r.period_end, r.computed_at) for r in _series(demo_store, "trx")} == before
    assert materialize(demo_store, registry, settings, [], "run-3") == []


def test_stated_variant_gets_its_own_artifact_and_its_own_series(demo_store, registry, settings):
    results, artifacts = _reconcile_and_materialize(demo_store, registry, settings, [
        (("IL-17 class share", 0.2621), {"unit": "fraction", "half": 0.00005, "stated": "IL-17 class share", "key": "a"},
         "trx_share"),
        (("TRx share", 0.0917), {"unit": "fraction", "half": 0.00005, "key": "b"}, "trx_share"),
    ])
    assert [r.validation_status for r in results] == [VS.VERIFIED, VS.VERIFIED]
    variant, governed = sorted(artifacts, key=lambda a: a.choices["basket"] == "advanced_therapy")
    assert variant.choices["basket"] == "il17_class" and governed.choices["basket"] == "advanced_therapy"
    assert Path(variant.export_path).name.endswith(f"_{variant.artifact_id[:8]}.sql")
    assert f"kpi_trx_share_v1_{variant.artifact_id[:8]}" in variant.function_sql
    assert "kpi_gov.kpi_trx_share_v1(as_of DATE)" in governed.function_sql
    june = {r.sql_artifact_id: r for r in _series(demo_store, "trx_share") if r.period_end == date(2026, 6, 30)}
    assert june[governed.artifact_id].value == pytest.approx(0.091731, abs=1e-6)
    assert june[variant.artifact_id].value == pytest.approx(0.2621, abs=1e-4)


def test_period_helpers():
    months = [date(2024, m, 28 if m == 2 else 30 if m in (4, 6, 9, 11) else 31) for m in range(7, 13)]
    assert complete_period_ends(months, "quarter") == [date(2024, 9, 30), date(2024, 12, 31)]
    assert complete_period_ends(months, "r6m") == [date(2024, 12, 31)]
    assert complete_period_ends(months, "ytd") == []
    assert window_start(date(2026, 6, 30), "r12m") == date(2025, 7, 1)
    assert window_start(date(2026, 6, 30), "ytd") == date(2026, 1, 1)
