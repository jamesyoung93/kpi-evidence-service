from __future__ import annotations

import pytest
from hints_testkit import ACCOUNT, PANEL, messages_for, row, table

from kpi_evidence.contracts import RegistryStatus
from kpi_evidence.hints import (
    brand_id_for, date_expression, kpi_slug, parse_date_format, parse_hints, parse_priority, parse_tolerance,
    parse_unit, sql_fragment_problems,
)
from kpi_evidence.registry import GRAINS


@pytest.mark.parametrize("text, unit, expected, warned", [
    ("0.5%", "count", {"rel": 0.005, "abs": 0.0}, False),
    ("0.05pp", "fraction", {"rel": 0.0, "abs": 0.0005}, False),
    ("abs:12", "count", {"rel": 0.0, "abs": 12.0}, False),
    ("abs = 0.001", "fraction", {"rel": 0.0, "abs": 0.001}, False),
    ("0.005", "count", {"rel": 0.005, "abs": 0.0}, False),
    ("0.5", "count", {"rel": 0.5, "abs": 0.0}, True),
    ("0.5%; abs:2", "count", {"rel": 0.005, "abs": 2.0}, False),
    ("0.05pp", "count", None, True),
    ("12", "count", None, True),
    ("loose", "count", None, True),
    (None, "count", None, False),
])
def test_parse_tolerance(text, unit, expected, warned):
    tol, notes = parse_tolerance(text, unit)
    if expected is None:
        assert tol is None
    else:
        assert tol == pytest.approx(expected)
    assert bool(notes) == warned


@pytest.mark.parametrize("text, unit", [("percent", "fraction"), ("%", "fraction"), ("Fraction", "fraction"),
                                        ("USD", "usd"), ("$", "usd"), ("count", "count"), (None, None)])
def test_parse_unit(text, unit):
    assert parse_unit(text) == (unit, None)


def test_parse_unit_rejects_unknown():
    unit, err = parse_unit("furlongs")
    assert unit is None and "furlongs" in err


def test_date_formats():
    assert parse_date_format(None) == ("date", None)
    assert parse_date_format("YYYY-MM") == ("yyyy-MM", None)
    assert parse_date_format("yyyymm") == ("yyyyMM", None)
    assert parse_date_format("dd/mm/yyyy")[1]
    assert date_expression("month", "date") == "month"
    assert date_expression("month", "yyyy-MM") == "CAST(month || '-01' AS DATE)"
    assert date_expression("ym", "yyyyMM") == (
        "CAST(CONCAT(SUBSTR(CAST(ym AS STRING), 1, 4), '-', SUBSTR(CAST(ym AS STRING), 5, 2), '-01') AS DATE)")


def test_slugs_and_brand_ids(registry):
    assert kpi_slug("TRx share") == "trx_share"
    assert kpi_slug("Call-plan attainment") == "call_plan_attainment"
    assert kpi_slug("12-month persistency") == "kpi_12_month_persistency"
    assert brand_id_for("Kestrel", registry) == "KESTREL"
    assert brand_id_for("Falcon Rx") == "FALCON_RX"


@pytest.mark.parametrize("text, fragment", [
    ("SUM(x); DROP TABLE t", "';'"), ("SUM(x) -- note", "comments"), ("SUM(x) /* c */", "comments"),
    ("x IN (SELECT a FROM t) OR 1 = 1 AND DELETE", "DELETE"), ("{measure}", "braces"), ("name = 'open", "quote"),
    ("CALL proc()", "CALL"),
])
def test_sql_fragment_problems(text, fragment):
    assert any(fragment in p for p in sql_fragment_problems(text))


def test_sql_fragment_allows_keywords_inside_literals_and_functions():
    assert sql_fragment_problems("channel = 'CALL CENTER' AND status <> 'DELETE'") == []
    assert sql_fragment_problems("SUM(CASE WHEN load_id = 'x' THEN REPLACE(a, 'b', 'c') END)") == []


