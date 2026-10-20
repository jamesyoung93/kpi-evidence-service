# Architecture and maintainer guide

This guide explains how `kpi_evidence` works, which rules its results depend on, and how to change it
without breaking them. The source code has no comments, so the reasoning lives here. Related references:
the notebook API in [NOTEBOOK.md](NOTEBOOK.md), Databricks deployment in [DEPLOY.md](DEPLOY.md), the KPI
table columns in [samples/KPI_TABLE.md](../samples/KPI_TABLE.md) and the registry YAML conventions in
[registry/README.md](../registry/README.md).

## 1. What the package does

`kpi_evidence` reads report files (PowerPoint decks with native charts, tables and embedded workbooks,
Excel workbooks, PDFs and images) and records every number with its exact location. It interprets each
number (label, value, unit, period, scope) and maps it to a KPI defined in the governed registry
(`registry/`) or in a KPI table of hints supplied by the user. For each mapped KPI it compiles
deterministic SQL under a definition fixed in advance, runs it on the platform tables for the same brand,
period and scope, and compares the two values within a declared tolerance. Every comparison gets a status,
a flag (GOOD, REVIEW, BLOCKED, INFO) and a one-sentence reason, and every SQL statement that ran is kept.
A run ends in a comparison report (pandas DataFrames, an HTML page, an Excel workbook and `.sql` files);
nothing is written to the catalog or published unless explicitly requested.

## 2. Data flow

### 2.1 Stages

`ReportCheck.run()` (`check.py`) runs the steps below in order. Steps 5 to 9 run once per document inside
`pipeline.process_stage`. Every per-document stage is wrapped in `pipeline.logged_stage`, which writes
`run_log` rows (RUNNING, then SUCCEEDED or FAILED).

| # | Step | Module and entry point | State tables written (`contracts.py`) |
|---|---|---|---|
| 0 | Setup | `ReportCheck._bootstrap`: `store.ensure_schema` (DDL generated from `contracts.ALL_TABLES` by `store/ddl.py`), the synthetic platform and governed views in demo mode, registry sync (`pipeline.sync_registry`) | `kpi_registry`, `brands`, `kpi_goal_list`, `report_types` |
| 1 | Register | `intake.register_file` (called by `add_reports`) | `documents`, `review_events` (`uploaded`) |
| 2 | Extract | `pipeline.extract_stage` -> `extract.router.extract_document`; `extract.ai_parse` when AI is on in Databricks | `extracted_values`, `document_pages`, `extraction_issues`, `documents` |
| 3 | Metadata | `intake.propose_metadata` and `intake.apply_proposals`; stated metadata through `intake.confirm_metadata` | `documents` (status, brand, report type, period, family, version, supersession), `kpi_results.is_current` of superseded documents, `review_events` (`confirmed_metadata`) |
| 4 | Platform checks | `ReportCheck.governed_views`, `ReportCheck._collect_dimension_values`, `pipeline.run_dq` -> `platform.run_dq_checks` (only when governed views exist) | `dq_results`, `run_log` |
| 5 | Interpret | `standardize.interpret` | `observations` |
| 6 | Map | `mapping.map_observations`, with the provider from `ai.get_provider` | `mapping_proposals`, `kpi_mappings` (reads `mapping_memory`) |
| 7 | Reconcile | `reconcile.evaluate_document` | `recon_runs`, `recon_trials`, `kpi_results` |
| 8 | Materialize | `materialize.materialize` | `sql_artifacts`, `platform_kpi_values`, `.sql` files under `<volume>/artifacts/sql/` |
| 9 | Conflicts | `conflicts.detect_conflicts` | `kpi_results` (REPORT_CONFLICT overlay), `review_events` (`conflict_detected`) |
| 10 | Report | `report.build_result` (reads only), then `ReportCheck._record_run` | `run_log` (stage `check`, with per-comparison facts for `changes_since`) |

A stage that raises marks the document `FAILED` with `failed_stage` and `error` (`pipeline._fail_document`);
the next `run()` retries it. `review_decisions`, `published_kpis`, `mapping_memory` and `reviewers` are
written only by the parked review flow (`review.py`), and `eval_runs` only by the evaluation. The batch
jobs (`cli.py`, job `kpi_pipeline`) and `pipeline.run_local` call the same stage functions with a different
store.

### 2.2 Identifiers and writes

Ids are content-addressed (`ids.stable_id`: sha256 over canonical JSON), so re-running a stage on the same
inputs rewrites the same rows.

| Id | Built from | Where |
|---|---|---|
| `doc_id` | sha256 of the file bytes | `intake.register_file` |
| `value_id` | doc_id, extraction method, locator | `extract._common.build_value` |
| `observation_id` | value_id, interpretation version | `standardize.interpret_value` |
| `result_id` | observation_id, kpi_id, KPI definition hash | `reconcile._Context.result` |
| `recon_run_id` | doc_id, kpi_id, definition hash, bindings hash, targets, start choices, platform snapshot, checks | `reconcile._reconcile_group` |
| `artifact_id` | kpi_id, definition hash, bindings hash, choices | `materialize.artifact_id_for` |
| `report_family` | brand, report type, period end | `intake._assign_family` |

Writes go through `Store.insert_new`, `upsert` and `replace` (`store/base.py`), which MERGE on each
dataclass `KEY`. `KpiResult.result_version` increases only when the report value, platform value, verdict
(before any conflict overlay) or definition choices change (`reconcile.same_inputs`), or when a conflict
overlay is added or removed (`conflicts._flag`, `conflicts._restore`). The KPI definition hash
(`registry.KpiDefinition.definition_hash`) covers id, version, unit, aggregation, grains, dimensions, claim
class, tolerance and the expanded `platform` block, so any change to how a KPI is computed or judged gives
new result ids.

### 2.3 Where state and platform live

| Construction | State store | Platform (where KPI SQL runs) |
|---|---|---|
| `ReportCheck(spark)` | in-memory DuckDB | `platform_access.DatabricksPlatform` over the Spark session |
| `ReportCheck(spark, state_path=...)` | DuckDB file copied to a local temporary file and written back after each successful run (`store/state_file.py`) | same |
| `ReportCheck(spark, persist=True)` | Delta service tables (`store/databricks_store.py`; run `kpi_bootstrap` first) | same |
| `ReportCheck(warehouse_id=...)` | in-memory DuckDB | SQL warehouse through the Statement Execution API |
| `ReportCheck()`, `ReportCheck.demo()` | one local DuckDB that also holds the synthetic platform (`load_demo_platform=True`) | `platform_access.StorePlatform` (the same database) |

`ReportCheck._build_settings` uses `backend="local"` for the state store unless `persist=True`, and
`platform_backend="databricks"` whenever Spark or a warehouse is given. `Settings` fields can also come
from `KPI_EVIDENCE_<FIELD>` environment variables (`Settings.from_env`). `Settings.ai_mode` defaults to
`local` (a deterministic stand-in used by the tests and `pipeline.run_local`); `ReportCheck` sets it to
`off` unless told otherwise. With `registry="auto"`, `ReportCheck` uses the bundled registry only in local
demo mode and an empty registry (bundled policy and report types, no KPIs) otherwise.

With `persist=True`, writes run as sequential idempotent statements. `BEGIN ATOMIC` blocks are used only
with `settings.extra["atomic"] = True` and only for blocks whose tables are all in
`store.ddl.TRANSACTIONAL_TABLES`; inside such a block every read must come before the first write
(`TransactionReadAfterWrite` otherwise).

### 2.4 Stage details

**Register.** The format comes from the bytes, not the extension (`intake.sniff`): OOXML content types,
`%PDF`, PNG, JPEG and TIFF signatures, and OLE containers (encrypted Office files and legacy `.ppt` and
`.xls`). Unsupported and encrypted files are registered with status `UNSUPPORTED_FORMAT` or `ENCRYPTED` and
a remedy sentence, and appear in `result.issues`. The same bytes registered twice are one document. Files
added by path are registered in place; bytes are written to `<volume>/inbox/YYYY/MM/DD/<sha12>_<name>`.

**Extract (no interpretation).** `extract/router.py` routes by format:

* PPTX and PPTM: `extract/pptx.py`, an adapter over the vendored `extract/_vendor/pptx_extract.py`. One
  value per chart data point (series x category) with the series number format; one per numeric table
  cell with its row label and detected column header; embedded workbook cells that a chart plots become a
  cache-versus-workbook check on the chart point (`CACHE_MATCHES_WORKBOOK`, `CACHE_DIFFERS_FROM_WORKBOOK`)
  instead of separate values; labeled numbers in slide text (`SLIDE_TEXT`, confidence 0.7); an
  `IMAGE_PLACEHOLDER` value for an image that covers at least 15% of the slide or sits on a slide without
  native data.
* XLSX: `extract/xlsx.py`, one value per numeric cell, with headers detected as for tables.
* PDF: `extract/pdf.py` (pdfplumber), table cells (confidence 0.9) and labeled numbers in text lines (0.7)
  with page and bounding box; a page without a text layer becomes a placeholder.
* Images: `router.extract_image_values`, one placeholder per file.

Native chart, table and workbook values have confidence 1.0; placeholders 0. Shared helpers live in
`extract/_common.py` (grid header detection, labeled numbers in text, footnotes, change-column flags) and
`extract/__init__.py` (`parse_number_parts`, which decides what counts as a number). Upstream uncertainty
reasons become `flags` with a dimension (`extraction`, `association`, `meaning`, `interpretation`).
Extractor warnings are stored as `extraction_issues`.

