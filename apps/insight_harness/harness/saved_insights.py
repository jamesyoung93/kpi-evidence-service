"""Session-safe saved tiles and watches.

This module intentionally has no filesystem or Streamlit dependency. A caller
owns one store per session and may serialize records explicitly if durable,
identity-scoped persistence is added later.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from . import tiles


SCHEMA_VERSION = 3
SESSION_STORE_KEY = "saved_insights_store"
_INHERIT = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _identity_id(identity: tuple) -> str:
    payload = json.dumps(identity, sort_keys=True, default=list, separators=(",", ":"))
    return "insight_" + hashlib.sha256(payload.encode()).hexdigest()[:12]


def _filters_record(filters: tiles.FilterItems) -> dict:
    return {
        dimension: list(value) if isinstance(value, tuple) else value
        for dimension, value in filters
    }


@dataclass(frozen=True)
class SavedInsight:
    """One versioned saved question; labels and timestamps are not identity."""

    id: str
    label: str
    spec: tiles.SavedQuestionSpec
    watched: bool = True
    catalog_tile_id: str | None = None
    added_at: str = ""
    stale_reason: str | None = None
    schema_version: int = SCHEMA_VERSION

    @property
    def identity(self) -> tuple:
        """Full behavior/display identity used for deduplication."""

        return (
            self.spec.metric,
            self.spec.filters,
            self.spec.source,
            self.spec.variant,
            self.spec.window,
            self.spec.basis,
            self.spec.viz_kind,
            self.spec.default_personas,
            self.spec.question_class,
            self.spec.breakdown_dimension,
            self.spec.retrieval_template,
            self.watched,
        )

    @property
    def is_stale(self) -> bool:
        return self.stale_reason is not None

    @property
    def metric(self) -> str:
        return self.spec.metric

    @property
    def filters(self) -> dict:
        return dict(self.spec.filters)

    @property
    def source(self) -> str | None:
        return self.spec.source

    @property
    def variant(self) -> str | None:
        return self.spec.variant

    @property
    def window(self) -> str:
        return self.spec.window

    @property
    def basis(self) -> str:
        return self.spec.basis

    @property
    def viz_kind(self) -> str:
        return self.spec.viz_kind

    @property
    def question_class(self) -> str:
        return self.spec.question_class

    def to_record(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "label": self.label,
            "catalog_tile_id": self.catalog_tile_id,
            "watched": self.watched,
            "added_at": self.added_at,
            "stale_reason": self.stale_reason,
            "intent_spec": {
                "metric": self.spec.metric,
                "filters": _filters_record(self.spec.filters),
                "source": self.spec.source,
                "variant": self.spec.variant,
                "window": self.spec.window,
                "basis": self.spec.basis,
                "viz_kind": self.spec.viz_kind,
                "default_personas": list(self.spec.default_personas),
                "question_class": self.spec.question_class,
                "breakdown_dimension": self.spec.breakdown_dimension,
                "retrieval_template": self.spec.retrieval_template,
            },
        }


def _make_saved(spec: tiles.SavedQuestionSpec, *, label: str, watched: bool,
                catalog_tile_id: str | None, added_at: str | None,
                insight_id: str | None, allow_stale: bool) -> SavedInsight:
    errors = tiles.spec_errors(spec)
    if errors and not allow_stale:
        raise ValueError("; ".join(errors))
    if catalog_tile_id is not None and catalog_tile_id not in tiles.TILES_BY_ID:
        if not allow_stale:
            raise ValueError(f"unknown catalog tile: {catalog_tile_id!r}")
        errors = (*errors, f"unknown catalog tile: {catalog_tile_id!r}")
    provisional = SavedInsight(
        id="",
        label=label or spec.metric,
        spec=spec,
        watched=bool(watched),
        catalog_tile_id=catalog_tile_id,
        added_at=added_at or _now(),
        stale_reason="; ".join(errors) if errors else None,
    )
    return replace(provisional, id=insight_id or _identity_id(provisional.identity))


def create_saved_insight(metric: str, filters: Mapping | tiles.FilterItems | None = None,
                         *, label: str | None = None, source: str | None = None,
                         variant: str | None = None, window: str = "Latest",
                         basis: str = "MoM", viz_kind: str = "sparkline",
                          default_personas: tuple[str, ...] = (), watched: bool = True,
                          question_class: str = "Descriptive",
                          breakdown_dimension: str | None = None,
                          retrieval_template: str | None = None,
                         catalog_tile_id: str | None = None,
                         added_at: str | None = None,
                         insight_id: str | None = None) -> SavedInsight:
    """Create a new registry-valid insight. Invalid specs are rejected."""

    spec = tiles.SavedQuestionSpec(
        metric=metric,
        filters=tiles.freeze_filters(filters),
        source=source,
        variant=variant,
        window=window,
        basis=basis,
        viz_kind=viz_kind,
        default_personas=default_personas,
        question_class=question_class,
        breakdown_dimension=breakdown_dimension,
        retrieval_template=retrieval_template,
    )
    return _make_saved(
        spec,
        label=label or metric,
        watched=watched,
        catalog_tile_id=catalog_tile_id,
        added_at=added_at,
        insight_id=insight_id,
        allow_stale=False,
    )


def save_spec(spec: tiles.SavedQuestionSpec, *, label: str,
              catalog_tile_id: str | None = None, watched: bool = True,
              scope=None) -> SavedInsight:
    """Snapshot one already-materialized spec and its active external scope."""

    scoped = replace(spec, filters=tiles.freeze_filters(
        tiles.effective_spec_filters(spec, scope)))
    return _make_saved(scoped, label=label, watched=watched,
                       catalog_tile_id=catalog_tile_id, added_at=None,
                       insight_id=None, allow_stale=False)


def save_catalog_tile(tile: tiles.TileDefinition | str, *, window: str | None = None,
                      basis: str | None = None, region=tiles.ALL_REGIONS, scope=None,
                      source=_INHERIT, variant=_INHERIT, viz_kind: str | None = None,
                      watched: bool = True, label: str | None = None) -> SavedInsight:
    """Create a saved insight from a catalog tile and its active controls."""

    definition = tiles.tile_definition(tile)
    materialized = tiles.materialize_spec(
        definition,
        window=window,
        basis=basis,
        source=None if source is _INHERIT else source,
        variant=None if variant is _INHERIT else variant,
        viz_kind=viz_kind,
    )
    selected_scope = region if scope is None else scope
    return save_spec(materialized.spec, label=label or definition.label,
                     catalog_tile_id=definition.id, watched=watched,
                     scope=selected_scope)


def _window_control(value) -> str:
    if value is None:
        return "Latest"
    if isinstance(value, Mapping) and value.get("kind") == "last_n":
        return _window_control(value.get("n"))
    if isinstance(value, int):
        match = next((name for name, months in tiles.WINDOW_CONTROLS.items()
                      if months == value), None)
        return match or str(value)
    text = str(value)
    aliases = {name.lower(): name for name in tiles.WINDOW_CONTROLS}
    return aliases.get(text.lower(), text)


def _basis_control(value) -> str:
    if value is None:
        return "MoM"
    if isinstance(value, str) and value in tiles.BASIS_CONTROLS:
        return value
    inverse = {code: label for label, code in tiles.BASIS_CONTROLS.items()}
    return inverse.get(str(value), str(value))


def migrate_legacy_watch(record: Mapping) -> SavedInsight:
    """Upgrade a metric+filters legacy watch without hiding stale records."""

    raw = record.get("intent_spec") if isinstance(record.get("intent_spec"), Mapping) else record
    spec = tiles.SavedQuestionSpec(
        metric=str(raw.get("metric") or ""),
        filters=tiles.freeze_filters(raw.get("filters") or {}),
        source=raw.get("source"),
        variant=raw.get("variant"),
        window=_window_control(raw.get("window")),
        basis=_basis_control(raw.get("basis") or raw.get("compare_basis")),
        viz_kind=str(raw.get("viz_kind") or raw.get("viz") or "sparkline"),
        default_personas=tuple(raw.get("default_personas") or ()),
        question_class=str(raw.get("question_class") or "Descriptive"),
        breakdown_dimension=raw.get("breakdown_dimension") or raw.get("dim_breakdown"),
        retrieval_template=raw.get("retrieval_template") or raw.get("template"),
    )
    return _make_saved(
        spec,
        label=str(record.get("label") or spec.metric or "Retired insight"),
        watched=bool(record.get("watched", True)),
        catalog_tile_id=record.get("catalog_tile_id"),
        added_at=record.get("added_at") or record.get("added"),
        insight_id=record.get("id"),
        allow_stale=True,
    )


def saved_insight_from_record(record: SavedInsight | Mapping) -> SavedInsight:
    if isinstance(record, SavedInsight):
        errors = tiles.spec_errors(record.spec)
        if errors and not record.stale_reason:
            return replace(record, stale_reason="; ".join(errors))
        return record
    try:
        version = int(record.get("schema_version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    if version != SCHEMA_VERSION:
        return migrate_legacy_watch(record)
    raw = record.get("intent_spec") or {}
    spec = tiles.SavedQuestionSpec(
        metric=str(raw.get("metric") or ""),
        filters=tiles.freeze_filters(raw.get("filters") or {}),
        source=raw.get("source"),
        variant=raw.get("variant"),
        window=_window_control(raw.get("window")),
        basis=_basis_control(raw.get("basis")),
        viz_kind=str(raw.get("viz_kind") or "sparkline"),
        default_personas=tuple(raw.get("default_personas") or ()),
        question_class=str(raw.get("question_class") or "Descriptive"),
        breakdown_dimension=raw.get("breakdown_dimension"),
        retrieval_template=raw.get("retrieval_template"),
    )
    saved = _make_saved(
        spec,
        label=str(record.get("label") or spec.metric),
        watched=bool(record.get("watched", True)),
        catalog_tile_id=record.get("catalog_tile_id"),
        added_at=record.get("added_at"),
        insight_id=record.get("id"),
        allow_stale=True,
    )
    # Recompute staleness against the current registry so a restored metric or
    # dimension automatically revives instead of retaining obsolete metadata.
    return saved


def migrate_legacy_watches(records: Iterable[SavedInsight | Mapping]) -> tuple[SavedInsight, ...]:
    return tuple(saved_insight_from_record(record) for record in records)


@dataclass(frozen=True)
class SaveResult:
    insight: SavedInsight
    added: bool


class InMemorySavedInsightStore:
    """Insertion-ordered, per-session store with full-identity deduplication."""

    def __init__(self, records: Iterable[SavedInsight | Mapping] = ()) -> None:
        self._items: dict[str, SavedInsight] = {}
        self._identities: dict[tuple, str] = {}
        for record in records:
            self.add(saved_insight_from_record(record))

    def __len__(self) -> int:
        return len(self._items)

    def all(self) -> tuple[SavedInsight, ...]:
        return tuple(self._items.values())

    def get(self, insight_id: str) -> SavedInsight | None:
        return self._items.get(insight_id)

    def add(self, insight: SavedInsight | Mapping) -> SaveResult:
        saved = saved_insight_from_record(insight)
        existing_id = self._identities.get(saved.identity)
        if existing_id is not None:
            return SaveResult(self._items[existing_id], False)
        if saved.id in self._items:
            raise ValueError(f"saved insight id collision: {saved.id!r}")
        self._items[saved.id] = saved
        self._identities[saved.identity] = saved.id
        return SaveResult(saved, True)

    def save(self, metric: str, filters=None, **kwargs) -> SaveResult:
        return self.add(create_saved_insight(metric, filters, **kwargs))

    def remove(self, insight_id: str) -> bool:
        saved = self._items.pop(insight_id, None)
        if saved is None:
            return False
        self._identities.pop(saved.identity, None)
        return True

    def clear(self) -> None:
        self._items.clear()
        self._identities.clear()

    def records(self) -> list[dict]:
        return [insight.to_record() for insight in self._items.values()]


def session_store(session_state: MutableMapping,
                  key: str = SESSION_STORE_KEY) -> InMemorySavedInsightStore:
    """Get/create the store owned by one caller-provided session mapping."""

    current = session_state.get(key)
    if isinstance(current, InMemorySavedInsightStore):
        return current
    if current is None:
        store = InMemorySavedInsightStore()
    elif isinstance(current, (list, tuple)):
        store = InMemorySavedInsightStore(current)
    else:
        raise TypeError(f"{key!r} must contain saved-insight records or a session store")
    session_state[key] = store
    return store
