from __future__ import annotations

import dataclasses
from datetime import date

import pandas as pd
import pytest

from kpi_evidence.contracts import STATUS_INFO, ValidationStatus
from kpi_evidence.report import (
    COMPARISON_COLUMNS, DOCUMENT_COLUMNS, EXTRACTION_COLUMNS, FLAG_ORDER, ISSUE_COLUMNS, KPI_COLUMNS, SQL_COLUMNS,
    build_result,
)

CONTRACT_COLUMNS = [
    "report_file", "source_type", "page", "object", "label_as_printed", "value_as_printed", "report_value", "unit",
    "period_label", "period_start", "period_end", "period_grain", "scope", "kpi_id", "kpi_label", "mapping_method",
    "definition_source", "definition", "platform_tables", "sql", "platform_value", "abs_diff", "rel_diff", "tolerance",
    "match_basis", "flag", "status", "status_label", "reason", "reused", "matched_alternative", "alternative_value",
    "metadata_confirmed", "result_id", "observation_id", "doc_id",
]
SOURCE_TYPES = {"PowerPoint chart", "PowerPoint table", "Embedded Excel", "Excel workbook", "Slide text", "PDF table",
                "PDF text", "Image (not read)", "AI-parsed page", "Reviewer correction"}
EXPECTED_FLAG = {
    "VERIFIED": "GOOD", "REPORT_ONLY": "GOOD", "UNMAPPED_CAPTURED": "INFO", "VARIANCE_REVIEW": "REVIEW",
    "INCOMPATIBLE_DEFINITION": "REVIEW", "UNCERTAIN_EXTRACTION": "REVIEW", "AMBIGUOUS_MAPPING": "REVIEW",
    "REPORT_CONFLICT": "REVIEW", "MISSING_ACCESS": "BLOCKED", "PLATFORM_DATA_ISSUE": "BLOCKED", "BLOCKED": "BLOCKED",
}


def _rows(result, **eq):
    df = result.comparisons
    for col, value in eq.items():
        df = df[df[col] == value]
    return df


def test_comparison_columns_exact_order(report_result):
    assert list(report_result.comparisons.columns) == CONTRACT_COLUMNS
    assert list(COMPARISON_COLUMNS) == CONTRACT_COLUMNS


def test_one_row_per_current_result(report_run, report_result):
    r = report_run
    marks = ", ".join(f":d{i}" for i in range(len(r.doc_ids)))
    n = r.store.query(f"SELECT COUNT(*) AS n FROM {r.store.fq('kpi_results')} WHERE is_current = TRUE "
                      f"AND doc_id IN ({marks})", {f"d{i}": d for i, d in enumerate(r.doc_ids)}).iloc[0, 0]
    assert len(report_result.comparisons) == int(n) > 30
    assert report_result.comparisons["result_id"].is_unique


def test_flag_mapping_and_status_labels(report_result):
    df = report_result.comparisons
    statuses = set(df["status"])
    assert {"VERIFIED", "REPORT_ONLY", "UNMAPPED_CAPTURED", "INCOMPATIBLE_DEFINITION", "UNCERTAIN_EXTRACTION",
            "REPORT_CONFLICT", "MISSING_ACCESS"} <= statuses
    for row in df.itertuples():
        assert row.flag == EXPECTED_FLAG[row.status], row.status
        assert row.status_label == STATUS_INFO[ValidationStatus(row.status)].label


def test_flag_mapping_covers_every_status():
    from kpi_evidence.report import FLAG_BY_STATUS, flag_for

    assert FLAG_BY_STATUS == EXPECTED_FLAG
    assert set(FLAG_BY_STATUS) == {s.value for s in ValidationStatus}
    assert flag_for("SOMETHING_NEW") == "BLOCKED"


def test_rows_sorted_by_flag_then_kpi_and_period(report_result):
    df = report_result.comparisons
    order = [FLAG_ORDER.index(f) for f in df["flag"]]
    assert order == sorted(order)
    good = df[df["flag"] == "GOOD"]
    labels = [str(x).lower() for x in good["kpi_label"]]
    assert labels == sorted(labels)
    trx = good[good["kpi_id"] == "trx"]
    assert list(trx["period_end"]) == sorted(trx["period_end"])


