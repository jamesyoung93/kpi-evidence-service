"""Governed market-basket resolution and basket-aware TRx share artifacts.

Basket choice is a semantic decision, not a chart control.  This module owns
the immutable membership registry, the adoption-stage default, explicit user
overrides, and the disclosure carried into the answer artifact and its hash.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

import pandas as pd

from . import semantic_layer as sl
from .provenance import (AnswerArtifact, TIER_ABSTAINED, TIER_VERIFIED,
                         _stable_hash)


BASKET_REGISTRY_VERSION = "market_baskets_v1"
RECENT_ADOPTER = "recent_adopter"
ESTABLISHED = "established"
NEVER_ADOPTER = "never_adopter"
MIXED_STAGE = "mixed"
UNKNOWN_STAGE = "unknown"
ADOPTION_STAGES = frozenset({
    RECENT_ADOPTER, ESTABLISHED, NEVER_ADOPTER, MIXED_STAGE, UNKNOWN_STAGE,
})


@dataclass(frozen=True)
class BasketMember:
    id: str
    label: str
    column: str


@dataclass(frozen=True)
class MarketBasket:
    id: str
    label: str
    owner: str
    semantic_variant: str
    denominator_column: str
    members: tuple[BasketMember, ...]
    notes: str


_BASKETS = {
    "il17_class": MarketBasket(
        id="il17_class",
        label="IL-17 class",
        owner="Brand Analytics",
        semantic_variant="il17_class",
        denominator_column="il17_class_trx",
        members=(
            BasketMember("brand", "Brand", "trx_units"),
            BasketMember("competitor_a", "IL-17 competitor A",
                         "il17_competitor_a_trx"),
            BasketMember("competitor_b", "IL-17 competitor B",
                         "il17_competitor_b_trx"),
        ),
        notes="Class basket for recent adopters; membership is governed and versioned.",
    ),
    "advanced_therapy": MarketBasket(
        id="advanced_therapy",
        label="advanced-therapy market",
        owner="Brand Analytics",
        semantic_variant="advanced_therapy",
        denominator_column="advanced_therapy_trx",
        members=(
            BasketMember("brand", "Brand", "trx_units"),
            BasketMember("competitor_a", "IL-17 competitor A",
                         "il17_competitor_a_trx"),
            BasketMember("competitor_b", "IL-17 competitor B",
                         "il17_competitor_b_trx"),
            BasketMember("advanced_other", "Other advanced therapies",
                         "advanced_other_trx"),
        ),
        notes="Broader basket for established or mixed adoption-stage scopes.",
    ),
}
BASKETS: Mapping[str, MarketBasket] = MappingProxyType(_BASKETS)


@dataclass(frozen=True)
class BasketResolution:
    basket_id: str
    semantic_variant: str
    adoption_stage: str
    overridden: bool
    reason: str
    disclosure: str
    registry_version: str = BASKET_REGISTRY_VERSION


def _validate_registry() -> None:
    variants = sl.METRICS["trx_share"]["variants"]
    for basket_id, basket in BASKETS.items():
        if basket.id != basket_id:
            raise ValueError(f"basket registry key/id mismatch: {basket_id}")
        if basket.semantic_variant not in variants:
            raise ValueError(f"unregistered share variant: {basket.semantic_variant}")
        variant = variants[basket.semantic_variant]
        if variant.get("denominator") != basket.denominator_column:
            raise ValueError(f"denominator drift for basket {basket_id}")
        if variant.get("basket") != basket_id:
            raise ValueError(f"semantic basket link drift for {basket_id}")
        member_columns = {member.column for member in basket.members}
        if "trx_units" not in member_columns or len(member_columns) != len(basket.members):
            raise ValueError(f"invalid membership for basket {basket_id}")


_validate_registry()


def registry_fingerprint() -> str:
    """Stable governance identity included in basket artifacts."""
    return _stable_hash({key: asdict(value) for key, value in BASKETS.items()})


def adoption_stage_for_scope(filters: Mapping | None = None) -> str:
    """Resolve a scope to one stage without silently averaging definitions."""
    accounts = sl.apply_filters(sl.load_accounts(), dict(filters or {}))
    if accounts.empty or "adoption_stage" not in accounts:
        return UNKNOWN_STAGE
    stages = sorted(set(accounts["adoption_stage"].dropna().astype(str)))
    if not stages:
        return UNKNOWN_STAGE
    return stages[0] if len(stages) == 1 else MIXED_STAGE


def resolve_basket(adoption_stage: str | None = None,
                   override: str | None = None) -> BasketResolution:
    """Choose and disclose the governed basket for an adoption stage.

    Recent adopters default to the class basket. Established HCPs use the
    broader market. Mixed, never-adopter, and unknown scopes use the broader
    basket so an aggregate never combines incompatible denominators.
    """
    stage = adoption_stage or UNKNOWN_STAGE
    if stage not in ADOPTION_STAGES:
        raise ValueError(f"unregistered adoption stage: {stage}")
    if override is not None and override not in BASKETS:
        raise ValueError(f"unregistered market basket override: {override}")

    governed = "il17_class" if stage == RECENT_ADOPTER else "advanced_therapy"
    basket_id = override or governed
    basket = BASKETS[basket_id]
    overridden = override is not None
    if overridden and override != governed:
        reason = (f"explicit user basket selection replaced the {stage} adaptive "
                  f"default ({BASKETS[governed].label})")
        disclosure = (f"Basket override: {basket.label}; governed {stage} default "
                      f"would be {BASKETS[governed].label}.")
    elif overridden:
        reason = f"explicit user basket selection matches the {stage} adaptive default"
        disclosure = (f"Basket selected: {basket.label}; this matches the governed "
                      f"{stage} default.")
    else:
        reason = f"adaptive governed default for adoption stage {stage}"
        disclosure = f"Basket: {basket.label} · adaptive default for {stage}."
    return BasketResolution(
        basket_id=basket_id,
        semantic_variant=basket.semantic_variant,
        adoption_stage=stage,
        overridden=overridden,
        reason=reason,
        disclosure=disclosure,
    )


def _member_reconciliation(frame: pd.DataFrame, basket: MarketBasket) -> dict:
    missing = [member.column for member in basket.members if member.column not in frame]
    if basket.denominator_column not in frame:
        missing.append(basket.denominator_column)
    if missing:
        raise ValueError("basket columns unavailable: " + ", ".join(sorted(set(missing))))
    member_total = sum(float(frame[member.column].sum()) for member in basket.members)
    denominator = float(frame[basket.denominator_column].sum())
    return {
        "member_total": member_total,
        "denominator": denominator,
        "absolute_error": abs(member_total - denominator),
        "reconciled": abs(member_total - denominator) <= max(1e-6, abs(denominator) * 1e-9),
    }


def reconciliation_for_scope(basket_id: str, filters: Mapping | None = None,
                             months: Sequence[str] | None = None,
                             source: str = "source_a") -> dict:
    """Return the governed member/denominator check for an exact tile scope."""

    if basket_id not in BASKETS:
        raise ValueError(f"unregistered market basket: {basket_id}")
    frame = sl.apply_filters(sl.load_fact(source), dict(filters or {}))
    if months is not None:
        frame = frame[frame["month"].isin(list(months))]
    return _member_reconciliation(frame, BASKETS[basket_id])


def answer_basket_share(filters: Mapping | None = None,
                        months: Sequence[str] | None = None,
                        adoption_stage: str | None = None,
                        basket_override: str | None = None,
                        source: str = "source_a",
                        question: str | None = None) -> AnswerArtifact:
    """Compute a ratio-of-sums share artifact under one governed basket."""
    scope = dict(filters or {})
    stage = adoption_stage or adoption_stage_for_scope(scope)
    basket_resolution = resolve_basket(stage, basket_override)
    basket = BASKETS[basket_resolution.basket_id]
    semantic_resolution = sl.resolve(
        "trx_share", source=source, variant=basket.semantic_variant)
    if semantic_resolution.source != source:
        raise ValueError(f"source {source!r} is not registered for basket-aware share")
    semantic_resolution.reason = (
        f"{basket_resolution.reason}; {basket.owner}-owned {basket.label} definition "
        f"on {sl.SOURCES[source]['name']}")

    frame = sl.apply_filters(sl.load_fact(source), scope)
    selected_months = list(months) if months is not None else (
        [str(frame["month"].max())] if not frame.empty else [])
    if selected_months:
        frame = frame[frame["month"].isin(selected_months)]
    question = question or f"What is TRx share in the {basket.label}?"
    if frame.empty:
        artifact = AnswerArtifact(
            question, "Descriptive", TIER_ABSTAINED, "basket_share",
            headline=f"Declined: no governed observations exist for {sl.scope_string(scope)}.",
            resolution=semantic_resolution,
        )
        artifact.caveats.append(basket_resolution.disclosure)
        artifact.data_version = sl.data_version()
        return artifact

    reconciliation = _member_reconciliation(frame, basket)
    if not reconciliation["reconciled"]:
        raise ValueError(
            f"basket denominator does not reconcile to governed members: {basket.id}")
    value = sl.aggregate_metric(frame, "trx_share", basket.semantic_variant)
    if pd.isna(value):
        tier = TIER_ABSTAINED
        headline = f"TRx share is undefined in the {basket.label}: denominator is zero."
    else:
        tier = TIER_VERIFIED
        headline = (f"TRx share in the {basket.label}: {value:.1%}. "
                    f"{basket_resolution.disclosure}")
    artifact = AnswerArtifact(
        question, "Descriptive", tier, "basket_share", headline=headline,
        value=None if pd.isna(value) else float(value), resolution=semantic_resolution,
        code=(
            f"frame = filter(load('{source}'), {scope})\n"
            f"frame = frame[frame.month.isin({selected_months})]\n"
            f"value = frame.trx_units.sum() / frame.{basket.denominator_column}.sum()"
        ),
    )
    artifact.caveats.extend([
        basket_resolution.disclosure,
        basket.notes,
        "Market share is descriptive; basket choice changes the denominator and must be compared like-for-like.",
    ])
    artifact.extras.update({
        "basket": asdict(basket),
        "basket_resolution": asdict(basket_resolution),
        "basket_registry_fingerprint": registry_fingerprint(),
        "basket_reconciliation": reconciliation,
        "scope": scope,
        "months": selected_months,
    })
    artifact.data_version = sl.data_version()
    return artifact
