# The KPI table (optional hints)

A KPI table tells the check **where you think each KPI lives in the warehouse and how you think it is
computed**. Nothing in it is trusted. Each row is compiled into SQL, run against every period the reports
show, and the comparison report says whether the hint reproduces the reports:

* **validated**: every compared period matches under the row's primary definition;
* **partly validated**: some periods match, others do not;
* **matches alternative**: only another candidate table or an alternative expression reproduces the reports
  (also when the primary definition cannot run);
* **not validated**: compared, but the gaps are not explained;
* **not computable**: the SQL could not run (missing table or column, no access, no data). The error is shown;
* **report only**: no table was given. Report values are captured with their source location;
* **not compared**: no report value was mapped to this KPI.

Use it from a notebook:

```python
import kpi_evidence as ke
check = ke.ReportCheck(spark)
check.add_reports("/Volumes/main/brand/reports/2026/")
check.set_kpis("/Volumes/main/brand/kpi_table.xlsx")   # returns the rows and any validation messages
result = check.run()
```

or directly: `kpi_evidence.hints.load_kpi_table(...)`, `parse_hints(...)`, `merge_registry(...)`.

The example files `kpi_table_example.csv` and `kpi_table_example.xlsx` hold the same table. They point at
the synthetic demo platform, so `ReportCheck.demo()` runs them against the bundled sample reports.
The example gives TRx, NRx and TRx share priority 1 and the call and sample KPIs priority 2.
To rebuild the workbook from the CSV, run
`save_kpi_table(load_kpi_table("samples/kpi_table_example.csv"), "samples/kpi_table_example.xlsx")`
(both in `kpi_evidence.hints`).

## Columns

Column names ignore case, spaces and underscores (`Value SQL`, `value_sql` and `VALUE SQL` are the same
column). A few other names are accepted: `name` for `kpi`; `sql`, `expression` or `measure` for `value_sql`;
`date` or `period_column` for `date_column`; `where` or `filter` for `filters`. Blank cells are fine. List
cells use `;` between items.

| Column | Needed | What to put in it |
|---|---|---|
| `kpi` | always | The KPI's name or id. The id is the name in lower_snake_case (`TRx share` becomes `trx_share`). When the id, or the name, is a KPI in the governed registry, the row **redefines how that KPI is computed for this check**: the registry's label, description and report synonyms are kept, and only the table, SQL, unit and tolerance come from the row. Otherwise the row creates a new KPI. |
| `aliases` | no | Labels as printed in the reports, separated by `;` (for example `TRx; Total prescriptions`). The name itself always counts. A label claimed by an earlier row stays with that row. |
| `table` | for a platform check | One or more fully qualified tables or views, separated by `;`. The **first is the primary**; the others are candidates. A match on a candidate is reported as *matches alternative*, never *validated*. |
| `value_sql` | one of | One aggregate expression that gives the KPI for a period, for example `SUM(trx_units)` or `SUM(calls) / NULLIF(SUM(call_plan), 0)`. |
| `numerator`, `denominator` | one of | Two aggregate expressions for a ratio of sums (for example market share). Use these or `value_sql`, not both. |
| `date_column` | for a platform check | The column holding each row's date or month. |
| `date_format` | no | `date` (default: a DATE, TIMESTAMP or `YYYY-MM-DD` text), `yyyy-MM` (text such as `2026-05`) or `yyyyMM` (text or a number such as `202605`). |
| `filters` | no | A fixed condition added to every query, for example `channel = 'RETAIL'`. It is wrapped in parentheses, so `OR` is safe. |
| `brand_column` | no | The column to filter by the report's brand. The check filters on the brand id (for example `KESTREL`). If your table spells the brand differently, leave this blank and put the condition in `filters` instead (`product_name = 'Kestrel 50mg'`). Leave it blank when the table holds one brand only. |
| `scope_columns` | no | How report scopes map to columns: `region=region_name; territory=terr_id`. Allowed scopes: region, district, territory, specialty, payer_channel. A bare `region` means the column is also called `region`. The check also reads the distinct values of these columns, so it can recognize "West" in a chart. |
| `unit` | no | `count` (default), `usd`, or `percent` / `fraction`. Percent KPIs are compared as fractions: a report's 84.4% is 0.844, so the SQL must return 0.844, not 84.4. |
| `grain` | no | The period types this definition is valid for: `month; quarter; r3m; r6m; r12m; ytd`. Default: all of them (or the registry's list when the row redefines a registry KPI). |
| `tolerance` | no | How close counts as a match: `0.5%` (relative), `0.05pp` (percentage points, for percent KPIs), `abs:12` (absolute units). Combine with `;` (the largest wins). Default: 0.5% for counts and dollars, 0.05 points for percents. The printed rounding of a report value is always allowed on top. |
| `alternatives` | no | Other expressions to test as explanations: `writers_only=SUM(...) / NULLIF(SUM(...), 0); other=...`. Works with `value_sql` only. An alternative that reproduces a report gives *matches alternative*. |
| `brand` | no | Restricts the row to one brand. Repeat a KPI with different brands to give each brand its own definition; a check uses the rows of its brand. The brand also helps fill a report's brand when the file does not say it. |
| `priority` | no | A whole number, 1 = highest. `check.run(max_priority=1)` compares only the priority-1 KPIs (other numbers are still extracted and mapped), and reports list higher-priority KPIs first. Blank means lowest. |
| `owner`, `notes` | no | Who to ask, and anything worth knowing. Both are carried into the report. |

### What is checked before anything runs

Hint SQL runs with **your own permissions**, but every fragment is still checked: no `;`, no comments, no
braces, no backslashes, no quote inside quoted text (`'O''Brien'`), and no statement keywords such as
DROP, DELETE, INSERT, UPDATE, MERGE, CREATE, GRANT or CALL (words inside quoted text are fine, so
`channel = 'CALL CENTER'` is allowed). Tables must be plain names such as `catalog.schema.table`, and
columns must be plain names. A row with an error is **skipped and listed with the reason**. A row with a
warning (an unknown scope name, a table without a schema) is used. Nothing is dropped silently.

## The example rows

The example is written against the demo platform's raw tables (`platform_demo.rx_account_monthly`,
`platform_demo.rx_panel_monthly`, `platform_demo.referrals_monthly` and the restricted
`platform_restricted.exfactory_monthly`). They store the month as text such as `2026-05` (hence
`date_format = yyyy-MM`) and have no brand column. The verdicts below are what the sample reports produce.

