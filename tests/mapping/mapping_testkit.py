from __future__ import annotations

from datetime import date

from kpi_evidence.ai import AIMappingResult
from kpi_evidence.contracts import Observation
from kpi_evidence.ids import scope_key, stable_id, utcnow

MEANING_FLAG = {"code": "NEARBY_LABEL_UNVERIFIED", "dimension": "meaning", "detail": "label from nearby text"}


def make_obs(label: str, *, unit: str | None = "count", context: str = "", flags: list | None = None,
             scope: dict | None = None, fp: str = "fp-layout-1", obs_id: str | None = None,
             value: float | None = 100.0) -> Observation:
    obs_id = obs_id or stable_id("obs", label, unit, context, fp, scope or {})
    return Observation(
        observation_id=obs_id, value_id="v-" + obs_id, doc_id="doc-1", interpretation_version="test@1",
        label=label, label_context=context, value=value, unit=unit, display_half_unit=0.5,
        period_start=date(2026, 5, 1), period_end=date(2026, 5, 31), period_grain="month", period_label="May 2026",
        period_source="category", period_confidence=1.0, scope=scope or {}, scope_key=scope_key(scope),
        brand="KESTREL", fingerprint=fp, stated_rule="", flags=flags or [], confidence=1.0, created_at=utcnow(),
        run_id="run-test")


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, kpi_id: str | None = None, confidence: float = 0.95, raise_exc: Exception | None = None):
        self.kpi_id = kpi_id
        self.confidence = confidence
        self.raise_exc = raise_exc
        self.calls: list[list] = []

    def map_labels(self, requests, labels):
        self.calls.append(list(requests))
        if self.raise_exc is not None:
            raise self.raise_exc
        out = []
        for r in requests:
            kpi = self.kpi_id if self.kpi_id is not None else (r.candidates[0] if r.candidates else None)
            out.append(AIMappingResult(r.observation_id, kpi, self.confidence, "Fake proposal. Second sentence.",
                                       self.model, "sha-fake", '{"raw": true}'))
        return out

    def complete(self, prompt):
        return ""

    def propose_metadata(self, text, fields):
        return {}
