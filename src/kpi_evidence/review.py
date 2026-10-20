from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .contracts import (STATUS_INFO, STRUCTURED_METHODS, DecisionSource, Document, DocumentPage, DocumentStatus,
                        ExtractedValue, ExtractionMethod, KpiMapping, KpiResult, MappingMethod, MappingProposal,
                        MappingStatus, MatchBasis, Observation, PlatformKpiValue, Publication, PublicationLabel,
                        ReconRun, ReconTrial, ReviewAction, ReviewDecision, ReviewEvent, SqlArtifact, StatusGroup,
                        StatusInfo, ValidationStatus)
from .ids import new_uuid, stable_id, utcnow
from .serving import format_value, in_list, is_missing, kpi_labels, relation_columns, status_columns

ACCEPTABLE = frozenset({
    ValidationStatus.VERIFIED, ValidationStatus.REPORT_ONLY, ValidationStatus.VARIANCE_REVIEW,
    ValidationStatus.INCOMPATIBLE_DEFINITION, ValidationStatus.REPORT_CONFLICT,
})

CORRECTION_INTERPRETATION = "reviewer-correction@v1"

PRIOR_DECISION_LABEL = "Earlier decision (for information - does not approve this value)"

QUEUE_COLUMNS = (
    "result_id", "result_version", "status", "status_label", "group", "kpi_id", "kpi_label", "label",
    "report_value", "platform_value", "rel_diff", "period_label", "scope_key", "file_name", "page",
    "reason_detail", "decided", "doc_id", "brand", "unit", "reason_code", "period_end",
)

PRIOR_COLUMNS = (
    "decision_id", "result_id", "decided_version", "action", "rationale", "decided_by", "decision_source",
    "decided_at", "corrected_value", "corrected_kpi_id", "doc_id", "file_name", "period_label", "period_end",
    "report_value", "platform_value", "validation_status", "definition_hash", "same_document",
    "file_changed", "definition_changed", "platform_changed", "still_valid", "changes", "informative", "label",
)

_VALUE_FLAG_DIMENSIONS = frozenset({"value", "extraction"})
_VALUE_FLAG_CODES = frozenset({"IMAGE_NOT_PARSED", "LOW_CONFIDENCE_VALUE", "OCR_LOW_CONFIDENCE"})


def _value_flag(flag: Any) -> bool:
    return isinstance(flag, dict) and (flag.get("dimension") in _VALUE_FLAG_DIMENSIONS
                                       or flag.get("code") in _VALUE_FLAG_CODES)


def _has_flag(flags: Any, code: str) -> bool:
    return any(isinstance(f, dict) and f.get("code") == code for f in (flags or []))


class DecisionError(ValueError):
    """The decision is not allowed by the review rules; the message is written for the reviewer."""


class StaleResult(DecisionError):
    """The result changed (new version, or no longer current) after the reviewer opened it."""


@dataclass
class Evidence:
    """Everything the evidence drawer shows for one result."""

    result: KpiResult
    observation: Observation | None
    value: ExtractedValue | None
    document: Document | None
    page: DocumentPage | None
    kpi: dict[str, Any] | None
    recon_run: dict[str, Any] | None
    trials: pd.DataFrame
    sql_artifact: SqlArtifact | None
    platform_series: pd.DataFrame
    prior_decisions: pd.DataFrame
    mapping: KpiMapping | None
    mapping_proposals: pd.DataFrame
    status_info: StatusInfo | None
    decisions: pd.DataFrame = field(default_factory=pd.DataFrame)
    publications: pd.DataFrame = field(default_factory=pd.DataFrame)

    def story(self) -> str:
        """One plain sentence: what the report says, what the platform says, and the comparison."""
        r = self.result
        unit = r.unit
        where = []
        if self.value is not None and self.value.slide_or_page is not None:
            deck = self.document is not None and self.document.format.value in ("PPTX", "PPTM")
            where.append(f"{'slide' if deck else 'page'} {self.value.slide_or_page}")
        if self.observation is not None and self.observation.period_label:
            where.append(self.observation.period_label)
        located = f" ({', '.join(where)})" if where else ""
        text = f"Report says {format_value(r.report_value, unit) or 'no readable value'}{located}."
        if r.platform_value is not None:
            text += f" Platform says {format_value(r.platform_value, unit)}"
            if self.kpi and self.kpi.get("label"):
                text += f" for {self.kpi['label']}"
            text += "."
            if r.rel_diff is not None:
                text += f" Difference {r.rel_diff * 100:+.1f}%."
        if self.status_info is not None:
            text += f" {self.status_info.label}."
        return text


@dataclass
class DecisionOutcome:
    decision: ReviewDecision
    publication: Publication | None
    new_result: KpiResult | None
    message: str
    retracted: list[Publication] = field(default_factory=list)
    replayed: bool = False


@dataclass
class PolicyCheck:
    """Why a result was or was not auto-published (``reasons`` lists every failed condition)."""

    result_id: str
    rule: str | None
    eligible: bool
    reasons: list[str]