def test_sql_present_for_every_platform_value(report_run, report_result):
    df = report_result.comparisons
    with_value = df[df["platform_value"].notna()]
    assert len(with_value) > 20
    for row in with_value.itertuples():
        kpi = report_run.registry.kpis[row.kpi_id]
        assert row.sql, row.result_id
        assert any(view in row.sql for view in kpi.platform.source_views), (row.kpi_id, row.sql)
        assert row.platform_tables and all(t in row.sql for t in row.platform_tables.split("; "))
        assert str(row.period_end) in row.sql


def test_ratio_sql_shows_both_components(report_result):
    share = _rows(report_result, kpi_id="trx_share")
    assert len(share)
    for sql in share["sql"]:
        assert "-- numerator" in sql and "-- denominator" in sql


def test_blocked_row_keeps_the_attempted_sql(report_result):
    row = _rows(report_result, kpi_id="exfactory_units").iloc[0]
    assert row["status"] == "MISSING_ACCESS" and row["flag"] == "BLOCKED"
    assert pd.isna(row["platform_value"])
    assert row["platform_tables"] == "platform_restricted.exfactory_monthly"


def test_source_types(report_result):
    df = report_result.comparisons
    assert set(df["source_type"]) <= SOURCE_TYPES
    chart = _rows(report_result, kpi_id="trx", object="TRx by month")
    assert set(chart["source_type"]) == {"PowerPoint chart"}
    assert set(_rows(report_result, report_file="Kestrel_Executive_Summary_Q2-2026.pdf")["source_type"]) == {"PDF table"}
    assert set(_rows(report_result, report_file="Kestrel_Field_Update_scan.pdf")["source_type"]) == {"Image (not read)"}
    assert set(_rows(report_result, kpi_id="speaker_program_lift")["source_type"]) == {"Slide text"}
    assert "PowerPoint table" in set(_rows(report_result, kpi_id="samples")["source_type"])


def test_row_content_as_printed(report_result):
    row = _rows(report_result, kpi_id="trx", value_as_printed="5.7K").iloc[0]
    assert row["page"] == 3 and row["report_value"] == 5700.0 and row["label_as_printed"] == "TRx"
    assert row["status"] == "VERIFIED" and row["match_basis"] == "WITHIN_ROUNDING"
    assert "display rounding" in row["tolerance"]
    assert row["definition_source"] == "registry" and row["definition"].startswith("Total prescriptions")
    assert row["mapping_method"] == "SYNONYM" and row["scope"] == "total" and row["period_grain"] == "month"
    assert row["period_end"] == date(2026, 5, 31) and row["period_label"] == "May 2026"
    region = _rows(report_result, kpi_id="nrx", scope="region=West")
    assert len(region) == 1 and region.iloc[0]["status"] == "VERIFIED"
    unmapped = _rows(report_result, status="UNMAPPED_CAPTURED")
    assert set(unmapped["definition_source"]) == {"none"} and unmapped["kpi_id"].isna().all()


def test_matched_alternative_columns(report_result):
    row = _rows(report_result, kpi_id="speaker_attendance").iloc[0]
    assert row["status"] == "INCOMPATIBLE_DEFINITION"
    assert row["matched_alternative"].startswith("measure=distinct_hcps (")
    assert row["alternative_value"] == pytest.approx(46.0)
    assert row["platform_value"] == pytest.approx(83.0)


def test_metadata_confirmed_flag(report_result, report_run):
    df = report_result.comparisons
    unconfirmed = df[df["report_file"] == "kestrel-update.pptx"]
    assert len(unconfirmed) and not unconfirmed["metadata_confirmed"].any()
    assert df[df["report_file"] != "kestrel-update.pptx"]["metadata_confirmed"].all()
    docs = report_result.documents.set_index("report_file")
    assert not docs.loc["kestrel-update.pptx", "metadata_confirmed"]
    assert docs.loc["kestrel-update.pptx", "metadata_basis"] == "proposed metadata used without confirmation"
    issues = report_result.issues
    hit = issues[(issues["category"] == "metadata not confirmed") & (issues["report_file"] == "kestrel-update.pptx")]
    assert len(hit) == 1 and "check.confirm" in hit.iloc[0]["remedy"]


