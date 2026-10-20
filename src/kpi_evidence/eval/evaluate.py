from __future__ import annotations

import dataclasses
import importlib
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .. import __version__
from ..contracts import Document, DocumentStatus, EvalRun
from ..ids import json_safe, sha256_bytes, stable_id, utcnow
from ..settings import Settings
from .metrics import LabelSet, compute_metrics, goal_list_coverage, load_labels, metric_rows

EVAL_ACTOR = "eval:labels"
EVAL_DB_FILE = "kpi_evidence_eval.duckdb"


@dataclass
class Confirmation:
    file: str
    doc_id: str
    status_before: str
    status_after: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _import_pipeline():
    try:
        return importlib.import_module("kpi_evidence.pipeline")
    except ImportError as exc:
        raise RuntimeError(
            "kpi_evidence.pipeline is not available; the evaluation runs the full pipeline "
            f"(pipeline.run_local) over the labeled report set ({exc})") from exc


def report_paths(labelset: LabelSet, reports_dir: Path) -> list[Path]:
    """Report files of the label set, in file-name order (oldest version first within a family)."""
    paths = [reports_dir / f for f in sorted(labelset.files)]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"labeled reports missing in {reports_dir}: {missing}; regenerate them with "
            "kpi_evidence.eval.synth_reports.generate_all()")
    return paths


def eval_settings(settings: Settings) -> Settings:
    """A copy of ``settings`` whose local DuckDB file and volume live under ``<local_dir>/eval``."""
    if not settings.is_local:
        return settings
    return dataclasses.replace(settings, local_dir=Path(settings.local_dir) / "eval", volume_path=None)


def eval_db_path(settings: Settings) -> Path:
    """Local DuckDB file of the evaluation run (``settings`` as returned by ``eval_settings``)."""
    return Path(settings.local_dir) / EVAL_DB_FILE


def open_eval_store(settings: Settings, pipeline: Any, *, reset: bool = True) -> Any:
    """An isolated store for the evaluation: a fresh local DuckDB file, or the pipeline's store on Databricks."""
    if not settings.is_local:
        return pipeline.open_store(settings)
    from ..store.duckdb_store import DuckDBStore

    db = eval_db_path(settings)
    if reset:
        for stale in (db, db.with_name(db.name + ".wal")):
            if stale.exists():
                stale.unlink()
    store = DuckDBStore(settings, db)
    store.ensure_schema()
    return store