def review_queue(store, *, brand: str | None = None, groups: tuple[str, ...] = ("REVIEW", "BLOCKED"),
                 include_decided: bool = False, registry=None) -> pd.DataFrame:
    """Current results that need a person, sorted by status (STATUS_INFO order), document, then page."""
    wanted_groups = {str(g).upper() for g in groups}
    statuses = [s.value for s, info in STATUS_INFO.items() if info.group.value in wanted_groups]
    if not statuses:
        return pd.DataFrame(columns=list(QUEUE_COLUMNS))
    marks, params = in_list("s", statuses)
    where = ["r.is_current = TRUE", f"r.validation_status IN ({marks})"]
    if brand:
        where.append("COALESCE(r.brand, d.brand) = :brand")
        params["brand"] = brand
    if not include_decided:
        where.append("dd.result_id IS NULL")
        if wanted_groups <= {StatusGroup.REVIEW.value, StatusGroup.BLOCKED.value}:
            view_cols = relation_columns(store, "review_open")
            if view_cols is not None and "result_id" in view_cols:
                where.append(f"r.result_id IN (SELECT result_id FROM {store.fq('review_open')})")
    t = store.fq
    sql = f"""
        SELECT r.result_id, r.result_version, r.validation_status AS status, r.kpi_id, o.label,
               r.report_value, r.platform_value, r.rel_diff, o.period_label, r.scope_key, d.file_name,
               v.slide_or_page AS page, r.reason_detail, r.doc_id, COALESCE(r.brand, d.brand) AS brand,
               r.unit, r.reason_code, r.period_end, dd.n_decisions
        FROM {t('kpi_results')} r
        LEFT JOIN {t('observations')} o ON o.observation_id = r.observation_id
        LEFT JOIN {t('extracted_values')} v ON v.value_id = o.value_id
        LEFT JOIN {t('documents')} d ON d.doc_id = r.doc_id
        LEFT JOIN (SELECT result_id, result_version, COUNT(*) AS n_decisions FROM {t('review_decisions')}
                   GROUP BY result_id, result_version) dd
               ON dd.result_id = r.result_id AND dd.result_version = r.result_version
        WHERE {' AND '.join(where)}
    """
    df = store.query(sql, params)
    if df.empty:
        return pd.DataFrame(columns=list(QUEUE_COLUMNS))
    labels = kpi_labels(store, registry)
    info = [status_columns(s) for s in df["status"]]
    df["status_label"] = [i[0] for i in info]
    df["group"] = [i[2] for i in info]
    df["_sort"] = [i[3] for i in info]
    df["kpi_label"] = [labels.get(k, k) if not is_missing(k) else None for k in df["kpi_id"]]
    df["decided"] = [not is_missing(n) and int(n) > 0 for n in df["n_decisions"]]
    df = df.sort_values(["_sort", "file_name", "doc_id", "page", "result_id"], na_position="last", kind="stable")
    return df.reset_index(drop=True)[list(QUEUE_COLUMNS)]


def record_opened(store, result_id: str, actor: str, *, request_id: str | None = None) -> None:
    """Log that a reviewer opened a result (reviewer-workload metric)."""
    event_id = stable_id("opened", result_id, actor, request_id or new_uuid())
    store.insert_new([ReviewEvent(event_id=event_id, result_id=result_id, doc_id=None, event="opened", actor=actor,
                                  event_at=utcnow(), detail={}, request_id=request_id)])


def evidence(store, registry, result_id: str) -> Evidence:
    """Assemble the evidence drawer for one result (raises KeyError for an unknown result)."""
    result = store.get(KpiResult, result_id=result_id)
    if result is None:
        raise KeyError(f"unknown result {result_id}")
    observation = store.get(Observation, observation_id=result.observation_id)
    value = store.get(ExtractedValue, value_id=observation.value_id) if observation else None
    document = store.get(Document, doc_id=result.doc_id)
    page = None
    if value is not None and value.slide_or_page is not None:
        page = store.get(DocumentPage, doc_id=result.doc_id, page_number=int(value.slide_or_page))
    run = store.get(ReconRun, recon_run_id=result.recon_run_id) if result.recon_run_id else None
    artifact = store.get(SqlArtifact, artifact_id=result.sql_artifact_id) if result.sql_artifact_id else None
    mapping, proposals = _mapping_for(store, registry, result.observation_id)
    try:
        status_info = STATUS_INFO[result.validation_status]
    except KeyError:
        status_info = None
    return Evidence(
        result=result, observation=observation, value=value, document=document, page=page,
        kpi=_kpi_dict(store, registry, result.kpi_id, result.kpi_version),
        recon_run=_run_summary(run), trials=_trials_frame(store, result, run), sql_artifact=artifact,
        platform_series=_platform_series(store, result), prior_decisions=prior_decisions(store, result, observation),
        mapping=mapping, mapping_proposals=proposals, status_info=status_info,
        decisions=store.read_df(ReviewDecision, "result_id = :r", {"r": result.result_id}, order_by="decided_at"),
        publications=store.read_df(Publication, "result_id = :r", {"r": result.result_id}, order_by="published_at"),
    )


def _kpi_dict(store, registry, kpi_id: str | None, kpi_version: int | None) -> dict[str, Any] | None:
    if not kpi_id:
        return None
    kdef = registry.kpis.get(kpi_id) if registry is not None else None
    if kdef is not None:
        p = kdef.platform
        return {
            "kpi_id": kdef.kpi_id, "version": kdef.version, "label": kdef.label, "definition": kdef.definition,
            "unit": kdef.unit, "aggregation": kdef.aggregation, "grains": list(kdef.grains),
            "dimensions": list(kdef.dimensions), "claim_class": kdef.claim_class, "owner": kdef.owner,
            "tolerance": {"rel": kdef.tolerance.rel, "abs": kdef.tolerance.abs,
                          "use_display_rounding": kdef.tolerance.use_display_rounding},
            "has_platform_spec": kdef.has_platform_spec, "definition_hash": kdef.definition_hash(),
            "harness_link": kdef.harness_link, "source_views": list(p.source_views) if p else [],
            "params": ({n: {"default": ps.default, "options": dict(ps.options), "describe": dict(ps.describe),
                            "permitted": list(ps.permitted)} for n, ps in p.params.items()} if p else {}),
            "approved_by": kdef.approved_by, "approved_at": kdef.approved_at, "source_file": kdef.source_file,
        }
    where, params = "kpi_id = :k", {"k": kpi_id}
    if kpi_version is not None:
        where += " AND kpi_version = :v"
        params["v"] = int(kpi_version)
    df = store.read_df("kpi_registry", where, params, order_by="kpi_version DESC", limit=1)
    if df.empty:
        return None
    row = df.iloc[0].to_dict()
    row["spec"] = json.loads(row["spec"]) if isinstance(row.get("spec"), str) else row.get("spec")
    row["version"] = row.get("kpi_version")
    return row


def _run_summary(run: ReconRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "recon_run_id": run.recon_run_id, "kpi_id": run.kpi_id, "kpi_version": run.kpi_version,
        "definition_hash": run.definition_hash, "bindings": run.bindings, "start_choices": run.start_choices,
        "final_choices": run.final_choices, "phase_a_outcome": run.phase_a_outcome,
        "phase_b_outcome": run.phase_b_outcome, "judge": run.judge, "reconloop_version": run.reconloop_version,
        "platform_snapshot": run.platform_snapshot, "policy": run.policy, "error": run.error,
        "started_at": run.started_at, "finished_at": run.finished_at,
    }


