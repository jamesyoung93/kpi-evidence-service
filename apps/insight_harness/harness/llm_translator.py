"""LLM translator: the drop-in that replaces rule-based parsing with a model.

Architecture contract (unchanged): the LLM's ONLY job is translation —
question -> structured Intent. It never answers, never computes, never touches
data. Its output is validated against the semantic-layer registry before any
engine runs: unknown metrics, dimension values, events, or classes are
rejected, and the pipeline falls back to the deterministic rule parser.

The translation itself becomes part of provenance: the raw model output and
the validation verdict are attached to the answer artifact, so even the parse
step is auditable.

Key handling: pass the key per-call (held in Streamlit session memory only)
or set ANTHROPIC_API_KEY in the environment. Nothing is written to disk.
"""
from __future__ import annotations

import json
import os

from . import baskets, semantic_layer as sl
from . import triage

DEFAULT_MODEL = "claude-sonnet-5"

VALID_CLASSES = {triage.RETRIEVAL, triage.DESCRIPTIVE, triage.DIAGNOSTIC,
                 triage.COHORT, triage.CAUSAL, triage.PREDICTIVE,
                 triage.OUT_OF_SCOPE}
RESPONSE_KEYS = {
    "question_class", "metric", "filters", "trend", "dim_breakdown",
    "event_id", "template", "window", "compare_basis", "basket_id", "reason",
}


