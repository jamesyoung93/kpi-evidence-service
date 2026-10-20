from __future__ import annotations

import contextlib
import json
import traceback
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, tracing
from .contracts import (
    Document, DocumentPage, DocumentStatus, ExtractedValue, ExtractionIssue, KpiMapping, KpiResult, Observation,
    RunLog, RunStatus, ValidationStatus, STATUS_INFO,
)
from .ids import new_uuid, utcnow
from .registry import Registry, load_registry
from .settings import Settings
from .store.base import Store
from .store.files import files_for

TERMINAL_DOC_STATES = {DocumentStatus.UNSUPPORTED_FORMAT, DocumentStatus.ENCRYPTED, DocumentStatus.SUPERSEDED}


@dataclass
class StageReport:
    doc_id: str
    file_name: str
    status: str
    counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RunSummary:
    run_id: str
    documents: list[StageReport]
    status_counts: dict[str, int]
    published: int
    db_path: str | None = None

    def as_text(self) -> str:
        lines = [f"run {self.run_id}: {len(self.documents)} document(s), {self.published} value(s) published"]
        for d in self.documents:
            extra = f" - {d.error}" if d.error else ""
            lines.append(f"  {d.file_name}: {d.status} {json.dumps(d.counts, sort_keys=True)}{extra}")
        lines.append("  results: " + ", ".join(f"{k}={v}" for k, v in sorted(self.status_counts.items())))
        return "\n".join(lines)


def open_store(settings: Settings, *, spark=None) -> Store:
    if settings.is_local:
        from .store.duckdb_store import DuckDBStore

        return DuckDBStore(settings)
    from .store.databricks_store import DatabricksStore

    return DatabricksStore(settings, spark=spark)


def new_run_id() -> str:
    return f"{utcnow():%Y%m%dT%H%M%SZ}-{new_uuid()[:8]}"


@contextlib.contextmanager
def logged_stage(store: Store, run_id: str, stage: str, unit: str = "*") -> Iterator[dict[str, int]]:
    """Record RUNNING then SUCCEEDED/FAILED for one stage unit; yields a counts dict to fill in."""
    started = utcnow()
    counts: dict[str, int] = {}
    store.upsert([RunLog(run_id=run_id, stage=stage, unit=unit, status=RunStatus.RUNNING, started_at=started,
                         finished_at=None, code_version=__version__)])
    try:
        with tracing.span(f"kpi_evidence.{stage}", unit=unit, run_id=run_id):
            yield counts
    except Exception as exc:
        store.upsert([RunLog(run_id=run_id, stage=stage, unit=unit, status=RunStatus.FAILED, started_at=started,
                             finished_at=utcnow(), code_version=__version__, counts=counts,
                             error=f"{type(exc).__name__}: {exc}")])
        raise
    store.upsert([RunLog(run_id=run_id, stage=stage, unit=unit, status=RunStatus.SUCCEEDED, started_at=started,
                         finished_at=utcnow(), code_version=__version__, counts=counts)])


def _fail_document(store: Store, doc: Document, stage: str, exc: BaseException, run_id: str) -> Document:
    doc.status = DocumentStatus.FAILED
    doc.failed_stage = stage
    doc.error = f"{type(exc).__name__}: {exc}"[:2000]
    doc.updated_at = utcnow()
    doc.last_run_id = run_id
    store.upsert([doc])
    return doc


def sync_registry(store: Store, registry: Registry) -> dict[str, int]:
    """Mirror the Git registry into the store (version-1 config rows; app edits use later versions)."""
    from .registry import lint

    problems = lint(registry)
    if problems:
        raise ValueError("registry lint failed:\n  " + "\n  ".join(problems))
    rows = registry.registry_rows()
    store.upsert(rows)
    brands, goals, rtypes = registry.config_rows()
    store.insert_new(brands)
    store.insert_new(goals)
    store.insert_new(rtypes)
    return {"kpis": len(rows), "brands": len(brands), "goal_list": len(goals), "report_types": len(rtypes)}