def test_single_table_hint_builds_a_draft_kpi():
    hints = parse_hints(table(row(kpi="Active writers", aliases="Writers; Active HCPs", unit="count",
                                  scope_columns="region=region_name; territory")))
    kpi = hints.kpis["active_writers"]
    assert kpi.status == RegistryStatus.DRAFT and kpi.approved_by is None
    assert kpi.label == "Active writers" and set(kpi.synonyms) == {"Writers", "Active HCPs", "Active writers"}
    assert kpi.dimensions == ("region", "territory") and kpi.grains == GRAINS
    assert kpi.tolerance.rel == 0.005 and kpi.tolerance.abs == 0.0
    assert kpi.raw["definition_source"] == "hint" and kpi.raw["hint_row"] == 1
    p = kpi.platform
    assert p.template == f"SELECT SUM(trx_units) AS value\nFROM {ACCOUNT}\nWHERE ${{window}} AND ${{scope}}"
    assert p.date_expr == "CAST(month || '-01' AS DATE)"
    assert p.scope_columns == {"region": "region_name", "territory": "territory"}
    assert p.source_views == (ACCOUNT,) and p.params == {}
    assert "brand" not in p.template
    assert hints.messages.empty


def test_brand_column_and_filters_are_bound():
    kpi = parse_hints(table(row(brand_column="brand_cd", filters="channel = 'RETAIL' OR channel = 'MAIL'"))).kpis["trx"]
    assert "WHERE brand_cd = ${brand} AND ${window} AND ${scope} AND (channel = 'RETAIL' OR channel = 'MAIL')" in \
        kpi.platform.template


def test_ratio_hint_and_percent_defaults():
    kpi = parse_hints(table(row(kpi="trx_share", value_sql=None, numerator="SUM(trx_units)",
                                denominator="SUM(advanced_therapy_trx)", unit="percent"))).kpis["trx_share"]
    assert kpi.unit == "fraction" and kpi.aggregation == "ratio_of_sums"
    assert kpi.tolerance.abs == 0.0005 and kpi.tolerance.rel == 0.0
    assert kpi.platform.ratio["numerator"].startswith("SELECT SUM(trx_units) AS value FROM")


def test_candidate_tables_become_a_source_param():
    kpi = parse_hints(table(row(table=f"{PANEL}; {ACCOUNT}; platform_demo.other_feed"))).kpis["trx"]
    source = kpi.platform.params["source"]
    assert source.hypothesis == "source" and source.default == "primary" and source.permitted == ("primary",)
    assert source.options == {"primary": PANEL, "alt_1": ACCOUNT, "alt_2": "platform_demo.other_feed"}
    assert source.describe == source.options
    assert "FROM {source}" in kpi.platform.template
    assert kpi.platform.source_views == (PANEL, ACCOUNT, "platform_demo.other_feed")
    assert kpi.platform.checks["period_has_rows"].endswith("FROM {source} WHERE ${window}")


def test_alternatives_become_a_measure_param():
    hints = parse_hints(table(row(kpi="call_attainment", value_sql="SUM(calls) / NULLIF(SUM(call_plan), 0)",
                                  unit="percent", alternatives="writers_only=SUM(CASE WHEN x = 1 THEN calls END) / "
                                                               "NULLIF(SUM(call_plan), 0); Mean of ratios = AVG(r)")))
    measure = hints.kpis["call_attainment"].platform.params["measure"]
    assert measure.hypothesis == "measure" and measure.default == "primary" and measure.permitted == ("primary",)
    assert measure.options["primary"] == "SUM(calls) / NULLIF(SUM(call_plan), 0)"
    assert measure.options["writers_only"].startswith("SUM(CASE WHEN x = 1")
    assert measure.options["mean_of_ratios"] == "AVG(r)"
    assert measure.keywords["writers_only"] == ["writers only"]
    assert hints.kpis["call_attainment"].platform.template.startswith("SELECT {measure} AS value")


def test_multiline_alternative_from_a_spreadsheet_cell_stays_one_expression():
    hints = parse_hints(table(row(alternatives="normalized=SUM(\n  trx_normalized\n)")))
    assert hints.kpis["trx"].platform.params["measure"].options["normalized"] == "SUM(\n  trx_normalized\n)"


def test_alternatives_with_a_ratio_are_ignored_with_a_warning():
    hints = parse_hints(table(row(value_sql=None, numerator="SUM(a)", denominator="SUM(b)", alternatives="x=SUM(c)")))
    assert "measure" not in hints.kpis["trx"].platform.params
    assert any("value_sql only" in m for m in messages_for(hints, 1, "warning"))


