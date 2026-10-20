from __future__ import annotations

import pandas as pd

from kpi_evidence.check import ReportCheck
from kpi_evidence.contracts import RegistryEntry, RegistryStatus
from kpi_evidence.hints import HINT_VERSION, parse_hints
from kpi_evidence.materialize import function_name

ROW = {"kpi": "trx", "table": "platform_demo.rx_account_monthly", "value_sql": "SUM(trx_units)",
       "date_column": "month", "date_format": "yyyy-MM"}


def test_hint_definitions_use_their_own_version(registry):
    hints = parse_hints(pd.DataFrame([ROW, {**ROW, "kpi": "Brand new KPI"}]), base_registry=registry)
    assert {k.version for k in hints.kpis.values()} == {HINT_VERSION} == {0}
    assert registry.kpis["trx"].version >= 1


def test_a_kpi_table_check_keeps_the_governed_registry_row(tmp_path):
    state = tmp_path / "state.duckdb"
    with ReportCheck(load_demo_platform=True, state_path=str(state)) as check:
        check._bootstrap(check._effective_registry())
    with ReportCheck(load_demo_platform=True, state_path=str(state)) as check:
        check.set_kpis(pd.DataFrame([ROW]))
        check._bootstrap(check._effective_registry())
        rows = {r.kpi_version: r for r in check.store.read(RegistryEntry, "kpi_id = :k", {"k": "trx"})}
    assert rows[1].status == RegistryStatus.APPROVED and rows[1].approved_by
    assert rows[0].status == RegistryStatus.DRAFT and rows[0].spec["definition_source"] == "hint"


def test_version_zero_flows_into_the_function_name(registry, settings):
    from kpi_evidence.compile import compile_kpi
    from kpi_evidence.materialize import _Unit

    kpi = parse_hints(pd.DataFrame([ROW]), base_registry=registry).kpis["trx"]
    compiled = compile_kpi(kpi, brand="KESTREL", scope={}, grain="month", settings=settings)
    unit = _Unit(kpi, compiled, compiled.governed_defaults, "a" * 16)
    assert function_name(unit) == "kpi_trx_v0"