def bootstrap(store: Store, registry: Registry, settings: Settings, *, load_demo: bool = False,
              run_id: str | None = None) -> dict[str, Any]:
    """Create tables and views, mirror the registry, optionally load the demo platform + governed views."""
    from . import platform

    run_id = run_id or new_run_id()
    store.ensure_schema()
    out: dict[str, Any] = {"run_id": run_id}
    if load_demo:
        out["platform_tables"] = platform.load_demo_platform(store, settings)
    out["governed_views"] = platform.create_governed_views(store, settings)
    store.ensure_views()
    out["registry"] = sync_registry(store, registry)
    return out


def run_dq(store: Store, registry: Registry, settings: Settings, run_id: str) -> dict[str, str]:
    from . import platform

    with logged_stage(store, run_id, "dq") as counts:
        results = platform.run_dq_checks(store, registry, settings, run_id)
        counts["checks"] = len(results)
        counts["failed"] = sum(1 for r in results if not r.passed)
    return platform.dq_status_by_view(store, settings)


def register_paths(store: Store, settings: Settings, paths: Iterable[str | Path], *, uploaded_by: str | None = None,
                   copy_into_volume: bool = True) -> list[Document]:
    from .intake import register_file

    docs = []
    for path in paths:
        p = Path(path)
        blob = p.read_bytes()
        docs.append(register_file(store, settings, blob=blob, file_name=p.name, uploaded_by=uploaded_by,
                                  source_path=None if copy_into_volume else str(p)))
    return docs


def register_inbox(store: Store, settings: Settings, run_id: str) -> list[Document]:
    from .intake import register_inbox as _register_inbox

    with logged_stage(store, run_id, "register") as counts:
        docs = _register_inbox(store, settings)
        counts["registered"] = len(docs)
    return docs


def extract_stage(store: Store, registry: Registry, settings: Settings, doc: Document, run_id: str) -> Document:
    """Bronze extraction + metadata proposal."""
    from .extract import extract_document
    from .intake import apply_proposals, propose_metadata

    if doc.status in TERMINAL_DOC_STATES:
        return doc
    try:
        with logged_stage(store, run_id, "extract", doc.doc_id) as counts:
            blob = files_for(settings).read_bytes(doc.file_path)
            output = extract_document(settings, doc, blob, run_id=run_id)
            if settings.platform_is_databricks and settings.ai_mode != "off":
                output = _maybe_ai_parse(store, settings, doc, output, run_id)
            if getattr(output, "failed", False):
                raise RuntimeError(getattr(output, "error_text", None) or "nothing could be read from the file")
            store.replace(ExtractedValue, {"doc_id": doc.doc_id}, output.values)
            store.replace(DocumentPage, {"doc_id": doc.doc_id}, output.pages)
            store.replace(ExtractionIssue, {"doc_id": doc.doc_id}, extraction_issues(doc.doc_id, output.issues, run_id))
            counts["values"] = len(output.values)
            counts["pages"] = len(output.pages)
            counts["issues"] = len(output.issues)
            doc.status = DocumentStatus.EXTRACTED
            doc.updated_at = utcnow()
            doc.last_run_id = run_id
            if output.document_properties:
                doc.metadata_proposals = {**(doc.metadata_proposals or {}), "_document_properties": output.document_properties}
            proposals = propose_metadata(registry, doc, output.pages, output.values)
            if not doc.confirmed_by:
                provider = None
                if settings.ai_mode in ("ai_query", "gateway"):
                    from .ai import get_provider

                    provider = get_provider(settings, store=store)
                doc = apply_proposals(store, registry, settings, doc, proposals, ai_provider=provider)
            else:
                doc.status = DocumentStatus.READY
                store.upsert([doc])
            counts["awaiting_confirmation"] = int(doc.status == DocumentStatus.AWAITING_CONFIRMATION)
    except Exception as exc:
        return _fail_document(store, doc, "extract", exc, run_id)
    return doc


def extraction_issues(doc_id: str, issues: Iterable[dict[str, Any]], run_id: str | None) -> list[ExtractionIssue]:
    """Store records for the issue dicts an extractor returned for one document, in their original order."""
    out = []
    for seq, item in enumerate(issues or []):
        out.append(ExtractionIssue(
            doc_id=doc_id, seq=seq, code=str(item.get("code") or "EXTRACTION_ISSUE"),
            severity=str(item.get("severity") or "warning").lower(), page=_page_number(item.get("page")),
            part=None if item.get("part") is None else str(item["part"])[:500],
            detail=None if item.get("detail") is None else str(item["detail"])[:2000], run_id=run_id))
    return out


