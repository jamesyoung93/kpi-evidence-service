from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..contracts import STATUS_INFO, StatusGroup, ValidationStatus
from ..ids import canonical_json, scope_key, sha256_text

AUTO_POLICY_PREFIX = "system:auto-policy"
UNMATCHED = "UNMATCHED"
_MISSING = 1e18
_METRIC_KEY = re.compile(r"[^A-Za-z0-9_\-./ ]+")


@dataclass
class LabelSet:
    labels: list[dict[str, Any]]
    documents: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    dataset: str = "labels"
    raw: Any = None

    @property
    def sha256(self) -> str:
        return sha256_text(canonical_json(self.raw if self.raw is not None else self.labels))

    @property
    def files(self) -> list[str]:
        names = [d["file"] for d in self.documents] or [lab["file"] for lab in self.labels]
        return list(dict.fromkeys(names))

    def document(self, file: str) -> dict[str, Any] | None:
        return next((d for d in self.documents if d["file"] == file), None)


def load_labels(source: str | Path | dict | list | LabelSet | None = None) -> LabelSet:
    """Load ``labels.json`` (object with labels/documents/coverage, or a bare list of labels)."""
    if isinstance(source, LabelSet):
        return source
    if source is None:
        from ..settings import demo_dir

        source = demo_dir("reports") / "labels.json"
    raw = json.loads(Path(source).read_text(encoding="utf-8")) if isinstance(source, (str, Path)) else source
    if isinstance(raw, list):
        return LabelSet(labels=[_normalize_label(x) for x in raw], raw=raw)
    return LabelSet(labels=[_normalize_label(x) for x in raw.get("labels", [])],
                    documents=list(raw.get("documents", [])), coverage=dict(raw.get("coverage", {})),
                    dataset=str(raw.get("dataset", "labels")), raw=raw)


def _normalize_label(label: dict[str, Any]) -> dict[str, Any]:
    out = dict(label)
    out.setdefault("scope", {})
    out["scope_key"] = out.get("scope_key") or scope_key(out["scope"])
    out.setdefault("display_half_unit", 0.0)
    out.setdefault("expected_reason", "")
    out.setdefault("also_acceptable", [])
    out.setdefault("page", None)
    out.setdefault("period_grain", None)
    out["period_end"] = _iso(out.get("period_end"))
    out.setdefault("label_id", sha256_text(canonical_json([out.get("file"), out.get("page"), out.get("locator_hint"),
                                                           out.get("kpi_id"), out["period_end"], out["scope"]]))[:16])
    return out


RESULT_COLUMNS = ["result_id", "doc_id", "file_name", "doc_status", "kpi_id", "period_end", "period_grain",
                  "scope_key", "report_value", "validation_status", "reason_code", "is_current", "brand", "page",
                  "extraction_method"]


def _iso(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.date().isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if pd.isna(value):
        return None
    return str(value)[:10]


def _none(value: Any) -> Any:
    if value is None:
        return None
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):
        return value


def results_frame(store: Any, files: list[str] | None = None) -> pd.DataFrame:
    """Every kpi_results row joined to its document file name and source page (slide/page number)."""
    from ..contracts import Document, ExtractedValue, KpiResult, Observation

    results = store.read_df(KpiResult)
    if results.empty:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    docs = store.read_df(Document, columns_sql="doc_id, file_name, status AS doc_status")
    obs = store.read_df(Observation, columns_sql="observation_id, value_id")
    vals = store.read_df(ExtractedValue, columns_sql="value_id, slide_or_page")
    frame = results.merge(docs, on="doc_id", how="left").merge(obs, on="observation_id", how="left")
    frame = frame.merge(vals, on="value_id", how="left").rename(columns={"slide_or_page": "page"})
    if files is not None:
        frame = frame[frame["file_name"].isin(files)]
    frame = frame.assign(period_end=frame["period_end"].map(_iso))
    for col in ("kpi_id", "report_value", "page", "brand", "period_grain", "extraction_method"):
        frame[col] = frame[col].map(_none).astype(object)
    frame["is_current"] = frame["is_current"].map(lambda v: bool(_none(v)))
    return frame[RESULT_COLUMNS].reset_index(drop=True)


