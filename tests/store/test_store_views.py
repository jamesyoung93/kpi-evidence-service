from __future__ import annotations

import dataclasses
import re
from datetime import date, datetime, timedelta

import pytest

from kpi_evidence.contracts import (
    STATUS_INFO,
    DecisionSource,
    Document,
    DocumentFormat,
    DocumentStatus,
    ExtractedValue,
    ExtractionMethod,
    KpiResult,
    Observation,
    Publication,
    PublicationLabel,
    ReviewAction,
    ReviewDecision,
    StatusGroup,
    ValidationStatus,
)
from kpi_evidence.settings import Settings, default_sql_dir
from kpi_evidence.store.base import split_sql

T0 = datetime(2026, 6, 10, 9, 0, 0)
VIEWS = ("published_kpis_current", "kpi_results_current", "review_open", "report_captures")


def publication(pid: str, *, kpi="trx", period_end=date(2026, 5, 31), published_at=T0, retracted=False,
                superseded_by=None, value=1.0, scope_key="{}", kpi_version=1, period_grain="month") -> Publication:
    return Publication(
        publication_id=pid, brand="KESTREL", kpi_id=kpi, kpi_version=kpi_version, scope_key=scope_key,
        period_start=period_end.replace(day=1), period_end=period_end, period_grain=period_grain,
        period_label="May 2026", value=value, unit="count", display_label=PublicationLabel.MATCHES_PLATFORM,
        validation_status=ValidationStatus.VERIFIED, review_action=ReviewAction.ACCEPT, platform_value=value,
        variance_rel=0.0, tolerance={}, definition_hash="d", sql_artifact_id=None, sql_sha=None,
        evidence_uri="/v/x.pptx", evidence_hash="doc1", evidence_locator={"slide": 2},
        extraction_method=ExtractionMethod.NATIVE_CHART, result_id="r-" + pid, decision_id="dec-" + pid,
        approved_by="a@x.com", approved_at=published_at, published_at=published_at, retracted=retracted,
        superseded_by=superseded_by)


def result(rid: str, status: ValidationStatus, *, version=1, current=True, observation_id="obs1",
           doc_id="doc1") -> KpiResult:
    return KpiResult(
        result_id=rid, result_version=version, observation_id=observation_id, doc_id=doc_id, brand="KESTREL",
        kpi_id="trx", kpi_version=1, definition_hash="d", scope_key="{}", period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31), period_grain="month", report_value=5.0, unit="count", platform_value=5.0,
        abs_diff=0.0, rel_diff=0.0, tolerance={}, match_basis=None, validation_status=status, reason_code="x",
        reason_detail="x", explanation={}, definition_choices={}, extraction_method=ExtractionMethod.NATIVE_TABLE,
        recon_run_id=None, sql_artifact_id=None, dq_status="pass", is_current=current, created_at=T0)


def decision(did: str, result_id: str, version: int) -> ReviewDecision:
    return ReviewDecision(decision_id=did, result_id=result_id, result_version=version, action=ReviewAction.ACCEPT,
                          rationale="ok", decided_by="a@x.com", decision_source=DecisionSource.HUMAN,
                          decided_at=T0, evidence_hash="e")


@pytest.fixture()
def context_rows(mem_store):
    doc = Document(doc_id="doc1", file_name="Kestrel_Review.pptx", file_path="/v/inbox/Kestrel_Review.pptx",
                   size_bytes=10, format=DocumentFormat.PPTX, status=DocumentStatus.PROCESSED, registered_at=T0,
                   updated_at=T0, report_type="monthly_brand_review")
    value = ExtractedValue(value_id="val1", doc_id="doc1", method=ExtractionMethod.NATIVE_TABLE,
                           extractor="pptx_extract", extractor_version="1", locator={"slide": 4, "row": 2},
                           slide_or_page=4, context={}, raw_text="5,910", raw_number=5910.0, number_format=None,
                           flags=[], confidence=1.0, created_at=T0)
    obs = Observation(observation_id="obs1", value_id="val1", doc_id="doc1", interpretation_version="1",
                      label="TRx", label_context="KPI table / TRx / May 2026", value=5910.0, unit="count",
                      display_half_unit=0.5, period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
                      period_grain="month", period_label="May 2026", period_source="category",
                      period_confidence=1.0, scope={}, scope_key="{}", brand="KESTREL", fingerprint="fp1",
                      stated_rule="", flags=[{"code": "X"}], confidence=1.0, created_at=T0)
    mem_store.upsert([doc])
    mem_store.upsert([value])
    mem_store.upsert([obs])
    return mem_store


def test_ensure_schema_creates_every_view(mem_store):
    names = set(mem_store.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = :s AND table_type = 'VIEW'",
        {"s": mem_store.settings.schema})["table_name"])
    assert set(VIEWS) <= names
    mem_store.ensure_schema()


def test_published_current_keeps_the_in_force_row_per_natural_key(mem_store):
    old = publication("p1", published_at=T0, superseded_by="p2", value=1.0)
    new = publication("p2", published_at=T0 + timedelta(days=1), value=2.0)
    retracted = publication("p3", kpi="nrx", retracted=True)
    other_period = publication("p4", period_end=date(2026, 4, 30))
    other_scope = publication("p5", scope_key='{"region":"West"}')
    other_version = publication("p6", kpi_version=2)
    race_a = publication("p7", kpi="calls", published_at=T0)
    race_b = publication("p8", kpi="calls", published_at=T0 + timedelta(hours=1))
    mem_store.insert_new([old, new, retracted, other_period, other_scope, other_version, race_a, race_b])
    current = mem_store.query("SELECT publication_id, value FROM kpi_evidence.published_kpis_current")
    assert set(current["publication_id"]) == {"p2", "p4", "p5", "p6", "p8"}
    assert float(current.set_index("publication_id").loc["p2", "value"]) == 2.0


