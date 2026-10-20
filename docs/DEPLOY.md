# Deploying the KPI Evidence Service on Databricks (notebook first)

The product is a **code package used from a notebook** (`docs/ARCHITECTURE.md` explains how it works). In a
Databricks notebook the Spark session reaches the live warehouse tables, so the KPI SQL
runs there. A run ends in a **comparison report** - a DataFrame with one row per compared value, a
visual HTML page and an Excel workbook. Nothing is published or piped onward automatically.

This guide covers: the quickest path (two wheels, one Volume, one notebook; no bundle), the bundle path,
the KPI table, permissions, AI options, the parked pieces and how to switch them on later, and the items
to test on first use. The notebook API is in `docs/NOTEBOOK.md`; the package internals and how to change
them are in `docs/ARCHITECTURE.md`.

Names: "Declarative Automation Bundles" is the 2026 name of Databricks Asset Bundles (same
`databricks bundle` CLI and YAML). "Unity Gateway" is the 2026 name of AI Gateway. "Genie agent" is the
2026 name of a Genie space.

## 0. What gets deployed, and what is parked

| Piece | File | Default | Purpose |
|---|---|---|---|
| Package wheels `kpi_evidence`, `reconloop` | `databricks.yml` `artifacts` | built and uploaded by `bundle deploy` | the engine; job environments install them directly |
| Notebook `check_reports` | `notebooks/check_reports.py` (bundle sync) | synced | the user surface: widgets in, comparison report out |
| Schemas `kpi_evidence`, `kpi_gov` | `resources/uc.yml` | created | optional Delta state (`persist=True`); governed views (registry / demo mode) |
| Volume `reports` | `resources/uc.yml` | created | `check/reports/`, `check/kpi_table.csv`, `check/out/`, `inbox/`, `artifacts/` |
| Volume `wheels` | `resources/uc.yml` | created | the two wheels + `requirements.txt` for `%pip install`; only admins write it |
| Job `kpi_wheels` | `resources/jobs.yml` | manual | copies the deployed wheels into the wheels volume |
| Job `kpi_check` | `resources/jobs.yml` | schedule PAUSED | optional scheduled checks: the notebook on a reports folder + KPI table |
| Job `kpi_bootstrap` | `resources/jobs.yml` | manual, optional | service tables (only for `persist=True` / `kpi_pipeline`), governed views, demo platform |
| Job `kpi_pipeline` | `resources/jobs.yml` | manual, optional | batch pipeline over `inbox/`; no file-arrival trigger, no publication |
| Job `kpi_dq_and_eval` | `resources/jobs.yml` | schedule PAUSED | DQ checks on governed views; evaluation on the synthetic set |
| Schema `platform_demo` (dev target only) | `databricks.yml` `targets.dev.resources` | dev only | synthetic platform base tables for the demo |
| **Parked**: review app, Genie agent, AI/BI dashboard | `resources/parked/` | **not deployed** | publication and consumption, switched on after the insight_harness hand-off (section 11) |
| **Optional configuration**: wheel variant of `kpi_check`, file-arrival trigger, parked variables, bootstrap parameters and publication tasks | `resources/parked/optional.yml` | **not deployed** | ready-to-insert YAML blocks (section 3) |

Tables and views are not bundle resources. A notebook check needs none: its working state is an
in-memory DuckDB and its SQL reads the tables named in the KPI table.

## 1. Quickest path: two wheels, one Volume, one notebook (no bundle)

With a release folder (`release/kpi_evidence_<version>/`, built by `python scripts/build_release.py`;
see `RELEASE.md`), upload the folder to a Volume and follow its `INSTALL.md`: it carries both wheels, an
offline wheelhouse, the notebook and the KPI table template. Without a release folder:

1. **Build the wheels** on a machine with this repository and `uv` (or pip):

   ```bash
   python scripts/publish_wheels.py build --volume-dir /Volumes/<catalog>/<schema>/<volume>/wheels
   ```

   This writes `build/notebook_wheels/` with `reconloop-0.5.0-py3-none-any.whl`,
   `kpi_evidence-0.1.0-py3-none-any.whl` and a `requirements.txt` that lists their absolute Volume
   paths, and prints the upload commands. In Git Bash on Windows prefix the command with
   `MSYS_NO_PATHCONV=1` (Git Bash rewrites `/Volumes/...` arguments; the script detects that and stops).

2. **Upload the three files** to that Volume folder - Catalog Explorer ("Upload to this volume"), or:

   ```bash
   databricks fs cp --overwrite build/notebook_wheels/reconloop-0.5.0-py3-none-any.whl dbfs:/Volumes/<catalog>/<schema>/<volume>/wheels/
   databricks fs cp --overwrite build/notebook_wheels/kpi_evidence-0.1.0-py3-none-any.whl dbfs:/Volumes/<catalog>/<schema>/<volume>/wheels/
   databricks fs cp --overwrite build/notebook_wheels/requirements.txt dbfs:/Volumes/<catalog>/<schema>/<volume>/wheels/
   ```

   Use a Volume that only you (or your admins) can write: whoever can replace those files decides
   what code your notebook runs.

3. **Install in a notebook** (serverless notebook compute; first cell), then restart Python:

   ```python
   %pip install -r /Volumes/<catalog>/<schema>/<volume>/wheels/requirements.txt
   ```
   ```python
   dbutils.library.restartPython()
   ```

   (Without `requirements.txt`: `%pip install <folder>/reconloop-0.5.0-py3-none-any.whl
   <folder>/kpi_evidence-0.1.0-py3-none-any.whl`.) The wheels' dependencies (pandas, duckdb, pyyaml,
   openpyxl, defusedxml, pdfplumber, databricks-sdk) come from PyPI or the workspace's default package
   repository.

