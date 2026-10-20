# reconloop

Reconcile a metric you rebuilt from source data against figures in a trusted report, one explained change at a time, and keep a record of the work.

A judge (a rule-based baseline, an LLM, or an agent working in a notebook) proposes a definitional change or a bounded group of changes that belong together. Code runs the queries and computes the variance. Guardrails accept or reject the whole proposal. The loop stops when the fit numbers match or no defensible change remains, and records the holdout check and possible causes of any residual. Declared options constrain the search; the result still needs review.

```
trusted report ──extract──▶ targets ─┐
                                     ▼
metric spec (allowed choices) ──▶ run ──▶ compare ──▶ judge proposes a named change
                                     ▲                         │
                                     └──── guardrails: accept / reject ◀┘
                                                     │
                                         reconciliation record (show your work)
```

## Why the judge matters: the demo in one paragraph

The bundled demo is a fictional grocery chain. It has three "trusted reports", each built with a hidden definition: fiscal or calendar week, comparable or all stores, gross or net. One report's older weeks were also restated up 1.5% after publication.

A judge that only minimizes the gap gets 2 of 3 right. On the restated report it keeps the wrong week definition, because that definition happens to soak up part of the restatement.

The stated-rule judge gets 3 of 3. It follows what the report says, reaches the right definition, and stops with an explained residual: an approximately 1.48% shortfall relative to targets restated upward by 1.5%, flagged as a possible restatement. Its holdout periods match within rounding tolerance.

Requires Python 3.11 or later. From a checkout of this repository:

```
pip install -e ".[dev]"
python examples/fictional_retail/generate_data.py
reconloop run examples/fictional_retail/config.toml --judge search      --out out_search
reconloop run examples/fictional_retail/config.toml --judge stated_rule --out out_rule
reconloop score out_rule examples/fictional_retail/answers.json
```

Open `out_rule/report.md` to see the step-by-step record.

## Prepare a run

```sh
python examples/setup_workflow/generate_data.py --out private/setup
reconloop discover private/setup/schema.json private/setup/targets.csv --table facts --dictionary private/setup/dictionary.csv --out private/setup-draft
```

Schema and CSV/XLSX dictionary metadata produce a configuration draft with candidate
mappings and explicit review questions. Draft configs cannot execute until reviewed
and approved. `extract_pdf_auto` can locate requested figures across PDF pages through
a caller-owned vision callback, retaining source evidence and requiring independent
review. See [setup workflow](docs/SETUP_WORKFLOW.md).

## Rasterized dashboard PDFs

Extract labeled KPI tiles from digital, hybrid or fully rasterized exports with
text-first OCR fallback. Each figure keeps its page, region and confidence;
missing, ambiguous or low-confidence matches require review.

```sh
python -m pip install -e ".[ocr,test-pdf]"
# Install the Tesseract executable separately and put it on PATH.
python examples/tableau_raster/generate_pdf.py
reconloop extract examples/tableau_raster/extract.toml --out out_extract
```

See [PDF extraction](docs/EXTRACTION.md) for tile coordinates, review crops, dates,
K/M/B values, percentages and limits. The included PDF is synthetic; real Tableau
layouts need their own templates. Solid-color bars can also be measured with two
verified linear-axis ticks and explicit regions. These estimates retain uncertainty
bounds and require independent review before becoming targets; automatic chart
interpretation is not supported. See the [calibrated bar example](examples/calibrated_bars).

## Multiple metrics in one run

```sh
python examples/multi_metric/generate_data.py
reconloop run examples/multi_metric/config.toml --out out_multi
reconloop score out_multi examples/multi_metric/answers.json
```

The example reconciles sales, orders and return rate across five periods. Shared
parameter groups reduce repeated config, while each metric keeps independent
decisions and holdout checks. Use `--report` and `--metric` to select a subset,
or `ReconSession.open_next()` and `progress()` for an agent work queue. See
[multi-metric workflow](docs/MULTI_METRIC.md).

Independent metrics can run with `--parallel --max-workers 4`. Config batches,
async completion callbacks, bounded fit precomputation and an optional source-versioned
scalar cache are covered in [execution](docs/EXECUTION.md).