@dataclass
class Match:
    label_id: str
    file: str
    page: int | None
    kpi_id: str | None
    expected_status: str
    expected_reason: str
    label_value: float | None
    result_id: str | None = None
    tier: str | None = None
    actual_status: str | None = None
    actual_reason: str | None = None
    actual_kpi_id: str | None = None
    actual_value: float | None = None
    value_found: bool = False
    kpi_ok: bool = False
    status_ok: bool = False
    status_ok_lenient: bool = False
    reason_ok: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tolerance(label: dict[str, Any]) -> float:
    return max(float(label.get("display_half_unit") or 0.0), 1e-9)


def _distance(label_value: float | None, result_value: Any) -> float:
    rv = _none(result_value)
    if label_value is None:
        return 0.0 if rv is None else _MISSING
    if rv is None:
        return _MISSING
    return abs(float(rv) - float(label_value))


def _same_page(label: dict[str, Any], row: dict[str, Any]) -> bool:
    lp, rp = label.get("page"), _none(row.get("page"))
    return lp is None or rp is None or int(lp) == int(rp)


def _strict(label: dict[str, Any], row: dict[str, Any]) -> bool:
    return (row["file_name"] == label["file"] and _none(row["kpi_id"]) == label.get("kpi_id")
            and row["scope_key"] == label["scope_key"]
            and (label.get("period_end") is None or row["period_end"] == label["period_end"])
            and (label.get("report_value") is None) == (_none(row["report_value"]) is None))


def match_labels(labels: list[dict[str, Any]], results: pd.DataFrame) -> list[Match]:
    """Assign at most one result to each label (strict tier first, then the page/value fallback)."""
    rows = results.to_dict(orient="records")
    matches = [_blank_match(lab) for lab in labels]
    used: set[int] = set()
    assigned: set[int] = set()

    pairs = []
    for li, lab in enumerate(labels):
        for ri, row in enumerate(rows):
            if _strict(lab, row):
                pairs.append((_distance(lab.get("report_value"), row["report_value"]), not _same_page(lab, row),
                              row.get("period_grain") != lab.get("period_grain"), not row["is_current"], li, ri))
    for *_rank, li, ri in sorted(pairs, key=lambda p: (*p[:5], str(rows[p[5]]["result_id"]))):
        if li not in assigned and ri not in used:
            assigned.add(li)
            used.add(ri)
            _fill(matches[li], labels[li], rows[ri], "strict")

    pairs = []
    for li, lab in enumerate(labels):
        if li in assigned:
            continue
        for ri, row in enumerate(rows):
            if ri in used or row["file_name"] != lab["file"] or not _same_page(lab, row):
                continue
            d = _distance(lab.get("report_value"), row["report_value"])
            if d <= _tolerance(lab):
                pairs.append((d, not row["is_current"], li, ri))
    for _d, _c, li, ri in sorted(pairs, key=lambda p: (p[0], p[1], p[2], str(rows[p[3]]["result_id"]))):
        if li not in assigned and ri not in used:
            assigned.add(li)
            used.add(ri)
            _fill(matches[li], labels[li], rows[ri], "fallback")
    return matches


def _blank_match(label: dict[str, Any]) -> Match:
    value = label.get("report_value")
    return Match(label_id=label["label_id"], file=label["file"], page=label.get("page"), kpi_id=label.get("kpi_id"),
                 expected_status=label["expected_status"], expected_reason=label.get("expected_reason") or "",
                 label_value=None if value is None else float(value))


def _fill(m: Match, label: dict[str, Any], row: dict[str, Any], tier: str) -> None:
    m.result_id, m.tier = row["result_id"], tier
    m.actual_status = str(row["validation_status"])
    m.actual_reason = str(_none(row["reason_code"]) or "")
    m.actual_kpi_id = _none(row["kpi_id"])
    rv = _none(row["report_value"])
    m.actual_value = None if rv is None else float(rv)
    if m.label_value is None:
        m.value_found = m.actual_value is None
    else:
        m.value_found = m.actual_value is not None and abs(m.actual_value - m.label_value) <= _tolerance(label)
    m.kpi_ok = m.actual_kpi_id == m.kpi_id
    m.status_ok = m.actual_status == m.expected_status
    m.status_ok_lenient = m.status_ok or m.actual_status in (label.get("also_acceptable") or [])
    m.reason_ok = m.actual_reason.startswith(m.expected_reason)