4. **Run the six lines** (`docs/NOTEBOOK.md` section 1):

   ```python
   import kpi_evidence as ke
   check = ke.ReportCheck(spark)                                  # KPI SQL runs on live tables via spark
   check.add_reports("/Volumes/main/brand/reports/2026/")         # files, folders, globs
   check.set_kpis("/Volumes/main/brand/kpi_table.xlsx")           # optional KPI table (section 5)
   result = check.run()
   result.show()                                                  # visual report inline
   ```

   Then `display(result.comparisons)`, `result.save_html("/Volumes/.../kpi_check.html")`,
   `result.save_excel("/Volumes/.../kpi_check.xlsx")` and `result.save_sql("/Volumes/.../sql")` (output
   paths accept `dbfs:/Volumes/...` too). Or import `notebooks/check_reports.py`, which
   does all of this with widgets (`notebooks/README.md`).

The notebook user needs `SELECT` on the tables the KPI table names, and nothing is written to tables
unless `persist=True` (section 6).

## 2. Bundle path: prerequisites

* **Databricks CLI >= 1.3.0** (direct deployment engine). The docs current on 2026-10-19 are for 1.21.0;
  CLI 1.20.0 removed the Terraform engine, which this bundle does not use. The CLI is not installed in
  this repository's environment, so `databricks bundle validate` has not run here: run it on a machine
  with the CLI (section 4). `tests/deploy` checks what can be checked offline.
* **uv** (the artifacts run `uv build --wheel`) and Python >= 3.11 on the deploying machine.
* Workspace features: Unity Catalog, serverless compute for notebooks and jobs, and a pro or serverless
  SQL warehouse for the SQL tasks of `kpi_bootstrap` and `kpi_pipeline` (the bundle looks the warehouse
  up by name at deploy time, so it must exist even if you never run those jobs).
* Your permissions: `USE CATALOG` + `CREATE SCHEMA` on the target catalog, `CAN USE` on the warehouse,
  and the right to create jobs.
* Groups (prod): `kpi-evidence-admins` and `kpi-evidence-users` (variables `admin_group`, `user_group`).
  The dev target uses the built-in `admins` and `users` groups, so a dev deploy needs no group setup.

## 3. Bundle path: configure

Variables (`databricks.yml`); set them per target, with `--var name=value`, or with `BUNDLE_VAR_name`:

| Variable | Default | Notes |
|---|---|---|
| `catalog` | dev: `main`; prod: required | catalog for both schemas and the volumes |
| `schema` / `gov_schema` | `kpi_evidence` / `kpi_gov` | dev mode prefixes schema names per user; everything references `${resources.schemas.*.name}` |
| `platform_schema` | `platform_demo` | demo only: the bundled governed views read `${platform}`; ported views name the real tables and do not use it |
| `warehouse_name` | `Serverless Starter Warehouse` | `warehouse_id` is looked up from it (SQL tasks only) |
| `ai_mode` | `off` | for the pipeline jobs: `off` / `ai_query` / `gateway` (section 10) |
| `ai_model` | `system.ai.claude-sonnet-4-6` | used only when `ai_mode=ai_query`; fallback `system.ai.gpt-5-4-mini` |
| `notify_email` | the deploying user | prod: owners' list |
| `admin_group` / `user_group` | `kpi-evidence-admins` / `kpi-evidence-users` (dev: `admins` / `users`) | grants in section 6 |
| `load_demo` | `false` (dev: `true`) | `kpi_bootstrap` loads the synthetic platform; use `false` once the governed views are ported (section 12) |
| `prod_run_as_sp` | placeholder | application id of the prod service principal |

The parked variables (`app_name`, `reviewer_group`, `consumer_group`, `genie_title`,
`genie_description`) are not in `databricks.yml`: they are the block `parked_variables` of
`resources/parked/optional.yml`, and their dev values the block `parked_dev_variables`.

### Optional configuration blocks: `resources/parked/optional.yml`

The bundle YAML (`databricks.yml` and `resources/*.yml`) carries no comments, so configuration that is
ready but switched off is not kept there as commented-out YAML. It lives in
`resources/parked/optional.yml`, which the `resources/*.yml` include does not reach (`*` never crosses
`/`), so nothing in it is deployed. Each top-level key is one block with three fields:

* `target`: the bundle file and the YAML location the block belongs to, written
  `<file>: <dotted location>`. A step such as `tasks[task_key=bootstrap]` selects the list item whose
  `task_key` is `bootstrap`.
* `how`: `append to the list` (add the items at the end of the list at `target`) or `merge into the
  mapping` (add the keys to the mapping at `target`; a key that already exists there is replaced, which
  only `check_wheel_variant` does, for `tasks`).
* `content`: the YAML exactly as it reads once inserted; only its indentation changes to fit the target.

| Block | Target | How | Details |
|---|---|---|---|
| `parked_variables` | `databricks.yml: variables` | merge | parked app, Genie agent and dashboard variables (section 11) |
| `parked_dev_variables` | `databricks.yml: targets.dev.variables` | merge | dev values for them: one app per developer (`kpi-evidence-review-dev`), built-in `users` group, no group setup |
| `check_wheel_variant` | `resources/jobs.yml: resources.jobs.kpi_check` | merge, replacing the notebook `tasks` | `python_wheel_task` variant of `kpi_check` (section 7) |
| `parked_bootstrap_parameters` | `resources/jobs.yml: resources.jobs.kpi_bootstrap.tasks[task_key=bootstrap].python_wheel_task.named_parameters` | merge | grants to the app service principal and groups, Genie placeholders (sections 8, 11) |
| `file_arrival_trigger` | `resources/jobs.yml: resources.jobs.kpi_pipeline` | merge | file-arrival trigger on `inbox/` (section 9) |
| `parked_publication_tasks` | `resources/jobs.yml: resources.jobs.kpi_pipeline.tasks` | append | tasks `publish` and `refresh_dashboard` (section 11) |

