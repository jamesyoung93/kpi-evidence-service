from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "samples"
EXAMPLE_CSV = SAMPLES / "kpi_table_example.csv"
EXAMPLE_XLSX = SAMPLES / "kpi_table_example.xlsx"
EXAMPLE_MD = SAMPLES / "KPI_TABLE.md"
REPORTS = SAMPLES / "reports"
ACCOUNT = "platform_demo.rx_account_monthly"
PANEL = "platform_demo.rx_panel_monthly"
JUNE = date(2026, 6, 30)
MAY = date(2026, 5, 31)


def row(**fields: Any) -> dict[str, Any]:
    base = {"kpi": "trx", "table": ACCOUNT, "value_sql": "SUM(trx_units)", "date_column": "month",
            "date_format": "yyyy-MM"}
    base.update(fields)
    return {k: v for k, v in base.items() if v is not None}


def table(*rows: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def messages_for(hints, row_number: int, level: str | None = None) -> list[str]:
    m = hints.messages
    m = m[m["row"] == row_number]
    if level:
        m = m[m["level"] == level]
    return list(m["message"])


def compute(store, settings, kpi, as_of: date, *, grain: str = "month", scope: dict | None = None,
            choices: dict | None = None, brand: str = "KESTREL") -> float:
    from kpi_evidence.compile import compile_kpi

    compiled = compile_kpi(kpi, brand=brand, scope=scope or {}, grain=grain, settings=settings)
    return compiled.spec.compute(store.runner(), choices or compiled.governed_defaults, as_of.isoformat())[0]


@dataclass
class SampleRun:
    store: Any
    registry: Any
    hints: Any
    results: pd.DataFrame


def run_samples(settings, *, kpi_table: Path = EXAMPLE_CSV) -> SampleRun:
    from kpi_evidence import intake, pipeline, platform
    from kpi_evidence.contracts import Document, DocumentStatus
    from kpi_evidence.hints import load_kpi_table, merge_registry, parse_hints, scope_values_sql
    from kpi_evidence.registry import load_registry
    from kpi_evidence.store.duckdb_store import DuckDBStore

    base = load_registry(ROOT / "registry")
    hints = parse_hints(load_kpi_table(kpi_table), base_registry=base)
    registry = merge_registry(base, hints)
    store = DuckDBStore(settings, ":memory:")
    run_id = "hints-e2e"
    pipeline.bootstrap(store, registry, settings, load_demo=True, run_id=run_id)
    files = sorted(p for p in REPORTS.iterdir() if p.suffix.lower() in (".pptx", ".pdf"))
    docs = pipeline.register_paths(store, settings, files)
    dq = pipeline.run_dq(store, registry, settings, run_id)
    dims = platform.dimension_values(store, settings)
    for dim, sql in scope_values_sql(hints):
        values = store.platform_access.query(sql)["v"].dropna().astype(str)
        dims.setdefault(dim, set()).update(v for v in values if v.strip())
    for doc in sorted(docs, key=lambda d: (d.registered_at, d.file_name)):
        doc = store.get(Document, doc_id=doc.doc_id)
        if doc.status == DocumentStatus.REGISTERED:
            doc = pipeline.extract_stage(store, registry, settings, doc, run_id)
        if doc.status == DocumentStatus.READY:
            pipeline.process_stage(store, registry, settings, doc, run_id, dq_status=dq, dimension_values=dims)
    labels = json.loads((REPORTS / "labels.json").read_text(encoding="utf-8"))
    for meta in labels["documents"]:
        doc = next((d for d in store.read(Document) if d.file_name == meta["file"]), None)
        if doc is None or doc.status != DocumentStatus.AWAITING_CONFIRMATION:
            continue
        fields = {k: meta[k] for k in ("brand", "report_type", "period_label", "period_grain") if meta.get(k)}
        fields.update({k: date.fromisoformat(meta[k]) for k in ("period_start", "period_end") if meta.get(k)})
        intake.confirm_metadata(store, registry, settings, doc.doc_id, fields, "hints-test")
        doc = store.get(Document, doc_id=doc.doc_id)
        pipeline.process_stage(store, registry, settings, doc, run_id, dq_status=dq, dimension_values=dims)
    results = store.query(
        f"SELECT d.file_name, o.label, o.value AS report_value, r.kpi_id, r.validation_status, r.reason_code, "
        f"r.reason_detail, r.period_end, r.period_grain, r.scope_key, r.platform_value, r.definition_hash, "
        f"r.explanation FROM {store.fq('kpi_results')} r "
        f"JOIN {store.fq('documents')} d ON d.doc_id = r.doc_id "
        f"JOIN {store.fq('observations')} o ON o.observation_id = r.observation_id "
        f"WHERE r.is_current = TRUE")
    return SampleRun(store=store, registry=registry, hints=hints, results=results)
