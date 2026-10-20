from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from kpi_evidence.eval import truth

JUNE = "2026-06-30"
MAY = "2026-05-31"


def test_design_reference_values_june_2026():
    assert truth.trx(JUNE) == pytest.approx(5783.041, abs=5e-4)
    assert truth.trx(JUNE, "quarter") == pytest.approx(17220.318, abs=5e-4)
    assert truth.trx_share(JUNE) == pytest.approx(0.0917, abs=5e-5)
    assert truth.trx_share(JUNE, basket="il17_class") == pytest.approx(0.2621, abs=5e-5)
    assert truth.call_attainment(JUNE) == pytest.approx(0.8442, abs=5e-5)
    assert truth.call_attainment(JUNE, eligibility="writers_only") == pytest.approx(0.9192, abs=5e-5)
    assert truth.call_attainment(JUNE, aggregation="mean_of_ratios") == pytest.approx(0.8576, abs=5e-5)
    assert truth.referrals_in(JUNE) == 147
    assert truth.referrals_in(JUNE, projection="projected") == pytest.approx(183.75)
    assert truth.speaker_attendance(JUNE) == 64
    assert truth.speaker_attendance(JUNE, measure="distinct_hcps") == 47
    assert truth.trx(JUNE, scope={"region": "West"}) == pytest.approx(1265.7, abs=0.05)


def test_research_map_values_june_2026():
    assert truth.nrx(JUNE) == pytest.approx(1122.263, abs=5e-4)
    assert truth.nbrx(JUNE) == pytest.approx(782.454, abs=5e-4)
    assert truth.calls(JUNE) == 1163
    assert truth.call_plan(JUNE) == pytest.approx(1377.6)
    assert truth.samples(JUNE) == 2476
    assert truth.new_writers(JUNE) == 2
    assert truth.active_referrers(JUNE) == 129
    regions = {r: truth.trx(JUNE, scope={"region": r}) for r in ("East", "North", "South", "West")}
    assert regions == pytest.approx({"East": 1666.3, "North": 1526.2, "South": 1324.8, "West": 1265.7}, abs=0.05)
    assert sum(regions.values()) == pytest.approx(truth.trx(JUNE))


def test_panel_lag_and_two_month_quarter():
    assert truth.trx(JUNE, source="panel") is None
    q2_panel = truth.trx(JUNE, "quarter", source="panel")
    assert q2_panel == pytest.approx(11783.067, abs=5e-4)
    assert q2_panel / truth.trx(JUNE, "quarter") - 1 == pytest.approx(-0.316, abs=5e-4)
    assert q2_panel == pytest.approx(truth.trx("2026-04-30", source="panel") + truth.trx(MAY, source="panel"))
    for kpi in (truth.nrx, truth.nbrx):
        assert kpi(MAY, source="panel") > kpi(MAY)


def test_null_semantics_and_windows():
    assert truth.new_writers("2024-07-31") is None
    assert truth.new_writers("2024-08-31") is not None
    assert truth.window_months(JUNE, "quarter") == ["2026-04", "2026-05", "2026-06"]
    assert truth.window_months("2026-02-28", "r3m") == ["2025-12", "2026-01", "2026-02"]
    assert truth.window_months(JUNE, "ytd")[0] == "2026-01" and len(truth.window_months(JUNE, "r12m")) == 12
    assert truth.period_start(JUNE, "quarter") == date(2026, 4, 1)
    assert truth.to_date("2026-05") == date(2026, 5, 31)
    assert truth.trx("2030-01-31") is None
    ytd = sum(truth.trx(f"2026-{m:02d}") for m in range(1, 7))
    assert truth.trx(JUNE, "ytd") == pytest.approx(ytd)


def test_ratio_of_sums_is_not_mean_of_monthly_ratios():
    q = truth.trx_share(JUNE, "quarter")
    monthly = [truth.trx_share(m) for m in ("2026-04-30", MAY, JUNE)]
    assert q != pytest.approx(sum(monthly) / 3, abs=1e-9)
    num = sum(truth.trx(m) for m in ("2026-04-30", MAY, JUNE))
    data = truth.load_platform()
    den = data.account[data.account["month"].isin(["2026-04", "2026-05", "2026-06"])]["advanced_therapy_trx"].sum()
    assert q == pytest.approx(num / den)