For related metrics, declare a coupled group to enforce identities such as
`share.numerator = category_volume.value`, or require matching parameter choices.
The bounded solver ranks candidates on fit periods and checks one selected
candidate against holdouts. It rejects the group if any final member or identity
fails. See [coupled reconciliation](docs/COUPLED_METRICS.md).

```sh
python examples/coupled_metrics/generate_data.py
reconloop run examples/coupled_metrics/config.toml --out out_coupled
reconloop score out_coupled examples/coupled_metrics/answers.json
```

## Versioned data, shares and territories

```sh
python examples/versioned_metrics/generate_data.py
reconloop run examples/versioned_metrics/config.toml --out out_versioned
reconloop score out_versioned examples/versioned_metrics/answers.json
reconloop revisions examples/versioned_metrics/config.toml --metric total_volume --parameter data_vintage --before published --after refreshed --records out_versioned --out out_revisions
```

The fictional example covers five metrics, duplicate category memberships, source
date/cycle partitions, dated territory assignments and numerator/denominator shares.
The stated-rule judge recovers 5/5 definitions and passes their holdouts. One share
requires a three-parameter proposal; each single edit initially makes it worse.
Ratio components are recorded even when their changes cancel in the final ratio.
See [versioned metrics](docs/VERSIONED_METRICS.md) for configuration and limits.

