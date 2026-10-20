from __future__ import annotations

import io

import pytest

from kpi_evidence.extract.xlsx import extract_xlsx_values
from kpi_evidence.ids import sha256_bytes


def _cells(rows, number_format=None):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "KPIs"
    for row in rows:
        ws.append(row)
    if number_format:
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, int):
                    cell.number_format = number_format
    buf = io.BytesIO()
    wb.save(buf)
    blob = buf.getvalue()
    out = extract_xlsx_values(sha256_bytes(blob), blob, "kpis.xlsx")
    return {v.locator["cell"]: v for v in out.values}, out


@pytest.mark.parametrize("number_format", [None, "0", "#,##0"])
def test_counts_that_look_like_years_are_values(number_format):
    rows = [["Month", "TRx", "NBRx", "Calls"], ["Mar 2026", 6260, 1985, 2010], ["Apr 2026", 5700, 2034, 2050],
            ["May 2026", 5740, 2101, 2099]]
    cells, out = _cells(rows, number_format)
    assert sorted(cells) == ["B2", "B3", "B4", "C2", "C3", "C4", "D2", "D3", "D4"]
    assert (cells["C3"].raw_number, cells["C3"].context["col_header"], cells["C3"].context["row_label"]) == (
        2034.0, "NBRx", "Apr 2026")
    assert out.issues == []


def test_year_headers_stay_labels():
    cells, _out = _cells([["Metric", 2025, 2026], ["TRx", 68000, 71000]])
    assert sorted(cells) == ["B2", "C2"]
    assert [cells["B2"].context["col_header"], cells["C2"].context["col_header"]] == ["2025", "2026"]


def test_two_level_year_headers_stay_labels():
    cells, _out = _cells([["", "TRx", None, "NRx", None], ["Region", 2025, 2026, 2025, 2026],
                          ["West", 1200, 1265, 300, 310]])
    assert sorted(cells) == ["B3", "C3", "D3", "E3"]
    assert cells["C3"].context["col_header"].endswith("2026")


def test_year_row_labels_stay_labels():
    cells, _out = _cells([["Year", "TRx"], [2024, 60000], [2025, 65000]])
    assert sorted(cells) == ["B2", "B3"]
    assert [cells["B2"].context["row_label"], cells["B3"].context["row_label"]] == ["2024", "2025"]
    cells, _out = _cells([[2024, 60000], [2025, 65000]])
    assert sorted(cells) == ["B1", "B2"]


def test_first_value_under_a_period_header_is_kept():
    cells, _out = _cells([["Metric", "May 2026"], ["NBRx", 2034], ["TRx", 5736]])
    assert sorted(cells) == ["B2", "B3"]
    assert (cells["B2"].raw_number, cells["B2"].context["row_label"], cells["B2"].context["col_header"]) == (
        2034.0, "NBRx", "May 2026")


def test_a_single_count_column_is_kept():
    cells, _out = _cells([["NBRx"], [1985], [2034], [2101]])
    assert [cells[c].raw_number for c in ("A2", "A3", "A4")] == [1985.0, 2034.0, 2101.0]
    cells, _out = _cells([["NBRx", "NRx"], [1985, 1500], [2034, 1600]])
    assert sorted(cells) == ["A2", "A3", "B2", "B3"]