**Metadata.** `intake.propose_metadata` proposes brand, report type, period and publication date from
deterministic sources: file name, document properties, first page title, footers such as "Data through May
2026", registry brand aliases and report-type keywords, and the most common period among the values' own
labels. Each proposal has a value, confidence, source and alternatives. A required field
(`policy.metadata.required_fields`; `brand` and `period_end` with an empty registry) below
`Settings.metadata_confidence_threshold` (0.8) leaves the document `AWAITING_CONFIRMATION`. AI proposals
fill only missing fields and are capped at 0.6, so they never clear the threshold. With
`strict_metadata=False` (the default) `ReportCheck` processes such documents with their best proposals and
marks them "proposed, not confirmed". Documents with the same brand, report type and period end form a
family. Versions are ordered by the file-name version token (`_v2`, `rev3`, `revised`), then registration
time; a newer current version marks the older one `SUPERSEDED` and its results not current.

**Platform checks.** When the governed views exist, `platform.run_dq_checks` runs the checks declared in
`platform.DQ_CHECKS` per view: `unique_grain`, `complete_months` (no gap between the first and last
`month`), `not_null_keys` (all hard) and `fresh` (soft: the latest month is no older than the reference
view's latest month minus the view's lag). A hard failure makes every KPI whose `source_views` include the
view `PLATFORM_DATA_ISSUE` (`dq_failed`). KPI table rows usually name raw tables, which have no DQ checks
(a row that names a governed view inherits its status); their SQL still carries the `query_resolves`,
`period_has_rows` and `complete_window` checks described under Reconcile.

**Interpret.** `standardize.interpret` turns every extracted value into exactly one observation:

* Value and unit (`read_value`). Stored numbers (chart caches, workbook cells) use the number format code
  (`classify_format`: percent formats keep the stored fraction, trailing thousands separators scale,
  currency symbols set `usd`) and fall back to header cues such as "(%)", "($)" or "(000s)". Printed text
  uses `parse_display_number` (K/M/B suffixes, %, pts/pp/bps, parentheses as negatives, currency, "~").
  Units are `fraction` (percent, points, bps), `usd`, `count`, or `unknown` (EUR, GBP, multiples).
* `display_half_unit`: half of the last printed digit after scaling (5.7K gives 50, 84.4% gives 0.0005);
  0 for stored numbers, which carry full precision.
* Label (`build_label`): the metric phrase from the row label, series name, column header or adjacent
  text. Change columns ("vs PY", "% chg", "YoY") get `DERIVED_CHANGE`, plan/target/forecast series get
  `NON_ACTUAL_SERIES`, causal wording gets `CAUSAL_LANGUAGE`.
* Period (`resolve_period`, grammar in `periods.py`). Value-level sources come first (category, series
  name, object title, column header, row label, text line), then a "data through" title, the page title,
  footnotes and finally the document metadata. A more specific period contained in a broader one wins.
  Disagreeing sources add `PERIOD_CONFLICT` and halve the period confidence. Window tokens (YTD, R3M, MAT)
  widen a month to its window.
* Scope (`resolve_scope`). Dimension values (region, district, territory, specialty, payer channel) are
  recognized only from an allow-list: governed-view values, distinct values of hinted `scope_columns`
  (capped at 1,000 per column) and the `dimension_values` argument. Chart categories that are neither
  periods nor known scope values get `SCOPE_UNKNOWN` instead of being treated as the brand total. Table,
  worksheet, PDF table and AI-parsed values check every row-label cell and every ` / ` part of the column
  header (`standardize._scope_unknown_flags`): after known scope values, periods, unit cues, the label, KPI
  names, the brand and neutral words (`_NEUTRAL_WORDS`) are removed, any remaining word makes the value
  `SCOPE_UNKNOWN` (`_scope_residual`; a cell with a change, plan or prior-period cue is skipped). A header
  annotation such as "(est.)" or "(restated)" therefore holds the value back as an unknown scope unless
  interpretation recognizes it (Section 6.10). A title that breaks values down "by region" also gives
  `SCOPE_UNKNOWN` to a value without a scope.
* `stated_rule`: titles, labels, footnotes and nearby text that may state a definition ("writers only",
  "IL-17 class share").
* `fingerprint`: a hash of the layout (report type, page title, object kind and title, label, column
  header) with digits, month names and years masked, so next month's deck matches.
* Confidence: extraction confidence times a period factor (1.0 when the period confidence is at least
  0.9, 0.9 from 0.6, 0.5 below).

**Flags.** Every flag has a dimension. Only `meaning` is read as a dimension; other flags act through
specific codes or not at all:

* `meaning` (`NON_ACTUAL_SERIES`, `LABEL_MISSING`, and extractor codes listed in
  `mapping.KNOWN_MEANING_CODES`): a synonym match with such a flag is `AMBIGUOUS` with reason
  `meaning_flagged`, so the row reads "Confirm which KPI". `DERIVED_CHANGE` is handled earlier: change
  columns are never mapped. Approved mapping memory is checked first and ignores these flags.
* `extraction` and `association`: no rule reads the dimension itself. `reconcile.extraction_gate` acts
  on specific codes and fields (Section 5.1); `PERIOD_CONFLICT` acts through the period confidence. Other
  codes (`HIDDEN_SLIDE`, `SCOPE_AMBIGUOUS`, `RAW_NUMBER_MISMATCH`) change no verdict.
* `interpretation` (`APPROXIMATE_VALUE`, `UNIT_FROM_TEXT_CUE`, `SCALE_FROM_HEADER`, `CAUSAL_LANGUAGE`):
  recorded as evidence; no verdict depends on them, so "~5.7K" can be VERIFIED. Causal KPIs come from
  the KPI's `claim_class`, not from `CAUSAL_LANGUAGE`.

**Map.** `mapping.decide_deterministic` tries, in order: approved mapping memory (fingerprint and masked
label); then registry synonyms, KPI labels and KPI id words as whole phrases (plural-folded; the longest
match wins; a tie between KPIs is ambiguous). Change columns are not mapped. A unique match with a
compatible unit and no `meaning` flag is `ACCEPTED`; anything else is `AMBIGUOUS` or `NO_MATCH`. Only then,
when AI is on, the model is asked about observations still `NO_MATCH` or `AMBIGUOUS`, with a
unit-compatible candidate list. A confident model answer makes the mapping `AMBIGUOUS` with method
`MODEL`, never `ACCEPTED` (`mapping.apply_model_result`). Every candidate is logged in
`mapping_proposals`. Only approved registry KPIs and KPI table hints are candidates (`Registry.approved`,
`hints.CheckRegistry`).

**Reconcile.** `reconcile.evaluate_document`:

1. `_pre_reconcile` settles what needs no platform SQL: unmapped values, ambiguous mappings, uncertain
   extraction, report-only and causal KPIs, unit mismatches.
2. The rest is grouped by KPI, brand, scope, grain and stated definition (`_group_key`). Each group is one
   reconloop run with one target per observation.
3. `compile.compile_kpi` renders the bindings and builds the reconloop `MetricSpec`. `trial_spec` adds a
   `query_resolves` check (the value SQL over no rows, so a missing table or column fails first), the
   declared checks (`period_has_rows`) and `complete_window` checks (every month of a quarter, rolling or
   YTD window is present). A check passes when it returns 0.
4. Phase A computes every target under the start choices: the governed defaults plus any permitted variant
   the report states. Errors are classified (`platform.classify_error`); a value about 100, 1,000 or
   1,000,000 times off is a scale mismatch; a value within tolerance is `VERIFIED`.
5. Targets outside tolerance go to Phase B: the reconloop judge (`StatedRuleJudge`, or `LLMJudge` with a
   stated-rule fallback when AI is on) searches the declared options (at most 3 changes,
   `Settings.phase_b_max_iter` iterations), and the grid (`precompute_fit`) evaluates up to
   `Settings.grid_max_candidates` (16) declared combinations as evidence. A judge move that reproduces the
   report gives `INCOMPATIBLE_DEFINITION` (`matches_alternative_definition`); otherwise the result is
   `VARIANCE_REVIEW` with the residual pattern and every declared combination that would have matched.
6. When the primary definition cannot run and the KPI declares alternatives, the error grid tries them. A
   match gives `INCOMPATIBLE_DEFINITION` (matches alternative) with the primary error kept in the
   explanation.
7. Every `MetricSpec.compute` call in every phase, value or error with its SQL, is recorded through
   `RecordingSpec` into `recon_trials`; the run record goes to `recon_runs`.

**Materialize.** For every result with a platform value, `materialize.materialize` writes one
`sql_artifacts` row per KPI, definition, bindings and choices: the value SQL with `{as_of}` left as a
placeholder, ratio components, checks, a Databricks `CREATE OR REPLACE FUNCTION` text and a sha. It fills
`platform_kpi_values` for every complete period the source covers plus the reconciled periods, through the
same `MetricSpec.compute` path; per-period errors are stored, not raised.

**Conflicts.** `conflicts.detect_conflicts` compares current results from different report families for
the same brand, KPI, scope, grain and period end. Values that differ by more than both the printed
roundings and the KPI tolerance become `REPORT_CONFLICT`. The verdict they replaced is kept in
`explanation["conflict"]["previous"]` and restored when the conflict goes away. Results with a unit or
scale mismatch, uncertain extraction, ambiguous mapping or no mapping are not compared. A newer version in
the same family is supersession, not a conflict.

### 2.5 Parked review and publication flow

`review.py` holds the decision and publication logic for the parked review app. `ReportCheck` never calls
it; it is kept and tested so publication can be switched on later (`docs/DEPLOY.md` section 11).

* `review.decide` records ACCEPT, REJECT or CORRECT in the append-only `review_decisions` table, with a
  `review_events` row. A rationale is required except for accepting a VERIFIED result. A result that
  changed or is no longer current since the reviewer opened it raises `StaleResult`.
* ACCEPT publishes (`review.publish`) with a display label: VERIFIED is "Matches platform",
  VARIANCE_REVIEW and INCOMPATIBLE_DEFINITION are "Accepted variance", REPORT_ONLY is "Report-only". An
  unmapped number needs CORRECT with a KPI first.
