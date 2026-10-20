from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from .contracts import (STATUS_INFO, CoverageState, Document, KpiResult, StatusGroup, ValidationStatus)
from .ids import scope_key as canonical_scope_key
from .ids import utcnow

NATURAL_KEY = ("brand", "kpi_id", "kpi_version", "scope_key", "period_grain", "period_start", "period_end")

HARNESS_COLUMNS = (
    "publication_id", "brand", "kpi_id", "kpi_version", "metric_id", "variant_id", "platform_source",
    "scope_key", "period_label", "period_start", "period_end", "aggregation", "value", "unit",
    "validation_status", "review_decision", "display_label", "platform_value", "variance_rel",
    "tolerance_rel", "display_precision", "evidence_uri", "evidence_hash", "evidence_locator",
    "extraction_method", "sql_artifact_id", "sql_hash", "decision_id", "rationale", "approved_by",
    "approved_at", "publication_seq",
)

COVERAGE_COLUMNS = (
    "brand", "kpi_id", "kpi_label", "goal_scope_key", "cadence", "expected_lag_periods",
    "expected_period_start", "expected_period_end", "expected_period_label", "coverage_state", "cell_label",
    "validation_status", "status_label", "status_group", "published", "published_value", "display_label",
    "value", "platform_value", "unit", "scope_key", "result_id", "doc_id", "file_name",
    "latest_period_found", "has_platform_spec", "owner", "expected_report_types",
)

BOARD_COLUMNS = (
    "result_id", "result_version", "kpi_id", "kpi_label", "label", "period_label", "period_end", "period_grain",
    "scope_key", "report_value", "platform_value", "abs_diff", "rel_diff", "unit", "status", "status_label",
    "status_tooltip", "group", "reason_code", "reason_detail", "page", "extraction_method", "match_basis",
    "decided", "published", "observation_id",
)

_COVERAGE_CELL = {
    CoverageState.NOT_FOUND: "Not found",
    CoverageState.STALE: "Stale",
    CoverageState.NO_ACCESS: "No access",
}


def relation_columns(store, name: str) -> set[str] | None:
    """Column names of a service table/view, or None when it does not exist (or cannot be read)."""
    try:
        df = store.query(f"SELECT * FROM {store.fq(name)} LIMIT 0")
    except Exception:
        return None
    return {str(c) for c in df.columns}


def in_list(prefix: str, values: list[Any]) -> tuple[str, dict[str, Any]]:
    """``(':p0, :p1', {'p0': .., 'p1': ..})`` for an IN (...) filter; callers guarantee non-empty values."""
    params = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return ", ".join(f":{k}" for k in params), params