To switch a block on, insert its `content` at its `target`, delete the block from `optional.yml` and run
`databricks bundle validate`. `tests/deploy` checks every block against the live YAML (the target
exists, merged keys do not clash, entry points and referenced files exist, and the jobs stay valid with
all blocks applied); it also pins the default, switched-off configuration, so update it in the same
change.

## 4. Bundle path: validate, deploy, publish the wheels, open the notebook

```bash
databricks auth login --host https://<workspace-host>      # or DATABRICKS_HOST / a profile
databricks bundle validate -t dev                          # warns on unknown keys: fix every warning
databricks bundle deploy   -t dev                          # builds and uploads both wheels, syncs the notebook
databricks bundle run kpi_wheels -t dev                    # copies the wheels into the wheels volume
```

`kpi_wheels` runs `scripts/publish_wheels.py copy`: it picks the newest `kpi_evidence` and `reconloop`
wheel under `${workspace.artifact_path}/.internal/` (where `bundle deploy` uploads them, with a dynamic
version such as `0.1.0+<timestamp>`), copies them to `/Volumes/<catalog>/<schema>/wheels/`, writes
`requirements.txt` with their absolute paths and then removes older copies. **Run it after every deploy
that changes Python code**; notebooks and `kpi_check` keep using the previous wheels until you do.
Fallback without the job: the `databricks fs cp` commands of section 1, against the wheels volume.

Then open the notebook: `<root_path>/files/notebooks/check_reports` (dev:
`/Workspace/Users/<you>/.bundle/kpi-evidence-service/dev/files/notebooks/check_reports`), attach
serverless compute, set the widgets and run all (`notebooks/README.md`). The default `wheels` widget
value for the bundle is `/Volumes/<catalog>/<schema>/wheels/requirements.txt` (in dev the schema name
carries the dev prefix; the job passes the right value).

The direct engine resets any field you remove from YAML to the platform default on the next deploy, so
change settings in YAML rather than in the UI. For prod, review a plan first:

```bash
databricks bundle plan   -t prod --var catalog=<catalog> --var prod_run_as_sp=<application-id> -o json > plan.json
databricks bundle deploy -t prod --var catalog=<catalog> --var prod_run_as_sp=<application-id> --plan plan.json
databricks bundle run kpi_wheels -t prod
```

## 5. The KPI table (optional hints, validated against the reports)

The **KPI table** tells the check where a KPI might come from. Each row is a *candidate* - tables, an
SQL expression, a date column, filters - and the run validates it: it compiles the hint to SQL, computes
the value for every period a report prints, and compares. The comparison report then says, per KPI,
whether the hint reproduces the reports (`validated`, `partly validated`, `matches alternative`,
`not validated`, `not computable`, `report only`, `not compared`). A hint is never trusted because it
was supplied.

Columns (full reference in `samples/KPI_TABLE.md`): `kpi` (required), `aliases`, `table` (one or more
fully qualified candidates, `;`-separated; first = primary), `value_sql` or `numerator` + `denominator`,
`date_column`, `date_format`, `filters`, `brand_column`, `scope_columns`, `unit`, `grain`, `tolerance`,
`alternatives`, `brand`, `priority`, `notes`, `owner`. A row without `table` is report-only (values are
captured). Example: `samples/kpi_table_example.csv`.

Where to put it:

* **Interactive notebook**: anywhere the notebook can read - a CSV/XLSX in a Volume
  (`check.set_kpis("/Volumes/.../kpi_table.xlsx")`), a Delta table (`check.set_kpis(spark.table("..."))`),
  a pandas DataFrame or a list of dicts.
* **`kpi_check` job**: a CSV/XLSX path in the `kpis` parameter. The conventional place is
  `/Volumes/<catalog>/<schema>/reports/check/kpi_table.csv`; empty means no KPI table (the report is
  then an inventory of the extracted values).

The hint SQL fragments are user code. They are validated (no `;`, no comments, no DDL/DML keywords,
dotted table names) and run with the permissions of whoever runs the check: the notebook user, or the
job's run-as identity for `kpi_check`. Keep a KPI table used by the job in a location only its owners can
write (section 6).

## 6. Permissions

**Nothing is written unless you ask.** `ReportCheck(spark)` keeps its working state in an in-memory
DuckDB; nothing is written to Unity Catalog tables or volumes except the files you save with
`save_html`, `save_excel` and `save_sql`, and the state file when you pass `state_path`. With `persist=True` the state goes to Delta service tables in
`<catalog>.<schema>` and the SQL exports to `<reports volume>/artifacts/sql/` (run `kpi_bootstrap`
first; it creates the tables).

| Who | Needs |
|---|---|
| Notebook user | serverless notebook compute; `USE CATALOG` + `USE SCHEMA` + **`SELECT` on every table or view the KPI table names** (or the governed views in registry mode); `READ VOLUME` where the reports are; `WRITE VOLUME` where the HTML / Excel files go; `READ VOLUME` on the wheels volume |
| ... with `persist=True` | also `SELECT`, `MODIFY` on the service schema (and the tables must exist: `kpi_bootstrap`) |
| `kpi_check` job identity (dev: the deployer; prod: `prod_run_as_sp`) | the same as a notebook user, for the tables in the job's KPI table; it owns the schemas and volumes |
| `kpi_wheels` job identity | read `${workspace.artifact_path}`, write the wheels volume (it owns both) |