def _page_number(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _maybe_ai_parse(store: Store, settings: Settings, doc: Document, output, run_id: str | None = None):
    from .extract.ai_parse import merge_ai_parse, needs_ai_parse, run_ai_parse

    if not needs_ai_parse(output):
        return output
    try:
        return merge_ai_parse(output, run_ai_parse(store, settings, doc, run_id=run_id))
    except Exception as exc:
        output.issues.append({"code": "AI_PARSE_FAILED", "severity": "warning", "page": None, "part": None,
                              "detail": f"{type(exc).__name__}: {exc}"})
        return output


def process_stage(store: Store, registry: Registry, settings: Settings, doc: Document, run_id: str, *,
                  dq_status: dict[str, str] | None = None, provider=None, dimension_values=None,
                  materialize_sql: bool = True, focus_kpis=None) -> list[KpiResult]:
    """Interpret, map, reconcile (focus KPIs only when given), materialize and detect conflicts for one document."""
    from . import platform
    from .ai import get_provider
    from .conflicts import detect_conflicts
    from .mapping import map_observations
    from .materialize import materialize
    from .reconcile import evaluate_document, is_reused
    from .standardize import interpret

    if doc.status not in (DocumentStatus.READY, DocumentStatus.PROCESSED):
        return []
    try:
        with logged_stage(store, run_id, "process", doc.doc_id) as counts:
            values = store.read(ExtractedValue, "doc_id = :d", {"d": doc.doc_id})
            pages = store.read(DocumentPage, "doc_id = :d", {"d": doc.doc_id}, order_by="page_number")
            dims = dimension_values if dimension_values is not None else platform.dimension_values(store, settings)
            observations = interpret(doc, values, pages, registry, settings, dims)
            store.replace(Observation, {"doc_id": doc.doc_id}, observations)
            provider = provider or get_provider(settings, store=store)
            mappings = map_observations(store, registry, settings, observations, provider)
            dq = dq_status if dq_status is not None else platform.dq_status_by_view(store, settings)
            results = evaluate_document(store, registry, settings, doc, observations, mappings, dq, run_id,
                                        provider=provider, focus_kpis=focus_kpis)
            artifacts = materialize(store, registry, settings, results, run_id) if materialize_sql else []
            conflicted = detect_conflicts(store, registry, settings, doc_ids=[doc.doc_id])
            counts.update(observations=len(observations), mappings=len(mappings), results=len(results),
                          sql_artifacts=len(artifacts), conflicts=len(conflicted),
                          reused=sum(1 for r in results if is_reused(r)),
                          outside_focus=len({o.observation_id for o in observations}) - len(results))
            for r in results:
                key = r.validation_status.value
                counts[key] = counts.get(key, 0) + 1
            doc.status = DocumentStatus.PROCESSED
            doc.updated_at = utcnow()
            doc.last_run_id = run_id
            doc.failed_stage = None
            doc.error = None
            store.upsert([doc])
    except Exception as exc:
        _fail_document(store, doc, "process", exc, run_id)
        return []
    return results


def publish_stage(store: Store, registry: Registry, settings: Settings, run_id: str,
                  doc_ids: list[str] | None = None) -> int:
    """Apply the versioned auto-publication policy to current, undecided results."""
    from .review import auto_publish

    with logged_stage(store, run_id, "publish") as counts:
        where = "is_current = TRUE"
        params: dict[str, Any] = {}
        if doc_ids:
            marks = ", ".join(f":d{i}" for i in range(len(doc_ids)))
            where += f" AND doc_id IN ({marks})"
            params = {f"d{i}": d for i, d in enumerate(doc_ids)}
        results = store.read(KpiResult, where, params)
        pubs = auto_publish(store, registry, settings, results, run_id)
        counts["published"] = len(pubs)
    return len(pubs)


def process_document(store: Store, registry: Registry, settings: Settings, doc_id: str, run_id: str,
                     *, publish: bool = True) -> StageReport:
    doc = store.get(Document, doc_id=doc_id)
    if doc is None:
        raise KeyError(f"unknown document {doc_id}")
    if doc.status == DocumentStatus.REGISTERED or doc.status == DocumentStatus.FAILED and doc.failed_stage == "extract":
        doc = extract_stage(store, registry, settings, doc, run_id)
    results = process_stage(store, registry, settings, doc, run_id) if doc.status == DocumentStatus.READY else []
    if publish and results:
        publish_stage(store, registry, settings, run_id, [doc.doc_id])
    doc = store.get(Document, doc_id=doc_id)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.validation_status.value] = counts.get(r.validation_status.value, 0) + 1
    return StageReport(doc_id=doc.doc_id, file_name=doc.file_name, status=doc.status.value, counts=counts, error=doc.error)


