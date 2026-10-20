from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

from .ai import (
    AIProvider, AIMappingResult, MappingRequest, kpi_labels, safe_map_labels,
)
from .contracts import (
    KpiMapping, MappingMemory, MappingMethod, MappingProposal, MappingStatus, Observation, ReasonCode,
)
from .ids import stable_id, utcnow
from .registry import DIMENSIONS, KpiDefinition, Registry, normalize_label
from .settings import Settings

DECIDED_BY = "system:mapping@v1"
REASON_DERIVED_CHANGE = "derived_change"
REASON_NO_VALUE = "no_value"
DERIVED_CHANGE = "DERIVED_CHANGE"
IMAGE_NOT_PARSED = "IMAGE_NOT_PARSED"

MEANING_DIMENSIONS = frozenset({"meaning"})
KNOWN_MEANING_CODES = frozenset({
    "MULTIPLE_CHARTS_WITHOUT_LOCAL_LABEL", "MISSING_CHART_CONTEXT", "NEARBY_LABEL_UNVERIFIED",
    "MISSING_SERIES_LABEL", "DUPLICATE_CHART_LABELS", "MISSING_CATEGORY_LABELS", "WORKSHEET_WITHOUT_LABELS",
    "TABLE_HEADER_UNCONFIRMED", "TABLE_CONTEXT_MISSING", "INFERRED_DESCRIPTION",
})

_FRACTION_UNITS = frozenset({"fraction", "percent", "pct", "%", "share", "ratio"})
_AMOUNT_UNITS = frozenset({"count", "usd", "currency", "units", "number"})
_MONTHS = ("jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun", "june", "jul",
           "july", "aug", "august", "sep", "sept", "september", "oct", "october", "nov", "november", "dec",
           "december")
_MONTH_SET = frozenset(_MONTHS)
_PERIOD_WORDS = frozenset({
    "ytd", "mtd", "qtd", "r3m", "r6m", "r12m", "l3m", "l6m", "l12m", "ttm", "mat", "rolling", "month", "months",
    "monthly", "quarter", "quarters", "quarterly", "year", "years", "week", "weeks", "period", "through", "thru",
    "to", "date", "ending", "ended", "as", "of", "fiscal", "calendar", "fy", "cy",
})
_PERIOD_TOKEN = re.compile(r"^(\d+|(fy|cy|q[1-4]|h[12])\d*|\d+(q[1-4]|h[12])|(" + "|".join(_MONTHS) + r")\d{2,4})$")
_GENERIC_SCOPE_WORDS = frozenset({"total", "totals", "grand", "all", "overall", "national", "us", "brand", "region",
                                  "regions", "district", "districts", "territory", "territories", "specialty",
                                  "payer", "channel", "segment", "series", "actual", "actuals", "act", "value",
                                  "values", "data", "amount", "count", "number", "n", "cur", "current", "ty",
                                  "this"}) | frozenset(DIMENSIONS)
_DIGITS = re.compile(r"\d+")


def unit_family(unit: str | None) -> str:
    """'fraction', 'amount' (count/usd) or 'unknown'."""
    u = str(unit or "").strip().lower()
    if u in _FRACTION_UNITS:
        return "fraction"
    if u in _AMOUNT_UNITS:
        return "amount"
    return "unknown"


def units_compatible(observation_unit: str | None, kpi_unit: str | None) -> bool:
    """Fractions only match fraction KPIs, count/usd only count/usd KPIs; an unknown unit matches any."""
    obs, kpi = unit_family(observation_unit), unit_family(kpi_unit)
    return obs == "unknown" or kpi == "unknown" or obs == kpi


def _flag_code(flag: Any) -> tuple[str, str]:
    if isinstance(flag, dict):
        return str(flag.get("code") or ""), str(flag.get("dimension") or "").lower()
    return str(flag or ""), ""


def has_flag(flags: list | None, code: str) -> bool:
    return any(_flag_code(f)[0] == code for f in flags or [])


def meaning_flags(flags: list | None) -> list[str]:
    """Codes of flags whose dimension is 'meaning' (or known meaning codes without a dimension)."""
    out = set()
    for f in flags or []:
        code, dim = _flag_code(f)
        if code and (dim in MEANING_DIMENSIONS or (not dim and code in KNOWN_MEANING_CODES)):
            out.add(code)
    return sorted(out)


