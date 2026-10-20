from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from interp_builders import DIMS, chart_point, codes, make_doc, make_page, make_value, table_cell, text_value

from kpi_evidence.contracts import ExtractionMethod
from kpi_evidence.ids import stable_id
from kpi_evidence.standardize import (
    fingerprint, fingerprint_parts, interpret, interpret_value, mask_text, period_factor,
)

FOOTER = ["Source: account-level feed. Data through May 2026"]


def run(values, *, doc=None, pages=None, registry=None, settings=None, dims=DIMS, **kw):
    return interpret(doc or make_doc(), values, pages or [], registry, settings, dims, **kw)


def one(value, **kw):
    [obs] = run([value], **kw)
    return obs


def test_kpi_table_cell_period_from_data_through_footer(registry):
    v = table_cell("5,910", row_label="TRx", col_header="Value", object_title="Key metrics",
                   page_title="Kestrel Monthly Brand Review - June 2026", footnotes=FOOTER)
    obs = one(v, registry=registry)
    assert obs.label == "TRx"
    assert obs.value == 5910.0 and obs.unit == "count" and obs.display_half_unit == 0.5
    assert (obs.period_start, obs.period_end, obs.period_grain) == (date(2026, 5, 1), date(2026, 5, 31), "month")
    assert obs.period_source == "footnote" and obs.period_confidence == pytest.approx(0.95)
    assert obs.confidence == pytest.approx(1.0)
    assert obs.brand == "KESTREL" and obs.scope == {} and obs.scope_key == "{}"


def test_publication_month_in_page_title_is_not_the_measurement_period(registry):
    v = table_cell("5,910", row_label="TRx", col_header="Value", page_title="Brand review June 2026")
    obs = one(v, registry=registry)
    assert obs.period_end == date(2026, 5, 31)
    assert obs.period_source == "document" and obs.period_confidence == pytest.approx(0.6)
    assert "PERIOD_CONFLICT" not in codes(obs)
    assert obs.confidence == pytest.approx(0.9)


def test_trend_chart_dec_to_may_from_categories(registry):
    cats = ["Dec-25", "Jan-26", "Feb-26", "Mar-26", "Apr-26", "May-26"]
    values = [chart_point(5700.0 + i, category=c, series_name="TRx", object_title="TRx trend",
                          page_title="TRx trend through May 2026", footnotes=FOOTER) for i, c in enumerate(cats)]
    obs = run(values, registry=registry)
    assert [o.period_end for o in obs] == [date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 28),
                                           date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31)]
    assert {o.period_source for o in obs} == {"category"}
    assert {o.label for o in obs} == {"TRx"}
    assert {o.display_half_unit for o in obs} == {0.0}
    assert len({o.fingerprint for o in obs}) == 1
    assert len({o.observation_id for o in obs}) == 6
    assert all("PERIOD_CONFLICT" not in codes(o) for o in obs)


def test_iso_date_categories_from_date_axes():
    v = chart_point(5783.041, category="2026-05-31", series_name="TRx")
    assert one(v).period_end == date(2026, 5, 31)


def test_excel_serial_category_when_axis_is_dated():
    v = chart_point(5783.041, category="46173", series_name="TRx", category_number_format="mmm-yy")
    obs = one(v)
    assert obs.period_end == date(2026, 5, 31) and obs.period_source == "category"


def test_bare_month_categories_resolve_against_document_period():
    obs = run([chart_point(1.0, category="Dec"), chart_point(2.0, category="May")])
    assert [o.period_end for o in obs] == [date(2025, 12, 31), date(2026, 5, 31)]
    assert obs[0].period_confidence == pytest.approx(0.7)
    assert obs[0].confidence == pytest.approx(0.9)


def test_bare_month_category_with_year_series():
    obs = one(chart_point(1.0, category="May", series_name="2025", object_title="TRx by month"))
    assert obs.period_end == date(2025, 5, 31) and obs.label == "TRx"


