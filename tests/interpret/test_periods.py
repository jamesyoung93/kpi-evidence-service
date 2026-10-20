from __future__ import annotations

from datetime import date

import pytest

from kpi_evidence.periods import (
    excel_serial_to_date, find_periods, parse_period, period_bounds, strip_periods, window_grain,
)

REF = date(2026, 5, 31)


def _span(p):
    return (p.start, p.end, p.grain)


@pytest.mark.parametrize("text", ["May 2026", "May-26", "May '26", "May'26", "2026-05", "05/2026", "5/2026",
                                  "May, 2026", "MAY 2026", "2026/05", "May-2026"])
def test_month_forms(text):
    p = parse_period(text)
    assert p is not None, text
    assert _span(p) == (date(2026, 5, 1), date(2026, 5, 31), "month")
    assert p.kind == "month"
    assert p.confidence >= 0.9


@pytest.mark.parametrize("text,month", [("September 2026", 9), ("Sept 2026", 9), ("Sep-26", 9), ("Dec-25", 12),
                                        ("June 2026", 6), ("Jan. 2026", 1)])
def test_month_names(text, month):
    p = parse_period(text)
    assert p.end.month == month and p.grain == "month"


def test_two_digit_year_is_slightly_less_confident():
    assert parse_period("May-26").confidence < parse_period("May 2026").confidence


def test_leap_february_month_end():
    assert parse_period("Feb 2028").end == date(2028, 2, 29)


@pytest.mark.parametrize("text", ["Q2 2026", "Q2'26", "Q2 '26", "Q2-2026", "Q2-26", "2Q26", "2Q 2026", "2026 Q2",
                                  "2026-Q2", "Q2 FY26", "FY26 Q2", "second quarter 2026"])
def test_quarter_forms(text):
    p = parse_period(text)
    assert p is not None, text
    assert _span(p) == (date(2026, 4, 1), date(2026, 6, 30), "quarter"), text


def test_fiscal_quarter_uses_fiscal_start():
    p = parse_period("Q2 FY26", fiscal_year_start_month=7)
    assert _span(p) == (date(2025, 10, 1), date(2025, 12, 31), "quarter")


def test_plain_quarter_is_calendar_even_with_fiscal_brand():
    p = parse_period("Q2 2026", fiscal_year_start_month=7)
    assert _span(p) == (date(2026, 4, 1), date(2026, 6, 30), "quarter")


@pytest.mark.parametrize("text,start,end", [
    ("H1 2026", date(2026, 1, 1), date(2026, 6, 30)),
    ("H2'26", date(2026, 7, 1), date(2026, 12, 31)),
    ("1H26", date(2026, 1, 1), date(2026, 6, 30)),
    ("first half 2026", date(2026, 1, 1), date(2026, 6, 30)),
])
def test_half_years_are_r6m(text, start, end):
    p = parse_period(text)
    assert _span(p) == (start, end, "r6m")
    assert p.kind == "half"


@pytest.mark.parametrize("text,fs,start,end", [
    ("FY2026", 1, date(2026, 1, 1), date(2026, 12, 31)),
    ("FY26", 1, date(2026, 1, 1), date(2026, 12, 31)),
    ("FY2026", 7, date(2025, 7, 1), date(2026, 6, 30)),
    ("FY26", 4, date(2025, 4, 1), date(2026, 3, 31)),
    ("FY 2026", 1, date(2026, 1, 1), date(2026, 12, 31)),
    ("FY2025/26", 7, date(2025, 7, 1), date(2026, 6, 30)),
])
def test_fiscal_years(text, fs, start, end):
    p = parse_period(text, fiscal_year_start_month=fs)
    assert _span(p) == (start, end, "r12m")
    assert p.kind == "fiscal_year"


def test_calendar_year_token():
    p = parse_period("CY2026", fiscal_year_start_month=7)
    assert _span(p) == (date(2026, 1, 1), date(2026, 12, 31), "r12m")


def test_ytd_with_month():
    p = parse_period("YTD May 2026")
    assert _span(p) == (date(2026, 1, 1), date(2026, 5, 31), "ytd")
    assert p.kind == "ytd"


