from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytest

from kpi_evidence.contracts import (Document, DocumentFormat, DocumentPage, DocumentStatus, ExtractedValue,
                                    ExtractionMethod, KpiMapping, KpiResult, MappingMethod, MappingProposal,
                                    MappingStatus, MatchBasis, Observation, PlatformKpiValue, ReconRun, ReconTrial,
                                    SqlArtifact, ValidationStatus)
from kpi_evidence.ids import scope_key as make_scope_key
from kpi_evidence.ids import stable_id, utcnow

BRAND = "KESTREL"


@dataclass
class Seeded:
    doc: Document
    value: ExtractedValue
    obs: Observation
    mapping: KpiMapping | None
    result: KpiResult


@dataclass
class Seeder:
    store: Any
    registry: Any
    _n: int = 0
    docs: dict[str, Document] = field(default_factory=dict)

    def doc(self, name: str = "Kestrel_Monthly_Brand_Review_2026-06.pptx", *, brand: str | None = BRAND,
            period_end: date | None = date(2026, 5, 31), status: DocumentStatus = DocumentStatus.PROCESSED,
            superseded_by: str | None = None) -> Document:
        if name in self.docs and superseded_by is None and status == DocumentStatus.PROCESSED:
            return self.docs[name]
        now = utcnow()
        d = Document(doc_id=stable_id("doc", name), file_name=name, file_path=f"/vol/inbox/{name}", size_bytes=100,
                     format=DocumentFormat.PPTX if name.endswith(".pptx") else DocumentFormat.PDF, status=status,
                     registered_at=now, updated_at=now, brand=brand, report_type="monthly_brand_review",
                     period_label="May 2026", period_start=None, period_end=period_end,
                     superseded_by_doc_id=superseded_by)
        self.store.upsert([d])
        self.docs[name] = d
        return d

    def page(self, doc: Document, number: int, *, hidden: bool = False, title: str = "TRx trend") -> DocumentPage:
        p = DocumentPage(doc_id=doc.doc_id, page_number=number, title=title, text=f"{title} text", notes=None,
                         hidden=hidden, image_count=0, page_width=12192000.0, page_height=6858000.0,
                         shapes=[{"shape_id": 4, "kind": "chart", "name": "Chart 1", "bbox": [1, 2, 3, 4]}],
                         created_at=utcnow())
        self.store.upsert([p])
        return p

    def seed(self, kpi_id: str | None = "trx", status: ValidationStatus = ValidationStatus.VERIFIED, *,
             doc: Document | None = None, report_value: float | None = 5910.0, platform_value: float | None = 5909.8,
             period_end: date | None = date(2026, 5, 31), grain: str = "month", scope: dict | None = None,
             method: ExtractionMethod = ExtractionMethod.NATIVE_CHART, mapping_method: MappingMethod | None = MappingMethod.SYNONYM,
             mapping_status: MappingStatus = MappingStatus.ACCEPTED, dq_status: str | None = "pass",
             page: int | None = 2, fingerprint: str = "fp-trx-chart", label: str = "TRx", unit: str = "count",
             context: dict | None = None, value_flags: list | None = None, is_current: bool = True,
             brand: str | None = BRAND, recon_run_id: str | None = None, sql_artifact_id: str | None = None,
             period_label: str | None = None, created_offset: int = 0, half_unit: float = 0.5,
             definition_choices: dict | None = None) -> Seeded:
        self._n += 1
        doc = doc or self.doc()
        locator = {"slide": page, "shape_id": 4, "series": 0, "point_index": self._n}
        ctx = {"slide_title": "TRx trend", "chart_title": "TRx by month", "series_name": "TRx",
               "category": f"cat{self._n}", **(context or {})}
        value = ExtractedValue(
            value_id=stable_id(doc.doc_id, method.value, locator), doc_id=doc.doc_id, method=method,
            extractor="test", extractor_version="1", locator=locator, slide_or_page=page, context=ctx,
            raw_text=str(report_value), raw_number=report_value, number_format="#,##0", flags=list(value_flags or []),
            confidence=1.0, created_at=utcnow())
        scope = scope or {}
        if period_end is not None:
            start = period_end.replace(day=1) if grain == "month" else (period_end - timedelta(days=80)).replace(day=1)
        else:
            start = None
        obs = Observation(
            observation_id=stable_id(value.value_id, "interp@1"), value_id=value.value_id, doc_id=doc.doc_id,
            interpretation_version="interp@1", label=label, label_context=f"{label} by month", value=report_value,
            unit=unit, display_half_unit=0.5, period_start=start, period_end=period_end, period_grain=grain,
            period_label=period_label or (f"{period_end:%b %Y}" if period_end else None), period_source="category",
            period_confidence=1.0, scope=scope, scope_key=make_scope_key(scope), brand=brand, fingerprint=fingerprint,
            stated_rule="", flags=[], confidence=1.0, created_at=utcnow())
        mapping = None
        if mapping_method is not None or mapping_status != MappingStatus.ACCEPTED:
            kdef = self.registry.kpis.get(kpi_id) if kpi_id else None
            mapping = KpiMapping(observation_id=obs.observation_id, registry_version=self.registry.version,
                                 status=mapping_status, method=mapping_method, kpi_id=kpi_id,
                                 kpi_version=kdef.version if kdef else None, candidates=[], reason="test",
                                 decided_by="system:mapping@v1", decided_at=utcnow())
        kdef = self.registry.kpis.get(kpi_id) if kpi_id else None
        definition_hash = kdef.definition_hash() if kdef else None
        rel = None
        if report_value is not None and platform_value:
            rel = (report_value - platform_value) / abs(platform_value)
        created = utcnow() + timedelta(seconds=created_offset)
        result = KpiResult(
            result_id=stable_id(obs.observation_id, kpi_id, definition_hash), result_version=1,
            observation_id=obs.observation_id, doc_id=doc.doc_id, brand=brand, kpi_id=kpi_id,
            kpi_version=kdef.version if kdef else None, definition_hash=definition_hash,
            scope_key=obs.scope_key, period_start=start, period_end=period_end, period_grain=grain,
            report_value=report_value, unit=unit, platform_value=platform_value,
            abs_diff=abs(report_value - platform_value) if report_value is not None and platform_value is not None else None,
            rel_diff=rel, tolerance={"rel": 0.005, "abs": 0.0, "half_unit": half_unit, "applied": 29.5},
            match_basis=MatchBasis.WITHIN_ROUNDING if status == ValidationStatus.VERIFIED else None,
            validation_status=status, reason_code="test", reason_detail=f"{status.value} detail",
            explanation={}, definition_choices=dict(definition_choices or {}), extraction_method=method, recon_run_id=recon_run_id,
            sql_artifact_id=sql_artifact_id, dq_status=dq_status, is_current=is_current, created_at=created)
        self.store.upsert([value])
        self.store.upsert([obs])
        if mapping is not None:
            self.store.upsert([mapping])
        self.store.upsert([result])
        return Seeded(doc=doc, value=value, obs=obs, mapping=mapping, result=result)

    def recon(self, seeded: Seeded, *, snapshot: dict | None = None, values: dict | None = None) -> ReconRun:
        r = seeded.result
        rid = stable_id("run", r.result_id)
        now = utcnow()
        start = {"measure": "units", "source": "account_feed"}
        run = ReconRun(recon_run_id=rid, doc_id=r.doc_id, kpi_id=r.kpi_id, kpi_version=r.kpi_version or 1,
                       definition_hash=r.definition_hash or "", bindings={"brand": BRAND}, bindings_hash="bh",
                       start_choices=start, final_choices=start, phase_a_outcome="verified", phase_b_outcome=None,
                       reconloop_version="0.5.0", judge=None, policy={}, record={},
                       platform_snapshot=snapshot if snapshot is not None else {"gv_rx_account_monthly": "v1"},
                       started_at=now, finished_at=now)
        trials = [
            ReconTrial(trial_id=stable_id(rid, 1), recon_run_id=rid, seq=1, phase="phase_a", choices=start,
                       as_of=r.period_end, value=r.platform_value, error=None, sql="SELECT 1 AS value", sql_sha="s1",
                       created_at=now),
            ReconTrial(trial_id=stable_id(rid, 2), recon_run_id=rid, seq=2, phase="grid",
                       choices={"measure": "normalized", "source": "account_feed"}, as_of=r.period_end, value=None,
                       error="boom", sql="SELECT 2 AS value", sql_sha="s2", created_at=now),
        ]
        artifact = SqlArtifact(artifact_id=stable_id("art", rid), kpi_id=r.kpi_id, kpi_version=r.kpi_version or 1,
                               definition_hash=r.definition_hash or "", bindings={}, choices=start,
                               value_sql_template="SELECT SUM(trx_units) AS value ...", components_sql={}, checks_sql={},
                               function_sql=None, sql_sha="sha-artifact", export_path="/vol/artifacts/sql/trx/x.sql",
                               created_at=now)
        series = [PlatformKpiValue(kpi_id=r.kpi_id, definition_hash=r.definition_hash or "", scope_key=r.scope_key,
                                   period_grain=r.period_grain, period_end=end, period_start=end.replace(day=1),
                                   brand=BRAND, value=v, components={}, sql_artifact_id=artifact.artifact_id,
                                   platform_snapshot={}, computed_at=now)
                  for end, v in (values or {date(2026, 4, 30): 5700.0, r.period_end: r.platform_value}).items()]
        self.store.upsert([run])
        self.store.upsert(trials)
        self.store.upsert([artifact])
        self.store.upsert(series)
        r.recon_run_id, r.sql_artifact_id = rid, artifact.artifact_id
        self.store.upsert([r])
        return run

    def proposal(self, seeded: Seeded, kpi_id: str, method: MappingMethod = MappingMethod.SYNONYM) -> MappingProposal:
        p = MappingProposal(proposal_id=stable_id("prop", seeded.obs.observation_id, kpi_id),
                            observation_id=seeded.obs.observation_id, method=method, kpi_id=kpi_id, confidence=0.9,
                            rationale="synonym match", registry_version=self.registry.version, created_at=utcnow())
        self.store.upsert([p])
        return p


