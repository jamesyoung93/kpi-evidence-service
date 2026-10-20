"""Persona presets and session-local tile layout state.

The functions in this module are deliberately UI-agnostic.  Callers provide a
session-owned mutable mapping (for example ``st.session_state``); no profile or
layout state is written to shared files or module globals.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from . import tiles


SESSION_LAYOUTS_KEY = "_insight_harness_persona_layouts"


@dataclass(frozen=True)
class FrozenScope(Mapping[str, tiles.FilterValue]):
    """Hashable immutable mapping used by persona and digest defaults."""

    _items: tiles.FilterItems = ()

    def __init__(self, value: Mapping | tiles.FilterItems | None = None) -> None:
        object.__setattr__(self, "_items", tiles.require_valid_scope(value))

    def __getitem__(self, key: str) -> tiles.FilterValue:
        return dict(self._items)[key]

    def __iter__(self) -> Iterator[str]:
        return (dimension for dimension, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return hash(self._items)


@dataclass(frozen=True)
class PersonaDefinition:
    """A validated, immutable bundle of persona defaults."""

    id: str
    label: str
    default_tile_ids: tuple[str, ...]
    default_scope: FrozenScope
    default_window: str
    default_basis: str
    digest_scope: FrozenScope

    def __post_init__(self) -> None:
        if not isinstance(self.default_scope, FrozenScope):
            object.__setattr__(self, "default_scope", FrozenScope(self.default_scope))
        if not isinstance(self.digest_scope, FrozenScope):
            object.__setattr__(self, "digest_scope", FrozenScope(self.digest_scope))
        if not self.id or not self.label:
            raise ValueError("persona id and label are required")
        if not self.default_tile_ids:
            raise ValueError(f"persona {self.id!r} needs at least one default tile")
        if len(self.default_tile_ids) != len(set(self.default_tile_ids)):
            raise ValueError(f"persona {self.id!r} has duplicate default tiles")
        unknown = [tile_id for tile_id in self.default_tile_ids
                   if tile_id not in tiles.TILES_BY_ID]
        if unknown:
            raise ValueError(f"persona {self.id!r} references unknown tiles: {unknown!r}")
        if tiles.scope_errors(self.default_scope):
            raise ValueError(f"persona {self.id!r} has unknown default scope")
        if tiles.scope_errors(self.digest_scope):
            raise ValueError(f"persona {self.id!r} has unknown digest scope")
        if self.default_window not in tiles.WINDOW_CONTROLS:
            raise ValueError(f"persona {self.id!r} has unknown window: "
                             f"{self.default_window!r}")
        if self.default_basis not in tiles.BASIS_CONTROLS:
            raise ValueError(f"persona {self.id!r} has unknown basis: "
                             f"{self.default_basis!r}")


def _first_scope(dimension: str) -> FrozenScope:
    values = tiles.dimension_values(dimension)
    if not values:
        raise ValueError(f"persona scope dimension {dimension!r} has no registered values")
    return FrozenScope({dimension: values[0]})


_NATIONAL = FrozenScope()
_REP_SCOPE = _first_scope("territory")
_DM_SCOPE = _first_scope("district")


PERSONAS = (
    PersonaDefinition(
        "sales_rep",
        "Sales Rep",
        ("trx", "call_attainment", "calls", "whitespace_hcps", "samples"),
        _REP_SCOPE,
        "R3M",
        "MoM",
        _REP_SCOPE,
    ),
    PersonaDefinition(
        "district_manager",
        "District Manager",
        ("trx", "nrx", "call_attainment", "payer_mix", "new_writers",
         "incoming_referrals"),
        _DM_SCOPE,
        "R3M",
        "MoM",
        _DM_SCOPE,
    ),
    PersonaDefinition(
        "brand_marketing",
        "Brand Marketing",
        ("trx", "nrx", "nbrx", "trx_share", "new_writers", "hcp_cohort"),
        _NATIONAL,
        "R6M",
        "YoY",
        _NATIONAL,
    ),
    PersonaDefinition(
        "market_access",
        "Market Access",
        ("trx_share", "incoming_referrals", "active_referrers", "payer_mix",
         "commercial_trx", "trx"),
        _NATIONAL,
        "R6M",
        "YoY",
        _NATIONAL,
    ),
    PersonaDefinition(
        "executive",
        "Executive",
        ("trx", "trx_share", "nbrx", "new_writers", "commercial_trx",
         "hcp_cohort"),
        _NATIONAL,
        "R12M",
        "YoY",
        _NATIONAL,
    ),
)
PERSONAS_BY_ID = MappingProxyType({persona.id: persona for persona in PERSONAS})


@dataclass(frozen=True)
class LayoutResolution:
    """A renderable layout plus saved ids whose definitions were retired."""

    tile_ids: tuple[str, ...]
    retired_tile_ids: tuple[str, ...]
    customized: bool

    @property
    def all_saved_tile_ids(self) -> tuple[str, ...]:
        return self.tile_ids + self.retired_tile_ids


def persona_definition(persona: PersonaDefinition | str) -> PersonaDefinition:
    """Resolve a definition or public persona id."""

    if isinstance(persona, PersonaDefinition):
        return persona
    try:
        return PERSONAS_BY_ID[persona]
    except KeyError as exc:
        raise ValueError(f"unknown persona id: {persona!r}") from exc


def _registered_ids(registered_tile_ids: Iterable[str] | None) -> tuple[str, ...]:
    ids = tuple(tiles.TILES_BY_ID) if registered_tile_ids is None else tuple(registered_tile_ids)
    if any(not isinstance(tile_id, str) or not tile_id for tile_id in ids):
        raise ValueError("registered tile ids must be non-empty strings")
    if len(ids) != len(set(ids)):
        raise ValueError("registered tile ids must be unique")
    return ids


def _layouts(session: MutableMapping[str, object]) -> dict[str, object]:
    raw = session.get(SESSION_LAYOUTS_KEY, {})
    return dict(raw) if isinstance(raw, dict) else {}


def _saved_ids(session: MutableMapping[str, object], persona: PersonaDefinition
               ) -> tuple[tuple[str, ...], bool]:
    layouts = _layouts(session)
    if persona.id not in layouts:
        return persona.default_tile_ids, False
    raw = layouts[persona.id]
    if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) for item in raw):
        return persona.default_tile_ids, False
    # Old or hand-edited session state can contain duplicates.  Recover its
    # first-seen ordering without allowing duplicated widgets downstream.
    return tuple(dict.fromkeys(raw)), True


def _write_ids(session: MutableMapping[str, object], persona: PersonaDefinition,
               tile_ids: Sequence[str]) -> None:
    layouts = _layouts(session)
    layouts[persona.id] = list(tile_ids)
    # Reassign the namespace so Streamlit observes the update and so no nested
    # dictionary is ever shared across user sessions by this module.
    session[SESSION_LAYOUTS_KEY] = layouts


def layout_for(session: MutableMapping[str, object], persona: PersonaDefinition | str, *,
               registered_tile_ids: Iterable[str] | None = None) -> LayoutResolution:
    """Resolve one session layout without discarding retired tile ids.

    Retired ids remain saved and are returned separately.  If a definition with
    the same id is registered again, the tile automatically rejoins the active
    layout on the next call.
    """

    definition = persona_definition(persona)
    saved, customized = _saved_ids(session, definition)
    registered = set(_registered_ids(registered_tile_ids))
    active = tuple(tile_id for tile_id in saved if tile_id in registered)
    retired = tuple(tile_id for tile_id in saved if tile_id not in registered)
    return LayoutResolution(active, retired, customized)


def add_tile(session: MutableMapping[str, object], persona: PersonaDefinition | str,
             tile_id: str, *,
             registered_tile_ids: Iterable[str] | None = None) -> LayoutResolution:
    """Append one registered tile to a persona's session layout."""

    definition = persona_definition(persona)
    registered = _registered_ids(registered_tile_ids)
    if tile_id not in set(registered):
        raise ValueError(f"cannot add unregistered tile: {tile_id!r}")
    saved, _ = _saved_ids(session, definition)
    if tile_id not in saved:
        saved = (*saved, tile_id)
    _write_ids(session, definition, saved)
    return layout_for(session, definition, registered_tile_ids=registered)


