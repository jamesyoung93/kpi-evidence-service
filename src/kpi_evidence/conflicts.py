from __future__ import annotations

from collections import defaultdict
from typing import Any

from .contracts import (
    DecisionSource, Document, DocumentStatus, KpiResult, ReasonCode, ReviewDecision, ReviewEvent, ValidationStatus,
)
from .ids import stable_id, utcnow
from .reconcile import CONFLICT_KEY, fmt_value, normalize_unit
from .registry import KpiDefinition, Registry
from .settings import Settings
from .store.base import Store

ACTOR = "system:conflicts@v1"

_NOT_COMPARABLE_STATUS = {ValidationStatus.UNCERTAIN_EXTRACTION, ValidationStatus.AMBIGUOUS_MAPPING,
                          ValidationStatus.UNMAPPED_CAPTURED}
_NOT_COMPARABLE_REASON = {ReasonCode.UNIT_MISMATCH, ReasonCode.SCALE_MISMATCH}


def _in_clause(prefix: str, values: list[str]) -> tuple[str, dict[str, Any]]:
    marks = ", ".join(f":{prefix}{i}" for i in range(len(values))) or "NULL"
    return f"({marks})", {f"{prefix}{i}": v for i, v in enumerate(values)}


_CANDIDATES = "is_current = TRUE AND kpi_id IS NOT NULL AND period_end IS NOT NULL AND report_value IS NOT NULL"


def _candidates(store: Store, doc_ids: list[str] | None) -> list[KpiResult]:
    if not doc_ids:
        return store.read(KpiResult, _CANDIDATES)
    clause, params = _in_clause("d", sorted(set(doc_ids)))
    own = store.read(KpiResult, f"{_CANDIDATES} AND doc_id IN {clause}", params)
    keys = {(r.kpi_id, r.period_end) for r in own}
    if not keys:
        return []
    kpis, kpi_params = _in_clause("k", sorted({k for k, _end in keys}))
    ends, end_params = _in_clause("e", sorted({end for _k, end in keys}))
    rows = store.read(KpiResult, f"{_CANDIDATES} AND kpi_id IN {kpis} AND period_end IN {ends}",
                      {**kpi_params, **end_params})
    return [r for r in rows if (r.kpi_id, r.period_end) in keys]


def _documents(store: Store, doc_ids: set[str]) -> dict[str, Document]:
    ids = sorted(doc_ids)
    out: dict[str, Document] = {}
    for i in range(0, len(ids), 200):
        clause, params = _in_clause("d", ids[i:i + 200])
        out.update({d.doc_id: d for d in store.read(Document, f"doc_id IN {clause}", params)})
    return out


def _human_decided(store: Store, results: list[KpiResult]) -> set[tuple[str, int]]:
    ids = sorted({r.result_id for r in results})
    out: set[tuple[str, int]] = set()
    for i in range(0, len(ids), 200):
        clause, params = _in_clause("r", ids[i:i + 200])
        params["src"] = DecisionSource.HUMAN.value
        for d in store.read(ReviewDecision, f"result_id IN {clause} AND decision_source = :src", params):
            out.add((d.result_id, d.result_version))
    return out


def _comparable(r: KpiResult) -> bool:
    if r.validation_status in _NOT_COMPARABLE_STATUS:
        return False
    previous = ((r.explanation or {}).get(CONFLICT_KEY) or {}).get("previous") or {}
    reason = previous.get("reason_code") if r.validation_status == ValidationStatus.REPORT_CONFLICT else r.reason_code
    return reason not in _NOT_COMPARABLE_REASON


