# KPI Evidence Service

Check the KPI numbers in brand reports (PowerPoint decks with native charts, tables and embedded
Excel; Excel workbooks; PDFs; images, which are read only with AI on in Databricks) against the platform
tables that should reproduce them, from a Databricks notebook, and get back a report that says, for
every number, where it came from, which SQL computed the platform value, and whether the two agree.

```python
import kpi_evidence as ke

check = ke.ReportCheck(spark)                               # KPI SQL runs on live tables via the notebook's Spark session
check.add_reports("/Volumes/main/brand/reports/2026/")      # files, folders, globs, or (name, bytes)
check.set_kpis("/Volumes/main/brand/kpi_table.xlsx")        # optional: KPI table with table/rule hints to validate
result = check.run()                                        # extract -> interpret -> map -> compute -> compare
result.show()                                               # visual summary inline
display(result.comparisons)                                 # one row per comparison
result.save_html("/Volumes/main/brand/out/kpi_check.html")
result.save_excel("/Volumes/main/brand/out/kpi_check.xlsx")
```

Nothing is written to your catalog and nothing is published anywhere: working state is an in-memory
database for the session (pass `state_path=` to keep it in a DuckDB file, or `persist=True` to keep it
in Delta).

## What you get back

`result.comparisons` has one row per number found in the reports:

| Column | Meaning |
|---|---|
| `report_file`, `source_type`, `page`, `object` | where the number came from: PowerPoint chart / table / embedded Excel, PDF table or text, slide text, image |
| `label_as_printed`, `value_as_printed`, `report_value`, `unit` | the label and value exactly as printed, and the parsed value (percents as fractions) |
| `period_label`, `period_start`, `period_end`, `period_grain`, `scope` | the measurement period the slide shows (not the publication month) and the scope (brand total, region, territory ...) |
| `kpi_id`, `kpi_label`, `mapping_method`, `definition_source`, `definition` | which KPI the label matched, how (`SYNONYM`, `MEMORY` for approved mapping memory, `MODEL` for an AI suggestion; `NO_MATCH` or `AMBIGUOUS` otherwise), whether the definition came from your KPI table (`hint`) or the governed registry (`registry`), and the definition in words |
| `platform_tables`, `sql`, `platform_value` | the tables read and the exact SQL executed for this period, and its result |
| `abs_diff`, `rel_diff`, `tolerance`, `match_basis` | the gap, the tolerance applied (relative, absolute, or the rounding of the printed value), and how the values matched (`EXACT`, `WITHIN_ROUNDING`, `WITHIN_TOLERANCE`) |
| `flag`, `status`, `status_label`, `reason` | the preliminary flag and one plain sentence explaining it |
| `reused` | True when a saved result was reused and no platform SQL ran for the row in this run |
| `matched_alternative`, `alternative_value` | a declared alternative definition that reproduces the report, when the primary one does not |
| `metadata_confirmed`, `result_id`, `observation_id`, `doc_id` | whether the report's metadata was confirmed by a person or proposed with confidence, and the row's ids (`result.evidence(...)` takes a row index, `result_id` or `observation_id`) |

Flags:

| Flag | Statuses | Meaning |
|---|---|---|
| GOOD | Matches platform | the platform reproduces the report under the definition you gave (exact, within rounding, or within tolerance) |
| GOOD | Captured (report only) | no table is declared for this KPI; the report value was captured with its source location |
| REVIEW | Variance to review | the platform differs by more than the tolerance |
| REVIEW | Definition differs | the report matches only a different definition, unit or scale (for example writers-only call attainment, or a panel source with a lag) |
| REVIEW | Check the extracted value / Confirm which KPI / Reports disagree | the value, its period or its scope could not be read with confidence, or it sits in an image that was not read; the label could be more than one KPI or is a plan, target or forecast; or two reports disagree |
| BLOCKED | No access to platform data / Platform data issue / Could not evaluate | the SQL could not run (permission, missing table or column, no data); the error is shown |
| INFO | Captured (not on the KPI list) | an extractable number that matched no KPI ("vs PY %" columns and the like) |

