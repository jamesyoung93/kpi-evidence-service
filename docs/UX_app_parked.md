# Review app UX (parked)

Parked on 2026-10-19: the notebook package and its comparison report are the surface (see
`docs/ARCHITECTURE.md`). The pages described here are not part of the package; this file records the
design for when the review app is switched on (`docs/DEPLOY.md` section 11).

The surface must be visual and low code: a person supplies a report, confirms only uncertain metadata
and reviews exceptions. Nobody opens a notebook or writes SQL. The UI rules apply:
plain names with a tooltip on every status and metric, American spelling, top-to-bottom flow, sortable
tables, one idea per screen with the evidence in a drawer, "model-derived" (never apologetic) wording,
and status labels and tooltips taken from `contracts.STATUS_INFO`.

## Where the pages live

`kpi_evidence.app` holds the Streamlit pages; all logic stays in Streamlit-free modules (`intake`,
`pipeline`, `review`, `serving`, `exports`, `genie`). The pages run two ways:

* mounted in the vendored insight_harness app under a sidebar group **Report KPIs** (spec: "use the
  insight-harness Databricks App for review and publication");
* standalone: `streamlit run src/kpi_evidence/app/standalone.py` (or `kpi-evidence app`).

Backend: `KPI_EVIDENCE_BACKEND=local` (DuckDB file, pipeline runs in-process) or `databricks` (SQL
warehouse store; processing is triggered as a job run and the page polls the document status).
Identity: `st.context.headers["X-Forwarded-Email"]` on Databricks; `settings.reviewer_identity` or
`local-reviewer` locally. Reviewer authorization: the `reviewers` table (brand-scoped); in local
mode everyone may review.

## Pages (sidebar order)

### 1. Add a report
1. Drop zone (`st.file_uploader`: pptx, pptm, pdf, xlsx, png, jpg). Under it: "Or drop files in the
   approved folder: `<volume>/inbox/` - they are picked up automatically."
2. After upload the page shows one sentence: **"Kestrel - Monthly brand review - data through May 2026 -
   version 1"**. Confident fields are chips. Only fields listed in `needs_confirmation` are inputs
   (brand selectbox from `brands`, report-type selectbox, period month picker), with the proposal and its
   source as help text ("from the footer 'Data through May 2026'").
3. One primary button: **Check this report** (local: runs process + publish in-process with a spinner;
   Databricks: `jobs.run_now(job_id, job_parameters={"doc_id": ...})` then polls).
4. A compact "Recent reports" table: file, brand, period, status chip, counts by group, uploaded by.
Unsupported or encrypted files show the remedy sentence from `documents.error`.

### 2. Report check (one document)
* Document picker (most recent first).
* Four tiles in one row: **Matches platform** (VERIFIED), **Captured** (REPORT_ONLY + UNMAPPED_CAPTURED),
  **Needs review** (REVIEW group), **Blocked** (BLOCKED group). Each tile has a tooltip and filters the
  table below when clicked.
* KPI table (sortable): KPI, period, scope, report value, platform value, difference, status chip,
  published. Values are formatted by unit (counts with separators, fractions as percents with one
  decimal, usd with $).
* Selecting a row opens the **evidence drawer** (`st.dialog` or a right-hand expander):
  * a one-sentence story: "Report says 5,910 (slide 2, chart 'TRx by month', May 2026). Platform says
    5,909.8 with the governed definition (account feed, units). Difference 0.0%, within the 0.5%
    tolerance."
  * the slide/page schematic: an inline SVG of the page rectangle with every shape outlined and the
    source shape highlighted (from `document_pages.shapes` and the value's `locator.bbox`); title and
    nearby text under it;
  * for chart values, a re-plot: the report series vs the platform series from `platform_kpi_values`
    (Altair line/points; the selected point highlighted);
  * definition, unit, tolerance, owner; the exact SQL in a code block with a copy button and the
    exported `.sql` path; "every configuration tested" as a sortable table (choices, value, difference,
    error);
  * prior decisions for this layout, labeled **"Earlier decision (for information - does not approve
    this value)"**, with a "source or definition changed" badge when `still_valid` is false.

### 3. Review queue
* One exception per screen, "3 of 11", with filters (brand, status). Card title = status label, then
  the story sentence, then `reason_detail`.
* Evidence drawer as above.
* Decision row: **Accept**, **Correct**, **Reject**; rationale text area (required except accepting a
  match). Correct reveals "Corrected value" and "Correct KPI" inputs. After a decision: a toast with the
  outcome ("Published as Accepted variance", "Re-checked: now Matches platform") and the next card.
* A stable `decision_id` per rendered card (session state) so double clicks do nothing.

### 4. Published KPIs
* Coverage grid: goal-list KPIs x the last three expected periods; cells are state chips (Matches
  platform / Report-only / Accepted variance / Needs review / Not found / Stale / No access).
* Published values table (sortable) with evidence link (file + slide), approved by/at, definition.
* Downloads: scorecard CSV, Excel, PowerPoint.
* **Ask about approved results**: a text box that calls Genie on Databricks (`genie.GenieClient.ask`);
  locally it says Genie is available when deployed and offers example questions answered from the
  published table with plain filters.

### 5. Setup
Tabs: Brands (aliases), Goal list (KPI x brand x cadence), KPI catalog (read-only definition and SQL;
editable label, synonyms, owner and tolerance within the registry's allowed band, saved as a draft
version), Reviewers. Drafts list with **Approve** for registry owners. A banner explains that SQL
changes are pull requests to `registry/kpis/`.

### 6. Quality
Data-quality results per governed view, the latest run log (stage, status, counts, errors), and the
latest evaluation (extraction accuracy, mapping accuracy, false-validation rate, coverage, reviewer
workload) with a **Run evaluation** button in local mode.

## insight_harness integration (minimal patches)

* `harness/published.py` reads `published_kpis_current` (local: the kpi_evidence DuckDB file; Databricks:
  warehouse) and attaches `extras["report_evidence"]` to descriptive / basket-share answers whose metric,
  variant and scope match a publication exactly. Cached by publication version.
* `views/common.py` renders a "Report evidence" block under the answer ("May 2026 brand deck, slide 2:
  5,910 - platform 5,909.8 - Matches platform - approved by ... on ..."); tiles show a small chip.
  Report status chips never reuse the harness "Verified" tier chip.
* `app.py` mounts the Report KPIs pages when `KPI_EVIDENCE_REVIEW_ENABLED` is true (default true
  locally).
* The 333 upstream tests keep passing; new tests cover the seam (empty publication table changes no hash;
  a matching publication adds the evidence block).