def mask_text(text: Any) -> str:
    """Normalized text with digits and month names masked (labels that change every period)."""
    tokens = []
    for tok in normalize_label(str(text or "")).split():
        if tok in _MONTH_SET:
            tokens.append("<month>")
        else:
            tokens.append(_DIGITS.sub("#", tok))
    return " ".join(tokens)


def memory_label(label: Any) -> str:
    """The label key used by mapping memory: normalized, with digits and month names masked."""
    return mask_text(label)


def _is_period_token(tok: str) -> bool:
    return tok in _MONTH_SET or tok in _PERIOD_WORDS or bool(_PERIOD_TOKEN.match(tok))


def label_is_uninformative(label: str, scope: dict | None = None, brand_aliases: dict[str, str] | None = None) -> bool:
    """True when the label is empty or only a period, a scope value or the brand (try the context)."""
    tokens = normalize_label(label).split()
    if not tokens:
        return True
    scope_tokens: set[str] = set()
    for value in (scope or {}).values():
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            scope_tokens.update(normalize_label(str(v)).split())
    for alias in brand_aliases or {}:
        scope_tokens.update(alias.split())
    return all(_is_period_token(t) or t in scope_tokens or t in _GENERIC_SCOPE_WORDS for t in tokens)


@dataclass(frozen=True)
class Phrase:
    tokens: tuple[str, ...]
    text: str
    kpi_id: str