def test_ytd_respects_fiscal_year_start():
    p = parse_period("YTD May 2026", fiscal_year_start_month=7)
    assert p.start == date(2025, 7, 1) and p.end == date(2026, 5, 31)


def test_bare_ytd_needs_reference():
    assert parse_period("YTD") is None
    p = parse_period("YTD", reference=REF)
    assert _span(p) == (date(2026, 1, 1), date(2026, 5, 31), "ytd")
    assert p.confidence <= 0.6


@pytest.mark.parametrize("text,grain,start", [
    ("R3M May 2026", "r3m", date(2026, 3, 1)),
    ("L3M May-26", "r3m", date(2026, 3, 1)),
    ("rolling 3 months ending May 2026", "r3m", date(2026, 3, 1)),
    ("R6M May 2026", "r6m", date(2025, 12, 1)),
    ("R12M May 2026", "r12m", date(2025, 6, 1)),
    ("MAT May-26", "r12m", date(2025, 6, 1)),
    ("TTM ending May 2026", "r12m", date(2025, 6, 1)),
    ("L12M May 2026", "r12m", date(2025, 6, 1)),
    ("May-26 R3M", "r3m", date(2026, 3, 1)),
    ("last 12 months to May 2026", "r12m", date(2025, 6, 1)),
])
def test_rolling_windows(text, grain, start):
    p = parse_period(text)
    assert p is not None, text
    assert (p.grain, p.start, p.end) == (grain, start, date(2026, 5, 31)), text
    assert p.kind == "rolling"


def test_mat_sep_26():
    p = parse_period("MAT Sep-26")
    assert _span(p) == (date(2025, 10, 1), date(2026, 9, 30), "r12m")


def test_bare_window_tokens_with_reference():
    assert parse_period("R3M") is None
    assert _span(parse_period("R3M", reference=REF)) == (date(2026, 3, 1), date(2026, 5, 31), "r3m")
    assert parse_period("L12M", reference=REF).grain == "r12m"
    assert parse_period("rolling 6 months", reference=REF).grain == "r6m"


def test_unsupported_window_lengths_keep_an_explicit_grain():
    assert parse_period("R4M May 2026").grain == "r4m"
    assert parse_period("L4W", reference=REF).grain == "r4w"


@pytest.mark.parametrize("text", ["data through May 2026", "Data thru May 2026", "as of 05/31/2026",
                                  "month ending May 31, 2026", "Data as of May 31, 2026", "period ending 2026-05-31",
                                  "Source: IQVIA NPA, data through May 2026"])
def test_through_phrases(text):
    p = parse_period(text)
    assert p is not None, text
    assert p.kind == "through", text
    assert _span(p) == (date(2026, 5, 1), date(2026, 5, 31), "month")


def test_through_label_is_the_printed_period_only():
    assert parse_period("Data through May 2026").label == "May 2026"


def test_through_wins_over_publication_month_in_same_footer():
    p = parse_period("Published June 2026 | data through May 2026")
    assert p.kind == "through" and p.end == date(2026, 5, 31)


def test_mid_month_as_of_is_less_confident():
    p = parse_period("as of June 15, 2026")
    assert p.end == date(2026, 6, 30)
    assert p.confidence < parse_period("as of June 30, 2026").confidence


def test_through_month_without_year_uses_reference():
    assert parse_period("data through May") is None
    p = parse_period("data through May", reference=date(2026, 6, 30))
    assert p.end == date(2026, 5, 31) and p.kind == "through"


@pytest.mark.parametrize("text", ["2026-05-31", "2026-05-31T00:00:00", "05/31/2026", "31/05/2026", "May 31, 2026",
                                  "31 May 2026", "2026-05-15"])
def test_day_dates_resolve_to_their_month(text):
    p = parse_period(text)
    assert _span(p) == (date(2026, 5, 1), date(2026, 5, 31), "month"), text


def test_ambiguous_slash_date_reads_us_order():
    p = parse_period("05/06/2026")
    assert p.end == date(2026, 5, 31)
    assert p.confidence < 0.9