* CORRECT writes a `REVIEWER_CORRECTION` value and observation and a `REVIEWER` mapping (plus mapping
  memory when the KPI changed), re-runs the verdict rules for that observation only
  (`reconcile.recheck_observation`) and retracts publications of the old result. A corrected value is
  VERIFIED only if the platform agrees.
* REJECT never publishes and retracts any current publication of the result.
* Earlier decisions on the same layout and KPI are shown for information only (`review.prior_decisions`),
  marked when the file, the definition or the platform snapshot changed.
* `review.auto_publish` applies `registry/policy.yaml`: VERIFIED results (switch `verified`) or REPORT_ONLY
  results whose layout a person accepted before (switch `report_only_known_layout`), and only when current,
  undecided, under the governed definition, from structured extraction, mapped by an allowed method, DQ
  passed (VERIFIED), not on a hidden slide and not superseded. Both switches are off.
* Publications are keyed by brand, KPI, KPI version, scope, grain and period (`review.natural_key`); a new
  value for a key sets `superseded_by` on the previous row, and `review.retract` marks a row retracted.

## 3. Module map

Package `src/kpi_evidence/`:

| Module | Responsibility and public entry points |
|---|---|
| `__init__.py` | Lazy exports `ReportCheck`, `CheckResult`, `load_kpi_table`, `__version__` |
| `check.py` | Notebook API: `ReportCheck` (`demo`, `add_reports`, `set_kpis`, `confirm`, `documents`, `run`, `focus_kpis`, `history`, `save_state`, `close`, `default_brand`, `governed_views`, `platform_description`); helpers `empty_registry`, `with_brands`, `resolve_kpi`, `period_fields` |
| `report.py` | `CheckResult` (`show`, `to_html`, `save_html`, `save_excel`, `save_sql`, `frames`, `to_spark`, `filter`, `evidence`, `changes_since`), `build_result`, column tuples, `FLAG_BY_STATUS`, `kpi_verdict`, frame builders |
| `report_html.py` | `render` (self-contained HTML), `chip`, `status_chip`, `verdict_chip`, inline SVG charts |
| `report_excel.py` | `write` (workbook written locally, then copied in one sequential write) |
| `hints.py` | KPI table: `load_kpi_table`, `parse_hints`, `merge_registry`, `save_kpi_table`, `sql_fragment_problems`, `scope_values_sql`, `lint_for_hints`, `HintSet`, `HintRow`, `CheckRegistry` |
| `registry.py` | `load_registry`, `lint`, `parse_kpi`, `expand_platform` (short form), `normalize_label`, `Registry`, `KpiDefinition`; vocabularies `UNITS`, `AGGREGATIONS`, `GRAINS`, `DIMENSIONS`, `BINDINGS` |
| `compile.py` | `compile_kpi` -> `CompiledKpi`, `CompileError`, `bindings_for`, `window_predicate`, `scope_predicate`, `detect_stated_variants`, `literal_for`, `sql_relations`, `resolve_check`, `window_completeness_checks` |
| `contracts.py` | State table dataclasses (`ALL_TABLES`), enums, `STATUS_INFO`, `ReasonCode`, `to_row`, `from_row` |
| `ids.py` | `stable_id`, `sha256_bytes`, `sha256_text`, `canonical_json`, `scope_key`, `json_safe`, `utcnow` |
| `settings.py` | `Settings` (`from_env`, schema and name helpers), `default_registry_dir`, `default_sql_dir`, `demo_dir` |
| `intake.py` | `sniff`, `register_file`, `register_inbox`, `propose_metadata`, `apply_proposals`, `confirm_metadata`, `document_grain`, `file_name_version` |
| `extract/__init__.py` | `extract_document`, `ExtractionOutput`, `parse_number_parts`, `parse_number_text`, `is_non_value` |
| `extract/router.py` | Route by format; `extract_image_values` |
| `extract/_common.py` | `build_value`, `finalize_values`, `analyze_grid`, `find_labeled_numbers`, `derived_change_flag`, `core_properties`, flag and issue helpers |
| `extract/pptx.py` | `extract_pptx_values` and the chart, table, text and image adapters over the vendored parser |
| `extract/_vendor/pptx_extract.py` | Vendored upstream parser, pinned by `VENDOR.json`; never edited |
| `extract/xlsx.py` | `extract_xlsx_values`, `worksheet_values` |
| `extract/pdf.py` | `extract_pdf_values` |
| `extract/ai_parse.py` | `run_ai_parse`, `needs_ai_parse`, `merge_ai_parse`, `parse_ai_document_output`, `build_ai_parse_sql` |
| `standardize.py` | `interpret`, `interpret_value`, `parse_display_number`, `classify_format`, `read_value`, `build_label`, `resolve_period`, `resolve_scope`, `fingerprint`, `stated_rule_text` |
| `periods.py` | `parse_period`, `find_periods`, `window_grain`, `window_bounds`, `period_bounds`, `grain_from_bounds`, `strip_periods`, `parse_year_label` |
| `mapping.py` | `map_observations`, `decide_deterministic`, `apply_model_result`, `remember_mapping`, `load_memory`, `fingerprint`, `units_compatible` |
| `ai.py` | `get_provider` (`OffProvider`, `LocalHeuristicProvider`, `AiQueryProvider`, `GatewayChatProvider`), `safe_map_labels`, `kpi_labels`, prompt and response-format builders |
| `platform.py` | `load_demo_platform`, `create_governed_views`, `run_dq_checks`, `dq_status_by_view`, `dimension_values`, `platform_snapshot`, `is_available`, `classify_error`, `governed_view_sources`, `snapshot_sources`; declarations `GOVERNED_VIEWS`, `DQ_CHECKS`, `BASE_TABLE_DQ_CHECKS`, `DIMENSION_SOURCE_VIEW`; runners `SparkSqlRunner`, `StatementRunner` |
| `platform_access.py` | `PlatformAccess`, `StorePlatform`, `DatabricksPlatform`, `spark_platform`, `warehouse_platform` |
| `pipeline.py` | `extract_stage`, `process_stage`, `run_dq`, `sync_registry`, `bootstrap`, `register_inbox`, `register_paths`, `publish_stage`, `process_document`, `run_local`, `new_run_id`, `logged_stage` |
| `reconcile.py` | `evaluate_document`, `recheck_observation`, `write_results`, `allowed_gap`, `within`, `match_basis`, `scale_factor`, `effective_status`, `kpi_priority`, `is_reused` |
| `materialize.py` | `materialize`, `artifact_id_for`, `sql_templates`, `function_sql`, `export_text`, `complete_period_ends` |
| `conflicts.py` | `detect_conflicts`, `values_conflict` |
| `cli.py` | `main` (`kpi-evidence check`, `lint`, `demo`) and job entry points `check_entry`, `bootstrap_entry`, `register_entry`, `extract_entry`, `process_entry`, `dq_entry`, `evaluate_entry`, `publish_entry` (parked) |
| `tracing.py` | `configure`, `span` (MLflow spans when `settings.tracing` is on) |
| `review.py` | Parked review flow: `review_queue`, `evidence`, `decide`, `publish`, `auto_publish`, `policy_checks`, `retract`, `prior_decisions` |
| `serving.py` | Parked consumer reads: `published_current`, `coverage`, `document_board`, `status_counts` |
| `exports.py` | Parked scorecards: `scorecard_csv`, `scorecard_xlsx`, `scorecard_pptx` |
| `genie.py` | Parked `GenieClient` (answers and drafted SQL are never trusted) |
| `store/base.py` | `Store` (`insert`, `insert_new`, `upsert`, `replace`, `read`, `read_df`, `get`, `count`, `transaction`, `runner`, `ensure_schema`, `ensure_views`, `attach_platform`), `AccessDenied`, `MetricQueryError`, `check_ident`, `sql_literal`, `scalar_value`, `split_sql` |
| `store/duckdb_store.py` | `DuckDBStore`, `DuckDBRunner` (simulates missing access to `restricted_schemas`) |
| `store/databricks_store.py` | `DatabricksStore` (Spark session or SQL warehouse) |
| `store/ddl.py` | `create_table_sql`, `check_constraints`, `TRANSACTIONAL_TABLES` |
| `store/files.py` | `FileStore` (local, FUSE or SDK), `files_for`, `safe_name` |
| `store/sqltext.py` | Parameter markers, Databricks literals, `inline_params` |
| `store/state_file.py` | `pull_state`, `push_state`, `state_location` |
| `eval/synth_reports.py` | `generate_all` (the labeled synthetic report set) |
| `eval/truth.py` | Independent pandas computation of every KPI (`kpi_value`) |
| `eval/metrics.py` | `load_labels`, `match_labels`, `compute_metrics` |
| `eval/evaluate.py` | `run_evaluation` |

Other folders:

| Path | Content |
|---|---|
| `registry/` | Governed KPI YAML (`kpis/*.yaml`), `param_groups.yaml`, `brands.yaml`, `goal_list.yaml`, `report_types.yaml`, `policy.yaml` |
| `src/sql/governed_views.sql` | The governed views: the only SQL that names platform tables |
| `src/sql/service_views.sql`, `src/sql/jobs/` | Service views over state tables; consumer views, grants and the work-list query for the jobs |
| `notebooks/check_reports.py` | Databricks notebook (source format) |
| `samples/` | Synthetic reports, `labels.json`, example KPI table and its reference |
| `libs/reconloop` | Vendored reconloop 0.5.0 (pinned in `libs/UPSTREAM.lock`) |
| `apps/insight_harness` | Pinned app; its demo CSVs are the synthetic platform data; the app itself is parked |
| `databricks.yml`, `resources/` | Bundle; parked pieces in `resources/parked/` |
| `scripts/` | `build_release.py`, `strip_comments.py`, `publish_wheels.py`; parked `build_app_bundle.py` |
| `examples/local_demo.py` | Local walk-through of the demo |

## 4. Rules that must not be broken

### 4.1 A value is VERIFIED only under a definition fixed before computing