def test_region_categories_set_scope(registry):
    values = [chart_point(v, category=c, series_name="NRx", object_title="NRx by region", footnotes=FOOTER)
              for c, v in (("West", 410.0), ("East", 380.0), ("West region", 1.0))]
    obs = run(values, registry=registry)
    assert [o.scope for o in obs] == [{"region": "West"}, {"region": "East"}, {"region": "West"}]
    assert obs[0].scope_key == '{"region":"West"}'
    assert {o.label for o in obs} == {"NRx"}
    assert obs[0].period_end == date(2026, 5, 31)
    assert obs[0].fingerprint == obs[1].fingerprint


def test_region_rows_in_a_table_use_the_column_header_as_label():
    obs = one(table_cell("84.4%", row_label="West", col_header="Call attainment", footnotes=FOOTER))
    assert obs.label == "Call attainment" and obs.scope == {"region": "West"}
    assert obs.value == pytest.approx(0.844) and obs.unit == "fraction"


def test_district_wins_over_its_region_name():
    obs = one(table_cell("1,265.7", row_label="West District 1", col_header="TRx"))
    assert obs.scope == {"district": "West District 1"}


def test_slide_title_region_phrase_sets_scope_but_plain_words_do_not():
    west = one(table_cell("1,265.7", row_label="TRx", col_header="May 2026", page_title="West region performance"))
    assert west.scope == {"region": "West"}
    plain = one(table_cell("5,910", row_label="TRx", col_header="May 2026", page_title="Commercial excellence"))
    assert plain.scope == {}


def test_scope_ambiguous_category():
    obs = one(chart_point(1.0, category="East vs West", series_name="TRx"))
    assert obs.scope == {} and "SCOPE_AMBIGUOUS" in codes(obs)


def test_scope_conflict_is_flagged_and_category_wins():
    obs = one(chart_point(1.0, category="East", series_name="TRx", page_title="West region deep dive"))
    assert obs.scope == {"region": "East"} and "SCOPE_CONFLICT" in codes(obs)


def test_vs_py_column_is_a_derived_change():
    obs = one(table_cell("+5.2%", row_label="TRx", col_header="vs PY %", footnotes=FOOTER))
    assert "DERIVED_CHANGE" in codes(obs)
    assert obs.label == "TRx vs PY %"
    assert obs.value == pytest.approx(0.052) and obs.unit == "fraction"
    assert obs.period_end == date(2026, 5, 31)
    assert "PRIOR_YEAR_SHIFTED" not in codes(obs)


@pytest.mark.parametrize("header", ["% chg", "YoY", "Growth", "Change vs LY", "MoM", "Delta", "Var. to plan"])
def test_change_headers(header):
    assert "DERIVED_CHANGE" in codes(one(table_cell("3.1%", row_label="NRx", col_header=header)))


def test_level_column_is_not_a_change():
    obs = one(table_cell("5,910", row_label="TRx", col_header="May 2026"))
    assert "DERIVED_CHANGE" not in codes(obs) and obs.period_source == "col_header"


def test_prior_year_column_shifts_the_period():
    obs = one(table_cell("5,512", row_label="TRx", col_header="PY", footnotes=FOOTER))
    assert obs.period_end == date(2025, 5, 31) and obs.period_start == date(2025, 5, 1)
    assert "PRIOR_YEAR_SHIFTED" in codes(obs) and obs.label == "TRx"


def test_il17_class_share_slide_text_states_its_rule(registry):
    v = text_value("26.2%", "IL-17 class share 26.2%", page_title="Market share", footnotes=FOOTER)
    obs = one(v, registry=registry)
    assert obs.label == "IL-17 class share"
    assert obs.value == pytest.approx(0.262) and obs.unit == "fraction" and obs.display_half_unit == pytest.approx(0.0005)
    assert "IL-17 class share" in obs.stated_rule
    assert obs.confidence == pytest.approx(0.7)


def test_slide_text_confidence_with_document_period_stays_above_review_floor():
    obs = one(text_value("26.2%", "IL-17 class share 26.2%"))
    assert obs.period_source == "document"
    assert obs.confidence == pytest.approx(0.63)


def test_text_value_5_9k():
    obs = one(text_value("5.9K", "TRx 5.9K", page_title="Highlights"))
    assert obs.value == 5900.0 and obs.display_half_unit == 50.0 and obs.label == "TRx"


