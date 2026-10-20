from __future__ import annotations

import pytest

from check_standins import REPORTS, install_standins
from kpi_evidence import materialize as materialize_module
from kpi_evidence.check import ACTOR_PROPOSALS, ACTOR_USER, ReportCheck
from kpi_evidence.contracts import Document
from kpi_evidence.settings import Settings

PDF = REPORTS / "Kestrel_Executive_Summary_Q2-2026.pdf"
UPDATE = REPORTS / "kestrel-update.pptx"


@pytest.fixture(scope="module")
def demo():
    with pytest.MonkeyPatch.context() as mp:
        install_standins(mp)
        check = ReportCheck(load_demo_platform=True)
        check.add_reports(REPORTS)
        first = check.run()
        second = check.run()
        yield check, first, second


@pytest.fixture()
def standins(monkeypatch):
    install_standins(monkeypatch)


def _docs(check: ReportCheck) -> dict[str, Document]:
    return {e.file_name: check.store.get(Document, doc_id=e.doc_id) for e in check._entries.values()}


def test_demo_run_compares_and_flags(demo):
    _check, first, _second = demo
    flags = set(first.comparisons["flag"])
    assert {"GOOD", "REVIEW"} <= flags
    statuses = set(first.comparisons["validation_status"])
    assert {"VERIFIED", "INCOMPATIBLE_DEFINITION", "REPORT_CONFLICT", "MISSING_ACCESS"} <= statuses
    call = first.call
    assert call["platform_description"].startswith("local DuckDB with the synthetic demo platform")
    assert {"setup", "extract", "metadata", "platform_checks", "process"} <= set(call["timings"])
    assert call["timings"]["documents_processed"] == 2


def test_documents_are_passed_in_file_name_order(demo):
    check, first, _ = demo
    names = [check.store.get(Document, doc_id=d).file_name for d in first.call["doc_ids"]]
    assert names == sorted(names, key=str.lower)


def test_decks_without_a_readable_period_wait_and_are_listed(demo):
    check, first, _ = demo
    docs = _docs(check)
    for name in ("kestrel-update.pptx", "Kestrel_Field_Update_scan.pdf"):
        assert docs[name].status.value == "AWAITING_CONFIRMATION"
        assert docs[name].confirmed_by is None
        assert name in set(first.issues["report_file"])
        assert docs[name].doc_id not in set(first.comparisons["doc_id"])
    assert docs["Kestrel_Monthly_Brand_Review_2026-06.pptx"].status.value == "SUPERSEDED"
    state = dict(zip(check.documents["file_name"], check.documents["metadata"]))
    assert state["kestrel-update.pptx"] == "needs confirmation"
    assert state["Kestrel_Executive_Summary_Q2-2026.pdf"] == "read from the report"


def test_rerun_is_idempotent_and_reuses_saved_results(demo):
    _check, first, second = demo
    assert set(second.comparisons["result_id"]) == set(first.comparisons["result_id"])
    assert second.call["timings"]["documents_processed"] == 2
    assert second.call["reused_ids"] and second.call["reused_ids"] <= set(first.comparisons["result_id"])
    assert dict(zip(second.comparisons["result_id"], second.comparisons["validation_status"])) == dict(
        zip(first.comparisons["result_id"], first.comparisons["validation_status"]))
    assert len(second.issues) == len(first.issues)


def test_confirm_then_rerun_processes_only_that_deck(demo):
    check, _first, _second = demo
    with pytest.MonkeyPatch.context() as mp:
        install_standins(mp)
        check.confirm("kestrel-update.pptx", period="June 2026")
        third = check.run()
    doc = _docs(check)["kestrel-update.pptx"]
    assert doc.status.value == "PROCESSED" and doc.confirmed_by == ACTOR_USER
    assert third.call["timings"]["documents_processed"] == 3
    rows = third.comparisons[third.comparisons["doc_id"] == doc.doc_id]
    assert not set(rows["result_id"]) & third.call["reused_ids"]
    by_kpi = dict(zip(rows["kpi_id"], rows["validation_status"]))
    assert by_kpi["calls"] == "VERIFIED" and by_kpi["trx_share"] == "INCOMPATIBLE_DEFINITION"
    assert "kestrel-update.pptx" in check.documents.loc[check.documents["metadata"] == "confirmed by a person",
                                                        "file_name"].tolist()


def test_low_confidence_metadata_is_used_unless_strict(standins):
    settings = Settings.from_env(metadata_confidence_threshold=0.99)
    loose = ReportCheck(settings=settings, load_demo_platform=True)
    loose.add_reports(PDF)
    result = loose.run()
    doc = _docs(loose)[PDF.name]
    assert doc.status.value == "PROCESSED" and doc.confirmed_by == ACTOR_PROPOSALS
    assert len(result.comparisons) > 0
    assert loose.documents.iloc[0]["metadata"] == "proposed, not confirmed"

    strict = ReportCheck(settings=settings, load_demo_platform=True, strict_metadata=True)
    strict.add_reports(PDF)
    result = strict.run()
    doc = _docs(strict)[PDF.name]
    assert doc.status.value == "AWAITING_CONFIRMATION" and doc.confirmed_by is None
    assert result.comparisons.empty and PDF.name in set(result.issues["report_file"])


def test_add_reports_overrides_are_applied_as_a_person(standins):
    check = ReportCheck(load_demo_platform=True)
    check.add_reports(UPDATE, brand="Kestrel", period="June 2026")
    result = check.run()
    doc = _docs(check)[UPDATE.name]
    assert doc.confirmed_by == ACTOR_USER and doc.brand == "KESTREL" and str(doc.period_end) == "2026-06-30"
    assert "VERIFIED" in set(result.comparisons["validation_status"])
    confirmed_at = doc.confirmed_at
    again = check.run()
    assert _docs(check)[UPDATE.name].confirmed_at == confirmed_at
    assert again.call["reused_ids"]


def test_invalid_override_becomes_an_issue_row(standins):
    check = ReportCheck(load_demo_platform=True)
    check.add_reports(UPDATE, period="June 2026", brand="KESTREL")
    check._entries[next(iter(check._entries))].overrides["period_grain"] = "fortnight"
    result = check.run()
    rows = result.issues[result.issues["category"] == "metadata not applied"]
    assert len(rows) == 1 and rows.iloc[0]["severity"] == "error" and "fortnight" in rows.iloc[0]["message"]


def test_materialize_false_skips_sql_artifacts_and_restores_the_stage(standins):
    original = materialize_module.materialize
    off = ReportCheck(load_demo_platform=True)
    off.add_reports(PDF)
    off.run(materialize=False)
    assert off.store.query("SELECT COUNT(*) AS n FROM kpi_evidence.sql_artifacts").iloc[0, 0] == 0
    assert materialize_module.materialize is original
    on = ReportCheck(load_demo_platform=True)
    on.add_reports(PDF)
    on.run()
    assert on.store.query("SELECT COUNT(*) AS n FROM kpi_evidence.sql_artifacts").iloc[0, 0] > 0


def test_unsupported_file_is_kept_and_never_processed(standins, tmp_path):
    legacy = tmp_path / "old.ppt"
    legacy.write_bytes(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1" + b"\x00" * 600)
    check = ReportCheck(load_demo_platform=True)
    check.add_reports(legacy)
    result = check.run()
    assert check.documents.iloc[0]["status"] == "UNSUPPORTED_FORMAT"
    assert result.comparisons.empty and result.call["doc_ids"] == [next(iter(check._entries))]
