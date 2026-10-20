"""Optional, structured, artifact-only phrasing for digest items.

The narrator cannot rank or compute. It must return a bounded JSON object, and
the response is accepted only when deterministic validation preserves metric,
scope, entities, direction, units, and every numeric claim while making no
causal assertion.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from collections.abc import Mapping

from .digest import DigestItemArtifact
from .llm_translator import DEFAULT_MODEL
from . import voice


_NUMBER = re.compile(
    r"(?<![\w])(?P<prefix>\$)?(?P<number>[-+]?\d[\d,]*(?:\.\d+)?)"
    r"(?P<suffix>%|σ)?")
_CAUSAL = re.compile(
    r"\b(caus(?:e|ed|es|ing|al)|because|due to|driven by|drove|"
    r"lead(?:s|ing)? to|led to|result(?:s|ed|ing)? (?:in|from)|as a result(?: of)?|"
    r"attribut(?:e|ed|es|ing|ion)|explain(?:s|ed|ing)?|"
    r"produc(?:e|ed|es|ing)|generat(?:e|ed|es|ing)|"
    r"contribut(?:e|ed|es|ing)(?: to)?|boost(?:s|ed|ing)?|"
    r"trigger(?:s|ed|ing)?|accelerat(?:e|ed|es|ing)|lift(?:s|ed|ing)?|"
    r"suppress(?:es|ed|ing)?|depress(?:es|ed|ing)?|influenc(?:e|ed|es|ing)|"
    r"spur(?:s|red|ring)?|spark(?:s|ed|ing)?|prompt(?:s|ed|ing)?|"
    r"induc(?:e|ed|es|ing)|yield(?:s|ed|ing)?|enabled?|responsible for|"
    r"impact of|effect of|in response to)\b", re.I)
_DIRECTION_WORDS = {
    "above": re.compile(
        r"\b(above|higher|up|increase(?:d)?|positive|jump(?:ed)?|moved|"
        r"accelerat(?:e|ed)|improv(?:e|ed)|filling up|picked up)\b", re.I),
    "below": re.compile(
        r"\b(below|lower|(?<!break it )down|decrease(?:d)?|negative|slipped|trailing|"
        r"softened|fell|dropped)\b", re.I),
    "different": re.compile(
        r"\b(differ(?:s|ent|ence)?|alternate|fork|two ways|two definitions|"
        r"reporting unit|definition question)\b", re.I),
}
_OPPOSITE = {"above": "below", "below": "above"}
_RESPONSE_FIELDS = {"text", "metric", "scope", "direction", "causal"}
_MACHINE_SCOPE = re.compile(
    r"\b(?:region|district|territory|specialty|payer_channel)\s*=", re.I)
_SNAKE_CASE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CARD_JARGON = re.compile(
    r"\b(?:governed|artifact|deterministic|gap rank|z[- ]?score|"
    r"standardized movement|native movement|relative difference|priority score)\b",
    re.I,
)
_PASSIVE_MOVEMENT = re.compile(
    r"\bwas\s+[+-]?[\d,.]+%\s+(?:above|below)\b", re.I)
_RELATIVE_RATIO_COPY = re.compile(
    r"(?:differs?\s+by\s+[+-]?[\d,.]+%|"
    r"[+-]?[\d,.]+%\s+relative\s+difference)",
    re.I,
)

SYSTEM = """Rewrite one computed digest headline into one concise sentence.
Return JSON only, with exactly these fields:
{"text": string, "metric": string, "scope": string,
 "direction": "above"|"below"|"different"|"neutral", "causal": false}