Numerical agreement found by searching definitions is not validation. The start choices are fixed before
any SQL runs: the governed defaults (for a KPI table row, its first table and its own expression) plus a
permitted variant only when the report text states it or the document's report type declares it in
`registry/report_types.yaml` (`assumed_definitions`). Text the report states wins over the report type.

* `compile.compile_kpi` builds `start_choices`; `compile.detect_stated_variants` puts keyword hits on
  permitted options into the start choices and hits on any other option into `stated_other`, which can
  only explain a gap. `reconcile._reconcile_group` passes
  `registry.assumed_definitions(doc.report_type, kpi_id)` as `assumed_choices`; an assumed option applies
  only when it is permitted, differs from the default and the report states nothing for that parameter.
* `reconcile._reconcile_group` assigns `VERIFIED` only in the Phase A loop. Phase B, the grid and the error
  grid produce only `INCOMPATIBLE_DEFINITION` or `VARIANCE_REVIEW`.
* `registry.lint` requires the default to be permitted, permitted options to exist, and report-type assumed
  definitions to be permitted options.
* `hints.platform_block` declares only `primary` as permitted for candidate tables (`source`) and
  alternatives (`measure`).
* Tests: `tests/reconcile/test_reconcile_verdicts.py`,
  `tests/reconcile/test_reconcile_alternative_after_error.py`,
  `tests/reconcile/test_reconcile_definition_wording.py`; the evaluation's false-validation rate must
  stay 0.

### 4.2 Tolerance arithmetic

* Agreement band (`reconcile.allowed_gap`): `max(rel x |report value|, abs, half_unit)`, where `half_unit`
  counts only when `use_display_rounding` is true. `reconcile.within` adds a slack of
  `1e-12 x max(1, |report value|)`. The relative part applies to the report value.
* Match basis (`reconcile.match_basis`): `EXACT` when the gap is at most `1e-9 x max(1, |report value|)`,
  `WITHIN_ROUNDING` when at most the half unit, otherwise `WITHIN_TOLERANCE`.
* `rel_diff` in the comparison is `(report - platform) / |platform|`.
* Phase B passes reconloop one relative tolerance per group: the largest of `rel` and
  `max(abs, half_unit) / |value|` over the group (`reconcile._phase_b_tolerance`).
* Conflicts (`conflicts.values_conflict`): two report values conflict when their gap exceeds the larger of
  the sum of their half units and `max(rel x max(|a|, |b|), abs)`.
* KPI table syntax (`hints.parse_tolerance`): `0.5%` relative, `0.05pp` absolute for fractions (0.0005),
  `abs:12` absolute; parts combine with `;` and the largest of each kind wins. Defaults
  (`hints.DEFAULT_TOLERANCE`): 0.5% for counts and dollars, 0.05 points for fractions. A row that
  redefines a registry KPI without a tolerance of its own keeps the registry tolerance, unless the row
  changes the unit.
* Registry syntax: `tolerance: {rel, abs, use_display_rounding}`. Tolerance is part of the definition hash.

### 4.3 Scale check

A platform value about 100, 1,000 or 1,000,000 times the report value, or the inverse (within 2%:
`reconcile.SCALE_FACTORS`, `SCALE_BAND`), is `INCOMPATIBLE_DEFINITION` with reason `scale_mismatch`
(`reconcile.scale_factor`). The check runs in Phase A before the tolerance test, so a percent read as a
whole number or a value printed in thousands without a cue is reported as a different scale, never as a
variance. Conflict detection skips these results.

### 4.4 Nothing silently disappears

Every input ends as a row with a status or as an issue with a reason.

* Files: unsupported, legacy and encrypted files are registered with a remedy (`intake._status_for`) and
  listed by `report.document_issues`.
* Extraction: failures set the document `FAILED` (`pipeline._fail_document`); extractor warnings are stored
  as `extraction_issues` and listed by `report.extraction_issues`; duplicate locators are recorded
  (`extract._common.finalize_values`); unreadable images and image-only pages become `IMAGE_PLACEHOLDER`
  values (`router.extract_image_values`, `pptx.image_placeholder`, `pdf._placeholder`). The one deliberate
  exception is images too small to hold data: logos and small pictures on slides with native data
  (`pptx._image_values`).
* Interpretation: one observation per extracted value (`standardize.interpret`).
* Reconciliation: one result per observation in scope (`reconcile._evaluate`); exceptions in
  pre-reconciliation or in a group become `BLOCKED` rows with the error.
* KPI table: invalid rows are skipped with an error message (`hints._RowParser`), shown by
  `report.hint_issues`.
* Run: engine warnings (`check._engine_log`), AI failures (`check._TrackedProvider`), DQ and scope-value
  failures become issues; a frame that cannot be built becomes an issue row (`report._safe_frame`).
* Materialization: per-period SQL errors are stored in `platform_kpi_values.error`.
* Focus: values outside a focused run stay in `result.extraction` with reason "outside this run's focus".

### 4.5 The model proposes; code decides

* Mapping: a model answer can only make a mapping `AMBIGUOUS` (`mapping.apply_model_result`); answers
  outside the candidate set are recorded as errors (`ai.safe_map_labels`, `ai.parse_mapping_response`).
* Metadata: model proposals are capped at 0.6 confidence (`intake.AI_CONFIDENCE_CAP`,
  `ai.METADATA_MAX_CONFIDENCE`), below the 0.8 threshold.
* Phase B: the LLM judge can only propose declared options, and its matches give `INCOMPATIBLE_DEFINITION`
  (`reconcile._make_judge`).
* `ai_parse_document`: chart values read from images are capped at 0.5 confidence
  (`extract.ai_parse.CHART_CONFIDENCE_CAP`), below the 0.6 extraction gate.
* Model output is traceable: mapping proposals are logged in `mapping_proposals` with the model, prompt
  sha and raw response; judge calls in `recon_runs.record["judge_calls"]`; metadata proposals keep their
  source (`ai:<model>`). With AI off every deterministic path works unchanged.

### 4.6 Brand, scope and period are bindings, not search parameters

* `compile.bindings_for` renders `${brand}`, `${window}`, `${scope}`, `${gov}`, `${platform}` and
  `${restricted}` before reconloop sees the SQL; the judge can change only declared `{param}` placeholders.
  The bindings are part of `bindings_hash` and of the artifact and run ids.
* `registry.lint` rejects unknown bindings, undeclared placeholders and literal braces.
* Scope values come from the allow-list (Section 2.4); a scope dimension the KPI does not declare raises
  `CompileError("scope_unsupported")`. A missing brand raises `brand_unknown` (UNCERTAIN_EXTRACTION).
* String literals are escaped for the platform dialect (`compile.literal_for`).

### 4.7 Reuse only when every input is unchanged

A comparison group is reused only when `recon_run_id` is unchanged: same file bytes, KPI definition hash
(which covers KPI table rows), bindings, targets (values, periods, splits), start choices, checks and
platform snapshot (`reconcile._reconcile_group`). `reconcile._reusable_results` also requires an available
snapshot, the same reconloop version and judge, identical observations and stored results, and never reuses
`BLOCKED` or `MISSING_ACCESS` results. `platform.platform_snapshot` pins each source: governed views map to
the tables their SQL reads (`platform.governed_view_sources`), named tables are read as named; Delta tables
by `DESCRIBE HISTORY` version, DuckDB relations by a content hash. A source that cannot be pinned is
`unavailable` and its groups are always recomputed. `materialize.series_is_current` applies the same rule
to platform series. `ReportCheck(reuse=False)` or `settings.extra["reuse_results"] = False` turns reuse
off.

### 4.8 Nothing is published automatically

* `registry/policy.yaml` has `auto_publish.verified: false` and `auto_publish.report_only_known_layout:
  false`; `review.auto_publish` publishes only what the policy allows.
* `ReportCheck` never calls the review or publication code.
* `kpi-evidence-publish` is not a console script, and the publish task is parked in
  `resources/parked/optional.yml`.
* Notebook mode writes nothing to Unity Catalog tables unless `persist=True`; files are written only by
  `save_html`, `save_excel`, `save_sql` and `state_path`.

### 4.9 Conventions

* SQL is written in the DuckDB / Databricks / Spark common subset: `CAST(x AS STRING)`, `||`,
  `DATE 'YYYY-MM-DD'`, `INTERVAL n MONTH`, `last_day`, `year`, `NULLIF`, `COALESCE`, single-column
  `COUNT(DISTINCT x)`. No `::`, no backticks, no `date_trunc` and no `QUALIFY` (use a `ROW_NUMBER()`
  subquery). `tests/platform/test_sql_dialects.py` parses every statement in the three dialects and
  `tests/platform/test_spark_dialect.py` runs them on Spark 4.0.1.
* Parameters use `:name` markers with scalar values only; an IN-list gets one marker per value.
  Identifiers never come from users; dynamic identifiers go through `store.base.check_ident`.
* No environment facts in code: catalog, schemas, brand, model and warehouse come from `Settings` and the
  registry. Outside the demo loader (`platform.BASE_TABLES`), platform table names appear only in
  `src/sql/governed_views.sql`, KPI table rows and the registry YAML.
* Vendored code (`libs/reconloop`, `extract/_vendor/pptx_extract.py`) is never edited in place. Work around
  upstream behavior in the adapter; upgrade by replacing the file and updating `libs/UPSTREAM.lock` and
  `VENDOR.json` (`tests/extract/test_vendor_pin.py`).
* Keep the design small: one store interface, one AI provider interface, plain functions per stage and
  dataclasses for records; no frameworks beyond pandas, reconloop, DuckDB and databricks-sdk.
* Code has no comments and at most one-line docstrings on public names (`scripts/strip_comments.py
  --check`). User-facing strings use American spelling and name no documents
  (`tests/release/test_release_source_text.py`). Bundle YAML carries no comments (`tests/deploy`).
* Report copy: plain names with tooltips, status labels and tooltips from `contracts.STATUS_INFO`,
  numbers formatted by unit.