def remove_tile(session: MutableMapping[str, object], persona: PersonaDefinition | str,
                tile_id: str, *,
                registered_tile_ids: Iterable[str] | None = None) -> LayoutResolution:
    """Remove an active or retired tile from a persona's session layout."""

    definition = persona_definition(persona)
    registered = _registered_ids(registered_tile_ids)
    saved, _ = _saved_ids(session, definition)
    _write_ids(session, definition, tuple(item for item in saved if item != tile_id))
    return layout_for(session, definition, registered_tile_ids=registered)


def reorder_tiles(session: MutableMapping[str, object], persona: PersonaDefinition | str,
                  ordered_tile_ids: Sequence[str], *,
                  registered_tile_ids: Iterable[str] | None = None) -> LayoutResolution:
    """Persist a complete permutation of the currently active tiles.

    Retired ids are kept after the active layout so they can recover if their
    definitions return.  Add/remove operations remain explicit and auditable.
    """

    definition = persona_definition(persona)
    registered = _registered_ids(registered_tile_ids)
    current = layout_for(session, definition, registered_tile_ids=registered)
    ordered = tuple(ordered_tile_ids)
    if len(ordered) != len(set(ordered)):
        raise ValueError("reordered tile ids must be unique")
    if set(ordered) != set(current.tile_ids):
        raise ValueError("reordered tile ids must be a permutation of the active layout")
    _write_ids(session, definition, (*ordered, *current.retired_tile_ids))
    return layout_for(session, definition, registered_tile_ids=registered)


def reset_layout(session: MutableMapping[str, object], persona: PersonaDefinition | str, *,
                 registered_tile_ids: Iterable[str] | None = None) -> LayoutResolution:
    """Remove one persona's customization and restore its current defaults."""

    definition = persona_definition(persona)
    registered = _registered_ids(registered_tile_ids)
    layouts = _layouts(session)
    layouts.pop(definition.id, None)
    if layouts:
        session[SESSION_LAYOUTS_KEY] = layouts
    else:
        session.pop(SESSION_LAYOUTS_KEY, None)
    return layout_for(session, definition, registered_tile_ids=registered)
