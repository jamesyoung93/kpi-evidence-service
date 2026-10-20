# Notebook API reference

The public API of `kpi_evidence` as used from a Databricks notebook or a local Python session: the
`ReportCheck` class, the KPI table, and the `CheckResult` it returns. How the package works inside, and how
to change it, is in [ARCHITECTURE.md](ARCHITECTURE.md); deployment and permissions are in
[DEPLOY.md](DEPLOY.md).

## 1. Quick start

```python
import kpi_evidence as ke

check = ke.ReportCheck(spark)                       # Databricks notebook: KPI SQL runs on live tables via spark
check.add_reports("/Volumes/main/brand/reports/2026/")   # files, folders, globs, (name, bytes) pairs
check.set_kpis("/Volumes/main/brand/kpi_table.xlsx")     # optional: KPI table with table/rule hints
result = check.run()                                # extract -> interpret -> map -> compute -> compare
result.show()                                       # visual HTML summary inline (displayHTML / IPython)
display(result.comparisons)                         # one row per comparison (pandas; result.to_spark() for Spark)
result.save_html("/Volumes/main/brand/out/kpi_check.html")
result.save_excel("/Volumes/main/brand/out/kpi_check.xlsx")
result.save_sql("/Volumes/main/brand/out/kpi_check_sql")    # one .sql file per SQL artifact
display(result.platform_values)                     # the platform series computed by that SQL
```

Output paths accept the same spellings as the inputs: `/Volumes/...` and `dbfs:/Volumes/...`.

Repeat checks across notebook sessions without repeating work, or check a few KPIs only:

```python
with ke.ReportCheck(spark, state_path="/Volumes/main/brand/state/kpi_check.duckdb") as check:
    check.add_reports("/Volumes/main/brand/reports/2026/")
    check.set_kpis("/Volumes/main/brand/kpi_table.xlsx")
    result = check.run(focus=["trx", "NRx"])         # or max_priority=1
    display(result.changes_since())                  # what moved since the previous run of these reports
    display(check.history())                         # runs recorded in the state file
```

Local demo (no Databricks): `ke.ReportCheck.demo().run().save_html("kpi_check.html")` uses the bundled
registry, synthetic platform, sample reports and `samples/kpi_table_example.csv`.

Nothing is published anywhere. State lives in an in-memory DuckDB unless `state_path=` (a DuckDB file, for
example on a Volume) or `persist=True` (Delta service tables).

## 2. `ReportCheck` (`src/kpi_evidence/check.py`)

```python
class ReportCheck:
    def __init__(self, spark=None, *, settings=None, registry="auto", brand=None, persist=False,
                 warehouse_id=None, catalog=None, gov_schema=None, ai_mode=None, dimension_values=None,
                 load_demo_platform=False, strict_metadata=False, state_path=None, reuse=True): ...
    @classmethod
    def demo(cls, *, kpi_table=True, **kwargs) -> "ReportCheck": ...
    def add_reports(self, sources, *, brand=None, report_type=None, period=None) -> pd.DataFrame
    def set_kpis(self, table, *, sheet=None, use_registry=True) -> pd.DataFrame   # hint rows + validation messages
    def confirm(self, report, **fields) -> None                                   # by file name or doc_id
    @property
    def documents(self) -> pd.DataFrame
    def run(self, *, materialize=True, focus=None, max_priority=None) -> CheckResult
    def focus_kpis(self, focus=None, max_priority=None, *, registry=None) -> frozenset[str] | None
    def history(self) -> pd.DataFrame        # run_id, started, documents, comparisons, reused, focus (oldest first)
    def save_state(self) -> str | None       # write the working copy back to state_path
    def close(self, *, save=True) -> None    # also on leaving a `with ReportCheck(...)` block
    def default_brand(self) -> str | None    # brand argument, KPI table brand, or the registry's only brand
    def governed_views(self) -> list[str]    # governed views the platform can read
    def platform_description(self, views=None) -> str   # where the KPI SQL ran, as shown in the report
```

