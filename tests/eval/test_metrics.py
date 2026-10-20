from __future__ import annotations

import math

import pandas as pd
import pytest

from kpi_evidence.contracts import DocumentStatus
from kpi_evidence.eval.metrics import (
    UNMATCHED, confusion_frame, compute_metrics, goal_list_coverage, load_labels, match_labels, metric_rows,
    results_frame,
)

A, B = "A.pptx", "B.pdf"
MAY, JUNE = "2026-05-31", "2026-06-30"


def _lab(file, page, kpi, period, value, half, status, reason="", *, scope=None, grain="month", also=()):
    return {"file": file, "page": page, "locator_hint": {"p": page, "k": kpi, "v": value}, "kpi_id": kpi,
            "period_end": period, "period_grain": grain, "scope": scope or {}, "report_value": value,
            "display_half_unit": half, "expected_status": status, "expected_reason": reason, "note": "",
            "also_acceptable": list(also)}


LABELS = {
    "dataset": "hand_built",
    "documents": [{"file": A, "final_status": "PROCESSED"}, {"file": B, "final_status": "PROCESSED"}],
    "coverage": {"brand": "KESTREL", "as_of": JUNE, "expected": {"trx": "FOUND", "nbrx": "FOUND", "exfactory_units": "NO_ACCESS"}},
    "labels": [
        _lab(A, 2, "trx", MAY, 5700.0, 50.0, "VERIFIED", "match_under_governed_default"),
        _lab(A, 2, "trx", MAY, 5736.363291, 0.0, "VERIFIED", "match_under_governed_default"),
        _lab(A, 3, "samples", MAY, 2768.0, 0.5, "VARIANCE_REVIEW"),
        _lab(A, 3, "call_attainment", MAY, 0.958, 0.0005, "INCOMPATIBLE_DEFINITION",
             "matches_alternative_definition"),
        _lab(A, 8, "exfactory_units", MAY, None, 0.0, "UNCERTAIN_EXTRACTION", "image_not_parsed"),
        _lab(A, 3, None, MAY, -0.021, 0.0005, "UNMAPPED_CAPTURED", "no_registry_match"),
        _lab(B, 1, "nrx", MAY, 1068.0, 0.5, "REPORT_CONFLICT", "conflicting_reports"),
        _lab(B, 1, "trx", JUNE, 11783.0, 0.5, "INCOMPATIBLE_DEFINITION", "matches_alternative_definition",
             grain="quarter", also=("VARIANCE_REVIEW",)),
    ],
}


@pytest.fixture()
def scored(mem_store, result_writer):
    w = result_writer
    labs = LABELS["labels"]
    r1 = w.from_label(labs[0])
    r2 = w.from_label(labs[1])
    w.from_label(labs[2], status="VERIFIED", reason="match_under_governed_default")
    w.from_label(labs[3], kpi_id="calls", reason="unit_mismatch")
    w.from_label(labs[4], kpi_id=None)
    w.from_label(labs[6], value=1069.0)
    w.from_label(labs[7], status="VARIANCE_REVIEW", reason="systematic_gap")
    w.result(A, page=7, kpi_id="exfactory_units", period_end=MAY, value=5966.0, status="MISSING_ACCESS",
             reason="permission_denied")
    w.result(A, page=4, kpi_id="nbrx", period_end="2026-04-30", value=775.0, status="VERIFIED")
    w.result(A, page=3, kpi_id="samples", period_end=MAY, value=2768.0, status="AMBIGUOUS_MAPPING", is_current=False)
    w.publish(r1, "system:auto-policy@v1")
    w.publish(r2, "reviewer@example.com")
    return compute_metrics(mem_store, LABELS)


def test_label_metrics(scored):
    m = scored
    assert m["labels"] == 8 and m["matched"] == 7 and m["matched_strict"] == 5
    assert m["extraction_accuracy"] == pytest.approx(6 / 8)
    assert m["mapping_accuracy"] == pytest.approx(5 / 8)
    assert m["status_accuracy"] == pytest.approx(5 / 8)
    assert m["status_accuracy_lenient"] == pytest.approx(6 / 8)
    assert m["reason_accuracy"] == pytest.approx(4 / 8)
    assert m["false_validations"] == 1
    assert m["false_validation_rate"] == pytest.approx(1 / 6)
    assert m["verified_recall"] == 1.0


