from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from reconloop.metricspec import MetricSpec, load_specs
from reconloop.sql import fragment

from .ids import canonical_json, sha256_text
from .registry import GRAINS, KpiDefinition, normalize_label
from .settings import Settings
from .store.base import sql_literal
from .store.sqltext import databricks_literal

_BINDING = re.compile(r"\$\{([A-Za-z_]+)\}")

_ROLLING = {"quarter": 3, "r3m": 3, "r6m": 6, "r12m": 12}


class CompileError(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class CompiledKpi:
    kpi: KpiDefinition
    spec: MetricSpec
    bindings: dict[str, Any]
    bindings_hash: str
    governed_defaults: dict[str, str]
    start_choices: dict[str, str]
    stated_variants: dict[str, str]
    stated_other: dict[str, str] = field(default_factory=dict)
    assumed_variants: dict[str, str] = field(default_factory=dict)
    window_checks: dict[str, str] = field(default_factory=dict)

    @property
    def definition_hash(self) -> str:
        return self.kpi.definition_hash()

    def checked_spec(self) -> MetricSpec:
        """The spec plus the checks that every month of a multi-month window is on the platform."""
        return replace(self.spec, checks={**self.spec.checks, **self.window_checks})

    def trial_spec(self) -> MetricSpec:
        """The checked spec, led by a check that the value queries' tables and columns exist."""
        return replace(self.spec, checks={**resolve_check(self.spec), **self.checked_spec().checks})

    def describe(self, choices: dict[str, str] | None = None) -> str:
        return self.spec.describe(choices or self.start_choices)

    def relations(self) -> list[str]:
        """Every table or view the KPI SQL can read under any declared option, checks included."""
        spec = self.spec
        defaults = spec.defaults()
        combos = [defaults] + [{**defaults, name: option} for name, param in spec.params.items()
                               for option in param.options]
        texts: list[str] = []
        for choices in combos:
            try:
                frags = {"as_of": RELATIONS_AS_OF, **{n: fragment(p.options[choices[n]], RELATIONS_AS_OF)
                                                     for n, p in spec.params.items()}}
                texts += list(spec.render_components(choices, RELATIONS_AS_OF).values())
                texts += [sql.format(**frags) for sql in spec.checks.values()]
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return list(dict.fromkeys(name for text in texts for name in sql_relations(text)))


RELATIONS_AS_OF = "2000-01-31"
_SQL_RELATION = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)", re.I)
_NOT_A_RELATION = re.compile(r"(?:\b(?:EXTRACT|TRIM|SUBSTRING|SUBSTR|POSITION|OVERLAY)\s*\([^()]*|\bDISTINCT\s+)$",
                             re.I)
_NOT_RELATION_WORDS = {"select", "where", "lateral", "unnest", "values", "table"}


