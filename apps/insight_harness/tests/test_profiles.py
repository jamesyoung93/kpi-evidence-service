"""Persona preset validation and session-safe layout customization."""
from dataclasses import FrozenInstanceError

import pytest

from harness import profiles, tiles


def test_all_five_personas_are_immutable_and_fully_validated():
    assert [persona.label for persona in profiles.PERSONAS] == [
        "Sales Rep",
        "District Manager",
        "Brand Marketing",
        "Market Access",
        "Executive",
    ]
    assert len(profiles.PERSONAS_BY_ID) == 5
    for persona in profiles.PERSONAS:
        assert persona.default_tile_ids
        assert set(persona.default_tile_ids) <= set(tiles.TILES_BY_ID)
        assert tiles.scope_errors(persona.default_scope) == ()
        assert tiles.scope_errors(persona.digest_scope) == ()
        assert persona.default_window in tiles.WINDOW_CONTROLS
        assert persona.default_basis in tiles.BASIS_CONTROLS
        assert hash(persona)
    with pytest.raises(FrozenInstanceError):
        profiles.PERSONAS[0].label = "Changed"


def test_persona_scopes_are_immutable_dimension_value_mappings():
    rep = profiles.PERSONAS_BY_ID["sales_rep"]
    manager = profiles.PERSONAS_BY_ID["district_manager"]
    assert set(rep.default_scope) == {"territory"}
    assert set(manager.default_scope) == {"district"}
    assert rep.default_scope["territory"] in tiles.dimension_values("territory")
    assert manager.default_scope["district"] in tiles.dimension_values("district")
    for persona_id in ("brand_marketing", "market_access", "executive"):
        assert dict(profiles.PERSONAS_BY_ID[persona_id].default_scope) == {}
    with pytest.raises(TypeError):
        rep.default_scope["territory"] = "changed"


@pytest.mark.parametrize("persona", profiles.PERSONAS, ids=lambda value: value.id)
def test_default_layout_is_ordered_and_not_customized(persona):
    state = profiles.layout_for({}, persona)
    assert state.tile_ids == persona.default_tile_ids
    assert state.retired_tile_ids == ()
    assert state.customized is False


def test_add_remove_reorder_and_reset_are_isolated_to_the_given_session():
    first: dict[str, object] = {}
    second: dict[str, object] = {}

    initial = profiles.layout_for(first, "brand_marketing")
    added = profiles.add_tile(first, "brand_marketing", "calls")
    assert added.tile_ids == (*initial.tile_ids, "calls")
    assert added.customized is True
    assert profiles.layout_for(second, "brand_marketing") == initial

    removed = profiles.remove_tile(first, "brand_marketing", "new_writers")
    assert "new_writers" not in removed.tile_ids
    reordered_ids = tuple(reversed(removed.tile_ids))
    reordered = profiles.reorder_tiles(first, "brand_marketing", reordered_ids)
    assert reordered.tile_ids == reordered_ids

    reset = profiles.reset_layout(first, "brand_marketing")
    assert reset == initial
    assert profiles.SESSION_LAYOUTS_KEY not in first


def test_empty_custom_layout_does_not_fall_back_to_defaults():
    session: dict[str, object] = {}
    persona = profiles.persona_definition("market_access")
    for tile_id in persona.default_tile_ids:
        profiles.remove_tile(session, persona, tile_id)
    state = profiles.layout_for(session, persona)
    assert state.tile_ids == ()
    assert state.customized is True


def test_add_is_idempotent_and_remove_accepts_unknown_or_retired_ids():
    session: dict[str, object] = {}
    once = profiles.add_tile(session, "brand_marketing", "calls")
    twice = profiles.add_tile(session, "brand_marketing", "calls")
    assert twice == once
    assert twice.tile_ids.count("calls") == 1
    assert profiles.remove_tile(session, "brand_marketing", "missing") == twice


def test_reorder_requires_an_exact_unique_permutation():
    session: dict[str, object] = {}
    current = profiles.layout_for(session, "executive")
    with pytest.raises(ValueError, match="permutation"):
        profiles.reorder_tiles(session, "executive", current.tile_ids[:-1])
    with pytest.raises(ValueError, match="unique"):
        profiles.reorder_tiles(
            session, "executive", (*current.tile_ids[:-1], current.tile_ids[0]))


def test_retired_tiles_remain_recoverable_and_removable():
    session = {
        profiles.SESSION_LAYOUTS_KEY: {
            "sales_rep": ["calls", "retired_kpi", "trx"],
        }
    }
    registered = ("calls", "trx")
    retired = profiles.layout_for(
        session, "sales_rep", registered_tile_ids=registered)
    assert retired.tile_ids == registered
    assert retired.retired_tile_ids == ("retired_kpi",)
    assert retired.all_saved_tile_ids == ("calls", "trx", "retired_kpi")

    recovered = profiles.layout_for(
        session, "sales_rep", registered_tile_ids=(*registered, "retired_kpi"))
    assert recovered.tile_ids == ("calls", "retired_kpi", "trx")
    assert recovered.retired_tile_ids == ()

    removed = profiles.remove_tile(
        session, "sales_rep", "retired_kpi", registered_tile_ids=registered)
    assert removed.tile_ids == registered
    assert removed.retired_tile_ids == ()


def test_reorder_preserves_retired_ids_for_future_recovery():
    session = {
        profiles.SESSION_LAYOUTS_KEY: {
            "sales_rep": ["calls", "retired_kpi", "trx"],
        }
    }
    state = profiles.reorder_tiles(
        session, "sales_rep", ("trx", "calls"),
        registered_tile_ids=("calls", "trx"))
    assert state.tile_ids == ("trx", "calls")
    assert state.retired_tile_ids == ("retired_kpi",)
    recovered = profiles.layout_for(
        session, "sales_rep", registered_tile_ids=("calls", "trx", "retired_kpi"))
    assert recovered.tile_ids == ("trx", "calls", "retired_kpi")


def test_persona_layouts_do_not_overwrite_each_other():
    session: dict[str, object] = {}
    profiles.remove_tile(session, "sales_rep", "trx")
    profiles.add_tile(session, "brand_marketing", "calls")
    layouts = session[profiles.SESSION_LAYOUTS_KEY]
    assert set(layouts) == {"sales_rep", "brand_marketing"}
    assert "trx" not in profiles.layout_for(session, "sales_rep").tile_ids
    assert "calls" in profiles.layout_for(session, "brand_marketing").tile_ids
    assert profiles.layout_for(session, "executive").customized is False


def test_corrupt_or_duplicate_session_state_recovers_safely():
    corrupt = {profiles.SESSION_LAYOUTS_KEY: "not a mapping"}
    state = profiles.layout_for(corrupt, "executive")
    assert state.tile_ids == profiles.PERSONAS_BY_ID["executive"].default_tile_ids
    assert state.customized is False

    duplicated = {
        profiles.SESSION_LAYOUTS_KEY: {"executive": ["trx", "trx", "trx_share"]}
    }
    state = profiles.layout_for(duplicated, "executive")
    assert state.tile_ids == ("trx", "trx_share")
    assert state.customized is True


def test_invalid_add_and_unknown_persona_are_rejected():
    with pytest.raises(ValueError, match="unregistered tile"):
        profiles.add_tile({}, "executive", "retired_kpi")
    with pytest.raises(ValueError, match="unknown persona"):
        profiles.layout_for({}, "not-a-persona")