## 5. Status vocabulary

### 5.1 Validation statuses

`contracts.ValidationStatus`; labels and tooltips in `contracts.STATUS_INFO`; the comparison flag comes
from `report.FLAG_BY_STATUS`.

| Status | Flag | Label | Produced by (reason codes) |
|---|---|---|---|
| `VERIFIED` | GOOD | Matches platform | Phase A within tolerance (`match_under_governed_default`, `match_under_stated_permitted_variant`, `match_under_report_type_definition`) |
| `REPORT_ONLY` | GOOD | Captured (report only) | `_pre_reconcile`: KPI without a platform definition, such as a KPI table row without `table` (`no_platform_definition`); causal claim (`causal_claim`) |
| `UNMAPPED_CAPTURED` | INFO | Captured (not on the KPI list) | `_pre_reconcile`: mapping `NO_MATCH` with a clean value, including change columns (`no_registry_match`) |
| `VARIANCE_REVIEW` | REVIEW | Variance to review | Phase B found no declared explanation (`single_period_gap_unexplained`, `systematic_gap`, `zero_target`, `unresolved` or `unresolved_<pattern>`) |
| `INCOMPATIBLE_DEFINITION` | REVIEW | Definition differs | `unit_mismatch` (`_pre_reconcile`), `scale_mismatch` (Phase A), `matches_alternative_definition` (Phase B or error grid), compile errors `window_unsupported`, `scope_unsupported` |
| `UNCERTAIN_EXTRACTION` | REVIEW | Check the extracted value | `reconcile.extraction_gate` (`image_not_parsed`, `low_confidence_value` below 0.6, `period_unknown`, `scope_unknown`); compile error `brand_unknown` |
| `AMBIGUOUS_MAPPING` | REVIEW | Confirm which KPI | Mapping `AMBIGUOUS` (`multiple_candidates`, `model_proposal_only`, `meaning_flagged`) |
| `REPORT_CONFLICT` | REVIEW | Reports disagree | `conflicts.detect_conflicts` (`conflicting_reports`); the replaced verdict stays in the explanation |
| `MISSING_ACCESS` | BLOCKED | No access to platform data | Phase A error classified as a permission error (`permission_denied`) |
| `PLATFORM_DATA_ISSUE` | BLOCKED | Platform data issue | Hard DQ failure of a source view (`dq_failed`); Phase A errors `platform_object_missing`, `no_data_for_period` (failed check, incomplete window, zero denominator), `null_result` |
| `BLOCKED` | BLOCKED | Could not evaluate | Unclassified error or crash (`execution_error`); compile error `binding_missing`; mapping to a KPI not in the registry |

Evaluation order: unmapped with a clean value, ambiguous mapping, extraction gate, KPI missing from the
registry, causal claim, no platform definition, unit mismatch (all in `_pre_reconcile`); then compile
errors, DQ failure, saved-result reuse, Phase A errors, scale mismatch, tolerance, error grid, Phase B (in
`_reconcile_group`); finally the conflict overlay. `STATUS_INFO` groups `UNMAPPED_CAPTURED` as GOOD (used by
the service views, the parked review queue and the evaluation), while comparisons flag it INFO.

`reconcile.extraction_gate` returns the first reason that applies: `image_not_parsed` (flag
`IMAGE_NOT_PARSED` or an image placeholder), `low_confidence_value` (no finite value, or confidence below
`GATE_CONFIDENCE`, 0.6), `period_unknown` (no period end or an unknown grain), then `scope_unknown` (flag
`SCOPE_UNKNOWN`). A value is clean when its gate reason is none, `period_unknown` or `scope_unknown`
(`reconcile._value_clean`): an unmapped clean value is `UNMAPPED_CAPTURED`, any other unmapped value
`UNCERTAIN_EXTRACTION`. The `reason` sentence comes from `reconcile._GATE_DETAIL`.

### 5.2 Other vocabularies

* Match basis (`contracts.MatchBasis`): `EXACT`, `WITHIN_ROUNDING`, `WITHIN_TOLERANCE` (Section 4.2).
* KPI verdicts (`report.kpi_verdict`, one per KPI definition): `validated` (every compared period and scope
  has only VERIFIED comparisons), `partly validated`, `matches alternative`, `not validated`,
  `not computable` (every compared period blocked, or the definition could not be applied), `report only`,
  `not compared`. A conflict row counts with its verdict before the conflict.
* Document statuses (`contracts.DocumentStatus`): `REGISTERED` -> `EXTRACTED` -> (`AWAITING_CONFIRMATION`
  ->) `READY` -> `PROCESSED`; also `UNSUPPORTED_FORMAT`, `ENCRYPTED`, `FAILED` (with `failed_stage`),
  `SUPERSEDED`.
* Mapping (`contracts.MappingStatus`, `MappingMethod`): `ACCEPTED`, `AMBIGUOUS`, `NO_MATCH`; methods
  `SYNONYM`, `MEMORY`, `MODEL`, `REVIEWER`.
* Coverage (`contracts.CoverageState`, parked `serving.coverage`): `FOUND`, `NOT_FOUND`, `STALE`,
  `NO_ACCESS`; a view over the goal list, never a result row.

## 6. Recipes

Run tests with the project interpreter, for example `.venv/Scripts/python.exe -m pytest -q tests/reconcile`
(`.venv/bin/python` on Linux or macOS). Before handing a change over, run
`python -m pytest -q -m "not spark"` and, when the change touches SQL, the Spark tests (Section 7).

### 6.1 Add a KPI to the registry (short form)

`registry/kpis/calls.yaml` is a complete short-form file to start from.

1. Create `registry/kpis/<kpi_id>.yaml` with:

   * `kpi_id` (lower_snake_case), `version: 1`, `status: APPROVED` (DRAFT KPIs are never mapped; APPROVED
     needs `approved_by`), `label`, `definition`, `mapping_description`, `owner`, `approved_by`,
     `approved_at`;
   * `unit` (`registry.UNITS`: `count`, `usd`, `fraction`), `aggregation` (`registry.AGGREGATIONS`: `sum`,
     `ratio_of_sums`, `point`, `count`; descriptive, part of the definition hash), `grains` (any of
     `registry.GRAINS`: `month`, `quarter`, `r3m`, `r6m`, `r12m`, `ytd`), `dimensions` (any of
     `registry.DIMENSIONS`);
   * `synonyms`: whole phrases, unique across KPIs; the longest matching phrase wins, so a synonym
     "trx dollars" on a new KPI takes the label "TRx dollars" away from `trx`'s "trx";
   * `claim_class`: `descriptive` (default) or `causal`; a causal KPI cannot have a `platform` block and
     its values are always REPORT_ONLY;
   * `tolerance: {rel, abs, use_display_rounding}`;
   * `harness_link` is optional (read only by the parked review flow);
   * the platform block:

   ```yaml
   platform:
     view: gv_rx_account_monthly
     value: SUM(calls)
   ```

   Use `numerator:` and `denominator:` for a ratio of sums. Optional keys: `where`, `brand_column`,
   `date_expr`, `scope_columns`, `param_groups`, `params`, `checks`. `registry.expand_platform` generates the
   template, the `period_has_rows` check and `source_views`.
2. If the column is not in the governed view, add it to `src/sql/governed_views.sql`.
3. Lint: `kpi-evidence lint` (`cli.cmd_lint`).
4. Update the KPI count in `tests/platform/test_registry_e2e.py` (`len({c.kpi_id ...}) == 13` counts KPIs
   with a platform definition).
5. To put the KPI on a brand's goal list, add a line to `registry/goal_list.yaml` (format in
   `registry/README.md`). The evaluation expects a coverage state for every goal-list KPI: add the KPI to
   `eval.synth_reports.coverage_expectations` (`NOT_FOUND` when no sample report prints it; also adjust the
   coverage sentence in `synth_reports._readme`), run `kpi_evidence.eval.synth_reports.generate_all()` to
   rewrite `samples/reports/labels.json` and `samples/reports/README.md`, and update the goal-list counts
   (`goal_kpis == 15`, `4 / 15`, `13 / 15`) in `tests/eval/test_metrics.py` and
   `tests/eval/test_evaluate.py` (and the FOUND count in `tests/eval/test_synth_reports.py` if the KPI is
   FOUND). Otherwise `test_perfect_results_on_generated_labels` and the sample-sync tests
   (`test_committed_sample_set_is_in_sync`, `tests/eval/test_synth_readme_text.py`) fail.
6. To show the KPI working when no sample report prints it, use a one-sheet XLSX report as a fixture: the
   row label is the KPI label or a synonym, the column headers are periods (`Jun 2026`, `Q2 2026`), and a
   currency number format sets unit `usd` (a percent format sets `fraction`):

   ```python
   import io
   from openpyxl import Workbook
   import kpi_evidence as ke

   wb = Workbook()
   wb.active.append(["KPI", "May 2026", "Jun 2026", "Q2 2026"])
   wb.active.append(["TRx", 5736.363, 5783.041, 17220.318])
   buf = io.BytesIO()
   wb.save(buf)
   check = ke.ReportCheck(load_demo_platform=True)
   check.add_reports(("fixture.xlsx", buf.getvalue()), brand="KESTREL",
                     report_type="monthly_brand_review", period="June 2026")
   check.run().comparisons[["label_as_printed", "period_label", "kpi_id", "status", "platform_value"]]
   ```

   `ReportCheck(load_demo_platform=True)` is local demo mode, so it uses the bundled registry and the
   synthetic platform; add `check.set_kpis(...)` to test a KPI table row instead.
7. Run `tests/platform`, `tests/mapping`, `tests/reconcile` and the evaluation (`tests/eval`, plus the
   opt-in end-to-end run): new synonyms can change how the sample reports map. Also compare
   `ReportCheck.demo(kpi_table=False).run().comparisons` before and after the change: with the bundled
   registry it has 38 rows (README, Status); a changed `kpi_id` or `status` on a sample row means a new
   synonym remapped it.