The bundle grants `user_group` (resources/uc.yml): `USE_SCHEMA, SELECT, MODIFY` on the service schema
(only used with `persist=True`), `USE_SCHEMA, SELECT, EXECUTE` on the governed schema, `READ_VOLUME,
WRITE_VOLUME` on the reports volume and **`READ_VOLUME` only** on the wheels volume (the code that
notebooks and the job identity execute). `USE CATALOG` is not bundle-managed: the catalog owner grants it.
Users get `CAN_VIEW` on every job.

A missing `SELECT` is not silent: the affected KPIs are flagged `MISSING_ACCESS` (or `PLATFORM_DATA_ISSUE`
when Unity Catalog reports the table as not found; section 13) and their verdict is "not computable".

**The job identity is not the user.** `kpi_check` runs its KPI table's SQL with the run-as identity's
grants. Do not give `CAN_MANAGE_RUN` on it, or write access to its KPI table, to people who must not read
what that identity reads; give the prod service principal only the `SELECT` grants that every reader of
the reports may see. Anyone with `MODIFY` on the service schema can change shared `persist=True` state.

## 7. Scheduled checks: the `kpi_check` job

`kpi_check` runs `notebooks/check_reports.py` as a **notebook task on serverless compute**, with a
**paused** weekly schedule (Mondays 07:00 UTC). Unpause it in the UI, or set `pause_status: UNPAUSED` in
`resources/jobs.yml`. Run it now:

```bash
databricks bundle run kpi_check -t dev --params reports=/Volumes/<c>/<s>/reports/check/reports/,kpis=/Volumes/<c>/<s>/reports/check/kpi_table.csv
```

