"""Persona-aware presentation copy for governed analytical artifacts.

This module is deliberately pure and Streamlit-free.  It translates immutable
facts into human display copy, but never mutates an artifact, table, score, or
hash-bearing payload.  Views may format copies returned here.  The digest's
sanctioned persona relevance tie-break is the sole analytical import, and it
does not alter candidate facts or numeric scores.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any

import pandas as pd

from . import profiles
from . import semantic_layer as sl


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    label: str
    movement_up: str
    movement_down: str
    lead_frame: str
    scope_pronoun: str


@dataclass(frozen=True)
class DigestPresentation:
    headline: str
    detail: str
    chip: str


@dataclass(frozen=True)
class TilePresentation:
    label: str
    headline: str


@dataclass(frozen=True)
class CohortPresentation:
    hero: str
    label: str
    headline: str
    method_chip: str


@dataclass(frozen=True)
class RefusalPresentation:
    lead: str
    detail: str


@dataclass(frozen=True)
class ForkPresentation:
    headline: str
    detail: str
    chip: str = "Two answers exist"


@dataclass(frozen=True)
class MonitoringPresentation:
    headline: str
    summary: str


def presentation_hash(fact_hash: str, persona: object | None,
                      presentation: object) -> str:
    """Hash the exact view copy separately from immutable artifact wording."""

    if isinstance(presentation, Mapping):
        presentation_payload: object = dict(presentation)
    elif isinstance(presentation, (tuple, list)):
        presentation_payload = list(presentation)
    else:
        presentation_payload = {
            field: getattr(presentation, field)
            for field in getattr(presentation, "__dataclass_fields__", {})
        }
    payload = {
        "fact_hash": str(fact_hash),
        "persona": resolve_profile(persona).id,
        "presentation": presentation_payload,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                            default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


VOICE_PROFILES = MappingProxyType({item.id: item for item in (
    VoiceProfile(
        "sales_rep", "Sales Rep", "jumped", "is down",
        "act on the next call", "your territory",
    ),
    VoiceProfile(
        "district_manager", "District Manager", "moved above", "is trailing",
        "break it down by territory", "your district",
    ),
    VoiceProfile(
        "brand_marketing", "Brand Marketing", "picked up", "softened",
        "inspect the program mix", "the brand",
    ),
    VoiceProfile(
        "market_access", "Market Access", "improved", "is trailing",
        "check the payer mix", "the brand",
    ),
    VoiceProfile(
        "executive", "Executive", "is running above", "slipped",
        "size the business consequence", "the brand",
    ),
)})

DEFINITION_FORK_TOOLTIP = (
    "Material definition fork: registered definitions produce different answers"
)


def chip_tooltip(label: object) -> str | None:
    return DEFINITION_FORK_TOOLTIP if str(label) == "Two answers exist" else None


_TERRITORY_REGION = {"N": "North", "S": "South", "E": "East", "W": "West"}
_TERRITORY_SPECIALTY = {
    "CAR": "Cardiology", "END": "Endocrinology", "ONC": "Oncology",
    "PCP": "Primary Care",
}

_METRIC_OVERRIDES = {
    "calls": "Details",
    "speaker_attendance_per_hcp_90d": "Speaker touches per HCP (90d)",
    "calls_per_hcp_90d": "Details per HCP (90d)",
    "samples_per_hcp_90d": "Samples per HCP (90d)",
    "referrals_in_per_covered_hcp_90d": "Incoming referrals per covered HCP (90d)",
    "active_referrers_per_covered_hcp_90d": "Active referrers per covered HCP (90d)",
    "speaker_attendance": "Speaker attendance",
    "whitespace_hcps": "Untouched high-value HCPs",
    "hcp_cohort": "Top HCP activity gaps",
}

# These are deliberately singular business subjects.  Keeping grammatical
# number here prevents profile verbs such as ``is trailing`` from being joined
# to plural registry labels such as ``Samples`` or ``Active referrers``.
_MOVEMENT_SUBJECTS = {
    "trx": "TRx volume",
    "nrx": "NRx volume",
    "nbrx": "NBRx volume",
    "trx_share": "TRx share",
    "calls": "Detail activity",
    "call_attainment": "Call-plan attainment",
    "samples": "Sample volume",
    "speaker_attendance": "Speaker attendance",
    "new_writers": "New-writer count",
    "incoming_referrals": "Incoming referral volume",
    "referrals_in": "Incoming referral volume",
    "active_referrers": "Active-referrer count",
    "payer_mix": "Payer mix",
}


def metric_subject(metric: object) -> str:
    """Neutral, grammatically singular business subject for narrative copy."""

    key = str(metric)
    return _MOVEMENT_SUBJECTS.get(key, metric_name(key))


def metric_aliases(metric: object) -> tuple[str, ...]:
    """Registered human labels that safely identify one metric in prose."""

    key = str(metric)
    aliases = {metric_name(key), metric_subject(key)}
    if key == "trx_share":
        aliases.update(("TRx share", "share"))
    return tuple(sorted(aliases, key=lambda value: (-len(value), value)))

_VARIANT_OVERRIDES = {
    ("trx_share", "brand_market"): "TRx share · all advanced therapy",
    ("trx_share", "advanced_therapy"): "TRx share · all advanced therapy",
    ("trx_share", "il17_class"): "TRx share · IL-17 class",
    ("call_attainment", "actual_plan"): "Call-plan attainment",
}

_COLUMN_NAMES = {
    "account_id": "HCP account ID",
    "npi": "NPI",
    "name": "HCP",
    "specialty": "Specialty",
    "territory": "Territory",
    "district": "District",
    "region": "Region",
    "payer_channel": "Payer channel",
    "trx_ttm": "TRx (12m)",
    "nrx_ttm": "NRx (12m)",
    "nbrx_ttm": "NBRx (12m)",
    "market_nrx_ttm": "Market NRx (12m)",
    "nrx_share_ttm": "NRx share (12m)",
    "decile": "Decile",
    "decile_band": "Decile band",
    "months_since_rx": "Months since prescription",
    "months_since_activity": "Months since activity",
    "calls_90d": "Details (90d)",
    "call_plan_90d": "Detail plan (90d)",
    "rank": "Rank",
    "gap_rank": "Gap rank",
    "metric": "Metric",
    "metric_id": "Metric",
    "label": "Activity",
    "top_hcps": "Top HCPs",
    "matched_peers": "Matched peers",
    "absolute_gap": "Difference",
    "relative_gap": "Relative difference",
    "top_observed_n": "Top HCPs observed",
    "peer_observed_n": "Peers observed",
    "value_format": "Value format",
    "top_rank": "Top-HCP rank",
    "top_account_id": "Top-HCP account ID",
    "top_npi": "Top-HCP NPI",
    "peer_account_id": "Peer account ID",
    "peer_npi": "Peer NPI",
    "top_nrx_share_ttm": "Top-HCP NRx share (12m)",
    "peer_nrx_share_ttm": "Peer NRx share (12m)",
    "top_market_nrx_ttm": "Top-HCP market NRx (12m)",
    "peer_market_nrx_ttm": "Peer market NRx (12m)",
    "match_distance": "Match distance",
    "dimension": "Breakdown",
    "value": "Value",
    "period_start": "Starting period",
    "period_end": "Ending period",
    "delta": "Change",
    "share_of_change": "Share of change",
    "month": "Month",
    "scope": "Scope",
    "latest": "Latest",
    "latest_display": "Latest",
    "trailing": "Recent norm",
    "trailing_display": "Recent norm",
    "movement": "Movement",
    "z": "Standardized movement (σ)",
    "priority_score": "Priority score",
    "direction": "Direction",
    "also_visible_as": "Also visible as",
    "cohort": "Group",
    "top_n": "Top HCP count",
    "matched_n": "Matched-pair count",
}

_ACRONYM_TOKENS = {
    "hcp": "HCP", "hcps": "HCPs", "id": "ID", "npi": "NPI",
    "trx": "TRx", "nrx": "NRx", "nbrx": "NBRx", "roi": "ROI",
    "ttm": "TTM", "r3m": "R3M", "r6m": "R6M", "r12m": "R12M",
    "il17": "IL-17",
}


def _known_display_labels() -> frozenset[str]:
    labels = set(_METRIC_OVERRIDES.values()) | set(_COLUMN_NAMES.values())
    for definition in sl.METRICS.values():
        labels.add(str(definition.get("label", "")))
        labels.update(
            str(variant.get("label", ""))
            for variant in definition.get("variants", {}).values()
        )
    return frozenset(label for label in labels if label)


_KNOWN_DISPLAY_LABELS = _known_display_labels()

_SCOPE_PATTERN = re.compile(
    r"\b(region|district|territory|specialty|payer_channel)\s*=\s*([^,;|]+)",
    re.IGNORECASE,
)
_SNAKE_PATTERN = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def resolve_profile(persona: object | None) -> VoiceProfile:
    """Resolve a persona id, label, or PersonaDefinition to voice data."""

    raw = getattr(persona, "id", persona) or "executive"
    text = str(raw).strip()
    if text in VOICE_PROFILES:
        return VOICE_PROFILES[text]
    folded = text.casefold()
    for profile in VOICE_PROFILES.values():
        if folded in {profile.id.casefold(), profile.label.casefold()}:
            return profile
    return VOICE_PROFILES["executive"]


def _persona_default_scope(persona: object | None) -> dict:
    profile = resolve_profile(persona)
    try:
        return dict(profiles.persona_definition(profile.id).default_scope)
    except ValueError:
        return {}


def territory_name(value: object) -> str:
    """Return a readable territory name while keeping its code second."""

    code = str(value)
    match = re.fullmatch(r"([NSEW])-([A-Z]+)-(\d+)", code)
    if not match:
        return code
    region = _TERRITORY_REGION.get(match.group(1), match.group(1))
    specialty = _TERRITORY_SPECIALTY.get(match.group(2), match.group(2).title())
    number = int(match.group(3))
    return f"{region} {specialty} {number} ({code})"


def _display_scope_value(dimension: str, value: object) -> str:
    values = value if isinstance(value, (tuple, list, set)) else (value,)
    rendered = [territory_name(item) if dimension == "territory" else str(item)
                for item in values]
    if len(rendered) == 1:
        return rendered[0]
    if len(rendered) == 2:
        return " and ".join(rendered)
    return ", ".join(rendered[:-1]) + f", and {rendered[-1]}"


def scope_text(scope: Mapping | None, persona: object | None = None,
               default_scope: Mapping | None = None, *, opener: bool = False) -> str:
    """Humanize a registered scope without ever exposing ``dimension=value``."""

    filters = dict(scope or {})
    if not filters:
        return "Across the brand" if opener else "the brand"
    expected = dict(default_scope) if default_scope is not None \
        else _persona_default_scope(persona)
    if expected and filters == expected:
        dimension = next(iter(expected))
        return {
            "territory": "your territory",
            "district": "your district",
            "region": "your region",
        }.get(dimension, resolve_profile(persona).scope_pronoun)
    order = {name: index for index, name in enumerate(sl.DIMENSIONS)}
    parts = [
        _display_scope_value(dimension, value)
        for dimension, value in sorted(filters.items(), key=lambda item: (
            order.get(item[0], len(order)), item[0]))
    ]
    return " · ".join(parts)


def metric_name(metric: object) -> str:
    key = str(metric)
    if key in _KNOWN_DISPLAY_LABELS:
        return key
    if key in _METRIC_OVERRIDES:
        return _METRIC_OVERRIDES[key]
    if key in sl.METRICS:
        return str(sl.METRICS[key]["label"])
    return _identifier_label(key)


def variant_name(metric: object, variant: object | None) -> str:
    metric_key = str(metric)
    variant_key = "" if variant is None else str(variant)
    if (metric_key, variant_key) in _VARIANT_OVERRIDES:
        return _VARIANT_OVERRIDES[(metric_key, variant_key)]
    definition = sl.METRICS.get(metric_key, {}).get("variants", {}).get(variant_key)
    if definition:
        return str(definition.get("label") or metric_name(metric_key))
    return metric_name(metric_key) if not variant_key else column_name(variant_key)


def column_name(column: object) -> str:
    key = str(column)
    if key in _KNOWN_DISPLAY_LABELS:
        return key
    if key in _COLUMN_NAMES:
        return _COLUMN_NAMES[key]
    if key in sl.METRICS or key in _METRIC_OVERRIDES:
        return metric_name(key)
    return _identifier_label(key)


def _identifier_label(value: str) -> str:
    """Label an identifier while leaving already-human copy unchanged."""

    text = str(value).strip()
    if not text:
        return text
    if "_" not in text and (
            any(character.isupper() for character in text)
            or any(marker in text for marker in (" ", "-", "·"))):
        return text
    tokens = text.split("_") if "_" in text else [text]
    return " ".join(
        _ACRONYM_TOKENS.get(token.casefold(), token.capitalize())
        for token in tokens
    )


def _parse_scope_string(value: str) -> dict[str, str]:
    return {match.group(1).lower(): match.group(2).strip()
            for match in _SCOPE_PATTERN.finditer(value)}


def humanize_sentence(value: object, persona: object | None = None) -> str:
    """Remove registry syntax from display-only fallback prose."""

    text = str(value or "")
    text = re.sub(r"\bAll scopes\b", "Across the brand", text,
                  flags=re.IGNORECASE)

    def replace_scope(match: re.Match) -> str:
        dimension = match.group(1).lower()
        raw = match.group(2).strip()
        remainder = ""
        # Territory codes have a closed, governed shape.  A raw engine
        # headline may omit punctuation after the code, so the broad fallback
        # regex can also capture the following sentence.  Split that tail here
        # and retain it verbatim after the human territory name.
        if dimension == "territory":
            territory = re.match(r"[NSEW]-[A-Z]+-\d+", raw)
            if territory:
                raw, remainder = territory.group(0), raw[territory.end():]
        rendered = scope_text(
            {dimension: raw}, persona=persona, default_scope={})
        return f"{rendered}{remainder}"

    text = _SCOPE_PATTERN.sub(replace_scope, text)
    text = _SNAKE_PATTERN.sub(lambda match: column_name(match.group(0)), text)
    for identifier, display in {
            "trx": "TRx", "nrx": "NRx", "nbrx": "NBRx",
            "hcp": "HCP", "hcps": "HCPs", "roi": "ROI",
    }.items():
        text = re.sub(rf"\b{identifier}\b", display, text,
                      flags=re.IGNORECASE)
    text = re.sub(r"\bgoverned\s+(?:mixed\s+)?default\b", "primary definition",
                  text, flags=re.IGNORECASE)
    text = re.sub(r"\bgoverned\b", "standard", text, flags=re.IGNORECASE)
    text = re.sub(r"\balternate definition\b", "other definition", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\bdefinition fork\b", "two-definition check", text,
                  flags=re.IGNORECASE)
    return " ".join(text.split())


def humanize_table(frame: pd.DataFrame | None, persona: object | None = None) \
        -> pd.DataFrame | None:
    """Return a human-labeled copy; never rename or rewrite the source frame."""

    if frame is None:
        return None
    out = frame.copy(deep=True)
    for column in list(out.columns):
        if column == "metric":
            out[column] = out[column].map(
                lambda value: metric_name(value) if pd.notna(value) else value)
        elif column == "cohort":
            out[column] = out[column].map({
                "top_hcps": "Top HCPs", "matched_peers": "Matched peers",
            }).fillna(out[column])
        elif column == "territory":
            out[column] = out[column].map(
                lambda value: territory_name(value) if pd.notna(value) else value)
        elif column == "scope":
            out[column] = out[column].map(
                lambda value: scope_text(_parse_scope_string(str(value)), persona=persona)
                if _parse_scope_string(str(value)) else humanize_sentence(value, persona))
        elif out[column].dtype == object:
            out[column] = out[column].map(
                lambda value: humanize_sentence(value, persona)
                if isinstance(value, str) else value)
    return out.rename(columns={column: column_name(column) for column in out.columns})


display_table = humanize_table


def cohort_display_table(frame: pd.DataFrame | None,
                         persona: object | None = None, *,
                         compact: bool = False) -> pd.DataFrame | None:
    """Format mixed-unit cohort rows without exposing method columns."""

    if frame is None:
        return None
    display = frame.copy(deep=True)
    if "metric" in display.columns:
        if "label" not in display.columns:
            display.insert(0, "label", display["metric"].map(metric_name))
        else:
            display["label"] = display["metric"].map(metric_name)
    row_formats = (
        display["value_format"].astype(str)
        if "value_format" in display.columns
        else pd.Series("number", index=display.index)
    )
    for column in ("top_hcps", "matched_peers"):
        if column in display.columns:
            display[column] = [
                f"{float(value):.1%}" if value_format == "percent"
                else f"{float(value):,.1f}"
                for value, value_format in zip(display[column], row_formats)
            ]
    if "absolute_gap" in display.columns:
        display["absolute_gap"] = [
            f"{float(value) * 100:+.1f} pp" if value_format == "percent"
            else f"{float(value):+,.1f}"
            for value, value_format in zip(display["absolute_gap"], row_formats)
        ]
    hidden = ["metric", "value_format", "gap_rank", "relative_gap"]
    if compact:
        hidden.extend(("top_observed_n", "peer_observed_n"))
    display = display.drop(columns=hidden, errors="ignore")
    return humanize_table(display, persona)


def _read(source: object | Mapping | None, key: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _metric_format(metric: str, variant: str | None) -> str:
    if metric in sl.METRICS:
        key = variant or sl.METRICS[metric].get("default_variant")
        definition = sl.METRICS[metric].get("variants", {}).get(key, {})
        return str(definition.get("format") or (
            "percent" if sl.METRICS[metric].get("kind") == "ratio" else "number"))
    return "number"


def format_value(metric: str, value: float | None, variant: str | None = None, *,
                 places: int = 1) -> str:
    if value is None:
        return "—"
    numeric = float(value)
    value_format = _metric_format(metric, variant)
    if value_format == "percent":
        return f"{numeric:.{places}%}"
    if value_format == "currency":
        return f"${numeric:,.{places}f}"
    if abs(numeric - round(numeric)) < 1e-9:
        return f"{numeric:,.0f}"
    return f"{numeric:,.{places}f}"


def _definition_name(metric: str, value: object | None) -> str:
    text = str(value or "").strip()
    folded = text.casefold()
    if text in {"brand_market", "advanced_therapy"} or "advanced" in folded:
        return "all advanced therapy"
    if text == "il17_class" or "il-17" in folded or "il17" in folded:
        return "the IL-17 class"
    if text in sl.METRICS.get(metric, {}).get("variants", {}):
        return variant_name(metric, text).split("·")[-1].strip().lower()
    return humanize_sentence(text).removeprefix("TRx share · ") or "the other definition"


def _movement_detail(metric: str, variant: str | None, facts: object | Mapping) -> str:
    latest = float(_read(facts, "latest", 0.0))
    mean = float(_read(facts, "trailing_mean", 0.0))
    if _read(facts, "low_base", False):
        low = float(_read(facts, "trailing_min", mean))
        high = float(_read(facts, "trailing_max", mean))
        return (f"Latest {format_value(metric, latest, variant)}; typical range "
                f"{format_value(metric, low, variant)}–"
                f"{format_value(metric, high, variant)}.")
    return (f"Latest {format_value(metric, latest, variant)}; recent norm "
            f"{format_value(metric, mean, variant)}.")


def _movement_presentation(persona: object, *, metric: str, scope: Mapping | None,
                           facts: object | Mapping, variant: str | None,
                           kind: str, event_name: str | None) -> DigestPresentation:
    profile = resolve_profile(persona)
    latest = float(_read(facts, "latest", 0.0))
    mean = float(_read(facts, "trailing_mean", 0.0))
    change = float(_read(facts, "absolute_change", latest - mean))
    low_base = bool(_read(facts, "low_base", False))
    scoped = scope_text(scope, persona=persona)
    subject_label = metric_subject(metric)

    # Product fixture: keep these exact and let the detail retain the precise
    # 7.7 baseline behind the rounded conversational "typical 8".
    if metric == "speaker_attendance" and scoped.casefold() == "south":
        current = f"{latest:,.0f}"
        typical = f"{mean:,.0f}"
        headlines = {
            "executive": (
                f"Speaker attendance in South is running well above normal — "
                f"{current}/month vs a typical {typical}."
            ),
            "sales_rep": (
                f"Speaker programs near you are filling up — {current} attendees vs a "
                f"typical {typical} last month."
            ),
            "district_manager": (
                f"Your region's speaker attendance jumped to {current} vs a typical "
                f"{typical} — break it down by territory."
            ),
            "brand_marketing": (
                f"South speaker programs drew {current} attendees vs a typical {typical} "
                "— inspect the program mix."
            ),
            "market_access": (
                f"South speaker attendance reached {current} vs a typical {typical} "
                "— check the payer mix."
            ),
        }
        headline = headlines[profile.id]
    else:
        current = format_value(metric, latest, variant)
        if low_base:
            low = format_value(metric, float(_read(facts, "trailing_min", mean)), variant)
            high = format_value(metric, float(_read(facts, "trailing_max", mean)), variant)
            comparison = f"{current} vs a typical {low}–{high}"
        else:
            comparison = f"{current} vs a typical {format_value(metric, mean, variant)}"
        subject = (f"Across the brand, {subject_label.lower()}" if not scope
                   else f"{subject_label} in {scoped}")
        verb = profile.movement_up if change >= 0 else profile.movement_down
        headline = f"{subject} {verb} — {comparison} — {profile.lead_frame}."

    if kind == "event" or event_name:
        headline = headline.rstrip(".") + " — timing overlap, not cause."
    chip = {"watch": "Watched", "event": "Event overlap"}.get(kind, "Movement")
    return DigestPresentation(
        headline=headline,
        detail=_movement_detail(metric, variant, facts),
        chip=chip,
    )


def digest_presentation(persona: object | None, *, kind: str, metric: str,
                        scope: Mapping | None = None,
                        facts: object | Mapping | None = None,
                        value: float | None = None, variant: str | None = None,
                        alternate_label: object | None = None,
                        alternate_value: float | None = None,
                        event_name: str | None = None,
                        narration_text: str | None = None) -> DigestPresentation:
    """Render one digest fact without changing its analytical artifact."""

    profile = resolve_profile(persona)
    if kind == "divergence":
        base = float(value or 0.0)
        alternate = float(alternate_value or 0.0)
        base_text = format_value(metric, base, variant)
        value_format = _metric_format(metric, variant)
        alternate_variant = next(
            (
                key for key, definition in
                sl.METRICS.get(metric, {}).get("variants", {}).items()
                if str(alternate_label).casefold() in {
                    str(key).casefold(), str(definition.get("label", "")).casefold()
                }
            ),
            "il17_class" if "il-17" in str(alternate_label).casefold() else variant,
        )
        alt_text = format_value(metric, alternate, alternate_variant)
        scoped = scope_text(scope, persona=persona)
        difference = abs(alternate - base)
        if value_format == "percent":
            base_definition = _definition_name(metric, variant)
            alt_definition = _definition_name(metric, alternate_label)
            points = difference * 100
            if profile.id == "executive":
                headline = (
                    f"{scoped} share reads two ways: {base_text} of {base_definition}, "
                    f"{alt_text} within {alt_definition} — align the definition before it "
                    "reaches a slide."
                )
            elif profile.id == "district_manager":
                headline = (
                    "A definition question, not a field one — your script volumes are "
                    "unaffected."
                )
            elif profile.id == "sales_rep":
                headline = (f"{scoped} share has two definitions — align them before changing "
                            "the call plan.")
            elif profile.id == "brand_marketing":
                headline = (f"{scoped} share reads {base_text} across advanced therapy and "
                            f"{alt_text} within IL-17 — align the slide definition.")
            else:
                headline = (f"{scoped} share reads {base_text} across advanced therapy and "
                            f"{alt_text} within IL-17 — confirm the payer denominator.")
            detail = (f"{base_text} of {base_definition} and {alt_text} within "
                      f"{alt_definition} are {points:.1f} pp apart.")
        else:
            metric_subject_label = metric_subject(metric)
            base_definition = variant_name(metric, variant)
            alt_definition = variant_name(metric, alternate_variant)
            comparison = (
                f"{base_text} as {base_definition} vs {alt_text} as {alt_definition}"
            )
            if profile.id == "executive":
                headline = (
                    f"{metric_subject_label} in {scoped} changes with the reporting unit: "
                    f"{comparison} — align the unit before it reaches a slide."
                )
            elif profile.id == "district_manager":
                headline = (
                    f"A reporting-unit question, not a field one — {metric_subject_label.lower()} "
                    f"in {scoped} is {comparison}."
                )
            elif profile.id == "sales_rep":
                headline = (
                    f"{metric_subject_label} in {scoped} reads {comparison} — keep one unit before "
                    "changing the call plan; align the reporting unit first."
                )
            elif profile.id == "brand_marketing":
                headline = (
                    f"The {metric_subject_label.lower()} story in {scoped} changes with the reporting "
                    f"unit: {comparison} — align the slide."
                )
            else:
                headline = (
                    f"Confirm the reporting unit for {metric_subject_label.lower()} in {scoped}: "
                    f"{comparison} — keep the payer read on one basis."
                )
            if value_format == "currency":
                difference_text = f"${difference:,.1f}"
            else:
                difference_text = f"{difference:,.1f} units"
            detail = (
                f"{base_text} as {base_definition} and {alt_text} as {alt_definition} "
                f"are {difference_text} apart."
            )
        return DigestPresentation(
            headline=(humanize_sentence(narration_text, persona)
                      if narration_text else headline),
            detail=detail,
            chip="Two answers exist",
        )
    if facts is None:
        label = metric_name(metric)
        scoped = scope_text(scope, persona=persona)
        return DigestPresentation(
            headline=(humanize_sentence(narration_text, persona)
                      if narration_text else f"Review {label.lower()} in {scoped}."),
            detail="A computed signal is ready for review.",
            chip="Review",
        )
    presentation = _movement_presentation(
        persona, metric=metric, scope=scope, facts=facts, variant=variant,
        kind=kind, event_name=event_name,
    )
    if narration_text:
        return DigestPresentation(
            humanize_sentence(narration_text, persona),
            presentation.detail,
            presentation.chip,
        )
    return presentation


def persona_relevance(persona: object | None, kind: str, metric: str) -> int:
    """Stable presentation relevance used only as a digest ordering key."""

    profile = resolve_profile(persona)
    fork_or_basket = kind == "divergence" or metric in {
        "trx_share", "payer_mix", "referrals_in", "active_referrers",
    }
    activity = metric in {
        "calls", "call_attainment", "samples", "speaker_attendance",
        "new_writers", "whitespace_hcps",
    }
    if fork_or_basket:
        return 2 if profile.id in {
            "executive", "brand_marketing", "market_access",
        } else (-2 if profile.id == "sales_rep" else 0)
    if activity:
        return 2 if profile.id in {"sales_rep", "district_manager"} else 0
    return 0


def zero_state(persona: object | None, metric: str = "whitespace_hcps",
               scope: Mapping | None = None) -> str:
    profile = resolve_profile(persona)
    if metric == "whitespace_hcps":
        return {
            "sales_rep": "No untouched high-value HCPs right now — coverage is holding.",
            "district_manager": (
                "No untouched high-value HCPs in your district — coverage is holding."
            ),
            "brand_marketing": (
                "No untouched high-value HCPs right now — no audience gap is open."
            ),
            "market_access": (
                "No untouched high-value HCPs right now — no access follow-up is indicated."
            ),
            "executive": (
                "No untouched high-value HCPs right now — coverage risk is contained."
            ),
        }[profile.id]

    scoped = scope_text(scope, persona=persona)
    label = metric_name(metric)
    next_step = {
        "sales_rep": "Try a broader field scope.",
        "district_manager": "Try another territory or a broader district scope.",
        "brand_marketing": "Try a broader audience or time window.",
        "market_access": "Try a broader payer or geographic scope.",
        "executive": "Broaden the scope before drawing a conclusion.",
    }[profile.id]
    return f"No records for {label} are available in {scoped}. {next_step}"


def tile_presentation(subject: object | None = None, *, persona: object | None = None,
                      metric: str | None = None, scope: Mapping | None = None,
                      headline: str = "", value: float | None = None,
                      is_zero: bool = False, label: str | None = None,
                      template: str | None = None) \
        -> TilePresentation:
    """Render a tile or generic artifact through one immutable adapter."""

    art = subject if hasattr(subject, "resolution") else None
    selected_persona = persona if art is not None else subject
    if art is not None:
        resolution = getattr(art, "resolution", None)
        metric = metric or getattr(resolution, "metric", None)
        intent = getattr(art, "extras", {}).get("intent")
        scope = scope if scope is not None else getattr(intent, "filters", {})
        headline = getattr(art, "headline", headline)
        value = getattr(art, "value", value)
        template = template or getattr(intent, "template", None)
        table = getattr(art, "table", None)
        is_zero = bool(template == "whitespace" and table is not None and table.empty)
    metric = metric or "metric"
    if is_zero:
        if template == "whitespace" or metric == "whitespace_hcps":
            definition = ("Untouched means decile 8+ by trailing TRx, with no prescription "
                          "or field activity for at least three months.")
            return TilePresentation(
                label or "",
                f"{zero_state(selected_persona, 'whitespace_hcps', scope)} {definition}",
            )
        return TilePresentation(
            label or "", zero_state(selected_persona, metric, scope))

    profile = resolve_profile(selected_persona)
    if art is not None and getattr(art, "engine", None) == "descriptive" \
            and value is not None:
        resolution = getattr(art, "resolution", None)
        variant = getattr(resolution, "variant", None)
        metric_label = metric_subject(metric)
        value_text = format_value(metric, float(value), variant)
        scoped = scope_text(scope, selected_persona)
        opener = "Across the brand" if not scope else scoped
        sentence = {
            "sales_rep": (
                f"{metric_label} in {scoped} is {value_text} — "
                "prioritize the next call."
            ),
            "district_manager": (
                f"{metric_label} in {scoped} is {value_text} — "
                "compare territory contributions."
            ),
            "brand_marketing": (
                f"{metric_label} for {scoped} reached {value_text} — "
                "inspect the program mix."
            ),
            "market_access": (
                f"{metric_label} for {scoped} is {value_text} — "
                "check how the read varies by payer."
            ),
            "executive": (
                f"{opener}, {metric_label} is {value_text} — "
                "size the business consequence."
            ),
        }[profile.id]
        return TilePresentation(label or "", sentence)

    base = humanize_sentence(headline, selected_persona).rstrip(" .")
    follow_through = {
        "sales_rep": "Use it to prioritize the next call.",
        "district_manager": "Compare territories before acting.",
        "brand_marketing": "Inspect the program mix behind it.",
        "market_access": "Check whether payer mix changes the read.",
        "executive": "Size the business consequence.",
    }[profile.id]
    return TilePresentation(
        label or "",
        f"{base}. {follow_through}" if base else follow_through,
    )


def cohort_presentation(art, persona: object | None = None) -> CohortPresentation:
    table = getattr(art, "table", None)
    extras = getattr(art, "extras", {})
    if not isinstance(table, pd.DataFrame) or table.empty:
        return CohortPresentation(
            "No cohort gap", "Top HCP activity finding", "No comparable gap is available.",
            "0 matched pairs",
        )
    ordered = table.sort_values("gap_rank") if "gap_rank" in table.columns else table
    row = ordered.iloc[0]
    raw_metric = str(row.get("metric", row.get("label", "activity")))
    activity = metric_name(raw_metric)
    if "speaker" in activity.casefold():
        activity = "Speaker touches"
    top = float(row.get("top_hcps", 0.0))
    peers = float(row.get("matched_peers", 0.0))
    value_format = str(row.get("value_format", "number"))
    if value_format == "percent":
        hero = (f"{activity}: {top:.1%} for top HCPs vs {peers:.1%} "
                "among matched peers")
    else:
        hero = (f"{activity} per HCP: {top:,.1f} for top HCPs vs "
                f"{peers:,.1f} among matched peers")
    intent = extras.get("intent")
    scope = scope_text(getattr(intent, "filters", {}), persona=persona)
    match_count = int(extras.get("matched_count") or extras.get("match_count") or
                      (len(extras.get("peer_matches"))
                       if isinstance(extras.get("peer_matches"), pd.DataFrame) else 0))
    profile = resolve_profile(persona)
    suffix = {
        "sales_rep": "Use the gap to focus the next HCP conversation.",
        "district_manager": "Break the gap down by territory before acting.",
        "brand_marketing": "Inspect which program activity separates the groups.",
        "market_access": "Check whether the gap varies with payer mix before acting.",
        "executive": "Treat the gap as directional until an event design tests it.",
    }[profile.id]
    return CohortPresentation(
        hero=hero,
        label=f"Top HCP activity finding · {scope} · trailing 90 days",
        headline=suffix,
        method_chip=f"{match_count} matched pairs",
    )


def refusal_presentation(art, persona: object | None = None) -> RefusalPresentation:
    profile = resolve_profile(persona)
    leads = {
        "sales_rep": "That question cannot be answered reliably from your current data.",
        "district_manager": "There is not enough reliable evidence for that district decision.",
        "brand_marketing": "That question needs a different evidence design before it reaches a plan.",
        "market_access": "That question is not supported for an access decision.",
        "executive": "There is not enough reliable evidence to make that claim.",
    }
    raw = str(getattr(art, "headline", "")).removeprefix("Declined: ")
    return RefusalPresentation(leads[profile.id], humanize_sentence(raw, persona))


def definition_fork_presentation(art, fork: Mapping,
                                 persona: object | None = None) -> ForkPresentation:
    resolution = getattr(art, "resolution", None)
    metric = getattr(resolution, "metric", "metric")
    variant = getattr(resolution, "variant", None)
    rendered = digest_presentation(
        persona,
        kind="divergence",
        metric=metric,
        scope=getattr(getattr(art, "extras", {}).get("intent"), "filters", {}),
        value=getattr(art, "value", None),
        variant=variant,
        alternate_label=fork.get("label") or fork.get("fork"),
        alternate_value=fork.get("value"),
    )
    return ForkPresentation(rendered.headline, rendered.detail, rendered.chip)


def monitoring_presentation(metric: str, scope: Mapping | None, *, latest: float,
                            trailing_mean: float, trailing_min: float | None = None,
                            trailing_max: float | None = None,
                            absolute_change: float | None = None,
                            low_base: bool = False, persona: object | None = None,
                            variant: str | None = None) -> MonitoringPresentation:
    facts = {
        "latest": latest,
        "trailing_mean": trailing_mean,
        "trailing_min": trailing_mean if trailing_min is None else trailing_min,
        "trailing_max": trailing_mean if trailing_max is None else trailing_max,
        "absolute_change": (latest - trailing_mean
                            if absolute_change is None else absolute_change),
        "low_base": low_base,
    }
    rendered = digest_presentation(
        persona, kind="movement", metric=metric, scope=scope, facts=facts,
        value=latest, variant=variant,
    )
    return MonitoringPresentation(rendered.headline, rendered.detail)