def test_kpi_columns_and_verdicts(report_result):
    kpis = report_result.kpis
    assert list(kpis.columns) == list(KPI_COLUMNS)
    by = kpis.set_index("kpi_id")
    assert by.loc["calls", "verdict"] == "validated"
    assert by.loc["calls", "periods_compared"] == by.loc["calls", "periods_validated"] == 3
    assert by.loc["trx", "verdict"] == "partly validated"
    assert by.loc["speaker_attendance", "verdict"] == "matches alternative"
    assert by.loc["exfactory_units", "verdict"] == "not computable"
    assert "PERMISSION_DENIED" in by.loc["exfactory_units", "error"]
    assert by.loc["brand_awareness", "verdict"] == "report only"
    assert by.loc["active_referrers", "verdict"] == "not compared"
    assert by.loc["active_referrers", "comparisons"] == 0
    assert set(kpis["definition_source"]) == {"registry"}
    assert "kpi_gov.gv_rx_account_monthly" in by.loc["trx", "platform_tables"]
    assert "Validated in 3 of 3" in by.loc["calls", "verdict_detail"]


def test_kpi_verdict_rules_on_hand_built_results():
    from kpi_evidence.contracts import KpiResult, MatchBasis
    from kpi_evidence.report import kpi_verdict

    def result(i, status, end, *, pv=100.0, reason="x", expl=None):
        return KpiResult(result_id=f"r{i}", result_version=1, observation_id=f"o{i}", doc_id="d", brand="B",
                         kpi_id="k", kpi_version=1, definition_hash="h", scope_key="{}", period_start=None,
                         period_end=end, period_grain="month", report_value=100.0, unit="count", platform_value=pv,
                         abs_diff=None, rel_diff=None, tolerance={}, match_basis=MatchBasis.EXACT,
                         validation_status=ValidationStatus(status), reason_code=reason, reason_detail="detail",
                         explanation=expl or {}, definition_choices={}, extraction_method=None, recon_run_id=None,
                         sql_artifact_id=None, dq_status=None, is_current=True, created_at=None)

    may, jun = date(2026, 5, 31), date(2026, 6, 30)
    assert kpi_verdict(None, [])["verdict"] == "not compared"
    assert kpi_verdict(None, [result(1, "VERIFIED", may), result(2, "VERIFIED", jun)])["verdict"] == "validated"
    partly = kpi_verdict(None, [result(1, "VERIFIED", may), result(2, "VARIANCE_REVIEW", jun)])
    assert partly["verdict"] == "partly validated" and partly["periods_validated"] == 1
    assert kpi_verdict(None, [result(1, "VARIANCE_REVIEW", may)])["verdict"] == "not validated"
    alt = result(1, "INCOMPATIBLE_DEFINITION", may, reason="matches_alternative_definition",
                 expl={"alternative": "source=panel", "alternative_value": 100.0})
    assert kpi_verdict(None, [alt])["verdict"] == "matches alternative"
    blocked = result(1, "PLATFORM_DATA_ISSUE", may, pv=None, reason="platform_object_missing",
                     expl={"error": "TABLE_OR_VIEW_NOT_FOUND: main.x.y"})
    out = kpi_verdict(None, [blocked])
    assert out["verdict"] == "not computable" and "TABLE_OR_VIEW_NOT_FOUND" in out["error"]
    conflict = result(1, "REPORT_CONFLICT", may, expl={"conflict": {"previous": {"validation_status": "VERIFIED"}}})
    assert kpi_verdict(None, [conflict])["verdict"] == "validated"


