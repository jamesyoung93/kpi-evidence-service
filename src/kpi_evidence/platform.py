from __future__ import annotations

import functools
import re
import time
import warnings
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .contracts import DqResult, ReasonCode, ValidationStatus
from .ids import canonical_json, sha256_text, stable_id, utcnow
from .registry import DIMENSIONS
from .settings import Settings, default_sql_dir, demo_dir
from .store.base import MetricQueryError, check_ident, scalar_value

DEMO_BRAND = "KESTREL"


@dataclass(frozen=True)
class BaseTable:
    schema_role: str
    csv: str | None = None


BASE_TABLES: dict[str, BaseTable] = {
    "rx_account_monthly": BaseTable("platform", "fact_source_a.csv"),
    "rx_panel_monthly": BaseTable("platform", "fact_source_b.csv"),
    "referrals_monthly": BaseTable("platform", "fact_referral.csv"),
    "accounts": BaseTable("platform", "accounts.csv"),
    "exfactory_monthly": BaseTable("restricted"),
}


@dataclass(frozen=True)
class GovernedView:
    description: str


GOVERNED_VIEWS: dict[str, GovernedView] = {
    "gv_rx_account_monthly": GovernedView("account x month prescriptions and CRM activity"),
    "gv_rx_panel_monthly": GovernedView("projected retail panel, latest load per grain"),
    "gv_referrals_monthly": GovernedView("observed referrals (about 80% HCP coverage)"),
}

DIRECT_SOURCES: dict[str, tuple[str, ...]] = {"exfactory_monthly": ("exfactory_monthly",)}


@dataclass(frozen=True)
class DqCheck:
    check: str
    severity: str
    columns: tuple[str, ...] = ()
    lag_months: int = 0
    note: str = ""


DQ_CHECKS: dict[str, tuple[DqCheck, ...]] = {
    "gv_rx_account_monthly": (
        DqCheck("unique_grain", "hard", ("brand", "account_id", "month")),
        DqCheck("complete_months", "hard"),
        DqCheck("not_null_keys", "hard", ("brand", "account_id", "month", "month_start", "month_end")),
        DqCheck("fresh", "soft", lag_months=0),
    ),
    "gv_rx_panel_monthly": (
        DqCheck("unique_grain", "hard", ("brand", "month", "territory", "payer_channel")),
        DqCheck("complete_months", "hard"),
        DqCheck("not_null_keys", "hard", ("brand", "month", "territory", "payer_channel", "month_start", "month_end")),
        DqCheck("fresh", "soft", lag_months=1),
    ),
    "gv_referrals_monthly": (
        DqCheck("unique_grain", "hard", ("brand", "account_id", "month")),
        DqCheck("complete_months", "hard"),
        DqCheck("not_null_keys", "hard", ("brand", "account_id", "month", "month_start", "month_end")),
        DqCheck("fresh", "soft", lag_months=0),
    ),
}

BASE_TABLE_DQ_CHECKS: dict[str, tuple[DqCheck, ...]] = {
    "rx_panel_monthly": (
        DqCheck("unique_grain", "soft", ("month", "territory", "payer_channel"),
                note="informational: the base table keeps every data load; gv_rx_panel_monthly keeps the latest"),
    ),
}

FRESHNESS_REFERENCE_VIEW = "gv_rx_account_monthly"
DIMENSION_SOURCE_VIEW = "gv_rx_account_monthly"

PANEL_RX_COLUMNS = ("trx_units", "trx_dollars", "trx_normalized", "nrx", "nbrx", "market_trx", "market_nrx",
                    "il17_competitor_a_trx", "il17_competitor_b_trx", "advanced_other_trx", "il17_class_trx",
                    "advanced_therapy_trx")
PANEL_CURRENT_LOAD = ("L2026-06", "2026-06-05")
PANEL_RESTATED_LOAD = ("L2024-10", "2024-10-05")
PANEL_RESTATED_MONTHS = ("2024-07", "2024-08", "2024-09")
PANEL_RESTATEMENT_FACTOR = 1.015
EXFACTORY_UNITS_PER_TRX = 1.04