def _choices_text(choices: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted((choices or {}).items())) or "(no options)"


def _trials_frame(store, result: KpiResult, run: ReconRun | None) -> pd.DataFrame:
    cols = ["seq", "phase", "choices", "choices_text", "as_of", "value", "error", "sql", "sql_sha",
            "for_this_period", "rel_diff", "is_start_choice", "trial_id"]
    if not result.recon_run_id:
        return pd.DataFrame(columns=cols)
    trials = store.read(ReconTrial, "recon_run_id = :r", {"r": result.recon_run_id}, order_by="seq")
    start = run.start_choices if run is not None else None
    rows = []
    for t in trials:
        this_period = result.period_end is not None and t.as_of == result.period_end
        rel = None
        if this_period and t.value is not None and result.report_value not in (None, 0):
            rel = (t.value - result.report_value) / abs(result.report_value)
        rows.append({"seq": t.seq, "phase": t.phase, "choices": t.choices, "choices_text": _choices_text(t.choices),
                     "as_of": t.as_of, "value": t.value, "error": t.error, "sql": t.sql, "sql_sha": t.sql_sha,
                     "for_this_period": this_period, "rel_diff": rel,
                     "is_start_choice": start is not None and t.choices == start, "trial_id": t.trial_id})
    return pd.DataFrame(rows, columns=cols)


def _platform_series(store, result: KpiResult) -> pd.DataFrame:
    if not result.kpi_id or not result.definition_hash or not result.period_grain:
        return pd.DataFrame(columns=[c for c in PlatformKpiValue.__dataclass_fields__])
    return store.read_df(PlatformKpiValue,
                         "kpi_id = :k AND definition_hash = :h AND scope_key = :s AND period_grain = :g",
                         {"k": result.kpi_id, "h": result.definition_hash, "s": result.scope_key,
                          "g": result.period_grain}, order_by="period_end")


def _mapping_for(store, registry, observation_id: str) -> tuple[KpiMapping | None, pd.DataFrame]:
    mappings = store.read(KpiMapping, "observation_id = :o", {"o": observation_id}, order_by="decided_at DESC")
    version = registry.version if registry is not None else None
    chosen = next((m for m in mappings if m.registry_version == version), mappings[0] if mappings else None)
    proposals = store.read_df(MappingProposal, "observation_id = :o", {"o": observation_id}, order_by="created_at")
    return chosen, proposals


def _snapshot_of_run(store, recon_run_id: str | None) -> dict[str, Any] | None:
    if not recon_run_id:
        return None
    run = store.get(ReconRun, recon_run_id=recon_run_id)
    return run.platform_snapshot if run is not None else None


def prior_decisions(store, result: KpiResult, observation: Observation | None) -> pd.DataFrame:
    """Decisions on the same layout fingerprint + KPI in other documents, and on earlier versions of this result."""
    if observation is None or not result.kpi_id or not observation.fingerprint:
        return pd.DataFrame(columns=list(PRIOR_COLUMNS))
    t = store.fq
    sql = f"""
        SELECT x.decision_id, x.result_id, x.result_version AS decided_version, x.action, x.rationale,
               x.decided_by, x.decision_source, x.decided_at, x.corrected_value, x.corrected_kpi_id,
               r.doc_id, d.file_name, o.period_label, r.period_end, r.report_value, r.platform_value,
               r.validation_status, r.definition_hash, r.recon_run_id
        FROM {t('review_decisions')} x
        JOIN {t('kpi_results')} r ON r.result_id = x.result_id
        JOIN {t('observations')} o ON o.observation_id = r.observation_id
        LEFT JOIN {t('documents')} d ON d.doc_id = r.doc_id
        WHERE o.fingerprint = :fp AND r.kpi_id = :kpi
          AND (r.doc_id <> :doc OR (x.result_id = :rid AND x.result_version <> :rver))
        ORDER BY x.decided_at DESC, x.decision_id
    """
    df = store.query(sql, {"fp": observation.fingerprint, "kpi": result.kpi_id, "doc": result.doc_id,
                           "rid": result.result_id, "rver": int(result.result_version)})
    if df.empty:
        return pd.DataFrame(columns=list(PRIOR_COLUMNS))
    current_snapshot = _snapshot_of_run(store, result.recon_run_id) or {}
    seen = _decided_snapshots(store, list(df["decision_id"]))
    rows = []
    for row in df.to_dict(orient="records"):
        snapshot = seen.get(row["decision_id"])
        if snapshot is None:
            snapshot = _snapshot_of_run(store, None if is_missing(row["recon_run_id"]) else row["recon_run_id"])
        file_changed = row["doc_id"] != result.doc_id
        prior_hash = None if is_missing(row["definition_hash"]) else row["definition_hash"]
        definition_changed = prior_hash != result.definition_hash
        platform_changed = (snapshot or {}) != current_snapshot
        changes = [name for name, flag in (("source file", file_changed), ("KPI definition", definition_changed),
                                           ("platform data", platform_changed)) if flag]
        row.update({"same_document": not file_changed, "file_changed": file_changed,
                    "definition_changed": definition_changed, "platform_changed": platform_changed,
                    "still_valid": not changes, "changes": ", ".join(changes), "informative": True,
                    "label": PRIOR_DECISION_LABEL})
        rows.append(row)
    return pd.DataFrame(rows, columns=list(PRIOR_COLUMNS))


