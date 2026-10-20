# Trusted Analytics with Language Models: Pilot Capabilities, Boundaries, and Tensions

## Recommendation

I recommend a scoped internal pilot in which the language model serves as the intent and orchestration layer inside a governed analytical system, not as the evidentiary authority. I built the harness to demonstrate how I would handle likely tensions, not to claim product readiness. The pilot should test whether governed definitions, deterministic computation and designed refusal turn natural-language access into verifiable answers.

I would test one domain with named metric owners and users. Success would mean faster retrieval, measurement and decomposition without losing the ability to inspect the source, definition, computation and data version. Causal claims should advance only through a reviewable study design. Unsupported questions should end in useful refusals rather than plausible prose.

## Capability model for the pilot

| Rung | Safe capability | Evidence the pilot must expose | Fallback when unsupported |
|---|---|---|---|
| Retrieval | Find records that meet explicit criteria | Returned rows, applied filters, source grain, result hash | Decline when the required grain or field is unavailable |
| Descriptive measurement | Compute governed levels, trends, and comparisons | Metric definition, source, period, alternate-definition or source forks | Name the covered range or metric registry instead of substituting silently |
| Diagnostic decomposition | Show where a measured change sits across dimensions | Contributions that reconcile to the total, comparison basis, “where, not why” warning | Offer a narrower governed breakdown; do not invent a causal explanation |
| Causal attribution | Propose and estimate a registered quasi-experimental design | Treated and control scopes, pre/post windows, assumption checks, sensitivity, analyst review | Refuse the causal claim and offer descriptive or diagnostic analysis |

Forecasting remains outside the ladder until a governed model has measurable error history.

## Required capability stack

The pilot requires more than a conversational front end. It needs a governed semantic and source registry with owners, named variants, known limitations, and configurable materiality; an intent contract that separates language interpretation from execution; deterministic retrieval, aggregation, decomposition, and causal-design engines; and an answer artifact that carries the resolved metric, variant, source, computation trace, caveats, result hash, and data version. It also needs scoped refusal copy with actionable reframes, a standing evaluation set that scores correct refusals and reproducibility, feedback telemetry, and a review path for causal work.

My proposed defaults address the main tensions directly. **Trust beats coverage:** unsupported forecasting, unregistered metrics, and causal questions without a registered event are declined. **Language is flexible; computation is constrained:** the language model may translate a question into structured intent, but governed code calculates the answer. **Disagreement is evidence:** the governed default remains primary while material source and definition forks are shown. **Decomposition precedes attribution:** “where” is answered arithmetically; “why” requires a design and human review. **Expansion is earned:** repeated refusals become candidates for new governed capability, not reasons to relax controls.

This direction is consistent with market movement, although it does not validate this particular design. [Looker Conversational Analytics](https://docs.cloud.google.com/looker/docs/conversational-analytics-overview?hl=en) documents semantic grounding and verified golden queries; [Databricks Genie Agents](https://docs.databricks.com/aws/en/genie/concepts) use trusted assets and benchmarks, with [generated SQL, filters, and sources available for inspection](https://docs.databricks.com/aws/en/genie/talk-to-genie); and [Power BI Copilot summaries cite the report visuals they use](https://learn.microsoft.com/en-us/power-bi/explore-reports/copilot-pane-summarize-content). [Tableau Explain Data](https://help.tableau.com/current/pro/desktop/en-us/explain_data_explained.htm) draws a complementary boundary by describing its explanations as correlational rather than causal and allowing a no-explanation result. The convergence is toward natural language plus governed context, testing, traceability, and explicit limits, not language fluency alone.

## Demonstrated capability versus production readiness

My current harness demonstrates the architecture on generated data. The generator injects known effects and source pathologies so estimates, divergence, and refusals can be scored; this is useful evaluation infrastructure, not production evidence. The standing set contains 17 questions, including five whose correct result is abstention. It demonstrates deterministic calculations, artifact export, result reproducibility, source and definition forks, scoped refusals, and a difference-in-differences proposal with computed checks.

The language-model translator is optional; without an Anthropic key the built-in rules parser runs, and no live-model quality claim follows from the demo. There is no access-control layer, no governed forecasting model, no production data connector, and no evidence yet on adoption, decision quality, latency under load, operating cost, or behavior with changing enterprise data. Analyst sign-off records both result hash and data version, but the current interface looks up review state by result hash alone. A numerically unchanged result could therefore inherit an in-session review badge across data versions; production use requires a compound hash-and-version key and a durable review store.

## Decisions required to start

I would start with a time-boxed pilot in one domain, with an accountable business sponsor, metric owners, analytics engineering, security, and an analyst reviewer. Before work starts, I would require decisions on the duration, supported rungs, governed metrics, permitted sources, and whether a registered event is suitable for causal design. I would keep each set deliberately small enough to own and audit, then build the pilot evaluation set from real user questions before opening access, including explicit refusal cases and known source or definition conflicts.

I would gate the pilot on complete provenance for every numerical answer, 100% reproducibility for identical inputs and configuration, 100% correct refusal on the high-risk refusal set, and documented review of every wrong or disputed result. I would keep forecasting, autonomous actions, externally published causal claims, and broad employee rollout out of scope. A production recommendation should require live-data evaluation, identity and row-level authorization, the sign-off-key fix, operational monitoring, ownership for registry changes, and evidence that users make faster decisions without accepting unverified answers.
