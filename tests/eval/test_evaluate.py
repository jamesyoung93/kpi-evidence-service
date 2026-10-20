from __future__ import annotations

import contextlib
import importlib.util
import inspect
import os
import sys
import types
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pytest

from kpi_evidence.contracts import Document, DocumentStatus, EvalRun
from kpi_evidence.eval import evaluate as ev
from kpi_evidence.eval import synth_reports as sr
from kpi_evidence.ids import sha256_bytes
from kpi_evidence.store.duckdb_store import DuckDBStore


@dataclass
class FakeSummary:
    run_id: str
    documents: list = field(default_factory=list)
    status_counts: dict = field(default_factory=dict)
    published: int = 0
    db_path: str | None = None


def _fake_pipeline(labels: dict, writer_cls, *, awaiting=(sr.UPDATE_DECK,), calls: dict):
    mod = types.ModuleType("kpi_evidence.pipeline")
    state: dict = {}
    final = {d["file"]: d["final_status"] for d in labels["documents"]}

    def writer(store):
        if state.get("store") is not store:
            state.update(store=store, writer=writer_cls(store))
        return state["writer"]

    def open_store(settings):
        store = DuckDBStore(settings)
        store.ensure_schema()
        calls["db"] = str(store.path)
        return store

    def run_local(settings, paths, *, store=None, **_kw):
        calls["paths"] = [Path(p).name for p in paths]
        w = writer(store)
        for path in paths:
            name = Path(path).name
            waiting = name in awaiting
            w.document(name, doc_id=sha256_bytes(Path(path).read_bytes()),
                       status=DocumentStatus.AWAITING_CONFIRMATION if waiting else DocumentStatus(final[name]))
            if not waiting:
                w.batch([lab for lab in labels["labels"] if lab["file"] == name])
        return FakeSummary(run_id="run-fake-1", db_path=calls.get("db"))

    def process_document(store, registry, settings, doc_id, run_id):
        calls.setdefault("processed", []).append(doc_id)
        doc = store.get(Document, doc_id=doc_id)
        w = writer(store)
        w.batch([lab for lab in labels["labels"] if lab["file"] == doc.file_name])
        w.set_status(doc.file_name, DocumentStatus.PROCESSED)

    mod.open_store, mod.run_local, mod.process_document = open_store, run_local, process_document
    return mod


def _fake_intake(calls: dict):
    mod = types.ModuleType("kpi_evidence.intake")

    def confirm_metadata(store, registry, settings, doc_id, fields, actor):
        calls.setdefault("confirmed", []).append({"doc_id": doc_id, "fields": fields, "actor": actor})
        doc = store.get(Document, doc_id=doc_id)
        doc.status = DocumentStatus.READY
        store.upsert([doc])
        return doc

    mod.confirm_metadata = confirm_metadata
    return mod


def _fake_mlflow(calls: dict, *, fail: bool = False):
    mlflow = types.ModuleType("mlflow")
    genai = types.ModuleType("mlflow.genai")
    scorers = types.ModuleType("mlflow.genai.scorers")

    @contextlib.contextmanager
    def start_run(run_name=None):
        if fail:
            raise RuntimeError("tracking server unreachable")
        calls["run_name"] = run_name
        yield types.SimpleNamespace(info=types.SimpleNamespace(run_id="mlrun-1"))

    def evaluate(data, scorers):
        out = {}
        for fn in scorers:
            names = inspect.signature(fn).parameters
            values = [fn(**{k: row[k] for k in names}) for row in data]
            out[f"{fn.__name__}/mean"] = sum(values) / len(values)
        calls["genai_rows"] = len(data)
        return types.SimpleNamespace(metrics=out)

    mlflow.set_experiment = lambda name: calls.__setitem__("experiment", name)
    mlflow.start_run = start_run
    mlflow.log_params = lambda params: calls.__setitem__("params", params)
    mlflow.log_metrics = lambda metrics: calls.__setitem__("metrics", metrics)
    mlflow.log_dict = lambda d, name: calls.setdefault("artifacts", []).append(name)
    genai.evaluate = evaluate
    scorers.scorer = lambda fn: fn
    mlflow.genai, genai.scorers = genai, scorers
    return {"mlflow": mlflow, "mlflow.genai": genai, "mlflow.genai.scorers": scorers}


@pytest.fixture()
def fakes(monkeypatch, synth_set, result_writer_cls):
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "kpi_evidence.pipeline", _fake_pipeline(synth_set["labels"], result_writer_cls, calls=calls))
    monkeypatch.setitem(sys.modules, "kpi_evidence.intake", _fake_intake(calls))
    monkeypatch.setitem(sys.modules, "kpi_evidence.serving", None)
    return calls


def _eval_rows(settings) -> list[EvalRun]:
    run_settings = ev.eval_settings(settings)
    return DuckDBStore(run_settings, ev.eval_db_path(run_settings)).read(EvalRun)


def test_missing_pipeline_is_a_clear_error(monkeypatch, settings, synth_set):
    monkeypatch.setitem(sys.modules, "kpi_evidence.pipeline", None)
    with pytest.raises(RuntimeError, match="pipeline"):
        ev.run_evaluation(settings, reports_dir=synth_set["dir"], log_to_mlflow=False)


