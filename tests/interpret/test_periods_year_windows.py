from __future__ import annotations

from datetime import date

import pytest

from kpi_evidence.periods import parse_period, parse_year_label, period_bounds, window_bounds, window_grain

REF = date(2026, 5, 31)


def _span(p):
    return (p.start, p.end, p.grain)


@pytest.mark.parametrize("text,start,end,grain", [
    ("YTD 2025", date(2025, 1, 1), date(2025, 5, 31), "ytd"),
    ("2025 YTD", date(2025, 1, 1), date(2025, 5, 31), "ytd"),
    ("YTD-2025", date(2025, 1, 1), date(2025, 5, 31), "ytd"),
    ("YTD 2026", date(2026, 1, 1), date(2026, 5, 31), "ytd"),
    ("MAT 2025", date(2024, 6, 1), date(2025, 5, 31), "r12m"),
])
def test_window_token_takes_the_printed_year(text, start, end, grain):
    p = parse_period(text, reference=REF)
    assert _span(p) == (start, end, grain)
    assert p.label == text


def test_window_token_with_a_year_needs_a_reference():
    assert parse_period("YTD 2025") is None


def test_fiscal_ytd_with_a_printed_year():
    p = parse_period("YTD 2025", reference=REF, fiscal_year_start_month=7)
    assert _span(p) == (date(2024, 7, 1), date(2025, 5, 31), "ytd")


@pytest.mark.parametrize("text", ["QTD", "Quarter to date", "quarter-to-date"])
def test_quarter_to_date(text):
    assert _span(parse_period(text, reference=REF)) == (date(2026, 4, 1), date(2026, 5, 31), "qtd")


def test_month_to_date_is_the_month():
    assert _span(parse_period("MTD", reference=REF)) == (date(2026, 5, 1), date(2026, 5, 31), "month")
    assert window_grain("TRx MTD") == "month"


def test_quarter_to_date_at_a_quarter_end_is_the_quarter():
    assert _span(parse_period("QTD Jun 2026")) == (date(2026, 4, 1), date(2026, 6, 30), "quarter")
    assert window_bounds(date(2026, 4, 30), "qtd") == (date(2026, 4, 1), date(2026, 4, 30), "month")
    assert window_bounds(date(2026, 5, 31), "qtd") == (date(2026, 4, 1), date(2026, 5, 31), "qtd")
    assert period_bounds(date(2026, 11, 30), "qtd") == (date(2026, 10, 1), date(2026, 11, 30))


@pytest.mark.parametrize("text,year", [("2025", 2025), ("2024", 2024), ("2025 Actual", 2025), ("2025A", 2025),
                                       ("Full year 2025", 2025), ("Calendar year 2024", 2024), ("2025 Total", 2025)])
def test_year_label(text, year):
    p = parse_year_label(text, reference=REF)
    assert _span(p) == (date(year, 1, 1), date(year, 12, 31), "r12m")
    assert p.kind == "calendar_year" and p.confidence < 0.9


def test_year_label_not_over_yet():
    assert parse_year_label("2026", reference=REF) is None
    assert parse_year_label("2026", reference=date(2026, 12, 31)).end == date(2026, 12, 31)
    assert parse_year_label("2026").end == date(2026, 12, 31)


@pytest.mark.parametrize("text", [None, "", "TRx 2025", "2025 Plan", "FY2025", "1.2025", "2,025", "Q1 2025", "20255"])
def test_not_a_year_label(text):
    assert parse_year_label(text, reference=REF) is None


def test_bare_year_is_still_not_a_period_in_free_text():
    assert parse_period("2025", reference=REF) is None
    assert parse_period("TRx 2,026 units", reference=REF) is None
