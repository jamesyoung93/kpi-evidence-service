# KPI registry

One YAML file per KPI in `kpis/`: definition, unit, dimensions, calculation, permitted comparison
choices, tolerance, owner and approval version. `kpi_evidence.registry` loads and lints it; the
bootstrap job mirrors it to the `kpi_registry` Delta table for SQL and Genie. Changes, including
calculation logic (`platform:` blocks: SQL templates, options, checks), are made by pull request.

## Template conventions

Two placeholder syntaxes, rendered in this order:

1. **Bindings** `${name}` - filled by `kpi_evidence.compile` *before* reconloop sees the SQL. They
   come from the observation and settings and are never searched by the judge:
   * `${gov}` / `${platform}` / `${restricted}` - schema prefixes for the active backend
   * `${brand}` - the brand id as an escaped SQL literal, e.g. `'KESTREL'`
   * `${window}` - period predicate over `month_start` / `month_end` relative to `{as_of}`
     (month, quarter, r3m, r6m, r12m, ytd)
   * `${scope}` - `region = 'West' AND ...` from allow-listed `dimensions`, or `TRUE`
2. **reconloop placeholders** `{as_of}` and `{<param>}` - `as_of` is the measurement period end;
   each `params` entry is a declared definitional choice with a governed `default`.

`permitted` options are definitions a report may state explicitly (for example "IL-17 class
share"). When the report text states one, it is evaluated first and can be VERIFIED as stated.
Any other option can only *explain* a gap (INCOMPATIBLE_DEFINITION), never verify it.

`platform: none` declares a report-only KPI: no platform source exists, so a confident extraction is
REPORT_ONLY (captured, not independently verified).

## Short form (most KPIs)

A simple aggregate KPI needs three lines; the loader expands them into the full template, the
`period_has_rows` check and `source_views`:

```yaml
platform:
  view: gv_rx_account_monthly
  value: SUM(calls)
```

* `view` is a governed view in `${gov}`, or `"{source}"` with a param group.
* `value` is the aggregate; use `numerator:` and `denominator:` instead for a ratio of sums.
* Optional keys: `where` (for example `"{eligibility}"`), `brand_column` (`null` means no brand
  filter), `date_expr` (the row date SQL for non-governed tables), `scope_columns` (for example
  `{region: region_name}`), `param_groups` (for example `[rx_source]`), `params` and `checks`.

Shared parameters live in `param_groups.yaml` (for example `rx_source`: account feed vs projected
panel). The definition hash covers the *expanded* calculation, so moving between the short and full
forms does not invalidate earlier results. Use the full form (`template:` / `ratio:`) when the SQL is
not a single aggregate over one relation (for example `exfactory_units`, `call_attainment`).

## Goal list

`goal_list.yaml` lists, per brand, the KPIs that reports are expected to cover:

```yaml
goal_list:
  - {brand_id: KESTREL, kpi_id: trx, cadence: monthly, expected_lag_periods: 1, expected_report_types: [monthly_brand_review, executive_summary]}
```

* `cadence` is `monthly` or `quarterly` (one value; anything else counts as monthly): how often a report
  is expected, independent of the KPI's `grains`. A KPI reported monthly and quarterly gets
  `cadence: monthly` and grains `[month, quarter]`.
* `expected_lag_periods` is counted in cadence periods (1 = last month's or last quarter's value).
* `expected_report_types` are keys of `report_types.yaml`; they are shown in the coverage view and not
  linted.
* `scope` (for example `{region: West}`) and `owner` are optional; the owner defaults to the KPI's owner.

The lint requires the KPI and the brand to exist. The registry sync writes the list to the `kpi_goal_list`
state table. The list feeds coverage only: the coverage states (`FOUND`, `NOT_FOUND`, `STALE`,
`NO_ACCESS`) in the evaluation (`kpi_evidence.eval.metrics.goal_list_coverage`) and the
`kpi_status_overview` view (one row per goal-list KPI). It does not change how a KPI is mapped or
checked. A new line changes the evaluation's expected coverage (maintainer guide, section 6.1, step 5).

## Report-type conventions

A report type may declare definitions it always uses, so its numbers are checked as stated instead of
being flagged "definition differs" every month. In `report_types.yaml`:

```yaml
report_types:
  - report_type: executive_summary
    assumed_definitions:
      trx: {source: panel}
```

The lint requires each assumed option (here `panel`) to be a permitted option of the KPI. These are
owner decisions in Git, never inferred from data. Text the report itself states wins over an
assumption.

## KPI table from a notebook (hints)

Analysts can also pass a KPI table of *hints* to `kpi_evidence.ReportCheck.set_kpis(...)` without
touching this registry. Each row names a KPI and, optionally, candidate tables, a SQL expression and a
date column. Hint KPIs are draft definitions validated against the reports; once a hint is validated,
promote it here by pull request.
