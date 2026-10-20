from __future__ import annotations

import re

import pytest

from kpi_evidence import platform
from kpi_evidence.settings import Settings
from kpi_evidence.store.base import split_sql

sqlglot = pytest.importorskip("sqlglot")

DIALECTS = ("databricks", "spark", "duckdb")
FORBIDDEN = [(re.compile(r"::"), "'::' cast"), (re.compile(r"`"), "backtick"), (re.compile(r"\bQUALIFY\b", re.I), "QUALIFY"),
             (re.compile(r"date_trunc", re.I), "date_trunc")]


class SqlRecorder:
    def __init__(self):
        self.statements: list[str] = []

    def run(self, sql: str) -> float:
        self.statements.append(sql)
        return 0.0


def _kpi_sql(registry, settings, cases, compile_case) -> dict[str, str]:
    out = {}
    for case in cases:
        compiled = compile_case(registry, settings, case)
        recorder = SqlRecorder()
        try:
            compiled.spec.compute(recorder, dict(case.choices), case.as_of)
        except ValueError as exc:
            assert "denominator is zero" in str(exc)
        for i, sql in enumerate(recorder.statements):
            out[f"{case.key}#{i}"] = sql
        out[f"{case.key}#render"] = compiled.spec.render(dict(case.choices), case.as_of)
    return out


def _platform_sql(settings: Settings) -> dict[str, str]:
    out = {f"governed_views#{i}": s for i, s in enumerate(split_sql(platform.governed_views_sql(settings)))}
    for view, checks in platform.DQ_CHECKS.items():
        for check in checks:
            out[f"dq#{view}#{check.check}"] = platform.check_sql(check, platform.gov_view_fq(settings, view))
    for table, checks in platform.BASE_TABLE_DQ_CHECKS.items():
        for check in checks:
            out[f"dq#{table}#{check.check}"] = platform.check_sql(check, platform.base_table_fq(settings, table))
    return out


@pytest.fixture(scope="module")
def databricks_settings():
    return Settings.from_env(backend="databricks", catalog="main")


@pytest.fixture(scope="module")
def all_sql(platform_registry, all_cases, case_tools, databricks_settings) -> dict[str, str]:
    sql = _platform_sql(databricks_settings)
    sql.update(_kpi_sql(platform_registry, databricks_settings, all_cases, case_tools["compile_case"]))
    return sql


def test_every_statement_was_collected(all_sql, all_cases):
    assert sum(k.startswith("governed_views#") for k in all_sql) == 3
    assert sum(k.endswith("#render") for k in all_sql) == len(all_cases)
    assert len(all_sql) > 3 * len(all_cases)
    assert all("main.kpi_gov." in s or "main.platform_restricted." in s or "main.platform_demo." in s
               for k, s in all_sql.items() if not k.startswith("governed_views"))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_sql_parses(all_sql, dialect):
    failures = {}
    for key, sql in all_sql.items():
        try:
            parsed = sqlglot.parse(sql, read=dialect)
            assert len(parsed) == 1 and parsed[0] is not None
        except Exception as exc:
            failures[key] = f"{type(exc).__name__}: {str(exc)[:200]}"
    assert not failures, failures


def test_sql_stays_in_the_common_subset(all_sql):
    hits = {key: label for key, sql in all_sql.items() for pattern, label in FORBIDDEN if pattern.search(sql)}
    assert not hits, hits
