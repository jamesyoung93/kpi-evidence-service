CREATE OR REPLACE VIEW ${schema}.kpi_status_overview AS
WITH goal_versions AS (
  SELECT brand_id, kpi_id, scope_key, version, status, cadence, expected_lag_periods, owner
  FROM ${schema}.kpi_goal_list
  WHERE status <> 'DRAFT'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY brand_id, kpi_id, scope_key ORDER BY version DESC) = 1
),
goals AS (
  SELECT brand_id, kpi_id, scope_key, cadence, expected_lag_periods, owner
  FROM goal_versions
  WHERE status = 'APPROVED'
),
registry AS (
  SELECT kpi_id, kpi_version, label, unit, has_platform_spec, claim_class
  FROM ${schema}.kpi_registry
  WHERE status = 'APPROVED'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY kpi_id ORDER BY kpi_version DESC) = 1
),
latest_result AS (
  SELECT brand, kpi_id, scope_key, period_end, period_grain, validation_status, status_group, status_label,
         reason_code, report_value, platform_value, doc_id
  FROM ${schema}.kpi_results_current
  WHERE kpi_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY brand, kpi_id, scope_key
    ORDER BY period_end DESC NULLS LAST, created_at DESC, result_version DESC) = 1
),
latest_published AS (
  SELECT brand, kpi_id, scope_key, period_end, period_label, value, unit, display_label,
         validation_status, evidence_uri, approved_at
  FROM ${schema}.published_kpis_current
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY brand, kpi_id, scope_key
    ORDER BY period_end DESC NULLS LAST, published_at DESC) = 1
)
SELECT
  g.brand_id AS brand,
  g.kpi_id,
  r.label AS kpi_label,
  r.unit,
  g.scope_key,
  g.cadence,
  g.owner AS goal_owner,
  COALESCE(r.has_platform_spec, FALSE) AS has_platform_source,
  lr.kpi_id IS NOT NULL AS found_in_reports,
  lr.period_end AS latest_period_end,
  lr.period_grain AS latest_period_grain,
  lr.validation_status AS latest_status,
  COALESCE(lr.status_label, 'Not found in a current report') AS latest_status_label,
  CASE
    WHEN lr.kpi_id IS NULL THEN 'Not found'
    WHEN lr.status_group = 'GOOD' THEN 'Good'
    WHEN lr.status_group = 'REVIEW' THEN 'Needs review'
    WHEN lr.status_group = 'BLOCKED' THEN 'Blocked'
    ELSE 'Unknown status'
  END AS latest_status_group,
  lr.reason_code AS latest_reason_code,
  lr.report_value AS latest_report_value,
  lr.platform_value AS latest_platform_value,
  lr.doc_id AS latest_doc_id,
  lp.period_end AS published_period_end,
  lp.period_label AS published_period_label,
  lp.value AS published_value,
  lp.display_label AS published_label,
  lp.validation_status AS published_status,
  lp.evidence_uri AS published_evidence_uri,
  lp.approved_at AS published_approved_at
FROM goals AS g
LEFT JOIN registry AS r
  ON r.kpi_id = g.kpi_id
LEFT JOIN latest_result AS lr
  ON lr.brand = g.brand_id AND lr.kpi_id = g.kpi_id AND lr.scope_key = g.scope_key
LEFT JOIN latest_published AS lp
  ON lp.brand = g.brand_id AND lp.kpi_id = g.kpi_id AND lp.scope_key = g.scope_key;