def test_report_only_row():
    hints = parse_hints(table({"kpi": "Brand awareness", "unit": "percent", "grain": "quarter; month"}))
    kpi = hints.kpis["brand_awareness"]
    assert kpi.platform is None and kpi.unit == "fraction" and kpi.grains == ("quarter", "month")
    assert hints.rows[0].report_only and hints.overview().loc[0, "check"] == "report only"


@pytest.mark.parametrize("fields, column, fragment", [
    ({"value_sql": "SUM(x); DROP TABLE t"}, "value_sql", "';'"),
    ({"filters": "1 = 1 -- all"}, "filters", "comments"),
    ({"numerator": "SUM(a)", "denominator": "SUM(b) UNION SELECT 1 FROM x; DELETE FROM y", "value_sql": None},
     "denominator", "DELETE"),
    ({"table": "catalog.schema.table.extra"}, "table", "dotted identifier"),
    ({"table": "my-catalog.s.t"}, "table", "dotted identifier"),
    ({"date_column": "month; x"}, "date_column", "plain column name"),
    ({"brand_column": "brand name"}, "brand_column", "plain column name"),
    ({"scope_columns": "region=region name"}, "scope_columns", "plain column name"),
    ({"value_sql": None, "numerator": "SUM(a)"}, "denominator", "both numerator and denominator"),
    ({"value_sql": None}, "value_sql", "no value_sql"),
    ({"numerator": "SUM(a)", "denominator": "SUM(b)"}, "value_sql", "not both"),
    ({"date_column": None}, "date_column", "required"),
    ({"table": None}, "table", "no table"),
    ({"unit": "furlongs"}, "unit", "furlongs"),
    ({"date_format": "dd.mm.yyyy"}, "date_format", "date_format"),
    ({"alternatives": "no equals sign"}, "alternatives", "name=EXPRESSION"),
    ({"alternatives": "primary=SUM(a)"}, "alternatives", "reserved"),
])
def test_invalid_rows_are_skipped_with_an_error(fields, column, fragment):
    hints = parse_hints(table(row(**fields), row(kpi="nrx", value_sql="SUM(nrx)")))
    assert "trx" not in hints.kpis and "nrx" in hints.kpis
    errors = hints.errors
    assert not hints.rows[0].accepted
    assert any(column == c and fragment in m for c, m in zip(errors["column"], errors["message"])), errors.to_dict("records")


def test_warnings_keep_the_row():
    hints = parse_hints(table(row(table="rx_account_monthly", scope_columns="region=region; brick=brick_id",
                                  grain="month; fortnight")))
    assert "trx" in hints.kpis
    warnings = " | ".join(messages_for(hints, 1, "warning"))
    assert "no schema" in warnings and "'brick'" in warnings and "'fortnight'" in warnings
    assert hints.kpis["trx"].platform.scope_columns == {"region": "region"}


def test_blank_rows_and_blank_kpi():
    hints = parse_hints(table(row(), {"kpi": "", "table": ""}, {"kpi": "", "value_sql": "SUM(x)"}))
    assert list(hints.kpis) == ["trx"]
    assert [r.row for r in hints.rows] == [1, 3]
    assert messages_for(hints, 3, "error") == ["kpi is blank; the row is skipped"]


def test_duplicate_kpi_rows_and_brand_choice(registry):
    dup = parse_hints(table(row(), row(value_sql="SUM(trx_normalized)")))
    assert dup.kpis["trx"].raw["hint_row"] == 1
    assert "duplicate row" in messages_for(dup, 2, "error")[0]
    by_brand = table(row(brand="Falcon", value_sql="SUM(falcon_units)"), row(brand="Kestrel"), row(value_sql="SUM(any)"))
    chosen = parse_hints(by_brand, base_registry=registry, default_brand="kestrel")
    assert chosen.kpis["trx"].raw["hint_row"] == 2
    assert "for brand FALCON" in messages_for(chosen, 1, "warning")[0]
    assert "defines trx for brand KESTREL" in messages_for(chosen, 3, "warning")[0]
    unscoped = parse_hints(by_brand.iloc[:2], base_registry=registry)
    assert unscoped.kpis["trx"].raw["hint_row"] == 1
    assert "several brands" in messages_for(unscoped, 2, "warning")[0]
    assert unscoped.brands == ["FALCON"] and unscoped.default_brand == "FALCON"


