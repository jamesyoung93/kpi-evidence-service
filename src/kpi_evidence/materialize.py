from __future__ import annotations

import calendar
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from reconloop.sql import fragment

from .compile import CompileError, CompiledKpi, compile_kpi
from .contracts import KpiResult, PlatformKpiValue, SqlArtifact
from .ids import canonical_json, json_safe, sha256_text, stable_id, utcnow
from .registry import KpiDefinition, Registry
from .settings import Settings
from .store.base import Store, check_ident
from .store.sqltext import databricks_string

log = logging.getLogger(__name__)

AS_OF_SENTINEL = "0001-01-01"
AS_OF_PLACEHOLDER = "{as_of}"
_WINDOW_MONTHS = {"month": 1, "quarter": 3, "r3m": 3, "r6m": 6, "r12m": 12}


def artifact_id_for(kpi_id: str, definition_hash: str, bindings_hash: str, choices: dict[str, str]) -> str:
    """Deterministic artifact id; ``kpi_evidence.reconcile`` stamps it on results before materialization."""
    return stable_id("sql_artifact", kpi_id, definition_hash, bindings_hash, dict(choices))


@dataclass
class _Unit:
    kpi: KpiDefinition
    compiled: CompiledKpi
    choices: dict[str, str]
    artifact_id: str
    results: list[KpiResult] = field(default_factory=list)

    @property
    def governed(self) -> bool:
        return self.choices == self.compiled.governed_defaults

    @property
    def grain(self) -> str:
        return self.compiled.bindings["grain"]

    @property
    def brand(self) -> str:
        return self.compiled.bindings["brand"]

    def choices_text(self) -> str:
        return self.compiled.describe(self.choices) or "none declared (single governed definition)"


def _template(text: str) -> str:
    return text.replace(AS_OF_SENTINEL, AS_OF_PLACEHOLDER)


def _fragments(compiled: CompiledKpi, choices: dict[str, str], as_of: str) -> dict[str, str]:
    frags = {"as_of": as_of}
    for name, param in compiled.spec.params.items():
        frags[name] = fragment(param.options[choices[name]], as_of)
    return frags


def sql_templates(compiled: CompiledKpi, choices: dict[str, str]) -> tuple[str, dict[str, str], dict[str, str]]:
    """(value_sql_template, components_sql, checks_sql), each with ``{as_of}`` left as literal text."""
    spec = compiled.spec
    value = _template(spec.render(choices, AS_OF_SENTINEL))
    components = {k: _template(v) for k, v in spec.render_components(choices, AS_OF_SENTINEL).items()}
    frags = _fragments(compiled, choices, AS_OF_SENTINEL)
    checks = {name: _template(sql.format(**frags)) for name, sql in compiled.checked_spec().checks.items()}
    return value, components, checks


def one_line(value: Any) -> str:
    """Text with every run of whitespace, line breaks included, collapsed to one space."""
    return " ".join(str(value).split())


def _bind_parameter(sql: str) -> str:
    return (sql.replace(f"DATE '{AS_OF_PLACEHOLDER}'", "as_of")
               .replace(f"'{AS_OF_PLACEHOLDER}'", "CAST(as_of AS STRING)")
               .replace(AS_OF_PLACEHOLDER, "CAST(as_of AS STRING)"))


def function_name(unit: _Unit) -> str:
    """``kpi_<id>_v<version>`` for the governed default over the brand total; suffixed otherwise."""
    base = f"kpi_{unit.kpi.kpi_id}_v{unit.kpi.version}"
    if unit.governed and not unit.compiled.bindings.get("scope"):
        return check_ident(base)
    return check_ident(f"{base}_{unit.artifact_id[:8]}")


def function_sql(settings: Settings, unit: _Unit, components: dict[str, str]) -> str:
    """Databricks SQL function text for humans and Genie (``RETURNS DOUBLE``; one DATE parameter)."""
    kpi = unit.kpi
    qualified = settings.platform_qualified(settings.gov_schema, function_name(unit))
    ratio = unit.compiled.spec.ratio
    definition = unit.compiled.definition_hash[:12]
    header = [f"-- {kpi.kpi_id} v{kpi.version} ({one_line(kpi.label)}); definition {definition}",
              f"-- choices: {one_line(unit.choices_text())}",
              "-- Data-quality checks are not part of the function; see checks_sql."]
    if ratio is None:
        body = f"(\n{_bind_parameter(components['value'])}\n)"
    else:
        header.append("-- Ratio: a zero denominator returns NULL here, while the pipeline "
                      "(MetricSpec.compute, zero_denominator=error) raises an error instead.")
        body = (f"(\n{_bind_parameter(components['numerator'])}\n) / NULLIF((\n"
                f"{_bind_parameter(components['denominator'])}\n), 0) * {ratio.scale!r}")
    comment = databricks_string(one_line(f"{kpi.label} v{kpi.version}"))
    return "\n".join(header) + (f"\nCREATE OR REPLACE FUNCTION {qualified}(as_of DATE)\nRETURNS DOUBLE\n"
                                f"COMMENT {comment}\nRETURN CAST({body} AS DOUBLE)")