def test_excel_serials_both_systems():
    assert excel_serial_to_date(46173) == date(2026, 5, 31)
    assert excel_serial_to_date(44711, "1904") == date(2026, 5, 31)
    assert excel_serial_to_date(59) == date(1900, 2, 28)
    assert excel_serial_to_date(61) == date(1900, 3, 1)
    with pytest.raises(ValueError):
        excel_serial_to_date(60)


def test_excel_serial_only_when_asked():
    assert parse_period("46173") is None
    p = parse_period("46173", excel_serial=True)
    assert _span(p) == (date(2026, 5, 1), date(2026, 5, 31), "month") and p.kind == "serial"
    assert parse_period("44711.0", excel_serial=True, date_system="1904").end == date(2026, 5, 31)


def test_bare_month_uses_latest_not_after_reference():
    assert parse_period("May") is None
    assert parse_period("May", reference=REF).end == date(2026, 5, 31)
    dec = parse_period("Dec", reference=REF)
    assert dec.end == date(2025, 12, 31)
    assert dec.confidence == pytest.approx(0.7)


def test_bare_quarter_with_reference():
    assert parse_period("Q2") is None
    assert _span(parse_period("Q2", reference=REF)) == (date(2026, 4, 1), date(2026, 6, 30), "quarter")
    assert parse_period("Q3", reference=REF).end == date(2025, 9, 30)


@pytest.mark.parametrize("text", [None, "", "   ", "Q2 priorities", "2026", "5,910", "TRx 2,026 units", "Q5 2026",
                                  "13/2026", "2026-13", "May 26", "FY", "Sales may increase", "10 Q2", "5-10-15",
                                  "IL-17 class share", "R3M", "Dec", "1.2025", "2026.05", "Top 20 HCPs", "84.4%",
                                  "Q2 TRx", "H1N1 vaccine", "12-month persistency"])
def test_not_a_period(text):
    assert parse_period(text) is None, text


def test_find_periods_in_order():
    found = find_periods("Kestrel Monthly Brand Review June 2026 - data through May 2026")
    assert [(p.end, p.kind) for p in found] == [(date(2026, 6, 30), "month"), (date(2026, 5, 31), "through")]


def test_find_periods_multiple_quarters():
    found = find_periods("Q2 2026 vs Q2 2025")
    assert [p.end for p in found] == [date(2026, 6, 30), date(2025, 6, 30)]
    assert parse_period("Q2 2026 vs Q2 2025").end == date(2026, 6, 30)


def test_find_periods_bare_quarter_needs_reference():
    assert find_periods("Q2 TRx 17,220") == []
    assert find_periods("Q2 TRx 17,220", reference=date(2026, 6, 30))[0].end == date(2026, 6, 30)


def test_window_grain():
    assert window_grain("TRx (R3M)") == "r3m"
    assert window_grain("YTD TRx") == "ytd"
    assert window_grain("MAT share") == "r12m"
    assert window_grain("TRx") is None
    assert window_grain(None) is None


def test_strip_periods():
    assert strip_periods("TRx YTD May 2026") == "TRx"
    assert strip_periods("Share Q2'26 (data through Jun 2026)") == "Share ( )"
    assert strip_periods("IL-17 class share") == "IL-17 class share"


def test_period_bounds():
    assert period_bounds(date(2026, 5, 15), "month") == (date(2026, 5, 1), date(2026, 5, 31))
    assert period_bounds(date(2026, 6, 30), "quarter") == (date(2026, 4, 1), date(2026, 6, 30))
    assert period_bounds(date(2026, 5, 31), "r12m") == (date(2025, 6, 1), date(2026, 5, 31))
    assert period_bounds(date(2026, 5, 31), "ytd", fiscal_year_start_month=10) == (date(2025, 10, 1), date(2026, 5, 31))
    assert period_bounds(date(2026, 5, 31), "r4m") == (date(2026, 2, 1), date(2026, 5, 31))


def test_contains():
    q2 = parse_period("Q2 2026")
    assert q2.contains(parse_period("May 2026"))
    assert not q2.contains(parse_period("Mar 2026"))
