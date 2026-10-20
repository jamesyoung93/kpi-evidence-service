from __future__ import annotations

from kpi_evidence.contracts import ExtractionMethod
from kpi_evidence.extract.xlsx import extract_xlsx_values
from kpi_evidence.ids import sha256_bytes


def _extract(blob):
    return extract_xlsx_values(sha256_bytes(blob), blob, "Kestrel_KPIs.xlsx", run_id="r1")


def _cells(out):
    return {(v.locator["sheet"], v.locator["cell"]): v for v in out.values}


def test_numeric_cells_with_headers_and_formats(workbook_bytes):
    out = _extract(workbook_bytes)
    cells = _cells(out)
    trx = cells[("KPIs", "B2")]
    assert trx.method == ExtractionMethod.WORKBOOK and trx.confidence == 1.0
    assert (trx.raw_text, trx.raw_number, trx.number_format) == ("5909.796", 5909.796, "#,##0")
    assert trx.context["row_label"] == "TRx" and trx.context["col_header"] == "May 2026"
    assert trx.context["header_row"] == ["Metric", "May 2026", "Apr 2026"] and trx.context["number_source"] == "stored"
    assert trx.locator == {"page": 1, "part": "xl/worksheets/sheet1.xml", "sheet": "KPIs", "cell": "B2", "row": 1, "col": 1}
    share = cells[("KPIs", "B3")]
    assert share.raw_number == 0.0917 and share.number_format == "0.0%"


def test_blank_row_starts_a_new_table_block(workbook_bytes):
    west = _cells(_extract(workbook_bytes))[("KPIs", "B7")]
    assert west.context["col_header"] == "TRx" and west.context["row_label"] == "West"
    assert west.context["header_row"] == ["Region", "TRx", "vs PY"]


def test_hidden_rows_sheets_and_change_columns_are_flagged(workbook_bytes):
    cells = _cells(_extract(workbook_bytes))
    codes = lambda v: [f["code"] for f in v.flags]
    assert "HIDDEN_CELL" in codes(cells[("KPIs", "B7")]) and cells[("KPIs", "B7")].context["hidden"] is True
    assert "DERIVED_CHANGE" in codes(cells[("KPIs", "C7")])
    notes = cells[("Notes", "B1")]
    assert notes.slide_or_page == 2 and "HIDDEN_SHEET" in codes(notes)


def test_formula_without_saved_result_is_an_issue_not_a_value(workbook_bytes):
    out = _extract(workbook_bytes)
    assert ("KPIs", "B4") not in _cells(out)
    assert any(i["code"] == "FORMULA_RESULT_MISSING" and "B4" in i["detail"] for i in out.issues)
    assert _cells(out)[("KPIs", "C4")].raw_number == 2031.5


def test_pages_one_per_sheet(workbook_bytes):
    out = _extract(workbook_bytes)
    assert [(p.page_number, p.title, p.hidden) for p in out.pages] == [(1, "KPIs", False), (2, "Notes", True)]
    assert "TRx" in out.pages[0].text
    assert out.document_properties["title"] == "Kestrel KPI workbook" and out.document_properties["page_count"] == 2
    assert len({v.value_id for v in out.values}) == len(out.values)


def test_not_a_workbook():
    out = _extract(b"plain text")
    assert out.failed and out.issues[0]["code"] == "NOT_AN_XLSX"