def restricted_schema(settings: Settings) -> str:
    """The schema holding sources the pipeline identity may not read (same rule as compile's ${restricted})."""
    return settings.restricted_schemas[0] if settings.restricted_schemas else "platform_restricted"


def base_table_fq(settings: Settings, table: str) -> str:
    """Qualified name of a declared base table for the active backend."""
    spec = BASE_TABLES[table]
    schema = settings.platform_schema if spec.schema_role == "platform" else restricted_schema(settings)
    return settings.platform_qualified(check_ident(schema), check_ident(table))


def gov_view_fq(settings: Settings, view: str) -> str:
    return settings.platform_qualified(check_ident(settings.gov_schema), check_ident(view))


def sql_bindings(settings: Settings) -> dict[str, str]:
    """Schema-prefix placeholders for platform SQL files (``${gov}``, ``${platform}``, ``${restricted}``)."""
    return {
        "gov": settings.platform_prefix(check_ident(settings.gov_schema)),
        "platform": settings.platform_prefix(check_ident(settings.platform_schema)),
        "restricted": settings.platform_prefix(check_ident(restricted_schema(settings))),
    }


_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_]+)\}")


def render_sql(text: str, settings: Settings) -> str:
    """Fill ``${gov}``/``${platform}``/``${restricted}``; an unknown placeholder is an error, never left in."""
    bindings = sql_bindings(settings)

    def sub(m: re.Match) -> str:
        if m.group(1) not in bindings:
            raise ValueError(f"unknown placeholder ${{{m.group(1)}}} in platform SQL")
        return bindings[m.group(1)]

    return _PLACEHOLDER.sub(sub, text)


def platform_of(store: Any) -> Any:
    """The object KPI/platform SQL runs on: ``store.platform_access`` (a PlatformAccess) or the store itself."""
    return getattr(store, "platform_access", store)


def ensure_platform_schemas(store: Any, settings: Settings) -> list[str]:
    """CREATE SCHEMA IF NOT EXISTS for the platform, governed and restricted schemas."""
    schemas = list(dict.fromkeys(sql_bindings(settings).values()))
    for schema in schemas:
        for part in schema.split("."):
            check_ident(part)
        platform_of(store).execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    return schemas


_CSV_DTYPES = {"npi": str, "month": str, "first_brand_month": str}


@functools.lru_cache(maxsize=4)
def _read_demo_csvs(directory: str) -> dict[str, pd.DataFrame]:
    d = Path(directory)
    out = {}
    for table, spec in BASE_TABLES.items():
        if spec.csv is None:
            continue
        path = d / spec.csv
        if not path.exists():
            raise FileNotFoundError(f"demo platform file missing: {path}")
        out[table] = pd.read_csv(path, dtype=_CSV_DTYPES)
    return out


