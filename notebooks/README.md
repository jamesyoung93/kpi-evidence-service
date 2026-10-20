# Notebooks

`check_reports.py` checks report KPIs against the platform: it reads report files (PowerPoint, PDF,
Excel, images), finds every number with its location, matches the numbers to KPIs, computes the same
KPIs on the platform tables with SQL through the notebook's Spark session, and compares them period by
period. It ends in a comparison report - tables shown in the notebook, and an HTML page, an Excel
workbook and the SQL files in the output folder. Nothing is published anywhere.

The API behind it is `kpi_evidence.ReportCheck` (`docs/NOTEBOOK.md`); deployment and permissions are in
`docs/DEPLOY.md`.

## Getting the notebook into a workspace

The file is in **Databricks source format**: a `.py` file whose first line is
`# Databricks notebook source`, with cells separated by `# COMMAND ----------` and Markdown cells as
`# MAGIC %md`. Databricks recognizes that header and imports the file as a Python notebook.

* **Bundle**: `databricks bundle deploy` syncs it as a notebook to
  `<root_path>/files/notebooks/check_reports` (dev:
  `/Workspace/Users/<you>/.bundle/kpi-evidence-service/dev/files/notebooks/check_reports`). That copy is
  overwritten by the next deploy: to keep your own edits, clone it first.
* **Import in the UI**: Workspace > (your folder) > kebab menu > **Import** > File > choose
  `check_reports.py`.
* **Import with the CLI**:

  ```bash
  databricks workspace import /Users/<you>/check_reports --file notebooks/check_reports.py --format SOURCE --language PYTHON
  ```

Attach **serverless** compute (or a Unity Catalog cluster with Python >= 3.11). The package itself comes
from the wheels (next section), not from the import.

## Before the first run: the wheels

The install cell runs `%pip install -r <wheels widget>` and restarts Python. The `requirements.txt` it
points at installs the two wheels (`reconloop`, `kpi_evidence`):

* release folder: upload `kpi_evidence_<version>/` to a Volume (its `INSTALL.md`) and point the widget at
  the `requirements.txt` in that folder; it installs offline from the folder's wheels and wheelhouse;
* bundle path: `databricks bundle run kpi_wheels -t <target>` after every deploy writes
  `/Volumes/<catalog>/<schema>/wheels/requirements.txt`;
* quickest path: `python scripts/publish_wheels.py build --volume-dir /Volumes/<c>/<s>/<v>/wheels`
  on a laptop, then upload the three files (DEPLOY.md section 1).

The bundle and quickest-path files list the wheels by absolute Volume path and take the dependencies
from PyPI or the workspace's package repository.

## Widgets

Fill them in at the top, then **Run all**. Empty optional widgets mean "not given".

| Widget | Default in the notebook | What to enter |
|---|---|---|
| `reports` | `/Volumes/main/kpi_evidence/reports/check/reports/` | a Volume folder or file with the reports (folders are read recursively) |
| `kpis` | empty | optional KPI table (CSV or XLSX) with hints to validate - candidate tables, SQL expressions, date columns (DEPLOY.md section 5). Empty: an inventory of every extracted number, nothing compared |
| `brand` | empty | optional brand, when neither the reports nor the KPI table name it; reports without a readable brand otherwise wait for `check.confirm(..., brand=...)` |
| `catalog` | `main` | catalog of the governed views (used only in registry / demo mode) |
| `gov_schema` | `kpi_gov` | schema of the governed views (same) |
| `out_dir` | `/Volumes/main/kpi_evidence/reports/check/out/` | a Volume folder for the HTML and Excel report and the SQL files (you need WRITE VOLUME) |
| `ai_mode` | `off` | `off` or `ai_query`: AI only proposes KPI matches and missing report metadata, and reads scanned pages and images; it never decides a flag (DEPLOY.md section 10) |
| `wheels` | `/Volumes/main/kpi_evidence/wheels/` | the folder that holds `requirements.txt` (the release folder or the wheels volume), or the path of a `requirements.txt` itself |
| `focus` | empty | optional KPI ids or names, comma separated: only those KPIs are compared in this run |
| `max_priority` | empty | optional whole number: only KPIs whose KPI table `priority` is this number or better (1 = highest) |
| `state_path` | empty | optional `.duckdb` file on a Volume that keeps results between runs, so unchanged comparisons are reused |

The defaults assume catalog `main` and the schema name `kpi_evidence`. In a dev deployment the schema
name carries the dev prefix, so adjust `reports`, `out_dir` and `wheels` when you run the notebook by hand.

**Scheduled runs**: the `kpi_check` job runs this notebook with job parameters of the same names
(`reports`, `kpis`, `out_dir`, `brand`, `catalog`, `gov_schema`, `ai_mode`, `wheels`), filled from the
bundle's volumes; `focus`, `max_priority` and `state_path` keep their empty defaults. Override the job
parameters per run with
`databricks bundle run kpi_check -t <target> --params reports=...,kpis=...`. If you add or rename a
widget, change `resources/jobs.yml` too (`tests/deploy` checks that every parameter the job passes is a
widget of this notebook).

## Where the outputs land

| Output | Where |
|---|---|
| Visual report | inline, from `result.show()` (in a job run: on the run's page) |
| Comparisons, KPI verdicts, issues, documents | `display(...)` tables in the notebook; `result.comparisons` etc. stay available for your own cells (`result.to_spark()` for Spark DataFrames) |
| HTML report | `<out_dir>/kpi_check_<UTC timestamp>.html` (`result.save_html`) - self-contained, opens from the Volume or as an email attachment |
| Excel workbook | `<out_dir>/kpi_check_<UTC timestamp>.xlsx` (`result.save_excel`) - Summary, Comparisons, KPI verdicts, SQL, Platform series, Documents, Extracted values, Issues |
| SQL files | `<out_dir>/kpi_check_<UTC timestamp>_sql/<kpi_id>/*.sql` (`result.save_sql`) - one file per SQL artifact, to compute the platform values again outside the notebook |
| Working state | in memory for this run (DuckDB) plus a temporary folder for SQL exports; both are gone when the session ends. With `state_path`, the state is saved to that file after every run and on `check.close()` |

Nothing is written to Unity Catalog tables. With `ke.ReportCheck(spark, persist=True)` (edit the cell
and leave `state_path` empty) the working state goes to the Delta service tables and the SQL exports to
the reports volume's `artifacts/sql/` - run `kpi_bootstrap` first (DEPLOY.md section 8).

## Permissions you need

`SELECT` on every table the KPI table names, `READ VOLUME` on the reports folder and the wheels volume,
`WRITE VOLUME` on `out_dir`. The KPI SQL runs with **your** permissions; a table you cannot read shows up
as `MISSING_ACCESS` (KPI verdict "not computable"), never as a silent gap.