def test_messages_frame_shape():
    hints = parse_hints(table(row(unit="furlongs")))
    assert list(hints.messages.columns) == ["row", "kpi", "level", "column", "message"]
    assert str(hints.messages["row"].dtype) == "Int64"
    overview = hints.overview()
    assert overview.loc[0, "used"] == False and overview.loc[0, "errors"] == 1


def test_override_keeps_registry_metadata_and_replaces_the_platform(registry):
    base = registry.kpis["trx"]
    hints = parse_hints(table(row(kpi="TRx", aliases="Rx volume", owner="me@example.com",
                                  scope_columns="region=region")), base_registry=registry)
    kpi = hints.kpis["trx"]
    assert hints.rows[0].overrides == "trx"
    assert (kpi.label, kpi.definition, kpi.harness_link) == (base.label, base.definition, base.harness_link)
    assert kpi.version == 0 and base.version >= 1
    assert kpi.synonyms[:len(base.synonyms)] == base.synonyms and "Rx volume" in kpi.synonyms
    assert kpi.owner == "me@example.com" and kpi.dimensions == base.dimensions
    assert kpi.tolerance == base.tolerance and kpi.grains == base.grains
    assert kpi.status == RegistryStatus.DRAFT and kpi.approved_by is None
    assert set(kpi.platform.params) == set() and ACCOUNT in kpi.platform.template
    assert kpi.definition_hash() != base.definition_hash()


def test_override_with_unit_and_tolerance(registry):
    kpi = parse_hints(table(row(kpi="calls", value_sql="SUM(calls)", tolerance="abs:3")),
                      base_registry=registry).kpis["calls"]
    assert kpi.tolerance.abs == 3.0 and kpi.tolerance.rel == 0.0


def test_name_matching_a_registry_synonym_redefines_that_kpi(registry):
    hints = parse_hints(table(row(kpi="Ex-factory units", table="platform_restricted.exfactory_monthly",
                                  value_sql="SUM(units_shipped)")), base_registry=registry)
    assert list(hints.kpis) == ["exfactory_units"]
    assert "matched by name" in messages_for(hints, 1, "warning")[0]


def test_causal_registry_kpi_cannot_get_a_table(registry):
    hints = parse_hints(table(row(kpi="speaker_program_lift", unit="percent")), base_registry=registry)
    assert hints.kpis == {}
    assert any("causal" in m for m in messages_for(hints, 1, "error"))


def test_label_collisions_between_rows_and_with_the_registry(registry):
    hints = parse_hints(table(row(kpi="Rx units", aliases="Volume"),
                              row(kpi="Script volume", aliases="Volume; Referrals")), base_registry=registry)
    assert "Volume" in hints.kpis["rx_units"].synonyms
    assert "Volume" not in hints.kpis["script_volume"].synonyms
    warnings = " | ".join(messages_for(hints, 2, "warning"))
    assert "already belongs to rx_units" in warnings and "synonym of registry KPI referrals_in" in warnings


@pytest.mark.parametrize("text, expected, problem", [
    (None, None, False), ("1", 1, False), ("2.0", 2, False), ("0", None, True), ("1.5", None, True),
    ("high", None, True),
])
def test_parse_priority(text, expected, problem):
    assert parse_priority(text)[0] == expected
    assert bool(parse_priority(text)[1]) == problem


def test_priority_column_reaches_the_kpi_definition():
    hints = parse_hints(table(row(kpi="TRx", priority="1"), row(kpi="NRx", value_sql="SUM(nrx)", priority="soon")))
    assert hints.kpis["trx"].raw["priority"] == 1 and hints.kpis["nrx"].raw["priority"] is None
    assert any("priority" in m for m in messages_for(hints, 2, "warning"))
    priorities = hints.overview()["priority"]
    assert str(priorities.dtype) == "Int64" and priorities.iloc[0] == 1 and priorities.isna().iloc[1]
    assert hints.kpis["trx"].definition_hash() == parse_hints(table(row(kpi="TRx"))).kpis["trx"].definition_hash()