def _confirm_fields(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("brand", "report_type", "period_label", "period_grain"):
        if meta.get(key):
            out[key] = meta[key]
    for key in ("period_start", "period_end", "publication_date"):
        if meta.get(key):
            out[key] = date.fromisoformat(meta[key])
    return out


def confirm_pending(store: Any, registry: Any, settings: Settings, labelset: LabelSet, reports_dir: Path,
                    run_id: str, pipeline: Any, *, actor: str = EVAL_ACTOR) -> list[Confirmation]:
    """Confirm labeled metadata for AWAITING_CONFIRMATION documents, then process them."""
    out: list[Confirmation] = []
    for meta in labelset.documents:
        path = reports_dir / meta["file"]
        if not path.exists():
            continue
        doc_id = sha256_bytes(path.read_bytes())
        doc = store.get(Document, doc_id=doc_id)
        if doc is None or doc.status != DocumentStatus.AWAITING_CONFIRMATION:
            continue
        item = Confirmation(file=meta["file"], doc_id=doc_id, status_before=doc.status.value, fields=_confirm_fields(meta))
        try:
            intake = importlib.import_module("kpi_evidence.intake")
            intake.confirm_metadata(store, registry, settings, doc_id, item.fields, actor)
            pipeline.process_document(store, registry, settings, doc_id, run_id)
        except Exception as exc:
            item.error = f"{type(exc).__name__}: {exc}"
        after = store.get(Document, doc_id=doc_id)
        item.status_after = after.status.value if after else None
        out.append(item)
    return out


def _coverage_frame(store: Any, registry: Any, brand: str | None, as_of: str | None) -> tuple[Any, str | None]:
    try:
        serving = importlib.import_module("kpi_evidence.serving")
        frame = serving.coverage(store, registry, brand, as_of=date.fromisoformat(as_of) if as_of else None)
        return frame, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def run_evaluation(settings: Settings, store: Any = None, *, log_to_mlflow: bool | None = None,
                   reports_dir: str | Path | None = None, labels: Any = None, reset: bool = True,
                   actor: str = EVAL_ACTOR) -> EvalRun:
    """Run the pipeline over the labeled report set, score it and record an ``eval_runs`` row."""
    from ..registry import load_registry
    from ..settings import demo_dir

    reports = Path(reports_dir) if reports_dir is not None else demo_dir("reports")
    labelset = load_labels(labels if labels is not None else reports / "labels.json")
    paths = report_paths(labelset, reports)
    pipeline = _import_pipeline()

    run_settings = settings
    if store is None:
        run_settings = eval_settings(settings)
        store = open_eval_store(run_settings, pipeline, reset=reset)
    registry = load_registry(run_settings.resolved_registry_dir())

    started = time.perf_counter()
    summary = pipeline.run_local(run_settings, paths, store=store)
    run_id = getattr(summary, "run_id", None) or stable_id("eval", labelset.sha256, utcnow().isoformat())
    confirmations = confirm_pending(store, registry, run_settings, labelset, reports, run_id, pipeline, actor=actor)
    processing_seconds = time.perf_counter() - started

    brand = labelset.coverage.get("brand")
    as_of = labelset.coverage.get("as_of")
    coverage, coverage_error = _coverage_frame(store, registry, brand, as_of)
    metrics = compute_metrics(store, labelset, coverage=coverage, registry=registry, brand=brand, as_of=as_of)
    if coverage is not None:
        metrics["goal_list_coverage_fallback"] = goal_list_coverage(store, labelset, registry=registry, brand=brand,
                                                                    as_of=as_of)
    matches = metrics.pop("matches")
    metrics["processing_seconds"] = round(processing_seconds, 3)
    metrics["documents"] = len(paths)
    metrics["seconds_per_document"] = round(processing_seconds / max(len(paths), 1), 3)

    details: dict[str, Any] = {
        "run_id": run_id,
        "labels_sha256": labelset.sha256,
        "reports_dir": str(reports),
        "db_path": getattr(store, "path", None) or getattr(summary, "db_path", None),
        "run_summary": _summary_dict(summary),
        "confirmations": [dataclasses.asdict(c) for c in confirmations],
        "coverage_source": "serving.coverage" if coverage is not None else "metrics.fallback",
        "coverage_error": coverage_error,
        "matches": matches,
        "cost": {"billing_join": "system.billing.usage.usage_metadata.job_run_id",
                 "job_run_id": os.environ.get("DATABRICKS_JOB_RUN_ID")},
    }
    should_log = bool(settings.mlflow_experiment) if log_to_mlflow is None else bool(log_to_mlflow)
    details["mlflow"] = log_mlflow(settings, metrics, details, labelset) if should_log else {"logged": False,
                                                                                               "reason": "disabled"}
    row = EvalRun(eval_id=stable_id("eval_run", labelset.sha256, run_id), dataset=f"{labelset.dataset}@{labelset.sha256[:12]}",
                  metrics=json_safe(metrics), details=json_safe(details), code_version=__version__, ran_at=utcnow())
    store.insert([row])
    return row


def _summary_dict(summary: Any) -> dict[str, Any]:
    if summary is None:
        return {}
    if dataclasses.is_dataclass(summary):
        return json_safe(dataclasses.asdict(summary))
    return {"repr": repr(summary)}


def log_mlflow(settings: Settings, metrics: dict[str, Any], details: dict[str, Any], labelset: LabelSet) -> dict[str, Any]:
    """Log one MLflow run (params, flat metrics, JSON artifacts)."""
    try:
        import mlflow
    except ImportError:
        return {"logged": False, "reason": "mlflow is not installed"}
    out: dict[str, Any] = {"logged": False, "genai_evaluate": {"ran": False}}
    try:
        if settings.mlflow_experiment:
            mlflow.set_experiment(settings.mlflow_experiment)
        with mlflow.start_run(run_name=f"kpi-evidence-eval-{details['run_id']}") as run:
            mlflow.log_params({"dataset": labelset.dataset, "labels_sha256": labelset.sha256[:16],
                               "labels": len(labelset.labels), "documents": len(labelset.files),
                               "ai_mode": settings.ai_mode, "backend": settings.backend, "code_version": __version__})
            mlflow.log_metrics(metric_rows(metrics))
            mlflow.log_dict(json_safe(metrics), "eval/metrics.json")
            mlflow.log_dict(json_safe({k: v for k, v in details.items() if k != "matches"}), "eval/details.json")
            mlflow.log_dict(json_safe(details["matches"]), "eval/matches.json")
            out.update(logged=True, mlflow_run_id=getattr(getattr(run, "info", None), "run_id", None))
            out["genai_evaluate"] = genai_evaluate(details["matches"])
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def genai_evaluate(matches: list[dict[str, Any]]) -> dict[str, Any]:
    """Run ``mlflow.genai.evaluate`` with code scorers over the label matches when the API exists."""
    try:
        import mlflow.genai
        from mlflow.genai.scorers import scorer
    except Exception as exc:
        return {"ran": False, "reason": f"mlflow.genai unavailable: {type(exc).__name__}: {exc}"}

    @scorer
    def status_match(outputs: dict, expectations: dict) -> bool:
        return outputs.get("validation_status") == expectations.get("expected_status")

    @scorer
    def kpi_match(outputs: dict, expectations: dict) -> bool:
        return outputs.get("kpi_id") == expectations.get("kpi_id")

    @scorer
    def value_found(outputs: dict) -> bool:
        return bool(outputs.get("value_found"))

    @scorer
    def no_false_validation(outputs: dict, expectations: dict) -> bool:
        return not (outputs.get("validation_status") == "VERIFIED" and expectations.get("expected_status") != "VERIFIED")

    data = [{"inputs": {"label_id": m["label_id"], "file": m["file"], "page": m["page"]},
             "outputs": {"validation_status": m["actual_status"], "kpi_id": m["actual_kpi_id"],
                         "report_value": m["actual_value"], "value_found": m["value_found"]},
             "expectations": {"expected_status": m["expected_status"], "kpi_id": m["kpi_id"],
                              "report_value": m["label_value"]}} for m in matches]
    try:
        result = mlflow.genai.evaluate(data=data, scorers=[status_match, kpi_match, value_found, no_false_validation])
    except Exception as exc:
        return {"ran": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"ran": True, "metrics": json_safe(dict(getattr(result, "metrics", {}) or {}))}
