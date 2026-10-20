from __future__ import annotations

import pytest

from kpi_evidence.extract import parse_number_parts, parse_number_text
from kpi_evidence.extract._common import (
    LABEL, NA, NUM, analyze_grid, derived_change_flag, find_labeled_numbers, find_numbers, is_footnote,
    text_cell_kind,
)


@pytest.mark.parametrize("text, expected", [
    ("5.9K", (5.9, "number")), ("84.4%", (84.4, "percent")), ("(1,234)", (-1234.0, "number")),
    ("$12.3M", (12.3, "currency")), ("5,909.796", (5909.796, "number")), ("+6%", (6.0, "percent")),
    ("(0.3%)", (-0.3, "percent")), ("−3.2", (-3.2, "number")), ("+0.5pp", (0.5, "points")),
    ("45 bps", (45.0, "bps")), ("1.2x", (1.2, "multiple")), ("US$5M", (5.0, "currency")),
    ("12.5 %", (12.5, "percent")), ("1.2bn", (1.2, "number")), ("0.844", (0.844, "number")),
    ("62%*", (62.0, "percent")),
])
def test_parse_number_text_returns_bare_number(text, expected):
    assert parse_number_text(text) == expected


@pytest.mark.parametrize("text", ["", "-", "—", "n/a", "NM", "May-26", "1 234", "1.234,5", "~12%", "12-15%",
                                  "-(5)", "Q2'26", "abc", None])
def test_parse_number_text_fails_closed(text):
    assert parse_number_text(text) is None


def test_parse_number_parts_keep_scale_for_interpretation():
    parts = parse_number_parts("$12.3M")
    assert (parts.suffix, parts.multiplier, parts.currency, parts.decimals) == ("M", 1e6, "$", 1)
    assert parse_number_parts("+8%").explicit_sign and parse_number_parts("(1,234)").accounting_negative


@pytest.mark.parametrize("text", ["5.9K", "84.4%", "(1,234)", "$12.3M", "-31.6%", "2,400", "−3.2", "+5%"])
def test_bare_number_agrees_with_reconloop_parser(text):
    from reconloop.extract.pdf import parse_number

    parts = parse_number_parts(text)
    assert parse_number(text, percent="points") == pytest.approx(parts.number * parts.multiplier)


def _labels(line):
    return [(h.raw_text, h.parts.number, h.label) for h in find_labeled_numbers(line)]


@pytest.mark.parametrize("line, expected", [
    ("Brand awareness: 62%", [("62%", 62.0, "Brand awareness")]),
    ("TRx 5,910", [("5,910", 5910.0, "TRx")]),
    ("+8% TRx", [("+8%", 8.0, "TRx")]),
    ("47 HCPs attended", [("47", 47.0, "HCPs attended")]),
    ("IL-17 class share 26.2%", [("26.2%", 26.2, "IL-17 class share")]),
    ("TRx 5,910 | NRx 2,100", [("5,910", 5910.0, "TRx"), ("2,100", 2100.0, "NRx")]),
    ("Share 9.2%; NBRx 1,234", [("9.2%", 9.2, "Share"), ("1,234", 1234.0, "NBRx")]),
    ("Q2 2026 TRx 17,220 -31.6%", [("17,220", 17220.0, "Q2 2026 TRx"), ("-31.6%", -31.6, "Q2 2026 TRx")]),
])
def test_labeled_numbers(line, expected):
    assert _labels(line) == expected


def test_label_sides_are_kept_separately():
    hit = find_labeled_numbers("Speaker programs drove +8% TRx")[0]
    assert (hit.label_before, hit.label_after, hit.label_source) == ("Speaker programs drove", "TRx", "before")
    inherited = find_labeled_numbers("Q2 2026 TRx 17,220 -31.6%")[1]
    assert inherited.label_source == "inherited"


@pytest.mark.parametrize("line", ["Data through May 2026", "Meeting at 10:30 on 2026-05-31", "Q2-2026 review",
                                  "Page 4 of 6", "Slide 3", "Dec 31, 2025", "FY2026 plan", "Top 10 accounts",
                                  "Dec-25 to May-26"])
def test_dates_years_and_references_are_not_numbers(line):
    assert find_numbers(line) == []


def test_spans_index_the_original_line():
    line = "TRx −5% vs PY"
    hit = find_labeled_numbers(line)[0]
    assert line[hit.start:hit.end] == hit.raw_text and hit.parts.number == -5.0


def test_footnotes_and_change_cues():
    assert is_footnote("Source: IQVIA NPA, data through Mar 2026") and is_footnote("Data through May 2026")
    assert not is_footnote("Brand awareness: 62%")
    assert derived_change_flag("vs PY")[0]["code"] == "DERIVED_CHANGE"
    assert derived_change_flag("% chg") and derived_change_flag("YoY growth") and not derived_change_flag("May 2026")


def test_header_detection_and_row_labels():
    texts = [["Metric", "May 2026", "vs PY"], ["TRx", "5.9K", "+6%"], ["Persistency", "n/a", "-"]]
    kinds = [[text_cell_kind(t)[0] for t in row] for row in texts]
    assert kinds[1] == [LABEL, NUM, NUM] and kinds[2] == [LABEL, NA, NA]
    layout = analyze_grid(texts, kinds)
    assert layout.header_rows == 1
    assert layout.col_headers == ["Metric", "May 2026", "vs PY"]
    assert layout.row_labels[1] == "TRx"


def test_merged_and_year_headers_compose():
    texts = [["Region", "Q2 2026", ""], ["", "2025", "2026"], ["West", "1,200", "1,265.7"]]
    kinds = [[text_cell_kind(t)[0] for t in row] for row in texts]
    layout = analyze_grid(texts, kinds, spans=[(0, 1, 2)])
    assert layout.header_rows == 2
    assert layout.col_headers[1:] == ["Q2 2026 / 2025", "Q2 2026 / 2026"]


def test_no_header_when_first_row_is_numeric():
    texts = [["TRx", "5,910"], ["NRx", "2,100"]]
    kinds = [[text_cell_kind(t)[0] for t in row] for row in texts]
    layout = analyze_grid(texts, kinds)
    assert layout.header_rows == 0 and layout.header_row() is None and layout.row_labels == ["TRx", "NRx"]