def _nullable_floats(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for col in out.columns:
        if pd.api.types.is_float_dtype(out[col]) and out[col].isna().any():
            out[col] = out[col].astype("Float64")
    return out


def panel_loads(source_b: pd.DataFrame) -> pd.DataFrame:
    """fact_source_b as the current load plus an older, pre-restatement load of the first three months."""
    current = source_b.assign(load_id=PANEL_CURRENT_LOAD[0], loaded_at=pd.Timestamp(PANEL_CURRENT_LOAD[1]))
    older = source_b[source_b["month"].isin(PANEL_RESTATED_MONTHS)].copy()
    older[list(PANEL_RX_COLUMNS)] = (older[list(PANEL_RX_COLUMNS)] / PANEL_RESTATEMENT_FACTOR).round(6)
    older = older.assign(load_id=PANEL_RESTATED_LOAD[0], loaded_at=pd.Timestamp(PANEL_RESTATED_LOAD[1]))
    return pd.concat([older, current], ignore_index=True)


def exfactory_monthly(source_a: pd.DataFrame, brand: str = DEMO_BRAND) -> pd.DataFrame:
    """Restricted ex-factory feed: per month round(SUM(trx_units) x 1.04) units shipped."""
    monthly = source_a.groupby("month", as_index=False)["trx_units"].sum().sort_values("month")
    units = (monthly["trx_units"] * EXFACTORY_UNITS_PER_TRX).round(0).astype("int64")
    return pd.DataFrame({"brand": brand, "month": monthly["month"].astype(str).to_numpy(), "units_shipped": units.to_numpy()})


def demo_platform_frames(settings: Settings | None = None, data_dir: str | Path | None = None) -> dict[str, pd.DataFrame]:
    """Build every demo base table as a DataFrame (no store access)."""
    if data_dir is None and settings is not None:
        data_dir = settings.extra.get("demo_platform_dir")
    directory = Path(data_dir) if data_dir else demo_dir("platform")
    raw = _read_demo_csvs(str(directory.resolve()))
    frames = {
        "rx_account_monthly": raw["rx_account_monthly"],
        "rx_panel_monthly": panel_loads(raw["rx_panel_monthly"]),
        "referrals_monthly": raw["referrals_monthly"],
        "accounts": raw["accounts"],
        "exfactory_monthly": exfactory_monthly(raw["rx_account_monthly"]),
    }
    return {name: _nullable_floats(frame) for name, frame in frames.items()}


def load_demo_platform(store: Any, settings: Settings) -> dict[str, int]:
    """Write the synthetic platform base tables."""
    frames = demo_platform_frames(settings)
    ensure_platform_schemas(store, settings)
    counts: dict[str, int] = {}
    for name, frame in frames.items():
        fq = base_table_fq(settings, name)
        counts[fq] = platform_of(store).write_dataframe(fq, frame, mode="overwrite")
    return counts


_CREATE_VIEW = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)", re.I)


def governed_views_sql(settings: Settings) -> str:
    """The rendered governed-views script for the active backend."""
    path = default_sql_dir() / "governed_views.sql"
    return render_sql(path.read_text(encoding="utf-8"), settings)


def view_names(script: str) -> list[str]:
    """Unqualified names of the views a script creates, in file order."""
    return [m.group(1).split(".")[-1] for m in _CREATE_VIEW.finditer(script)]


def create_governed_views(store: Any, settings: Settings) -> list[str]:
    """Create or replace the governed views; returns their (unqualified) names."""
    script = governed_views_sql(settings)
    platform_of(store).execute(f"CREATE SCHEMA IF NOT EXISTS {sql_bindings(settings)['gov']}")
    platform_of(store).execute_script(script)
    return view_names(script)