def strip_sql_text(sql: str) -> str:
    """SQL with comments removed and every string literal emptied."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            out.append(" ")
            i = n if end < 0 else end + 2
        elif sql[i] == "'":
            j = i + 1
            while j < n and not (sql[j] == "'" and not sql.startswith("''", j)):
                j += 2 if sql.startswith("''", j) else 1
            out.append("''")
            i = j + 1
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def sql_relations(sql: str | None) -> list[str]:
    """Tables and views a SQL text reads (FROM and JOIN targets, subqueries included), in order."""
    if not sql:
        return []
    text = strip_sql_text(str(sql))
    out: list[str] = []
    for m in _SQL_RELATION.finditer(text):
        if _NOT_A_RELATION.search(text[:m.start()]):
            continue
        name = re.sub(r"\s+", "", m.group(1))
        if name.lower() not in _NOT_RELATION_WORDS and name not in out:
            out.append(name)
    return out


WINDOW_CHECK = "complete_window"
RESOLVE_CHECK = "query_resolves"
_OUTER_FROM = re.compile(r"\bFROM\s+(\{[A-Za-z_]\w*\}|[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){0,2})", re.I)


def _outer_relation(sql: str) -> str | None:
    text = strip_sql_text(sql)
    for m in _OUTER_FROM.finditer(text):
        before = text[:m.start()]
        if before.count("(") == before.count(")") and not _NOT_A_RELATION.search(before):
            return m.group(1)
    return None


def resolve_check(spec: MetricSpec) -> dict[str, str]:
    """A check that runs the value queries over no rows, so a missing table or column fails first."""
    if spec.ratio:
        body = f"(\n{spec.ratio.numerator}\n) AS n CROSS JOIN (\n{spec.ratio.denominator}\n) AS d"
    else:
        body = f"(\n{spec.template}\n) AS q"
    return {RESOLVE_CHECK: f"SELECT COUNT(*) AS value FROM {body} WHERE 1 = 0"}


def window_completeness_checks(sqls: list[str], grain: str, date_expr: str | None, window: str) -> dict[str, str]:
    """Checks returning how many months of a quarter, rolling or YTD window the source of each query lacks."""
    if grain == "ytd":
        months = "month(DATE '{as_of}')"
    elif grain in _ROLLING:
        months = str(_ROLLING[grain])
    else:
        return {}
    month = f"last_day(CAST({_escape_braces(date_expr)} AS DATE))" if date_expr else "month_start"
    relations = list(dict.fromkeys(r for r in (_outer_relation(sql) for sql in sqls) if r))
    return {(WINDOW_CHECK if i == 0 else f"{WINDOW_CHECK}_{i + 1}"):
            f"SELECT {months} - COUNT(DISTINCT {month}) AS value FROM {relation} WHERE {window}"
            for i, relation in enumerate(relations)}


def window_predicate(grain: str, date_expr: str | None = None) -> str:
    """Period predicate relative to ``{as_of}`` (a month end)."""
    if date_expr:
        d = f"CAST({_escape_braces(date_expr)} AS DATE)"
        n = 1 if grain == "month" else _ROLLING.get(grain)
        if n is not None:
            return f"{d} > last_day(DATE '{{as_of}}' - INTERVAL {n} MONTH) AND {d} <= DATE '{{as_of}}'"
        if grain == "ytd":
            return f"year({d}) = year(DATE '{{as_of}}') AND {d} <= DATE '{{as_of}}'"
        raise CompileError("window_unsupported", f"period grain {grain!r} has no window predicate")
    if grain == "month":
        return "month_end = DATE '{as_of}'"
    if grain in _ROLLING:
        n = _ROLLING[grain]
        return f"month_start > DATE '{{as_of}}' - INTERVAL {n} MONTH AND month_end <= DATE '{{as_of}}'"
    if grain == "ytd":
        return "year(month_start) = year(DATE '{as_of}') AND month_end <= DATE '{as_of}'"
    raise CompileError("window_unsupported", f"period grain {grain!r} has no window predicate")


def literal_for(value: Any, dialect: str) -> str:
    """A SQL string literal for the platform dialect (Spark SQL backslash-escapes; DuckDB doubles quotes)."""
    return databricks_literal(value) if dialect == "databricks" else sql_literal(value)


def scope_predicate(scope: dict[str, Any] | None, allowed: tuple[str, ...], dialect: str = "duckdb",
                    columns: dict[str, str] | None = None) -> str:
    parts = []
    columns = columns or {}
    for dim, value in sorted((scope or {}).items()):
        if dim not in allowed and dim not in columns:
            raise CompileError("scope_unsupported", f"scope dimension {dim!r} is not declared for this KPI")
        col = _escape_braces(columns.get(dim, dim))
        values = value if isinstance(value, (list, tuple)) else [value]
        if len(values) == 1:
            parts.append(f"{col} = {_escape_braces(literal_for(str(values[0]), dialect))}")
        else:
            inner = ", ".join(_escape_braces(literal_for(str(v), dialect)) for v in values)
            parts.append(f"{col} IN ({inner})")
    return " AND ".join(parts) if parts else "TRUE"


def _escape_braces(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


def render_bindings(text: str, bindings: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in bindings:
            raise CompileError("binding_missing", f"unknown binding ${{{name}}}")
        return bindings[name]

    return _BINDING.sub(sub, text)


def _keyword_hit(text: str, keyword: str) -> bool:
    k = normalize_label(keyword)
    return bool(k) and re.search(rf"(?<!\w){re.escape(k)}(?!\w)", text) is not None


def detect_stated_variants(kpi: KpiDefinition, text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return (permitted, other): options whose keywords appear in the report's stated text."""
    if kpi.platform is None or not text:
        return {}, {}
    norm = normalize_label(text)
    permitted: dict[str, str] = {}
    other: dict[str, str] = {}
    for name, param in kpi.platform.params.items():
        hits = [opt for opt, words in param.keywords.items() if any(_keyword_hit(norm, w) for w in words)]
        if len(hits) != 1:
            continue
        opt = hits[0]
        if opt == param.default:
            continue
        (permitted if opt in param.permitted else other)[name] = opt
    return permitted, other


