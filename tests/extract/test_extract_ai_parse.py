from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from kpi_evidence.contracts import DocumentFormat, ExtractionMethod
from kpi_evidence.extract import ExtractionOutput
from kpi_evidence.extract.ai_parse import (
    build_ai_parse_sql, html_table_grid, merge_ai_parse, needs_ai_parse, parse_ai_document_output, run_ai_parse,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ai_parse_v2_field_update.json").read_text(encoding="utf-8"))
DOC_ID = "a" * 64


def _codes(v):
    return [f["code"] for f in v.flags]


def test_sql_reads_only_the_given_paths():
    sql = build_ai_parse_sql(["/Volumes/main/kpi_evidence/reports/inbox/2026/06/05/abc_scan.pdf",
                              "/Volumes/main/kpi_evidence/reports/inbox/2026/06/05/def_shot.png"])
    assert sql.count("read_files(") == 2 and sql.count("UNION ALL") == 1
    assert "read_files('/Volumes/main/kpi_evidence/reports/inbox/2026/06/05/abc_scan.pdf', format => 'binaryFile')" in sql
    assert "to_json(ai_parse_document(content, map('version', '2.0'" in sql
    assert "'descriptionElementTypes', 'figure'" in sql and "imageOutputPath" not in sql
    assert "sha2(content, 256) AS content_sha256" in sql and "::" not in sql and "`" not in sql


def test_sql_options_and_guards():
    sql = build_ai_parse_sql(["/Volumes/c/s/v/a.pdf"], image_output_path="/Volumes/c/s/v/artifacts/parse_images")
    assert "'imageOutputPath', '/Volumes/c/s/v/artifacts/parse_images/'" in sql
    for bad in ["/Volumes/c/s/v/inbox/", "/Volumes/c/s/v/*.pdf", "/tmp/a.pdf", "/Volumes/c/s/v/it's.pdf", ""]:
        with pytest.raises(ValueError):
            build_ai_parse_sql([bad])
    with pytest.raises(ValueError):
        build_ai_parse_sql(["/Volumes/c/s/v/a.pdf"], version="1.0")
    with pytest.raises(ValueError):
        build_ai_parse_sql([])


@pytest.fixture(scope="module")
def parsed() -> ExtractionOutput:
    return parse_ai_document_output(DOC_ID, FIXTURE, run_id="r1")


def test_table_html_becomes_cells_with_composed_headers(parsed):
    cells = {(v.locator["row"], v.locator["col"]): v for v in parsed.values if v.locator.get("element_id") == 2}
    assert set(cells) == {(2, 1), (2, 2), (3, 1)}
    west = cells[(2, 1)]
    assert west.method == ExtractionMethod.AI_PARSE and (west.raw_text, west.raw_number) == ("1,265.7", 1265.7)
    assert west.context["row_label"] == "West" and west.context["col_header"] == "May 2026 / TRx"
    assert west.context["object_kind"] == "ai_parse" and west.context["element_type"] == "table"
    assert west.confidence == 0.87 and "AI_PARSED" in _codes(west)
    assert west.locator["page"] == 1 and west.locator["bbox"] == [60.0, 300.0, 840.0, 220.0]
    assert "DERIVED_CHANGE" in _codes(cells[(2, 2)])


def test_text_elements_give_labeled_numbers(parsed):
    text = {v.raw_text: v for v in parsed.values if v.locator.get("element_id") == 1}
    assert set(text) == {"5,910", "62%", "184"}
    assert text["5,910"].context["row_label"] == "TRx" and text["5,910"].confidence == 0.91
    assert text["62%"].context["text_line"] == "Brand awareness: 62%"
    assert text["184"].context["row_label"] == "Referrals (projected)" and "LABEL_FROM_ADJACENT_LINE" in _codes(text["184"])
    assert text["62%"].context["page_title"] == "Kestrel Field Update - May 2026"
    assert text["62%"].context["footnotes"] == ["Source: CRM. Data through May 2026"]


def test_footers_and_page_numbers_are_not_values(parsed):
    assert not [v for v in parsed.values if v.locator.get("element_id") in (3, 4)]


def test_chart_json_values_are_flagged_and_capped(parsed):
    chart = [v for v in parsed.values if v.locator.get("element_id") == 5]
    assert [(v.context["category"], v.raw_number) for v in chart] == [("East", 84.4), ("West", 91.9)]
    assert all({"AI_PARSED", "AI_CHART_READ"} <= set(_codes(v)) for v in chart)
    assert all(v.confidence == 0.5 for v in chart)
    assert chart[0].context["series_name"] == "Attainment" and chart[0].context["axis_title"] == "% of plan"
    assert chart[0].locator["page"] == 2 and chart[1].raw_text == "91.9%"


def test_unreadable_figure_and_failed_page_stay_placeholders(parsed):
    placeholders = [v for v in parsed.values if v.method == ExtractionMethod.IMAGE_PLACEHOLDER]
    assert sorted((v.slide_or_page, v.locator.get("element_id")) for v in placeholders) == [(2, 6), (3, None)]
    assert all("IMAGE_NOT_PARSED" in _codes(v) and v.raw_number is None for v in placeholders)
    assert any(i["code"] == "AI_PARSE_PAGE_ERROR" and i["page"] == 3 for i in parsed.issues)


def test_pages_and_versions(parsed):
    assert [p.page_number for p in parsed.pages] == [1, 2, 3]
    first = parsed.pages[0]
    assert first.title == "Kestrel Field Update - May 2026" and "TRx 5,910" in first.text and "West" in first.text
    assert parsed.pages[1].image_count == 2
    assert parsed.extractor_versions["ai_parse_document"] == "2.0"
    assert len({v.value_id for v in parsed.values}) == len(parsed.values)
    assert all(v.extractor == "ai_parse_document" for v in parsed.values)


def test_missing_document_is_an_error():
    out = parse_ai_document_output(DOC_ID, {"error_status": [], "metadata": {"version": "2.0"}})
    assert out.failed and out.issues[0]["code"] == "AI_PARSE_EMPTY"


def test_html_grid_handles_spans():
    grid, spans, hint = html_table_grid("<table><tr><th rowspan='2'>A</th><th colspan='2'>B</th></tr>"
                                        "<tr><th>b1</th><th>b2</th></tr><tr><td>x</td><td>1</td><td>2</td></tr></table>")
    assert grid == [["A", "B", ""], ["", "b1", "b2"], ["x", "1", "2"]] and spans == [(0, 1, 2)] and hint == 2


class _FakeDatabricksStore:
    dialect = "databricks"

    def __init__(self, frame):
        self.frame, self.sql = frame, []

    def query(self, sql, params=None):
        self.sql.append(sql)
        if isinstance(self.frame, Exception):
            raise self.frame
        return self.frame


class _Doc:
    doc_id = DOC_ID
    file_path = "/Volumes/main/kpi_evidence/reports/inbox/2026/06/05/aaaaaaaaaaaa_Kestrel_Field_Update_scan.pdf"
    format = DocumentFormat.PDF


def test_run_ai_parse_on_databricks(settings):
    settings.ai_mode = "ai_query"
    store = _FakeDatabricksStore(pd.DataFrame([{"path": _Doc.file_path, "length": 10, "content_sha256": DOC_ID,
                                                "parsed_json": json.dumps(FIXTURE)}]))
    out = run_ai_parse(store, settings, _Doc())
    assert _Doc.file_path in store.sql[0] and "inbox/'" not in store.sql[0]
    assert any(v.method == ExtractionMethod.AI_PARSE for v in out.values)
    assert not any(i["code"] == "AI_PARSE_CONTENT_MISMATCH" for i in out.issues)


def test_run_ai_parse_failures_are_issues(settings):
    settings.ai_mode = "ai_query"
    out = run_ai_parse(_FakeDatabricksStore(PermissionError("PERMISSION_DENIED: ai functions")), settings, _Doc())
    assert out.failed and "PERMISSION_DENIED" in out.issues[0]["detail"]
    settings.ai_mode = "off"
    assert run_ai_parse(_FakeDatabricksStore(None), settings, _Doc()).issues[0]["code"] == "AI_PARSE_DISABLED"


def test_run_ai_parse_is_databricks_only(mem_store, settings):
    with pytest.raises(NotImplementedError, match="Databricks"):
        run_ai_parse(mem_store, settings, _Doc())


def test_merge_replaces_placeholders_page_by_page(pdf_bytes):
    from kpi_evidence.extract.pdf import extract_pdf_values

    base = extract_pdf_values(DOC_ID, pdf_bytes, "summary.pdf")
    assert needs_ai_parse(base)
    ai = parse_ai_document_output(DOC_ID, FIXTURE)
    merged = merge_ai_parse(base, ai)
    native = [v for v in base.values if v.method != ExtractionMethod.IMAGE_PLACEHOLDER]
    assert all(v in merged.values for v in native)
    assert not [v for v in merged.values if v.slide_or_page == 1 and v.method == ExtractionMethod.AI_PARSE]
    page2 = [v for v in merged.values if v.slide_or_page == 2]
    assert {v.method for v in page2 if v.locator.get("element_id") == 5} == {ExtractionMethod.AI_PARSE}
    assert not [v for v in page2 if v.method == ExtractionMethod.IMAGE_PLACEHOLDER and "element_id" not in v.locator]
    assert len(merged.pages) == 3 and merged.extractor_versions["ai_parse_document"] == "2.0"
    assert not merged.failed


def test_merge_keeps_placeholder_when_ai_reads_nothing(pdf_bytes):
    from kpi_evidence.extract.pdf import extract_pdf_values

    base = extract_pdf_values(DOC_ID, pdf_bytes, "summary.pdf")
    failed = ExtractionOutput(issues=[{"code": "AI_PARSE_FAILED", "severity": "error", "detail": "quota"}])
    merged = merge_ai_parse(base, failed)
    assert [v.value_id for v in merged.values] == [v.value_id for v in base.values]
    assert merged.issues[-1]["severity"] == "warning"