def _fold(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def match_tokens(text: Any) -> list[str]:
    """Normalized, plural-folded words used for whole-phrase synonym matching."""
    return [_fold(t) for t in normalize_label(str(text or "")).split()]


def synonym_phrases(kpis: list[KpiDefinition]) -> list[Phrase]:
    """Whole-word phrases per KPI: registry synonyms, the KPI label and the kpi_id words."""
    out: dict[tuple[tuple[str, ...], str], Phrase] = {}
    for k in kpis:
        for raw in [*k.synonyms, k.label, k.kpi_id]:
            tokens = tuple(match_tokens(raw))
            if tokens:
                out[(tokens, k.kpi_id)] = Phrase(tokens, normalize_label(raw), k.kpi_id)
    return sorted(out.values(), key=lambda p: (p.kpi_id, p.text))


def _contains(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(tuple(tokens[i:i + n]) == phrase for i in range(len(tokens) - n + 1))


def longest_synonym_hits(text: str, phrases: list[Phrase]) -> list[Phrase]:
    """Phrases found in ``text`` as whole words that share the maximum length in words."""
    tokens = match_tokens(text)
    hits = [p for p in phrases if p.tokens and _contains(tokens, p.tokens)]
    if not hits:
        return []
    best = max(len(p.tokens) for p in hits)
    return [p for p in hits if len(p.tokens) == best]


_fp_guard = threading.local()


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _standardize_fingerprint():
    try:
        from . import standardize
    except Exception:
        return None
    helper = getattr(standardize, "fingerprint", None)
    if not callable(helper) or getattr(helper, "__module__", "") == __name__:
        return None
    return helper


def fingerprint(observation_like: Any) -> str:
    """Layout fingerprint: the stored one, else ``standardize.fingerprint``, else a masked-label hash."""
    existing = _field(observation_like, "fingerprint")
    if existing:
        return str(existing)
    helper = None if getattr(_fp_guard, "active", False) else _standardize_fingerprint()
    if helper is not None:
        _fp_guard.active = True
        try:
            value = helper(observation_like)
            if value:
                return str(value)
        except Exception:
            pass
        finally:
            _fp_guard.active = False
    return _fallback_fingerprint(observation_like)


def _fallback_fingerprint(obj: Any) -> str:
    ctx = _field(obj, "context") or {}
    ctx = ctx if isinstance(ctx, dict) else {}
    parts = [
        _field(obj, "report_type"),
        ctx.get("slide_title") or _field(obj, "page_title"),
        _field(obj, "kind") or _field(obj, "method"),
        ctx.get("chart_title") or ctx.get("table_title"),
        ctx.get("series_name") or ctx.get("row_label") or _field(obj, "label"),
        ctx.get("col_header"),
    ]
    if not any(parts[1:]):
        parts = [_field(obj, "label_context"), _field(obj, "label")]
    return stable_id("fingerprint@mapping-fallback", *[mask_text(str(getattr(p, "value", p) or "")) for p in parts])


@dataclass
class Candidate:
    kpi_id: str | None
    method: MappingMethod
    confidence: float
    rationale: str
    source: str
    note: str | None = None
    model: str | None = None
    prompt_sha: str | None = None
    raw_response: str | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kpi_id": self.kpi_id, "method": self.method.value,
                               "confidence": round(float(self.confidence), 4), "source": self.source}
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class Decision:
    status: MappingStatus
    method: MappingMethod | None
    kpi_id: str | None
    reason: str
    candidates: list[Candidate] = field(default_factory=list)
    model_candidates: list[str] = field(default_factory=list)

    @property
    def needs_model(self) -> bool:
        return bool(self.model_candidates)


@dataclass
class _Context:
    kpis: dict[str, KpiDefinition]
    phrases: list[Phrase]
    memory: dict[tuple[str, str], MappingMemory]
    brand_aliases: dict[str, str]
    floor: float


def _compatible_ids(ctx: _Context, unit: str | None) -> list[str]:
    return sorted(k for k, d in ctx.kpis.items() if units_compatible(unit, d.unit))


def decide_deterministic(obs: Observation, ctx: _Context) -> Decision:
    """Memory, skips and synonym gates for one observation (no model involved)."""
    flags = obs.flags or []
    label_norm = normalize_label(obs.label or "")
    context_norm = normalize_label(obs.label_context or "")
    if has_flag(flags, IMAGE_NOT_PARSED) or not (label_norm or context_norm):
        why = "image not parsed" if has_flag(flags, IMAGE_NOT_PARSED) else "no label"
        return Decision(MappingStatus.NO_MATCH, None, None, f"{REASON_NO_VALUE}: {why}; nothing to map")

    mem = ctx.memory.get((obs.fingerprint, memory_label(obs.label))) if label_norm else None
    if mem is not None and mem.kpi_id in ctx.kpis:
        kpi = ctx.kpis[mem.kpi_id]
        cand = Candidate(mem.kpi_id, MappingMethod.MEMORY, 1.0,
                         f"Approved by {mem.approved_by} for this layout and label.", "memory")
        if units_compatible(obs.unit, kpi.unit):
            return Decision(MappingStatus.ACCEPTED, MappingMethod.MEMORY, mem.kpi_id,
                            f"memory_match: approved mapping {mem.memory_id[:12]}", [cand])
        cand.note = "unit_mismatch"
        return Decision(MappingStatus.AMBIGUOUS, MappingMethod.MEMORY, mem.kpi_id,
                        f"{ReasonCode.UNIT_MISMATCH}: approved mapping to {mem.kpi_id} ({kpi.unit}) "
                        f"but the value is {obs.unit}", [cand])

    if has_flag(flags, DERIVED_CHANGE):
        return Decision(MappingStatus.NO_MATCH, None, None,
                        f"{REASON_DERIVED_CHANGE}: change column (captured, not mapped)")

    compatible_all = _compatible_ids(ctx, obs.unit)
    if label_norm and not label_is_uninformative(obs.label, obs.scope, ctx.brand_aliases):
        source, text = "label", obs.label
    else:
        source, text = "label_context", obs.label_context
    hits = longest_synonym_hits(text, ctx.phrases)
    if not hits:
        return Decision(MappingStatus.NO_MATCH, None, None, ReasonCode.NO_REGISTRY_MATCH,
                        model_candidates=compatible_all)

    phrase_of = {}
    for p in hits:
        phrase_of.setdefault(p.kpi_id, p.text)
    kpi_ids = sorted(phrase_of)
    base = 1.0 if source == "label" else 0.85
    if len(kpi_ids) > 1:
        cands = [Candidate(k, MappingMethod.SYNONYM, round(base / len(kpi_ids), 4),
                           f"The {source.replace('_', ' ')} contains '{phrase_of[k]}', a synonym of {k}, "
                           f"as long as another KPI's synonym.", source, note="tie") for k in kpi_ids]
        tied_compatible = [k for k in kpi_ids if units_compatible(obs.unit, ctx.kpis[k].unit)]
        return Decision(MappingStatus.AMBIGUOUS, MappingMethod.SYNONYM, None,
                        f"{ReasonCode.MULTIPLE_CANDIDATES}: {', '.join(kpi_ids)}", cands,
                        model_candidates=tied_compatible or compatible_all)

    kpi_id = kpi_ids[0]
    kpi = ctx.kpis[kpi_id]
    cand = Candidate(kpi_id, MappingMethod.SYNONYM, base,
                     f"The {source.replace('_', ' ')} contains the registry synonym '{phrase_of[kpi_id]}' "
                     f"(longest match).", source)
    if not units_compatible(obs.unit, kpi.unit):
        cand.note, cand.confidence = "unit_mismatch", 0.3
        if source == "label":
            return Decision(MappingStatus.AMBIGUOUS, MappingMethod.SYNONYM, kpi_id,
                            f"{ReasonCode.UNIT_MISMATCH}: the label names {kpi_id} ({kpi.unit}) "
                            f"but the value is {obs.unit}", [cand], model_candidates=compatible_all)
        return Decision(MappingStatus.NO_MATCH, None, None,
                        f"{ReasonCode.NO_REGISTRY_MATCH}: context names {kpi_id} but its unit differs",
                        [cand], model_candidates=compatible_all)
    codes = meaning_flags(flags)
    if codes:
        return Decision(MappingStatus.AMBIGUOUS, MappingMethod.SYNONYM, kpi_id,
                        f"{ReasonCode.MEANING_FLAGGED}: {', '.join(codes)}", [cand], model_candidates=compatible_all)
    return Decision(MappingStatus.ACCEPTED, MappingMethod.SYNONYM, kpi_id,
                    f"synonym_match: '{phrase_of[kpi_id]}' in {source}", [cand])


def apply_model_result(decision: Decision, result: AIMappingResult, floor: float) -> Decision:
    """Fold one model proposal into a decision."""
    if decision.status == MappingStatus.ACCEPTED:
        return decision
    cand = Candidate(result.kpi_id, MappingMethod.MODEL, float(result.confidence or 0.0), result.rationale,
                     "model", model=result.model, prompt_sha=result.prompt_sha, raw_response=result.raw_response)
    if result.error:
        cand.note = "error"
        decision.candidates.append(cand)
        if decision.status == MappingStatus.NO_MATCH:
            decision.reason += " (model proposal failed)"
        return decision
    if result.kpi_id is None:
        decision.candidates.append(cand)
        return decision
    confident = cand.confidence >= floor
    if not confident:
        cand.note = "below_floor"
    if decision.status == MappingStatus.NO_MATCH:
        if confident:
            decision.candidates.insert(0, cand)
            decision.status, decision.method, decision.kpi_id = MappingStatus.AMBIGUOUS, MappingMethod.MODEL, result.kpi_id
            decision.reason = (f"{ReasonCode.MODEL_PROPOSAL_ONLY}: {result.model} suggests {result.kpi_id} "
                               f"(confidence {cand.confidence:.2f})")
        else:
            decision.candidates.append(cand)
            decision.reason = (f"{ReasonCode.NO_REGISTRY_MATCH}: low-confidence model suggestion {result.kpi_id} "
                               f"({cand.confidence:.2f})")
        return decision
    decision.candidates.append(cand)
    if decision.kpi_id is None and confident:
        decision.kpi_id = result.kpi_id
        decision.reason += f" (model suggests {result.kpi_id})"
    return decision


def _read_in(store, cls: type, column: str, values: list[str], extra_where: str = "",
             extra_params: dict[str, Any] | None = None, chunk: int = 400) -> list[Any]:
    out: list[Any] = []
    unique = sorted(set(v for v in values if v))
    for start in range(0, len(unique), chunk):
        part = unique[start:start + chunk]
        params = {f"v{i}": v for i, v in enumerate(part)}
        where = f"{column} IN ({', '.join(':' + k for k in params)})"
        if extra_where:
            where += f" AND {extra_where}"
        out.extend(store.read(cls, where, {**params, **(extra_params or {})}))
    return out


def load_memory(store, observations: list[Observation]) -> dict[tuple[str, str], MappingMemory]:
    """Active approved mappings for the observations' fingerprints, keyed by (fingerprint, label key)."""
    rows = _read_in(store, MappingMemory, "fingerprint", [o.fingerprint for o in observations], "active = TRUE")
    out: dict[tuple[str, str], MappingMemory] = {}
    for m in sorted(rows, key=lambda m: (m.approved_at, m.memory_id)):
        out[(m.fingerprint, m.normalized_label)] = m
    return out


def remember_mapping(store, observation: Any, kpi_id: str, actor: str, decision_id: str | None = None) -> MappingMemory:
    """Record a reviewer-approved mapping for this layout fingerprint and label (used by review)."""
    if not kpi_id or not actor:
        raise ValueError("remember_mapping needs a kpi_id and an actor")
    fp = fingerprint(observation)
    label = memory_label(_field(observation, "label") or "")
    mem = MappingMemory(memory_id=stable_id(fp, label), fingerprint=fp, normalized_label=label, kpi_id=str(kpi_id),
                        approved_by=str(actor), approved_at=utcnow(), source_decision_id=decision_id, active=True)
    store.upsert([mem])
    return mem


def _request(obs: Observation, candidates: list[str]) -> MappingRequest:
    return MappingRequest(observation_id=obs.observation_id, label=obs.label or "", label_context=obs.label_context or "",
                          unit=obs.unit, period_grain=obs.period_grain, scope_key=obs.scope_key or "{}",
                          candidates=list(candidates))


def _proposal_rows(obs: Observation, decision: Decision, registry_version: str, now) -> list[MappingProposal]:
    rows = []
    for c in decision.candidates:
        rationale = c.rationale if not c.note or c.note == "error" else f"{c.rationale} [{c.note}]"
        rows.append(MappingProposal(
            proposal_id=stable_id(obs.observation_id, c.method.value, c.kpi_id, registry_version),
            observation_id=obs.observation_id, method=c.method, kpi_id=c.kpi_id, confidence=float(c.confidence),
            rationale=rationale[:500], registry_version=registry_version, created_at=now, model=c.model,
            prompt_sha=c.prompt_sha, raw_response=c.raw_response, run_id=obs.run_id))
    return rows


def _same_decision(a: KpiMapping, b: KpiMapping) -> bool:
    return (a.status, a.method, a.kpi_id, a.kpi_version, a.candidates, a.reason) == \
           (b.status, b.method, b.kpi_id, b.kpi_version, b.candidates, b.reason)


def map_observations(store, registry: Registry, settings: Settings, observations: list[Observation],
                     provider: AIProvider | None) -> list[KpiMapping]:
    """Map each observation to a KPI (or not) under the code gates; write proposals and mappings."""
    unique: dict[str, Observation] = {}
    for obs in observations or []:
        unique.setdefault(obs.observation_id, obs)
    obs_list = list(unique.values())
    if not obs_list:
        return []
    version = registry.version
    kpis = {k.kpi_id: k for k in registry.approved()}
    ctx = _Context(kpis=kpis, phrases=synonym_phrases(list(kpis.values())), memory=load_memory(store, obs_list),
                   brand_aliases=registry.brand_aliases(), floor=float(settings.mapping_model_floor))
    decisions = {o.observation_id: decide_deterministic(o, ctx) for o in obs_list}

    pending = [o for o in obs_list if decisions[o.observation_id].needs_model]
    if pending and provider is not None and getattr(provider, "name", "off") != "off":
        requests = [_request(o, decisions[o.observation_id].model_candidates) for o in pending]
        for res in safe_map_labels(provider, requests, kpi_labels(registry)):
            apply_model_result(decisions[res.observation_id], res, ctx.floor)

    now = utcnow()
    existing = {m.observation_id: m for m in _read_in(store, KpiMapping, "observation_id", list(unique),
                                                      "registry_version = :rv", {"rv": version})}
    proposals: list[MappingProposal] = []
    mappings: list[KpiMapping] = []
    to_write: list[KpiMapping] = []
    for obs in obs_list:
        d = decisions[obs.observation_id]
        proposals.extend(_proposal_rows(obs, d, version, now))
        prior = existing.get(obs.observation_id)
        if prior is not None and (prior.method == MappingMethod.REVIEWER or not prior.decided_by.startswith("system:")):
            mappings.append(prior)
            continue
        kpi = kpis.get(d.kpi_id) if d.kpi_id else None
        row = KpiMapping(observation_id=obs.observation_id, registry_version=version, status=d.status, method=d.method,
                         kpi_id=d.kpi_id, kpi_version=kpi.version if kpi else None,
                         candidates=[c.as_json() for c in d.candidates], reason=d.reason, decided_by=DECIDED_BY,
                         decided_at=now)
        if prior is not None and _same_decision(prior, row):
            row.decided_at = prior.decided_at
        mappings.append(row)
        to_write.append(row)
    if proposals:
        store.insert_new(proposals)
    if to_write:
        store.upsert(to_write)
    return mappings
