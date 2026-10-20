"""Pipeline-only execution for registry tiles and saved insights."""
from __future__ import annotations

from dataclasses import dataclass

from . import pipeline, tiles
from .saved_insights import SavedInsight


_DEFAULT = object()


class StaleInsightError(ValueError):
    """Raised when a retained legacy record can no longer be evaluated."""


@dataclass(frozen=True)
class TileEvaluation:
    spec: tiles.SavedQuestionSpec
    canonical_question: str
    intent: object
    artifact: object
    catalog_tile_id: str | None = None
    saved_insight_id: str | None = None
    override_disclosures: tuple[str, ...] = ()

    @property
    def result_hash(self) -> str:
        return self.artifact.result_hash


def evaluate_spec(spec: tiles.SavedQuestionSpec, *,
                  region=tiles.ALL_REGIONS, scope=None,
                  translation: dict | None = None,
                  catalog_tile_id: str | None = None,
                  saved_insight_id: str | None = None,
                  override_disclosures: tuple[str, ...] = ()) -> TileEvaluation:
    """Evaluate through the same governed entry point used by other surfaces."""

    tiles.require_valid_spec(spec)
    selected_scope = region if scope is None else scope
    intent = tiles.intent_for_spec(spec, selected_scope)
    artifact = pipeline.answer_intent(
        intent,
        source=spec.source,
        variant=spec.variant,
        translation=translation,
    )
    return TileEvaluation(
        spec=spec,
        canonical_question=intent.question,
        intent=intent,
        artifact=artifact,
        catalog_tile_id=catalog_tile_id,
        saved_insight_id=saved_insight_id,
        override_disclosures=tuple(override_disclosures),
    )


def evaluate_tile(tile: tiles.TileDefinition | str, *, window: str | None = None,
                  basis: str | None = None,
                  region=tiles.ALL_REGIONS, scope=None,
                  source=_DEFAULT, variant=_DEFAULT,
                  viz_kind: str | None = None,
                  translation: dict | None = None) -> TileEvaluation:
    definition = tiles.tile_definition(tile)
    materialized = tiles.materialize_spec(
        definition,
        window=window,
        basis=basis,
        source=None if source is _DEFAULT else source,
        variant=None if variant is _DEFAULT else variant,
        viz_kind=viz_kind,
    )
    return evaluate_spec(
        materialized.spec,
        scope=region if scope is None else scope,
        translation=translation,
        catalog_tile_id=definition.id,
        override_disclosures=materialized.disclosures,
    )


def evaluate_saved(insight: SavedInsight, *,
                   region=tiles.ALL_REGIONS, scope=None,
                   translation: dict | None = None) -> TileEvaluation:
    if insight.is_stale:
        raise StaleInsightError(insight.stale_reason)
    return evaluate_spec(
        insight.spec,
        scope=region if scope is None else scope,
        translation=translation,
        catalog_tile_id=insight.catalog_tile_id,
        saved_insight_id=insight.id,
    )


def evaluate_many(insights, *, region=tiles.ALL_REGIONS, scope=None) -> tuple[TileEvaluation, ...]:
    selected_scope = region if scope is None else scope
    return tuple(evaluate_saved(insight, scope=selected_scope) for insight in insights)