def to_date(value: Any) -> date | None:
    """Coerce a date-like cell (date, Timestamp, ISO text, NaT/None) to ``datetime.date``."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.date()
    if hasattr(value, "date") and callable(value.date):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def format_value(value: Any, unit: str | None) -> str:
    """Human formatting by unit: fractions as percents, usd with $, counts with separators."""
    if is_missing(value):
        return ""
    v = float(value)
    if unit == "fraction":
        return f"{v * 100:.1f}%"
    if unit == "usd":
        return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"
    if abs(v - round(v)) < 1e-9:
        return f"{v:,.0f}"
    return f"{v:,.1f}" if abs(v) >= 100 else f"{v:,.3g}"


def scope_text(scope_key: str | None) -> str:
    """'Brand total' for '{}', else 'region: West' style text."""
    try:
        scope = json.loads(scope_key or "{}")
    except (TypeError, ValueError):
        return str(scope_key)
    if not scope:
        return "Brand total"
    return ", ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in sorted(scope.items()))


def status_columns(status: Any) -> tuple[str, str, str, int]:
    """(label, tooltip, group, sort) for a validation status value (unknown values sort last)."""
    try:
        info = STATUS_INFO[ValidationStatus(str(status))]
    except (ValueError, KeyError):
        return str(status), "", "", 10_000
    return info.label, info.tooltip, info.group.value, info.sort


def kpi_labels(store, registry=None) -> dict[str, str]:
    """kpi_id -> plain label, from the registry when given, else from the ``kpi_registry`` mirror."""
    if registry is not None:
        return {k: d.label for k, d in registry.kpis.items()}
    try:
        df = store.query(
            f"SELECT kpi_id, label FROM {store.fq('kpi_registry')} "
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY kpi_id ORDER BY kpi_version DESC) = 1")
    except Exception:
        return {}
    return {str(r.kpi_id): str(r.label) for r in df.itertuples()}


def published_current(store, brand: str | None = None) -> pd.DataFrame:
    """Current publications: one row per natural key, not retracted and not superseded."""
    params: dict[str, Any] = {}
    brand_filter = ""
    if brand:
        brand_filter = "brand = :brand"
        params["brand"] = brand
    order = "brand, kpi_id, scope_key, period_end, publication_id"
    if relation_columns(store, "published_kpis_current") is not None:
        where = f" WHERE {brand_filter}" if brand_filter else ""
        return store.query(f"SELECT * FROM {store.fq('published_kpis_current')}{where} ORDER BY {order}", params)
    where = "retracted = FALSE AND superseded_by IS NULL" + (f" AND {brand_filter}" if brand_filter else "")
    partition = ", ".join(NATURAL_KEY)
    sql = (f"SELECT * FROM {store.fq('published_kpis')} WHERE {where} "
           f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY published_at DESC, publication_id DESC) = 1 "
           f"ORDER BY {order}")
    return store.query(sql, params)


def harness_scope_key(scope_key: str | None) -> str:
    """Canonical scope_key in the consumer view format."""
    scope = json.loads(scope_key or "{}")
    scope = {k: (sorted(v) if isinstance(v, (list, tuple)) else v) for k, v in scope.items()}
    if "territory" in scope:
        scope.pop("district", None)
        scope.pop("region", None)
    elif "district" in scope:
        scope.pop("region", None)
    return json.dumps(scope, sort_keys=True)


def _yyyy_mm(value: Any) -> str | None:
    d = to_date(value)
    return f"{d:%Y-%m}" if d else None


def harness_published(store, registry=None, brand: str | None = None) -> pd.DataFrame:
    """Current publications in the consumer view column shape."""
    cur = published_current(store, brand)
    if cur.empty:
        return pd.DataFrame(columns=list(HARNESS_COLUMNS))
    seq = store.query(f"SELECT publication_id, ROW_NUMBER() OVER (ORDER BY published_at, publication_id) AS seq "
                      f"FROM {store.fq('published_kpis')}")
    seq_by_id = {str(r.publication_id): int(r.seq) for r in seq.itertuples()}
    aggregation = {k: d.aggregation for k, d in registry.kpis.items()} if registry is not None else {}
    rows = []
    for r in cur.to_dict(orient="records"):
        tol = _json(r.get("tolerance"))
        half = tol.get("half_unit")
        rows.append({
            "publication_id": r["publication_id"], "brand": r["brand"], "kpi_id": r["kpi_id"],
            "kpi_version": r["kpi_version"], "metric_id": r.get("harness_metric_id"),
            "variant_id": r.get("harness_variant_id"), "platform_source": r.get("harness_source"),
            "scope_key": harness_scope_key(r.get("scope_key")), "period_label": r.get("period_label"),
            "period_start": _yyyy_mm(r.get("period_start")), "period_end": _yyyy_mm(r.get("period_end")),
            "aggregation": aggregation.get(r["kpi_id"]), "value": r.get("value"), "unit": r.get("unit"),
            "validation_status": r.get("validation_status"),
            "review_decision": str(r.get("review_action") or "").lower() or None,
            "display_label": r.get("display_label"), "platform_value": r.get("platform_value"),
            "variance_rel": r.get("variance_rel"), "tolerance_rel": tol.get("rel"),
            "display_precision": (2 * float(half)) if not is_missing(half) else None,
            "evidence_uri": r.get("evidence_uri"), "evidence_hash": r.get("evidence_hash"),
            "evidence_locator": r.get("evidence_locator"), "extraction_method": r.get("extraction_method"),
            "sql_artifact_id": r.get("sql_artifact_id"), "sql_hash": r.get("sql_sha"),
            "decision_id": r.get("decision_id"), "rationale": r.get("rationale"),
            "approved_by": r.get("approved_by"), "approved_at": r.get("approved_at"),
            "publication_seq": seq_by_id.get(str(r["publication_id"])),
        })
    return pd.DataFrame(rows, columns=list(HARNESS_COLUMNS))


def read_published_current(brand: str | None = None, store=None, registry=None) -> pd.DataFrame:
    """Current publications for consumers; opens the configured store when none is given."""
    if store is None:
        from .pipeline import open_store
        from .settings import Settings

        store = open_store(Settings.from_env())
    return harness_published(store, registry, brand)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if is_missing(value) or value == "":
        return {}
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def add_months(d: date, months: int) -> date:
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True)
class ExpectedPeriod:
    start: date
    end: date
    label: str
    grain: str


def expected_periods(as_of: date, cadence: str, lag: int, count: int = 3) -> list[ExpectedPeriod]:
    """The ``count`` most recent periods a report is expected to cover, oldest first."""
    if cadence == "quarterly":
        anchor = month_end(add_months(as_of, -3 * lag))
        if anchor.month % 3:
            anchor = month_end(add_months(anchor, -(anchor.month % 3)))
        ends = [month_end(add_months(anchor, -3 * i)) for i in range(count)]
        out = [ExpectedPeriod(add_months(e, -2).replace(day=1), e, f"Q{(e.month - 1) // 3 + 1} {e.year}", "quarter")
               for e in ends]
    else:
        anchor = month_end(add_months(as_of, -lag))
        ends = [month_end(add_months(anchor, -i)) for i in range(count)]
        out = [ExpectedPeriod(e.replace(day=1), e, f"{calendar.month_name[e.month]} {e.year}", "month") for e in ends]
    return list(reversed(out))


def default_as_of(store, brand: str) -> date:
    """Latest document period_end for the brand, else today (UTC)."""
    df = store.query(f"SELECT MAX(period_end) AS p FROM {store.fq('documents')} "
                     f"WHERE brand = :brand AND period_end IS NOT NULL", {"brand": brand})
    latest = to_date(df.iloc[0, 0]) if not df.empty else None
    return latest or utcnow().date()


def _rank_key(r: KpiResult, goal_scope: str, grain: str, published_keys: set[tuple]) -> tuple:
    scope_rank = 0 if r.scope_key == goal_scope else (1 if r.scope_key == "{}" else 2)
    published = (r.kpi_id, r.scope_key, r.period_grain, r.period_end) in published_keys
    _label, _tip, _group, sort = status_columns(r.validation_status.value)
    created = r.created_at.timestamp() if r.created_at else 0.0
    return (0 if published else 1, scope_rank, 0 if r.period_grain == grain else 1, sort, -created, r.result_id)


def coverage(store, registry, brand: str, as_of: date | None = None) -> pd.DataFrame:
    """One row per goal-list KPI x expected period for ``brand``."""
    as_of = as_of or default_as_of(store, brand)
    goals = registry.goal_kpis(brand)
    kpi_ids = sorted({g["kpi_id"] for g in goals})
    results: list[KpiResult] = []
    files: dict[str, str] = {}
    if kpi_ids:
        marks, params = in_list("k", kpi_ids)
        params["brand"] = brand
        where = (f"is_current = TRUE AND kpi_id IN ({marks}) AND period_end IS NOT NULL "
                 f"AND (brand = :brand OR brand IS NULL)")
        results = store.read(KpiResult, where, params)
        doc_ids = sorted({r.doc_id for r in results})
        docs: dict[str, Document] = {}
        if doc_ids:
            dmarks, dparams = in_list("d", doc_ids)
            docs = {d.doc_id: d for d in store.read(Document, f"doc_id IN ({dmarks})", dparams)}
        files = {d: doc.file_name for d, doc in docs.items()}
        results = [r for r in results if r.brand == brand or (r.brand is None and docs.get(r.doc_id)
                                                               and docs[r.doc_id].brand == brand)]
    pubs = published_current(store, brand)
    pub_by_key: dict[tuple, dict[str, Any]] = {}
    for p in pubs.to_dict(orient="records"):
        pub_by_key[(p["kpi_id"], p["scope_key"], p.get("period_grain"), to_date(p.get("period_end")))] = p
    published_keys = set(pub_by_key)

    rows: list[dict[str, Any]] = []
    for g in goals:
        kpi_id = g["kpi_id"]
        kdef = registry.kpis.get(kpi_id)
        cadence = str(g.get("cadence", "monthly"))
        lag = int(g.get("expected_lag_periods", 1))
        goal_scope = canonical_scope_key(g.get("scope"))
        kres = [r for r in results if r.kpi_id == kpi_id]
        latest = max(kres, key=lambda r: (r.period_end, r.created_at), default=None)
        base = {
            "brand": brand, "kpi_id": kpi_id, "kpi_label": kdef.label if kdef else kpi_id,
            "goal_scope_key": goal_scope, "cadence": cadence, "expected_lag_periods": lag,
            "has_platform_spec": bool(kdef.has_platform_spec) if kdef else False,
            "owner": g.get("owner") or (kdef.owner if kdef else ""),
            "expected_report_types": ", ".join(g.get("expected_report_types", [])),
            "latest_period_found": latest.period_end if latest else None,
        }
        for period in expected_periods(as_of, cadence, lag):
            row = {**base, "expected_period_start": period.start, "expected_period_end": period.end,
                   "expected_period_label": period.label}
            at = [r for r in kres if r.period_end == period.end]
            if at:
                best = min(at, key=lambda r: _rank_key(r, goal_scope, period.grain, published_keys))
                state = (CoverageState.NO_ACCESS if best.validation_status == ValidationStatus.MISSING_ACCESS
                         else CoverageState.FOUND)
                pub = pub_by_key.get((best.kpi_id, best.scope_key, best.period_grain, best.period_end))
                row.update(_result_cells(best, pub, files))
            else:
                if latest is not None and latest.validation_status == ValidationStatus.MISSING_ACCESS:
                    state = CoverageState.NO_ACCESS
                elif any(r.period_end < period.end for r in kres):
                    state = CoverageState.STALE
                else:
                    state = CoverageState.NOT_FOUND
                row.update(_result_cells(None, None, files))
            row["coverage_state"] = state.value
            row["cell_label"] = _cell_label(state, row)
            rows.append(row)
    return pd.DataFrame(rows, columns=list(COVERAGE_COLUMNS))


def _result_cells(r: KpiResult | None, pub: dict[str, Any] | None, files: dict[str, str]) -> dict[str, Any]:
    if r is None:
        return {"validation_status": None, "status_label": None, "status_group": None, "published": False,
                "published_value": None, "display_label": None, "value": None, "platform_value": None,
                "unit": None, "scope_key": None, "result_id": None, "doc_id": None, "file_name": None}
    label, _tip, group, _sort = status_columns(r.validation_status.value)
    return {"validation_status": r.validation_status.value, "status_label": label, "status_group": group,
            "published": pub is not None, "published_value": pub.get("value") if pub else None,
            "display_label": pub.get("display_label") if pub else None, "value": r.report_value,
            "platform_value": r.platform_value, "unit": r.unit, "scope_key": r.scope_key,
            "result_id": r.result_id, "doc_id": r.doc_id, "file_name": files.get(r.doc_id)}


def _cell_label(state: CoverageState, row: dict[str, Any]) -> str:
    if state != CoverageState.FOUND:
        return _COVERAGE_CELL.get(state, state.value)
    if row.get("published") and row.get("display_label"):
        return str(row["display_label"])
    if row.get("status_group") == StatusGroup.REVIEW.value:
        return "Needs review"
    return str(row.get("status_label") or "")


def document_board(store, doc_id: str, registry=None) -> pd.DataFrame:
    """Every current result of one document with plain status labels, groups and decision flags."""
    t = store.fq
    sql = f"""
        SELECT r.result_id, r.result_version, r.kpi_id, o.label, o.period_label, r.period_end, r.period_grain,
               r.scope_key, r.report_value, r.platform_value, r.abs_diff, r.rel_diff, r.unit,
               r.validation_status AS status, r.reason_code, r.reason_detail, v.slide_or_page AS page,
               r.extraction_method, r.match_basis, r.observation_id,
               dd.n_decisions, pp.n_publications
        FROM {t('kpi_results')} r
        LEFT JOIN {t('observations')} o ON o.observation_id = r.observation_id
        LEFT JOIN {t('extracted_values')} v ON v.value_id = o.value_id
        LEFT JOIN (SELECT result_id, result_version, COUNT(*) AS n_decisions FROM {t('review_decisions')}
                   GROUP BY result_id, result_version) dd
               ON dd.result_id = r.result_id AND dd.result_version = r.result_version
        LEFT JOIN (SELECT result_id, COUNT(*) AS n_publications FROM {t('published_kpis')}
                   WHERE retracted = FALSE AND superseded_by IS NULL GROUP BY result_id) pp
               ON pp.result_id = r.result_id
        WHERE r.doc_id = :doc_id AND r.is_current = TRUE
    """
    df = store.query(sql, {"doc_id": doc_id})
    if df.empty:
        return pd.DataFrame(columns=list(BOARD_COLUMNS))
    labels = kpi_labels(store, registry)
    info = [status_columns(s) for s in df["status"]]
    df["status_label"] = [i[0] for i in info]
    df["status_tooltip"] = [i[1] for i in info]
    df["group"] = [i[2] for i in info]
    df["_sort"] = [i[3] for i in info]
    df["kpi_label"] = [labels.get(k, k) if not is_missing(k) else None for k in df["kpi_id"]]
    df["decided"] = [not is_missing(n) and int(n) > 0 for n in df["n_decisions"]]
    df["published"] = [not is_missing(n) and int(n) > 0 for n in df["n_publications"]]
    df = df.sort_values(["_sort", "page", "result_id"], na_position="last", kind="stable")
    return df.reset_index(drop=True)[list(BOARD_COLUMNS)]


def status_counts(store, doc_id: str | None = None, brand: str | None = None) -> dict[str, int]:
    """Current results per status group: ``{'GOOD': n, 'REVIEW': n, 'BLOCKED': n}`` (zeros included)."""
    where = ["r.is_current = TRUE"]
    params: dict[str, Any] = {}
    if doc_id:
        where.append("r.doc_id = :doc_id")
        params["doc_id"] = doc_id
    if brand:
        where.append("COALESCE(r.brand, d.brand) = :brand")
        params["brand"] = brand
    df = store.query(
        f"SELECT r.validation_status AS status, COUNT(*) AS n FROM {store.fq('kpi_results')} r "
        f"LEFT JOIN {store.fq('documents')} d ON d.doc_id = r.doc_id WHERE {' AND '.join(where)} "
        f"GROUP BY r.validation_status", params)
    counts = {g.value: 0 for g in StatusGroup}
    for row in df.itertuples():
        group = status_columns(row.status)[2]
        if group:
            counts[group] += int(row.n)
    return counts
