# Insight Harness

[![Verify](https://github.com/jamesyoung93/insight_harness/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/jamesyoung93/insight_harness/actions/workflows/ci.yml)

Ask business questions in plain English. Insight Harness validates the request,
calculates results in Python, and returns an auditable answer with source and
calculation provenance.

This Python/Streamlit workbench runs on bundled synthetic pharma commercial
data, with no API key required. Explore persona-specific KPI tiles, questions in
plain language, monitoring, a top-three digest, and registered causal designs.

The trust boundary is simple: a language model may translate a question or
rephrase a digest headline, but it never computes an answer. Registry-validated
intents enter deterministic engines, and every result is exported as a
provenance-stamped artifact with its source, variant, data version, code, tier,
and stable hash.

> [!IMPORTANT]
> All bundled records and effects are deterministic synthetic demo data. The
> dataset is monthly, not weekly, and contains no patient, prescriber, or other
> real-world data. Do not use this repository to make clinical or production
> commercial decisions without replacing and validating the full data contract.

![Insight Harness running with synthetic data and the built-in parser](docs/assets/workbench.png)

*The running app with bundled synthetic data and the built-in parser. No model
credential is needed for this view.*

## Quick start

Python 3.12 is the CI runtime.

```bash
python -m venv .venv
# PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -c constraints.txt -r requirements-dev.txt
python data/generate_demo_data.py
python -m streamlit run app.py
```

Run the verification suite separately:

```bash
python -m compileall -q app.py harness views tests
python -m pytest -q
```

The app is fully usable without an API key. In that mode, questions use the
bounded rule parser and digest cards use deterministic templates.

### Three examples to inspect

| Question or scenario | Expected behavior | Evidence |
|---|---|---|
| “What is TRx in the West region?” | Calculate the latest month's total prescriptions for West from the registered source | Answer artifact with scope, source, data version, and result hash |
| “Forecast TRx for next quarter” | Return a scoped refusal because no forecasting model is registered | Abstained artifact with suggested supported questions |
| A model returns `"metric": ["trx"]` or `"filters": []` | Reject the malformed translation and use the built-in parser; preserve the question's scope or refusal | Artifact records the rejected translation's fallback reason; regression tests exercise the translator and pipeline |

Try the first two questions in **Home → Explore**. The third is a deliberately
injected model response, reproducible without an API key:

```bash
python -m pytest -q tests/test_llm_validation.py
```

## What is implemented

| Surface | Capability |
|---|---|
| **Home** | Persona defaults; a compact, governed top-three signal strip; live KPI, diagnostic, retrieval, referral, and matched-cohort tiles; compact window/comparison/data controls; adaptive basket disclosure; material-fork disclosure; Watch, Open, Break down, Expand, and artifact download actions; natural-language exploration below the tiles. Tile sparklines and expanded answer charts share an explicit padded y-domain derived from every finite visible series, so small movements remain legible without forcing zero onto the scale |
| **Digest** | Three deterministic, scope-diverse signals with executive-language headlines, category labels, 12-point endpoint-marked sparklines, evidence stamps, and the disclosed priority calculation in “Why this surfaced”; artifact downloads, full-size dialogs, resolution-preserving drill-through, and optional validated model phrasing |
| **Monitoring** | Session-owned watched insights plus an anomaly feed ranked with priority score v2 (standardized movement, relative movement, and business scale); low-base guards suppress inflated percentage/scale terms, while overlapping scopes and the registered details/attainment relationship are clustered into one story |
| **Deep exploration** | Large governed dialogs for tiles, charts, and digest stories; modal-local window, comparison, and split controls; national → region → district → territory breadcrumbs; territory-level ranked synthetic HCP records with NPI and a disclosed minimum-volume floor; drag reorder through a pinned component with Move up/down controls retained as the fallback |
| **Causal Studio** | Difference-in-differences proposals for three registered synthetic events, including treated/control scopes, computed pre-trend and sensitivity checks, and caveats; analyst sign-off requires administrator-token authentication and adds a provenance stamp without promoting the Hypothesis tier |
| **Semantic Layer** | Public read-only metric and source registries; server-token-gated administration of the materiality threshold and default variants; atomic configuration writes with embedded audit history and a synced local JSONL mirror |
| **Reliability** | A pharma-only independent golden set covering values, ratios, retrieval, decomposition, causal effects, divergence, refusals, and source/data contracts; the set auto-runs once per data version and leads with pass, reproducibility, correct-refusal, and correction-rate scores; every case runs twice for hash reproducibility |
| **How answers are produced** | In-product explanation of tiers, provenance, and the authority boundary between translation and computation |

### Persona presets

The five presets are Sales Rep, District Manager, Brand Marketing, Market
Access, and Executive. Sales Rep starts at territory scope, District Manager at
district scope, and the other presets at national scope. Each preset supplies a
tile set, scope, window, comparison basis, and digest scope.

District Manager and Market Access presets surface observed referral tiles;
Brand Marketing and Executive presets surface the governed top-NRx-share versus
matched-peer cohort tile. These remain saved-question specifications evaluated
through the same pipeline as Ask.

Tile add/remove/reorder choices, saved defaults, watches, and public digest
history are owned by the current Streamlit session. They are intentionally not
written to a shared profile file, so one anonymous viewer cannot change another
viewer's workspace. They expire with the session; production persistence needs
authenticated user identity and an external store.

## Governed pharma contract

The registry exposes these monthly metrics:

- TRx with units, gross-dollar, and payer-normalized variants
- NRx and NBRx
- TRx market share under governed `il17_class` and `advanced_therapy` baskets,
  with immutable membership, reconciliation, adoption-stage adaptive defaults,
  and disclosed user overrides
- details delivered, call plan, and call-plan attainment
- samples dropped, speaker attendance, and new writers
- observed incoming referrals and active referrers, with computed scope
  completeness and no projection of uncovered HCPs

The registered dimensions are `territory`, `district`, `region`, `specialty`,
and `payer_channel`. The HCP universe also carries a stable synthetic 10-digit
NPI plus derived decile, adoption stage, trailing Rx/share, recency, and 90-day
activity fields for governed retrieval and cohort selection.

The three source products have distinct, registered contracts:

- `source_a`: **Direct/DDD + specialty pharmacy feed**, at one
  `account_id` × `month` row, covering prescriptions and field activity with no
  reporting lag.
- `source_b`: **Projected retail panel**, aggregated by month and the five
  registered dimensions, with a one-month lag, registered regional projection
  factors, and an early-history restatement. It does not claim HCP or
  field-activity grain.
- `referral`: **Referral relationship feed**, at receiving-HCP × month grain
  for a deterministic 80% of eligible HCPs. It is observed-only: a covered zero
  is zero, while an uncovered HCP remains unknown rather than being imputed or
  projected.

Source comparisons use the common available window. Missing panel coverage is
reported as a coverage gap rather than misclassified as a value fork. Ratio
metrics are aggregated from numerator and denominator; an undefined denominator
stays undefined rather than becoming a false zero. Ratio decomposition is
deliberately refused because no governed share-decomposition method is
registered.

Three synthetic events are registered with exact treated and control scopes:

- speaker-program launch in two East territories, with matched North territory
  controls;
- South Medicare Part D formulary win, with North/East Medicare Part D
  controls; and
- competitor launch in West Cardiology, with Cardiology controls from the
  other three regions.

`data/ground_truth.json` records the injected effects, source pathologies,
grains, and invariants used by the independent evaluation path.

## Answer pipeline

```text
question or saved spec
  -> triage / validated model translation       harness/triage.py
  -> governed source + variant resolution       harness/semantic_layer.py
  -> deterministic engine                       harness/engines/
  -> common-window divergence + caveats         harness/services.py
  -> provenance-stamped AnswerArtifact          harness/provenance.py
```

Question classes are Retrieval, Descriptive, Diagnostic, Cohort comparison,
Causal, Predictive, and Out of scope. Predictive requests are refused because
the registry contains no governed forecasting model. Causal requests execute
only when they match a registered event and preserve its treated population;
otherwise the system returns a scoped reframe.

The built-in parser also recognizes this cohort comparison:

> Compare the activity mix of top 20 HCPs by NRx share with matched peers

The deterministic recipe applies disclosed NRx and market-NRx floors, selects
the top 20 by trailing NRx share, and matches without replacement on exact
region, specialty, and governed decile band before minimizing market-NRx
opportunity distance. It compares R3M details, samples, speaker attendance,
call-plan attainment, and observed referral rates. The artifact remains
**Directional**, prints the matching recipe and coverage caveat, and offers a
Causal Studio handoff rather than implying that the activity gaps caused share.

Basket-specific Ask routes are also explicit, for example “What is TRx share in
the IL-17 class?” Share tiles resolve their adoption-stage adaptive default and
disclose the selected basket; an explicit tile or Ask choice is stamped as an
override.

A tile is an immutable saved-question specification, not a separate dashboard
calculation. Tile rendering, Ask, Monitoring drill-through, and digest
drill-through all enter the same answer pipeline. Cache identity includes the
materialized spec, effective scope, governance configuration, and data version.

## Architecture

| Layer | Responsibility |
|---|---|
| **App shell** | `app.py` owns navigation, minimal Streamlit chrome, the compact translator status, and the collapsed optional-model connector. The full zero-key workbench remains available through the built-in parser |
| **Presentation** | `views/common.py` renders the reusable answer hero, shared explicit visible-data chart domain, bordered artifact card, full-size answer dialog, divergence and caveat disclosures, provenance stamp with result hash, and compact feedback/download actions; no view computes a governed number |
| **Governance** | `harness/semantic_layer.py` resolves registered metrics, dimensions, sources, variants, events, and materiality before execution |
| **Execution** | `harness/pipeline.py` routes validated intents into deterministic descriptive, retrieval, decomposition, basket-share, cohort, and causal engines; identical intent, configuration, and data version produce the same artifact hash |
| **Trust record** | `views/reliability.py` runs and session-caches the independent golden set by data version; tests cover engines, contracts, reproducibility, security policy, and headless UI flows |

## Optional language-model features

Users may enter their own Anthropic API key in the collapsed **Connect a
language model…** sidebar panel for the current session. Model selection stays
hidden until an authorized key is present. A deployment-owned
`ANTHROPIC_API_KEY` is disabled for anonymous use
unless the operator also sets:

```text
INSIGHT_HARNESS_ALLOW_PUBLIC_LLM=true
```

The deployment controls an allowlist of model IDs and a per-session model-call
limit. Model translation is registry-validated before execution; invalid output
falls back to rules. Digest rewriting receives a bounded fact payload and is
accepted only if metric, scope, entities, direction, units, numeric claims, and
non-causal wording validate exactly. The deterministic template wins on any
failure.

See [SECURITY.md](SECURITY.md) before enabling model access or governance writes
on a public deployment.

Feedback question identifiers are keyed HMACs rather than plain hashes. By
default, a random per-process key prevents identifiers from being correlated
across restarts. Operators that need stable, controlled correlation may set
`INSIGHT_HARNESS_TELEMETRY_HASH_KEY`; treat that value as a secret and define a
retention purpose before enabling it.

## Synthetic data and reproducibility

`data/generate_demo_data.py` uses seed `42` to produce:

- 240 synthetic HCPs across 24 months (`2024-07` through `2026-06`);
- 5,760 account-month rows in source A;
- 1,104 aggregated, one-month-lagged panel rows in source B;
- 4,608 observed referral rows covering exactly 80% of eligible HCPs; and
- an HCP table derived and reconciled from source A.

Generated CSV files use explicit UTF-8/LF output. The committed benchmark now
contains five generated artifacts; include every one in the release drift
check:

```bash
python data/generate_demo_data.py
git diff --exit-code -- data/accounts.csv data/fact_source_a.csv \
  data/fact_source_b.csv data/fact_referral.csv data/ground_truth.json
```

The in-app label “Daily digest” describes the intended workflow, not fake daily
rotation. With the committed monthly snapshot, the digest changes only when its
data version, governance, persona/scope, watches, or identity-scoped history
changes. A true morning digest requires a real refreshed feed.

## Evaluation and CI

### What is measured

| Evaluation | What it establishes | Boundary |
|---|---|---|
| Deterministic golden set | Calculations, data contracts, scoped refusals, and repeatable artifact hashes against independently computed expectations | Runs through the built-in parser and deterministic engines; does not call a live model |
| Model-output validation and fallback tests | Malformed responses are rejected and fallback preserves supported calculations and scoped refusals | Uses injected responses with the external SDK call mocked |
| Live small-model tool calling | Not yet benchmarked | The optional translator currently calls Anthropic; this repository does not report vLLM/Ollama accuracy or an autonomous tool-calling benchmark |

Passing software tests establishes behavior for the covered cases. Live-model
translation accuracy, recovery success rates, and latency need a separate
evaluation with a named model, fixed data snapshot, and independently checked
held-out questions.

The test suite includes deterministic engine tests, data-contract and golden-set
checks, tile/spec/cache parity, saved-insight migrations, session isolation,
digest ranking and narrator attacks, priority/low-base/clustering contracts,
basket reconciliation, incomplete-referral semantics, cohort matching and hash
determinism, shared chart-scale behavior, dialogs/drill, provenance hashing,
runtime policy, and headless Streamlit page flows.

GitHub Actions runs on pull requests and pushes to `main` with read-only
repository permissions. It resolves the declared dependency ranges against the
tested direct-version pins in `constraints.txt`, checks the dependency graph,
regenerates the benchmark, compiles the app, and runs the full test suite.

For operational configuration, state-file behavior, deployment, and the release
checklist, see [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Repository map

```text
app.py                          Streamlit router and runtime-policy controls
views/
  home.py                       persona tile band and exploration workspace
  tile_detail.py                governed tile dialog and geographic drill UI
  digest.py                     deterministic top-three digest
  monitoring.py                 session watches and anomaly feed
  causal_studio.py              registered causal design proposals
  registry.py                   registry browser and authenticated governance
  reliability.py                golden-set accuracy record
  common.py                     artifact rendering and drill-through helpers
harness/
  semantic_layer.py             metrics, dimensions, sources, events, governance
  baskets.py / referrals.py     basket registry and observed-source contracts
  drill.py                      deterministic geo hierarchy and HCP endpoint
  tiles.py / tile_runtime.py     saved-question specs, identity, and execution
  saved_insights.py             schema-migrated session watch/tile store
  triage.py / llm_translator.py rule and optional model translation
  engines/                      descriptive, retrieval, decomposition, cohort, causal
  services.py                   divergence, monitoring, feedback, caveats
  digest.py / digest_store.py   ranking, artifacts, and history stores
  digest_narrator.py            optional bounded/validated phrasing
  pipeline.py                   orchestration and independent golden set
  provenance.py                 stable answer artifacts and hashes
  runtime_policy.py             model, quota, privacy, and admin policy
data/
  generate_demo_data.py         deterministic pharma benchmark generator
  ground_truth.json             independent event/source/data contract
  fact_source_a.csv             account-month system-of-record facts
  fact_source_b.csv             aggregated projected panel
  fact_referral.csv             observed-only receiving-HCP referral facts
  accounts.csv                  derived HCP universe
tests/                          unit, contract, golden, and AppTest coverage
```

## License

Insight Harness is available under the [MIT License](LICENSE). The bundled dataset is deterministic synthetic demo data and contains no patient, prescriber, or other real-world records.