def test_documents_frame(report_result):
    docs = report_result.documents
    assert list(docs.columns) == list(DOCUMENT_COLUMNS)
    assert len(docs) == 5
    by = docs.set_index("report_file")
    assert by.loc["Kestrel_Monthly_Brand_Review_2026-06.pptx", "status"] == "SUPERSEDED"
    assert by.loc["Kestrel_Monthly_Brand_Review_2026-06.pptx", "superseded_by"] == "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx"
    assert by.loc["Kestrel_Monthly_Brand_Review_2026-06.pptx", "comparisons"] == 0
    assert by.loc["Kestrel_Monthly_Brand_Review_2026-06_v2.pptx", "pages"] == 9
    assert by.loc["Kestrel_Executive_Summary_Q2-2026.pdf", "period_grain"] == "quarter"


def test_extraction_inventory(report_result):
    ext = report_result.extraction
    assert list(ext.columns) == list(EXTRACTION_COLUMNS)
    assert ext["value_id"].is_unique and len(ext) >= len(report_result.comparisons)
    chart = ext[(ext["object"] == "TRx by month") & (ext["category"] == "Dec-25")]
    assert len(chart) == 2
    assert chart.iloc[0]["location"].startswith("slide 2") and "point 1" in chart.iloc[0]["location"]
    assert "CACHE_MATCHES_WORKBOOK" in chart.iloc[0]["flags"]
    image = ext[ext["source_type"] == "Image (not read)"]
    assert len(image) and image["value_as_printed"].isna().all()


def test_sql_frame(report_result):
    sql = report_result.sql
    assert list(sql.columns) == list(SQL_COLUMNS)
    assert sql["alternative"].dtype == bool
    artifacts = sql[~sql["alternative"]]
    assert len(artifacts) and artifacts["value_sql"].str.contains("{as_of}", regex=False).all()
    ids = set(report_result.comparisons.loc[report_result.comparisons["platform_value"].notna(), "result_id"])
    assert artifacts["comparisons"].sum() == len(ids)
    variant = artifacts[(artifacts["kpi_id"] == "trx_share") & ~artifacts["governed_default"]]
    assert len(variant) == 1 and "basket=il17_class" in variant.iloc[0]["choices"]
    assert "region=West" in set(sql["scope"])


def test_issues_and_summary(report_result):
    issues = report_result.issues
    assert list(issues.columns) == list(ISSUE_COLUMNS)
    access = issues[issues["category"] == "No access to platform data"]
    assert len(access) == 1 and access.iloc[0]["kpi_id"] == "exfactory_units" and access.iloc[0]["severity"] == "error"
    s = report_result.summary
    assert s["comparisons"] == len(report_result.comparisons)
    assert sum(s["by_flag"].values()) == len(report_result.comparisons)
    assert s["tiles"]["matches_platform"] == int((report_result.comparisons["status"] == "VERIFIED").sum())
    assert s["tiles"]["other_numbers"] == int((report_result.comparisons["status"] == "UNMAPPED_CAPTURED").sum())
    assert s["kpi_source"] == "registry" and s["documents"] == 5 and s["timings"] == {"total_seconds": 1.5}
    assert s["by_document"]["Kestrel_Monthly_Brand_Review_2026-06.pptx"]["status"] == "SUPERSEDED"


class _HintSet:
    def __init__(self, messages, rows):
        self.messages, self.rows = messages, rows


def _hint_registry(registry):
    kpi = registry.kpis["calls"]
    raw = {**kpi.raw, "definition_source": "hint", "hint_row": 3, "hint_notes": "from the field team",
           "hint": {"kpi": "Calls", "kpi_id": "calls", "tables": ["main.field.calls_monthly", "main.field.calls_v2"],
                    "value_sql": "SUM(calls)", "date_column": "call_date", "notes": "from the field team"}}
    return dataclasses.replace(registry, kpis={**registry.kpis, "calls": dataclasses.replace(kpi, raw=raw)})