def _half(r: KpiResult) -> float:
    try:
        return float((r.tolerance or {}).get("half_unit") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def values_conflict(a: KpiResult, b: KpiResult, kpi: KpiDefinition | None, *, use_rounding: bool = True) -> bool:
    """True when the two report values differ by more than both display half-units and the KPI tolerance."""
    if normalize_unit(a.unit) != normalize_unit(b.unit):
        return False
    gap = abs(a.report_value - b.report_value)
    rounding = (_half(a) + _half(b)) if use_rounding else 0.0
    tolerance = 0.0
    if kpi is not None:
        tolerance = max(kpi.tolerance.rel * max(abs(a.report_value), abs(b.report_value)), kpi.tolerance.abs)
    return gap > max(rounding, tolerance) + 1e-12 * max(1.0, abs(a.report_value), abs(b.report_value))


def _family(doc: Document) -> str:
    return doc.report_family or doc.doc_id


def _other_info(other: KpiResult, docs: dict[str, Document]) -> dict[str, Any]:
    return {"result_id": other.result_id, "doc_id": other.doc_id, "file_name": docs[other.doc_id].file_name,
            "report_value": other.report_value}


def _detail(result: KpiResult, others: list[dict[str, Any]]) -> str:
    first = others[0]
    more = f" {len(others) - 1} more report(s) also disagree." if len(others) > 1 else ""
    return (f"{first['file_name']} gives {fmt_value(first['report_value'], result.unit)} for the same KPI, period "
            f"and scope; this report gives {fmt_value(result.report_value, result.unit)}.{more}")


def _flag(result: KpiResult, others: list[dict[str, Any]]) -> bool:
    explanation = dict(result.explanation or {})
    block = dict(explanation.get(CONFLICT_KEY) or {})
    if result.validation_status == ValidationStatus.REPORT_CONFLICT:
        if block.get("others") == others:
            return False
        block["others"] = others
    else:
        block = {"previous": {"validation_status": result.validation_status.value,
                              "reason_code": result.reason_code, "reason_detail": result.reason_detail},
                 "others": others}
        result.validation_status = ValidationStatus.REPORT_CONFLICT
        result.reason_code = ReasonCode.CONFLICTING_REPORTS
        result.result_version += 1
    explanation[CONFLICT_KEY] = block
    result.explanation = explanation
    result.reason_detail = _detail(result, others)
    return True


def _restore(result: KpiResult) -> bool:
    previous = ((result.explanation or {}).get(CONFLICT_KEY) or {}).get("previous")
    if not previous or not previous.get("validation_status"):
        return False
    result.validation_status = ValidationStatus(previous["validation_status"])
    result.reason_code = previous.get("reason_code") or ""
    result.reason_detail = previous.get("reason_detail") or ""
    result.explanation = {k: v for k, v in (result.explanation or {}).items() if k != CONFLICT_KEY}
    result.result_version += 1
    return True


def _event(result: KpiResult, others: list[dict[str, Any]], now) -> ReviewEvent:
    return ReviewEvent(
        event_id=stable_id("conflict_detected", result.result_id, result.result_version,
                           sorted(o["result_id"] for o in others)),
        result_id=result.result_id, doc_id=result.doc_id, event="conflict_detected", actor=ACTOR, event_at=now,
        detail={"kpi_id": result.kpi_id, "period_end": result.period_end, "others": others})


def detect_conflicts(store: Store, registry: Registry, settings: Settings,
                     doc_ids: list[str] | None = None) -> list[KpiResult]:
    """Flag conflicting current results; returns the checked results that are REPORT_CONFLICT afterwards."""
    candidates = _candidates(store, doc_ids)
    docs = _documents(store, {r.doc_id for r in candidates})
    live = [r for r in candidates if r.doc_id in docs and docs[r.doc_id].status != DocumentStatus.SUPERSEDED
            and _comparable(r)]
    groups: dict[tuple, list[KpiResult]] = defaultdict(list)
    for r in live:
        groups[(r.brand, r.kpi_id, r.scope_key, r.period_grain, r.period_end)].append(r)
    focus = set(doc_ids) if doc_ids else None
    use_rounding = bool(((registry.policy or {}).get("conflicts") or {}).get("compare_display_rounding", True))
    decided = _human_decided(store, live)
    now = utcnow()
    changed: list[KpiResult] = []
    events: list[ReviewEvent] = []
    flagged: list[KpiResult] = []
    for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if focus is not None and not any(m.doc_id in focus for m in members):
            continue
        kpi = registry.kpis.get(key[1])
        for m in members:
            others = sorted((_other_info(o, docs) for o in members
                             if o.doc_id != m.doc_id and _family(docs[o.doc_id]) != _family(docs[m.doc_id])
                             and values_conflict(m, o, kpi, use_rounding=use_rounding)),
                            key=lambda o: (o["file_name"], o["result_id"]))
            if (m.result_id, m.result_version) in decided:
                if others:
                    events.append(_event(m, others, now))
                continue
            if others:
                if _flag(m, others):
                    changed.append(m)
                flagged.append(m)
            elif m.validation_status == ValidationStatus.REPORT_CONFLICT and _restore(m):
                changed.append(m)
    if changed:
        store.upsert(changed)
    if events:
        store.insert_new(events)
    return flagged