def test_matches_explain_each_label(scored):
    by_kpi = {(x["kpi_id"], x["label_value"]): x for x in scored["matches"]}
    assert by_kpi[("trx", 5700.0)]["tier"] == "strict" and by_kpi[("trx", 5700.0)]["actual_value"] == 5700.0
    assert by_kpi[("trx", 5736.363291)]["actual_value"] == 5736.363291
    wrong_kpi = by_kpi[("call_attainment", 0.958)]
    assert wrong_kpi["tier"] == "fallback" and wrong_kpi["value_found"] and not wrong_kpi["kpi_ok"]
    assert wrong_kpi["status_ok"] and not wrong_kpi["reason_ok"]
    image = by_kpi[("exfactory_units", None)]
    assert image["tier"] == "fallback" and image["value_found"] and image["actual_status"] == "UNCERTAIN_EXTRACTION"
    assert by_kpi[(None, -0.021)]["tier"] is None and by_kpi[(None, -0.021)]["actual_status"] is None
    conflict = by_kpi[("nrx", 1068.0)]
    assert conflict["tier"] == "strict" and not conflict["value_found"] and conflict["status_ok"]


def test_workload_auto_publish_and_confusion(scored):
    wl = scored["reviewer_workload"]
    assert wl == {"current_results": 9, "review_count": 4, "review_share": pytest.approx(4 / 9), "blocked_count": 1,
                  "blocked_share": pytest.approx(1 / 9), "good_count": 4}
    assert scored["auto_published"] == 1 and scored["published"] == 2
    assert scored["auto_publish_rate"] == pytest.approx(1 / 9)
    assert scored["confusion"] == {
        "INCOMPATIBLE_DEFINITION": {"INCOMPATIBLE_DEFINITION": 1, "VARIANCE_REVIEW": 1},
        "REPORT_CONFLICT": {"REPORT_CONFLICT": 1},
        "UNCERTAIN_EXTRACTION": {"UNCERTAIN_EXTRACTION": 1},
        "UNMAPPED_CAPTURED": {UNMATCHED: 1},
        "VARIANCE_REVIEW": {"VERIFIED": 1},
        "VERIFIED": {"VERIFIED": 2},
    }
    frame = confusion_frame(scored)
    assert frame.loc["VARIANCE_REVIEW", "VERIFIED"] == 1 and frame.loc["VERIFIED", UNMATCHED] == 0
    assert scored["result_status_counts"]["VERIFIED"] == 4 and "AMBIGUOUS_MAPPING" not in scored["result_status_counts"]


def test_per_document(scored):
    a, b = scored["per_document"][A], scored["per_document"][B]
    assert (a["labels"], a["matched"], b["labels"], b["matched"]) == (6, 5, 2, 2)
    assert a["false_validations"] == 1 and a["results"] == 8 and a["current_results"] == 7
    assert a["status_accuracy"] == pytest.approx(4 / 6) and b["status_accuracy"] == pytest.approx(1 / 2)
    assert a["document_status"] == "PROCESSED" and a["document_status_ok"]


def test_goal_list_coverage_fallback(scored):
    cov = scored["goal_list_coverage"]
    assert cov["source"] == "fallback" and cov["as_of"] == JUNE and cov["goal_kpis"] == 15
    states = cov["states"]
    assert states["trx"] == "FOUND" and states["samples"] == "FOUND" and states["calls"] == "FOUND"
    assert states["nrx"] == "FOUND" and states["nbrx"] == "STALE" and states["exfactory_units"] == "NO_ACCESS"
    assert states["active_referrers"] == "NOT_FOUND" and states["brand_awareness"] == "NOT_FOUND"
    assert cov["found"] == 4 and cov["share_found"] == pytest.approx(4 / 15)
    assert cov["expected_agreement"] == pytest.approx(2 / 3)


