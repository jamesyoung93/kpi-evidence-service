from __future__ import annotations

from datetime import date

from kpi_evidence.contracts import KpiResult, MatchBasis, ValidationStatus
from kpi_evidence.report import VERDICT_INFO, kpi_verdict
from kpi_evidence.reconcile import effective_status

MAY, JUN = date(2026, 5, 31), date(2026, 6, 30)


def _result(i: int, status: str, end: date, *, scope: str = "{}", grain: str = "month", explanation=None,
            report_value: float = 100.0) -> KpiResult:
    return KpiResult(result_id=f"r{i}", result_version=1, observation_id=f"o{i}", doc_id="d", brand="B",
                     kpi_id="k", kpi_version=1, definition_hash="h", scope_key=scope, period_start=None,
                     period_end=end, period_grain=grain, report_value=report_value, unit="count",
                     platform_value=100.0, abs_diff=None, rel_diff=None, tolerance={}, match_basis=MatchBasis.EXACT,
                     validation_status=ValidationStatus(status), reason_code="r", reason_detail="detail",
                     explanation=explanation or {}, definition_choices={}, extraction_method=None,
                     recon_run_id=None, sql_artifact_id=None, dq_status=None, is_current=True, created_at=None)


def test_one_mismatch_in_a_period_makes_the_kpi_partly_validated():
    out = kpi_verdict(None, [_result(1, "VERIFIED", MAY), _result(2, "VARIANCE_REVIEW", MAY, report_value=300.0)])
    assert out["verdict"] == "partly validated"
    assert out["periods_compared"] == 1 and out["periods_validated"] == 0
    assert "month 2026-05-31 (1 of 2 values match)" in out["verdict_detail"]


def test_a_mismatch_in_one_of_two_periods():
    rows = [_result(1, "VERIFIED", MAY), _result(2, "VERIFIED", JUN), _result(3, "VARIANCE_REVIEW", JUN)]
    out = kpi_verdict(None, rows)
    assert (out["verdict"], out["periods_compared"], out["periods_validated"]) == ("partly validated", 2, 1)
    assert "not in month 2026-06-30 (1 of 2 values match)" in out["verdict_detail"]


def test_report_conflicts_count_with_their_earlier_status():
    agree = _result(1, "REPORT_CONFLICT", MAY,
                    explanation={"conflict": {"previous": {"validation_status": "VERIFIED"}}})
    differ = _result(2, "REPORT_CONFLICT", MAY,
                     explanation={"conflict": {"previous": {"validation_status": "VARIANCE_REVIEW"}}})
    out = kpi_verdict(None, [agree, differ])
    assert out["verdict"] == "partly validated" and out["periods_validated"] == 0
    assert "2 report value(s) disagree with another report." in out["verdict_detail"]
    assert kpi_verdict(None, [agree])["verdict"] == "validated"


def test_other_scopes_and_grains_are_separate_periods():
    rows = [_result(1, "VERIFIED", MAY), _result(2, "VARIANCE_REVIEW", MAY, scope='{"region": "West"}'),
            _result(3, "VERIFIED", MAY, grain="quarter")]
    out = kpi_verdict(None, rows)
    assert (out["periods_compared"], out["periods_validated"]) == (3, 2)
    assert "region=West" in out["verdict_detail"] and "values match" not in out["verdict_detail"]


def test_all_matching_values_still_validate():
    rows = [_result(1, "VERIFIED", MAY), _result(2, "VERIFIED", MAY), _result(3, "VERIFIED", JUN)]
    out = kpi_verdict(None, rows)
    assert (out["verdict"], out["periods_compared"], out["periods_validated"]) == ("validated", 2, 2)
    assert "Every report value" in VERDICT_INFO["validated"][1]


def test_validated_kpis_in_a_real_run_have_only_matching_values(report_result):
    comps = report_result.comparisons
    index = report_result._index
    validated = report_result.kpis[report_result.kpis["verdict"] == "validated"]
    assert len(validated)
    for kpi_id in validated["kpi_id"]:
        results = [index.results[r] for r in comps.loc[comps["kpi_id"] == kpi_id, "result_id"]]
        compared = [r for r in results if r.platform_value is not None]
        assert compared, kpi_id
        assert all(effective_status(r) == "VERIFIED" for r in compared), kpi_id