| Argument | Effect |
|---|---|
| `spark` | KPI SQL runs on the live tables through this Spark session, with the notebook user's permissions |
| `warehouse_id` | without `spark`: KPI SQL runs on this SQL warehouse (for example from a local IDE with SDK authentication) |
| neither | local mode: state and platform are one DuckDB; `load_demo_platform=True` (implied by `demo()`) loads the synthetic platform and governed views |
| `persist=True` | working state goes to the Delta service tables (run the `kpi_bootstrap` job first) instead of memory |
| `state_path` | a `.duckdb` file (local, `/Volumes/...` or `dbfs:/Volumes/...`) that keeps results between runs; exclusive with `persist=True` |
| `registry` | `"auto"` (default): the bundled registry in local demo mode, none (an empty registry) otherwise; `"bundled"`, `"none"`, a directory path or a `Registry` |
| `brand` | brand for reports that do not name one |
| `catalog`, `gov_schema` | where the governed views live (registry and demo mode) |
| `ai_mode` | `"off"` (default), `"ai_query"` or `"gateway"` (`"local"` is a deterministic stand-in for tests); AI only proposes, it never decides a flag |
| `dimension_values` | extra scope values, for example `{"region": ["East", "West"]}` |
| `strict_metadata` | `True`: skip reports whose metadata nobody confirmed instead of using the best proposals |
| `reuse` | `False`: recompute every comparison even when nothing changed |

Behavior:

* `demo(kpi_table=True)` (the default) loads the synthetic platform, adds the sample reports and passes
  `samples/kpi_table_example.csv` to `set_kpis`. A row whose `kpi` is a registry KPI (for example `trx`)
  replaces that KPI's registry definition for the check (`definition_source = "hint"`).
  `demo(kpi_table=False)` runs the bundled registry alone, so the two definitions can be compared on the
  same reports. Other keyword arguments go to `ReportCheck`.
* `add_reports` registers every supported file (pptx, pptm, pdf, xlsx, xlsm, png, jpg, jpeg, tif, tiff),
  recursing into folders; `/Volumes/...` paths are read through the FUSE path in notebooks. Legacy (ppt,
  pps, xls), other unsupported and encrypted files are registered with their remedy and appear in
  `result.issues`. Metadata given to `add_reports` applies to the files of that call. A `(name, bytes)`
  pair is registered like a file, so a report built in memory (for example a one-sheet workbook) can be
  checked with `add_reports(("name.xlsx", data), brand=..., report_type=..., period=...)`.