Job parameters (pushed down to the notebook's widgets; `base_parameters` spells out the same mapping):
`reports` (folder, file or glob), `kpis` (KPI table path; empty = none), `out_dir` (folder for the HTML
and Excel report), `brand` (empty = from the KPI table or the report metadata), `catalog`, `gov_schema`,
`ai_mode` (`off` or `ai_query`; default `off`), `wheels` (the `requirements.txt` the notebook installs).
The notebook's `focus`, `max_priority` and `state_path` widgets are not job parameters, so a job run keeps
their empty defaults. The run page shows the rendered report inline; the files land in `out_dir` as
`kpi_check_<UTC timestamp>.html`, `.xlsx` and the `kpi_check_<UTC timestamp>_sql/` folder.

**Why a notebook task is the default**, and not the `python_wheel_task` variant (block
`check_wheel_variant` in `resources/parked/optional.yml`, entry point `kpi-evidence-check` = `kpi_evidence.cli:check_entry`):

* a scheduled run executes exactly what a person runs by hand - one code path, one set of widgets;
* the run page shows the visual report inline, so a reviewer opens the run and sees it;
* the widgets are the parameters ("Run now with different parameters" works with no new contract).

The cost: a notebook cannot use a job environment, so it installs the wheels from the wheels volume and
depends on `kpi_wheels` having run after the last deploy. The wheel variant installs the bundle-built
wheels directly in a serverless job environment (no volume copy, no notebook) and writes the same HTML
and Excel files (no SQL folder); switch to it (merge the block into `resources.jobs.kpi_check` of `resources/jobs.yml`: it adds the
job environment and replaces the notebook `tasks`, keeping the task key `check` so the run history
stays readable) when nobody needs the inline report. Its entry point is the console script
`kpi-evidence-check` (`kpi_evidence.cli:check_entry`).

Identity: the job runs as its `run_as` identity (the deployer in dev, the service principal in prod), and
the KPI table's SQL runs with that identity's grants. Keep the KPI table where only owners can write, and
give `CAN_MANAGE_RUN` only to people who may read what the job identity reads (section 6). The task has
`max_retries: 0`: a failed check is looked at, not retried.

## 8. Optional: `kpi_bootstrap`

Not needed for notebook checks. Run it only for `persist=True`, for `kpi_pipeline`, or for the demo:

```bash
databricks bundle run kpi_bootstrap -t dev                              # dev: load_demo=true
databricks bundle run kpi_bootstrap -t dev --params seed_samples=true   # also copy the sample reports into inbox/
```

Idempotent. The `kpi-evidence-bootstrap` entry point creates the service tables (`CREATE TABLE IF NOT EXISTS`,
generated from `kpi_evidence.contracts`), loads the synthetic platform (`load_demo=true`), creates the
governed views (`src/sql/governed_views.sql`), the service views and `src/sql/jobs/consumer_views.sql`,
mirrors the registry YAML into `kpi_registry`, `brands`, `kpi_goal_list`, `report_types`, and creates
`inbox/` and `artifacts/` in the reports volume. From `src/sql/jobs/grants.sql` it runs only the
statements whose principal it receives, and it fills the Genie agent's placeholders only with a
`genie_space_id`; the default job passes neither (block `parked_bootstrap_parameters` in
`resources/parked/optional.yml`, section 11), so it grants nothing and leaves Genie alone. Its second task, `verify_list_ready`, runs the pipeline's SQL work-list query against the new
`documents` table (first-use test of the SQL-task parameter syntax, section 13).

With the demo loaded (`load_demo=true`), `ke.ReportCheck(spark, registry=..., gov_schema=...)` can check
the sample reports against `platform_demo` through the governed views. After the governed views are ported
to real tables (section 12), run `kpi_bootstrap` with `--params load_demo=false`.

## 9. Optional: `kpi_pipeline` (manual)

The batch pipeline (`register -> list_ready -> extract (for each document) -> process`) processes files in
`/Volumes/<catalog>/<schema>/reports/inbox/` into the service tables (`kpi_results_current`,
`sql_artifacts`, ...). It needs `kpi_bootstrap` first and runs only when started:

```bash
databricks bundle run kpi_pipeline -t dev
databricks bundle run kpi_pipeline -t dev --params ai_mode=off,max_docs=50
```

There is **no file-arrival trigger** and **no publication** by default. To react to dropped files, merge
the block `file_arrival_trigger` of `resources/parked/optional.yml` into `resources.jobs.kpi_pipeline` in
`resources/jobs.yml` (it sets `pause_status: UNPAUSED` because development mode pauses triggers that do
not set it). Only new file names fire the trigger; without file events on the storage, at most 50
file-arrival jobs per workspace and 10,000 files under the path. The publication tasks are the block
`parked_publication_tasks` of the same file (section 11).

## 10. AI options (off by default)

Every deterministic path works with AI off: registry synonyms, KPI-table aliases, approved mapping memory,
native PPTX / XLSX / PDF extraction. Image-only slides and scans then stay `UNCERTAIN_EXTRACTION`.

| Mode | What runs |
|---|---|
| `off` (default) | deterministic paths only; no model proposals |
| `ai_query` | `ai_query` with `responseFormat` against the `system.ai` model in `ai_model` (mapping and metadata proposals), `ai_parse_document` for scans and images |
| `gateway` | OpenAI-compatible calls through a Unity Gateway service (for external providers, which `ai_query` cannot call); needs `Settings.ai_gateway_base_url` |

* Notebook: `ke.ReportCheck(spark, ai_mode="ai_query")`. The model comes from `Settings.ai_model`
  (environment variable `KPI_EVIDENCE_AI_MODEL`, default `system.ai.claude-sonnet-4-6`).
* Pipeline jobs: `databricks bundle deploy --var ai_mode=ai_query` (all runs) or
  `bundle run kpi_pipeline --params ai_mode=ai_query` (one run).
* A model proposal is never a verdict: it is shown as model-derived and needs a deterministic match or a
  person.

Before switching AI on:

1. **Partner-powered AI features: check before 2026-11-01.** The workspace toggle disappears on
   2026-11-01; each workspace keeps its current value and only the account team can change it afterwards.
   It may gate partner models (Claude, GPT) in `ai_query` (unverified) and it gates Genie agents. Check it
   in the admin settings now, even with AI off.
2. **Unity Gateway enforcement** (on by default for new workspaces): the legacy `databricks-*`
   pay-per-token endpoints return `PERMISSION_DENIED`; use `system.ai.*` names only. Check:
   `SELECT ai_query('system.ai.claude-sonnet-4-6', 'Reply with OK')`.
3. **Region**: `ai_parse_document`, `ai_query` and the chosen model must be available in the workspace
   region (Gemini needs cross-geo routing).
4. **`responseFormat` with Claude**: Claude 5.5-family models return HTTP 400 on `response_format`
   through Chat Completions; whether `ai_query(..., responseFormat => ...)` behaves the same is
   unverified. If proposals fail, use `system.ai.gpt-5-4-mini`.

## 11. Parked pieces and how to switch them on

Parked on 2026-10-19 until a hand-off to insight_harness is agreed: check results are not piped into
the app automatically. Nothing below is deployed; `tests/deploy` keeps the parked files consistent so
switching on stays a file move.

| Piece | Where it is parked |
|---|---|
| Review / publication app (vendored insight_harness + evidence pages; `MODEL_SERVICE` binding; app permissions) | `resources/parked/app.yml.disabled`, `resources/parked/insight_harness.app.yaml` (was `apps/insight_harness/app.yaml`), `scripts/parked/build_app_bundle.py`, `docs/UX_app_parked.md` |
| Genie agent and AI/BI dashboard over published values | `resources/parked/consume.yml.disabled` (JSON in `src/genie/`, `src/dashboards/`) |
| Auto-publication | `registry/policy.yaml` (`auto_publish` off); tasks `publish` + `refresh_dashboard` in block `parked_publication_tasks` of `resources/parked/optional.yml` |
| Grants to the app service principal and consumer groups; Genie placeholder substitution | `src/sql/jobs/grants.sql`; block `parked_bootstrap_parameters` in `resources/parked/optional.yml` |
| Variables | blocks `parked_variables` and `parked_dev_variables` in `resources/parked/optional.yml` |

Switching on, in order (also in `resources/parked/README.md`):

1. Agree the hand-off: which values publish (policy), who reviews, what insight_harness reads.
2. Move `resources/parked/app.yml.disabled` to `resources/app.yml` and `consume.yml.disabled` to
   `resources/consume.yml` (their relative paths assume `resources/`); move
   `resources/parked/insight_harness.app.yaml` back to `apps/insight_harness/app.yaml`.
3. `databricks.yml`: merge the block `parked_variables` of `resources/parked/optional.yml` into
   `variables` and `parked_dev_variables` into `targets.dev.variables`; in `sync`, remove the `apps/**`
   exclude and add `apps/insight_harness/vendor_wheels/*.whl` to `include`.
4. `resources/jobs.yml`: merge `parked_bootstrap_parameters` into the bootstrap task's
   `named_parameters` and append `parked_publication_tasks` to `kpi_pipeline.tasks` (both from
   `resources/parked/optional.yml`); merge `file_arrival_trigger` too if the app's upload page should
   start the pipeline. Delete each inserted block from `optional.yml` (section 3). The `publish` task
   keeps `max_retries: 0` and `disable_auto_optimization: true` so it runs at most once per run:
   serverless auto-optimization could otherwise retry it beyond the retry policy.
5. `registry/policy.yaml`: set the agreed `auto_publish` switches.
6. `python scripts/parked/build_app_bundle.py --install` (the App installs the two wheels from
   `apps/insight_harness/vendor_wheels/`; pins for its Python 3.11 runtime are explained in the script).
7. `databricks bundle validate`, `deploy`, `bundle run kpi_bootstrap` (grants and Genie placeholders),
   `bundle run kpi_review` (deploy does not start the app).

First-deploy checks that apply only to the parked pieces: Genie agent creation before the tables exist
(if it fails: deploy without `genie_spaces`, run bootstrap, restore); `${resources.genie_spaces.kpi_published.id}`
as the space id reference; the app's `MODEL_SERVICE` `uc_securable` binding (Public Preview; fallback:
grant `EXECUTE` manually); `config.env` replacing vs merging with `app.yaml` env; relative
`./vendor_wheels/...` paths in the App's `requirements.txt`; `delta.feature.catalogManaged` transactions
for publish (`BEGIN ATOMIC` is used only with `settings.extra["atomic"] = True` and only for blocks that write
catalog-managed tables; otherwise every write runs as its own idempotent statement); dashboard widget spec versions; the on-behalf-of-user Genie client
(`auth_type="pat"`); the Apps upload size limit. A Medium app costs about 0.5 DBU per hour while running.

