# Operations guide

This guide covers local verification and demo deployment. The bundled workload
is deterministic synthetic monthly pharma data; it is not a production data
pipeline.

## Runtime and installation

Use Python 3.12 to match CI.

```bash
python -m venv .venv
# PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -c constraints.txt -r requirements-dev.txt
python -m pip check
```

`requirements.txt` and `requirements-dev.txt` declare supported ranges;
`constraints.txt` pins the direct versions exercised by CI. Update the
constraints intentionally and rerun the complete release checks when upgrading
dependencies.

`streamlit-sortables==0.3.1` is pinned in both runtime requirements and CI
constraints. Release verification includes drag reorder and the Move up/down
button fallback in the smoke checklist below.

Start the app from the repository root:

```bash
python -m streamlit run app.py
```

The deterministic parser requires no external service. If an Anthropic call is
unavailable, times out, exceeds the session allowance, returns an invalid intent,
or produces an unsafe digest rewrite, the UI retains the governed fallback.

## Configuration

Environment variables and root-level Streamlit secrets use the same names.

| Setting | Default | Purpose |
|---|---:|---|
| `ANTHROPIC_API_KEY` | unset | Optional deployment-owned key; still unavailable to anonymous sessions unless public use is explicitly enabled |
| `INSIGHT_HARNESS_ALLOW_PUBLIC_LLM` | `false` | Allows anonymous sessions to spend the deployment-owned key |
| `INSIGHT_HARNESS_LLM_SESSION_LIMIT` | `25` | Combined per-session allowance enforced by the model-backed UI paths; clamped to 1–500 |
| `INSIGHT_HARNESS_LLM_MODELS` | app defaults | Comma-separated model allowlist shown in the sidebar |
| `INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN` | unset | Enables shared governance writes and causal analyst sign-off after constant-time token validation |
| `INSIGHT_HARNESS_TELEMETRY_HASH_KEY` | random per process | Optional secret HMAC key for stable feedback question identifiers across process restarts |
| `INSIGHT_HARNESS_LOG_RAW_QUESTIONS` | `false` | Opts raw question text into feedback telemetry |

Start from `.env.example` for a local environment loader or
`.streamlit/secrets.toml.example` for Streamlit hosting. The application does not
load `.env` itself; export those values with your shell or environment manager.

Recommended public defaults:

```text
INSIGHT_HARNESS_ALLOW_PUBLIC_LLM=false
INSIGHT_HARNESS_LLM_SESSION_LIMIT=25
INSIGHT_HARNESS_LOG_RAW_QUESTIONS=false
```

Leave `INSIGHT_HARNESS_TELEMETRY_HASH_KEY` unset when feedback identifiers do not
need to correlate across restarts. If stable correlation has an approved
purpose, configure a long random secret, restrict access to it, and rotate it
under the telemetry retention policy; rotation intentionally breaks linkage to
older identifiers.

## Regenerate and verify the benchmark

The generator owns the committed data contract. It uses a local NumPy generator
with seed `42`, writes explicit UTF-8/LF files, derives the HCP table from source
A, emits a deterministic observed-only referral feed, and records exact
event/source truth in `ground_truth.json`.

```bash
python data/generate_demo_data.py
git diff --exit-code -- data/accounts.csv data/fact_source_a.csv \
  data/fact_source_b.csv data/fact_referral.csv data/ground_truth.json
```

Expected shape after regeneration:

| Artifact | Grain | Expected rows |
|---|---|---:|
| `data/fact_source_a.csv` | `account_id`, `month` | 5,760 |
| `data/fact_source_b.csv` | month + territory/district/region/specialty/payer channel | 1,104 |
| `data/fact_referral.csv` | receiving `account_id`, `month` | 4,608 |
| `data/accounts.csv` | `account_id` | 240 |

The source-A calendar covers July 2024 through June 2026. Source B intentionally
ends in May 2026 because its registered reporting lag is one month. The referral
feed covers exactly 80% of eligible HCPs; it is observed-only, so uncovered HCPs
remain unknown rather than becoming zero or projected activity.

If generator output changes intentionally, update the registry, ground truth,
independent golden expectations, and documentation together. Never update golden
values merely to make a failing implementation pass.

## Verification and release

Run the same checks as CI:

```bash
python -m pip check
python data/generate_demo_data.py
git diff --exit-code -- data/accounts.csv data/fact_source_a.csv \
  data/fact_source_b.csv data/fact_referral.csv data/ground_truth.json
python -m compileall -q app.py harness views tests
python -m pytest -q
```

Before a demo or release, also exercise these flows in a clean Streamlit
session:

1. Home renders the selected persona and governed top-three strip without a
   model credential; small visible changes remain legible on tile sparklines
   and the same expanded-answer chart because both use the shared explicit
   visible-data y-domain.
2. Scope, window, comparison, source, and sales-type controls do not crash tiles
   that lack a requested source or variant; the fallback is disclosed.
3. A tile can be watched, opened, broken down, expanded, and downloaded. Tile
   reorder works by drag when the pinned component loads and by Move up/down as
   the keyboard fallback.
