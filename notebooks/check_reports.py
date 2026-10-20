# Databricks notebook source
# MAGIC %md
# MAGIC # Check report KPIs against the platform
# MAGIC
# MAGIC This notebook reads report files (PowerPoint, PDF, Excel, images), finds every number with its
# MAGIC location, matches the numbers to KPIs, computes the same KPIs on the platform tables with SQL and
# MAGIC compares the two, period by period. It ends in a **comparison report**: a table you can filter here,
# MAGIC and an HTML page, an Excel workbook and the SQL files saved to the output folder.
# MAGIC
# MAGIC **Nothing is published anywhere.** The working state lives in memory for this run only, unless you
# MAGIC set `state_path`.
# MAGIC
# MAGIC Fill in the widgets at the top, then run all cells:
# MAGIC
# MAGIC | Widget | What to enter |
# MAGIC |---|---|
# MAGIC | `reports` | A Volume folder or file with the reports, e.g. `/Volumes/main/brand/reports/2026/` (folders are read recursively) |
# MAGIC | `kpis` | Optional: a KPI table (CSV or XLSX) with hints - candidate tables, SQL expressions, date columns - to validate |
# MAGIC | `brand` | Optional: the brand, when the reports or the KPI table do not name it |
# MAGIC | `catalog`, `gov_schema` | Where the governed views live (only used when the registry or the views exist) |
# MAGIC | `out_dir` | A Volume folder for the HTML and Excel report and the SQL files |
# MAGIC | `ai_mode` | `off` (default) or `ai_query`: AI only proposes KPI matches and missing report metadata, and reads scanned pages and images; it never decides a flag |
# MAGIC | `wheels` | The folder with the `requirements.txt` to install from, or that file: the release folder on a Volume, or the wheels Volume the `kpi_wheels` job writes to |
# MAGIC | `focus` | Optional: KPI ids or names, comma separated, to check only those KPIs in this run |
# MAGIC | `max_priority` | Optional: check only KPIs whose KPI table `priority` is at most this number (1 = highest) |
# MAGIC | `state_path` | Optional: a `.duckdb` file on a Volume that keeps results between runs, so unchanged comparisons are reused instead of recomputed |

# COMMAND ----------