`reconloop job config.toml --out private/runs` provides one isolated batch for an
external scheduler, with a JSON manifest and exit codes for success, review, or
failure. It does not install a scheduler. Difficult PDF crops can use a caller's
vision endpoint with [mandatory transcription review](docs/EXTRACTION.md#vision-assisted-transcription).

## Deployment checks and shared targets

```sh
reconloop doctor --ocr
reconloop publish-targets private/extraction private/targets --id reviewed-2026-01
```

The diagnostic renders a PDF and checks the actual OCR engine. Reviewed extraction
packages can be published as immutable, hash-verified bundles on local storage or
mounted Unity Catalog volumes. The repository includes a pinned-package cluster
init script and a readiness notebook. See [deployment and storage](docs/DEPLOYMENT.md)
for runtime requirements, permissions and checks to run on the actual cluster.

Sessions support `defer`, `resume` and private JSON checkpoints without resetting
their history or change budget. Closed records include stable-ratio diagnostics;
configured vintage and category breakdowns can investigate residuals while keeping
the final definition fixed. See [lifecycle and diagnostics](docs/LIFECYCLE_DIAGNOSTICS.md).

## Ways to run it

| Mode | Judge | Use when |
|---|---|---|
| `reconloop run` | `SearchJudge` or `StatedRuleJudge` | Batch runs, baselines, CI |
| `reconloop run` with `[coupled]` groups | Bounded deterministic joint search | Explicit identities and shared choices within a report |
| `reconcile(..., judge=LLMJudge(complete))` | Any model behind a `complete(prompt) -> str` function | You have an API or serving endpoint |
| `ReconSession` in a notebook | An agent (Genie or Assistant, Copilot) or a person | Interactive work. The agent follows `AGENT_GUIDE.md` and drives `try_change(...)` |

In every mode the model only proposes. Code computes, and guardrails decide.

## Core ideas

- **The metric spec is the governance.** Each metric is a SQL template plus declared options per parameter (week definition, universe, measure...). Each parameter is tied to a named reason from `hypotheses.CATALOG`. The judge can only move between declared options. A genuinely new option can be proposed with `add_option`, and it's flagged for human review.
- **An explained gap beats a small gap.** A change can be accepted because it makes the gap *consistent* across periods, even if the average gap grows, as long as the holdout doesn't get worse. That is how a restatement shows up as a clean level shift instead of being absorbed by a wrong definition.
- **Holdout values are omitted from judge state and session status.** Guardrails check whether a change worsens holdout variance, and final results outside holdout tolerance stay unresolved. This is an API convention, not isolation from Python code that can access the runner or files.
- **Every run leaves a record:** start and final definition, each change with its reason, accepted or rejected with why, the variance at each step, the residual pattern with likely causes, and the final SQL.

## Layout

```
src/reconloop/
  schema.py        Target, Change, Iteration, Residual, ReconRecord
  hypotheses.py    the catalog of legitimate reasons numbers differ
  metricspec.py    SQL template + declared options per parameter
  sql.py           snapshot, deduplication and dated assignment builders
  revisions.py     comparisons between declared source vintages
  coupled.py       fit-only joint search with cross-metric constraints
  storage.py       immutable verified target bundles and mounted paths
  doctor.py        dependency, PDF rendering and OCR readiness checks
  discovery.py     metadata mappings and review-required config drafts
  diagnostics.py   post-close factors, components and declared breakdowns
  checkpoint.py    resumable session state bound to inputs and policy
  execution.py     bounded parallel dispatch and fit precomputation
  jobs.py          isolated batch runs and scheduler manifests
  compare.py       variance and residual shape (level shift / drift / noisy)
  guardrails.py    acceptance rules
  loop.py          automatic loop with any judge
  session.py       step-by-step API for an agent or a person
  judge/           search, stated_rule, llm
  runners/         duckdb (local), spark (Databricks)
  extract/         CSV and PDF targets, tile OCR, extraction evidence
  record.py        JSON + Markdown records
  scoring.py       score a run against known answers
examples/fictional_retail/   demo data generator, config, answers
examples/multi_metric/       three KPIs with shared parameter declarations
examples/tableau_raster/     synthetic raster PDF and extraction template
examples/versioned_metrics/ five KPIs with snapshots, shares and territories
examples/coupled_metrics/   volumes and share constrained together
examples/calibrated_bars/   synthetic chart and explicit linear calibration
examples/setup_workflow/   fictional schema, dictionary and target generator
notebooks/                   Databricks notebook for agent-as-judge
AGENT_GUIDE.md               instructions for the agent
docs/                        design, adapting to your data, extraction
```

## Status

Version 0.5.0 adds reviewed report discovery and configuration drafts, deferred
sessions with checkpoints, post-close diagnostics, bounded parallel execution,
async completions, fit precomputation and a versioned scalar cache. It also includes
coupled reconciliation, reviewed bar-chart estimates, verified target bundles and
cluster readiness checks.
Coupled groups require aligned periods and declared identities; interactive
single-metric sessions reject coupled configurations. Chart measurement requires
explicit calibration and review. Live cluster installation, scheduling and volume
access still need validation in the deployment environment. Real-report model
extraction accuracy is not benchmarked.
See `docs/DESIGN.md` for planned extensions.

`explained_residual` means a pattern suggests possible causes; it does not establish that a restatement occurred. Agent-added options remain subject to human review, even if their numbers match.

## Data and execution boundaries

Use trusted SQL templates, option fragments and Python judges only. The runners execute SQL with the permissions of their DuckDB process or Spark session; reconloop is not a SQL sandbox. `add_option` makes SQL executable immediately and logs it for later review. Use a restricted data environment for agent-driven work.

Targets must have ISO dates, finite values and a `fit` or `holdout` split. Queries must return exactly one row and one finite column named `value`. A NULL or missing result is an error; use `COALESCE` explicitly when zero is the intended meaning. Relative variance against a zero target is undefined for a nonzero result and is written as `null` in JSON, with the reconciliation left unresolved.

An LLM completion callback receives report labels, stated rules and fit figures. Use an endpoint approved for that data. Keep real reports, business configurations, credentials and generated reconciliation records outside this public repository. All bundled demo inputs are generated locally from fictional data.

## Development

```sh
python -m pip install -e ".[dev]" build
python -m pytest -q
python -m build
```

CI runs the test suite and package build on Windows and Linux with Python 3.11 and
3.13. Linux jobs install Tesseract and require the real OCR integration test.
For those tests locally, install `.[dev,test-pdf]` and the Tesseract executable.
Spark's scalar-result contract is covered with a test double; a live Databricks
workspace and external LLM endpoint are not required by the tests.

MIT license. All demo data is fictional.
