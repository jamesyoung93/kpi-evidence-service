# Parked deployment pieces (not deployed)

Status: parked on 2026-10-19. The product is a code package used from a notebook; a run ends in a
comparison report (DataFrame, HTML, Excel). Nothing is piped onward automatically until a hand-off is agreed.

`databricks.yml` includes `resources/*.yml` only. This folder is not matched by that glob (`*` never
crosses `/`), and the parked resource files also carry a `.disabled` extension, so nothing here is
deployed. `tests/deploy` checks both.

| File | What it is | Depends on |
|---|---|---|
| `optional.yml` | optional configuration blocks, ready to insert into the bundle YAML (which carries no comments): `parked_variables`, `parked_dev_variables` (`databricks.yml`), `check_wheel_variant`, `parked_bootstrap_parameters`, `file_arrival_trigger`, `parked_publication_tasks` (`resources/jobs.yml`). Each block names its `target`, `how` (append to a list or merge into a mapping) and `content` | `docs/DEPLOY.md` section 3 |
| `app.yml.disabled` | Databricks App `kpi_review`: the vendored insight_harness with the KPI evidence pages (review, decisions, publication), bound to the warehouse, the reports volume, the pipeline job, the Genie agent, the model service (`MODEL_SERVICE` securable) and the MLflow experiment | the parked variables, `consume.yml.disabled` (Genie agent), `insight_harness.app.yaml`, `scripts/parked/build_app_bundle.py` |
| `insight_harness.app.yaml` | the app's runtime config, moved from `apps/insight_harness/app.yaml` | must go back to `apps/insight_harness/app.yaml` (the App reads it from its source folder) |
| `consume.yml.disabled` | AI/BI dashboard `kpi_coverage` and Genie agent `kpi_published` over published values | the parked variables; publication switched on; the partner-powered AI setting (Genie) |

Related parked pieces outside this folder:

* `databricks.yml`: the `sync` settings for the app's wheels (step 3 below).
* `registry/policy.yaml`: `auto_publish` is off.
* `src/sql/jobs/grants.sql`: applied by the bootstrap only when it receives an `app_sp`.

## Switching on (after the hand-off to insight_harness is agreed)

The full sequence, with the checks to run, is in `docs/DEPLOY.md` section 11. In short:

1. `mv resources/parked/app.yml.disabled resources/app.yml` and
   `mv resources/parked/consume.yml.disabled resources/consume.yml` (the relative paths inside, such
   as `../apps/insight_harness`, assume the files sit in `resources/`).
2. `mv resources/parked/insight_harness.app.yaml apps/insight_harness/app.yaml`.
3. From `optional.yml`, merge `parked_variables` into `variables` of `databricks.yml` and
   `parked_dev_variables` into its `targets.dev.variables`; in `sync`, remove the `apps/**` exclude and
   add `apps/insight_harness/vendor_wheels/*.whl` to `include`.
4. From `optional.yml`, merge `parked_bootstrap_parameters` into the bootstrap task's
   `named_parameters` and append `parked_publication_tasks` to `kpi_pipeline.tasks` in
   `resources/jobs.yml`. Delete each inserted block from `optional.yml`.
5. Flip `registry/policy.yaml` `auto_publish` switches as agreed.
6. Build the app's wheels from the project root: `python scripts/parked/build_app_bundle.py --install`.
7. `databricks bundle validate`, `deploy`, `bundle run kpi_bootstrap`, `bundle run kpi_review`.
