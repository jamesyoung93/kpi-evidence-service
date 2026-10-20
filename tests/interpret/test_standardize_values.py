from __future__ import annotations

import pytest
from interp_builders import chart_point, codes, make_value, table_cell

from kpi_evidence.contracts import ExtractionMethod
from kpi_evidence.standardize import classify_format, parse_display_number, read_value


@pytest.mark.parametrize("text,value,unit,half", [
    ("5.9K", 5900.0, "count", 50.0),
    ("84.4%", 0.844, "fraction", 0.0005),
    ("5,910", 5910.0, "count", 0.5),
    ("(1,234)", -1234.0, "count", 0.5),
    ("$12.3M", 12_300_000.0, "usd", 50_000.0),
    ("5909.796", 5909.796, "count", 0.0005),
    ("62%", 0.62, "fraction", 0.005),
    ("+6%", 0.06, "fraction", 0.005),
    ("-3.2", -3.2, "count", 0.05),
    ("−3.2", -3.2, "count", 0.05),
    ("(12.5%)", -0.125, "fraction", 0.0005),
    ("+2.1 pts", 0.021, "fraction", 0.0005),
    ("45 bps", 0.0045, "fraction", 0.00005),
    ("1.2bn", 1.2e9, "count", 5e7),
    ("3MM", 3e6, "count", 5e5),
    ("12.3 million", 12.3e6, "count", 5e4),
    ("€3.1B", 3.1e9, "unknown", 5e7),
    ("1.2x", 1.2, "unknown", 0.05),
    ("5,910*", 5910.0, "count", 0.5),
    ("-$20", -20.0, "usd", 0.5),
    ("12.5 %", 0.125, "fraction", 0.0005),
])
def test_parse_display_number(text, value, unit, half):
    dn = parse_display_number(text)
    assert dn is not None, text
    assert dn.value == pytest.approx(value)
    assert dn.unit == unit
    assert dn.half_unit == pytest.approx(half)


def test_bare_number_keeps_no_scale():
    dn = parse_display_number("5.9K")
    assert dn.number == 5.9 and dn.scale == 1000.0 and dn.decimals == 1


def test_approximate_marker():
    assert parse_display_number("~12%").approximate


@pytest.mark.parametrize("text", [None, "", "-", "—", "n/a", "NM", "12-15%", "1.234,5", "1,23", "-(5)", "abc",
                                  "(5", "5%%", "$ (1.2) M"])
def test_not_a_number(text):
    assert parse_display_number(text) is None


@pytest.mark.parametrize("code,percent,currency,scale,decimals,is_date", [
    ("0.0%", True, None, 1.0, 1, False),
    ("0%", True, None, 1.0, 0, False),
    ('#,##0,"K"', False, None, 1e3, 0, False),
    ('#,##0.0,"K"', False, None, 1e3, 1, False),
    ('#,##0,,"M"', False, None, 1e6, 0, False),
    ("$#,##0.00", False, "usd", 1.0, 2, False),
    ("[$$-409]#,##0", False, "usd", 1.0, 0, False),
    ("[$€-407]#,##0.0", False, "eur", 1.0, 1, False),
    ('0.0"%"', False, None, 1.0, 1, False),
    ("mmm-yy", False, None, 1.0, None, True),
    ("#,##0;(#,##0)", False, None, 1.0, 0, False),
])
def test_classify_format(code, percent, currency, scale, decimals, is_date):
    f = classify_format(code)
    assert (f.percent, f.currency, f.display_scale, f.decimals, f.is_date) == (percent, currency, scale, decimals, is_date)
    assert not f.general


@pytest.mark.parametrize("code", [None, "", "General", "general"])
def test_general_format(code):
    assert classify_format(code).general


def test_stored_percent_stays_a_fraction():
    v = chart_point(0.844, category="West", series_name="Call attainment", number_format="0.0%")
    r = read_value(v, v.context)
    assert (r.value, r.unit, r.half_unit) == (0.844, "fraction", 0.0)


