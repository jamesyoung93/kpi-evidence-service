from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from kpi_evidence.contracts import (
    Document, DocumentFormat, DocumentStatus, ExtractedValue, ExtractionMethod, KpiResult, Observation, Publication,
    PublicationLabel, ReviewAction, ValidationStatus,
)
from kpi_evidence.ids import scope_key, sha256_text

STAMP = datetime(2026, 6, 10, 9, 0, 0)


@pytest.fixture(scope="session")
def synth_set(tmp_path_factory) -> dict[str, Any]:
    from kpi_evidence.eval.synth_reports import LABELS_FILE, generate_all

    out = tmp_path_factory.mktemp("synth_reports")
    paths = generate_all(out)
    return {"dir": out, "paths": paths, "labels": json.loads((out / LABELS_FILE).read_text(encoding="utf-8"))}


def _date(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


class ResultWriter:
    def __init__(self, store):
        self.store = store
        self.docs: dict[str, Document] = {}
        self.n = 0
        self._pending: list[list] | None = None

    def batch(self, labels: list[dict[str, Any]]) -> list[KpiResult]:
        self._pending = [[], [], []]
        try:
            out = [self.from_label(lab) for lab in labels]
            for records in self._pending:
                self.store.insert(records)
        finally:
            self._pending = None
        return out

    def document(self, file: str, *, doc_id: str | None = None, status: DocumentStatus = DocumentStatus.PROCESSED) -> Document:
        if file not in self.docs:
            fmt = DocumentFormat.PDF if file.lower().endswith(".pdf") else DocumentFormat.PPTX
            doc = Document(doc_id=doc_id or sha256_text(file), file_name=file, file_path=f"/tmp/{file}", size_bytes=1,
                           format=fmt, status=status, registered_at=STAMP, updated_at=STAMP, brand="KESTREL")
            self.store.upsert([doc])
            self.docs[file] = doc
        return self.docs[file]

    def set_status(self, file: str, status: DocumentStatus) -> None:
        doc = self.docs[file]
        doc.status = status
        self.store.upsert([doc])

    def result(self, file: str, *, page: int | None, kpi_id: str | None, period_end: str | None, value: float | None,
               status: str, reason: str = "", scope: dict | None = None, grain: str = "month",
               is_current: bool = True, half_unit: float = 0.0, result_id: str | None = None) -> KpiResult:
        doc = self.document(file)
        self.n += 1
        i = self.n
        sk = scope_key(scope)
        ev = ExtractedValue(value_id=f"v{i}", doc_id=doc.doc_id, method=ExtractionMethod.NATIVE_TABLE, extractor="test",
                            extractor_version="0", locator={"i": i}, slide_or_page=page, context={}, raw_text=None,
                            raw_number=value, number_format=None, flags=[], confidence=1.0, created_at=STAMP)
        obs = Observation(observation_id=f"o{i}", value_id=ev.value_id, doc_id=doc.doc_id, interpretation_version="t",
                          label="", label_context="", value=value, unit=None, display_half_unit=half_unit,
                          period_start=None, period_end=_date(period_end), period_grain=grain, period_label=None,
                          period_source=None, period_confidence=1.0, scope=dict(scope or {}), scope_key=sk,
                          brand="KESTREL", fingerprint="", stated_rule="", flags=[], confidence=1.0, created_at=STAMP)
        res = KpiResult(result_id=result_id or f"r{i}", result_version=1, observation_id=obs.observation_id,
                        doc_id=doc.doc_id, brand="KESTREL", kpi_id=kpi_id, kpi_version=1 if kpi_id else None,
                        definition_hash=None, scope_key=sk, period_start=None, period_end=_date(period_end),
                        period_grain=grain, report_value=value, unit=None, platform_value=None, abs_diff=None,
                        rel_diff=None, tolerance={}, match_basis=None, validation_status=ValidationStatus(status),
                        reason_code=reason, reason_detail="", explanation={}, definition_choices={},
                        extraction_method=ExtractionMethod.NATIVE_TABLE, recon_run_id=None, sql_artifact_id=None,
                        dq_status=None, is_current=is_current, created_at=STAMP)
        if self._pending is not None:
            for bucket, record in zip(self._pending, (ev, obs, res)):
                bucket.append(record)
        else:
            self.store.insert([ev])
            self.store.insert([obs])
            self.store.insert([res])
        return res

    def from_label(self, lab: dict[str, Any], **override: Any) -> KpiResult:
        reason = lab.get("expected_reason") or "single_period_gap_unexplained"
        spec = dict(page=lab["page"], kpi_id=lab["kpi_id"], period_end=lab["period_end"], value=lab["report_value"],
                    status=lab["expected_status"], reason=reason, scope=lab["scope"], grain=lab["period_grain"] or "month",
                    half_unit=lab["display_half_unit"])
        spec.update(override)
        return self.result(lab["file"], **spec)

    def publish(self, res: KpiResult, approved_by: str) -> None:
        pub = Publication(publication_id=f"p-{res.result_id}", brand="KESTREL", kpi_id=res.kpi_id or "x", kpi_version=1,
                          scope_key=res.scope_key, period_start=None, period_end=res.period_end,
                          period_grain=res.period_grain, period_label=None, value=res.report_value or 0.0, unit=None,
                          display_label=PublicationLabel.MATCHES_PLATFORM, validation_status=res.validation_status,
                          review_action=ReviewAction.ACCEPT, platform_value=None, variance_rel=None, tolerance={},
                          definition_hash=None, sql_artifact_id=None, sql_sha=None, evidence_uri="", evidence_hash="",
                          evidence_locator={}, extraction_method=None, result_id=res.result_id, decision_id="d",
                          approved_by=approved_by, approved_at=STAMP, published_at=STAMP)
        self.store.insert([pub])


@pytest.fixture()
def result_writer(mem_store) -> ResultWriter:
    return ResultWriter(mem_store)


@pytest.fixture()
def eval_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture()
def result_writer_cls() -> type[ResultWriter]:
    return ResultWriter