4. Monitoring preserves the saved source, variant, basis, and scope on
   drill-through.
5. Digest renders three or fewer diverse governed signals with category labels,
   endpoint-marked sparklines, and full-size dialogs; it downloads as one
   artifact and preserves resolution on breakdown.
6. Causal Studio shows registered treated/control scopes and remains
   Hypothesis-tier. Analyst review is locked until the administrator token is
   authenticated on Semantic Layer; sign-off records provenance for the exact
   result but does not promote its tier.
7. Reliability completes the independent set and reports reproducibility.
8. A share tile discloses its adaptive basket, an explicit IL-17 or advanced-
   therapy choice is stamped as an override, and its governed members reconcile
   to the denominator.
9. Market Access referral tiles disclose computed scope completeness; uncovered
   HCPs remain unknown, not zero.
10. The exact question “Compare the activity mix of top 20 HCPs by NRx share
    with matched peers” returns a Directional artifact with selection floors,
    matching recipe, referral coverage, stable evidence, and a Causal Studio
    handoff.
11. An expanded geographic tile drills national → region → district → territory
    and exposes a ranked synthetic-NPI table at territory scope.
12. Semantic Layer stays read-only without an admin token; a test deployment can
   apply and log an intentional authorized governance change.

GitHub Actions performs dependency, regeneration, compilation, and test checks
on pull requests and pushes to `main` with `contents: read` permission.

## Runtime state

| State | Public UI behavior | Storage |
|---|---|---|
| Persona layout and saved default | Isolated to one viewer/session | Streamlit session memory |
| Saved insights and watches | Isolated to one viewer/session | Streamlit session memory |
| Public digest history/novelty | Isolated to one viewer/session | In-memory digest store |
| Governance configuration and bounded authoritative audit history | Shared, admin-authorized | ignored local JSON |
| Governance history export mirror | Shared | ignored local JSONL |
| Feedback/corrections | Shared metadata; raw questions off by default | ignored local JSONL |
| Reliability run history | Shared | ignored local JSONL |

The harness also exposes a durable digest-history store and retains legacy local
watchlist helpers for non-UI callers. The public app does not use those as an
anonymous cross-viewer profile. Any durable deployment must provide an
authenticated owner namespace and controlled storage.

Ignored runtime paths are:

```text
data/governance_config.json
data/governance_log.jsonl
data/feedback_log.jsonl
data/eval_history.jsonl
data/digest_history.jsonl
data/watchlist.json
```

These files may disappear on container restart and are not a backup. Each
governance change and its audit record are committed together by atomic config
replacement; the fsynced JSONL file is an append-friendly mirror, not the
authoritative transaction record. If history matters, replace or export these
files to a durable service with identity, access control, atomicity, retention,
and monitoring.

## Streamlit Community Cloud

1. Create an app from the repository root with branch `main` and entrypoint
   `app.py`.
2. Select Python 3.12.
3. Deploy without secrets first and verify deterministic-parser mode.
4. Add only the required settings through Streamlit's secret management.
5. Leave `INSIGHT_HARNESS_ALLOW_PUBLIC_LLM=false` unless anonymous use of the
   deployment-owned key is intentional.
6. Configure `INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN` only if the public process
   must support shared governance writes or authenticated causal sign-off.
7. After the `main` deployment refreshes, repeat the release smoke flows above
   against the hosted URL. A successful local check or merge alone is not proof
   that the hosted process has pulled the new commit.

Managed-hosting local files are not a durable system of record. The “Daily
digest” remains a deterministic view over the current monthly snapshot; schedule
real source refreshes before describing it as a true daily feed.

## Troubleshooting

**The model feature says it is off even though a deployment key exists.** This is
the secure default. Enter a session key, or explicitly set
`INSIGHT_HARNESS_ALLOW_PUBLIC_LLM=true` if deployment-funded anonymous access is
intended.

**A requested source or variant is unavailable on a tile.** Heterogeneous tiles
do not all support panel data or every TRx variant. The materialized spec clamps
the incompatible override to its governed default and discloses the reason.

**Source B has no June 2026 answer.** This is the registered one-month panel lag.
The correct behavior is a no-data abstention, not zero or source-A substitution.

**A share breakdown is refused.** TRx share is a ratio. Descriptive aggregation
is governed; contribution decomposition is intentionally unavailable until a
mix-effects method is registered.

**A share answer changes when I select a different basket.** This is expected:
IL-17 class and advanced therapy have different governed denominators. The
selected/default basket, registry fingerprint, reconciliation, and any override
are part of the artifact evidence.

**A referral tile is lower than an external total.** The bundled referral feed
is observed-only at 80% HCP coverage and is not projected. Use the computed
scope completeness disclosure; uncovered HCPs are unknown, not zero.

**A digest does not rotate each day.** Static monthly data does not justify fake
date-seeded rotation. It changes with data, governance, persona/scope, watches,
or identity-scoped history.

**History vanished after a restart.** Session state and managed-host local files
are ephemeral by design in this demo. Use an authenticated durable store for
production history.