def test_text_value_label_after_number():
    obs = one(text_value("47", "47 HCPs attended speaker programs"))
    assert obs.label == "HCPs attended speaker programs" and obs.value == 47.0


def test_text_value_label_picks_the_phrase_next_to_the_number():
    obs = one(text_value("2,100", "TRx 5,910 | NRx 2,100"))
    assert obs.label == "NRx"


def test_text_change_callout():
    obs = one(text_value("+12%", "NBRx +12% vs PY"))
    assert obs.label == "NBRx vs PY" and "DERIVED_CHANGE" in codes(obs)


def test_causal_claim_is_not_marked_as_change():
    obs = one(text_value("+8%", "Speaker programs drove +8% TRx"))
    assert "DERIVED_CHANGE" not in codes(obs)
    assert "Speaker programs drove +8% TRx" in obs.stated_rule


def test_projected_referrals_keep_the_qualifier(registry):
    obs = one(text_value("184", "Projected referrals: 184"), registry=registry)
    assert obs.label == "Projected referrals" and "projected" in obs.stated_rule.lower()


def test_writers_only_call_attainment_row_states_rule():
    obs = one(table_cell("91.9%", row_label="Call attainment (writers only)", col_header="May 2026"))
    assert obs.value == pytest.approx(0.919)
    assert "writers only" in obs.stated_rule


def test_stated_rule_keeps_definitional_nearby_text_only(registry):
    v = chart_point(0.262, category="May-26", series_name="TRx share", number_format="0.0%",
                    nearby_text=["Market basket: IL-17 class", "Highlights"], object_title="Share trend")
    obs = one(v, registry=registry)
    assert "Market basket: IL-17 class" in obs.stated_rule
    assert "Highlights" not in obs.stated_rule


def test_series_period_and_window_tokens():
    obs = one(chart_point(17000.0, category="May-26", series_name="TRx R3M"))
    assert (obs.period_start, obs.period_end, obs.period_grain) == (date(2026, 3, 1), date(2026, 5, 31), "r3m")
    assert obs.label == "TRx"


def test_ytd_title_with_month_columns():
    obs = one(table_cell("28,000", row_label="TRx", col_header="May 2026", object_title="YTD performance"))
    assert obs.period_grain == "ytd" and obs.period_start == date(2026, 1, 1)


def test_finer_column_period_inside_title_quarter():
    obs = one(table_cell("5,600", row_label="TRx", col_header="Apr", object_title="Q2 2026 scorecard",
                         ), doc=make_doc(period_start=date(2026, 4, 1), period_end=date(2026, 6, 30)))
    assert obs.period_end == date(2026, 4, 30) and obs.period_grain == "month"
    assert "PERIOD_CONFLICT" not in codes(obs)


def test_period_conflict_lowers_confidence():
    obs = one(table_cell("5,600", row_label="TRx", col_header="April 2026", object_title="May 2026 scorecard"))
    assert "PERIOD_CONFLICT" in codes(obs)
    assert obs.period_confidence < 0.6
    assert obs.confidence == pytest.approx(0.5)


def test_value_beyond_data_through_is_a_conflict():
    obs = one(table_cell("5,600", row_label="TRx", col_header="June 2026", footnotes=FOOTER))
    assert "PERIOD_CONFLICT" in codes(obs)


def test_quarter_footer_uses_document_grain():
    doc = make_doc(report_type="executive_summary", period_start=date(2026, 4, 1), period_end=date(2026, 6, 30),
                   period_label="Q2 2026", publication_date=date(2026, 7, 1))
    obs = one(table_cell("17,220", row_label="TRx", col_header="Value", footnotes=["Data through June 2026"]), doc=doc)
    assert (obs.period_start, obs.period_end, obs.period_grain) == (date(2026, 4, 1), date(2026, 6, 30), "quarter")


def test_bare_quarter_in_pdf_text_line():
    doc = make_doc(report_type="executive_summary", period_start=date(2026, 4, 1), period_end=date(2026, 6, 30),
                   )
    v = make_value("17,220", method=ExtractionMethod.PDF_TEXT, object_kind="pdf_text", number_source="text",
                   text_line="Q2 TRx 17,220 (panel)")
    obs = one(v, doc=doc)
    assert obs.period_grain == "quarter" and obs.period_end == date(2026, 6, 30) and obs.label == "TRx"