@pytest.fixture()
def store(mem_store):
    return mem_store


@pytest.fixture()
def seeder(store, registry) -> Seeder:
    return Seeder(store, registry)


@pytest.fixture()
def fake_siblings(monkeypatch):
    state = types.SimpleNamespace(recheck_status=ValidationStatus.VERIFIED, recheck_platform=5909.8,
                                  recheck_dq="pass", recheck_calls=[], remember_calls=[], raise_on_recheck=None)

    def recheck_observation(store, registry, settings, observation, kpi_id, run_id):
        state.recheck_calls.append((observation.observation_id, kpi_id, run_id))
        if state.raise_on_recheck:
            raise state.raise_on_recheck
        kdef = registry.get(kpi_id)
        value = store.get(ExtractedValue, value_id=observation.value_id)
        platform = state.recheck_platform
        result = KpiResult(
            result_id=stable_id(observation.observation_id, kpi_id, kdef.definition_hash()), result_version=1,
            observation_id=observation.observation_id, doc_id=observation.doc_id, brand=observation.brand,
            kpi_id=kpi_id, kpi_version=kdef.version, definition_hash=kdef.definition_hash(),
            scope_key=observation.scope_key, period_start=observation.period_start, period_end=observation.period_end,
            period_grain=observation.period_grain, report_value=observation.value, unit=observation.unit,
            platform_value=platform,
            abs_diff=None if platform is None else abs(observation.value - platform),
            rel_diff=None if not platform else (observation.value - platform) / abs(platform),
            tolerance={"rel": kdef.tolerance.rel}, match_basis=None, validation_status=state.recheck_status,
            reason_code="recheck", reason_detail="re-checked", explanation={}, definition_choices={},
            extraction_method=value.method if value else None, recon_run_id=None, sql_artifact_id=None,
            dq_status=state.recheck_dq, is_current=True, created_at=utcnow(), run_id=run_id)
        store.upsert([result])
        return result

    def remember_mapping(store, observation, kpi_id, actor, decision_id=None):
        state.remember_calls.append((observation.observation_id, kpi_id, actor, decision_id))

    reconcile = types.ModuleType("kpi_evidence.reconcile")
    reconcile.recheck_observation = recheck_observation
    mapping = types.ModuleType("kpi_evidence.mapping")
    mapping.remember_mapping = remember_mapping
    monkeypatch.setitem(sys.modules, "kpi_evidence.reconcile", reconcile)
    monkeypatch.setitem(sys.modules, "kpi_evidence.mapping", mapping)
    return state


@pytest.fixture(scope="session")
def registry(registry):
    import copy

    reg = copy.copy(registry)
    policy = copy.deepcopy(registry.policy)
    policy.setdefault("auto_publish", {}).update(verified=True, report_only_known_layout=True)
    reg.policy = policy
    return reg