dbutils.widgets.text("reports", "/Volumes/main/kpi_evidence/reports/check/reports/", "1. Reports (file or folder)")
dbutils.widgets.text("kpis", "", "2. KPI table (optional)")
dbutils.widgets.text("brand", "", "3. Brand (optional)")
dbutils.widgets.text("catalog", "main", "4. Catalog")
dbutils.widgets.text("gov_schema", "kpi_gov", "5. Governed schema")
dbutils.widgets.text("out_dir", "/Volumes/main/kpi_evidence/reports/check/out/", "6. Output folder")
dbutils.widgets.dropdown("ai_mode", "off", ["off", "ai_query"], "7. AI mode")
dbutils.widgets.text("wheels", "/Volumes/main/kpi_evidence/wheels/", "8. Wheels folder or requirements.txt")
dbutils.widgets.text("focus", "", "9. Focus KPIs (optional)")
dbutils.widgets.text("max_priority", "", "10. Max priority (optional)")
dbutils.widgets.text("state_path", "", "11. Saved results file (optional)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Install the package
# MAGIC
# MAGIC Point the `wheels` widget at the folder that holds the `requirements.txt` for the two wheels
# MAGIC (`kpi_evidence` and `reconloop`), or at that file:
# MAGIC
# MAGIC * **Release folder:** upload the folder to a Volume as its `INSTALL.md` describes and enter the
# MAGIC   folder's path, for example `/Volumes/<catalog>/<schema>/<volume>/kpi_evidence_<version>/`.
# MAGIC * **Bundle deployment:** the `kpi_wheels` job copies the wheels into the wheels Volume and writes a
# MAGIC   `requirements.txt` there; enter that Volume's path (the default is `/Volumes/main/kpi_evidence/wheels/`).
# MAGIC   Run that job after every `databricks bundle deploy`.

# COMMAND ----------

wheels = dbutils.widgets.get("wheels").strip()
wheels_requirements = wheels if wheels.endswith(".txt") else wheels.rstrip("/") + "/requirements.txt"
print(f"installing from {wheels_requirements}")

# COMMAND ----------

# MAGIC %pip install --quiet -r $wheels_requirements

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Read the reports and the KPI table
# MAGIC
# MAGIC `ReportCheck(spark)` runs the KPI SQL on the live tables through this notebook's Spark session, with
# MAGIC **your** permissions. `add_reports` registers every supported file (pptx, pptm, pdf, xlsx, xlsm, png,
# MAGIC jpg, tif); legacy (ppt, xls), unsupported or encrypted files are kept with a remedy and listed under issues.
# MAGIC
# MAGIC The KPI table is optional. Each row is a **hint to validate**, not a truth: the check computes the hinted
# MAGIC SQL for every report period and tells you whether it reproduces the reports. The table below lists each
# MAGIC row and whether it was accepted; invalid rows are shown with their error and skipped.

# COMMAND ----------

import datetime

import kpi_evidence as ke

reports = dbutils.widgets.get("reports").strip()
kpis = dbutils.widgets.get("kpis").strip()
brand = dbutils.widgets.get("brand").strip() or None
catalog = dbutils.widgets.get("catalog").strip() or None
gov_schema = dbutils.widgets.get("gov_schema").strip() or None
out_dir = dbutils.widgets.get("out_dir").strip().rstrip("/")
ai_mode = dbutils.widgets.get("ai_mode").strip() or "off"
focus = [k.strip() for k in dbutils.widgets.get("focus").split(",") if k.strip()] or None
max_priority = int(dbutils.widgets.get("max_priority")) if dbutils.widgets.get("max_priority").strip() else None
state_path = dbutils.widgets.get("state_path").strip() or None

check = ke.ReportCheck(spark, catalog=catalog, gov_schema=gov_schema, brand=brand, ai_mode=ai_mode,
                       state_path=state_path)
display(check.add_reports(reports))
if kpis:
    display(check.set_kpis(kpis))
else:
    print("No KPI table: the report lists every extracted number (an inventory) and nothing is compared.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Run the check
# MAGIC
# MAGIC For every report: extract the numbers, read the metadata (brand, report type, period), interpret each
# MAGIC number (label, unit, period, scope such as a region), match it to a KPI, compute the KPI on the platform
# MAGIC for the same period and scope, and compare. Every SQL statement that ran is kept in the report.
# MAGIC
# MAGIC Reports whose brand or period could not be read with confidence are checked with the best guess when
# MAGIC there is one, and marked **metadata not confirmed**. When there is no guess at all, the report is not
# MAGIC compared and is listed under issues as **awaiting confirmation**. In both cases you can state the
# MAGIC metadata yourself: run `check.confirm("deck.pptx", period="May 2026")` (or `brand=...`) in a new cell,
# MAGIC then run the **Run the check** cell (`result = check.run(...)`) and the cells after it again.
# MAGIC
# MAGIC With `focus` or `max_priority`, only those KPIs are computed; every other number is still extracted and
# MAGIC listed in `result.extraction` as outside this run's focus. With `state_path`, a comparison is computed
# MAGIC again only when the report file, the KPI definition, the scope or period, or the platform table version
# MAGIC changed; otherwise the saved result is reused and marked `reused`. Results are saved to `state_path` at
# MAGIC the end of each run.

# COMMAND ----------

result = check.run(focus=focus, max_priority=max_priority)
result.show()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Read the comparisons
# MAGIC
# MAGIC One row per number compared. The most useful columns:
# MAGIC
# MAGIC * `report_file`, `source_type`, `page`, `object`, `label_as_printed`, `value_as_printed` - where the number is.
# MAGIC * `kpi_id`, `definition_source` (`registry` or `hint`), `platform_tables`, `sql` - what was computed and how.
# MAGIC * `report_value`, `platform_value`, `abs_diff`, `rel_diff`, `tolerance`, `period_label` - the comparison.
# MAGIC * `flag` and `reason` - the preliminary verdict in plain words.
# MAGIC
# MAGIC How to read the flags:
# MAGIC
# MAGIC | Flag | Meaning | What to do |
# MAGIC |---|---|---|
# MAGIC | **GOOD** | The platform reproduces the report value under the primary definition, or the KPI has no table and the value was captured | Nothing; spot-check the SQL if the definition is new |
# MAGIC | **REVIEW** | A gap the tolerance does not cover, a different definition (e.g. an alternative table matches), two reports disagreeing, a value read with low confidence, or an unclear KPI match | Open the row's evidence (`result.evidence(i)`) and decide |
# MAGIC | **BLOCKED** | The SQL could not run: missing table or column, no access, or no data for the period | Fix the KPI table hint or request access |
# MAGIC | **INFO** | A number that matches no KPI | Add it to the KPI table if it matters |
# MAGIC
# MAGIC These flags are preliminary: a match found only on an alternative table is never called validated.

# COMMAND ----------

display(result.comparisons)

# COMMAND ----------

# MAGIC %md
# MAGIC ### KPI verdicts and issues
# MAGIC
# MAGIC `result.kpis` has one row per KPI definition: in how many periods it matched (`validated`,
# MAGIC `partly validated`, `matches alternative`, `not validated`, `not computable`, `report only`,
# MAGIC `not compared`). A period counts as validated only when every report value for it matches the
# MAGIC platform. `result.issues` lists what needs a person: unreadable files or pages, unconfirmed metadata,
# MAGIC invalid KPI table rows and SQL failures.

# COMMAND ----------

display(result.kpis)

# COMMAND ----------

display(result.issues)

# COMMAND ----------

display(check.documents)

# COMMAND ----------

if state_path:
    display(check.history())
    display(result.changes_since())

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Save the report
# MAGIC
# MAGIC The HTML page is self-contained (open it from the Volume or attach it to an email); the Excel workbook
# MAGIC has a summary sheet and one sheet per table (comparisons, KPI verdicts, SQL, platform series, documents,
# MAGIC extracted values, issues). The SQL folder holds one `.sql` file for each KPI definition, scope and period grain that
# MAGIC produced platform values, so the platform numbers can be computed again outside this notebook.

# COMMAND ----------

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
dbutils.fs.mkdirs(out_dir)
html_path = result.save_html(f"{out_dir}/kpi_check_{stamp}.html")
excel_path = result.save_excel(f"{out_dir}/kpi_check_{stamp}.xlsx")
sql_files = result.save_sql(f"{out_dir}/kpi_check_{stamp}_sql")
print(f"HTML report:    {html_path}")
print(f"Excel workbook: {excel_path}")
print(f"SQL files:      {len(sql_files)} in {out_dir}/kpi_check_{stamp}_sql")