* Metadata: proposals are computed from the file; stated metadata (`add_reports(brand=..., period=...)`,
  `confirm(...)`) wins. A report whose required fields were not read with confidence is processed with its
  best proposals and marked "metadata not confirmed"; with `strict_metadata=True` it is skipped and listed
  in `result.issues`. A default brand (argument, KPI table `brand` column, or the registry's only brand)
  fills a missing brand.
* Scope values (for example "West" in chart categories) come from the governed views when the platform has
  them, from `SELECT DISTINCT` of every hinted `scope_columns` column (at most 1,000 values per column), and
  from `dimension_values`. Without any, chart categories that are not periods are flagged and never
  treated as the brand total.
* `run()` extracts, reads metadata, interprets, maps, compares, materializes the SQL and checks for
  conflicts between reports. Data-quality checks run only when the governed views exist. Check-level
  messages (metadata not applied, scope values, data quality, KPI table, engine notes, AI failures) are part
  of `result.issues`. Each run is recorded in the state for `history()` and `changes_since()`.
* `state_path`: the file is copied to a local temporary file when the check is created, the run works on
  that copy, and the copy is written back after every successful `run()` and on `close()` or leaving the
  `with` block, through `<state_path>.tmp` and a rename (a plain copy where the volume does not support
  renames). A failed run, or an exception inside the `with` block, never writes the state back.

## 3. KPI table (`src/kpi_evidence/hints.py`)

Accepted inputs: a CSV or XLSX path (Volume or local), a pandas DataFrame, a Spark DataFrame, a table name
(read through the check's Spark session), a dict or a list of dicts. Column names ignore case, spaces and
underscores; blanks are allowed. One row per KPI (repeat a KPI with `brand` to scope it). The full column
reference with examples is in [samples/KPI_TABLE.md](../samples/KPI_TABLE.md).

| Column | Required | Meaning |
|---|---|---|
| `kpi` | yes | Name or id. id = lower_snake_case slug; an existing registry id redefines how that KPI is computed |
| `aliases` | no | `;`-separated labels as printed in reports (the name is always an alias) |
| `table` | for a platform check | One or more candidate tables or views, `;`-separated, fully qualified. First = primary |
| `value_sql` | one of | Aggregate expression, for example `SUM(trx_units)` or `SUM(calls) / NULLIF(SUM(call_plan), 0)` |
| `numerator`, `denominator` | one of | Aggregate expressions for a ratio of sums |
| `date_column` | for a platform check | Column with the row date |
| `date_format` | no | `date` (default: DATE, TIMESTAMP or `YYYY-MM-DD`), `yyyy-MM`, `yyyyMM` |
| `filters` | no | Static SQL predicate, for example `channel = 'RETAIL'` |
| `brand_column` | no | Column to filter by the report's brand (omit when the table holds one brand) |
| `scope_columns` | no | `region=region_name; territory=terr_id` (report scope -> column) |
| `unit` | no | `count` (default), `usd`, `percent` / `fraction` |
| `grain` | no | Allowed periods: `month; quarter; r3m; r6m; r12m; ytd` (default all) |
| `tolerance` | no | `0.5%` relative, `0.05pp` absolute percentage points, `abs:12` absolute |
| `alternatives` | no | `name=EXPR; name2=EXPR` alternative value expressions to test as explanations |
| `brand` | no | Restrict the row to one brand |
| `priority` | no | Whole number, 1 = highest; blank = lowest. Drives `run(max_priority=n)` and the report order |
| `notes`, `owner` | no | Carried into the report |

Rules:

* A row without `table` is a report-only KPI: matching report values are captured (flag GOOD, "Captured
  (report only)").
* Hint SQL fragments are user code executed with the notebook user's own permissions. They are validated:
  no `;`, no comments, no braces, no backslashes, no quotes inside quoted text, no statement keywords; table
  names must be identifiers of one to three parts (a name without a schema is a warning). Invalid rows are
  returned with an error message and skipped, never silently.
* Several candidate tables become a declared `source` choice and `alternatives` a declared `measure`
  choice, with the primary as the default. Only the primary definition can produce VERIFIED; a match on any
  other candidate is INCOMPATIBLE_DEFINITION "matches alternative", also when the primary fails to run
  (missing table or column), so the report shows which candidate works.
* KPI table rows are draft definitions: `definition_source = "hint"` everywhere, and their verdicts
  validate the hint, not a governed definition.

## 4. `CheckResult` (`src/kpi_evidence/report.py`, `report_html.py`, `report_excel.py`)

```python
@dataclass
class CheckResult:
    comparisons: pd.DataFrame   # section 5
    kpis: pd.DataFrame          # one row per KPI definition used: periods compared, matches, verdict (section 6)
    documents: pd.DataFrame     # file, format, brand, report type, period, version, status, metadata confirmed?
    extraction: pd.DataFrame    # every extracted value with its source location (inventory)
    sql: pd.DataFrame           # every deterministic SQL artifact (kpi, definition, choices, sql, export path)
    issues: pd.DataFrame        # extraction issues, metadata needing confirmation, invalid KPI rows, failures
    summary: dict               # counts by flag / status / document, run id, platform description, timings
    platform_values: pd.DataFrame  # the materialized platform series of this run's SQL artifacts
    def show(self) -> None
    def text_summary(self) -> str        # a few plain lines: counts per tile and verdict (also the repr)
    def to_html(self, *, title=None) -> str
    def save_html(self, path, *, title=None) -> str
    def save_excel(self, path) -> str
    def save_sql(self, folder) -> list[str]   # one .sql file per SQL artifact, from the stored SQL text
    def frames(self) -> dict[str, pd.DataFrame]   # the seven frames by name
    def to_spark(self, spark=None) -> dict[str, "pyspark.sql.DataFrame"]
    def evidence(self, key) -> dict      # key = comparison row index, result_id or observation_id
    def filter(self, *, flag=None, kpi=None, report=None) -> pd.DataFrame
    def changes_since(self, previous_run_id=None) -> pd.DataFrame
```

`report.build_result(store, registry, settings, doc_ids, *, hints=None, run_id=None, platform_description="",
timings=None, extra_issues=None, focus_kpis=None, reused_ids=None, previous_runs=None)` assembles it from
the state store.

* `summary` also holds `focus` (sorted KPI ids, or None), `reused` (comparisons taken from saved results)
  and `ai` (mode, outcome, requests, failures).
* `platform_values`: one row per SQL artifact, period grain and period end, for every period the platform
  covers. Columns: `kpi_id, kpi_label, definition, choices, scope, period_grain, period_start, period_end,
  brand, value, unit, components, error, computed_at, artifact_id`. It is part of `to_spark()` and of the
  Excel workbook.
* `save_sql(folder)` writes `<folder>/<kpi_id>/<kpi_id>_<first 16 characters of the artifact id>.sql` for
  every row of `sql` (value SQL, ratio components, checks and the Databricks function, with `{as_of}` left
  as a placeholder) and returns the paths. It uses the SQL text held in the result, so it works after the
  check is closed.
* `save_html`, `save_excel` and `save_sql` map `dbfs:/Volumes/...` to the `/Volumes/...` mount path and
  create missing parent folders.
* `issues` includes the warnings and errors the extractors reported (category `extraction issue`: a page,
  slide or sheet that could not be read, a formula cell without a saved result), one row per report, code
  and severity. They are kept in the state, so later runs that reuse the extraction still list them.
* `extraction` has `in_focus` and `reason` after `status`: outside a focus, `in_focus = False`, `kpi_id` is
  the mapped KPI, `status` is blank and `reason` is "outside this run's focus".
* `documents.metadata_confirmed` (and the comparison column of the same name) is True when a person stated
  the metadata (`confirm()`, `add_reports(brand=..., period=...)`) or every required field was proposed with
  enough confidence; False when the run used proposals nobody confirmed, when the document awaits
  confirmation, or when it was never read. `metadata_basis` says which.
* `changes_since(previous_run_id=None)`: comparisons that are `new`, `changed` (status, report value or
  platform value) or `removed` versus an earlier run. The default previous run is the latest earlier run in
  the state that covered any of the same documents. When that run had a focus, comparisons of KPIs outside
  it are compared with the latest earlier run that covered them (their `previous_run_id` names that run),
  so an unfocused run after a focused one lists only real changes. Columns: change, report_file, kpi_id,
  kpi_label, period_end, scope, previous_status, status, previous_report_value, report_value,
  previous_platform_value, platform_value, previous_run_id, result_id, observation_id, doc_id.

## 5. Comparison columns (in this order)

`report_file, source_type, page, object, label_as_printed, value_as_printed, report_value, unit,
period_label, period_start, period_end, period_grain, scope, kpi_id, kpi_label, mapping_method,
definition_source, definition, platform_tables, sql, platform_value, abs_diff, rel_diff, tolerance,
match_basis, flag, status, status_label, reason, reused, matched_alternative, alternative_value,
metadata_confirmed, result_id, observation_id, doc_id`

* `source_type`: "PowerPoint chart", "PowerPoint table", "Embedded Excel", "Excel workbook", "Slide text",
  "PDF table", "PDF text", "Image (not read)", "AI-parsed page", "Reviewer correction".
* `object`: chart or table title, sheet name or text line. `page`: slide or page number.
* `sql`: the exact SQL executed for the platform value under the definition used (both components for a
  ratio); `platform_tables`: the relations it reads.
* `flag`: GOOD (VERIFIED, REPORT_ONLY), INFO (UNMAPPED_CAPTURED), REVIEW (VARIANCE_REVIEW,
  INCOMPATIBLE_DEFINITION, UNCERTAIN_EXTRACTION, AMBIGUOUS_MAPPING, REPORT_CONFLICT), BLOCKED
  (MISSING_ACCESS, PLATFORM_DATA_ISSUE, BLOCKED). `status_label` and the tooltip text come from
  `contracts.STATUS_INFO`.
* `reused`: True when no platform SQL ran for the row in this run (a saved result was reused, section 10,
  or the report was not reprocessed because nothing changed).
* Rows are sorted by KPI priority (when the KPI table gives one; blanks last), flag (REVIEW, BLOCKED, GOOD,
  INFO), KPI, period, report file and page.

## 6. KPI verdicts (`result.kpis`)

One row per KPI definition, sorted by priority then verdict. Columns: `kpi_id, kpi_label,
definition_source, priority, verdict, verdict_detail, periods_compared, periods_validated, comparisons,
verified, review, blocked, report_only, first_period, last_period, unit, definition, platform_tables,
hint_table, hint_value_sql, hint_notes, hint_row, error`. With a focus, only focus KPIs are listed.
`verdict`:

* `validated`: every compared period VERIFIED under the primary definition. A period (grain, period end
  and scope) counts as validated only when every comparison of the KPI in it is VERIFIED (a report-conflict
  row counts with the status it had before the conflict);
* `partly validated`: some comparisons VERIFIED, others not, in the same or in other periods; the detail
  names the periods that are not validated and how many of their values match;
* `matches alternative`: no primary match, but a declared alternative or candidate reproduces the reports;
* `not validated`: compared, gaps not explained;
* `not computable`: the SQL could not run (missing object, no access, no data); the error is shown;
* `report only`: no table declared; values captured;
* `not compared`: no report value mapped to this KPI.

## 7. HTML report

From top to bottom: title and run facts (reports, platform, KPI table, focus when set, saved results
reused, run time); five tiles (Matches platform, Captured, Needs review, Blocked, Other numbers) with
tooltips; the KPI verdict table; per KPI a small inline SVG chart of report values against platform values
across periods (historical reports side by side), then the comparison rows for that KPI, each with an
evidence drawer (source location, value as printed, definition, exact SQL, alternatives tried, reason) and
a "reused" chip on reused rows. Documents, issues, the extraction inventory and the SQL artifacts follow,
collapsed. The page is self-contained: inline CSS, a small inline script for table sorting, no external
resources and no network access. Labels are plain names with tooltips, status labels come from
`STATUS_INFO`, and numbers are formatted by unit.

## 8. Excel workbook

Sheets: Summary, Comparisons, KPI verdicts, SQL, Platform series (`result.platform_values`), Documents,
Extracted values, Issues. The header row is bold and frozen, with an autofilter, column widths, percent
format for fraction columns, and wrapped text on the SQL sheet. The workbook is written with openpyxl to a
local temporary file, then copied to the target in one sequential write, because Volumes do not support the
random writes a workbook needs.

## 9. Run focus

`run(focus=["trx", "Total prescriptions"], max_priority=None)`: `focus` takes KPI ids, names, labels or
aliases (resolved the way the KPI table resolves them; an unknown or ambiguous name raises `ValueError`);
`max_priority=n` adds every KPI whose priority is n or better. When either is set, every observation is
still extracted, interpreted and mapped, but only observations whose mapped KPI, or any mapping candidate
(so an ambiguous match that could be a focus KPI is kept), is in the focus are compared and materialized.
Comparisons and KPI verdicts cover the focus KPIs; earlier results of other KPIs of the same documents stay
current, so a later unfocused run shows them again.

## 10. Saved results

A comparison group (one KPI, scope, grain and stated definition within a report) is computed again only
when one of these changed: the report file bytes, the KPI definition (including KPI table rows), the
bindings, the targets (values and periods), the start choices, or the platform data (the platform
snapshot). Otherwise the stored results of the matching comparison run are returned without running any
platform SQL; they keep their result ids and versions and show `reused = True`. Results that were blocked
or denied access are always computed again.

The platform snapshot pins every source of the KPI: governed views map to the tables they read; dotted
KPI table names are read as named (`DESCRIBE HISTORY <table> LIMIT 1` on Databricks; on DuckDB a hash of the
row count and a sum per column). A source that cannot be pinned (a view over non-Delta data, no access, a
missing table) is "unavailable", and a group with any unavailable source is always computed again. The
platform series of a SQL artifact is skipped when every stored row has the same snapshot and the SQL is
unchanged. `ReportCheck(reuse=False)` turns reuse off.