`result.kpis` gives one verdict per KPI definition: **validated** (every compared period matches),
**partly validated**, **matches alternative**, **not validated**, **not computable**, **report only**,
or **not compared**. That is how a KPI table of hints gets validated against your historical reports.

## The KPI table

Optional. One row per KPI you care about, with hints at the table and rule that should reproduce
the reports. Hints are treated as candidates to validate, never as truth.

| Column | Example |
|---|---|
| `kpi` | `trx` |
| `aliases` | `TRx; Total Rx; Total prescriptions` |
| `table` | `main.commercial.rx_monthly` (several candidates separated by `;`, the first is primary) |
| `value_sql` | `SUM(trx_units)` (or `numerator` + `denominator` for a ratio of sums) |
| `date_column`, `date_format` | `month`, `yyyy-MM` |
| `filters` | `channel = 'RETAIL'` |
| `brand_column`, `scope_columns` | `brand`, `region=region_name; territory=terr_id` |
| `unit`, `grain`, `tolerance` | `count`, `month; quarter`, `0.5%` |
| `alternatives` | `normalized=SUM(trx_normalized)` (alternative definitions to test as explanations) |

A row without `table` is report-only: values are captured, flagged GOOD, and not verified. Full column
reference: [samples/KPI_TABLE.md](samples/KPI_TABLE.md); worked example:
[samples/kpi_table_example.csv](samples/kpi_table_example.csv).

