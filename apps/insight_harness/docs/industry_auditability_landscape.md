# How close is the market to an auditable analytics answer?

*An audit log can show that a system produced an answer. Analytical auditability must show why that answer has the meaning, value and evidentiary status it claims.*

As of July 2026, I no longer think the architecture demonstrated by the Insight Harness is an isolated position. Several commercial systems are moving towards governed semantics, inspectable computation, evaluation and human review. The closest example I found is Komodo Health's Marmot. Others implement substantial parts of the same pattern.

This comparison is based on public vendor documentation rather than independent product testing. It considers proximity to the question-to-evidence problem in the demo, not overall product quality or market position. The companion [individual writing context](individual_writing_context.md) covers the related ideas being developed by practitioners rather than product vendors.

| Product | What the public evidence shows | What remains unclear |
|---|---|---|
| [Komodo Health Marmot](https://www.komodohealth.com/perspectives/building-the-framework-for-trusted-ai-in-healthcare/) | Structured intent, deterministic execution, visible code and patient-level evidence, versioned conversations, evaluation, abstention and review | Healthcare-specific; no public answer-artifact specification or detailed causal-assumption workflow |
| [Databricks Genie Agents](https://docs.databricks.com/aws/en/genie/monitor) | Inspectable and editable SQL, trusted queries and functions, review requests, monitored conversations and benchmark runs | No documented answer artifact pinned to both data and semantic-model versions |
| [Snowflake Cortex Analyst](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/verified-query-repository) | Generated SQL, request identifiers, semantic-model selection, verified queries, ambiguity handling and regression evaluation | Strong components, but no documented bundle joining them into one durable result |
| [Omni Agent](https://docs.omni.co/ai/chat) | Semantic queries, executable SQL, field definitions, tool context, audit logs and Git-backed models | [No built-in response-accuracy test is currently offered](https://omni.co/ai); agent self-checking is not external ground truth |
| [Hex](https://hex.tech/product/notebooks/) | Visible SQL and Python, a dependency graph, project history and versioned semantic definitions | An auditable workspace rather than a bounded answer-and-refusal contract |
| [Palantir AIP](https://www.palantir.com/docs/foundry/aip-observability/overview) | Execution history, function versions, distributed traces, inputs, outputs, prompts, tool calls and staged human review | A general workflow platform rather than a direct conversational-analytics product |
| [Power BI Copilot](https://learn.microsoft.com/en-us/power-bi/create-reports/copilot-ask-data-question) | Fields and filters used, source-visual citations, explicit unsupported questions and administrative audit records | Citations identify report content, not the full computation behind a result |
| [causaLens](https://causalens.com/system-of-work) | Causal models, provenance, human approval, guardrails and auditor-agent concepts | Limited public detail about estimator diagnostics, assumptions and answer-level versioning |

## The closest parallel

Komodo Health's Marmot is the most direct overlap. In an [architecture article published on 19 May 2026](https://www.komodohealth.com/perspectives/building-the-framework-for-trusted-ai-in-healthcare/), Komodo places an LLM planner on one side of a boundary and deterministic SQL, cohort logic and curated tools on the other. The response can expose the interpreted question, plan, executed queries, returned data, underlying code and patient-level evidence. Komodo also describes a virtual file system that versions and branches conversations, evaluation of accuracy, citation fidelity and abstention, and human review for high-stakes outputs. Its [product page](https://www.komodohealth.com/product/marmot/) says analytical steps and intermediate results persist and can be replayed.

That is close enough that I would cite it directly rather than imply that the planner-versus-computation boundary is novel. It also clarifies the claim I can responsibly make. I am showing, in a small system that can be inspected end to end, how I would resolve several tensions within an emerging category.

Databricks and Snowflake provide further evidence of convergence. [Databricks Genie](https://docs.databricks.com/aws/en/genie/monitor) lets users expose and edit generated SQL, promote reviewed logic into trusted assets, request human review and compare generated results with SQL ground truth. [Snowflake Cortex Analyst](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/rest-api) returns generated SQL and request identifiers and can withhold SQL in favour of suggestions when a question is ambiguous. Its [verified-query repository](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst/verified-query-repository) records the question, SQL, verifier and verification time, while its [evaluation framework](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-analyst-evaluations) supports repeatable comparison with expected answers.

Omni and Hex approach the problem through inspectable analytical workspaces. Omni's [Workbook Inspector](https://docs.omni.co/analyze-explore/workbook-inspector) exposes the semantic query, executable database SQL, filters and calculations; its live AI queries can be opened and inspected in a workbook. Hex keeps [SQL and Python inside a graph-based analytical notebook](https://hex.tech/product/notebooks/), with [history and versions](https://learn.hex.tech/docs/explore-data/projects/history-and-versions) supporting comparison and restoration.

There are also useful open-source and infrastructure precedents. [Wren AI](https://docs.getwren.ai/oss/reference/architecture) combines semantic context with text-to-SQL and inspectable query history. [Langfuse](https://langfuse.com/docs/observability/overview), [Phoenix](https://arize.com/docs/phoenix) and [MLflow](https://mlflow.org/docs/latest/genai/tracing/) can preserve model and tool traces; [OpenLineage](https://openlineage.io/docs/spec/facets/) and [dbt artifacts](https://docs.getdbt.com/reference/artifacts/dbt-artifacts) can preserve upstream data lineage. Those are valuable substrates, but they do not by themselves define the business-level contract for an analytical answer.

## Three different trails

These products reveal why "audit trail" needs qualification.

An **operational audit trail** records who invoked a system, when they did so, what resource was accessed and whether the action succeeded. Microsoft Purview, Omni and Palantir all provide facilities of this kind. It matters for security, compliance and incident investigation, but it does not establish that an analytical claim is correct.

An **execution trace** records prompts, selected tools, generated queries, inputs, outputs and errors. Palantir's [AIP observability](https://www.palantir.com/docs/foundry/aip-observability/trace-view) is an extensive example, while Omni exposes similar information within the analytical workspace. This helps an expert debug the system. It still does not establish that "revenue" had the intended definition or that the data state behind the answer can be reconstructed later.

An **analytical evidence trail** connects the interpreted question to governed definitions, selected sources, executable computation, data state, caveats and the exact result. That is the standard required to defend and reproduce a number. Marmot makes the strongest public commercial claim in this direction. Most other products expose some of the necessary layers without publicly documenting a single object that binds them together.

## What remains differentiated

In the public material I reviewed, I have not found a single user-facing workflow that combines all of the following: validated intent before execution; governed metric and source definitions with legitimate alternatives shown as visible forks; deterministic computation; an answer-level artifact carrying a readable trace, identifiers, data-version hash, evidentiary tier, computed caveats and result hash; evaluated scoped refusal; and a progression from descriptive measurement through decomposition to designed causal attribution with explicit assumption checks.

Each part has precedents. Their combination is the differentiation.

The visible forks matter in particular. Commercial semantic layers usually aim to select and enforce one governed definition. The demo also shows the numerical consequence of choosing another legitimate source or definition, without collapsing the distinction into generic uncertainty language. The causal rung matters for a similar reason. causaLens documents causal models and human oversight, but mainstream conversational-analytics products generally stop before assumption-governed attribution.

The market framing I would use is therefore modest but substantive: the industry increasingly agrees that language should plan and governed systems should calculate. The Insight Harness explores what follows when that principle is carried through to competing definitions, refusal, answer artifacts and causal boundaries.
