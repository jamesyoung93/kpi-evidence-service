# kpi_evidence

Checks the KPI numbers printed in reports (PowerPoint decks with native charts, tables and embedded
Excel; Excel workbooks; PDFs; images, which are read only with AI on in Databricks) against the
platform tables that should reproduce them, from a Databricks notebook. The result says, for every
number, where it came from, which SQL computed the platform value and whether the two agree.

## Use it in a notebook

```python
import kpi_evidence as ke

check = ke.ReportCheck(spark)
check.add_reports("/Volumes/main/brand/reports/2026/")
check.set_kpis("/Volumes/main/brand/kpi_table.xlsx")
result = check.run()
result.show()
display(result.comparisons)
result.save_html("/Volumes/main/brand/out/kpi_check.html")
result.save_excel("/Volumes/main/brand/out/kpi_check.xlsx")
```

* `ReportCheck(spark)` runs the KPI SQL on the live tables through the notebook's Spark session, with
  your permissions.
* `add_reports` takes files, folders, globs or `(name, bytes)` pairs.
* `set_kpis` is optional. Each row of the KPI table is a hint to validate (candidate tables, a SQL
  expression, a date column), never a truth.
* `result.comparisons` has one row per number; `result.kpis` has one verdict per KPI definition;
  `result.issues` lists what needs a person.
* `check.run(focus=["trx"])` or `check.run(max_priority=1)` checks only some KPIs.
* When a report's brand or period cannot be read, run `check.confirm("deck.pptx", period="May 2026")`
  and then `check.run()` again.

Nothing is written to your catalog and nothing is published. The working state lives in memory unless
you pass `state_path=` (a DuckDB file that keeps results between runs, so unchanged comparisons are
reused) or `persist=True` (Delta tables).

To try it without Databricks, run the bundled demo over five synthetic reports:

```python
import kpi_evidence as ke

ke.ReportCheck.demo().run().save_html("kpi_check.html")
```

## Flags

| Flag | Meaning |
|---|---|
| GOOD | The platform reproduces the report value under the definition you gave (exact, within rounding or within tolerance), or the KPI has no table and the value was captured with its location |
| REVIEW | A gap the tolerance does not cover, a different definition, two reports that disagree, or a value, period or KPI that could not be read with confidence |
| BLOCKED | The SQL could not run (no access, missing table or column, no data for the period); the error is shown |
| INFO | A number that matches no KPI |

Flags are preliminary. A report value counts as a match only under the KPI's primary definition; if only
an alternative definition reproduces it, the flag is REVIEW. AI is off by default; when on
(`ai_mode="ai_query"`), it only proposes KPI matches and missing report metadata and reads scanned
pages and images. It never decides a flag.

## Install from the release folder

Upload the release folder `kpi_evidence_<version>/` to a Unity Catalog volume, keeping its name. In a
notebook on serverless compute or a Unity Catalog cluster (Python 3.11 or 3.12):

```python
%pip install -r /Volumes/<catalog>/<schema>/<volume>/kpi_evidence_<version>/requirements.txt
```

Then, in the next cell:

```python
dbutils.library.restartPython()
```

The install needs no internet access: the folder holds both wheels and every dependency. The runtime
keeps its own pandas (1.5.3 or later). The folder also has a ready-made notebook
(`notebooks/check_reports.py`), a KPI table template and `INSTALL.md` with the details.
