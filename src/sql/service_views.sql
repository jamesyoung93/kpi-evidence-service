CREATE OR REPLACE VIEW ${schema}.published_kpis_current AS
SELECT p.*
FROM ${schema}.published_kpis AS p
WHERE NOT p.retracted
  AND p.superseded_by IS NULL
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY p.brand, p.kpi_id, p.kpi_version, p.scope_key, p.period_grain, p.period_start, p.period_end
  ORDER BY p.published_at DESC, p.approved_at DESC, p.publication_id DESC
) = 1;

CREATE OR REPLACE VIEW ${schema}.kpi_results_current AS
SELECT
  r.*,
  CASE r.validation_status
    WHEN 'VERIFIED' THEN 'GOOD'
    WHEN 'REPORT_ONLY' THEN 'GOOD'
    WHEN 'UNMAPPED_CAPTURED' THEN 'GOOD'
    WHEN 'VARIANCE_REVIEW' THEN 'REVIEW'
    WHEN 'INCOMPATIBLE_DEFINITION' THEN 'REVIEW'
    WHEN 'UNCERTAIN_EXTRACTION' THEN 'REVIEW'
    WHEN 'AMBIGUOUS_MAPPING' THEN 'REVIEW'
    WHEN 'REPORT_CONFLICT' THEN 'REVIEW'
    WHEN 'MISSING_ACCESS' THEN 'BLOCKED'
    WHEN 'PLATFORM_DATA_ISSUE' THEN 'BLOCKED'
    WHEN 'BLOCKED' THEN 'BLOCKED'
  END AS status_group,
  CASE r.validation_status
    WHEN 'VERIFIED' THEN 'Matches platform'
    WHEN 'REPORT_ONLY' THEN 'Captured (report only)'
    WHEN 'UNMAPPED_CAPTURED' THEN 'Captured (not on the KPI list)'
    WHEN 'VARIANCE_REVIEW' THEN 'Variance to review'
    WHEN 'INCOMPATIBLE_DEFINITION' THEN 'Definition differs'
    WHEN 'UNCERTAIN_EXTRACTION' THEN 'Check the extracted value'
    WHEN 'AMBIGUOUS_MAPPING' THEN 'Confirm which KPI'
    WHEN 'REPORT_CONFLICT' THEN 'Reports disagree'
    WHEN 'MISSING_ACCESS' THEN 'No access to platform data'
    WHEN 'PLATFORM_DATA_ISSUE' THEN 'Platform data issue'
    WHEN 'BLOCKED' THEN 'Could not evaluate'
  END AS status_label,
  CASE r.validation_status
    WHEN 'VERIFIED' THEN 10
    WHEN 'REPORT_ONLY' THEN 20
    WHEN 'UNMAPPED_CAPTURED' THEN 30
    WHEN 'VARIANCE_REVIEW' THEN 40
    WHEN 'INCOMPATIBLE_DEFINITION' THEN 50
    WHEN 'UNCERTAIN_EXTRACTION' THEN 60
    WHEN 'AMBIGUOUS_MAPPING' THEN 70
    WHEN 'REPORT_CONFLICT' THEN 80
    WHEN 'MISSING_ACCESS' THEN 90
    WHEN 'PLATFORM_DATA_ISSUE' THEN 100
    WHEN 'BLOCKED' THEN 110
  END AS status_sort
FROM ${schema}.kpi_results AS r
WHERE r.is_current;

CREATE OR REPLACE VIEW ${schema}.review_open AS
SELECT
  r.*,
  o.label,
  o.label_context,
  o.period_label,
  o.fingerprint,
  o.stated_rule,
  o.flags AS observation_flags,
  v.slide_or_page,
  v.raw_text,
  v.locator,
  d.file_name,
  d.file_path,
  d.report_type
FROM ${schema}.kpi_results_current AS r
LEFT JOIN ${schema}.observations AS o ON o.observation_id = r.observation_id
LEFT JOIN ${schema}.extracted_values AS v ON v.value_id = o.value_id
LEFT JOIN ${schema}.documents AS d ON d.doc_id = r.doc_id
WHERE r.status_group IN ('REVIEW', 'BLOCKED')
  AND NOT EXISTS (
    SELECT 1
    FROM ${schema}.review_decisions AS x
    WHERE x.result_id = r.result_id
      AND x.result_version = r.result_version
  );

CREATE OR REPLACE VIEW ${schema}.report_captures AS
SELECT
  r.*,
  o.label,
  o.label_context,
  o.period_label,
  o.fingerprint,
  o.stated_rule,
  o.flags AS observation_flags,
  v.slide_or_page,
  v.raw_text,
  v.locator,
  d.file_name,
  d.file_path,
  d.report_type
FROM ${schema}.kpi_results_current AS r
LEFT JOIN ${schema}.observations AS o ON o.observation_id = r.observation_id
LEFT JOIN ${schema}.extracted_values AS v ON v.value_id = o.value_id
LEFT JOIN ${schema}.documents AS d ON d.doc_id = r.doc_id
WHERE r.validation_status IN ('REPORT_ONLY', 'UNMAPPED_CAPTURED');
