from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kpi_evidence import pipeline
from kpi_evidence.contracts import Document, DocumentStatus
from kpi_evidence.eval.evaluate import _confirm_fields, confirm_pending, report_paths
from kpi_evidence.eval.metrics import load_labels
from kpi_evidence.ids import sha256_bytes
from kpi_evidence.intake import confirm_metadata
from kpi_evidence.report import UNCONFIRMED_ACTOR, build_result
from kpi_evidence.settings import Settings, demo_dir
from kpi_evidence.store.duckdb_store import DuckDBStore

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = ROOT / ".kpi_evidence_scratch" / "report"
UNCONFIRMED_FILE = "kestrel-update.pptx"


@pytest.fixture(scope="session")
def report_run(tmp_path_factory, registry):
    tmp = tmp_path_factory.mktemp("report_run")
    settings = Settings.from_env(local_dir=tmp / ".kpi_evidence", ai_mode="off", registry_dir=ROOT / "registry")
    store = DuckDBStore(settings, ":memory:")
    reports = demo_dir("reports")
    labels = load_labels(reports / "labels.json")
    paths = report_paths(labels, reports)
    summary = pipeline.run_local(settings, paths, store=store, publish=False)
    files = {p.name: sha256_bytes(p.read_bytes()) for p in paths}
    doc_id = files[UNCONFIRMED_FILE]
    doc = store.get(Document, doc_id=doc_id)
    if doc is not None and doc.status == DocumentStatus.AWAITING_CONFIRMATION:
        meta = next(m for m in labels.documents if m["file"] == UNCONFIRMED_FILE)
        confirm_metadata(store, registry, settings, doc_id, _confirm_fields(meta), UNCONFIRMED_ACTOR)
        pipeline.process_document(store, registry, settings, doc_id, summary.run_id, publish=False)
    confirm_pending(store, registry, settings, labels, reports, summary.run_id, pipeline)
    return SimpleNamespace(store=store, settings=settings, registry=registry, doc_ids=list(files.values()),
                           run_id=summary.run_id, files=files)


@pytest.fixture(scope="session")
def report_result(report_run):
    r = report_run
    return build_result(r.store, r.registry, r.settings, r.doc_ids, run_id=r.run_id,
                        platform_description="local DuckDB demo platform", timings={"total_seconds": 1.5})
