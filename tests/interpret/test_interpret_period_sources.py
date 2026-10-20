from __future__ import annotations

from datetime import date

import pytest
from interp_builders import DIMS, chart_point, codes, make_doc, table_cell

from kpi_evidence.standardize import _ScopeIndex, interpret, interpret_value

FOOTER = ["Data through May 2026"]


def run(values, *, doc=None, registry=None, dims=DIMS):
    return interpret(doc or make_doc(), values, [], registry, None, dims)


def one(value, **kw):
    [obs] = run([value], **kw)
    return obs


def span(obs):
    return obs.period_start, obs.period_end, obs.period_grain


def test_through_title_does_not_replace_quarter_categories():
    cats = ["Q3 2025", "Q4 2025", "Q1 2026", "Q2 2026"]
    obs = run([chart_point(18000.0 + i, category=c, series_name="TRx", object_title="Quarterly TRx, data through May 2026")
               for i, c in enumerate(cats)])
    assert [span(o) for o in obs[:3]] == [(date(2025, 7, 1), date(2025, 9, 30), "quarter"),
                                          (date(2025, 10, 1), date(2025, 12, 31), "quarter"),
                                          (date(2026, 1, 1), date(2026, 3, 31), "quarter")]
    assert all(o.period_source == "category" and o.period_confidence >= 0.9 for o in obs[:3])
    assert all("PERIOD_CONFLICT" not in codes(o) for o in obs[:3])
    assert span(obs[3]) == (date(2026, 4, 1), date(2026, 6, 30), "quarter")
    assert "PERIOD_CONFLICT" in codes(obs[3]) and obs[3].period_confidence < 0.6


@pytest.mark.parametrize("title", ["TRx trend, data through May 2026", "TRx trend (as of May 2026)"])
def test_through_title_keeps_every_trend_point(title):
    obs = run([chart_point(v, category=c, series_name="TRx", object_title=title)
               for c, v in (("Mar 2026", 6260.0), ("Apr 2026", 5701.0), ("May 2026", 5736.0))])
    assert [o.period_end for o in obs] == [date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31)]
    assert all("PERIOD_CONFLICT" not in codes(o) and o.period_confidence >= 0.9 for o in obs)


def test_through_title_is_the_period_when_nothing_else_names_one():
    obs = one(table_cell("5,910", row_label="TRx", col_header="Value", object_title="Key metrics, data through May 2026"))
    assert span(obs) == (date(2026, 5, 1), date(2026, 5, 31), "month") and obs.period_source == "object_title"


def test_value_beyond_a_through_title_is_a_conflict():
    obs = one(table_cell("6,010", row_label="TRx", col_header="June 2026", object_title="KPIs, data through May 2026"))
    assert "PERIOD_CONFLICT" in codes(obs)


@pytest.mark.parametrize("header,year", [("2024", 2024), ("2025", 2025), ("2025 Actual", 2025), ("Full year 2025", 2025),
                                         ("2025 / Actual", 2025)])
def test_year_column_is_that_calendar_year(header, year):
    obs = one(table_cell("69,870", row_label="TRx", col_header=header, footnotes=FOOTER))
    assert span(obs) == (date(year, 1, 1), date(year, 12, 31), "r12m")
    assert obs.period_source == "col_header" and "PERIOD_CONFLICT" not in codes(obs)


def test_year_category_and_year_series_are_calendar_years():
    cat = one(chart_point(0.088, category="2023", series_name="TRx share", number_format="0.0%"))
    assert span(cat) == (date(2023, 1, 1), date(2023, 12, 31), "r12m") and cat.period_source == "category"
    series = one(chart_point(1265.0, category="West", series_name="2025"))
    assert span(series) == (date(2025, 1, 1), date(2025, 12, 31), "r12m") and series.scope == {"region": "West"}


def test_year_that_is_not_over_yet_is_an_unknown_period():
    obs = one(table_cell("30,200", row_label="TRx", col_header="2026", footnotes=FOOTER))
    assert obs.period_end is None and obs.period_source == "none"
    assert "PERIOD_UNKNOWN" in codes(obs)


def test_year_inside_a_label_is_not_replaced_by_the_document_period():
    obs = one(table_cell("72,107", row_label="TRx 2025 launch cohort", col_header="Value"))
    assert obs.period_end is None and "PERIOD_UNKNOWN" in codes(obs)


@pytest.mark.parametrize("header,start,end", [("YTD 2025", date(2025, 1, 1), date(2025, 5, 31)),
                                              ("2025 YTD", date(2025, 1, 1), date(2025, 5, 31)),
                                              ("YTD 2026", date(2026, 1, 1), date(2026, 5, 31))])
def test_ytd_column_keeps_its_printed_year(header, start, end):
    obs = one(table_cell("28,644", row_label="TRx", col_header=header, footnotes=FOOTER))
    assert span(obs) == (start, end, "ytd")


@pytest.mark.parametrize("header", ["May 2026 / Prior year", "May 2026 / PY", "Prior year / May 2026", "May 2026 PY",
                                    "May 2026 / LY"])
