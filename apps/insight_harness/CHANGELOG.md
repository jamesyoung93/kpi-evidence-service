# Changelog

## 2026-07-17 — Persona-aware presentation voice

- Added one pure presentation voice layer for Sales Rep, District Manager,
  Brand Marketing, Market Access, and Executive wording. The adapters
  humanize scopes, metrics, variants, territory names, table columns,
  movements, definition differences, cohort findings, refusals, and zero
  states without mutating analytical artifacts.
- Reframed Home, Digest, Monitoring, cohort, tile, and Ask surfaces around
  persona-relevant decisions. Raw registry syntax and snake-case identifiers
  stay in machine contracts; user copy uses natural scope and display names.
- Expressed differences between share definitions in percentage points,
  promoted the cohort's leading activity gap above match methodology, and
  moved ranking formulas and evidence hashes behind explicit detail controls.
- Added five-persona rendering fixtures, copy lints, artifact/hash invariance
  tests, and a rendered Home/Digest/Monitoring persona walk. The optional
  language-model narrator remains a validated rewrite of the human template;
  all functionality continues to work without an API key.

## 2026-07-15 — Round 2: ranking integrity and decision depth

- Applied one explicit, padded visible-data y-domain to compact tile sparklines
  and expanded answer charts, using all finite primary and reference values so
  small movements do not appear flat until zoomed.
- Replaced anomaly ranking with disclosed priority score v2: 45% standardized
  movement, 20% relative movement, and 35% business scale. Added low-base
  guards, overlapping-row clustering, and registered details/attainment story
  grouping.
- Added executive-language digest headlines, category labels, 12-point
  endpoint-marked sparklines, evidence stamps, full-size dialogs, and a compact
  governed top-three strip on Home.
- Added large-format tile and answer dialogs, modal-local analysis controls,
  national-to-territory breadcrumbs, territory-level synthetic-NPI retrieval,
  a Top writers tile, and drag reorder through pinned
  `streamlit-sortables==0.3.1` with the tested buttons retained as fallback.
- Added immutable IL-17-class and advanced-therapy basket definitions,
  member-to-denominator reconciliation, adoption-stage adaptive defaults,
  explicit overrides, and basket identity in governed artifacts.
- Added an observed-only referral source at deterministic 80% HCP coverage,
  incoming-referral and active-referrer metrics/tiles, computed scope
  completeness, and unknown-preserving aggregation for uncovered HCPs.
- Added the deterministic, Directional top-20 NRx-share cohort comparison:
  disclosed volume floors, exact region/specialty/decile-band peer matching,
  R3M activity profiles, referral-coverage caveats, stable recipe/result hashes,
  grouped comparison output, and Causal Studio handoff. The built-in Ask route
  recognizes “Compare the activity mix of top 20 HCPs by NRx share with matched
  peers.”
- Expanded contract, golden, determinism, and headless Streamlit coverage for
  the new ranking, scale, dialog/drill, basket, referral, and cohort paths.
  Release candidates still require the full regeneration, compilation, and
  test checks; release completion also requires a clean-session hosted smoke
  test after the deployment refreshes.

## 2026-07-15 — Phase 0: polish and chrome

- Minimized Streamlit chrome and reduced the sidebar to navigation, data version,
  translator status, and a collapsed optional-model connector.
- Reworked governed answers into bordered artifact cards with a reusable hero,
  compact icon feedback, one download menu, and the result hash in the stamp.
- Tightened diagnostic waterfall marks and separated divergence from caveat
  disclosure with its own glyph.
- Made Monitoring denser and more legible at a 1.6 default sensitivity, and made
  Reliability auto-run once per data version with a four-stat scorecard.
- Preserved deterministic computation, provenance, scoped refusals, model
  validation, deployment opt-in, session quotas, and the fully functional
  zero-key path.