def status_counts(store: Store, doc_ids: list[str] | None = None) -> dict[str, int]:
    where = "is_current = TRUE"
    params: dict[str, Any] = {}
    if doc_ids:
        marks = ", ".join(f":d{i}" for i in range(len(doc_ids)))
        where += f" AND doc_id IN ({marks})"
        params = {f"d{i}": d for i, d in enumerate(doc_ids)}
    df = store.query(f"SELECT validation_status, COUNT(*) AS n FROM {store.fq('kpi_results')} WHERE {where} "
                     f"GROUP BY validation_status", params)
    return {str(r.validation_status): int(r.n) for r in df.itertuples()}


def run_local(settings: Settings | None = None, paths: Iterable[str | Path] | None = None, *,
              include_samples: bool = False, reset: bool = False, publish: bool = True,
              store: Store | None = None) -> RunSummary:
    """Run every stage locally (DuckDB) for the given files (and/or the bundled synthetic reports)."""
    settings = settings or Settings.from_env()
    if reset:
        db = settings.local_db_path()
        if db.exists():
            db.unlink()
    tracing.configure(settings)
    registry = load_registry(settings.resolved_registry_dir())
    store = store or open_store(settings)
    run_id = new_run_id()
    bootstrap(store, registry, settings, load_demo=True, run_id=run_id)
    files = [Path(p) for p in (paths or [])]
    if include_samples:
        from .settings import demo_dir

        files += sorted(p for p in demo_dir("reports").iterdir()
                        if p.suffix.lower() in (".pptx", ".pptm", ".pdf", ".xlsx", ".png", ".jpg", ".jpeg"))
    docs = register_paths(store, settings, files)
    dq = run_dq(store, registry, settings, run_id)
    from . import platform

    dims = platform.dimension_values(store, settings)
    reports = []
    for doc in sorted(docs, key=lambda d: (d.registered_at, d.file_name)):
        doc = store.get(Document, doc_id=doc.doc_id)
        if doc.status in (DocumentStatus.REGISTERED,) or (doc.status == DocumentStatus.FAILED and doc.failed_stage == "extract"):
            doc = extract_stage(store, registry, settings, doc, run_id)
        if doc.status == DocumentStatus.READY:
            process_stage(store, registry, settings, doc, run_id, dq_status=dq, dimension_values=dims)
        doc = store.get(Document, doc_id=doc.doc_id)
        reports.append(StageReport(doc_id=doc.doc_id, file_name=doc.file_name, status=doc.status.value, error=doc.error))
    published = publish_stage(store, registry, settings, run_id, [d.doc_id for d in docs]) if publish else 0
    for rep in reports:
        final = store.get(Document, doc_id=rep.doc_id)
        rep.status, rep.error = final.status.value, final.error
        rep.counts = status_counts(store, [rep.doc_id])
    db_path = str(settings.local_db_path()) if settings.is_local else None
    return RunSummary(run_id=run_id, documents=reports, status_counts=status_counts(store, [d.doc_id for d in docs]),
                      published=published, db_path=db_path)


def status_label(status: str) -> str:
    try:
        return STATUS_INFO[ValidationStatus(status)].label
    except (ValueError, KeyError):
        return status


def describe_failure(exc: BaseException) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()