def test_published_current_rows_read_back_as_publications(mem_store):
    from kpi_evidence.contracts import from_row

    pub = publication("p1")
    mem_store.insert_new([pub])
    rows = mem_store.read_df("published_kpis_current").to_dict(orient="records")
    assert [from_row(Publication, r) for r in rows] == [pub]


def test_published_current_treats_null_periods_as_one_key(mem_store):
    a = dataclasses.replace(publication("p1", superseded_by="p2"), period_start=None, period_end=None)
    b = dataclasses.replace(publication("p2", published_at=T0 + timedelta(days=1)), period_start=None, period_end=None)
    mem_store.insert_new([a, b])
    assert list(mem_store.query("SELECT publication_id FROM kpi_evidence.published_kpis_current")["publication_id"]) == ["p2"]


def test_results_current_maps_every_status_like_status_info(mem_store):
    rows = [result(f"r-{s.value}", s) for s in ValidationStatus]
    rows.append(result("r-old", ValidationStatus.VERIFIED, current=False))
    mem_store.insert_new(rows)
    df = mem_store.query("SELECT result_id, validation_status, status_group, status_label, status_sort "
                         "FROM kpi_evidence.kpi_results_current")
    assert "r-old" not in set(df["result_id"])
    assert len(df) == len(ValidationStatus)
    for rec in df.to_dict(orient="records"):
        info = STATUS_INFO[ValidationStatus(rec["validation_status"])]
        assert (rec["status_group"], rec["status_label"], int(rec["status_sort"])) == (
            info.group.value, info.label, info.sort)


def test_review_open_lists_undecided_review_and_blocked_results(context_rows):
    store = context_rows
    store.insert_new([
        result("r-good", ValidationStatus.VERIFIED),
        result("r-variance", ValidationStatus.VARIANCE_REVIEW),
        result("r-access", ValidationStatus.MISSING_ACCESS),
        result("r-decided", ValidationStatus.AMBIGUOUS_MAPPING),
        result("r-reopened", ValidationStatus.UNCERTAIN_EXTRACTION, version=2),
        result("r-stale", ValidationStatus.VARIANCE_REVIEW, current=False),
        result("r-orphan", ValidationStatus.REPORT_CONFLICT, observation_id="missing", doc_id="missing"),
    ])
    store.insert_new([decision("d1", "r-decided", 1), decision("d2", "r-reopened", 1)])
    df = store.query("SELECT * FROM kpi_evidence.review_open").set_index("result_id")
    assert set(df.index) == {"r-variance", "r-access", "r-reopened", "r-orphan"}
    row = df.loc["r-variance"]
    assert row["label"] == "TRx" and row["period_label"] == "May 2026"
    assert row["label_context"] == "KPI table / TRx / May 2026"
    assert row["file_name"] == "Kestrel_Review.pptx" and int(row["slide_or_page"]) == 4
    assert row["status_group"] == StatusGroup.REVIEW.value
    assert df.loc["r-access", "status_group"] == StatusGroup.BLOCKED.value
    assert df.loc["r-orphan"].isna()["file_name"], "a missing document never hides a result"


def test_report_captures_lists_current_captured_values_with_context(context_rows):
    store = context_rows
    store.insert_new([
        result("r-ro", ValidationStatus.REPORT_ONLY),
        result("r-un", ValidationStatus.UNMAPPED_CAPTURED),
        result("r-ver", ValidationStatus.VERIFIED),
        result("r-ro-old", ValidationStatus.REPORT_ONLY, current=False),
    ])
    df = store.query("SELECT result_id, label, raw_text, file_name FROM kpi_evidence.report_captures")
    assert set(df["result_id"]) == {"r-ro", "r-un"}
    assert set(df["raw_text"]) == {"5,910"} and set(df["file_name"]) == {"Kestrel_Review.pptx"}


def _script(settings: Settings) -> str:
    text = (default_sql_dir() / "service_views.sql").read_text(encoding="utf-8")
    text = text.replace("${schema}", settings.schema_prefix(settings.schema))
    return text.replace("${gov}", settings.schema_prefix(settings.gov_schema))


def test_views_use_only_the_common_sql_subset():
    raw = (default_sql_dir() / "service_views.sql").read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in raw.splitlines())
    assert "::" not in code and "`" not in code
    assert not re.search(r"\b(date_trunc|ILIKE|STRUCT_PACK|LIST_VALUE)\b", code, re.I)
    assert "KESTREL" not in code.upper(), "no brand in SQL"


def test_views_parse_as_databricks_sql():
    sqlglot = pytest.importorskip("sqlglot")
    settings = Settings.from_env(backend="databricks", catalog="main")
    statements = split_sql(_script(settings))
    assert len(statements) == len(VIEWS)
    for stmt, view in zip(statements, VIEWS):
        parsed = sqlglot.parse_one(stmt, read="databricks")
        assert parsed.key == "create"
        assert f"main.kpi_evidence.{view}" in stmt
        assert "${" not in stmt