## 12. Using the registry and governed views (instead of, or with, a KPI table)

Real use is usually KPI table only. The governed path stays available:

1. **Governed views** (`src/sql/governed_views.sql`): replace each `FROM ${platform}.<table>` with the real
   tables and the literal `'KESTREL' AS brand` with the real brand column; keep the column contract
   (brand, month, month_start, month_end, scope dimensions, measures). This file is the only SQL porting
   seam. `kpi_bootstrap` (with `load_demo=false`) creates the views in `kpi_gov`. The demo reads the same
   file, so a port replaces the demo views; make it in a deployment branch. The full procedure (column
   contract, views without a real source, local check, tests that no longer apply) is in
   `docs/ARCHITECTURE.md` section 6.4.
2. **Registry** (`registry/`): `kpis/*.yaml` (definitions, synonyms, tolerances, `platform` blocks),
   `brands.yaml`, `goal_list.yaml`, `report_types.yaml`, `policy.yaml`. In a notebook pass it explicitly:
   `ke.ReportCheck(spark, registry="<path or Registry>")` (with spark, `registry="auto"` means none). A
   KPI-table row with an existing registry id overrides that KPI's platform definition.
3. **Access**: grant the identity that runs the checks `SELECT` on the tables behind the governed views
   (and `USE_SCHEMA, SELECT, EXECUTE` on `kpi_gov`, which the bundle grants to `user_group`).

## 13. Unverified items: test these on first use