def export_rel_path(unit: _Unit) -> str:
    """Volume-relative export path; non-default choices get the artifact id so files never collide."""
    name = f"{unit.compiled.definition_hash[:12]}_{unit.compiled.bindings_hash[:8]}"
    if not unit.governed:
        name += f"_{unit.artifact_id[:8]}"
    return f"artifacts/sql/{unit.kpi.kpi_id}/{name}.sql"


def export_text(unit: _Unit, artifact: SqlArtifact, generated_at: datetime) -> str:
    kpi = unit.kpi
    lines = [
        f"-- KPI: {kpi.kpi_id} ({one_line(kpi.label)}) version {kpi.version}",
        f"-- Definition: {one_line(kpi.definition)}",
        f"-- Definition hash: {unit.compiled.definition_hash}",
        f"-- Choices: {one_line(unit.choices_text())}"
        + ("" if unit.governed else " (not the governed default)"),
        f"-- Bindings: {canonical_json(unit.compiled.bindings)}",
        f"-- Artifact: {artifact.artifact_id}  sql_sha: {artifact.sql_sha}",
        f"-- Generated at: {generated_at.isoformat()}Z",
        "-- Replace {as_of} with the measurement period end (YYYY-MM-DD) before running.",
        "",
        "-- Value",
        artifact.value_sql_template.rstrip() + ";",
    ]
    if set(artifact.components_sql) != {"value"}:
        lines += ["", "-- Components (the pipeline divides these in Python)"]
        for name, sql in artifact.components_sql.items():
            lines += [f"-- {one_line(name)}", sql.rstrip() + ";"]
    if artifact.checks_sql:
        lines += ["", "-- Checks (each must return 0 before the value is trusted)"]
        for name, sql in artifact.checks_sql.items():
            lines += [f"-- check: {one_line(name)}", sql.rstrip() + ";"]
    if artifact.function_sql:
        lines += ["", "-- Databricks SQL function", artifact.function_sql.rstrip() + ";"]
    return "\n".join(lines) + "\n"


def build_artifact(settings: Settings, unit: _Unit, now: datetime) -> SqlArtifact:
    value, components, checks = sql_templates(unit.compiled, unit.choices)
    sha = sha256_text(canonical_json({"value": value, "components": components, "checks": checks}))
    return SqlArtifact(
        artifact_id=unit.artifact_id, kpi_id=unit.kpi.kpi_id, kpi_version=unit.kpi.version,
        definition_hash=unit.compiled.definition_hash, bindings=unit.compiled.bindings, choices=dict(unit.choices),
        value_sql_template=value, components_sql=components, checks_sql=checks,
        function_sql=function_sql(settings, unit, components), sql_sha=sha, export_path=None, created_at=now)