def test_period_unknown():
    doc = make_doc(period_start=None, period_end=None, period_label=None, publication_date=None)
    obs = one(table_cell("5,910", row_label="TRx"), doc=doc)
    assert obs.period_end is None and obs.period_source == "none" and obs.period_confidence == 0.0
    assert "PERIOD_UNKNOWN" in codes(obs)
    assert obs.confidence == pytest.approx(1.0)


def test_image_placeholder_becomes_reviewable_observation():
    v = make_value(None, method=ExtractionMethod.IMAGE_PLACEHOLDER, object_kind="image", object_title="Picture 3",
                   page_title="Field highlights")
    obs = one(v)
    assert obs.value is None and obs.unit is None
    assert "IMAGE_NOT_PARSED" in codes(obs)
    assert obs.confidence == 0.0
    assert obs.label == "Picture 3"


def test_image_flag_not_duplicated():
    v = make_value(None, method=ExtractionMethod.IMAGE_PLACEHOLDER, object_kind="image",
                   flags=[{"code": "IMAGE_NOT_PARSED", "dimension": "extraction", "detail": "large image"}])
    assert [f["code"] for f in one(v).flags].count("IMAGE_NOT_PARSED") == 1


def test_hidden_slide_flag_from_page():
    v = table_cell("5,910", row_label="TRx", col_header="May 2026", page=7)
    obs = one(v, pages=[make_page(7, "Backup", hidden=True)])
    assert "HIDDEN_SLIDE" in codes(obs)


def test_page_title_filled_from_pages():
    v = table_cell("1,265.7", row_label="TRx", col_header="May 2026", page=4)
    obs = one(v, pages=[make_page(4, "West region performance")])
    assert obs.scope == {"region": "West"}
    assert "West region performance" in obs.label_context


def test_upstream_flags_are_copied():
    v = chart_point(1.0, category="May-26", flags=[{"code": "NEARBY_LABEL_UNVERIFIED", "dimension": "meaning",
                                                     "detail": "x"}])
    assert "NEARBY_LABEL_UNVERIFIED" in codes(one(v))


def test_plan_series_is_flagged_and_labeled():
    obs = one(chart_point(6000.0, category="May-26", series_name="Plan", object_title="TRx"))
    assert obs.label == "TRx Plan" and "NON_ACTUAL_SERIES" in codes(obs)


def test_generic_series_name_falls_back_to_title():
    obs = one(chart_point(6000.0, category="May-26", series_name="Series 1", object_title="Total prescriptions"))
    assert obs.label == "Total prescriptions"


def test_total_row_is_brand_total():
    obs = one(table_cell("5,910", row_label="Total", col_header="TRx"))
    assert obs.label == "TRx" and obs.scope == {}


def test_missing_label_is_flagged():
    obs = one(table_cell("5,910", row_label="2026", col_header=None))
    assert obs.label == "" and "LABEL_MISSING" in codes(obs)


def test_no_dimension_values_means_no_scope():
    obs = one(chart_point(1.0, category="West", series_name="NRx"), dims=None)
    assert obs.scope == {}


def test_observation_id_is_value_id_plus_version():
    v = table_cell("5,910", row_label="TRx", col_header="May 2026")
    assert one(v).observation_id == stable_id(v.value_id, "v1")
    assert one(v, interpretation_version="v2").observation_id == stable_id(v.value_id, "v2")


def test_interpret_is_deterministic_except_timestamp():
    values = [table_cell("5,910", row_label="TRx", col_header="May 2026", footnotes=FOOTER),
              chart_point(410.0, category="West", series_name="NRx")]
    a, b = run(values), run(values)
    strip = [replace(o, created_at=None) for o in a]
    assert strip == [replace(o, created_at=None) for o in b]


def test_one_observation_per_value():
    values = [table_cell("n/a", row_label="TRx"), make_value(None, method=ExtractionMethod.IMAGE_PLACEHOLDER),
              table_cell("5,910", row_label="TRx", col_header="May 2026")]
    assert len(run(values)) == 3