The two field updates (`kestrel-update.pptx` and `Kestrel_Field_Update_scan.pdf`) do not state their
period clearly, so a plain `ReportCheck.demo().run()` lists them under issues as awaiting confirmation and
leaves the update's values out. The table describes the run after
`check.confirm("kestrel-update.pptx", period="June 2026")` and
`check.confirm("Kestrel_Field_Update_scan.pdf", period="May 2026")`; the KPI verdicts are the same either
way.

| Row | KPI | What the row says | What the sample reports show |
|---:|---|---|---|
| 1 | `trx` | Total prescriptions = `SUM(trx_units)` from the account feed, with the retail panel as a second candidate table. Region, district, territory, specialty and payer channel map to same-named columns. Tolerance 0.5%. | **Partly validated.** The monthly deck's TRx trend (Dec-25 to May-26) and its KPI table match the account feed. The executive summary's "TRx (retail panel)" for Q2 covers April and May only: the panel has a one-month lag and no June, so neither candidate reproduces the quarter and that row goes to review. |
| 2 | `nrx` | New prescriptions = `SUM(nrx)` from the account feed. | **Partly validated.** The NRx by region chart matches. The headline May NRx agrees with the platform, but the monthly deck and the executive summary print different May values, so both are flagged *reports disagree*. |
| 3 | `nbrx` | New-to-brand prescriptions = `SUM(nbrx)`. | **Validated.** May NBRx 775 matches within the printed rounding. |
| 4 | `trx_share` | Market share as a ratio: `SUM(trx_units)` over `SUM(advanced_therapy_trx)`, a percent with a 0.05-point tolerance. | **Partly validated.** "TRx share 9.1%" matches (deck and executive summary). The deck's "IL-17 class share 26.1%" and the update's unqualified "Share 26.2%" use a different market basket, which this row does not declare, so they show *variance to review*. |
| 5 | `call_attainment` | Calls over planned calls, `SUM(calls) / NULLIF(SUM(call_plan), 0)`, for HCPs with a plan (`filters: call_plan > 0`), with an alternative `writers_only` that counts only HCPs who wrote the brand. | **Partly validated.** The four regional values match. The deck's KPI table prints "Call attainment (writers only) 95.8%", which matches only the `writers_only` alternative, so that row shows *definition differs*. |
| 6 | `calls` | `SUM(calls)`, with a **deliberately wrong first table** (the panel has no `calls` column) and the account feed as the second candidate. | **Matches alternative** in all three reports. The primary table cannot run, and the report says so; the second candidate reproduces every value. This is how the table tells you which candidate works. |
| 7 | `samples` | `SUM(samples)`, tolerance `abs:5` (five units). | **Validated.** The re-issued deck's 2,611 matches. The first issue's overstated 2,768 belongs to a superseded document. |
| 8 | `new_writers` | `SUM(new_writers)`; the first data month has no history and is empty, never zero. | **Validated.** May new writers match. |
| 9 | `referrals_in` | Observed referrals, `SUM(referrals_in)` from the referral feed. | **Not validated.** The deck prints *projected* referrals (210 against 168 observed). Adding `alternatives: projected=SUM(referrals_in) / 0.8` would turn this into *matches alternative*. |
| 10 | `exfactory_units` | `SUM(units_shipped)` from the restricted supply-chain table, filtered by its `brand` column, tolerance `abs:12`. | **Not computable.** The demo identity has no access to that schema, so the report shows *no access to platform data* with the error. |
| 11 | `brand_awareness` | No table: a survey result, percent, valid for months and quarters. | **Report only.** The Q1 tracker value (62%) is captured with its slide location. |
| 12 | `share_of_voice` | No table: a promotion-audit result, percent. | **Report only.** May share of voice (18%) is captured. |
| 13 | `persistency_12m` | No table: a vendor study, percent, valid for quarters and months. | **Report only.** The Q1 value (71%) is captured. |
| 14 | `active_referrers` | `SUM(active_referrers)` from the referral feed. | **Not compared.** No sample report prints active referrers. The row is valid, and the report lists it so you can see it was never tested. |

KPIs in the governed registry that the table does not mention (for example speaker attendance) are still
checked with their registry definition when the registry is used, and the report labels them
"registry" rather than "hint". Every example row names a registry KPI, so in `ReportCheck.demo()` the
rows replace those registry definitions; `ReportCheck.demo(kpi_table=False)` checks the same reports with
the registry definitions alone, which shows the two side by side.