def _export(settings: Settings, unit: _Unit, artifact: SqlArtifact, now: datetime) -> str | None:
    from .store.files import files_for

    try:
        return files_for(settings).write_bytes(export_rel_path(unit), export_text(unit, artifact, now).encode("utf-8"))
    except Exception as exc:
        log.warning("SQL export failed for %s: %s", unit.artifact_id, exc)
        return None


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _months_back(d: date, k: int) -> date:
    idx = d.year * 12 + (d.month - 1) - k
    return _month_end(idx // 12, idx % 12 + 1)


def complete_period_ends(month_ends: list[date], grain: str) -> list[date]:
    """Period ends whose whole window lies inside the covered months (partial windows are skipped)."""
    present = set(month_ends)
    ordered = sorted(present)
    if grain == "ytd":
        return [m for m in ordered if all(_month_end(m.year, k) in present for k in range(1, m.month + 1))]
    if grain not in _WINDOW_MONTHS:
        return []
    n = _WINDOW_MONTHS[grain]
    candidates = [m for m in ordered if m.month % 3 == 0] if grain == "quarter" else ordered
    return [m for m in candidates if all(_months_back(m, k) in present for k in range(n))]


def window_start(end: date, grain: str) -> date:
    if grain == "ytd":
        return date(end.year, 1, 1)
    return _months_back(end, _WINDOW_MONTHS.get(grain, 1) - 1).replace(day=1)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except (ValueError, TypeError):
            return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def source_view(unit: _Unit) -> str:
    """The governed view the chosen definition reads (first declared source view named in the SQL)."""
    views = unit.kpi.platform.source_views
    sql = unit.compiled.spec.render(unit.choices, AS_OF_SENTINEL)
    for view in views:
        if re.search(rf"\b{re.escape(view)}\b", sql):
            return view
    return views[0]


_FROM_RELATION = re.compile(r"\bFROM\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*){0,2})\b", re.I)


def period_listing_sql(store: Store, unit: _Unit) -> str:
    """SQL listing the month ends the platform source covers for this unit."""
    date_expr = unit.kpi.platform.date_expr
    if not date_expr:
        return (f"SELECT DISTINCT month_end AS month_end FROM {store.gov(source_view(unit))} "
                f"WHERE brand = :brand")
    match = _FROM_RELATION.search(unit.compiled.spec.render(unit.choices, AS_OF_SENTINEL))
    if match is None:
        raise ValueError("no source relation found in the KPI SQL")
    return f"SELECT DISTINCT last_day(CAST({date_expr} AS DATE)) AS month_end FROM {match.group(1)}"


def period_ends(store: Store, unit: _Unit) -> list[date]:
    """Complete period ends covered by the platform source, plus the reconciled periods themselves."""
    months: list[date] = []
    try:
        sql = period_listing_sql(store, unit)
        df = store.platform_access.query(sql, {"brand": unit.brand} if ":brand" in sql else None)
        months = [d for d in (_as_date(v) for v in df["month_end"].tolist()) if d is not None]
    except Exception as exc:
        log.info("could not list periods for %s: %s", unit.kpi.kpi_id, exc)
    ends = set(complete_period_ends(months, unit.grain))
    ends |= {r.period_end for r in unit.results if r.period_end is not None}
    return sorted(ends)


def _snapshot(store: Store, settings: Settings, unit: _Unit) -> dict[str, Any]:
    try:
        from . import platform as _platform
        from .reconcile import snapshot_names

        return dict(_platform.platform_snapshot(store, settings, snapshot_names(unit.kpi, unit.compiled)) or {})
    except Exception:
        return {}


def platform_series(store: Store, unit: _Unit, scope_key: str, snapshot: dict[str, Any],
                    now: datetime) -> list[PlatformKpiValue]:
    from .reconcile import read_session

    with read_session(store):
        return _series_rows(store, unit, scope_key, snapshot, now)


def _series_rows(store: Store, unit: _Unit, scope_key: str, snapshot: dict[str, Any],
                 now: datetime) -> list[PlatformKpiValue]:
    runner = store.platform_access.runner()
    rows = []
    for end in period_ends(store, unit):
        try:
            value, components = unit.compiled.spec.compute(runner, unit.choices, end.isoformat())
            comps = {k: v for k, v in (components or {}).items() if k != "queries"}
            error = None
        except Exception as exc:
            value, comps, error = None, {}, f"{type(exc).__name__}: {exc}"[:2000]
        rows.append(PlatformKpiValue(
            kpi_id=unit.kpi.kpi_id, definition_hash=unit.compiled.definition_hash, scope_key=scope_key,
            period_grain=unit.grain, period_end=end, period_start=window_start(end, unit.grain), brand=unit.brand,
            value=value, components=json_safe(comps), sql_artifact_id=unit.artifact_id, platform_snapshot=snapshot,
            computed_at=now, error=error))
    return rows


def _units(registry: Registry, settings: Settings, results: list[KpiResult]) -> dict[str, _Unit]:
    units: dict[str, _Unit] = {}
    for r in results:
        if r.platform_value is None or not r.kpi_id or not r.definition_hash or not r.period_grain or not r.brand:
            continue
        kpi = registry.kpis.get(r.kpi_id)
        if kpi is None or kpi.platform is None or kpi.definition_hash() != r.definition_hash:
            continue
        try:
            compiled = compile_kpi(kpi, brand=r.brand, scope=json.loads(r.scope_key or "{}"), grain=r.period_grain,
                                   settings=settings)
            choices = compiled.spec.initial_choices(r.definition_choices or {})
        except (CompileError, ValueError):
            continue
        aid = artifact_id_for(kpi.kpi_id, compiled.definition_hash, compiled.bindings_hash, choices)
        units.setdefault(aid, _Unit(kpi, compiled, choices, aid)).results.append(r)
    return units


def _pkv_key(row: PlatformKpiValue) -> tuple:
    return (row.sql_artifact_id, row.period_grain, row.period_end)


def _same_value_row(a: PlatformKpiValue, b: PlatformKpiValue) -> bool:
    return (canonical_json([a.value, a.error, a.components, a.period_start, a.brand, a.scope_key, a.platform_snapshot])
            == canonical_json([b.value, b.error, b.components, b.period_start, b.brand, b.scope_key,
                               b.platform_snapshot]))


def _existing_values(store: Store, rows: list[PlatformKpiValue]) -> dict[tuple, PlatformKpiValue]:
    out: dict[tuple, PlatformKpiValue] = {}
    for artifact_id in sorted({r.sql_artifact_id for r in rows}):
        for row in store.read(PlatformKpiValue, "sql_artifact_id = :a", {"a": artifact_id}):
            out[_pkv_key(row)] = row
    return out


def _write_series(store: Store, governed: dict[tuple, PlatformKpiValue], variants: dict[tuple, PlatformKpiValue]) -> int:
    rows = {**variants, **governed}
    existing = _existing_values(store, list(rows.values()))
    writes = [row for key, row in rows.items() if key not in existing or not _same_value_row(existing[key], row)]
    if writes:
        store.upsert(writes)
    return len(writes)


def series_is_current(store: Store, unit: _Unit, old: SqlArtifact | None, artifact: SqlArtifact,
                      snapshot: dict[str, Any]) -> bool:
    """True when the stored series of this artifact was computed from the same SQL and the same platform data."""
    from .platform import is_available

    if old is None or old.sql_sha != artifact.sql_sha or not is_available(snapshot):
        return False
    rows = store.read(PlatformKpiValue, "sql_artifact_id = :a", {"a": unit.artifact_id})
    if not rows or any(canonical_json(r.platform_snapshot or {}) != canonical_json(snapshot) for r in rows):
        return False
    covered = {(r.period_grain, r.period_end) for r in rows}
    return all((unit.grain, r.period_end) in covered for r in unit.results if r.period_end is not None)


def materialize(store: Store, registry: Registry, settings: Settings, results: list[KpiResult],
                run_id: str) -> list[SqlArtifact]:
    """Write SQL artifacts (+ .sql exports) and platform series for results with a platform value (idempotent)."""
    from .reconcile import stored_form

    now = utcnow()
    units = _units(registry, settings, results)
    if not units:
        return []
    artifacts: list[SqlArtifact] = []
    governed_rows: dict[tuple, PlatformKpiValue] = {}
    variant_rows: dict[tuple, PlatformKpiValue] = {}
    relinked: list[KpiResult] = []
    snapshots: dict[str, dict[str, Any]] = {}
    for unit in sorted(units.values(), key=lambda u: (not u.governed, u.kpi.kpi_id, u.artifact_id)):
        artifact = build_artifact(settings, unit, now)
        old = store.get(SqlArtifact, artifact_id=unit.artifact_id)
        if old is not None and old.sql_sha == artifact.sql_sha:
            artifact.created_at = old.created_at
        artifact.export_path = _export(settings, unit, artifact, now) or (old.export_path if old else None)
        artifacts.append(artifact)
        if unit.kpi.kpi_id not in snapshots:
            snapshots[unit.kpi.kpi_id] = _snapshot(store, settings, unit)
        if not series_is_current(store, unit, old, artifact, snapshots[unit.kpi.kpi_id]):
            target = governed_rows if unit.governed else variant_rows
            for row in platform_series(store, unit, unit.results[0].scope_key, snapshots[unit.kpi.kpi_id], now):
                target.setdefault(_pkv_key(row), row)
        for r in unit.results:
            if r.sql_artifact_id != unit.artifact_id:
                r.sql_artifact_id = unit.artifact_id
                relinked.append(stored_form(r))
    store.upsert(artifacts)
    _write_series(store, governed_rows, variant_rows)
    if relinked:
        store.upsert(relinked)
    return artifacts