### 6.2 Add a KPI to the registry (full form)

Use `template:` (or `ratio:` with `numerator`, `denominator`, `scale`, `zero_denominator`) when the SQL is
not one aggregate over one relation; `registry/kpis/trx.yaml`, `call_attainment.yaml` and
`exfactory_units.yaml` are examples.

* Each SQL text returns one row with one column named `value`; NULL is an error (use `COALESCE` when zero
  is meant).
* Filter with `brand = ${brand} AND ${window} AND ${scope}`. `${window}` compares `month_start` and
  `month_end` with `{as_of}`; set `date_expr` when the relation has no such columns.
* Each declared choice is a `params` entry: `hypothesis` (a key of reconloop's `CATALOG`, such as
  `source`, `measure`, `universe`, `basket`), `default`, `options`, `describe`, `keywords` (report wording
  that states the option) and `permitted` (options a report may state and still be VERIFIED). Every
  `{param}` in the value SQL must be declared, and every declared param must be used.
* Shared params go in `registry/param_groups.yaml` and are referenced with `param_groups: [name]`.
* Then follow the remaining steps of 6.1 (governed view column, lint, test counts, goal list, tests), and
  run `tests/platform/test_spark_dialect.py`.

### 6.3 Add a KPI through a KPI table row

1. Add a row to the user's CSV or XLSX: `kpi`, `aliases`, `table` (first is primary), `value_sql` or
   `numerator` + `denominator`, `date_column`, `date_format`, and optionally `filters`, `brand_column`,
   `scope_columns`, `unit`, `grain`, `tolerance`, `alternatives`, `brand`, `priority`, `owner`, `notes`
   (`samples/KPI_TABLE.md`). A row without `table` is report-only.
2. `hints.parse_hints` validates the row (`sql_fragment_problems`, identifiers, structure) and builds a
   DRAFT `KpiDefinition` (version 0, `definition_source: hint`) through `hints.raw_definition` and
   `hints.platform_block`. `hints.merge_registry` adds it to the check's registry. A row whose `kpi` names
   a registry KPI redefines how that KPI is computed for the check (its label, definition and synonyms are
   kept); an alias claimed by a row is removed from the other registry KPIs.
3. For the bundled example, edit `samples/kpi_table_example.csv`, list the KPI in the table of
   `samples/KPI_TABLE.md` (`tests/hints/test_hints_samples.py` requires every row there), and rebuild the
   workbook with `hints.save_kpi_table(hints.load_kpi_table("samples/kpi_table_example.csv"),
   "samples/kpi_table_example.xlsx")`.
4. To promote a validated hint into the registry, write it in the short form: `from: <table>`,
   `value: <value_sql>`, `date_expr` from `hints.date_expression`, `where: <filters>`, `brand_column`,
   `scope_columns`.
5. Validate a table without running a check: `kpi-evidence lint --kpis <table.csv>`.
6. To compare a row with the registry definition it replaces, run the same reports twice:
   `ReportCheck.demo()` uses the example table's definition (`definition_source` `hint`) and
   `ReportCheck.demo(kpi_table=False)` the registry's. For values no sample report prints, use the XLSX
   fixture of 6.1 step 6 and `check.set_kpis(...)`.
7. Tests: `tests/hints`, `tests/check/test_check_integration.py`, `tests/check/test_check_notebook_mode.py`
   (rows in `tests/check/check_standins.py`).

### 6.4 Port the governed views to real platform tables

`src/sql/governed_views.sql` is the seam between the registry and the platform. The demo
(`ReportCheck.demo()`, `kpi_bootstrap` with `load_demo=true`, the `demo_store` fixture) reads the same
file and the same `platform.GOVERNED_VIEWS`, `DQ_CHECKS` and `BASE_TABLE_DQ_CHECKS`, and no setting points
at another SQL file. A port therefore replaces the bundled demo views: do it in a deployment branch.

1. Before porting a view, list the KPIs that read it:

   ```bash
   python -c "from kpi_evidence.registry import load_registry; r = load_registry(); [print(k, *d.platform.source_views) for k, d in sorted(r.kpis.items()) if d.platform]"
   ```

   In the bundled registry `gv_rx_account_monthly` feeds `trx`, `nrx`, `nbrx`, `trx_share` and the CRM
   KPIs (`calls`, `call_plan`, `samples`, `new_writers`, `call_attainment` through `is_writer`,
   `speaker_attendance`); `gv_rx_panel_monthly` is the `panel` option of `trx`, `nrx`, `nbrx` and
   `trx_share`; `gv_referrals_monthly` feeds `referrals_in` and `active_referrers`. A KPI whose columns
   the real table lacks becomes "not computable".
2. In each view, replace `${platform}.<table>` with the real fully qualified table and keep the column
   contract:
   * `brand` holds the registry `brand_id` (`registry/brands.yaml`, for example `KESTREL`), because
     `${brand}` renders that id as a literal and the comparison is case-sensitive. Replace the literal
     `'KESTREL' AS brand` with the real column and map other spellings in the view (`CASE` or a lookup
     join).
   * `month` (text `YYYY-MM`, which the DQ checks parse), `month_start` and `month_end`, typed DATE. The
     month window is `month_end = DATE '{as_of}'` (`compile.window_predicate`), so `month_end` must be the
     month's last day. From a DATE column `d`: `substr(CAST(d AS STRING), 1, 7) AS month`,
     `CAST(substr(CAST(d AS STRING), 1, 7) || '-01' AS DATE) AS month_start`, `last_day(d) AS month_end`.
   * All five scope columns (`region`, `district`, `territory`, `specialty`, `payer_channel`).
     `platform.dimension_values` selects all five from `platform.DIMENSION_SOURCE_VIEW`; if one is
     missing, no scope values come from the views and the run lists an issue. Use
     `CAST(NULL AS STRING) AS <dimension>` for a missing one.
   * Measures under the names the registry reads (for example `trx AS trx_units`, or
     `market_trx AS advanced_therapy_trx` after confirming it is the governed basket). Leave out measures
     the table does not have; KPIs that need them stop with "Platform data issue" naming the column.
   * Keep the latest-load `ROW_NUMBER()` pattern for restated feeds.
3. Remove views without a real source from `governed_views.sql`, `platform.GOVERNED_VIEWS`,
   `platform.DQ_CHECKS` and `platform.BASE_TABLE_DQ_CHECKS` (its demo entry reads the base table
   `rx_panel_monthly`), and keep the `DQ_CHECKS` grain columns (including `brand`) and freshness lags in
   line with the views that stay. A view still declared in `DQ_CHECKS` but not created fails its hard
   checks, which blocks every KPI listing it in `source_views`, even when the KPI's default definition does
   not read it. Then drop the removed view from the registry: from the param options that name it
   (`rx_source` in `registry/param_groups.yaml`, the `source` params of `trx.yaml` and `trx_share.yaml`)
   and from explicit `source_views`. Until then, KPIs that read it stop with "Platform data issue" and
   `tests/platform/test_dq.py::test_every_registry_source_view_is_governed_or_direct` fails.
4. KPIs that read `${restricted}` or `${platform}` directly (`exfactory_units`) bypass the views; port their
   `platform` block too.
5. Snapshots need no change as long as every view in a KPI's `source_views` is still defined in the
   script: `platform.governed_view_sources` reads the FROM and JOIN clauses of the rendered script, so a
   ported view is pinned by the Delta versions of the tables it reads. A removed view
   (`unavailable: unknown_object`) or a view over non-Delta data cannot be pinned, so its KPIs are always
   recomputed; for a removed view, until the registry drops it (step 3).
6. Check the port locally. DuckDB reserves the database name `main`, so `main.commercial.rx_monthly` cannot
   be created locally, and `ReportCheck()` without the demo never creates governed views. Create the
   tables under two-part names, point `platform.default_sql_dir` at a copy of the script without the
   catalog, and create the views yourself (every table the script reads must exist):

   ```python
   from pathlib import Path
   from kpi_evidence import platform
   from kpi_evidence.check import ReportCheck

   folder = Path("port_check_sql")
   folder.mkdir(exist_ok=True)
   text = Path("src/sql/governed_views.sql").read_text(encoding="utf-8")
   (folder / "governed_views.sql").write_text(text.replace("main.commercial.", "commercial."), encoding="utf-8")
   platform.default_sql_dir = lambda: folder
   check = ReportCheck(registry="bundled", brand="KESTREL")
   check.store.execute("CREATE SCHEMA IF NOT EXISTS commercial")
   check.store.execute("CREATE TABLE commercial.rx_monthly AS SELECT * FROM read_csv_auto('rx_monthly_sample.csv')")
   platform.create_governed_views(check.store, check.settings)
   check.add_reports("reports/")
   result = check.run()
   ```

   `tests/check/test_check_ported_view_reuse.py` uses the same pattern with `monkeypatch`.
7. Deploy with `kpi_bootstrap` and `load_demo=false` (the dev target defaults to `true`); it creates the
   views in `<catalog>.kpi_gov`. The ported views name the real tables, so `platform_schema` is not used by
   them. Run with `ReportCheck(spark, registry="bundled", catalog=..., gov_schema=...)` or a registry path.
8. Tests: `tests/platform/test_sql_dialects.py::test_sql_parses` and `tests/deploy/test_deploy_sql.py`
   still apply. Tests that encode the demo views no longer do and must be adapted or skipped in the
   deployment branch: everything that uses the `demo_store` fixture, `ReportCheck.demo()` or
   `pipeline.run_local` on the samples (most of `tests/check`, `tests/reconcile`, `tests/eval` and
   `tests/cli`), `test_platform_view_lineage.py`, `test_platform_multibrand_views.py`, `test_dq.py`,
   `test_sql_dialects.py::test_every_statement_was_collected` (it counts 3 governed views),
   `test_spark_dialect.py` and `tests/check/test_check_ported_view_reuse.py`.

### 6.5 Change a tolerance

