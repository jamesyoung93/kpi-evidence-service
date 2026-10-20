from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from kpi_evidence.compile import compile_kpi
from kpi_evidence.hints import parse_hints
from kpi_evidence.materialize import _Unit, build_artifact, export_text, one_line

ROW = {"table": "platform_demo.rx_account_monthly", "value_sql": "SUM(nrx)", "date_column": "month",
       "date_format": "yyyy-MM"}
NOW = datetime(2026, 10, 19, 12, 0, 0)


def _unit(name: str, settings) -> _Unit:
    kpi = next(iter(parse_hints(pd.DataFrame([{**ROW, "kpi": name}])).kpis.values()))
    compiled = compile_kpi(kpi, brand="KESTREL", scope={}, grain="month", settings=settings)
    return _Unit(kpi, compiled, compiled.governed_defaults, "a" * 16)


def _header(text: str, stop: str) -> list[str]:
    lines = text.splitlines()
    return lines[:next(i for i, line in enumerate(lines) if line.startswith(stop))]


@pytest.mark.parametrize("name", ["NRx\nSELECT 'injected' AS marker", "NRx\r\nCREATE TABLE t AS SELECT 1;\n--",
                                  "NRx DROP TABLE t"])
def test_a_line_break_in_the_kpi_name_stays_inside_the_comment(name, settings):
    unit = _unit(name, settings)
    artifact = build_artifact(settings, unit, NOW)
    assert all(line.startswith("--") for line in _header(artifact.function_sql, "CREATE OR REPLACE FUNCTION"))
    exported = export_text(unit, artifact, NOW)
    assert all(not line or line.startswith("--") for line in _header(exported, "SELECT"))
    assert one_line(name) in exported


def test_the_function_comment_uses_spark_string_escaping(settings):
    artifact = build_artifact(settings, _unit("Kestrel's\\TRx", settings), NOW)
    assert "COMMENT 'Kestrel\\'s\\\\TRx v0'" in artifact.function_sql
    assert "''" not in artifact.function_sql.split("COMMENT ", 1)[1].splitlines()[0]