def _share(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _group(status: str) -> str | None:
    try:
        return STATUS_INFO[ValidationStatus(status)].group.value
    except (ValueError, KeyError):
        return None


def _label_scores(matches: list[Match]) -> dict[str, Any]:
    n = len(matches)
    not_verified = [m for m in matches if m.expected_status != ValidationStatus.VERIFIED.value]
    expected_verified = [m for m in matches if m.expected_status == ValidationStatus.VERIFIED.value]
    false_val = [m for m in not_verified if m.actual_status == ValidationStatus.VERIFIED.value]
    return {
        "labels": n,
        "matched": sum(m.tier is not None for m in matches),
        "matched_strict": sum(m.tier == "strict" for m in matches),
        "match_rate": _share(sum(m.tier is not None for m in matches), n),
        "extraction_accuracy": _share(sum(m.value_found for m in matches), n),
        "mapping_accuracy": _share(sum(m.tier is not None and m.kpi_ok for m in matches), n),
        "status_accuracy": _share(sum(m.status_ok for m in matches), n),
        "status_accuracy_lenient": _share(sum(m.status_ok_lenient for m in matches), n),
        "reason_accuracy": _share(sum(m.status_ok and m.reason_ok for m in matches), n),
        "false_validation_rate": _share(len(false_val), len(not_verified)),
        "false_validations": len(false_val),
        "verified_recall": _share(sum(m.actual_status == "VERIFIED" for m in expected_verified), len(expected_verified)),
    }


def confusion(matches: list[Match]) -> dict[str, dict[str, int]]:
    """{expected_status: {actual_status or UNMATCHED: count}} with sorted keys."""
    table: dict[str, Counter] = {}
    for m in matches:
        table.setdefault(m.expected_status, Counter())[m.actual_status or UNMATCHED] += 1
    return {exp: dict(sorted(c.items())) for exp, c in sorted(table.items())}


def confusion_frame(metrics_or_table: dict[str, Any]) -> pd.DataFrame:
    """Expected x actual counts as a DataFrame (rows expected, columns actual)."""
    table = metrics_or_table.get("confusion", metrics_or_table)
    return pd.DataFrame(table).T.fillna(0).astype(int).sort_index().sort_index(axis=1)


def _workload(current: pd.DataFrame) -> dict[str, Any]:
    groups = current["validation_status"].map(lambda s: _group(str(s))) if not current.empty else pd.Series(dtype=object)
    n = len(current)
    review = int((groups == StatusGroup.REVIEW.value).sum())
    blocked = int((groups == StatusGroup.BLOCKED.value).sum())
    return {"current_results": n, "review_count": review, "review_share": _share(review, n),
            "blocked_count": blocked, "blocked_share": _share(blocked, n),
            "good_count": int((groups == StatusGroup.GOOD.value).sum())}


def _auto_publish(store: Any, current: pd.DataFrame) -> dict[str, Any]:
    from ..contracts import Publication

    n = len(current)
    if n == 0:
        return {"auto_published": 0, "auto_publish_rate": None, "published": 0}
    pubs = store.read_df(Publication, columns_sql="result_id, approved_by, retracted")
    pubs = pubs[pubs["result_id"].isin(set(current["result_id"]))]
    if "retracted" in pubs:
        pubs = pubs[~pubs["retracted"].fillna(False).astype(bool)]
    auto = pubs[pubs["approved_by"].astype(str).str.startswith(AUTO_POLICY_PREFIX)]
    return {"auto_published": int(auto["result_id"].nunique()), "auto_publish_rate": _share(int(auto["result_id"].nunique()), n),
            "published": int(pubs["result_id"].nunique())}


def _per_document(labelset: LabelSet, matches: list[Match], results: pd.DataFrame) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for file in labelset.files:
        ms = [m for m in matches if m.file == file]
        rows = results[results["file_name"] == file]
        statuses = rows["doc_status"].dropna().astype(str).unique().tolist()
        scores = _label_scores(ms)
        out[file] = {
            "labels": len(ms), "matched": scores["matched"],
            "extraction_accuracy": scores["extraction_accuracy"], "mapping_accuracy": scores["mapping_accuracy"],
            "status_accuracy": scores["status_accuracy"], "false_validations": scores["false_validations"],
            "results": int(len(rows)), "current_results": int(rows["is_current"].sum()) if len(rows) else 0,
            "document_status": statuses[0] if len(statuses) == 1 else (statuses or None),
        }
    return out


def _document_statuses(store: Any, files: list[str]) -> dict[str, str]:
    from ..contracts import Document

    docs = store.read_df(Document, columns_sql="file_name, status, updated_at")
    docs = docs[docs["file_name"].isin(files)].sort_values("updated_at")
    return {str(r.file_name): str(r.status) for r in docs.itertuples()}


def goal_list_coverage(store: Any, labelset: LabelSet | None = None, *, coverage: pd.DataFrame | None = None,
                       registry: Any = None, brand: str | None = None, as_of: date | str | None = None,
                       results: pd.DataFrame | None = None) -> dict[str, Any]:
    """Share of goal-list KPIs FOUND for the brand's latest expected period."""
    expected = (labelset.coverage.get("expected") if labelset else None) or {}
    brand = brand or (labelset.coverage.get("brand") if labelset else None)
    as_of = as_of or (labelset.coverage.get("as_of") if labelset else None)
    if coverage is not None:
        states, source = _states_from_frame(coverage, brand), "provided"
    else:
        states, source = _fallback_states(store, labelset, registry=registry, brand=brand, as_of=as_of,
                                          results=results), "fallback"
    total = len(states)
    found = sum(1 for s in states.values() if s == "FOUND")
    agree = [k for k in expected if states.get(k) == expected[k]]
    return {"share_found": _share(found, total), "found": found, "goal_kpis": total, "states": dict(sorted(states.items())),
            "source": source, "as_of": _iso(as_of), "brand": brand,
            "expected_agreement": _share(len(agree), len(expected)) if expected else None}


def _states_from_frame(coverage: pd.DataFrame, brand: str | None) -> dict[str, str]:
    frame = coverage.copy()
    state_col = next((c for c in ("coverage_state", "state", "status") if c in frame.columns), None)
    if state_col is None or "kpi_id" not in frame.columns:
        raise ValueError("coverage frame needs kpi_id and coverage_state (or state) columns")
    brand_col = next((c for c in ("brand", "brand_id") if c in frame.columns), None)
    if brand and brand_col:
        frame = frame[frame[brand_col] == brand]
    period_col = next((c for c in ("expected_period_end", "period_end") if c in frame.columns), None)
    if period_col:
        frame = frame.assign(_pe=frame[period_col].map(_iso).fillna("")).sort_values(["kpi_id", "_pe"], kind="stable")
    latest = frame.groupby("kpi_id", sort=True).tail(1)
    return {str(r["kpi_id"]): str(r[state_col]) for r in latest.to_dict(orient="records")}


def _month_end(year: int, month: int) -> date:
    import calendar

    return date(year, month, calendar.monthrange(year, month)[1])


def _shift_months(d: date, months: int) -> date:
    index = d.year * 12 + d.month - 1 + months
    return _month_end(index // 12, index % 12 + 1)


def expected_period_end(as_of: date, cadence: str, lag: int) -> date:
    """The latest period a report is expected to cover at ``as_of`` (``lag`` in cadence periods)."""
    if cadence == "quarterly":
        anchor = _shift_months(as_of, -3 * lag)
        return _shift_months(anchor, -(anchor.month % 3)) if anchor.month % 3 else anchor
    return _shift_months(as_of, -lag)


def _friendliness(status: str) -> int:
    try:
        return STATUS_INFO[ValidationStatus(status)].sort
    except (ValueError, KeyError):
        return 10_000


def _fallback_states(store: Any, labelset: LabelSet | None, *, registry: Any, brand: str | None,
                     as_of: date | str | None, results: pd.DataFrame | None) -> dict[str, str]:
    if registry is None:
        from ..registry import load_registry

        registry = load_registry(store.settings.resolved_registry_dir())
    if brand is None:
        brand = next(iter(registry.brands), None)
    frame = results if results is not None else results_frame(store, labelset.files if labelset else None)
    current = frame[frame["is_current"] & (frame["brand"] == brand) & frame["period_end"].notna()] if len(frame) else frame
    if as_of is None:
        from ..ids import utcnow

        as_of = utcnow().date()
    as_of_d = date.fromisoformat(_iso(as_of))
    states: dict[str, str] = {}
    for goal in registry.goal_kpis(brand):
        rows = current[current["kpi_id"] == goal["kpi_id"]] if len(current) else current
        end = expected_period_end(as_of_d, goal.get("cadence", "monthly"), int(goal.get("expected_lag_periods", 1)))
        at = rows[rows["period_end"] == end.isoformat()] if len(rows) else rows
        missing = ValidationStatus.MISSING_ACCESS.value
        if len(at):
            best = min(at["validation_status"].astype(str), key=_friendliness)
            states[goal["kpi_id"]] = "NO_ACCESS" if best == missing else "FOUND"
        elif len(rows) and str(rows.sort_values("period_end")["validation_status"].iloc[-1]) == missing:
            states[goal["kpi_id"]] = "NO_ACCESS"
        elif len(rows) and (rows["period_end"] < end.isoformat()).any():
            states[goal["kpi_id"]] = "STALE"
        else:
            states[goal["kpi_id"]] = "NOT_FOUND"
    return states


def compute_metrics(store: Any, labels: LabelSet | list | dict | str | Path | None = None, *,
                    coverage: pd.DataFrame | None = None, registry: Any = None, brand: str | None = None,
                    as_of: date | str | None = None, include_matches: bool = True) -> dict[str, Any]:
    """Score the store's results against the labels."""
    labelset = load_labels(labels)
    results = results_frame(store, labelset.files)
    matches = match_labels(labelset.labels, results)
    current = results[results["is_current"]] if len(results) else results
    metrics: dict[str, Any] = _label_scores(matches)
    metrics["reviewer_workload"] = _workload(current)
    metrics.update(_auto_publish(store, current))
    metrics["goal_list_coverage"] = goal_list_coverage(store, labelset, coverage=coverage, registry=registry,
                                                       brand=brand, as_of=as_of, results=results)
    metrics["per_document"] = _per_document(labelset, matches, results)
    doc_status = _document_statuses(store, labelset.files)
    for file, row in metrics["per_document"].items():
        row["document_status"] = doc_status.get(file, row["document_status"])
        expected_doc = labelset.document(file)
        row["document_status_ok"] = (expected_doc is not None and doc_status.get(file) == expected_doc.get("final_status"))
    metrics["confusion"] = confusion(matches)
    metrics["result_status_counts"] = dict(sorted(Counter(current["validation_status"].astype(str)).items())) if len(current) else {}
    if include_matches:
        metrics["matches"] = [m.as_dict() for m in matches]
    return metrics


def metric_rows(metrics: dict[str, Any]) -> dict[str, float]:
    """Flatten the numeric metrics for ``mlflow.log_metrics`` (keys sanitized, None/NaN dropped)."""
    flat: dict[str, float] = {}

    def put(key: str, value: Any) -> None:
        if isinstance(value, bool):
            value = float(value)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            flat[_METRIC_KEY.sub("_", key)] = float(value)

    for key, value in metrics.items():
        if key in ("matches", "confusion", "per_document", "result_status_counts"):
            continue
        if isinstance(value, dict):
            for sub, v in value.items():
                if not isinstance(v, dict):
                    put(f"{key}.{sub}", v)
        else:
            put(key, value)
    for file, row in (metrics.get("per_document") or {}).items():
        for sub in ("extraction_accuracy", "mapping_accuracy", "status_accuracy", "false_validations", "labels"):
            put(f"doc.{file}.{sub}", row.get(sub))
    for status, n in (metrics.get("result_status_counts") or {}).items():
        put(f"results.{status}", n)
    return dict(sorted(flat.items()))
