"""How answers are produced: the user-voiced explanation of tiers, refusals,
and the provenance stamp."""
from __future__ import annotations

import streamlit as st


def render() -> None:
    st.title("How answers are produced")
    st.markdown("""
Every answer here follows the same path. Your question is translated into a
governed query, matched to a registered metric definition, computed by a
deterministic engine, and stamped with its provenance; measured values and
breakdowns are additionally recomputed under every registered alternate
definition and source, and material differences are flagged. No step
improvises a definition, and no answer is generated from a model's memory — a
language model, when enabled, only translates your wording into the governed
query, and its translation is checked before anything runs.

No model credentials are bundled with the app. Without an Anthropic API key,
questions use the bounded built-in parser. You can add your own key for the
current session. A deployment-owned key is used for anonymous sessions only
when its owner explicitly enables public model access and sets a per-session
allowance.

Questions can carry an explicit time window ("TRx in Q1 2026", "trend details
for the last 6 months"), a comparison basis ("vs prior month", "vs same month
last year"), and governed territory, district, region, specialty, or payer
scope. Those choices are validated against the available monthly data and
disclosed in the answer's headline. Eligible descriptive, diagnostic, and
retrieval answers can be pinned with **Watch**, preserving their exact question
class, source, variant, scope, window, and comparison for the session-owned
Monitoring list. Causal designs stay event-bound in the Causal Studio and are
never flattened into a trend watch. The **Digest** ranks three diverse,
scope-relevant signals; the **Reliability** page keeps the public accuracy,
reproducibility, correct-refusal, and correction record.

### What the tiers mean

| Tier | What it means | How to use it |
|---|---|---|
| **Verified** | A deterministic calculation on governed data using a registered definition. Running it again reproduces the complete result artifact. | Safe to quote, with the caveats shown. |
| **Directional** | Correlational or model-assisted; labeled as such. | Use for orientation, not for commitments. |
| **Hypothesis** | A designed estimate — for example, an event study with computed assumption checks. | Treat as a finding to review. Authenticated sign-off records review provenance but does not promote the tier. |
| **Abstained** | The question can't be answered reliably with what's registered. | Use the suggested reframe, or register the missing metric or event with your governance team. |

### Scoped refusals

When a question asks for something the governed registry can't support — a
forecast with no registered model, a metric that isn't defined, a causal claim
with no registered event behind it — the system declines and says why, and
offers a reframe it *can* answer reliably. A refusal is the system protecting
your credibility, not a dead end: the reframe usually gets you the decision
you actually needed.

### Reading the provenance stamp

Under every answer you'll find a stamp like:

`translator: built-in parser · engine: descriptive · result hash: 3f9a… · data version: b5b6… · 2026-07-15T14:03:22+00:00`

- **translator** — how your wording became a query: the built-in parser, or
  `language model (validated), 132 ms` when a model translated it (the
  translation is checked against the registry, and the round-trip time is
  shown). If a model's translation couldn't be used, the stamp says so and the
  built-in parser's answer is shown instead.
- **engine** — which deterministic calculation produced the number.
- **result hash** — a fingerprint of the full deterministic artifact: values,
  tables, chart series, resolution, caveats, divergence, and data version. If
  two people's answers differ, compare hashes first.
- **data version** — a fingerprint of the underlying data, so you know exactly
  which snapshot the answer describes.
- An analyst sign-off in the Causal Studio appends `analyst-reviewed` with the
  date, recorded against that exact result hash.

The **Provenance** expander on each answer shows the resolved metric variant,
source, and the exact calculation that ran. The **Download answer (JSON)**
action exports all of it — intent, resolution, code, hashes, caveats,
divergence — so an answer can be audited or reproduced outside this tool.

### When to trust a number outright

- **Verified, no flags** — quote it. The definition, source, and calculation
  are all governed and disclosed.
- **Verified, with a divergence flag** — the number is correct under the
  governed default, but a registered alternate definition or source moves it
  materially. Open *Same question, different answer* before circulating; if
  the fork matters to your decision, escalate it to metric governance.
- **Hypothesis** — a designed estimate with computed assumption checks. Read
  the checks; anything flagged needs analyst judgment before the estimate is
  acted on.
- **Abstained** — there is no number. That is the answer.
""")