class TranslationError(Exception):
    """kind='unavailable' (no key/SDK/API failure) or 'rejected' (registry
    validation refused the model's output). The pipeline renders a product-voice
    fallback message per kind; the raw text here is audit detail only."""

    def __init__(self, message: str, kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


def _registry_context() -> str:
    fact = sl.load_fact("source_a")
    dims = {d: sorted(fact[d].unique().tolist()) for d in sl.DIMENSIONS}
    metrics = {mid: {"label": m["label"],
                     "synonyms": [k for k, v in sl.METRIC_KEYWORDS.items() if v == mid]}
               for mid, m in sl.METRICS.items()}
    events = {eid: {"name": e["name"], "start": e["start"],
                    "scope": e["scope"], "metrics": e["metrics"],
                    "default_metric": e.get("default_metric", e["metrics"][0]),
                    "keywords": e["keywords"]}
              for eid, e in sl.EVENTS.items()}
    return json.dumps({"metrics": metrics, "dimensions": dims, "events": events,
                       "market_baskets": sorted(baskets.BASKETS),
                       "retrieval_templates": [
                           "whitespace", "top_accounts", "top_writers",
                       ]}, indent=1)


SYSTEM = """You translate a business question into a structured intent for a \
deterministic analytics engine. You do NOT answer the question. Respond with \
ONLY a JSON object, no prose, no markdown fences, with exactly these keys:

question_class: one of "Retrieval" | "Descriptive" | "Diagnostic" | "Cohort comparison" | "Causal" | "Predictive" | "Out of scope"
metric: a metric id from the registry, or null. For Causal questions about a registered event, set the metric the question asks about; if none is stated, use that event's registered default_metric.
filters: object mapping dimension name -> ONE registry value or an ARRAY of registry values (empty object if none)
trend: true if the question asks for a time series / by-month view, else false
dim_breakdown: for Diagnostic only, the dimension explicitly requested, or null
event_id: an event id from the registry if the question asks about the causal impact of that specific event, else null
template: for Retrieval only, "whitespace" (high-value accounts with no recent activity), "top_accounts" (generic TRx/NRx/NBRx ranking), or "top_writers" (the fixed governed top-15 trailing-NRx-share recipe), else null
window: null, or an explicit time window the question names: {"kind":"last_n","n":<int>} | {"kind":"quarter","q":<1-4>,"year":<yyyy>} | {"kind":"month","month":<1-12>,"year":<yyyy>}
compare_basis: null, or "prior_month" | "prior_quarter" | "yoy" when the question names a comparison basis for a change
basket_id: for an explicit TRx-share basket only, "il17_class" or "advanced_therapy"; otherwise null
reason: for Predictive / Out of scope / Causal-with-no-event, one sentence explaining the refusal, else ""

Classification rules:
- "why / what caused / impact of / ROI of" -> Causal. If it matches a registered event, set event_id; otherwise leave event_id null and give a reason suggesting the diagnostic reframe.
- "which X account for / break down / where did the change come from" -> Diagnostic.
- "list / find / top N / whitespace / no activity" -> Retrieval.
- "top 15 HCP writers by trailing-12-month NRx share" -> Retrieval with metric "nrx" and template "top_writers".
- a top-N NRx-share activity-mix question explicitly asking for matched peers -> Cohort comparison, metric "nrx".
- explicit IL-17-class or advanced-therapy TRx share -> Descriptive with metric "trx_share" and the registered basket_id.
- forecasts and predictions -> Predictive (always refused; say why in reason).
- a question that cannot be mapped to a registered metric or template -> Out of scope, with reason.
- Never invent metric ids, dimension values, or event ids not present in the registry.

Registry:
"""


def translate(question: str, api_key: str | None = None,
              model: str = DEFAULT_MODEL) -> tuple[triage.Intent, dict]:
    """Translate via the Anthropic API. Raises TranslationError on any failure;
    callers fall back to the rule parser. Returns (validated Intent, meta)."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise TranslationError("no API key provided")
    try:
        import anthropic
    except ImportError as e:
        raise TranslationError("anthropic SDK not installed (pip install anthropic)") from e

    try:
        client = anthropic.Anthropic(api_key=key, timeout=15.0, max_retries=1)
        msg = client.messages.create(
            model=model, max_tokens=500, temperature=0,
            system=SYSTEM + _registry_context(),
            messages=[{"role": "user", "content": question}],
        )
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:
        raise TranslationError(f"API call failed: {e}") from e

    return _validate(question, raw)


def _validate(question: str, raw: str) -> tuple[triage.Intent, dict]:
    """Registry validation: the LLM proposes, the semantic layer disposes."""
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        d = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise TranslationError(f"model returned non-JSON: {raw[:120]}", kind="rejected") from e
    if not isinstance(d, dict):
        raise TranslationError("model response must be one JSON object", kind="rejected")
    if set(d) != RESPONSE_KEYS:
        missing, extra = sorted(RESPONSE_KEYS - set(d)), sorted(set(d) - RESPONSE_KEYS)
        raise TranslationError(
            f"response keys must match the bounded contract; missing={missing}, extra={extra}",
            kind="rejected")
    if not isinstance(d["trend"], bool):
        raise TranslationError("trend must be a JSON boolean", kind="rejected")
    if not isinstance(d["reason"], str):
        raise TranslationError("reason must be a string", kind="rejected")

    # Check JSON types before registry membership: arrays and objects are
    # unhashable and must become a rejection, not escape as a TypeError.
    for field in ("metric", "event_id", "basket_id", "template",
                  "compare_basis", "dim_breakdown"):
        if d[field] is not None and not isinstance(d[field], str):
            raise TranslationError(f"{field} must be a string or null", kind="rejected")

    qc = d.get("question_class")
    if not isinstance(qc, str) or qc not in VALID_CLASSES:
        raise TranslationError(f"invalid question_class: {qc!r}", kind="rejected")

    metric = d.get("metric")
    if metric is not None and metric not in sl.METRICS:
        raise TranslationError(f"unregistered metric: {metric!r}", kind="rejected")

    filters = d["filters"]
    if not isinstance(filters, dict):
        raise TranslationError("filters must be an object", kind="rejected")
    fact = sl.load_fact("source_a")
    for k, v in filters.items():
        vals = v if isinstance(v, list) else [v]
        try:
            if k not in sl.DIMENSIONS or not vals or \
                    any(x not in set(fact[k].unique()) for x in vals):
                raise TranslationError(f"unregistered filter: {k}={v!r}", kind="rejected")
            filters[k] = sorted(vals) if len(vals) > 1 else vals[0]
        except TypeError as e:  # unhashable/unorderable values from the model
            raise TranslationError(f"malformed filter: {k}={v!r}", kind="rejected") from e

    event_id = d.get("event_id")
    if event_id is not None and event_id not in sl.EVENTS:
        raise TranslationError(f"unregistered event: {event_id!r}", kind="rejected")

    basket_id = d.get("basket_id")
    if basket_id is not None and basket_id not in baskets.BASKETS:
        raise TranslationError(f"unregistered market basket: {basket_id!r}", kind="rejected")
    if basket_id is not None and not (qc == triage.DESCRIPTIVE and metric == "trx_share"):
        raise TranslationError(
            "basket_id is valid only for Descriptive trx_share intents", kind="rejected")

    template = d.get("template")
    if template not in (None, "whitespace", "top_accounts", "top_writers"):
        raise TranslationError(f"invalid template: {template!r}", kind="rejected")
    if qc == triage.RETRIEVAL and template is None:
        raise TranslationError("Retrieval intents require a registered template", kind="rejected")
    if qc != triage.RETRIEVAL and template is not None:
        raise TranslationError("templates are valid only for Retrieval intents", kind="rejected")
    if template == "top_writers" and metric != "nrx":
        raise TranslationError(
            "top_writers requires metric 'nrx' and its governed NRx-share recipe",
            kind="rejected")

    window_spec = d.get("window")
    window = None
    if window_spec is not None:
        if not isinstance(window_spec, dict):
            raise TranslationError("window must be an object", kind="rejected")
        try:
            window, window_refusal = triage.resolve_window(window_spec)
        except (KeyError, ValueError, TypeError) as e:
            raise TranslationError(f"invalid window: {window_spec!r}", kind="rejected") from e
        if window_refusal:
            # faithful translation of an out-of-range window: the refusal is a
            # scoping decision, not a translation failure
            intent = triage.Intent(question, triage.OUT_OF_SCOPE, metric, filters,
                                   reason=window_refusal)
            return intent, {"translator": "llm", "raw": cleaned, "validated": True}

    basis = d.get("compare_basis")
    if basis not in (None, "prior_month", "prior_quarter", "yoy"):
        raise TranslationError(f"invalid compare_basis: {basis!r}", kind="rejected")

    dim_breakdown = d.get("dim_breakdown")
    if dim_breakdown is not None and dim_breakdown not in sl.DIMENSIONS:
        raise TranslationError(f"invalid dim_breakdown: {dim_breakdown!r}", kind="rejected")
    if (qc == triage.DIAGNOSTIC) != (dim_breakdown is not None):
        raise TranslationError(
            "Diagnostic intents require dim_breakdown and no other class may set it",
            kind="rejected")

    # enforce the same guardrails the rule parser enforces
    if qc in (triage.DESCRIPTIVE, triage.DIAGNOSTIC) and metric is None:
        raise TranslationError("descriptive/diagnostic intent without a metric", kind="rejected")
    if qc == triage.COHORT:
        metric = metric or "nrx"
        if metric != "nrx" or d["trend"] or window is not None or basis is not None:
            raise TranslationError(
                "Cohort comparison requires metric 'nrx' and the governed R3M recipe",
                kind="rejected")
    if event_id is not None and qc != triage.CAUSAL:
        raise TranslationError("event_id is valid only for Causal intents", kind="rejected")
    if qc == triage.CAUSAL and event_id is not None and metric is None:
        event = sl.EVENTS[event_id]
        metric = event.get("default_metric", event.get("metrics", ["trx"])[0])
    if qc == triage.CAUSAL and event_id is not None \
            and metric not in sl.EVENTS[event_id].get("metrics", []):
        raise TranslationError(
            f"metric {metric!r} is not registered for event {event_id!r}", kind="rejected")

    # the model never authors rendered refusal copy: for abstention classes the
    # curated product reason is substituted, and the model's own explanation is
    # kept in the translation meta for audit only
    refused = qc in (triage.PREDICTIVE, triage.OUT_OF_SCOPE) or \
        (qc == triage.CAUSAL and event_id is None)
    reason = triage.refusal_reason(qc) if refused else ""

    intent = triage.Intent(
        question=question, question_class=qc, metric=metric, filters=filters,
        trend=d["trend"], dim_breakdown=dim_breakdown,
        event_id=event_id, template=template,
        reason=reason, window=window, compare_basis=basis, basket_id=basket_id,
    )
    meta = {"translator": "llm", "raw": cleaned, "validated": True,
            "model_reason": d["reason"]}
    return intent, meta