def test_hint_kpis_and_messages(report_run):
    r = report_run
    messages = pd.DataFrame([{"row": 4, "kpi": "nbrx_bad", "level": "error", "column": "value_sql",
                              "message": "value_sql contains ';' (only one aggregate expression is allowed)"}])
    hints = _HintSet(messages, [object(), object()])
    result = build_result(r.store, _hint_registry(r.registry), r.settings, r.doc_ids, hints=hints, run_id=r.run_id)
    calls = result.comparisons[result.comparisons["kpi_id"] == "calls"]
    assert len(calls) and set(calls["definition_source"]) == {"hint"}
    row = result.kpis.set_index("kpi_id").loc["calls"]
    assert row["definition_source"] == "hint" and row["hint_row"] == 3
    assert (row["hint_table"], row["hint_value_sql"], row["hint_notes"]) == (
        "main.field.calls_monthly; main.field.calls_v2", "SUM(calls)", "from the field team")
    assert "KPI table row" in result.to_html()
    assert "primary definition" in row["verdict_detail"]
    hint_issue = result.issues[result.issues["category"] == "KPI table"].iloc[0]
    assert hint_issue["severity"] == "error" and hint_issue["kpi_id"] == "nbrx_bad" and "row 4" in hint_issue["item"]
    assert result.summary["kpi_source"] == "KPI table + registry"
    assert result.summary["hint_rows"] == 2 and result.summary["hint_messages"] == 1


def test_hint_fields_and_messages_from_the_real_hints_module():
    hints = pytest.importorskip("kpi_evidence.hints")
    from kpi_evidence.report import hint_fields, hint_issues

    frame = pd.DataFrame([
        {"kpi": "Field calls", "table": "main.field.calls_monthly; main.field.calls_v2", "value_sql": "SUM(calls)",
         "date_column": "call_date", "notes": "field team"},
        {"kpi": "Bad one", "table": "main.x.y", "value_sql": "SUM(a); DROP TABLE x", "date_column": "d"},
        {"kpi": "Share", "table": "main.x.share", "numerator": "SUM(a)", "denominator": "SUM(b)", "date_column": "d",
         "unit": "percent"},
    ])
    hint_set = hints.parse_hints(frame)
    by_label = {k.label: k for k in hint_set.kpis.values()}
    assert hint_fields(by_label["Field calls"]) == {
        "table": "main.field.calls_monthly; main.field.calls_v2", "value_sql": "SUM(calls)", "notes": "field team",
        "row": 1}
    assert hint_fields(by_label["Share"])["value_sql"] == "(SUM(a)) / (SUM(b))"
    issues = hint_issues(hint_set)
    assert issues and {i["severity"] for i in issues} == {"error"}
    assert all(i["kpi_id"] == "Bad one" and i["item"].startswith("row 2") for i in issues)


def test_alternative_text():
    from kpi_evidence.report import alternative_text

    expl = {"alternative": "source=candidate_2",
            "differing_options": {"source": {"from": "primary", "to": "candidate_2", "describe": "main.s.panel"}}}
    assert alternative_text(expl) == "source=candidate_2 (main.s.panel)"
    assert alternative_text({"alternative": "basket=il17_class"}) == "basket=il17_class"
    assert alternative_text({}) is None and alternative_text(None) is None


def test_missing_state_tables_give_empty_frames_and_issues(settings, registry):
    from kpi_evidence.store.duckdb_store import DuckDBStore

    store = DuckDBStore(settings, ":memory:")
    result = build_result(store, registry, settings, ["0" * 64])
    for name, cols in (("comparisons", COMPARISON_COLUMNS), ("documents", DOCUMENT_COLUMNS),
                       ("extraction", EXTRACTION_COLUMNS), ("sql", SQL_COLUMNS), ("issues", ISSUE_COLUMNS)):
        assert list(getattr(result, name).columns) == list(cols), name
    assert result.comparisons.empty and result.documents.empty
    assert (result.issues["category"] == "report").sum() >= 1
    assert "documents" in " ".join(result.summary["state_errors"])
    assert set(result.kpis["verdict"]) == {"not compared"}
    assert result.to_html().startswith("<!doctype html>")


def test_empty_doc_list(mem_store, registry, settings):
    result = build_result(mem_store, registry, settings, [])
    assert result.comparisons.empty and result.issues.empty and result.summary["comparisons"] == 0