def test_missing_reports_are_a_clear_error(settings, tmp_path, synth_set):
    (tmp_path / "labels.json").write_text((synth_set["dir"] / "labels.json").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="generate_all"):
        ev.run_evaluation(settings, reports_dir=tmp_path, log_to_mlflow=False)


def test_run_evaluation_end_to_end_with_fakes(monkeypatch, settings, synth_set, fakes):
    for name, mod in _fake_mlflow(fakes).items():
        monkeypatch.setitem(sys.modules, name, mod)
    settings.mlflow_experiment = "/Shared/kpi-evidence-eval"
    row = ev.run_evaluation(settings, reports_dir=synth_set["dir"])

    assert fakes["paths"] == sorted(sr.REPORT_FILES)
    assert Path(row.details["db_path"]) == Path(settings.local_dir) / "eval" / ev.EVAL_DB_FILE
    (confirmed,) = fakes["confirmed"]
    assert confirmed["fields"]["brand"] == "KESTREL" and confirmed["fields"]["report_type"] == "field_update"
    assert confirmed["fields"]["period_end"] == date(2026, 6, 30) and confirmed["actor"] == ev.EVAL_ACTOR
    assert row.details["confirmations"][0]["status_after"] == "PROCESSED"
    m = row.metrics
    assert m["labels"] == 75 and m["status_accuracy"] == 1.0 and m["false_validation_rate"] == 0
    assert m["goal_list_coverage"]["share_found"] == pytest.approx(13 / 15) and m["documents"] == 5
    assert m["processing_seconds"] >= 0 and row.details["coverage_source"] == "metrics.fallback"
    assert all(d["document_status_ok"] for d in m["per_document"].values())
    assert len(row.details["matches"]) == 75 and row.dataset.startswith(sr.DATASET + "@")
    assert fakes["experiment"] == "/Shared/kpi-evidence-eval" and fakes["params"]["labels"] == 75
    assert fakes["metrics"]["status_accuracy"] == 1.0
    assert fakes["artifacts"] == ["eval/metrics.json", "eval/details.json", "eval/matches.json"]
    assert row.details["mlflow"]["logged"] and row.details["mlflow"]["mlflow_run_id"] == "mlrun-1"
    genai = row.details["mlflow"]["genai_evaluate"]
    assert genai["ran"] and genai["metrics"]["status_match/mean"] == 1.0 and fakes["genai_rows"] == 75
    (stored,) = _eval_rows(settings)
    assert stored.eval_id == row.eval_id and stored.metrics["labels"] == 75


def test_logging_defaults_to_the_experiment_setting(monkeypatch, settings, synth_set, fakes):
    for name, mod in _fake_mlflow(fakes).items():
        monkeypatch.setitem(sys.modules, name, mod)
    assert settings.mlflow_experiment is None
    row = ev.run_evaluation(settings, reports_dir=synth_set["dir"])
    assert row.details["mlflow"] == {"logged": False, "reason": "disabled"} and "metrics" not in fakes


def test_mlflow_failures_are_recorded_not_raised(monkeypatch, settings, synth_set, fakes):
    for name, mod in _fake_mlflow(fakes, fail=True).items():
        monkeypatch.setitem(sys.modules, name, mod)
    row = ev.run_evaluation(settings, reports_dir=synth_set["dir"], log_to_mlflow=True)
    assert row.details["mlflow"]["logged"] is False and "unreachable" in row.details["mlflow"]["error"]
    assert len(_eval_rows(settings)) == 1


def test_confirmation_errors_are_recorded(monkeypatch, settings, synth_set, fakes):
    monkeypatch.setitem(sys.modules, "kpi_evidence.intake", None)
    row = ev.run_evaluation(settings, reports_dir=synth_set["dir"], log_to_mlflow=False)
    (item,) = row.details["confirmations"]
    assert item["file"] == sr.UPDATE_DECK and item["status_after"] == "AWAITING_CONFIRMATION"
    assert "ImportError" in item["error"] or "ModuleNotFoundError" in item["error"]
    assert row.metrics["per_document"][sr.UPDATE_DECK]["matched"] == 0


_SIBLINGS = ("pipeline", "intake", "extract", "standardize", "periods", "mapping", "ai", "platform", "reconcile",
             "materialize", "conflicts", "review", "serving")


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("KPI_EVIDENCE_EVAL_E2E"), reason="set KPI_EVIDENCE_EVAL_E2E=1 to run the real pipeline")
def test_real_pipeline_evaluation(settings):
    missing = [m for m in _SIBLINGS if importlib.util.find_spec(f"kpi_evidence.{m}") is None]
    if missing:
        pytest.skip(f"pipeline modules not available yet: {missing}")
    row = ev.run_evaluation(settings, log_to_mlflow=False)
    m = row.metrics
    print({k: m[k] for k in ("extraction_accuracy", "mapping_accuracy", "status_accuracy", "false_validation_rate")})
    assert m["labels"] == 75 and 0.0 <= m["status_accuracy"] <= 1.0