def test_fingerprint_stable_across_months():
    june = fingerprint(report_type="monthly_brand_review", page_title="TRx trend through May 2026",
                       object_kind="chart", object_title="TRx Dec-25 to May-26", label="TRx", col_header=None)
    july = fingerprint(report_type="monthly_brand_review", page_title="TRx trend through June 2026",
                       object_kind="chart", object_title="TRx Jan-26 to Jun-26", label="TRx", col_header=None)
    assert june == july
    other = fingerprint(report_type="executive_summary", page_title="TRx trend through June 2026",
                        object_kind="chart", object_title="TRx Jan-26 to Jun-26", label="TRx", col_header=None)
    assert other != june


def test_fingerprint_parts_mask_dates_quarters_and_digits():
    parts = fingerprint_parts("monthly_brand_review", "Q2'26 review FY2026", "table", "Top 20 HCPs", "TRx", "May 2026")
    assert parts == ("monthly_brand_review", "<q>'# review <fy>", "table", "top # hcps", "trx", "<m> <y>")
    assert mask_text("2Q26 results") == "<q># results"
    assert mask_text("Data through 05/31/2026") == "data through #/#/<y>"


def test_fingerprint_of_observation_returns_its_own():
    obs = one(table_cell("5,910", row_label="TRx", col_header="May 2026"))
    assert fingerprint(obs) == obs.fingerprint
    assert fingerprint({"report_type": "monthly_brand_review", "label": "TRx"}) == fingerprint(
        report_type="monthly_brand_review", label="TRx")


def test_table_cells_of_one_row_share_layout_across_period_columns():
    may = one(table_cell("5,910", row_label="TRx", col_header="May 2026"))
    jun = one(table_cell("6,010", row_label="TRx", col_header="Jun 2026"))
    assert may.fingerprint == jun.fingerprint


def test_period_factor_table():
    assert period_factor(0.95) == 1.0
    assert period_factor(0.6) == 0.9
    assert period_factor(0.475) == 0.5
    assert period_factor(0.0) == 1.0


def test_interpret_value_without_pages():
    obs = interpret_value(make_doc(), table_cell("5,910", row_label="TRx", col_header="May 2026"),
                          index=__import__("kpi_evidence.standardize", fromlist=["_ScopeIndex"])._ScopeIndex.build(DIMS))
    assert obs.label == "TRx"


def test_observation_round_trips_through_the_store(mem_store, registry):
    values = [table_cell("84.4%", row_label="West", col_header="Call attainment", footnotes=FOOTER),
              make_value(None, method=ExtractionMethod.IMAGE_PLACEHOLDER, object_kind="image")]
    obs = run(values, registry=registry)
    from kpi_evidence.contracts import Observation

    mem_store.replace(Observation, {"doc_id": obs[0].doc_id}, obs)
    back = {o.observation_id: o for o in mem_store.read(Observation, "doc_id = :d", {"d": obs[0].doc_id})}
    assert back[obs[0].observation_id].scope == {"region": "West"}
    assert back[obs[0].observation_id].period_end == date(2026, 5, 31)
    assert back[obs[1].observation_id].value is None


def test_causal_claim_text_becomes_lift_phrase_with_scope():
    v = text_value("+8%", "Speaker programs drove +8% TRx lift in the East", page_title="Speaker program lift - May 2026")
    obs = one(v)
    assert obs.label == "TRx lift from Speaker programs"
    assert obs.scope == {"region": "East"}
    assert "CAUSAL_LANGUAGE" in codes(obs) and "DERIVED_CHANGE" not in codes(obs)
    assert obs.period_end == date(2026, 5, 31) and obs.value == pytest.approx(0.08)


def test_passive_causal_wording():
    obs = one(text_value("+8%", "+8% TRx lift driven by speaker programs"))
    assert obs.label == "TRx lift from speaker programs" and "CAUSAL_LANGUAGE" in codes(obs)


def test_breakdown_phrase_is_not_part_of_the_label():
    obs = one(chart_point(319.9, category="East", series_name="May 2026", object_title="NRx by region"))
    assert obs.label == "NRx" and obs.scope == {"region": "East"} and obs.period_end == date(2026, 5, 31)
    assert obs.period_source == "series"