def test_relations_parser():
    from kpi_evidence.report import relations

    sql = ("-- numerator FROM nowhere\nSELECT EXTRACT(YEAR FROM d) AS y, SUM(x) AS value FROM main.s.fact f "
           "JOIN `main`.`s`.`dim` d ON f.k = d.k LEFT JOIN (SELECT k FROM main.s.other) o ON o.k = f.k "
           "WHERE note = 'from main.s.fake' AND a IS DISTINCT FROM b")
    assert relations(sql) == ["main.s.fact", "main.s.dim", "main.s.other"]
    assert relations(None) == [] and relations("SELECT 1") == []


def test_focus_limits_comparisons_and_verdicts(report_run, report_result):
    r = report_run
    focused = build_result(r.store, r.registry, r.settings, r.doc_ids, run_id=r.run_id, focus_kpis={"trx"})
    assert set(focused.comparisons["kpi_id"].dropna()) == {"trx"}
    assert list(focused.kpis["kpi_id"]) == ["trx"] and focused.summary["focus"] == ["trx"]
    trx_rows = report_result.comparisons[report_result.comparisons["kpi_id"] == "trx"]
    assert set(trx_rows["result_id"]) <= set(focused.comparisons["result_id"])
    ext = focused.extraction
    assert ext["in_focus"].dtype == bool and (~ext["in_focus"]).any()
    processed = set(focused.documents.loc[focused.documents["status"] == "PROCESSED", "doc_id"])
    outside = ext[~ext["in_focus"] & ext["doc_id"].isin(processed)]
    assert len(outside) and set(outside["reason"]) == {"outside this run's focus"}
    assert not ext.loc[~ext["doc_id"].isin(processed), "reason"].eq("outside this run's focus").any()
    assert ext.loc[~ext["in_focus"], "status"].isna().all()
    assert report_result.extraction["in_focus"].all() and report_result.summary["focus"] is None


def test_reused_column_chip_and_count(report_run, report_result):
    r = report_run
    picked = list(report_result.comparisons["result_id"].head(3))
    result = build_result(r.store, r.registry, r.settings, r.doc_ids, run_id=r.run_id, reused_ids=picked)
    assert result.comparisons["reused"].dtype == bool
    assert set(result.comparisons.loc[result.comparisons["reused"], "result_id"]) == set(picked)
    assert result.summary["reused"] == 3 and report_result.summary["reused"] == 0
    html_text = result.to_html()
    assert html_text.count('<span class="ic" aria-hidden="true">i</span>reused</span>') == 3
    assert "3 of " in html_text and "reused from earlier runs" in html_text


def test_changes_since_lists_new_changed_and_removed_rows(report_run, report_result):
    r = report_run
    rows = report_result.comparisons
    first, second = rows.iloc[0], rows.iloc[1]
    record = {str(row["result_id"]): {"status": row["status"], "report_value": row["report_value"],
                                      "platform_value": row["platform_value"], "kpi_id": row["kpi_id"],
                                      "doc_id": row["doc_id"], "report_file": row["report_file"]}
              for _, row in rows.iloc[2:].iterrows()}
    record[str(second["result_id"])] = {"status": "VARIANCE_REVIEW" if second["status"] != "VARIANCE_REVIEW"
                                        else "VERIFIED", "report_value": second["report_value"],
                                        "platform_value": second["platform_value"], "doc_id": second["doc_id"]}
    record["gone"] = {"status": "VERIFIED", "report_value": 1.0, "platform_value": 1.0, "kpi_id": "trx",
                      "doc_id": first["doc_id"], "report_file": first["report_file"]}
    previous = [{"run_id": "earlier", "results": record}]
    result = build_result(r.store, r.registry, r.settings, r.doc_ids, run_id=r.run_id, previous_runs=previous)
    changes = result.changes_since()
    by_change = {c: set(g["result_id"]) for c, g in changes.groupby("change")}
    assert by_change == {"new": {first["result_id"]}, "changed": {second["result_id"]}, "removed": {"gone"}}
    assert set(changes["previous_run_id"]) == {"earlier"}
    assert result.changes_since("earlier").equals(changes)
    with pytest.raises(KeyError):
        result.changes_since("unknown-run")
    assert len(report_result.changes_since()) == len(rows)