| # | Item | How to test | If it fails |
|---|---|---|---|
| 1 | `${workspace.artifact_path}/.internal/` is where `bundle deploy` uploads the artifact wheels, and the path resolves with or without the `/Workspace` prefix | `bundle run kpi_wheels`; it lists the wheels it found | pass `--params source=<folder>` (see the job's error message and the workspace browser), or use the `databricks fs cp` fallback |
| 2 | Newest-wheel selection by modification time on the workspace file system | `kpi_wheels` output names the copied wheels; compare with the deploy's version | delete old wheels under `.internal/` and re-run |
| 3 | Writing and deleting files in a Volume from a serverless Python script task (FUSE) | `kpi_wheels` | copy with `databricks fs cp` (section 1) |
| 4 | The notebook's install cell `%pip install --quiet -r $wheels_requirements` (a Python variable expanded inside `%pip`) on serverless notebook compute, and PyPI access for the dependencies | run the notebook's first cells | write the path literally in the `%pip` cell; workspace default package repository (or `--index-url` in the requirements file) |
| 5 | `reconloop==0.5.0` (pinned by kpi_evidence) accepts the dynamic version `0.5.0+<timestamp>` (PEP 440 ignores the local label for `==`) | the `%pip install` above | pin `reconloop>=0.5.0` in `pyproject.toml` |
| 6 | Notebook task with job parameters and `base_parameters` of the same names (job parameters win; values are identical by design) | `bundle run kpi_check --params ...`; check the widget values in the run | drop `base_parameters` (pushdown alone suffices) |
| 7 | `notebooks/check_reports.py` synced as a notebook (`# Databricks notebook source` header) and found by `notebook_path: ../notebooks/check_reports.py` | `bundle deploy`; open `<root_path>/files/notebooks/` | import it manually (`notebooks/README.md`) and point `notebook_path` at the workspace path |
| 8 | Bundle `grants` on schemas and volumes: authoritative (removes grants made elsewhere) or additive under the direct engine; volume privilege names `READ_VOLUME` / `WRITE_VOLUME` | `bundle plan -o json`; `SHOW GRANTS ON VOLUME ...` after deploy | grant manually and remove the `grants` block; relevant again when `grants.sql` runs for the parked app |
| 9 | Missing grant reported as permission denied vs not found (`MISSING_ACCESS` vs `PLATFORM_DATA_ISSUE`) | a KPI-table row on a table you cannot read | adjust `platform.classify_error` |
| 10 | Excel output saved to a Volume path. Volumes do not support random (non-sequential) writes such as zip and Excel files, so `save_excel` writes the workbook to a local temporary file with openpyxl and copies it to the target sequentially | `result.save_excel("/Volumes/...")`, and the notebook's last cell | `result.save_excel("/tmp/kpi_check.xlsx")`, then `dbutils.fs.cp("file:/tmp/kpi_check.xlsx", "/Volumes/...")` |
| 11 | `ai_query(..., responseFormat => ...)` with `system.ai.claude-sonnet-4-6` | `ReportCheck(spark, ai_mode="ai_query")` on the samples | `system.ai.gpt-5-4-mini` |
| 12 | Partner-powered AI setting gating `system.ai` partner models in `ai_query` | `SELECT ai_query(...)` on a warehouse | AI off or a Databricks-hosted model |
| 13 | SQL-task parameter syntax `{{catalog}}` inside `IDENTIFIER()` (`list_ready_documents.sql`) | `kpi_bootstrap.verify_list_ready` | replace each `{{name}}` with `:name`; no YAML change |
| 14 | Dev-mode schema prefix and the dev override of the bootstrap's `platform_schema` (task merge by `task_key`) | `bundle validate -t dev -o json` | pass `platform_schema` at run time |
| 15 | `{{job.trigger.file_arrival.location}}` on manual runs (empty or literal text) - manual is now the default | `bundle run kpi_pipeline` | `register` treats empty and `{{...}}` as "scan the whole inbox" |
| 16 | For each over an empty `output.rows` | `bundle run kpi_pipeline` with an empty inbox | `process` runs with `ALL_DONE` either way |
| 17 | File-arrival URL on `inbox/` before the folder exists (only after adding the trigger block) | deploy with the trigger | run `kpi_bootstrap` (creates `inbox/`) and deploy again |
| 18 | Variable lookup that references another variable (`warehouse: ${var.warehouse_name}`) | `bundle validate` | put the warehouse name literally in the lookup |
| 19 | Experiment path `${workspace.root_path}/kpi-evidence-mlflow` | deploy | use a Shared folder path |
| 20 | `sync.include` of `notebooks/*.py` when the glob matches (harmless) and `exclude: apps/**` not affecting the artifact build (the wheel build reads `apps/insight_harness/data/*.csv` locally) | `bundle deploy`; `import kpi_evidence` in the notebook and `ke.ReportCheck.demo()` | remove the exclude |

## 14. Job parameters (CLI contract)

All wheel tasks call `kpi_evidence.cli` entry points (console scripts named `kpi-evidence-<stage>`) with
`--key=value` arguments. The parked publication task uses `kpi-evidence-publish`, which is not a console
script until publication is switched on: add `kpi-evidence-publish = "kpi_evidence.cli:publish_entry"` to
`[project.scripts]` in `pyproject.toml` then. Job parameters are
also pushed down to every key-value task, so the CLI parses with `argparse.parse_known_args()`.

| Entry point | Job | Named parameters |
|---|---|---|
| pipeline stages (all below except `kpi-evidence-check`) | | `backend` (`databricks`), `catalog`, `schema`, `gov_schema`, `volume_path` (`/Volumes/<c>/<s>/reports`), `run_id` (`{{job.run_id}}`) |
| `kpi-evidence-bootstrap` | `kpi_bootstrap` | + `platform_schema`, `load_demo`, `seed_samples` (`true`/`false`). Parked (block `parked_bootstrap_parameters` in `resources/parked/optional.yml`): `app_sp` (app SP application id), `admin_group`, `reviewer_group`, `consumer_group`, `genie_space_id`. **Statements of `grants.sql` whose principal is not passed are skipped (the default passes none); without `genie_space_id` the Genie substitution is skipped.** |
| `kpi-evidence-register` | `kpi_pipeline` | + `ai_mode`, `ai_model`, `trigger_location` (`{{job.trigger.file_arrival.location}}`; empty on manual runs) |
| `kpi-evidence-extract` | `kpi_pipeline` (for each) | + `ai_mode`, `ai_model`, `doc_id` (`{{input.doc_id}}`) |
| `kpi-evidence-process` | `kpi_pipeline` | + `ai_mode`, `ai_model` |
| `kpi-evidence-publish` (parked; not a console script yet) | `kpi_pipeline` (parked block `parked_publication_tasks`) | + `ai_mode`, `ai_model` |
| `kpi-evidence-dq` | `kpi_dq_and_eval` | + `ai_mode`, `ai_model` |
| `kpi-evidence-evaluate` | `kpi_dq_and_eval` | + `ai_mode`, `ai_model`, `mlflow_experiment` (experiment id) |
| `kpi-evidence-check` = `kpi_evidence.cli:check_entry` | `kpi_check`, wheel variant (block `check_wheel_variant`) | `reports` (`--reports`: files or folders, comma-separated, Volume paths), `kpis` (`--kpis`: KPI table CSV/XLSX path; empty = none), `out_dir` (`--out_dir`: folder for `kpi_check_<UTC timestamp>.html` / `.xlsx`), `catalog` (`--catalog`), `gov_schema` (`--gov_schema`), `brand` (`--brand`: empty = from the KPI table or metadata), `ai_mode` (`--ai_mode`: `off` / `ai_query`), `run_id` (`{{job.run_id}}`). It also accepts `--strict_metadata=true` (skip reports whose metadata nobody confirmed), which the job does not pass. Not a pipeline stage: no `backend` / `schema` / `volume_path` (state stays in memory) |

Tasks that are not wheel tasks:

| Job | Task | Parameters |
|---|---|---|
| `kpi_wheels` | `spark_python_task` `scripts/publish_wheels.py` | `copy --source {{job.parameters.source}} --dest {{job.parameters.dest}}`; defaults `${workspace.artifact_path}/.internal` and `/Volumes/<c>/<s>/wheels` |
| `kpi_check` | `notebook_task` `notebooks/check_reports.py` | widgets `reports`, `kpis`, `out_dir`, `brand`, `catalog`, `gov_schema`, `ai_mode`, `wheels` (job parameters of the same names; `base_parameters` maps them explicitly) |

Job parameters that can be overridden per run: `catalog`, `schema`, `gov_schema`, `volume_path`, `ai_mode`,
`ai_model`, `max_docs` (pipeline; default 200, capped at 500 by the SQL), `load_demo`, `seed_samples`
(bootstrap), the eight `kpi_check` parameters, `source` / `dest` (`kpi_wheels`). Anyone with
`CAN_MANAGE_RUN` can override them: they are not a security control.

## 15. Cost and monitoring

* Notebook checks bill serverless notebook compute while the notebook runs. Nothing runs on a schedule
  until someone unpauses `kpi_check` or `kpi_dq_and_eval`.
* `kpi_check` and `kpi_pipeline` use `performance_target: STANDARD` (cheaper, 4-6 minutes start-up);
  `kpi_wheels` uses `PERFORMANCE_OPTIMIZED` (a one-minute copy someone waits for).
* Jobs carry tags (`project`, `component`, `target`) and pass `{{job.run_id}}` to the stages, so runs join
  to billing: `system.billing.usage` where `usage_metadata.job_run_id` = the run id. AI functions bill
  under `billing_origin_product = 'AI_FUNCTIONS'` (only with `ai_mode=ai_query`).
* No app is deployed, so there is no always-on cost.
* Failure and long-run emails go to `notify_email`.

## 16. Troubleshooting

* **`ModuleNotFoundError: kpi_evidence` in the notebook**: the install cell did not run, Python was not
  restarted (`dbutils.library.restartPython()`), or the wheels volume is empty - run `kpi_wheels`.
* **The notebook runs old code after a deploy**: `kpi_wheels` was not run; or the same version was already
  installed in this session (quickest path: wheels built without a dynamic version) - detach and
  reattach, or `%pip install --force-reinstall --no-deps <the two wheels>`.
* **`kpi_wheels` fails with "no wheel for [...]"**: deploy first; the message lists the wheels it found
  and where it looked (section 13 item 1).
* **Several kpi_evidence wheels in `dist/`**: delete `dist/` and deploy again so the environment glob
  matches one wheel.
* **A KPI is "not computable"**: the comparison row shows the SQL error. `MISSING_ACCESS`: grant `SELECT`
  to whoever ran the check (section 6). `PLATFORM_DATA_ISSUE` (object missing): check the table name in
  the KPI table (fully qualified, `catalog.schema.table`).
* **The `kpi_check` run fails on a widget**: the job passes `reports`, `kpis`, `out_dir`, `brand`,
  `catalog`, `gov_schema`, `ai_mode`, `wheels`; the notebook must define each (tests/deploy checks it).
* **`save_excel` fails on a Volume path**: save to a local file and copy it (section 13 item 10).
* **`bundle deploy` fails on the warehouse lookup**: set `--var warehouse_name=<an existing warehouse>`
  (needed by the SQL tasks of the optional jobs).

## 17. Notes on the bundle YAML

The bundle files carry no comments (`tests/deploy` checks it). The notes that explain individual
settings, and that are not covered by the sections above, are kept here.

`databricks.yml`:

* `include: resources/*.yml` does not reach `resources/parked/` because `*` never crosses `/`; keep it
  non-recursive.
* `sync`: `notebooks/check_reports.py` starts with `# Databricks notebook source`, so it is synced as a
  notebook; `include: notebooks/*.py` only makes the intent explicit. `exclude: apps/**` keeps the
  parked app out of the workspace; the wheel build reads its demo CSVs locally.
* `artifacts`: `dynamic_version: true` patches a timestamp into each wheel version. Serverless caches job
  environments by package version, so changed code with an unchanged version would otherwise run stale.
* `targets.dev`: the workspace host comes from `DATABRICKS_HOST` or a CLI profile (`databricks auth
  login`), or add `workspace.host`. `catalog: main` is the dev catalog (it needs `CREATE SCHEMA`). The dev
  override of the bootstrap task passes the dev-prefixed `platform_demo` schema name, because development
  mode prefixes schema names.
* `targets.prod`: set `workspace.host` or use a profile; `notify_email` is a placeholder for the owners'
  distribution list.

`resources/jobs.yml`:

* Paths are relative to the file: `../dist/*.whl` is the `kpi_evidence` wheel and
  `../libs/reconloop/dist/*.whl` the `reconloop` wheel, both built by the artifacts and uploaded to
  `${workspace.artifact_path}/.internal/`.
* Key-value tasks (`named_parameters`, `base_parameters`) receive the job parameters too. JSON-array
  `parameters` (the `kpi_wheels` script task) do not, so they reference `{{job.parameters.*}}`
  explicitly.
* `kpi_check`: `pause_status: PAUSED` is explicit so that prod is paused too.
* `kpi_pipeline`: `queue.enabled` lets repeated runs queue (content-hash ids make repeats harmless);
  `max_docs` defaults to 200 because SQL task output is limited to 1,000 rows / 48 KB; `process` uses
  `run_if: ALL_DONE` so that one document's failed extraction does not block the others. For MLflow
  tracing from the pipeline stages (`settings.tracing`), add `mlflow-skinny>=3.4` to its `default`
  environment.
* `kpi_dq_and_eval`: `mlflow-skinny` in the `eval` environment logs the evaluation metrics to the
  experiment; `evaluate` also runs without it.

`resources/uc.yml`:

* Two volumes on purpose: Unity Catalog grants volume privileges per volume, not per folder. The wheels
  volume holds code that notebooks and `kpi_check` install and execute, so only admins write it; report
  uploaders write the reports volume only. Admins inherit `ALL_PRIVILEGES` on the volumes from the schema.
* Users need `USE_SCHEMA` on the service schema to reach the volumes; `SELECT` and `MODIFY` matter only
  with `persist=True`.
* Development mode prefixes schema names (not volume names), so every file refers to
  `${resources.schemas.<key>.name}`, never to `${var.schema}`.