def test_exfactory_simulation_and_errors():
    data = truth.load_platform()
    june = data.account[data.account["month"] == "2026-06"]["trx_units"].sum()
    assert truth.exfactory_units(JUNE) == round(june * 1.04)
    with pytest.raises(truth.TruthError):
        truth.exfactory_units(JUNE, scope={"region": "West"})
    with pytest.raises(truth.TruthError):
        truth.trx(JUNE, grain="week")
    with pytest.raises(truth.TruthError):
        truth.kpi_value("brand_awareness", JUNE)
    with pytest.raises(truth.TruthError):
        truth.calls(JUNE, scope={"brand": "X"})
    assert truth.kpi_value("calls", JUNE, scope={"region": ["East", "West"]}) == (
        truth.calls(JUNE, scope={"region": "East"}) + truth.calls(JUNE, scope={"region": "West"}))


@pytest.fixture(scope="module")
def sql_store(tmp_path_factory):
    from kpi_evidence.settings import Settings, default_registry_dir, default_sql_dir, demo_dir
    from kpi_evidence.store.duckdb_store import DuckDBStore

    settings = Settings.from_env(local_dir=tmp_path_factory.mktemp("truth_sql"), registry_dir=default_registry_dir())
    store = DuckDBStore(settings, ":memory:")
    store.ensure_schema()
    d = demo_dir("platform")
    s = store.settings
    a = pd.read_csv(d / "fact_source_a.csv", dtype={"npi": str})
    b = pd.read_csv(d / "fact_source_b.csv")
    b["load_id"], b["loaded_at"] = "L2026-06", pd.Timestamp("2026-06-05")
    r = pd.read_csv(d / "fact_referral.csv", dtype={"npi": str})
    store.write_dataframe(f"{s.platform_schema}.rx_account_monthly", a)
    store.write_dataframe(f"{s.platform_schema}.rx_panel_monthly", b)
    store.write_dataframe(f"{s.platform_schema}.referrals_monthly", r)
    sql = (default_sql_dir() / "governed_views.sql").read_text(encoding="utf-8")
    sql = sql.replace("${gov}", s.gov_schema).replace("${platform}", s.platform_schema)
    sql = sql.replace("${restricted}", s.restricted_schemas[0])
    store.execute_script(sql)
    return store


CASES = [
    ("trx", "month", {}, {}), ("trx", "quarter", {}, {}), ("trx", "ytd", {}, {}), ("trx", "r12m", {}, {}),
    ("trx", "month", {"region": "West"}, {}), ("trx", "month", {}, {"measure": "normalized"}),
    ("trx", "quarter", {}, {"source": "panel"}), ("nrx", "month", {}, {}), ("nrx", "month", {}, {"source": "panel"}),
    ("nbrx", "r3m", {"specialty": "Cardiology"}, {}), ("nbrx", "month", {}, {"source": "panel"}),
    ("trx_share", "month", {}, {}), ("trx_share", "month", {}, {"basket": "il17_class"}),
    ("trx_share", "quarter", {"region": "East"}, {}), ("calls", "month", {}, {}), ("call_plan", "quarter", {}, {}),
    ("call_attainment", "month", {}, {}), ("call_attainment", "month", {}, {"eligibility": "writers_only"}),
    ("call_attainment", "month", {}, {"aggregation": "mean_of_ratios"}),
    ("call_attainment", "quarter", {"region": "North"}, {}), ("samples", "month", {}, {}),
    ("speaker_attendance", "month", {}, {}), ("speaker_attendance", "month", {}, {"measure": "distinct_hcps"}),
    ("new_writers", "ytd", {}, {}), ("referrals_in", "month", {}, {}),
    ("referrals_in", "month", {}, {"projection": "projected"}), ("active_referrers", "quarter", {}, {}),
]


@pytest.mark.parametrize("period_end", [MAY, JUNE])
@pytest.mark.parametrize("kpi_id,grain,scope,choices", CASES)
def test_truth_matches_registry_sql(sql_store, registry, kpi_id, grain, scope, choices, period_end):
    from kpi_evidence.compile import compile_kpi

    expected = truth.kpi_value(kpi_id, period_end, grain, scope, **choices)
    compiled = compile_kpi(registry.get(kpi_id), brand="KESTREL", scope=scope, grain=grain, settings=sql_store.settings)
    if choices.get("source") == "panel" and period_end == JUNE and grain == "month":
        assert expected is None
        return
    value, _components = compiled.spec.compute(sql_store.runner(), {**compiled.governed_defaults, **choices}, period_end)
    assert value == pytest.approx(expected, rel=1e-9, abs=1e-9)