def test_prior_year_part_of_a_two_row_header_shifts_the_period(header):
    obs = one(table_cell("5,301", row_label="TRx", col_header=header, footnotes=FOOTER))
    assert span(obs) == (date(2025, 5, 1), date(2025, 5, 31), "month")
    assert "PRIOR_YEAR_SHIFTED" in codes(obs)


def test_printed_prior_year_period_is_not_shifted_twice():
    obs = one(table_cell("5,301", row_label="TRx", col_header="PY (May 2025)", footnotes=FOOTER))
    assert span(obs) == (date(2025, 5, 1), date(2025, 5, 31), "month")
    assert "PRIOR_YEAR_SHIFTED" not in codes(obs)


def test_prior_year_column_shifts_the_document_period_too():
    obs = one(table_cell("5,512", row_label="TRx", col_header="PY"))
    assert span(obs) == (date(2025, 5, 1), date(2025, 5, 31), "month") and "PRIOR_YEAR_SHIFTED" in codes(obs)


@pytest.mark.parametrize("header", ["Prior month", "Previous month", "PM", "Last month"])
def test_prior_month_column_is_the_month_before(header):
    obs = one(table_cell("5,701", row_label="TRx", col_header=header, footnotes=FOOTER))
    assert span(obs) == (date(2026, 4, 1), date(2026, 4, 30), "month")
    assert "PRIOR_PERIOD_SHIFTED" in codes(obs)


def test_prior_quarter_next_to_a_monthly_value_is_an_unknown_period():
    obs = one(table_cell("17,100", row_label="TRx", col_header="Prior quarter", footnotes=FOOTER))
    assert obs.period_end is None and "PERIOD_UNKNOWN" in codes(obs)


def test_prior_quarter_next_to_a_quarterly_value_is_the_quarter_before():
    obs = one(table_cell("17,100", row_label="TRx", col_header="Q2 2026 / Prior quarter"))
    assert span(obs) == (date(2026, 1, 1), date(2026, 3, 31), "quarter")


def test_quarter_to_date_column_is_not_the_month():
    qtd = one(table_cell("11,437", row_label="TRx", col_header="QTD", footnotes=FOOTER))
    assert span(qtd) == (date(2026, 4, 1), date(2026, 5, 31), "qtd")
    mtd = one(table_cell("5,736", row_label="TRx", col_header="MTD", footnotes=FOOTER))
    assert span(mtd) == (date(2026, 5, 1), date(2026, 5, 31), "month")


def test_change_column_against_prior_year_is_not_shifted():
    obs = one(table_cell("+5.2%", row_label="TRx", col_header="May 2026 / vs PY", footnotes=FOOTER))
    assert obs.period_end == date(2026, 5, 31) and "PRIOR_YEAR_SHIFTED" not in codes(obs)


def test_outer_year_level_dates_bare_month_categories():
    obs = run([chart_point(5000.0, category="Jan", category_levels=["2025", "Jan"]),
               chart_point(5040.0, category="May", category_levels=["2025", "May"]),
               chart_point(5160.0, category="May", category_levels=["2026", "May"])])
    assert [o.period_end for o in obs] == [date(2025, 1, 31), date(2025, 5, 31), date(2026, 5, 31)]
    assert all(o.period_source == "category" and o.period_grain == "month" for o in obs)


def test_outer_year_level_dates_bare_quarter_categories():
    obs = one(chart_point(17000.0, category="Q1", category_levels=["2025", "Q1"]))
    assert span(obs) == (date(2025, 1, 1), date(2025, 3, 31), "quarter")


def test_outer_fiscal_year_level_uses_the_fiscal_calendar():
    v = chart_point(5000.0, category="Aug", category_levels=["FY2026", "Aug"])
    obs = interpret_value(make_doc(), v, index=_ScopeIndex.build(DIMS), fs=7)
    assert obs.period_end == date(2025, 8, 31)


def test_outer_region_level_sets_scope():
    obs = run([chart_point(1210.0, category="Apr 2026", category_levels=["West", "Apr 2026"]),
               chart_point(1520.0, category="May 2026", category_levels=["East", "May 2026"])])
    assert [o.scope for o in obs] == [{"region": "West"}, {"region": "East"}]
    assert [o.period_end for o in obs] == [date(2026, 4, 30), date(2026, 5, 31)]


def test_outer_period_level_dates_region_categories():
    obs = one(chart_point(1265.0, category="West", category_levels=["May 2026", "West"]))
    assert obs.period_end == date(2026, 5, 31) and obs.period_source == "category" and obs.scope == {"region": "West"}


def test_outer_region_level_without_dimension_values_is_scope_unknown():
    obs = one(chart_point(1210.0, category="Apr 2026", category_levels=["West", "Apr 2026"]), dims=None)
    assert obs.scope == {} and "SCOPE_UNKNOWN" in codes(obs)


def test_levels_naming_different_regions_conflict():
    obs = one(chart_point(1.0, category="East", category_levels=["West", "East"]))
    assert "SCOPE_CONFLICT" in codes(obs)
