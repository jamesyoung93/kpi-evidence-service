# Individual writing context: verification, AI analytics, and refusal

*Research checked 12 July 2026. This note is editorial context for “Fluent and Wrong Looks Exactly Like Fluent and Right”, not a claim of exhaustive coverage.*

## Bottom line

The broad diagnosis is shared: fluent AI output is cheap, analytical work is unusually difficult to verify, governed context matters, and abstention has to be evaluated. The essay should not present those ideas as discoveries made in isolation.

The demo is still differentiated by the way it combines them. It makes the model an intent and orchestration layer rather than the evidentiary authority; validates intent before computation; uses deterministic engines; exposes material source and definition forks; records hashes and data versions; scores appropriate refusal; and carries the same evidence ladder through to causal design and human review. I found no individual account that puts that full combination into one running analytics demo.

## Closest individual precedents

| Author and work | Where it overlaps | What this demo adds | Editorial use |
|---|---|---|---|
| Hamel Husain, [“It’s Hard to Eval” Is a Product Smell](https://hamel.dev/blog/posts/eval-smell/) (29 June 2026) | The closest parallel. Husain opens with an AI data agent answering a net-revenue question and argues that the product should expose governed definitions, calculations, sources, assumptions and unverified items. | The harness constrains the system earlier: the model translates, a registry validates, deterministic engines compute, unsupported questions are refused, and artifacts carry hashes, versions, computed forks and causal tiers. | Acknowledge directly in the public essay. Position the demo as one narrower implementation of verification-first design, not as the origin of the principle. |
| Benn Stancil, [Can analysis ever be automated?](https://benn.substack.com/p/can-analysis-ever-be-automated) (1 August 2025) | Stancil names almost the same trust catch-22: charts and analysis are not self-validating, and checking them can require recreating the work. | The harness is an attempted product answer to that diagnosis: it reduces the cost of checking by attaching the resolved meaning, deterministic recipe, version, forks and stopping boundary to the result. | Cite once near the central verification argument. Make the movement from diagnosis to implemented response explicit. |
| Eugene Yan, [Product Evals in Three Simple Steps](https://eugeneyan.com/writing/product-evals/) (2025) | Advocates task-specific evaluation sets and binary pass/fail criteria; refusal is an example of an objectively testable output. | The demo turns correct refusal and reproducibility into visible analytics-product properties, not only developer-side evaluation criteria. | Useful support for the internal brief or a longer version of the refusal section. The current public essay does not need another eval citation. |
| Simon Willison, [Hallucinations in code are the least dangerous form of LLM mistakes](https://simonwillison.net/2025/Mar/2/hallucinations-in-code/) (2 March 2025) | Distinguishes errors that execution catches from polished mistakes in prose that have no automatic failure signal. | The harness tries to construct a compiler-like verification channel for analytical claims without pretending that execution alone proves the business definition is right. | Keep as supporting context. Benn Stancil makes the analytics-specific version more directly. |
| Chip Huyen, [Building LLM applications for production](https://huyenchip.com/2023/04/11/llm-engineering.html) (11 April 2023) | Treats LLM applications as composed systems with tools, control flow and evaluations rather than single prompts. | The demo specifies the authority boundary for analytics: language interpretation may be probabilistic, but evidence and calculation are registry-governed. | Appropriate in a technical appendix or internal architecture discussion; too general to add to the public essay now. |
| Michael Kaminsky, [Data Dies in Darkness](https://locallyoptimistic.com/post/data-dies-in-darkness/) (15 April 2018) | Connects trust to metric ownership, visible quality checks, monitoring and a correction flywheel. | The harness applies that organisational instinct at answer level through artifacts, flags, versioned evaluation history and visible governance changes. | Useful precedent for the accumulating-trust close, but adding it to the essay would make the final section feel referenced rather than personal. |
| Alex Petralia, [The left and the right hands of data](https://alexpetralia.com/2025/07/01/the-left-and-the-right-hands-of-data/) (1 July 2025) | Argues that analytical correctness depends on reconciliation with the user-facing source and business context, not only valid backend code. | The demo handles cases where more than one governed source or definition is legitimate and makes the resulting disagreement visible instead of forcing a single reconciliation. | Best used in discussion with analytics practitioners, or in an expanded piece about source and definition forks. |

## Repetition and differentiation

The highest repetition risk is the essay’s central diagnosis. Stancil already argues that automated analysis has a verification problem, and Husain now makes a closely related product-design case using the same kind of net-revenue example. The revised essay should therefore say plainly that the diagnosis is shared.

The differentiation begins where those accounts stop or take a different path:

- Husain proposes an inspectable, AI-generated notebook organised for expert review. This demo limits the model’s authority before calculation and can decline the question before an analytical engine runs.
- Stancil describes a verification catch-22. This demo tests whether structured evidence can reduce the cost of checking without claiming to eliminate it.
- General eval writing treats refusal as a label. This demo makes refusal a user-facing artifact with a reason, scope, alternative questions and a scored reliability history.
- Semantic-layer arguments usually seek one governed answer. This demo also exposes legitimate, material disagreement between registered sources and definitions.
- Causal-method writing distinguishes association from causation. This demo turns that distinction into product permissions, tiers, assumption checks and a manual review boundary.

The combination is the contribution: a small, inspectable demonstration of how these tensions could be handled together.

## Recommended integration

Keep the public essay sparse. The two added links to Stancil and Husain are enough: they acknowledge the nearest intellectual neighbours and sharpen the claim that this is a concrete response. Adding every adjacent source would turn a first-person build essay into a literature review.

Use the other sources in three ways:

1. Keep this note as the supporting reading map for internal discussion and future revisions.
2. Draw on Yan, Huyen and Willison when the audience wants implementation or evaluation detail.
3. Draw on Kaminsky and Petralia when the discussion moves from system mechanics to metric ownership, reconciliation and organisational trust.

The safest positioning sentence is: *This is not a claim that I found the one correct architecture for AI analytics. It is a working demo of how I would handle a set of tensions I expect a serious implementation to encounter.*