## Try it locally (no Databricks)

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e libs/reconloop -e ".[dev]"
.venv/Scripts/python.exe -c "import kpi_evidence as ke; ke.ReportCheck.demo().run().save_html('kpi_check.html')"
```

On Linux or macOS the interpreter is `.venv/bin/python`. The venv installs this checkout in editable mode;
to run another checkout with it, see "Testing another checkout" in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) section 7, and confirm which tree is imported with
`python -c "import kpi_evidence; print(kpi_evidence.__file__)"`.

The demo runs five synthetic reports for the fictional brand Kestrel against a synthetic platform
(insight_harness's seed-42 data, loaded into DuckDB), with an example KPI table. It is built so every
flag and verdict appears at least once. The two field updates do not state their period clearly, so
they are listed under issues as awaiting confirmation until you call `check.confirm(...)`.

## How it decides (and why you can trust a "Matches platform")

* **The definition is fixed before the number is computed.** A report value is VERIFIED only when the
  platform reproduces it under the KPI's primary definition (the first table and expression of a KPI
  table row, or the registry's governed default), or under a registry variant the report itself states
  (for example "IL-17 class share"). If only a different declared definition matches, the result is
  "Definition differs", never a match: the closest number is not proof.
* **The model proposes; code computes.** AI is off by default. When on (`ai_mode="ai_query"`), it
  suggests which KPI an unfamiliar label means, proposes report metadata that could not be read, and
  reads scanned pages and images with `ai_parse_document`. A model's KPI suggestion always goes to
  review; it never decides a flag.
* **Every tested configuration is recorded** (reconloop trials), with its exact SQL.
* **Nothing disappears.** Unreadable images, unknown periods, missing tables and permission errors are
  explicit rows with a reason, not empty output.
* **Brand, scope and period are bound from the report**, so a search over definitions can never "fix" a
  gap by quietly switching region or month.

## Layout

```
src/kpi_evidence/
  check.py            ReportCheck (the notebook API)          report*.py   CheckResult, HTML, Excel
  hints.py            KPI table -> draft KPI definitions       registry.py / compile.py   governed KPI registry -> SQL
  intake.py           file sniffing, registration, metadata    extract/     PPTX (vendored parser), XLSX, PDF, ai_parse_document
  standardize.py      values, units, periods, scope            periods.py   period grammar (Q2'26, R3M, YTD, data through ...)
  mapping.py / ai.py  label -> KPI (synonyms, memory, AI)      reconcile.py verdicts, every trial recorded (reconloop)
  materialize.py      deterministic SQL artifacts + series     platform.py  governed views, data-quality checks
  platform_access.py  where KPI SQL runs (Spark / warehouse / DuckDB)
  store/              state tables (in-memory DuckDB, a DuckDB file with state_path, or Delta with persist=True)
registry/             governed KPI definitions (YAML, reviewed in Git)
src/sql/              governed views (the porting seam), service views
notebooks/            Databricks notebook template          samples/   synthetic reports, labels, example KPI table
libs/reconloop        pinned reconloop 0.5.0                  apps/insight_harness   pinned app (hand-off parked)
docs/                 ARCHITECTURE.md, NOTEBOOK.md, DEPLOY.md
```

## For maintainers

Read these before changing the package:

1. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how a report becomes a comparison, the module map, the
   rules every change must keep, step-by-step recipes with the tests to run, and known limits.
2. [docs/NOTEBOOK.md](docs/NOTEBOOK.md): the notebook API (`ReportCheck`, the KPI table, `CheckResult`).
3. [docs/DEPLOY.md](docs/DEPLOY.md): deployment, permissions and AI options on Databricks.

Release steps are in [RELEASE.md](RELEASE.md).

## Run focus and saved results

```python
result = check.run(focus=["trx", "trx_share"])      # or check.run(max_priority=1) with a priority column
check = ke.ReportCheck(spark, state_path="/Volumes/main/brand/state/kpi_check.duckdb")
check.add_reports("/Volumes/main/brand/reports/2026/")
check.set_kpis("/Volumes/main/brand/kpi_table.xlsx")
result = check.run()                                 # unchanged comparisons are reused, marked reused
check.history(); result.changes_since(); check.close()
```

A comparison is computed again only when the report file, the KPI definition, the scope or period, or
the platform table version changed. A source whose version cannot be read is always recomputed.

## Install in Databricks

`python scripts/build_release.py --verify` builds `release/kpi_evidence_<version>/`: the two wheels, an
offline wheelhouse for Linux (x86_64 and ARM, Python 3.11 and 3.12), the notebook, the KPI table template
and `INSTALL.md`. Upload the folder to a Volume, then in a notebook:

```python
%pip install -r /Volumes/<catalog>/<schema>/<volume>/kpi_evidence_0.1.0/requirements.txt
```

Then, in the next cell:

```python
dbutils.library.restartPython()
```

Runtime pandas 1.5.3 and later is supported, so LTS runtimes keep their own pandas.

## Status

* Local end to end: the five synthetic reports hold 75 labeled numbers; the evaluation (registry only,
  metadata confirmed) scores extraction, mapping and status accuracy of 1.0 with no false "Matches
  platform". The labels were built with the engine, so treat this as a regression baseline, not a measure
  of accuracy on real decks.
* A plain `ReportCheck.demo().run()` gives 38 comparisons: the 34 values of the re-issued monthly deck and
  the 4 of the executive summary (the first deck is superseded, and the two field updates await
  confirmation; confirming them gives 41). `ReportCheck.demo(kpi_table=False)` gives the same 38 under the
  registry definitions. Compare its rows before and after a registry change to confirm that new synonyms
  did not remap sample values.
* 1,977 tests pass on Python 3.12 with pandas 3, local Spark included (2 skipped: the Databricks CLI check
  and the opt-in end-to-end evaluation); the tests without Spark also pass on Python 3.11 with pandas 1.5.3
  and numpy 1.26 (the LTS runtime combination), and the demo reproduces exactly there.
* Databricks paths (Spark store, SQL warehouse, ai_query, ai_parse_document, bundle) are verified against
  fakes and local Spark 4.0.1, not yet on a workspace. `docs/DEPLOY.md` lists what to test on first use.
* Parked until agreed: the review app inside insight_harness and automatic publication of approved values.

Upstream pins: `libs/UPSTREAM.lock`.