def _decided_snapshots(store, decision_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not decision_ids:
        return {}
    marks, params = in_list("e", [stable_id("decided", d) for d in decision_ids])
    out: dict[str, dict[str, Any]] = {}
    for ev in store.read(ReviewEvent, f"event_id IN ({marks})", params):
        detail = ev.detail or {}
        if detail.get("decision_id") and isinstance(detail.get("platform_snapshot"), dict):
            out[detail["decision_id"]] = detail["platform_snapshot"]
    return out


def evidence_hash(result: KpiResult) -> str:
    """Hash of what the reviewer saw: report value, platform value, definition, source file and status."""
    return stable_id(result.report_value, result.platform_value, result.definition_hash, result.doc_id,
                     result.validation_status.value)


def decide(store, registry, settings, result_id: str, action: ReviewAction | str, rationale: str, actor: str, *,
           corrected_value: float | None = None, corrected_kpi_id: str | None = None,
           request_id: str | None = None, decision_id: str | None = None,
           result_version: int | None = None) -> DecisionOutcome:
    """Record a human decision (ACCEPT / REJECT / CORRECT) and apply its consequences."""
    action = ReviewAction(str(action).upper())
    actor = (actor or "").strip()
    if not actor:
        raise DecisionError("A decision needs the reviewer's identity.")
    rationale = (rationale or "").strip()
    decision_id = decision_id or new_uuid()
    existing = store.get(ReviewDecision, decision_id=decision_id)
    if existing is not None:
        return _replay(store, existing)
    result = store.get(KpiResult, result_id=result_id)
    if result is None:
        raise KeyError(f"unknown result {result_id}")
    _check_current(result, result_version if result_version is not None else result.result_version)
    _validate(registry, action, result, rationale, corrected_value, corrected_kpi_id)
    decision = ReviewDecision(
        decision_id=decision_id, result_id=result.result_id, result_version=result.result_version, action=action,
        rationale=rationale, decided_by=actor, decision_source=DecisionSource.HUMAN, decided_at=utcnow(),
        evidence_hash=evidence_hash(result), corrected_value=corrected_value, corrected_kpi_id=corrected_kpi_id,
        policy_version=None, request_id=request_id)
    if action == ReviewAction.ACCEPT:
        return _accept(store, registry, result, decision)
    if action == ReviewAction.REJECT:
        return _reject(store, result, decision)
    return _correct(store, registry, settings, result, decision)


def _check_current(result: KpiResult, seen_version: int) -> None:
    if not result.is_current:
        raise StaleResult("This result is no longer current (a newer report or re-check replaced it). Reload it.")
    if int(seen_version) != int(result.result_version):
        raise StaleResult(f"This result changed since it was opened (version {seen_version} -> "
                          f"{result.result_version}). Review the updated evidence.")


def _validate(registry, action: ReviewAction, result: KpiResult, rationale: str,
              corrected_value: float | None, corrected_kpi_id: str | None) -> None:
    status = result.validation_status
    if not rationale and not (action == ReviewAction.ACCEPT and status == ValidationStatus.VERIFIED):
        raise DecisionError("Add a short rationale (required for every decision except accepting a match).")
    if action == ReviewAction.ACCEPT:
        if status == ValidationStatus.UNMAPPED_CAPTURED or not result.kpi_id:
            raise DecisionError("This number is not on the KPI list yet. Use Correct and choose the KPI first.")
        if status not in ACCEPTABLE:
            raise DecisionError(f"'{STATUS_INFO[status].label}' cannot be accepted as is. Correct the value or KPI, "
                                f"or reject it.")
        if result.report_value is None:
            raise DecisionError("There is no readable value to publish. Use Correct to enter it.")
    if action == ReviewAction.CORRECT:
        if corrected_value is None and not corrected_kpi_id:
            raise DecisionError("A correction needs a corrected value, a corrected KPI, or both.")
        kpi_id = corrected_kpi_id or result.kpi_id
        if not kpi_id:
            raise DecisionError("This number is not on the KPI list. Choose the KPI it measures.")
        if registry is None or kpi_id not in registry.kpis:
            raise DecisionError(f"Unknown KPI {kpi_id!r}.")
        if corrected_value is not None and not _finite(corrected_value):
            raise DecisionError("The corrected value must be a finite number.")


def _finite(value: Any) -> bool:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return v == v and v not in (float("inf"), float("-inf"))


def _decided_event(store, decision: ReviewDecision, result: KpiResult,
                   extra: dict[str, Any] | None = None) -> ReviewEvent:
    detail = {
        "decision_id": decision.decision_id, "action": decision.action.value, "result_version": decision.result_version,
        "status": result.validation_status.value, "report_value": result.report_value,
        "platform_value": result.platform_value, "definition_hash": result.definition_hash, "doc_id": result.doc_id,
        "platform_snapshot": _snapshot_of_run(store, result.recon_run_id) or {},
        "decision_source": decision.decision_source.value, **(extra or {}),
    }
    return ReviewEvent(event_id=stable_id("decided", decision.decision_id), result_id=result.result_id,
                       doc_id=result.doc_id, event="decided", actor=decision.decided_by, event_at=decision.decided_at,
                       detail=detail, request_id=decision.request_id)


def _accept(store, registry, result: KpiResult, decision: ReviewDecision) -> DecisionOutcome:
    with store.transaction():
        fresh = store.get(KpiResult, result_id=result.result_id)
        _check_current(fresh, result.result_version)
        store.insert_new([decision])
        store.insert_new([_decided_event(store, decision, fresh)])
        pub = publish(store, registry, fresh, decision)
    return DecisionOutcome(decision=decision, publication=pub, new_result=None,
                           message=f"Published as {pub.display_label.value}.")


def _retract_result(store, result_id: str, decision: ReviewDecision, verb: str) -> list[Publication]:
    return [retract(store, p.publication_id, decision.decided_by, f"{verb}: {decision.rationale}",
                    request_id=decision.request_id) for p in _current_publications_of(store, result_id)]


def _reject(store, result: KpiResult, decision: ReviewDecision) -> DecisionOutcome:
    with store.transaction():
        fresh = store.get(KpiResult, result_id=result.result_id)
        _check_current(fresh, result.result_version)
        store.insert_new([decision])
        retracted = _retract_result(store, result.result_id, decision, "Rejected")
        store.insert_new([_decided_event(store, decision, fresh,
                                         {"retracted": [p.publication_id for p in retracted]})])
    message = "Rejected; nothing was published."
    if retracted:
        message = f"Rejected; retracted {len(retracted)} published value(s)."
    return DecisionOutcome(decision=decision, publication=retracted[0] if retracted else None, new_result=None,
                           message=message, retracted=retracted)


def _correct(store, registry, settings, result: KpiResult, decision: ReviewDecision) -> DecisionOutcome:
    observation = store.get(Observation, observation_id=result.observation_id)
    if observation is None:
        raise DecisionError("The original observation is missing; re-run the document before correcting.")
    original_value = store.get(ExtractedValue, value_id=observation.value_id)
    kpi_id = decision.corrected_kpi_id or result.kpi_id
    kdef = registry.get(kpi_id)
    new_value, new_obs = build_correction(observation, original_value, decision)
    mapping = KpiMapping(
        observation_id=new_obs.observation_id, registry_version=registry.version, status=MappingStatus.ACCEPTED,
        method=MappingMethod.REVIEWER, kpi_id=kpi_id, kpi_version=kdef.version,
        candidates=[{"kpi_id": kpi_id, "method": MappingMethod.REVIEWER.value, "confidence": 1.0}],
        reason=f"Chosen by reviewer: {decision.rationale}", decided_by=decision.decided_by,
        decided_at=decision.decided_at)
    store.upsert([new_value])
    store.upsert([new_obs])
    store.upsert([mapping])
    original_mapping, _ = _mapping_for(store, registry, observation.observation_id)
    kpi_changed = decision.corrected_kpi_id is not None and (
        decision.corrected_kpi_id != result.kpi_id or original_mapping is None
        or original_mapping.status != MappingStatus.ACCEPTED)
    if kpi_changed:
        _remember_mapping(store, new_obs, kpi_id, decision)
    run_id = f"review-{decision.decision_id[:12]}"
    new_result = _recheck(store, registry, settings, new_obs, kpi_id, run_id)

    with store.transaction():
        fresh = store.get(KpiResult, result_id=result.result_id)
        _check_current(fresh, result.result_version)
        store.insert_new([decision])
        retracted = _retract_result(store, result.result_id, decision, "Corrected")
        store.insert_new([_decided_event(store, decision, fresh, {
            "new_observation_id": new_obs.observation_id, "new_value_id": new_value.value_id,
            "new_result_id": new_result.result_id if new_result else None, "kpi_changed": kpi_changed,
            "retracted": [p.publication_id for p in retracted]})])
        fresh.is_current = False
        store.upsert([fresh])
    pubs = auto_publish(store, registry, settings, [new_result], run_id) if new_result is not None else []
    publication = pubs[0] if pubs else None
    label = STATUS_INFO[new_result.validation_status].label if new_result else "not re-checked"
    message = f"Re-checked: now {label}."
    message += (f" Published as {publication.display_label.value}." if publication
                else " It waits for a decision before publishing.")
    return DecisionOutcome(decision=decision, publication=publication, new_result=new_result, message=message,
                           retracted=retracted)


def build_correction(observation: Observation, original: ExtractedValue | None,
                     decision: ReviewDecision) -> tuple[ExtractedValue, Observation]:
    """The REVIEWER_CORRECTION value and observation for a CORRECT decision (deterministic ids per decision)."""
    now = decision.decided_at
    value_corrected = decision.corrected_value is not None and (
        observation.value is None or abs(float(decision.corrected_value) - float(observation.value)) > 1e-12)
    new_number = float(decision.corrected_value) if decision.corrected_value is not None else observation.value
    locator = {**(original.locator if original else {}), "corrects_value_id": observation.value_id,
               "decision_id": decision.decision_id}
    value_id = stable_id(observation.doc_id, ExtractionMethod.REVIEWER_CORRECTION.value, locator)
    context = {**(original.context if original else {}), "corrected_by": decision.decided_by,
               "correction_rationale": decision.rationale, "value_corrected": value_corrected,
               "original_value": observation.value, "original_raw_text": original.raw_text if original else None,
               "original_method": original.method.value if original else None,
               "corrected_kpi_id": decision.corrected_kpi_id}
    flags = list(original.flags if original else [])
    if value_corrected:
        flags = [f for f in flags if not _value_flag(f)]
    flags.append({"code": "REVIEWER_CORRECTION", "dimension": "value" if value_corrected else "mapping",
                  "detail": f"decision {decision.decision_id}"})
    new_value = ExtractedValue(
        value_id=value_id, doc_id=observation.doc_id, method=ExtractionMethod.REVIEWER_CORRECTION,
        extractor="reviewer", extractor_version="v1", locator=locator,
        slide_or_page=original.slide_or_page if original else None, context=context,
        raw_text=(repr(new_number) if value_corrected else (original.raw_text if original else None)),
        raw_number=new_number,
        number_format=None if value_corrected else (original.number_format if original else None),
        flags=flags, confidence=1.0, created_at=now, run_id=None, source_record_id=observation.value_id)
    obs_flags = [f for f in observation.flags if not (value_corrected and _value_flag(f))]
    obs_flags.append({"code": "REVIEWER_CORRECTION", "dimension": "value" if value_corrected else "mapping",
                      "detail": f"corrects observation {observation.observation_id}"})
    new_obs = Observation(
        observation_id=stable_id(value_id, CORRECTION_INTERPRETATION), value_id=value_id, doc_id=observation.doc_id,
        interpretation_version=CORRECTION_INTERPRETATION, label=observation.label,
        label_context=observation.label_context, value=new_number, unit=observation.unit,
        display_half_unit=0.0 if value_corrected else observation.display_half_unit,
        period_start=observation.period_start, period_end=observation.period_end,
        period_grain=observation.period_grain, period_label=observation.period_label,
        period_source=observation.period_source, period_confidence=observation.period_confidence,
        scope=dict(observation.scope), scope_key=observation.scope_key, brand=observation.brand,
        fingerprint=observation.fingerprint, stated_rule=observation.stated_rule, flags=obs_flags,
        confidence=1.0, created_at=now, run_id=None)
    return new_value, new_obs


def _remember_mapping(store, observation: Observation, kpi_id: str, decision: ReviewDecision) -> None:
    from .mapping import remember_mapping

    remember_mapping(store, observation, kpi_id, decision.decided_by, decision_id=decision.decision_id)


def _recheck(store, registry, settings, observation: Observation, kpi_id: str, run_id: str) -> KpiResult:
    from .reconcile import recheck_observation

    result = recheck_observation(store, registry, settings, observation, kpi_id, run_id)
    if result is None:
        raise DecisionError("The re-check produced no result; the correction was not recorded.")
    return result


def _replay(store, decision: ReviewDecision) -> DecisionOutcome:
    pubs = store.read(Publication, "decision_id = :d", {"d": decision.decision_id}, order_by="published_at")
    event = store.get(ReviewEvent, event_id=stable_id("decided", decision.decision_id))
    detail = (event.detail or {}) if event else {}
    retracted = [p for p in (store.get(Publication, publication_id=i) for i in detail.get("retracted", []))
                 if p is not None]
    new_result = None
    if decision.action == ReviewAction.CORRECT:
        new_obs = detail.get("new_observation_id")
        if new_obs:
            found = store.read(KpiResult, "observation_id = :o", {"o": new_obs}, order_by="created_at DESC", limit=1)
            new_result = found[0] if found else None
            if new_result is not None:
                pubs = pubs or store.read(Publication, "result_id = :r", {"r": new_result.result_id},
                                          order_by="published_at")
    return DecisionOutcome(decision=decision, publication=pubs[-1] if pubs else (retracted[0] if retracted else None),
                           new_result=new_result, message="This decision was already recorded; nothing changed.",
                           retracted=retracted, replayed=True)


def natural_key(brand: str, result: KpiResult, kpi_version: int) -> tuple:
    """(brand, kpi_id, kpi_version, scope_key, period_grain, period_start, period_end)."""
    return (brand, result.kpi_id, int(kpi_version), result.scope_key, result.period_grain, result.period_start,
            result.period_end)


def display_label(result: KpiResult, value_corrected: bool = False) -> PublicationLabel:
    """Consumer-facing label from the machine status and how the value was approved."""
    status = result.validation_status
    if status == ValidationStatus.VERIFIED:
        return PublicationLabel.MATCHES_PLATFORM
    if value_corrected:
        return PublicationLabel.CORRECTED
    if status == ValidationStatus.REPORT_ONLY:
        return PublicationLabel.REPORT_ONLY
    if status in (ValidationStatus.VARIANCE_REVIEW, ValidationStatus.INCOMPATIBLE_DEFINITION):
        return PublicationLabel.ACCEPTED_VARIANCE
    if status == ValidationStatus.REPORT_CONFLICT:
        return PublicationLabel.ACCEPTED_VARIANCE if result.platform_value is not None else PublicationLabel.REPORT_ONLY
    if status == ValidationStatus.UNMAPPED_CAPTURED:
        return PublicationLabel.CAPTURED
    raise DecisionError(f"'{STATUS_INFO[status].label}' results are not published.")


def definition_variant(result: KpiResult, kdef) -> dict[str, str]:
    """The definition choices that differ from the KPI's governed defaults (``{}`` = governed definition)."""
    if kdef is None or kdef.platform is None:
        return {}
    defaults = {name: p.default for name, p in kdef.platform.params.items()}
    return {k: v for k, v in (result.definition_choices or {}).items() if k in defaults and v != defaults[k]}


def _current_publications_of(store, result_id: str) -> list[Publication]:
    return store.read(Publication, "result_id = :r AND retracted = FALSE AND superseded_by IS NULL",
                      {"r": result_id}, order_by="published_at")


def _current_for_key(store, key: tuple) -> list[Publication]:
    brand, kpi_id, kpi_version, scope, grain, start, end = key
    rows = store.read(Publication, "brand = :b AND kpi_id = :k AND kpi_version = :v AND scope_key = :s "
                                   "AND retracted = FALSE AND superseded_by IS NULL",
                      {"b": brand, "k": kpi_id, "v": kpi_version, "s": scope})
    return [p for p in rows if (p.period_grain, p.period_start, p.period_end) == (grain, start, end)]


def publish(store, registry, result: KpiResult, decision: ReviewDecision) -> Publication:
    """Publish ``result`` under ``decision``; idempotent on ``publication_id``."""
    if not result.kpi_id:
        raise DecisionError("A value needs a KPI before it can be published.")
    if result.report_value is None:
        raise DecisionError("There is no value to publish.")
    observation = store.get(Observation, observation_id=result.observation_id)
    value = store.get(ExtractedValue, value_id=observation.value_id) if observation else None
    document = store.get(Document, doc_id=result.doc_id)
    brand = result.brand or (document.brand if document else None) or (observation.brand if observation else None)
    if not brand:
        raise DecisionError("The report's brand is unknown; confirm the report metadata first.")
    kdef = registry.kpis.get(result.kpi_id) if registry is not None else None
    kpi_version = result.kpi_version if result.kpi_version is not None else (kdef.version if kdef else None)
    if kpi_version is None:
        raise DecisionError(f"Unknown KPI version for {result.kpi_id}.")
    key = natural_key(brand, result, kpi_version)
    publication_id = stable_id(*key, decision.decision_id)
    existing = store.get(Publication, publication_id=publication_id)
    if existing is not None:
        return existing
    corrected = bool(value is not None and value.method == ExtractionMethod.REVIEWER_CORRECTION)
    value_corrected = corrected and bool((value.context or {}).get("value_corrected"))
    artifact = store.get(SqlArtifact, artifact_id=result.sql_artifact_id) if result.sql_artifact_id else None
    link = {} if definition_variant(result, kdef) else ((kdef.harness_link if kdef else None) or {})
    locator = {**(value.locator if value else {}), "page": value.slide_or_page if value else None}
    now = utcnow()
    pub = Publication(
        publication_id=publication_id, brand=brand, kpi_id=result.kpi_id, kpi_version=int(kpi_version),
        scope_key=result.scope_key, period_start=result.period_start, period_end=result.period_end,
        period_grain=result.period_grain, period_label=observation.period_label if observation else None,
        value=float(result.report_value), unit=result.unit, display_label=display_label(result, value_corrected),
        validation_status=result.validation_status,
        review_action=ReviewAction.CORRECT if corrected else ReviewAction.ACCEPT,
        platform_value=result.platform_value, variance_rel=result.rel_diff, tolerance=dict(result.tolerance or {}),
        definition_hash=result.definition_hash, sql_artifact_id=result.sql_artifact_id,
        sql_sha=artifact.sql_sha if artifact else None,
        evidence_uri=document.file_path if document else "", evidence_hash=result.doc_id, evidence_locator=locator,
        extraction_method=value.method if value else result.extraction_method, result_id=result.result_id,
        decision_id=decision.decision_id, approved_by=decision.decided_by, approved_at=decision.decided_at,
        published_at=now, harness_metric_id=link.get("metric_id"), harness_variant_id=link.get("variant_id"),
        harness_source=link.get("source"), retracted=False, superseded_by=None, rationale=decision.rationale or None)
    with store.transaction():
        previous = [p for p in _current_for_key(store, key) if p.publication_id != publication_id]
        store.insert_new([pub])
        for p in previous:
            p.superseded_by = publication_id
        if previous:
            store.upsert(previous)
        store.insert_new([ReviewEvent(
            event_id=stable_id("published", publication_id), result_id=result.result_id, doc_id=result.doc_id,
            event="published", actor=decision.decided_by, event_at=now,
            detail={"publication_id": publication_id, "decision_id": decision.decision_id,
                    "display_label": pub.display_label.value, "value": pub.value,
                    "superseded": [p.publication_id for p in previous]},
            request_id=decision.request_id)])
    return pub


def retract(store, publication_id: str, actor: str, rationale: str, *, request_id: str | None = None) -> Publication:
    """Withdraw a publication: same row, ``retracted=True`` (append-only log, no new row), plus an event."""
    rationale = (rationale or "").strip()
    if not rationale:
        raise DecisionError("Add a short rationale for the retraction.")
    if not (actor or "").strip():
        raise DecisionError("A retraction needs the reviewer's identity.")
    pub = store.get(Publication, publication_id=publication_id)
    if pub is None:
        raise KeyError(f"unknown publication {publication_id}")
    if pub.retracted:
        return pub
    pub.retracted = True
    with store.transaction():
        store.upsert([pub])
        store.insert_new([ReviewEvent(
            event_id=stable_id("retracted", publication_id), result_id=pub.result_id, doc_id=pub.evidence_hash,
            event="retracted", actor=actor, event_at=utcnow(),
            detail={"publication_id": publication_id, "rationale": rationale, "decision_id": pub.decision_id},
            request_id=request_id)])
    return pub


def policy_version(registry) -> str:
    return str((registry.policy or {}).get("version", "v1"))


def auto_policy_actor(registry) -> str:
    return f"system:auto-policy@{policy_version(registry)}"


@dataclass
class _Context:
    observations: dict[str, Observation]
    values: dict[str, ExtractedValue]
    documents: dict[str, Document]
    mappings: dict[str, KpiMapping]
    decided: set[tuple[str, int]]
    hidden_pages: set[tuple[str, int]]
    human_accepted: set[tuple[str, str]]
    kpis: dict[str, Any] = field(default_factory=dict)

    def brand(self, result: KpiResult) -> str | None:
        obs = self.observations.get(result.observation_id)
        doc = self.documents.get(result.doc_id)
        return result.brand or (doc.brand if doc else None) or (obs.brand if obs else None)

    def key(self, result: KpiResult) -> tuple | None:
        kdef = self.kpis.get(result.kpi_id or "")
        version = result.kpi_version if result.kpi_version is not None else (kdef.version if kdef else None)
        brand = self.brand(result)
        if not brand or not result.kpi_id or version is None:
            return None
        return natural_key(brand, result, version)


def _read_in(store, cls: type, column: str, ids: list[Any], chunk: int = 200) -> list[Any]:
    out: list[Any] = []
    unique = sorted({i for i in ids if i is not None})
    for start in range(0, len(unique), chunk):
        marks, params = in_list("i", unique[start:start + chunk])
        out += store.read(cls, f"{column} IN ({marks})", params)
    return out


def _load_context(store, registry, results: list[KpiResult]) -> _Context:
    observations = {o.observation_id: o for o in _read_in(store, Observation, "observation_id",
                                                          [r.observation_id for r in results])}
    values = {v.value_id: v for v in _read_in(store, ExtractedValue, "value_id",
                                              [o.value_id for o in observations.values()])}
    documents = {d.doc_id: d for d in _read_in(store, Document, "doc_id", [r.doc_id for r in results])}
    mappings: dict[str, KpiMapping] = {}
    version = registry.version
    for m in sorted(_read_in(store, KpiMapping, "observation_id", list(observations)), key=lambda m: m.decided_at):
        if m.observation_id not in mappings or m.registry_version == version or \
                mappings[m.observation_id].registry_version != version:
            mappings[m.observation_id] = m
    decided = {(d.result_id, d.result_version) for d in _read_in(store, ReviewDecision, "result_id",
                                                                 [r.result_id for r in results])}
    hidden: set[tuple[str, int]] = set()
    for p in _read_in(store, DocumentPage, "doc_id", list(documents)):
        if p.hidden:
            hidden.add((p.doc_id, p.page_number))
    accepted = store.query(
        f"SELECT DISTINCT o.fingerprint, r.kpi_id FROM {store.fq('review_decisions')} x "
        f"JOIN {store.fq('kpi_results')} r ON r.result_id = x.result_id "
        f"JOIN {store.fq('observations')} o ON o.observation_id = r.observation_id "
        f"WHERE x.action = 'ACCEPT' AND x.decision_source = 'HUMAN' AND r.kpi_id IS NOT NULL")
    human = {(str(a.fingerprint), str(a.kpi_id)) for a in accepted.itertuples()}
    return _Context(observations, values, documents, mappings, decided, hidden, human, dict(registry.kpis))


def _check(result: KpiResult, ctx: _Context, policy: dict[str, Any]) -> PolicyCheck:
    reasons: list[str] = []
    status = result.validation_status
    rule = None
    if status == ValidationStatus.VERIFIED:
        rule = "verified"
        if not policy.get("verified", False):
            reasons.append("policy_disabled")
    elif status == ValidationStatus.REPORT_ONLY:
        rule = "report_only_known_layout"
        if not policy.get("report_only_known_layout", False):
            reasons.append("policy_disabled")
    else:
        return PolicyCheck(result.result_id, None, False, ["status_needs_person"])
    if not result.is_current:
        reasons.append("not_current")
    if (result.result_id, result.result_version) in ctx.decided:
        reasons.append("already_decided")
    if not result.kpi_id:
        reasons.append("missing_kpi")
    obs = ctx.observations.get(result.observation_id)
    value = ctx.values.get(obs.value_id) if obs else None
    doc = ctx.documents.get(result.doc_id)
    if not ctx.brand(result):
        reasons.append("missing_brand")
    if definition_variant(result, ctx.kpis.get(result.kpi_id or "")):
        reasons.append("non_default_definition")
    method = value.method if value is not None else result.extraction_method
    trusted = method in STRUCTURED_METHODS or method == ExtractionMethod.REVIEWER_CORRECTION
    if policy.get("require_structured_extraction", True) and not trusted:
        reasons.append("extraction_not_structured")
    mapping = ctx.mappings.get(result.observation_id)
    allowed = {str(m).upper() for m in policy.get("allow_mapping_methods", ["SYNONYM", "MEMORY", "REVIEWER"])}
    if mapping is None or mapping.status != MappingStatus.ACCEPTED or mapping.method is None \
            or mapping.method.value not in allowed:
        reasons.append("mapping_method_not_allowed")
    if rule == "verified" and result.dq_status != "pass":
        reasons.append("dq_not_passed")
    if policy.get("exclude_hidden_slides", True):
        page = value.slide_or_page if value is not None else None
        hidden = bool(value is not None and ((value.context or {}).get("hidden")
                                             or _has_flag(value.flags, "HIDDEN_SLIDE")))
        if hidden or (page is not None and (result.doc_id, int(page)) in ctx.hidden_pages):
            reasons.append("hidden_slide")
    if doc is None or doc.status == DocumentStatus.SUPERSEDED or doc.superseded_by_doc_id:
        reasons.append("document_superseded")
    if rule == "report_only_known_layout":
        if obs is None or (obs.fingerprint, result.kpi_id) not in ctx.human_accepted:
            reasons.append("no_prior_human_accept")
    return PolicyCheck(result.result_id, rule, not reasons, reasons)


_BASIS_RANK = {MatchBasis.EXACT: 0, MatchBasis.WITHIN_ROUNDING: 1, MatchBasis.WITHIN_TOLERANCE: 2}


def _half_unit(result: KpiResult) -> float:
    raw = (result.tolerance or {}).get("half_unit")
    return float(raw) if raw is not None and not is_missing(raw) else float("inf")


def _precision_rank(result: KpiResult) -> tuple:
    created = result.created_at.timestamp() if result.created_at else 0.0
    return (_half_unit(result), _BASIS_RANK.get(result.match_basis, 3), -created, result.result_id)


def _equivalent(result: KpiResult, pub: Publication) -> bool:
    v = float(result.report_value)
    allowed = max(_half_unit(result) if _half_unit(result) != float("inf") else 0.0, 1e-9 * max(1.0, abs(v)))
    return abs(pub.value - v) <= allowed


def policy_checks(store, registry, results: list[KpiResult]) -> list[PolicyCheck]:
    """Evaluate the auto-publication policy for each result without writing anything."""
    if not results:
        return []
    policy = dict((registry.policy or {}).get("auto_publish") or {})
    ctx = _load_context(store, registry, results)
    checks = [_check(r, ctx, policy) for r in results]
    groups: dict[tuple, list[tuple[PolicyCheck, KpiResult]]] = {}
    for check, result in zip(checks, results):
        key = ctx.key(result) if check.eligible else None
        if key is not None:
            groups.setdefault(key, []).append((check, result))
    for key, members in groups.items():
        members.sort(key=lambda m: _precision_rank(m[1]))
        for check, _result in members[1:]:
            check.eligible = False
            check.reasons.append("another_value_for_same_key")
        best_check, best = members[0]
        if any(_equivalent(best, p) for p in _current_for_key(store, key)):
            best_check.eligible = False
            best_check.reasons.append("already_published")
    return checks


def auto_publish(store, registry, settings, results: list[KpiResult], run_id: str | None) -> list[Publication]:
    """Publish what the versioned policy allows (``registry.policy['auto_publish']``)."""
    results = [r for r in results if r is not None]
    checks = policy_checks(store, registry, results)
    actor = auto_policy_actor(registry)
    version = policy_version(registry)
    published: list[Publication] = []
    by_id = {r.result_id: r for r in results}
    for check in checks:
        if not check.eligible:
            continue
        result = by_id[check.result_id]
        rationale = _auto_rationale(store, result, check, version)
        decision = ReviewDecision(
            decision_id=stable_id("auto", result.result_id, result.result_version, version),
            result_id=result.result_id, result_version=result.result_version, action=ReviewAction.ACCEPT,
            rationale=rationale, decided_by=actor, decision_source=DecisionSource.AUTO_POLICY, decided_at=utcnow(),
            evidence_hash=evidence_hash(result), policy_version=version, request_id=run_id)
        try:
            with store.transaction():
                store.insert_new([decision])
                store.insert_new([_decided_event(store, decision, result, {"rule": check.rule})])
                published.append(publish(store, registry, result, decision))
        except Exception as exc:
            store.insert_new([ReviewEvent(
                event_id=stable_id("publish_failed", decision.decision_id), result_id=result.result_id,
                doc_id=result.doc_id, event="publish_failed", actor=actor, event_at=utcnow(),
                detail={"error": f"{type(exc).__name__}: {exc}", "rule": check.rule}, request_id=run_id)])
    return published


def _auto_rationale(store, result: KpiResult, check: PolicyCheck, version: str) -> str:
    if check.rule == "verified":
        text = f"Auto-policy {version}: matches the platform under the governed definition"
        if result.match_basis is not None:
            text += f" ({result.match_basis.value.lower().replace('_', ' ')})"
    else:
        text = f"Auto-policy {version}: report-only KPI on a layout a reviewer accepted before"
    obs = store.get(Observation, observation_id=result.observation_id)
    value = store.get(ExtractedValue, value_id=obs.value_id) if obs else None
    if value is not None and value.method == ExtractionMethod.REVIEWER_CORRECTION:
        text += f"; corrected by {(value.context or {}).get('corrected_by', 'a reviewer')}"
    return text + "."


__all__ = [
    "ACCEPTABLE", "DecisionError", "DecisionOutcome", "Evidence", "PolicyCheck", "StaleResult",
    "auto_publish", "build_correction", "decide", "definition_variant", "display_label", "evidence", "evidence_hash",
    "natural_key",
    "policy_checks", "prior_decisions", "publish", "record_opened", "retract", "review_queue",
]
