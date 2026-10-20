from __future__ import annotations

import io

import openpyxl
import pytest

from kpi_evidence import pipeline
from kpi_evidence.contracts import ExtractionIssue
from kpi_evidence.intake import register_file
from kpi_evidence.report import build_result
from kpi_evidence.store.duckdb_store import DuckDBStore


def _formula_workbook() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Kestrel Monthly Brand Review - data through May 2026"
    ws.append([])
    ws.append(["Metric", "May 2026"])
    ws.append(["TRx", 5910])
    ws.append(["NRx", "=B4*0.4"])
    ws.append(["NBRx", "=B5/2"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture()
def extracted(settings, registry):
    store = DuckDBStore(settings, ":memory:")
    store.ensure_schema()
    doc = register_file(store, settings, blob=_formula_workbook(), file_name="formulas.xlsx")
    doc = pipeline.extract_stage(store, registry, settings, doc, "run-1")
    return store, doc


def test_extract_stage_stores_every_issue(extracted):
    store, doc = extracted
    rows = sorted(store.read(ExtractionIssue, "doc_id = :d", {"d": doc.doc_id}), key=lambda r: r.seq)
    assert [r.code for r in rows] == ["FORMULA_RESULT_MISSING", "FORMULA_RESULT_MISSING"]
    assert [r.seq for r in rows] == [0, 1]
    assert {r.severity for r in rows} == {"warning"} and {r.run_id for r in rows} == {"run-1"}
    assert "Summary!B5" in rows[0].detail and "Summary!B6" in rows[1].detail


def test_extraction_again_replaces_the_issues(extracted, registry, settings):
    store, doc = extracted
    pipeline.extract_stage(store, registry, settings, doc, "run-2")
    rows = store.read(ExtractionIssue, "doc_id = :d", {"d": doc.doc_id})
    assert len(rows) == 2 and {r.run_id for r in rows} == {"run-2"}


def test_issues_reach_the_report(extracted, registry, settings):
    store, doc = extracted
    result = build_result(store, registry, settings, [doc.doc_id])
    rows = result.issues[result.issues["category"] == "extraction issue"]
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row["severity"] == "warning" and row["report_file"] == "formulas.xlsx" and row["count"] == 2
    assert row["item"].startswith("FORMULA_RESULT_MISSING (sheet ")
    assert "Summary!B5" in row["message"] and "Summary!B6" in row["message"]
    assert "Excel" in row["remedy"]
    assert result.summary["issues"] == len(result.issues)
    assert "No issues:" not in result.to_html()


def test_info_issues_are_stored_but_not_listed(extracted, registry, settings):
    store, doc = extracted
    store.insert([ExtractionIssue(doc_id=doc.doc_id, seq=9, code="EXTRACTOR_WARNING", severity="info", page=None,
                                  part=None, detail="note", run_id="run-1")])
    result = build_result(store, registry, settings, [doc.doc_id])
    assert "EXTRACTOR_WARNING" not in " ".join(result.issues["item"].astype(str))


def test_issue_records_keep_order_and_tolerate_odd_values():
    issues = [{"code": "PAGE_EXTRACTION_FAILED", "severity": "WARNING", "page": "3", "part": None, "detail": "x"},
              {"severity": "error", "page": "n/a", "part": {"sheet": 1}, "detail": None}]
    first, second = pipeline.extraction_issues("d1", issues, "r1")
    assert (first.seq, first.code, first.severity, first.page) == (0, "PAGE_EXTRACTION_FAILED", "warning", 3)
    assert (second.seq, second.code, second.severity, second.page) == (1, "EXTRACTION_ISSUE", "error", None)
    assert second.part == "{'sheet': 1}" and second.detail is None
