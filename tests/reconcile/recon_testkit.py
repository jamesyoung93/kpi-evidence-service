from __future__ import annotations

from datetime import date, datetime

from kpi_evidence.contracts import (
    Document, DocumentFormat, DocumentStatus, ExtractedValue, ExtractionMethod, KpiMapping, MappingMethod,
    MappingStatus, Observation,
)
from kpi_evidence.ids import scope_key, sha256_text, stable_id

BRAND = "KESTREL"
NOW = datetime(2026, 10, 19, 12, 0, 0)
JUNE = date(2026, 6, 30)
MONTH_ENDS = [date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30),
              date(2026, 5, 31)]


def make_doc(store, name: str = "Kestrel_Monthly_Brand_Review_2026-06.pptx", *, family: str | None = "fam-monthly",
             version: int = 1, status: DocumentStatus = DocumentStatus.READY, brand: str | None = BRAND) -> Document:
    doc = Document(doc_id=sha256_text(name), file_name=name, file_path=f"/volume/inbox/{name}", size_bytes=1000,
                   format=DocumentFormat.PPTX if name.endswith(".pptx") else DocumentFormat.PDF, status=status,
                   registered_at=NOW, updated_at=NOW, brand=brand, report_type="monthly_brand_review",
                   period_end=date(2026, 5, 31), version=version, report_family=family)
    store.upsert([doc])
    return doc


def _period_start(end: date, grain: str) -> date:
    if grain == "quarter":
        month = end.month - 2
        return date(end.year, month, 1)
    return end.replace(day=1)


def make_obs(doc: Document, label: str, value: float | None, *, period_end: date | None = JUNE,
             grain: str | None = "month", unit: str | None = "count", half: float = 0.0, scope: dict | None = None,
             stated: str = "", confidence: float = 1.0, flags: list | None = None, key: str = "") -> Observation:
    value_id = stable_id(doc.doc_id, "test", label, key, period_end, grain, scope_key(scope))
    return Observation(
        observation_id=stable_id(value_id, "test-v1"), value_id=value_id, doc_id=doc.doc_id,
        interpretation_version="test-v1", label=label, label_context=label, value=value, unit=unit,
        display_half_unit=half, period_start=_period_start(period_end, grain or "month") if period_end else None,
        period_end=period_end, period_grain=grain, period_label=str(period_end), period_source="category",
        period_confidence=1.0, scope=scope or {}, scope_key=scope_key(scope), brand=doc.brand,
        fingerprint=sha256_text(label), stated_rule=stated, flags=flags or [], confidence=confidence,
        created_at=NOW)


def store_value(store, obs: Observation, method: ExtractionMethod = ExtractionMethod.NATIVE_TABLE) -> ExtractedValue:
    ev = ExtractedValue(value_id=obs.value_id, doc_id=obs.doc_id, method=method, extractor="test", extractor_version="1",
                        locator={"slide": 3}, slide_or_page=3, context={}, raw_text=str(obs.value),
                        raw_number=obs.value, number_format=None, flags=[], confidence=1.0, created_at=NOW)
    store.upsert([ev])
    return ev


def make_mapping(obs: Observation, kpi_id: str | None, *, status: MappingStatus = MappingStatus.ACCEPTED,
                 method: MappingMethod | None = MappingMethod.SYNONYM, candidates: list | None = None,
                 reason: str = "synonym", registry_version: str = "test") -> KpiMapping:
    if status == MappingStatus.NO_MATCH:
        method = None
    return KpiMapping(observation_id=obs.observation_id, registry_version=registry_version, status=status,
                      method=method, kpi_id=kpi_id, kpi_version=1 if kpi_id else None,
                      candidates=candidates if candidates is not None else ([{"kpi_id": kpi_id, "method": "SYNONYM",
                                                                              "confidence": 1.0}] if kpi_id else []),
                      reason=reason, decided_by="system:mapping@v1", decided_at=NOW)


def run(store, registry, settings, doc, pairs, *, dq=None, provider=None, run_id="run-1"):
    from kpi_evidence.reconcile import evaluate_document

    observations, mappings = [], []
    for obs, target in pairs:
        observations.append(obs)
        if isinstance(target, KpiMapping):
            mappings.append(target)
        elif target is not None:
            mappings.append(make_mapping(obs, target))
    return evaluate_document(store, registry, settings, doc, observations, mappings, dq or {}, run_id,
                             provider=provider)


def one(store, registry, settings, doc, obs, target, **kw):
    return run(store, registry, settings, doc, [(obs, target)], **kw)[0]