def test_stored_display_scaled_format_is_not_rescaled():
    v = chart_point(5909.796, category="May-26", number_format='#,##0.0,"K"')
    r = read_value(v, v.context)
    assert r.value == 5909.796 and r.unit == "count" and r.half_unit == 0.0
    assert not r.flags


def test_stored_general_with_percent_axis_title():
    v = chart_point(9.2, category="May-26", series_name="TRx share", number_format="General", axis_title="Share (%)")
    r = read_value(v, v.context)
    assert r.value == pytest.approx(0.092) and r.unit == "fraction"
    assert {"UNIT_FROM_TEXT_CUE", "PERCENT_POINTS_ASSUMED"} <= {f["code"] for f in r.flags}


def test_stored_general_fraction_under_percent_cue_is_kept():
    v = chart_point(0.092, category="May-26", series_name="TRx share (%)", number_format="General")
    r = read_value(v, v.context)
    assert r.value == pytest.approx(0.092) and r.unit == "fraction"


def test_stored_general_with_thousands_header():
    v = chart_point(5.91, category="May-26", series_name="TRx", number_format=None, axis_title="TRx (000s)")
    r = read_value(v, v.context)
    assert r.value == pytest.approx(5910.0)
    assert "SCALE_FROM_HEADER" in {f["code"] for f in r.flags}


def test_stored_currency_format():
    v = chart_point(1922.87, category="May-26", number_format="$#,##0.00")
    assert read_value(v, v.context).unit == "usd"


def test_text_cell_with_thousands_header_scales_half_unit_too():
    v = table_cell("5.9", row_label="TRx (000s)", col_header="May 2026")
    r = read_value(v, v.context)
    assert r.value == pytest.approx(5900.0) and r.half_unit == pytest.approx(50.0)


def test_text_cell_under_percent_header_reads_points():
    v = table_cell("9.2", row_label="TRx share", col_header="Share (%)")
    r = read_value(v, v.context)
    assert r.value == pytest.approx(0.092) and r.unit == "fraction" and r.half_unit == pytest.approx(0.0005)


def test_text_cell_currency_header():
    v = table_cell("1,250", row_label="Net sales ($)", col_header="May 2026")
    assert read_value(v, v.context).unit == "usd"


def test_unparseable_text_falls_back_to_extractor_number_with_flag():
    v = table_cell("5 910", row_label="TRx", raw_number=5910.0)
    r = read_value(v, v.context)
    assert r.value == 5910.0
    assert "NUMBER_TEXT_UNPARSED" in {f["code"] for f in r.flags}


def test_text_not_a_number():
    v = table_cell("n/a", row_label="TRx")
    r = read_value(v, v.context)
    assert r.value is None and "VALUE_NOT_NUMERIC" in {f["code"] for f in r.flags}


def test_raw_number_mismatch_is_flagged_and_text_wins():
    v = table_cell("5,910", row_label="TRx", raw_number=5900.0)
    r = read_value(v, v.context)
    assert r.value == 5910.0 and "RAW_NUMBER_MISMATCH" in {f["code"] for f in r.flags}


def test_number_source_inferred_from_method():
    stored = make_value("0.844", raw_number=0.844, method=ExtractionMethod.EMBEDDED_WORKSHEET, number_format="0.0%",
                        row_label="Call attainment")
    assert read_value(stored, stored.context).value == 0.844
    text = make_value("84.4%", raw_number=84.4, method=ExtractionMethod.PDF_TABLE, row_label="Call attainment")
    assert read_value(text, text.context).value == pytest.approx(0.844)


def test_stored_date_format_is_flagged():
    v = make_value("46173", raw_number=46173.0, method=ExtractionMethod.WORKBOOK, number_format="yyyy-mm-dd",
                   row_label="Report date")
    r = read_value(v, v.context)
    assert r.unit == "unknown" and "VALUE_IS_DATE" in {f["code"] for f in r.flags}


def test_image_placeholder_has_no_value():
    v = make_value(None, method=ExtractionMethod.IMAGE_PLACEHOLDER, object_kind="image")
    r = read_value(v, v.context)
    assert r.value is None and r.unit is None


def test_codes_helper_smoke():
    assert codes(type("O", (), {"flags": [{"code": "X"}]})()) == {"X"}
