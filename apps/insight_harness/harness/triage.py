"""Question triage: classify, parse, route.

Incoming questions are classified into {RETRIEVAL, DESCRIPTIVE, DIAGNOSTIC,
COHORT, CAUSAL, PREDICTIVE, OUT_OF_SCOPE} and parsed into a structured Intent that the
deterministic engines consume.

This rule-based parser is the default translator and the fallback whenever a
language-model translation fails registry validation. Both translators emit
the same Intent contract, so everything downstream is translator-agnostic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import semantic_layer as sl

RETRIEVAL, DESCRIPTIVE, DIAGNOSTIC, COHORT, CAUSAL, PREDICTIVE, OUT_OF_SCOPE = (
    "Retrieval", "Descriptive", "Diagnostic", "Cohort comparison", "Causal",
    "Predictive", "Out of scope")

_CAUSAL = re.compile(r"\b(why|caused?|drove|drive[sn]?|impact of|effect of|because of|due to|roi of)\b", re.I)
_DIAGNOSTIC = re.compile(
    r"\b(account for|break ?down|decompos|which regions?|"
    r"which specialties|which territories|which districts|which payer channels|"
    r"contribut|where did)\b", re.I)
_RETRIEVAL = re.compile(r"\b(list|find|show me the accounts|which accounts|top \d+|whitespace|no activity)\b", re.I)
_TOP_WRITERS = re.compile(
    r"(?=.*\btop\s+15\b)(?=.*\b(?:hcp\s+writers?|hcps?)\b)"
    r"(?=.*\bnrx[\s-]+share\b)",
    re.I,
)
_PREDICTIVE = re.compile(r"\b(forecast|predict|will|next quarter|next year|project(ed|ion)?)\b", re.I)
_TREND = re.compile(r"\b(trend|over time|by month|monthly|last \d+ months)\b", re.I)
_COHORT = re.compile(
    r"(?=.*\btop\b)(?=.*\bnrx[\s-]+share\b)"
    r"(?=.*\b(?:peer|match|compar|activity|mix|profile|different|gap))",
    re.I,
)
_BASKET_PATTERNS = {
    "il17_class": re.compile(r"\bil\s*[- ]?17(?:\s+class|\s+basket|\s+market)?\b", re.I),
    "advanced_therapy": re.compile(r"\badvanced[- ]therapy(?:\s+market|\s+basket)?\b", re.I),
}

# explicit time windows
_W_LAST_N = re.compile(r"\blast (\d{1,2}) months?\b", re.I)
_W_QUARTER = re.compile(r"\bq([1-4])\s+(\d{4})\b", re.I)
_MONTH_NAMES = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
_W_MONTH = re.compile(r"\b(" + "|".join(_MONTH_NAMES) + r")\s+(\d{4})\b", re.I)

# comparison bases for change questions
_BASES = {
    "yoy": re.compile(r"\b(same month last year|year over year|yoy|vs\.? last year|versus last year)\b", re.I),
    "prior_month": re.compile(r"\b(prior month|previous month|month over month|mom|vs\.? last month)\b", re.I),
    "prior_quarter": re.compile(r"\b(prior quarter|previous quarter|quarter over quarter|qoq|vs\.? last quarter)\b", re.I),
}
BASIS_LABELS = {"prior_month": "vs prior month", "prior_quarter": "vs prior quarter",
                "yoy": "vs same month last year"}


@dataclass
class Window:
    """A validated, clamped time window: always a non-empty list of available
    months, with a label the headline can disclose."""
    kind: str            # "last_n" | "quarter" | "month"
    months: list         # resolved YYYY-MM strings, all available
    label: str


def resolve_window(spec: dict | None) -> tuple[Window | None, str | None]:
    """Resolve a window spec against the available months. Returns
    (window, None) on success, (None, reason) when the window is entirely
    outside the covered range — that reason becomes a scoped refusal, because
    silently substituting months would misdescribe the answer."""
    if not spec:
        return None, None
    avail = sl.months()
    kind = spec.get("kind")
    if kind == "last_n":
        n = int(spec.get("n", 0))
        if n < 1:
            return None, None
        months = avail[-n:]
        label = f"last {n} months" if n <= len(avail) else \
            f"last {n} months (all {len(avail)} available)"
        return Window("last_n", months, label), None
    if kind == "quarter":
        year, q = int(spec["year"]), int(spec["q"])
        if not 1 <= q <= 4:
            raise ValueError(f"invalid quarter: {q}")
        wanted = [f"{year}-{m:02d}" for m in range(3 * q - 2, 3 * q + 1)]
        base_label = f"Q{q} {year}"
    elif kind == "month":
        month = int(spec["month"])
        if not 1 <= month <= 12:
            raise ValueError(f"invalid month: {month}")
        wanted = [f"{int(spec['year'])}-{month:02d}"]
        base_label = wanted[0]
    else:
        raise ValueError(f"unknown window kind: {kind!r}")
    months = [m for m in wanted if m in avail]
    if not months:
        return None, (f"The governed data covers {avail[0]} through {avail[-1]}; "
                      f"{base_label} is outside that range.")
    label = base_label if len(months) == len(wanted) else \
        f"{base_label} (partial: {months[0]}–{months[-1]} available)"
    return Window(kind, months, label), None


def _window_spec(q: str) -> dict | None:
    if m := _W_LAST_N.search(q):
        return {"kind": "last_n", "n": int(m.group(1))}
    if m := _W_QUARTER.search(q):
        return {"kind": "quarter", "q": int(m.group(1)), "year": int(m.group(2))}
    if m := _W_MONTH.search(q):
        return {"kind": "month", "month": _MONTH_NAMES[m.group(1).lower()],
                "year": int(m.group(2))}
    return None


def _find_basis(q: str) -> str | None:
    for basis, rx in _BASES.items():
        if rx.search(q):
            return basis
    return None


@dataclass
class Intent:
    question: str
    question_class: str
    metric: str | None = None
    filters: dict = field(default_factory=dict)   # dim -> value or [values]
    trend: bool = False
    dim_breakdown: str | None = None
    event_id: str | None = None
    reason: str = ""                              # populated when OUT_OF_SCOPE
    template: str | None = None                   # retrieval template id
    window: Window | None = None                  # validated explicit window
    compare_basis: str | None = None              # prior_month|prior_quarter|yoy
    basket_id: str | None = None                   # explicit governed share basket


def _find_metric(q: str) -> str | None:
    ql = q.lower()
    for kw, metric in sorted(sl.METRIC_KEYWORDS.items(), key=lambda kv: -len(kv[0])):
        if kw in ql:
            return metric
    return None


def _find_filters(q: str) -> dict:
    ql, filters = q.lower(), {}
    fact = sl.load_fact("source_a")
    for dim in sl.DIMENSIONS:
        hits = [val for val in sorted(fact[dim].unique())
                if re.search(rf"\b{re.escape(val.lower())}\b", ql)]
        if len(hits) == 1:
            filters[dim] = hits[0]
        elif hits:
            filters[dim] = hits  # multi-value filter, deterministic order
    return filters


def _find_event(q: str) -> str | None:
    ql = q.lower()
    best, best_hits = None, 0
    for eid, ev in sl.EVENTS.items():
        hits = sum(1 for kw in ev["keywords"] if kw in ql)
        if hits > best_hits:
            best, best_hits = eid, hits
    return best if best_hits >= 1 else None


def _find_breakdown_dimension(q: str) -> str | None:
    """Return the dimension the question explicitly asks to decompose."""
    ql = q.lower()
    phrases = {
        "payer_channel": ("payer channel", "payer channels"),
        "specialty": ("specialty", "specialties"),
        "territory": ("territory", "territories"),
        "district": ("district", "districts"),
        "region": ("region", "regions"),
    }
    for dimension, names in phrases.items():
        if any(re.search(rf"\b{re.escape(name)}\b", ql) for name in names):
            return dimension
    return None


def _find_basket(q: str) -> str | None:
    matches = [basket_id for basket_id, pattern in _BASKET_PATTERNS.items()
               if pattern.search(q)]
    return matches[0] if len(matches) == 1 else None


def parse(question: str) -> Intent:
    q = question.strip()
    metric = _find_metric(q)
    filters = _find_filters(q)
    window, window_refusal = resolve_window(_window_spec(q))
    basis = _find_basis(q)
    basket_id = _find_basket(q)

    if window_refusal:
        return Intent(q, OUT_OF_SCOPE, metric, filters, reason=window_refusal)

    if _PREDICTIVE.search(q):
        return Intent(q, PREDICTIVE, metric, filters, reason=refusal_reason(PREDICTIVE))

    if _COHORT.search(q):
        return Intent(q, COHORT, "nrx", filters)

    if _CAUSAL.search(q):
        event = _find_event(q)
        if event:
            default_metric = sl.EVENTS[event].get(
                "default_metric", sl.EVENTS[event].get("metrics", ["trx"])[0])
            return Intent(q, CAUSAL, metric or default_metric, filters, event_id=event)
        return Intent(q, CAUSAL, metric, filters, reason=refusal_reason(CAUSAL))

    if _DIAGNOSTIC.search(q):
        if metric is None:
            return Intent(q, OUT_OF_SCOPE, reason=refusal_reason(OUT_OF_SCOPE))
        return Intent(q, DIAGNOSTIC, metric, filters,
                      dim_breakdown=_find_breakdown_dimension(q),
                      window=window, compare_basis=basis)

    if _RETRIEVAL.search(q):
        ql = q.lower()
        if _TOP_WRITERS.search(q):
            # NRx share is an account-grain selection field, not the registered
            # aggregate TRx-share metric that the generic "share" keyword finds.
            return Intent(q, RETRIEVAL, "nrx", filters, template="top_writers")
        template = "whitespace" if ("whitespace" in ql or "no activity" in ql or "no calls" in ql) else "top_accounts"
        return Intent(q, RETRIEVAL, metric or "trx", filters, template=template)

    if metric is not None:
        return Intent(q, DESCRIPTIVE, metric, filters, trend=bool(_TREND.search(q)),
                      window=window, compare_basis=basis,
                      basket_id=basket_id if metric == "trx_share" else None)

    return Intent(q, OUT_OF_SCOPE, reason=refusal_reason(OUT_OF_SCOPE))


def refusal_reason(question_class: str) -> str:
    """Curated, product-authored refusal copy. Used by both translators, so a
    language-model translation can never author the refusal a user reads."""
    if question_class == PREDICTIVE:
        return ("This asks for a forecast, and no governed forecasting model is "
                "registered — any number would be a guess you couldn't verify. "
                "The measured history is available instead.")
    if question_class == CAUSAL:
        return ("This question asks for a causal claim no registered event supports, "
                "so there is no valid way to test it. A breakdown can show where the "
                "change sits; if the event is real, ask your governance team to "
                "register it.")
    return ("This question doesn't map to a governed metric or list. The registry "
            "currently covers: "
            + ", ".join(m["label"].lower() for m in sl.METRICS.values()) + ".")