Copy metric, scope, and expected_direction exactly from the supplied JSON. The text
must explicitly name metric_label and the human scope_label; never expose the machine
scope field as dimension=value. Preserve every number, sign, and unit from
templated_headline, add no number, and use the supplied direction. Never claim that an
event caused, drove, explained, or produced a movement."""


def rewrite_policy(item: DigestItemArtifact) -> tuple[bool, str]:
    """Return whether model phrasing is allowed for this artifact class.

    Registered-event candidates intentionally retain their governed template.
    Even a numerically faithful paraphrase could turn temporal overlap into an
    attribution, so these items never leave the deterministic phrasing path.
    """

    if item.candidate.kind == "event" or item.candidate.event_id is not None:
        return False, "registered-event candidates use deterministic non-causal wording"
    return True, "model phrasing is permitted subject to deterministic validation"


def numeric_values(text: str) -> tuple[float, ...]:
    """Compatibility helper returning all parsed numeric values."""

    return tuple(value for value, _unit in numeric_claims(text))


def numeric_claims(text: str) -> tuple[tuple[float, str], ...]:
    claims = []
    for match in _NUMBER.finditer(text):
        try:
            value = float(match.group("number").replace(",", ""))
        except ValueError:
            continue
        unit = ({"$": "currency"}.get(match.group("prefix"))
                or {"%": "percent", "σ": "sigma"}.get(match.group("suffix"))
                or "number")
        claims.append((value, unit))
    return tuple(claims)


def _claim_key(claim: tuple[float, str]) -> tuple[str, str]:
    value, unit = claim
    return (format(value, ".12g"), unit)


def expected_direction(item: DigestItemArtifact) -> str:
    if item.candidate.kind == "divergence":
        return "different"
    facts = item.candidate.facts
    if facts is None:
        return "neutral"
    return "above" if facts.absolute_change >= 0 else "below"


def _voice_context(item: DigestItemArtifact, persona: object | None = None):
    candidate = item.candidate
    resolution = candidate.artifact.resolution
    presentation = voice.digest_presentation(
        persona or "executive",
        kind=candidate.kind,
        metric=candidate.metric,
        scope=candidate.filter_dict,
        facts=candidate.facts.to_dict() if candidate.facts else None,
        value=candidate.artifact.value,
        variant=resolution.variant if resolution else candidate.variant,
        alternate_label=candidate.fork_label,
        alternate_value=candidate.fork_value,
        event_name=candidate.event_name,
    )
    return (
        presentation.headline,
        voice.scope_text(candidate.filter_dict, persona or "executive"),
        voice.metric_subject(candidate.metric),
    )


def _decode_response(response: str | Mapping) -> tuple[dict | None, str | None]:
    if isinstance(response, Mapping):
        payload = dict(response)
    else:
        try:
            payload = json.loads(response)
        except (json.JSONDecodeError, TypeError):
            return None, "the narrator response was not a JSON object"
    if not isinstance(payload, dict):
        return None, "the narrator response was not a JSON object"
    if set(payload) != _RESPONSE_FIELDS:
        return None, "the narrator response did not match the bounded schema"
    return payload, None


def validate_rewrite(item: DigestItemArtifact,
                      response: str | Mapping, *,
                      template_headline: str | None = None,
                      scope_label: str | None = None,
                      metric_label: str | None = None,
                      persona: object | None = None) -> tuple[bool, str]:
    """Deterministically reject altered facts or semantically unsafe prose."""

    allowed, policy_reason = rewrite_policy(item)
    if not allowed:
        return False, policy_reason
    payload, error = _decode_response(response)
    if payload is None:
        return False, str(error)
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or "\n" in text or len(text) > 800:
        return False, "the rewrite must be one bounded, non-empty line"
    default_template, default_scope, default_metric = _voice_context(item, persona)
    template_headline = template_headline or default_template
    scope_label = scope_label or default_scope
    metric_label = metric_label or default_metric
    cleaned = text.strip()
    expected_scope = item.fact_payload()["scope"]
    visible_scope = scope_label
    expected = expected_direction(item)
    if payload.get("metric") != item.candidate.metric:
        return False, "the rewrite changed the governed metric"
    if payload.get("scope") != expected_scope:
        return False, "the rewrite changed the governed scope"
    if payload.get("direction") != expected:
        return False, "the rewrite changed the computed direction"
    if payload.get("causal") is not False or _CAUSAL.search(cleaned):
        return False, "the rewrite introduced a causal claim"

    visible_metrics = {metric_label, *voice.metric_aliases(item.candidate.metric)}
    if not any(label.casefold() in cleaned.casefold()
               for label in visible_metrics if label):
        return False, "the rewrite omitted the governed metric label"
    if visible_scope.casefold() not in cleaned.casefold():
        return False, "the rewrite omitted the human scope label"
    if _MACHINE_SCOPE.search(cleaned):
        return False, "the rewrite exposed machine scope syntax"
    if _SNAKE_CASE.search(cleaned):
        return False, "the rewrite exposed a machine identifier"
    if _PASSIVE_MOVEMENT.search(cleaned):
        return False, "the rewrite used passive percentage movement copy"
    if _RELATIVE_RATIO_COPY.search(cleaned):
        return False, "the rewrite used a relative percentage for a definition gap"
    if _CARD_JARGON.search(cleaned):
        return False, "the rewrite exposed analyst-only methodology language"

    required = Counter(_claim_key(claim) for claim in numeric_claims(
        template_headline))
    proposed = Counter(_claim_key(claim) for claim in numeric_claims(cleaned))
    if proposed != required:
        return False, "the rewrite changed a computed number, sign, unit, or multiplicity"

    if expected in _DIRECTION_WORDS:
        if not _DIRECTION_WORDS[expected].search(cleaned):
            return False, "the rewrite omitted the computed direction"
        opposite = _OPPOSITE.get(expected)
        if opposite and _DIRECTION_WORDS[opposite].search(cleaned):
            return False, "the rewrite contradicted the computed direction"
    return True, "validated against metric, scope, direction, units, and numeric facts"


def rewrite_item(item: DigestItemArtifact, api_key: str | None = None,
                 model: str = DEFAULT_MODEL, timeout_seconds: float = 12.0, *,
                 template_headline: str | None = None,
                 scope_label: str | None = None,
                 metric_label: str | None = None,
                 persona: str | None = None,
                 ) -> DigestItemArtifact:
    """Return a validated structured rewrite or the deterministic template."""

    allowed, policy_reason = rewrite_policy(item)
    default_template, default_scope, default_metric = _voice_context(item, persona)
    template = template_headline or default_template
    scope_label = scope_label or default_scope
    metric_label = metric_label or default_metric
    if not allowed:
        return item.with_narration(template, {
            "narrator": "template", "validated": True,
            "fallback_kind": "policy", "fallback_reason": policy_reason,
        })
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return item.with_narration(template, {
            "narrator": "template", "validated": True,
            "fallback_kind": "unavailable", "fallback_reason": "no API key provided",
        })
    started = time.perf_counter()
    raw = ""
    try:
        import anthropic
        client = anthropic.Anthropic(
            api_key=key, timeout=timeout_seconds, max_retries=0)
        facts = item.fact_payload() | {
            "expected_direction": expected_direction(item),
            "response_fields": sorted(_RESPONSE_FIELDS),
            "templated_headline": template,
            "scope_label": scope_label or item.fact_payload()["scope"],
            "metric_label": metric_label or item.fact_payload()["metric_label"],
            "persona": persona,
        }
        message = client.messages.create(
            model=model, max_tokens=220, temperature=0, system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(
                facts, sort_keys=True, default=str)}],
        )
        raw = "".join(block.text for block in message.content
                      if getattr(block, "type", "") == "text").strip()
        valid, reason = validate_rewrite(
            item, raw, template_headline=template,
            scope_label=scope_label, metric_label=metric_label, persona=persona)
        latency = int((time.perf_counter() - started) * 1000)
        if valid:
            payload = json.loads(raw)
            return item.with_narration(str(payload["text"]).strip(), {
                "narrator": "language_model", "validated": True, "model": model,
                "latency_ms": latency, "raw": raw, "validation_reason": reason,
            })
        return item.with_narration(template, {
            "narrator": "template", "validated": True, "model": model,
            "latency_ms": latency, "raw": raw, "fallback_kind": "rejected",
            "fallback_reason": reason,
        })
    except Exception as exc:
        return item.with_narration(template, {
            "narrator": "template", "validated": True, "model": model,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "raw": raw, "fallback_kind": "unavailable",
            "fallback_reason": f"{type(exc).__name__}: narrator request failed",
        })
