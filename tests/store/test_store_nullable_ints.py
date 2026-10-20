import pandas as pd

from kpi_evidence.contracts import KpiResult, columns, from_row


def test_pandas_na_in_nullable_columns_reads_as_none():
    row = {name: None for name, *_ in columns(KpiResult)}
    row.update(result_id="r", result_version=1, observation_id="o", doc_id="d", scope_key="{}",
               tolerance="{}", validation_status="VERIFIED", reason_code="x", reason_detail="y", explanation="{}",
               definition_choices="{}", is_current=True, created_at="2026-10-19T00:00:00",
               kpi_version=pd.NA, report_value=pd.NA, period_end=pd.NaT)
    result = from_row(KpiResult, row)
    assert result.kpi_version is None and result.report_value is None and result.period_end is None