def bindings_for(kpi: KpiDefinition, *, brand: str, scope: dict[str, Any] | None, grain: str,
                 settings: Settings) -> dict[str, str]:
    if not brand:
        raise CompileError("brand_unknown", "the observation has no brand")
    if grain not in GRAINS:
        raise CompileError("window_unsupported", f"period grain {grain!r} is unknown")
    if grain not in kpi.grains:
        raise CompileError("window_unsupported", f"{kpi.kpi_id} is not defined for {grain!r} periods")
    restricted = settings.restricted_schemas[0] if settings.restricted_schemas else "platform_restricted"
    dialect = "databricks" if settings.platform_is_databricks else "duckdb"
    return {
        "gov": settings.platform_prefix(settings.gov_schema),
        "platform": settings.platform_prefix(settings.platform_schema),
        "restricted": settings.platform_prefix(restricted),
        "brand": _escape_braces(literal_for(brand, dialect)),
        "window": window_predicate(grain, kpi.platform.date_expr if kpi.platform else None),
        "scope": scope_predicate(scope, kpi.dimensions, dialect,
                                 kpi.platform.scope_columns if kpi.platform else None),
    }


def compile_kpi(kpi: KpiDefinition, *, brand: str, scope: dict[str, Any] | None, grain: str,
                settings: Settings, stated_text: str = "", reconloop_tolerance: float | None = None,
                assumed_choices: dict[str, str] | None = None) -> CompiledKpi:
    """Build the reconloop spec for one KPI under fixed bindings."""
    if kpi.platform is None:
        raise CompileError("no_platform_definition", f"{kpi.kpi_id} has no platform specification")
    p = kpi.platform
    bindings = bindings_for(kpi, brand=brand, scope=scope, grain=grain, settings=settings)
    entry: dict[str, Any] = {
        "unit": kpi.unit,
        "tolerance": float(reconloop_tolerance if reconloop_tolerance is not None else max(kpi.tolerance.rel, 1e-9)),
        "params": {},
        "checks": {n: render_bindings(sql, bindings) for n, sql in p.checks.items()},
    }
    if p.template:
        entry["template"] = render_bindings(p.template, bindings)
    else:
        entry["ratio"] = {
            "numerator": render_bindings(p.ratio["numerator"], bindings),
            "denominator": render_bindings(p.ratio["denominator"], bindings),
            "scale": float(p.ratio.get("scale", 1.0)),
            "zero_denominator": str(p.ratio.get("zero_denominator", "error")),
        }
    for name, param in p.params.items():
        entry["params"][name] = {
            "hypothesis": param.hypothesis,
            "default": param.default,
            "options": {o: render_bindings(sql, bindings) for o, sql in param.options.items()},
            "describe": dict(param.describe),
            "keywords": {o: list(w) for o, w in param.keywords.items()},
        }
    bindings_record = {"brand": brand, "scope": dict(scope or {}), "grain": grain,
                       "gov": bindings["gov"], "platform": bindings["platform"], "restricted": bindings["restricted"]}
    bindings_hash = sha256_text(canonical_json(bindings_record))
    name = f"{kpi.kpi_id}@{bindings_hash[:12]}"
    spec = load_specs({"metrics": {name: entry}})[name]
    defaults = spec.defaults()
    stated, other = detect_stated_variants(kpi, stated_text)
    assumed: dict[str, str] = {}
    for name, option in (assumed_choices or {}).items():
        param = p.params.get(name)
        if (param is not None and option in param.permitted and option != param.default and name not in stated
                and name not in other):
            assumed[name] = option
    ratio = entry.get("ratio") or {}
    queries = [entry["template"]] if "template" in entry else [ratio["numerator"], ratio["denominator"]]
    complete = window_completeness_checks(queries, grain, p.date_expr, bindings["window"])
    checks = {name: sql for name, sql in complete.items() if name not in spec.checks}
    return CompiledKpi(kpi=kpi, spec=spec, bindings=bindings_record, bindings_hash=bindings_hash,
                       governed_defaults=defaults, start_choices={**defaults, **assumed, **stated},
                       stated_variants={**assumed, **stated}, stated_other=other, assumed_variants=assumed,
                       window_checks=checks)
