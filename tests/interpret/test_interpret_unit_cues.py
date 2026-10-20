from __future__ import annotations

import pytest
from interp_builders import chart_point, make_value, table_cell

from kpi_evidence.contracts import ExtractionMethod
from kpi_evidence.standardize import classify_format, read_value


def flags(reading):
    return {f["code"] for f in reading.flags}


def read(v):
    return read_value(v, v.context)


@pytest.mark.parametrize("fmt", ["#,##0.0", "0.0", "#,##0", None, "General"])
def test_thousands_header_scales_plain_number_formats(fmt):
    r = read(chart_point(5.8, category="May-26", series_name="TRx (000s)", number_format=fmt))
    assert r.value == pytest.approx(5800.0) and r.unit == "count" and "SCALE_FROM_HEADER" in flags(r)


@pytest.mark.parametrize("fmt", ["#,##0.0", "0.0"])
def test_percent_axis_title_reads_percentage_points_for_plain_number_formats(fmt):
    r = read(chart_point(9.1, category="May-26", series_name="TRx share", number_format=fmt, axis_title="Share (%)"))
    assert r.value == pytest.approx(0.091) and r.unit == "fraction" and "PERCENT_POINTS_ASSUMED" in flags(r)


def test_workbook_cell_with_number_format_under_thousands_header():
    v = make_value("6.26", raw_number=6.26, method=ExtractionMethod.WORKBOOK, number_format="#,##0.0",
                   row_label="Mar 2026", col_header="TRx (000s)", object_kind="worksheet", number_source="stored")
    assert read(v).value == pytest.approx(6260.0)


def test_display_scaled_format_is_still_not_rescaled():
    r = read(chart_point(5909.796, category="May-26", series_name="TRx (000s)", number_format='#,##0.0,"K"'))
    assert r.value == 5909.796 and not r.flags


def test_currency_format_still_takes_a_scale_header():
    v = make_value("12.3", raw_number=12.3, method=ExtractionMethod.WORKBOOK, number_format="$#,##0.0",
                   row_label="Net sales ($M)", col_header="May 2026", object_kind="worksheet", number_source="stored")
    r = read(v)
    assert r.value == pytest.approx(12_300_000.0) and r.unit == "usd"


def test_table_caption_unit_applies_to_its_cells():
    v = make_value("1.3", method=ExtractionMethod.PDF_TABLE, object_kind="pdf_table", number_source="text",
                   object_title="TRx (000s)", row_label="West", col_header="May 2026")
    r = read(v)
    assert r.value == pytest.approx(1300.0) and r.half_unit == pytest.approx(50.0) and "SCALE_FROM_HEADER" in flags(r)


def test_table_caption_sentence_is_not_a_unit_cue():
    v = table_cell("1,213", row_label="Calls", col_header="May 2026", object_title="Percent of calls on target rose")
    r = read(v)
    assert r.value == 1213.0 and r.unit == "count" and not r.flags


def test_combo_chart_title_cue_applies_only_to_the_series_it_names():
    title = "TRx and TRx share (%)"
    trx = read(chart_point(5736.36, category="May-26", series_name="TRx", number_format="General",
                           object_title=title, series_count=2))
    share = read(chart_point(9.13, category="May-26", series_name="TRx share", number_format="General",
                             object_title=title, series_count=2))
    assert (trx.value, trx.unit, trx.flags) == (5736.36, "count", [])
    assert share.value == pytest.approx(0.0913) and share.unit == "fraction"


def test_combo_chart_scale_cue_applies_only_to_the_series_it_names():
    title = "TRx (000s) and TRx share"
    trx = read(chart_point(5.73636, category="May-26", series_name="TRx", number_format="General",
                           object_title=title, series_count=2))
    share = read(chart_point(0.0913, category="May-26", series_name="TRx share", number_format="General",
                             object_title=title, series_count=2))
    assert trx.value == pytest.approx(5736.36)
    assert share.value == pytest.approx(0.0913) and "SCALE_FROM_HEADER" not in flags(share)


def test_multi_series_chart_with_one_measure_in_the_title_applies_it_to_every_series():
    for brand, value in (("Kestrel", 9.1), ("Competitor A", 26.2)):
        r = read(chart_point(value, category="May-26", series_name=brand, number_format="General",
                             object_title="Market share (%)", series_count=2))
        assert r.unit == "fraction" and r.value == pytest.approx(value / 100)


def test_single_series_chart_title_cue_still_applies():
    r = read(chart_point(9.2, category="May-26", series_name="TRx share", number_format="General",
                         object_title="Share (%)", series_count=1))
    assert r.value == pytest.approx(0.092) and r.unit == "fraction"


@pytest.mark.parametrize("code", ['0.0"%"', "0.0\\%", '0"%"', '#,##0.0" %"'])
def test_literal_percent_format_reads_percentage_points(code):
    assert classify_format(code).literal_percent and not classify_format(code).percent
    r = read(chart_point(84.4, category="May-26", series_name="Call attainment", number_format=code))
    assert r.value == pytest.approx(0.844) and r.unit == "fraction" and "UNIT_FROM_FORMAT_LITERAL" in flags(r)


def test_real_percent_format_is_not_a_literal_percent():
    assert not classify_format("0.0%").literal_percent
    r = read(chart_point(0.844, category="May-26", series_name="Call attainment", number_format="0.0%"))
    assert (r.value, r.unit, r.flags) == (0.844, "fraction", [])