* Registry KPI: edit `tolerance: {rel, abs, use_display_rounding}` in `registry/kpis/<kpi_id>.yaml`.
* KPI table row: the `tolerance` column. Defaults: `hints.DEFAULT_TOLERANCE`.
* The arithmetic itself lives in `reconcile.allowed_gap`, `within`, `match_basis` and `tolerance_text`;
  change it only together with Section 4.2.

The tolerance is part of the definition hash, so results get new ids and nothing is reused. Tests:
`tests/reconcile/test_reconcile_verdicts.py`, `tests/reconcile/test_conflicts.py`,
`tests/hints/test_hints_parse.py`, the evaluation.

### 6.6 Add a synonym or handle a new label wording

* A new name for a KPI: add it to `synonyms` in the KPI YAML (whole phrase; the same phrase on two KPIs
  fails `registry.lint`) or to the KPI table `aliases`.
* Wording that states a definition ("writers only", "projected"): add it to the option's `keywords`; it
  reaches `compile.detect_stated_variants` through `standardize.stated_rule_text`.
* A label the extractors do not pick up: text lines in `extract._common.find_labeled_numbers`, table
  headers and row labels in `extract._common.analyze_grid`, slide text in `extract/pptx.py`
  (`text_values`).
* A label that is picked up but read wrongly: `standardize.build_label` (generic words, change cues
  `_CHANGE_CUE`, plan and target words `_NON_ACTUAL`) and `mapping.label_is_uninformative` (labels made only
  of periods, scope values or the brand fall back to the label context). A word in `_NON_ACTUAL` gives the
  `meaning` flag `NON_ACTUAL_SERIES`, so the value reads "Confirm which KPI"; a marker that should hold
  back a value of a known KPI (an estimate) belongs in 6.10 instead.
* Tests: `tests/mapping/test_mapping.py`, `tests/interpret/test_interpret_text_labels.py`,
  `test_interpret_row_labels.py`, `tests/extract/test_extract_text_tokens.py`,
  `tests/reconcile/test_reconcile_ambiguous_wording.py`, `test_reconcile_definition_wording.py`, the
  evaluation.

### 6.7 Support a new report layout or number format

Extraction finds numbers and their surroundings; interpretation decides what they mean. Keep the two apart.

* Extraction (`src/kpi_evidence/extract/`): the route in `router._route`, one extractor per format
  returning `ExtractionOutput`. Each value needs a stable `locator` (it defines `value_id`) and the context
  keys interpretation reads: `page_title`, `object_title`, `series_name`, `category`, `row_label`,
  `row_labels`, `col_header`, `axis_title`, `text_line`, `footnotes`, `number_source`, `series_count`,
  `hidden`. PPTX quirks are fixed in `extract/pptx.py`, never in the vendored parser.
* Interpretation: `standardize.read_value`, `parse_display_number`, `classify_format`, `build_label`,
  `resolve_period`, `resolve_scope`. Interpretation records what a value is (flags); reconciliation decides
  what that means for the verdict. A value that is readable but must not be verified (an estimate, a
  provisional figure) gets a flag in `standardize.interpret_value` and a reason in
  `reconcile.extraction_gate` (6.10); do not use mapping to hold it back.
* A new printed number format must be accepted by both parsers, which must agree:
  `extract.parse_number_parts` (`_CELL_NUMBER`, `SCALE_SUFFIXES`) decides that a cell is a number, and
  `standardize.parse_display_number` (`_NUM_TEXT`, `_SCALE_WORDS`) reads value, unit and half unit.
* A new file type also needs `contracts.DocumentFormat`, `intake.sniff` and `SUPPORTED_FORMATS`, and
  `check.REPORT_EXTENSIONS`. A new extraction method needs `contracts.ExtractionMethod` (and
  `STRUCTURED_METHODS` if structured), `report.SOURCE_BY_METHOD` and `standardize._KIND_BY_METHOD`.
* Tests: `tests/extract`, `tests/interpret`, `tests/intake`, the evaluation.

### 6.8 Add a period token

* Dated tokens (months, quarters, halves, fiscal years): a pattern in `periods._ANCHORS` and a branch in
  `periods._build`. Window tokens (YTD, R3M, MAT): `periods._WINDOW` and `periods._window_grain_of`.
  "Data through" phrases: `periods._THROUGH`. Bare tokens without a year are resolved against the reference
  date in `periods._bare`. `strip_periods` reuses the same patterns to clean labels.
* A new grain must be added everywhere grains are listed: `registry.GRAINS`, `periods.GRAIN_MONTHS`,
  `compile._ROLLING` and `compile.window_predicate`, `materialize._WINDOW_MONTHS` and
  `complete_period_ends`, `hints._GRAIN_SPELLINGS`, the grain list in `intake._normalize_confirmed`, and
  `eval.truth._ROLLING`. A grain missing from `registry.GRAINS` ends as `window_unsupported`.
* Tests: `tests/interpret/test_periods.py`, `test_periods_bare_windows.py`, `test_periods_year_windows.py`,
  `test_interpret_period_sources.py`, `tests/extract/test_extract_window_headers.py`; for a new grain also
  `tests/platform`, `tests/reconcile/test_reconcile_window_completeness.py` and the Spark tests.

### 6.9 Add a validation status end to end

1. `contracts.py`: a `ValidationStatus` member, its `STATUS_INFO` entry (group, label, tooltip, sort) and
   any new `ReasonCode` constants.
2. `reconcile.py`: the rule that produces it, at the right place in the evaluation order (Section 5.1);
   decide whether it may be reused (`_NO_REUSE_STATUSES`).
3. `report.py`: the flag in `FLAG_BY_STATUS` (unknown statuses flag as BLOCKED); `_COMPARED` if it counts
   as a compared period for KPI verdicts; `summary_dict` tiles if it needs its own tile.
4. HTML: `report_html.status_chip` takes the label and tooltip from `STATUS_INFO` and the color from the
   flag, so a status in an existing flag group needs nothing more. A new flag group needs `_GROUP_CLASS`,
   `_GROUP_ICON` and a `.chip` CSS class; a new tile needs `_TILES_TOOLTIP`, `_TILES_CAPTION`,
   `_TILES_GROUP` and `report.TILE_NAMES`.
5. Other consumers: `conflicts._NOT_COMPARABLE_STATUS`, the CASE lists in `src/sql/service_views.sql`, the
   parked `review.ACCEPTABLE`.
6. Stored values: CHECK constraints come from the enum (`store.ddl.check_constraints`). Delta tables get
   them again on `ensure_schema` (run `kpi_bootstrap`); a DuckDB state file created earlier keeps its old
   constraint, so start a new `state_path` file.
7. Documentation: the flag tables in `README.md`, Section 5 of this guide, `docs/NOTEBOOK.md`.
8. Tests: `tests/reconcile`, `tests/report` (`test_report_frames.py`, `test_report_outputs.py`),
   `tests/store/test_store_views.py`, `tests/review`, `tests/eval/test_metrics.py`.

### 6.10 Add a reason under an existing status

The common case is a new reason to hold back a readable value whose KPI is clear but which must not be
verified (an estimate, a provisional figure). Such a value gets a flag and a gate reason, which give
`UNCERTAIN_EXTRACTION` ("Check the extracted value"); a `meaning` flag would give "Confirm which KPI"
instead (Section 2.4, Flags).

1. `contracts.ReasonCode`: the new constant.
2. The flag: added in `standardize.interpret_value` (or a function it calls, such as `build_label`), with
   dimension `extraction` or `association`. If the cue is a header or row-label annotation, it already
   reads as an unknown scope today (`standardize._scope_residual`); flag it before removing it from scope
   detection, or the value will be compared.
3. The rule in `reconcile.extraction_gate`, at the right place in its order: image, missing or
   low-confidence value, unknown period, unknown scope (Section 5.1).
4. The reason sentence in `reconcile._GATE_DETAIL`; it is what the `reason` column shows, and every gate
   reason needs one.
5. If the value itself is readable, add the reason to `reconcile._value_clean`, so an unmapped value stays
   `UNMAPPED_CAPTURED` ("Captured (not on the KPI list)").
6. Documentation: the reason list in Section 5.1 and, if the meaning changes, the flag table in
   `README.md`.
7. Nothing else switches on gate reasons: conflicts, flags and verdicts use statuses (the only reason codes
   read elsewhere are `unit_mismatch`, `scale_mismatch`, the compile errors and `matches_alternative`). The
   evaluation compares reason codes with the labels, so if a sample value changes status or reason, change
   its label in `eval/synth_reports.py` and run `generate_all()`.
8. Tests: `tests/interpret` (`test_interpret_scope_unknown.py`), `tests/reconcile`, the evaluation.

### 6.11 Switch AI on

* Notebook: `ReportCheck(spark, ai_mode="ai_query")`. The model is `Settings.ai_model` (environment
  variable `KPI_EVIDENCE_AI_MODEL`, default `system.ai.claude-sonnet-4-6`) and must be `system.ai.<name>`
  or `catalog.schema.name` (`ai.validate_model_name`). `ai_query` runs only where the platform dialect is
  Databricks.
* External providers: `ai_mode="gateway"` with `Settings.ai_gateway_base_url` (https; a Databricks token
  for Databricks hosts, `OPENAI_API_KEY` otherwise).
* `ai_mode="local"` is a deterministic heuristic stand-in for tests and the demo.
* Effects: model mapping proposals for unmatched and ambiguous labels, metadata proposals for missing
  fields, `ai_parse_document` for image placeholders of files read from `/Volumes/...`, and the LLM judge in
  Phase B. All are bounded by Section 4.5. Failures appear in `result.issues` (category `AI`) and in
  `result.summary["ai"]`, and the run continues as with AI off. The judge name is part of the reuse check,
  so switching AI on recomputes groups.