_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def _month_index(label: str) -> int | None:
    m = _MONTH.match(str(label).strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    return int(m.group(1)) * 12 + int(m.group(2)) - 1


def _month_label(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _reference_label(reference: str | date | datetime | None) -> str | None:
    if reference is None:
        return None
    if isinstance(reference, (date, datetime)):
        return f"{reference.year:04d}-{reference.month:02d}"
    text = str(reference).strip()[:7]
    if _month_index(text) is None:
        raise ValueError(f"reference month must be YYYY-MM or a date, got {reference!r}")
    return text


def check_sql(check: DqCheck, fq: str) -> str:
    """The SQL a check runs (portable: DuckDB, Databricks SQL, Spark SQL)."""
    cols = ", ".join(check_ident(c) for c in check.columns)
    if check.check == "unique_grain":
        return (f"SELECT COUNT(*) AS dup_keys, COALESCE(SUM(n - 1), 0) AS extra_rows "
                f"FROM (SELECT {cols}, COUNT(*) AS n FROM {fq} GROUP BY {cols} HAVING COUNT(*) > 1) d")
    if check.check == "not_null_keys":
        cond = " OR ".join(f"{check_ident(c)} IS NULL" for c in check.columns)
        return f"SELECT COUNT(*) AS violations FROM {fq} WHERE {cond}"
    if check.check == "complete_months":
        return f"SELECT DISTINCT month FROM {fq}"
    if check.check == "fresh":
        return f"SELECT MAX(month) AS latest_month FROM {fq}"
    raise ValueError(f"unknown DQ check {check.check!r}")


def _example_duplicates_sql(check: DqCheck, fq: str) -> str:
    cols = ", ".join(check.columns)
    return f"SELECT {cols}, COUNT(*) AS n FROM {fq} GROUP BY {cols} HAVING COUNT(*) > 1 ORDER BY n DESC, {cols} LIMIT 3"


def _evaluate(store: Any, check: DqCheck, fq: str, reference: str | None) -> tuple[bool, int, str]:
    df = platform_of(store).query(check_sql(check, fq))
    if check.check == "unique_grain":
        dup_keys, extra = int(df.iloc[0, 0] or 0), int(df.iloc[0, 1] or 0)
        if extra == 0:
            return True, 0, f"one row per ({', '.join(check.columns)})"
        examples = platform_of(store).query(_example_duplicates_sql(check, fq)).to_dict(orient="records")
        sample = "; ".join(", ".join(f"{k}={v}" for k, v in row.items()) for row in examples)
        return False, extra, f"{extra} duplicate rows over {dup_keys} ({', '.join(check.columns)}) keys, e.g. {sample}"
    if check.check == "not_null_keys":
        n = int(df.iloc[0, 0] or 0)
        return n == 0, n, f"{n} rows with a NULL in ({', '.join(check.columns)})"
    if check.check == "complete_months":
        labels = [str(v) for v in df.iloc[:, 0].tolist() if v is not None and not pd.isna(v)]
        if not labels:
            return False, 1, "no rows: the view has no months"
        indexes = [_month_index(v) for v in labels]
        bad = sorted(v for v, i in zip(labels, indexes) if i is None)
        good = sorted({i for i in indexes if i is not None})
        present = set(good)
        missing = [_month_label(i) for i in range(good[0], good[-1] + 1) if i not in present] if good else []
        violations = len(missing) + len(bad)
        detail = f"{len(good)} months {_month_label(good[0])}..{_month_label(good[-1])}" if good else "no valid months"
        if missing:
            detail += f"; missing {', '.join(missing[:12])}" + (" ..." if len(missing) > 12 else "")
        if bad:
            detail += f"; unparseable month values {bad[:5]}"
        return violations == 0, violations, detail
    if check.check == "fresh":
        latest = df.iloc[0, 0]
        latest_i = None if latest is None or pd.isna(latest) else _month_index(str(latest))
        if reference is None:
            return False, 1, "reference month unavailable (the reference feed could not be read)"
        expected_i = _month_index(reference) - check.lag_months
        expected = _month_label(expected_i)
        if latest_i is None:
            return False, 1, f"no latest month; expected >= {expected}"
        behind = max(0, expected_i - latest_i)
        return behind == 0, behind, (f"latest month {_month_label(latest_i)}; expected >= {expected} "
                                     f"(reference {reference}, lag {check.lag_months})")
    raise ValueError(f"unknown DQ check {check.check!r}")


def _resolve_reference(store: Any, settings: Settings, reference: str | date | datetime | None) -> str | None:
    label = _reference_label(reference)
    if label is not None:
        return label
    try:
        df = platform_of(store).query(f"SELECT MAX(month) AS m FROM {gov_view_fq(settings, FRESHNESS_REFERENCE_VIEW)}")
    except Exception:
        return None
    value = df.iloc[0, 0]
    return None if value is None or pd.isna(value) or _month_index(str(value)) is None else str(value)


def _views_used_by(registry: Any) -> dict[str, list[str]]:
    used: dict[str, list[str]] = defaultdict(list)
    if registry is None:
        return used
    for kpi in sorted(registry.kpis.values(), key=lambda k: k.kpi_id):
        if kpi.platform is not None:
            for view in kpi.platform.source_views:
                used[view].append(kpi.kpi_id)
    return used


def evaluate_dq_checks(store: Any, settings: Settings, check_run_id: str, *, registry: Any = None,
                       reference_month: str | date | datetime | None = None,
                       checked_at: datetime | None = None) -> list[DqResult]:
    """Run every declared check and return the rows (no writes)."""
    checked_at = checked_at or utcnow()
    reference = _resolve_reference(store, settings, reference_month)
    used = _views_used_by(registry)
    targets = [(view, gov_view_fq(settings, view), checks) for view, checks in DQ_CHECKS.items()]
    targets += [(base_table_fq(settings, t), base_table_fq(settings, t), checks) for t, checks in BASE_TABLE_DQ_CHECKS.items()]
    results = []
    for object_name, fq, checks in targets:
        for check in checks:
            try:
                passed, violations, detail = _evaluate(store, check, fq, reference)
            except Exception as exc:
                status, code, why = classify_error(exc)
                passed, violations, detail = False, 1, f"check could not run ({status.value}/{code}): {why}"
            if check.note:
                detail = f"{check.note}. {detail}"
            if not passed and check.severity == "hard" and used.get(object_name):
                detail += f". Blocks KPIs: {', '.join(used[object_name])}"
            results.append(DqResult(check_run_id=check_run_id, object_name=object_name, check_name=check.check,
                                    severity=check.severity, passed=passed, violations=int(violations),
                                    detail=detail[:2000], checked_at=checked_at))
    return results


def dq_check_run_id(run_id: str) -> str:
    return stable_id("dq_check_run", run_id)


def run_dq_checks(store: Any, registry: Any, settings: Settings, run_id: str, *,
                  reference_month: str | date | datetime | None = None) -> list[DqResult]:
    """Run the governed-view checks and write them to ``dq_results`` (re-running a run_id replaces its rows)."""
    check_run_id = dq_check_run_id(run_id)
    results = evaluate_dq_checks(store, settings, check_run_id, registry=registry, reference_month=reference_month)
    store.replace(DqResult, {"check_run_id": check_run_id}, results)
    return results


def latest_dq_results(store: Any) -> list[DqResult]:
    """Rows of the most recent check run (empty when DQ never ran)."""
    df = store.query(f"SELECT check_run_id, MAX(checked_at) AS last_checked_at FROM {store.fq(DqResult.TABLE)} "
                     f"GROUP BY check_run_id ORDER BY last_checked_at DESC, check_run_id DESC LIMIT 1")
    if df.empty:
        return []
    return store.read(DqResult, "check_run_id = :cid", {"cid": str(df.iloc[0, 0])})


def dq_status_by_view(store: Any, settings: Settings, registry: Any = None) -> dict[str, str]:
    """``{view: 'pass' | 'fail' | 'not_checked'}`` from the latest check run."""
    base_objects = {base_table_fq(settings, t) for t in BASE_TABLE_DQ_CHECKS}
    hard: dict[str, list[bool]] = defaultdict(list)
    for row in latest_dq_results(store):
        if row.object_name not in base_objects and row.severity == "hard":
            hard[row.object_name].append(bool(row.passed))
    status = {view: "not_checked" for view in GOVERNED_VIEWS}
    status.update({view: "pass" if all(flags) else "fail" for view, flags in hard.items()})
    if registry is None:
        try:
            from .registry import load_registry

            registry = load_registry(settings.resolved_registry_dir())
        except FileNotFoundError:
            registry = None
    for view in _views_used_by(registry):
        status.setdefault(view, "not_checked")
    return status


def dimension_values(store: Any, settings: Settings) -> dict[str, set[str]]:
    """``{dimension: values}`` for region, district, territory, specialty and payer channel."""
    cols = ", ".join(DIMENSIONS)
    df = platform_of(store).query(f"SELECT DISTINCT {cols} FROM {gov_view_fq(settings, DIMENSION_SOURCE_VIEW)}")
    return {d: {str(v).strip() for v in df[d].tolist() if v is not None and not pd.isna(v) and str(v).strip()}
            for d in DIMENSIONS}


@functools.lru_cache(maxsize=16)
def _view_sources(script: str) -> dict[str, tuple[str, ...]]:
    from .compile import sql_relations, strip_sql_text

    text = strip_sql_text(script)
    matches = list(_CREATE_VIEW.finditer(text))
    out: dict[str, tuple[str, ...]] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[m.group(1).split(".")[-1].lower()] = tuple(sql_relations(text[m.end():end]))
    return out


def governed_view_sources(settings: Settings) -> dict[str, tuple[str, ...]]:
    """Relations each governed view reads, from the FROM and JOIN clauses of the rendered governed_views.sql."""
    return _view_sources(governed_views_sql(settings))


def _governed_view(settings: Settings, name: str, views: Iterable[str]) -> str | None:
    head, _, short = name.rpartition(".")
    if short.lower() not in views:
        return None
    if not head:
        return short.lower()
    prefixes = {settings.platform_prefix(settings.gov_schema).lower(), settings.gov_schema.lower()}
    return short.lower() if head.lower() in prefixes else None


def snapshot_sources(settings: Settings, names: Iterable[str]) -> dict[str, tuple[str, ...] | str]:
    """The relations that pin each source name, or the reason it cannot be pinned."""
    try:
        sources: dict[str, tuple[str, ...]] | None = governed_view_sources(settings)
    except (OSError, ValueError):
        sources = None
    views = set(sources or ()) | {v.lower() for v in GOVERNED_VIEWS}
    out: dict[str, tuple[str, ...] | str] = {}
    for name in dict.fromkeys(str(n) for n in names):
        short = name.split(".")[-1]
        view = _governed_view(settings, name, views)
        if view is not None:
            relations = (sources or {}).get(view)
            out[name] = relations if relations else (
                "unavailable: unknown_object: the governed view has no readable definition in governed_views.sql")
        elif "." not in name and short in DIRECT_SOURCES:
            out[name] = tuple(base_table_fq(settings, t) for t in DIRECT_SOURCES[short])
        elif "." not in name and short in BASE_TABLES:
            out[name] = (base_table_fq(settings, short),)
        elif _DOTTED.match(name):
            out[name] = (name,)
        else:
            out[name] = "unavailable: unknown_object: not a governed view, direct source or table"
    return out


def _unavailable(exc: BaseException) -> str:
    _status, code, detail = classify_error(exc)
    return f"unavailable: {code}: {detail}"[:300]


_NUMERIC_TYPE = re.compile(r"^(TINYINT|SMALLINT|INTEGER|INT|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|UBIGINT"
                           r"|UHUGEINT|FLOAT|REAL|DOUBLE|DECIMAL|NUMERIC)\b", re.I)
_DOTTED = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){1,2}$")


def _relation_columns(store: Any, relation: str) -> list[tuple[str, str]]:
    parts = relation.split(".")
    where = "table_schema = :s AND table_name = :t" + (" AND table_catalog = :c" if len(parts) == 3 else "")
    params = {"s": parts[-2], "t": parts[-1], **({"c": parts[0]} if len(parts) == 3 else {})}
    df = platform_of(store).query(f"SELECT column_name, data_type FROM information_schema.columns WHERE {where} "
                                  f"ORDER BY ordinal_position", params)
    if df.empty:
        raise MetricQueryError(f"Catalog Error: table {relation} does not exist")
    return [(str(name), str(kind)) for name, kind in zip(df["column_name"], df["data_type"])]


def relation_content_hash(store: Any, relation: str) -> str:
    """Content hash of any relation: row count plus a sum per column (numeric values, or value hashes)."""
    for part in relation.split("."):
        check_ident(part)
    cols = _relation_columns(store, relation)
    quoted = ['"' + c.replace('"', '""') + '"' for c, _kind in cols]
    sums = [f"CAST(SUM(CAST({q} AS DECIMAL(38,6))) AS VARCHAR)" if _NUMERIC_TYPE.match(kind)
            else f"CAST(SUM(hash({q})) AS VARCHAR)" for q, (_c, kind) in zip(quoted, cols)]
    sql = f"SELECT {', '.join(['COUNT(*) AS n', *(f'{s} AS s{i}' for i, s in enumerate(sums))])} FROM {relation}"
    check = getattr(platform_of(store).runner(), "check_access", None)
    if check is not None:
        check(sql)
    row = platform_of(store).query(sql).iloc[0].tolist()
    payload = {"relation": relation, "rows": int(row[0]),
               "columns": [[c, k, None if v is None or pd.isna(v) else str(v)] for (c, k), v in zip(cols, row[1:])]}
    return "content:" + sha256_text(canonical_json(payload))[:16]


def relation_delta_version(store: Any, relation: str) -> str:
    """Latest Delta version of a table (``DESCRIBE HISTORY``); raises for views and non-Delta data."""
    for part in relation.split("."):
        check_ident(part)
    df = platform_of(store).query(f"DESCRIBE HISTORY {relation} LIMIT 1")
    if df.empty or "version" not in df.columns:
        raise MetricQueryError("DESCRIBE HISTORY returned no version")
    return f"delta_version:{int(df['version'].iloc[0])}"


def is_available(snapshot: dict[str, Any] | None) -> bool:
    """True when a snapshot pins every source (non-empty and no ``unavailable`` entry)."""
    return bool(snapshot) and not any(str(v).startswith("unavailable") for v in snapshot.values())


def platform_snapshot(store: Any, settings: Settings, views: Iterable[str]) -> dict[str, str]:
    """``{relation: Delta version or content hash}`` for the sources behind ``views``."""
    out: dict[str, str] = {}
    databricks = getattr(platform_of(store), "dialect", None) == "databricks"
    reader = relation_delta_version if databricks else relation_content_hash
    for name, relations in snapshot_sources(settings, views).items():
        if isinstance(relations, str):
            out[name] = relations
            continue
        for relation in relations:
            if relation in out:
                continue
            try:
                out[relation] = reader(store, relation)
            except Exception as exc:
                out[relation] = _unavailable(exc)
    return dict(sorted(out.items()))


_PERMISSION = re.compile(
    r"PERMISSION_DENIED|INSUFFICIENT_PERMISSIONS|INSUFFICIENT_PRIVILEGES|AccessDenied|PermissionDenied|access denied"
    r"|insufficient privileges|does not have (?:the )?(?:required )?(?:\w+ )?(?:privilege|permission)|User does not have",
    re.I)
_MISSING = re.compile(
    r"TABLE_OR_VIEW_NOT_FOUND|SCHEMA_NOT_FOUND|CATALOG_NOT_FOUND|UNRESOLVED_COLUMN|UNRESOLVED_FIELD|UNRESOLVED_ROUTINE"
    r"|Table or view not found|does not exist|cannot be found|Catalog Error|Binder Error", re.I)
_CHECK_FAILED = re.compile(r"quality check '?([\w.\-]+)'? failed", re.I)
_NULL = re.compile(r"returned NULL", re.I)
_ZERO_DENOMINATOR = re.compile(r"denominator is zero", re.I)
_PERMISSION_SQLSTATES = {"42501"}
_MISSING_SQLSTATES = {"42P01", "42704", "42703", "3F000", "42883", "42P06"}


def _call(obj: Any, name: str) -> str:
    fn = getattr(obj, name, None)
    if not callable(fn):
        return ""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return str(fn() or "")
    except Exception:
        return ""


def _error_facts(exc: BaseException) -> tuple[str, str, str]:
    conditions, states, texts = [], [], []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < 5:
        seen.add(id(current))
        conditions += [_call(current, "getCondition") or _call(current, "getErrorClass"),
                       str(getattr(current, "error_code", "") or "")]
        states += [_call(current, "getSqlState"), str(getattr(current, "sql_state", "") or "")]
        texts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    condition = " ".join(c for c in conditions if c and c != "None")
    sqlstate = " ".join(s for s in states if s and s != "None")
    return condition, sqlstate, "\n".join(texts)


def _first_line(exc: BaseException, condition: str) -> str:
    text = str(exc).strip()
    text = text.split("\n\tat ")[0].split("JVM stacktrace")[0]
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    prefix = f"{type(exc).__name__}" + (f"[{condition.split()[0]}]" if condition else "")
    return f"{prefix}: {line}"[:500]


def classify_error(exc: BaseException) -> tuple[ValidationStatus, str, str]:
    """Map a platform evaluation error to (status, reason_code, detail)."""
    condition, sqlstate, text = _error_facts(exc)
    detail = _first_line(exc, condition)
    haystack = f"{condition}\n{text}"
    if (isinstance(exc, PermissionError) or set(sqlstate.split()) & _PERMISSION_SQLSTATES
            or _PERMISSION.search(haystack)):
        return ValidationStatus.MISSING_ACCESS, ReasonCode.PERMISSION_DENIED, detail
    if _CHECK_FAILED.search(text):
        return ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.NO_DATA_FOR_PERIOD, detail
    if _NULL.search(text):
        return ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.NULL_RESULT, detail
    if _ZERO_DENOMINATOR.search(text):
        return ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.NO_DATA_FOR_PERIOD, detail
    if set(sqlstate.split()) & _MISSING_SQLSTATES or _MISSING.search(haystack):
        return ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.PLATFORM_OBJECT_MISSING, detail
    return ValidationStatus.BLOCKED, ReasonCode.EXECUTION_ERROR, detail


class SparkSqlRunner:
    """Runner over a SparkSession (Lakeflow job tasks, notebooks, Databricks Connect)."""

    def __init__(self, spark: Any):
        self.spark = spark
        self.calls = 0
        self._cache: dict[str, float] = {}

    def run(self, sql: str) -> float:
        if sql not in self._cache:
            frame = self.spark.sql(sql)
            rows = frame.take(2)
            self.calls += 1
            data = [list(r.asDict().values()) if hasattr(r, "asDict") else list(r) for r in rows]
            self._cache[sql] = scalar_value(pd.DataFrame(data, columns=list(frame.columns)))
        return self._cache[sql]

    def close(self) -> None:
        self._cache.clear()


class StatementFailed(RuntimeError):
    """A SQL warehouse statement ended in FAILED/CANCELED/CLOSED (carries the service error code)."""

    def __init__(self, message: str, error_code: str | None = None, sql_state: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.sql_state = sql_state


class StatementRunner:
    """Runner over the Databricks SQL Statement Execution API (Databricks App; no Spark session)."""

    _TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}

    def __init__(self, client: Any, warehouse_id: str, *, wait_timeout: str = "30s",
                 timeout_seconds: float = 300.0, poll_seconds: float = 1.0):
        self.client = client
        self.warehouse_id = warehouse_id
        self.wait_timeout = wait_timeout
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.calls = 0
        self._cache: dict[str, float] = {}

    @staticmethod
    def _state(resp: Any) -> str:
        state = getattr(getattr(resp, "status", None), "state", None)
        return str(getattr(state, "value", state) or "")

    def _execute(self, sql: str) -> Any:
        from databricks.sdk.service.sql import Disposition, ExecuteStatementRequestOnWaitTimeout, Format

        api = self.client.statement_execution
        resp = api.execute_statement(statement=sql, warehouse_id=self.warehouse_id, wait_timeout=self.wait_timeout,
                                     on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CONTINUE,
                                     disposition=Disposition.INLINE, format=Format.JSON_ARRAY, row_limit=2)
        deadline = time.monotonic() + self.timeout_seconds
        while self._state(resp) not in self._TERMINAL:
            if time.monotonic() >= deadline:
                api.cancel_execution(resp.statement_id)
                raise TimeoutError(f"statement {resp.statement_id} exceeded {self.timeout_seconds:g}s")
            time.sleep(self.poll_seconds)
            resp = api.get_statement(resp.statement_id)
        return resp

    def run(self, sql: str) -> float:
        if sql in self._cache:
            return self._cache[sql]
        resp = self._execute(sql)
        self.calls += 1
        state = self._state(resp)
        if state != "SUCCEEDED":
            err = getattr(resp.status, "error", None)
            code = getattr(getattr(err, "error_code", None), "value", getattr(err, "error_code", None))
            message = getattr(err, "message", None) or f"statement {state}"
            raise StatementFailed(f"{code or state}: {message}", error_code=str(code) if code else None,
                                  sql_state=getattr(resp.status, "sql_state", None))
        columns = [c.name for c in (resp.manifest.schema.columns or [])]
        rows = (resp.result.data_array if resp.result is not None else None) or []
        self._cache[sql] = scalar_value(pd.DataFrame(rows, columns=columns))
        return self._cache[sql]

    def close(self) -> None:
        self._cache.clear()