def test_goal_list_coverage_from_frame(mem_store):
    frame = pd.DataFrame([
        {"brand": "KESTREL", "kpi_id": "trx", "period_end": "2026-04-30", "state": "NOT_FOUND"},
        {"brand": "KESTREL", "kpi_id": "trx", "period_end": "2026-05-31", "state": "FOUND"},
        {"brand": "KESTREL", "kpi_id": "nrx", "period_end": "2026-05-31", "state": "STALE"},
        {"brand": "OTHER", "kpi_id": "calls", "period_end": "2026-05-31", "state": "FOUND"},
    ])
    cov = goal_list_coverage(mem_store, load_labels(LABELS), coverage=frame)
    assert cov["source"] == "provided" and cov["states"] == {"nrx": "STALE", "trx": "FOUND"}
    assert cov["share_found"] == 0.5
    serving_like = pd.DataFrame([
        {"brand": "KESTREL", "kpi_id": "trx", "expected_period_end": "2026-05-31", "coverage_state": "FOUND"},
        {"brand": "KESTREL", "kpi_id": "trx", "expected_period_end": "2026-03-31", "coverage_state": "NOT_FOUND"},
        {"brand": "KESTREL", "kpi_id": "nbrx", "expected_period_end": "2026-05-31", "coverage_state": "NO_ACCESS"},
    ])
    assert goal_list_coverage(mem_store, coverage=serving_like)["states"] == {"nbrx": "NO_ACCESS", "trx": "FOUND"}
    with pytest.raises(ValueError):
        goal_list_coverage(mem_store, load_labels(LABELS), coverage=pd.DataFrame({"kpi_id": ["trx"]}))


def test_expected_period_end_follows_cadence_and_lag():
    from datetime import date

    from kpi_evidence.eval.metrics import expected_period_end

    assert expected_period_end(date(2026, 6, 30), "monthly", 1) == date(2026, 5, 31)
    assert expected_period_end(date(2026, 6, 30), "quarterly", 1) == date(2026, 3, 31)
    assert expected_period_end(date(2026, 8, 15), "quarterly", 1) == date(2026, 3, 31)
    assert expected_period_end(date(2026, 10, 1), "quarterly", 1) == date(2026, 6, 30)
    assert expected_period_end(date(2026, 1, 31), "monthly", 2) == date(2025, 11, 30)


def test_metric_rows_flat_and_finite(scored):
    rows = metric_rows(scored)
    assert rows["status_accuracy"] == pytest.approx(5 / 8)
    assert rows["reviewer_workload.review_count"] == 4.0
    assert rows["goal_list_coverage.share_found"] == pytest.approx(4 / 15)
    assert rows["doc.A.pptx.status_accuracy"] == pytest.approx(4 / 6)
    assert rows["results.MISSING_ACCESS"] == 1.0
    assert all(isinstance(v, float) and math.isfinite(v) for v in rows.values())
    assert not any(k.startswith(("matches", "confusion")) for k in rows)


def test_empty_store_counts_every_label_as_a_miss(mem_store):
    m = compute_metrics(mem_store, LABELS)
    assert m["matched"] == 0 and m["extraction_accuracy"] == 0 and m["status_accuracy"] == 0
    assert m["false_validation_rate"] == 0 and m["reviewer_workload"]["current_results"] == 0
    assert m["auto_publish_rate"] is None and m["confusion"]["VERIFIED"] == {UNMATCHED: 2}
    assert m["goal_list_coverage"]["share_found"] == 0


def test_perfect_results_on_generated_labels(mem_store, result_writer, synth_set):
    labels = synth_set["labels"]
    result_writer.batch(labels["labels"])
    for doc in labels["documents"]:
        result_writer.set_status(doc["file"], DocumentStatus(doc["final_status"]))
    m = compute_metrics(mem_store, labels)
    assert m["labels"] == 75 and m["matched_strict"] == 75
    for key in ("extraction_accuracy", "mapping_accuracy", "status_accuracy", "reason_accuracy", "verified_recall"):
        assert m[key] == 1.0, key
    assert m["false_validation_rate"] == 0
    assert all(row["document_status_ok"] for row in m["per_document"].values())
    assert m["goal_list_coverage"]["states"] == labels["coverage"]["expected"]
    assert m["goal_list_coverage"]["expected_agreement"] == 1.0
    assert m["goal_list_coverage"]["share_found"] == pytest.approx(13 / 15)


def test_load_labels_accepts_a_bare_list_and_results_frame_joins_pages(mem_store, result_writer):
    ls = load_labels(LABELS["labels"])
    assert len(ls.labels) == 8 and ls.files == [A, B] and all(lab["scope_key"] == "{}" for lab in ls.labels)
    result_writer.result(A, page=5, kpi_id="calls", period_end=MAY, value=1.0, status="VERIFIED")
    frame = results_frame(mem_store)
    assert frame.loc[0, "page"] == 5 and frame.loc[0, "file_name"] == A and frame.loc[0, "period_end"] == MAY
    assert match_labels(ls.labels, frame)[0].tier is None