* Workspace prerequisites: `docs/DEPLOY.md` section 10.
* Tests: `tests/mapping/test_ai.py`, `tests/mapping/test_ai_token_hosts.py`,
  `tests/extract/test_extract_ai_parse.py`, `tests/reconcile/test_reconcile_verdicts.py`,
  `tests/check/test_check_engine_status.py`.

### 6.12 Run in notebook mode or as the local demo

* Notebook: `ReportCheck(spark)` (Section 2.3). Pass `registry="bundled"` or a path to use the registry
  with governed views; `persist=True` needs `kpi_bootstrap` first. The notebook template is
  `notebooks/check_reports.py`; its widgets must match `resources/jobs.yml` (`tests/deploy`).
* Local demo: `ReportCheck.demo().run()`, `kpi-evidence demo --out <folder>`,
  `kpi-evidence check --demo` or `python examples/local_demo.py`. The demo loads the synthetic platform
  (`platform.load_demo_platform`) and governed views into one DuckDB, uses the bundled registry, the sample
  reports and `samples/kpi_table_example.csv`, whose rows replace the registry definitions of the same KPI
  ids; `ReportCheck.demo(kpi_table=False)` uses the registry alone. `store.duckdb_store.DuckDBRunner`
  denies SQL that touches `settings.restricted_schemas`, which produces `MISSING_ACCESS` for ex-factory
  units.
* Tests: `tests/check/test_check_notebook_mode.py` (fake Spark session in `tests/check/check_standins.py`),
  `tests/check/test_check_run.py`, `tests/cli/test_cli_local.py`.

### 6.13 Use focus and saved results

* `run(focus=[...])` or `run(max_priority=n)`: `ReportCheck.focus_kpis` resolves names like the KPI table
  does. `reconcile.in_focus` keeps observations whose mapped KPI or any candidate KPI is in the focus;
  everything is still extracted, interpreted and mapped. `reconcile.write_results` retires only results in
  scope, so other KPIs keep their current results. Entry points: `pipeline.process_stage(..., focus_kpis=)`
  and `reconcile.evaluate_document(..., focus_kpis=)`.
* `state_path=`: `history()` and `result.changes_since()` read `run_log` rows of stage `check`
  (`check.run_record`). A failed run, or an exception inside a `with` block, never writes the state back.
* Reuse rules: Section 4.7. Reused rows have `reused = True` and no platform SQL ran for them.
* Tests: `tests/check/test_check_focus_state.py`, `test_check_rerun_recheck.py`,
  `tests/reconcile/test_reconcile_focus_reuse.py`, `tests/report/test_report_changes_focus.py`.

### 6.14 Cut a release

1. Set the version in `pyproject.toml` and `src/kpi_evidence/__init__.py`.
2. Run `python -m pytest -q -m "not spark"`.
3. Run `python scripts/build_release.py --verify`. It copies the wheel inputs to a temporary tree, strips
   comments there with `scripts/strip_comments.py` (proving each file unchanged otherwise), builds both
   wheels, downloads a Linux wheelhouse for Python 3.11 and 3.12, writes `release/kpi_evidence_<version>/`
   and its zip, and with `--verify` runs the demo from the installed wheels.
4. Hand over the zip. Options and exit codes are in `RELEASE.md`; the bundle path is in `docs/DEPLOY.md`.
5. Tests: `tests/release`.

## 7. Testing

**Layout.** One folder per area under `tests/`: `check` (`ReportCheck` end to end), `cli`, `deploy`
(bundle YAML, notebook, SQL files, `docs/DEPLOY.md` content), `eval`, `extract`, `hints`, `intake`,
`interpret` (values, labels, periods, scope), `mapping` (synonyms, AI providers, Genie client), `platform`
(registry cases on DuckDB, SQL dialects, Spark, DQ, snapshots), `reconcile` (verdicts, conflicts,
materialization), `release`, `report`, `review` (parked publication flow), `store` (DuckDB and fake
Databricks clients). Area helpers are in `*_testkit.py` and `*_builders.py`. Shared fixtures in
`tests/conftest.py`: `settings` (temporary local folder, `ai_mode="local"`), `registry`, `mem_store`
(in-memory DuckDB with the schema) and `demo_store` (plus the synthetic platform and governed views).
Tests use no network and no workspace; Databricks paths run against fakes.

**Markers.** `spark` needs pyspark and a Java runtime and is skipped otherwise; `slow` marks end-to-end
runs over the synthetic reports.

```bash
.venv/Scripts/python.exe -m pytest -q                          # everything available
.venv/Scripts/python.exe -m pytest -q -m "not spark"           # what a release requires
.venv/Scripts/python.exe -m pytest -q -m "not spark and not slow"
KPI_EVIDENCE_EVAL_E2E=1 .venv/Scripts/python.exe -m pytest -q tests/eval/test_evaluate.py
```

**Testing another checkout.** The project venv installs `src` and `libs/reconloop` in editable mode, so it
imports the checkout it was created from, and `settings.default_registry_dir()` and `default_sql_dir()`
follow the imported package. To test another checkout, give it its own venv (`uv venv` and
`uv pip install -e libs/reconloop -e ".[dev]"` there), or put its `src` and `libs/reconloop/src` first on
`PYTHONPATH`. On Windows use Windows paths separated by `;` (in Git Bash, `pwd -W` prints them); a list
such as `/c/a;/c/b` is silently ignored and the editable checkout is imported instead. Check which tree
is imported:

```bash
python -c "import kpi_evidence; from kpi_evidence.settings import default_registry_dir; print(kpi_evidence.__file__, default_registry_dir())"
```

**Synthetic platform and reports.** The synthetic platform is the seed-42 data in
`apps/insight_harness/data/` (240 HCPs, July 2024 to June 2026), loaded by `platform.load_demo_platform`
as `platform_demo` tables plus the restricted `platform_restricted.exfactory_monthly`. The five reports in
`samples/reports/` (with `labels.json` and `README.md`) are generated byte for byte by
`kpi_evidence.eval.synth_reports.generate_all()` from values computed by the independent pandas path in
`eval/truth.py`. `samples/reports/README.md` lists every labeled number with its expected status. Do not
edit these files by hand: change the generator, run `generate_all()` and commit the output;
`tests/eval/test_synth_reports.py` checks that regeneration is byte-identical. June 2026 reference
values pinned in `tests/platform/test_registry_e2e.py` include TRx 5,783.041, Q2 TRx 17,220.318, TRx share
9.17% (IL-17 class 26.21%), call attainment 84.42% (writers only 91.92%, mean of ratios 85.76%), referrals
147 observed and 183.75 projected, speaker attendance 64 (47 distinct HCPs) and West TRx 1,265.7.

**Evaluation.** `eval.evaluate.run_evaluation(settings)` runs `pipeline.run_local` over the labeled set with
the registry (not the KPI table), confirms labeled metadata for documents awaiting confirmation, and
scores the store with `eval.metrics.compute_metrics`: extraction accuracy, mapping accuracy, status
accuracy, false-validation rate (VERIFIED where the label says otherwise), goal-list coverage, reviewer
workload and processing time. It writes an `eval_runs` row and logs to MLflow when an experiment is set.
It scores all 75 labeled numbers, including the 34 of the superseded first deck; a plain
`ReportCheck.demo().run()` reports current documents only (38 comparisons, README "Status").
The labels were built with the engine, so the evaluation is a regression baseline, not a measure of
accuracy on real decks.

**Python 3.11 with pandas 1.5.3.** Databricks LTS runtimes keep their own pandas, so the package supports
pandas 1.5.3 and later. To run the suite on that combination:

```bash
uv venv --python 3.11 .venv311
uv pip install --python .venv311/Scripts/python.exe "pandas==1.5.3" "numpy==1.26.4" "pyarrow==14.0.2" \
    -e libs/reconloop -e . pytest "python-pptx>=1,<2" reportlab sqlglot
.venv311/Scripts/python.exe -m pytest -q -m "not spark"
```

`streamlit` and `altair` from the `dev` extra are not needed for the tests.

## 8. Known limits

* Databricks paths (Spark store, SQL warehouse, `ai_query`, `ai_parse_document`, bundle) are verified
  against fakes and local Spark 4.0.1, not on a workspace; `docs/DEPLOY.md` section 13 lists what to test
  on first use.
* YTD windows in SQL are calendar years (`compile.window_predicate`); `fiscal_year_start_month` affects
  period parsing only.
* Scope dimensions are limited to region, district, territory, specialty and payer channel, and only
  allow-listed values are recognized.
* Units are `count`, `usd` and `fraction`; other currencies read as `unknown`.
* Images, scans and image-only slides are not read locally (UNCERTAIN_EXTRACTION). On Databricks,
  `ai_parse_document` needs AI on and files read from `/Volumes/...`; chart values read from images stay
  below the extraction gate, and deck page numbers are assumed to equal slide numbers
  (`AI_PAGE_MAPPING_UNVERIFIED`). Small images on slides with native data, and logos under 3% of the slide,
  are not recorded.
* Legacy `.ppt` and `.xls` files and encrypted files are registered with a remedy but not read.
* KPI table alternatives work with `value_sql` only; more than 16 candidate combinations skip the full grid.
* A multi-month window needs every month on the platform; otherwise the result is PLATFORM_DATA_ISSUE.
* The Databricks function text (`materialize.function_sql`) returns NULL for a zero denominator, while the
  pipeline raises an error.
* DuckDB state files keep the CHECK constraints they were created with; a new enum value needs a new state
  file.
* `registry/policy.yaml` holds `auto_publish.*`, `metadata.required_fields` and
  `conflicts.compare_display_rounding`. The metadata confidence threshold is
  `Settings.metadata_confidence_threshold`, and a model-proposed mapping is never accepted without a
  person; neither is configurable in the policy file.
* The review app, publication, Genie agent and dashboard are parked (`resources/parked/`,
  `docs/UX_app_parked.md`); `review.py`, `serving.py`, `exports.py` and `genie.py` are kept and tested but
  not used by `ReportCheck`.
