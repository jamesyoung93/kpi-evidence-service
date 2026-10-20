"""Glanceable governed KPIs composed with the question workspace."""
from __future__ import annotations

import copy
import html
import json
import logging
from dataclasses import asdict, replace

import altair as alt
import pandas as pd
import streamlit as st

from harness import baskets, digest as digest_service, voice
from harness import profiles, saved_insights, services, tile_runtime, tiles, triage
from harness import semantic_layer as sl
from harness.digest_store import InMemoryDigestHistoryStore
from harness.provenance import TIER_ABSTAINED
from views import ask, common, digest as digest_view, tile_detail

try:  # The tested buttons remain available if the optional component cannot load.
    from streamlit_sortables import sort_items
except ImportError:  # pragma: no cover - exercised in minimal local test installs
    sort_items = None


_CONTROL_KEYS = ("home_window", "home_basis", "home_scope", "home_source", "home_variant")
_PERSONA_KEY = "home_persona"
_PERSONA_LAST_KEY = "_home_persona_last"
_PERSONA_APPLIED_KEY = "_home_persona_applied"
_SESSION_DEFAULTS_KEY = "_home_persona_session_defaults"
_DEFAULT_PERSONA = "executive"


@st.cache_data(show_spinner=False)
def _cached_tile(cache_identity: tiles.TileCacheKey):
    """Execute the exact immutable spec represented by the cache identity."""

    return tile_runtime.evaluate_spec(
        cache_identity.spec, scope=dict(cache_identity.effective_scope)).artifact


@st.cache_data(show_spinner=False)
def _cached_home_digest(data_version: str, governance_fingerprint: str,
                        persona_label: str, scope_json: str, watches_json: str):
    """Cache the fixed walk-in strip across reruns for identical governed inputs."""

    del data_version, governance_fingerprint  # hash-only cache identity inputs
    return digest_service.build_digest(
        persona=persona_label,
        scope=json.loads(scope_json),
        watches=json.loads(watches_json),
        store=InMemoryDigestHistoryStore(),
        limit=3,
        record=False,
    )


def _format_value(art) -> str:
    if art.resolution is None:
        return common.format_artifact_value(art, art.value)
    return voice.format_value(
        art.resolution.metric, art.value, art.resolution.variant)


def _format_delta(art) -> str | None:
    comparison = art.extras.get("comparison", {})
    delta = common.format_comparison_delta(art, comparison)
    if delta is None and comparison.get("available") and art.resolution is not None:
        # Count metrics frequently have a zero reference, so a relative percent
        # is undefined even though the native movement is perfectly meaningful.
        delta = common.format_native_delta(
            art.resolution.metric, art.resolution.variant, comparison.get("delta"))
        if delta == "—":
            return None
    elif delta is None:
        return None
    label = comparison.get("basis_label", "comparison")
    return f"{delta} {label}"


def _sparkline_domain(chart_df: pd.DataFrame, *, padding_ratio: float = 0.08) \
        -> list[float] | None:
    """Backward-compatible tile wrapper around the shared chart-domain rule."""

    return common.visible_y_domain(chart_df, padding_ratio=padding_ratio)


def _sparkline_chart(chart_df: pd.DataFrame | None) -> alt.Chart | None:
    """Build the compact chart separately so its visual contract is testable."""

    if chart_df is None or chart_df.empty:
        return None
    series = [column for column in chart_df.columns if column != "month"]
    long = chart_df.melt("month", var_name="series", value_name="value").dropna()
    if long.empty:
        return None
    palette = [common.C_PRIMARY, common.C_REFERENCE][:len(series)]
    dash = [[1, 0], [6, 4]][:len(series)]
    domain = _sparkline_domain(chart_df)
    return alt.Chart(long).mark_line(point=False, strokeWidth=2).encode(
        x=alt.X("month:O", axis=None),
        y=alt.Y("value:Q", axis=None,
                scale=alt.Scale(domain=domain, zero=False, nice=False)),
        color=alt.Color("series:N", legend=None,
                        scale=alt.Scale(domain=series, range=palette)),
        strokeDash=alt.StrokeDash("series:N", legend=None,
                                  scale=alt.Scale(domain=series, range=dash)),
        tooltip=[alt.Tooltip("month:O"), alt.Tooltip("series:N"),
                 alt.Tooltip("value:Q", format=",.1f")],
    ).properties(height=72, description="KPI trend; reference series uses a dashed line.")


def _sparkline(chart_df: pd.DataFrame | None) -> None:
    chart = _sparkline_chart(chart_df)
    if chart is None:
        st.caption("No trend is available for this scope.")
        return
    st.altair_chart(chart, width="stretch")


def _compact_stamp(art, persona: profiles.PersonaDefinition | str | None = None) -> None:
    resolution = art.resolution
    source = sl.SOURCES[resolution.source]["name"] if resolution else "No source"
    st.markdown(common.chip(art.tier), unsafe_allow_html=True)
    with st.expander("Evidence"):
        st.caption(f"{source} · result `{art.result_hash}` · data `{art.data_version}`")
        if resolution:
            st.caption(voice.humanize_sentence(resolution.reason, persona))


def _tile_badges(art, *, material_forks: int = 0, override_notes: int = 0) -> None:
    badges = [common.chip(art.tier)]
    if material_forks:
        badges.append(
            '<span class="question-chip" style="border:1px solid #B07C0E;'
            'border-radius:12px;padding:2px 9px;font-size:.78rem;color:#765100">'
            '<span title="Material definition fork: registered definitions produce '
            'different answers">⚠ Two answers exist</span></span>')
    if override_notes:
        badges.append(
            '<span class="question-chip" style="border:1px solid #6B7280;'
            'border-radius:12px;padding:2px 9px;font-size:.78rem;color:#52514e">'
            f'{override_notes} override note{"s" if override_notes != 1 else ""}</span>')
    st.markdown(" ".join(badges), unsafe_allow_html=True)


def _tile_label(definition: tiles.TileDefinition, scope,
                persona: profiles.PersonaDefinition | str | None = None) -> str:
    external = dict(tiles.require_valid_scope(scope))
    filters = tiles.effective_filters(definition, scope=scope)
    if external and all(filters.get(key) == value for key, value in external.items()):
        return f"{definition.label} · {voice.scope_text(external, persona)}"
    return definition.label


def _queue_tile_question(question: str, source: str | None, variant: str | None,
                         basis: str | None = None) -> None:
    helper = getattr(common, "queue_question_with_resolution", None)
    if helper is not None:
        helper(question, source, variant, basis=basis)
        return
    st.session_state["ask_src"] = source or "governed default"
    st.session_state["_ask_src_last"] = st.session_state["ask_src"]
    st.session_state["ask_var"] = variant or "governed default"
    st.session_state["_ask_var_last"] = st.session_state["ask_var"]
    common.queue_question(question)


def _watch_tile(spec: tiles.SavedQuestionSpec, scope, definition: tiles.TileDefinition,
                persona: profiles.PersonaDefinition | str | None = None) -> None:
    insight = saved_insights.save_spec(
        spec, label=_tile_label(definition, scope, persona), catalog_tile_id=definition.id,
        watched=True, scope=scope)
    result = saved_insights.session_store(st.session_state).add(insight)
    st.toast("Watching — see Monitoring." if result.added else "Already on the watchlist.")


def _render_tile_body(definition: tiles.TileDefinition, spec: tiles.SavedQuestionSpec,
                      scope, art, persona: profiles.PersonaDefinition) -> None:
    count = len(art.table) if art.table is not None else None
    cohort = None
    if spec.question_class == triage.COHORT:
        cohort = voice.cohort_presentation(art, persona)
        presentation = cohort
    else:
        presentation = voice.tile_presentation(
            art,
            persona=persona,
            metric=definition.metric,
            scope=tiles.effective_spec_filters(spec, scope),
            headline=art.headline,
            value=art.value,
            is_zero=count == 0 if count is not None else False,
            label=_tile_label(definition, scope, persona),
            template=spec.retrieval_template,
        )
    label = presentation.label or _tile_label(definition, scope, persona)
    basket_resolution = art.extras.get("basket_resolution", {})
    basket_id = basket_resolution.get("basket_id") if isinstance(
        basket_resolution, dict) else None
    if basket_id in baskets.BASKETS:
        label += f" · {baskets.BASKETS[basket_id].label}"
    if spec.viz_kind == "count":
        count = count or 0
        st.metric(label, f"{count:,}")
        st.caption(presentation.headline)
        if art.table is not None and not art.table.empty:
            st.dataframe(voice.humanize_table(art.table.head(5), persona),
                         width="stretch", hide_index=True,
                         height=min(220, 38 + 35 * min(len(art.table), 5)))
    elif spec.viz_kind == "table":
        st.markdown(f"**{label}**")
        if cohort is not None:
            st.caption(cohort.hero)
            st.caption(cohort.headline)
            st.markdown(
                '<span class="question-chip" style="border:1px solid #d9e0e8;'
                'border-radius:12px;padding:2px 9px;font-size:.74rem;color:#52514e">'
                f'{html.escape(cohort.method_chip)}</span>',
                unsafe_allow_html=True,
            )
        else:
            st.caption(presentation.headline)
        if art.table is not None:
            display = (
                voice.cohort_display_table(
                    art.table.head(8), persona, compact=True)
                if cohort is not None
                else voice.humanize_table(art.table.head(8), persona)
            )
            st.dataframe(display,
                         width="stretch", hide_index=True,
                         height=min(300, 38 + 35 * min(len(art.table), 8)))
    else:
        st.metric(label, _format_value(art), _format_delta(art))
        st.caption(presentation.headline)
        if art.resolution and art.resolution.metric == "new_writers":
            current_month = art.extras.get("comparison", {}).get("current_month")
            period = f" · latest {current_month}" if current_month else ""
            st.caption(f"{spec.window} monthly trend{period}")
        if spec.viz_kind in ("sparkline", "line"):
            _sparkline(art.chart_df)


def _tile_actions(definition: tiles.TileDefinition, spec: tiles.SavedQuestionSpec,
                  scope, art, persona: profiles.PersonaDefinition, *, material_forks=(), disclosures=(),
                  definition_notes=()) -> None:
    filters = tiles.effective_spec_filters(spec, scope)
    window_n = tiles.WINDOW_CONTROLS[spec.window] \
        if spec.question_class not in (triage.RETRIEVAL, triage.COHORT) else None
    basis_code = tiles.BASIS_CONTROLS[spec.basis] \
        if spec.question_class not in (triage.RETRIEVAL, triage.COHORT) else None
    breakdown = services.breakdown_question(spec.metric, filters, window_n, basis_code)
    question = tiles.canonical_question_for_spec(spec, scope)
    expand = False
    with st.popover("Actions", width="stretch"):
        resolution = art.resolution
        source = sl.SOURCES[resolution.source]["name"] if resolution else "No source"
        st.caption(f"{art.tier} · {source} · data `{art.data_version}` · "
                   f"result `{art.result_hash}`")
        if resolution:
            st.caption(voice.humanize_sentence(resolution.reason, persona))
        for note in definition_notes:
            st.caption(f"Definition: {note}")
        for disclosure in disclosures:
            st.caption(f"Override note: {disclosure}")
        if material_forks:
            st.markdown("**Why two answers exist**")
            for fork in material_forks:
                comparison = voice.definition_fork_presentation(art, fork, persona)
                st.markdown(f"- {comparison.detail}")
        if resolution or disclosures or material_forks:
            st.divider()
        expand = st.button(
            f"Expand {definition.label}", key=f"tile_{definition.id}_expand",
            width="stretch", help="Open a large governed chart with local drill controls.")
        if spec.question_class != triage.COHORT:
            st.button(f"Watch {definition.label}", key=f"tile_{definition.id}_watch",
                      on_click=_watch_tile, args=(spec, scope, definition, persona),
                      width="stretch")
        st.download_button(
            f"Download {definition.label} JSON", data=art.to_json(),
            file_name=f"tile_{definition.id}_{art.result_hash}.json",
            mime="application/json", key=f"tile_{definition.id}_download", width="stretch")
        st.button(f"Open {definition.label}", key=f"tile_{definition.id}_open",
                  on_click=_queue_tile_question,
                  args=(question, spec.source, spec.variant, basis_code), width="stretch")
        if spec.question_class != triage.COHORT:
            st.button(f"Break down {definition.label}", key=f"tile_{definition.id}_breakdown",
                      on_click=_queue_tile_question,
                      args=(breakdown, spec.source, spec.variant, basis_code), width="stretch")
    if expand:
        tile_detail.show_tile_dialog(definition.id, spec, scope)


def _adaptive_basket(materialized: tiles.MaterializedSpec, scope,
                     requested_variant: str | None):
    """Resolve share tiles to one named basket before cache identity is built."""

    if materialized.spec.metric != "trx_share":
        return materialized, None
    filters = tiles.effective_spec_filters(materialized.spec, scope)
    stage = baskets.adoption_stage_for_scope(filters)
    override_by_variant = {
        "il17_class": "il17_class",
        "advanced_therapy": "advanced_therapy",
        "brand_market": "advanced_therapy",
    }
    override = override_by_variant.get(requested_variant or "")
    resolution = baskets.resolve_basket(stage, override)
    spec = tiles.require_valid_spec(replace(
        materialized.spec, variant=resolution.semantic_variant))
    return replace(materialized, spec=spec), resolution


def _render_tile(definition: tiles.TileDefinition, window: str, basis: str,
                 scope, source: str | None, variant: str | None,
                 persona: profiles.PersonaDefinition) -> None:
    try:
        materialized = tiles.materialize_spec(
            definition, window=window, basis=basis, source=source, variant=variant)
        materialized, basket_resolution = _adaptive_basket(
            materialized, scope, variant)
        scope_disclosures = tiles.fixed_scope_disclosures(materialized.spec, scope)
        identity = tiles.cache_key_for_spec(materialized.spec, scope=scope)
        art = _cached_tile(identity)
    except Exception:
        logging.getLogger(__name__).exception("tile evaluation failed: %s", definition.id)
        st.error(f"{definition.label} could not be evaluated; no unverified value is shown.")
        return

    definition_notes = ()
    if basket_resolution is not None:
        art = copy.deepcopy(art)
        art.extras["basket_resolution"] = asdict(basket_resolution)
        art.extras["basket_registry_fingerprint"] = baskets.registry_fingerprint()
        chart_months = (
            art.chart_df["month"].dropna().astype(str).drop_duplicates().tolist()
            if art.chart_df is not None and "month" in art.chart_df else None
        )
        art.extras["basket_reconciliation"] = baskets.reconciliation_for_scope(
            basket_resolution.basket_id,
            tiles.effective_spec_filters(materialized.spec, scope),
            chart_months,
            art.resolution.source if art.resolution is not None else "source_a",
        )
        if art.resolution is not None:
            art.resolution.reason = (
                f"{basket_resolution.reason}; {art.resolution.reason}")
        if basket_resolution.disclosure not in art.caveats:
            art.caveats.append(basket_resolution.disclosure)
        definition_notes = (basket_resolution.disclosure,)

    if art.tier == TIER_ABSTAINED:
        refusal = voice.refusal_presentation(art, persona)
        st.error(refusal.lead)
        st.caption(refusal.detail)
        _compact_stamp(art, persona)
        return

    disclosures = (*materialized.disclosures, *scope_disclosures)
    material = [fork for fork in art.divergence if fork.get("material")]
    _render_tile_body(definition, materialized.spec, scope, art, persona)
    _tile_badges(art, material_forks=len(material), override_notes=len(disclosures))
    _tile_actions(definition, materialized.spec, scope, art, persona,
                  material_forks=material, disclosures=disclosures,
                  definition_notes=definition_notes)


def _restore_controls() -> None:
    if "home_scope" not in st.session_state and "home_region" in st.session_state:
        try:
            st.session_state["home_scope"] = tiles.scope_key(st.session_state["home_region"])
        except ValueError:
            pass
    for key in _CONTROL_KEYS:
        previous = f"_{key}_last"
        if key not in st.session_state and previous in st.session_state:
            st.session_state[key] = st.session_state[previous]


def _remember_controls() -> None:
    for key in _CONTROL_KEYS:
        if key in st.session_state:
            st.session_state[f"_{key}_last"] = st.session_state[key]


def _set_controls(window: str, basis: str, scope) -> None:
    values = {"home_window": window, "home_basis": basis,
              "home_scope": tiles.scope_key(scope)}
    for key, value in values.items():
        st.session_state[key] = value
        st.session_state[f"_{key}_last"] = value


def _session_defaults() -> dict[str, dict]:
    raw = st.session_state.get(_SESSION_DEFAULTS_KEY, {})
    return dict(raw) if isinstance(raw, dict) else {}


def _valid_snapshot(persona: profiles.PersonaDefinition) -> dict | None:
    snapshot = _session_defaults().get(persona.id)
    if not isinstance(snapshot, dict):
        return None
    tile_ids = snapshot.get("tile_ids")
    if not isinstance(tile_ids, (list, tuple)) or any(
            tile_id not in tiles.TILES_BY_ID for tile_id in tile_ids):
        return None
    if snapshot.get("window") not in tiles.WINDOW_CONTROLS:
        return None
    if snapshot.get("basis") not in tiles.BASIS_CONTROLS:
        return None
    raw_scope = snapshot.get("scope", snapshot.get("region", tiles.ALL_REGIONS))
    if tiles.scope_errors(raw_scope):
        return None
    snapshot = dict(snapshot)
    snapshot["scope"] = dict(tiles.require_valid_scope(raw_scope))
    return snapshot


def _replace_layout(persona: profiles.PersonaDefinition, tile_ids: tuple[str, ...]) -> None:
    profiles.reset_layout(st.session_state, persona)
    current = profiles.layout_for(st.session_state, persona)
    for tile_id in tuple(current.tile_ids):
        if tile_id not in tile_ids:
            profiles.remove_tile(st.session_state, persona, tile_id)
    current = profiles.layout_for(st.session_state, persona)
    for tile_id in tile_ids:
        if tile_id not in current.tile_ids:
            profiles.add_tile(st.session_state, persona, tile_id)
    profiles.reorder_tiles(st.session_state, persona, tile_ids)


def _apply_persona(persona: profiles.PersonaDefinition) -> None:
    snapshot = _valid_snapshot(persona)
    if snapshot is None:
        _set_controls(persona.default_window, persona.default_basis, persona.default_scope)
        return
    _set_controls(snapshot["window"], snapshot["basis"], snapshot["scope"])
    _replace_layout(persona, tuple(snapshot["tile_ids"]))


def _persona_selector() -> profiles.PersonaDefinition:
    if _PERSONA_KEY not in st.session_state:
        remembered = st.session_state.get(_PERSONA_LAST_KEY, _DEFAULT_PERSONA)
        st.session_state[_PERSONA_KEY] = (
            remembered if remembered in profiles.PERSONAS_BY_ID else _DEFAULT_PERSONA)
    persona_id = st.selectbox(
        "Persona view",
        tuple(profiles.PERSONAS_BY_ID),
        key=_PERSONA_KEY,
        format_func=lambda value: profiles.PERSONAS_BY_ID[value].label,
        help="Each preset supplies a starting tile set, scope, window, and comparison.",
    )
    st.session_state[_PERSONA_LAST_KEY] = persona_id
    persona = profiles.persona_definition(persona_id)
    if st.session_state.get(_PERSONA_APPLIED_KEY) != persona_id:
        _apply_persona(persona)
        st.session_state[_PERSONA_APPLIED_KEY] = persona_id
    st.caption(f"Preset: {persona.default_window} · {persona.default_basis} · "
               f"{voice.scope_text(dict(persona.default_scope), persona)}")
    return persona


def _move_tile(persona_id: str, tile_id: str, offset: int) -> None:
    state = profiles.layout_for(st.session_state, persona_id)
    ordered = list(state.tile_ids)
    index = ordered.index(tile_id)
    target = index + offset
    if 0 <= target < len(ordered):
        ordered[index], ordered[target] = ordered[target], ordered[index]
        profiles.reorder_tiles(st.session_state, persona_id, ordered)


def _save_session_default(persona_id: str) -> None:
    state = profiles.layout_for(st.session_state, persona_id)
    defaults = _session_defaults()
    defaults[persona_id] = {
        "tile_ids": list(state.tile_ids),
        "window": st.session_state.get("home_window"),
        "basis": st.session_state.get("home_basis"),
        "scope": dict(tiles.scope_from_key(st.session_state.get("home_scope", tiles.ALL_SCOPES))),
    }
    st.session_state[_SESSION_DEFAULTS_KEY] = defaults
    st.toast("Saved for this session.")


def _reset_persona(persona_id: str) -> None:
    persona = profiles.persona_definition(persona_id)
    profiles.reset_layout(st.session_state, persona)
    defaults = _session_defaults()
    defaults.pop(persona_id, None)
    if defaults:
        st.session_state[_SESSION_DEFAULTS_KEY] = defaults
    else:
        st.session_state.pop(_SESSION_DEFAULTS_KEY, None)
    _set_controls(persona.default_window, persona.default_basis, persona.default_scope)


def _customize_tiles(persona: profiles.PersonaDefinition) -> profiles.LayoutResolution:
    state = profiles.layout_for(st.session_state, persona)
    with st.popover("Customize tiles", width="stretch"):
        st.caption("Changes stay in this app session and never alter another viewer's layout.")
        if sort_items is not None and len(state.tile_ids) > 1:
            label_to_id = {tiles.TILES_BY_ID[tile_id].label: tile_id
                           for tile_id in state.tile_ids}
            sorted_labels = sort_items(
                list(label_to_id), direction="vertical",
                key=f"home_{persona.id}_drag_order",
                custom_style="""
                    .sortable-component { padding: 0; margin: 0 0 .5rem 0; }
                    .sortable-item { border: 1px solid #d9e0e8; border-radius: 7px;
                        background: #f7f9fc; color: #172033; padding: 7px 10px;
                        margin: 4px 0; cursor: grab; }
                """,
            )
            if (len(sorted_labels) == len(label_to_id)
                    and set(sorted_labels) == set(label_to_id)):
                sorted_ids = tuple(label_to_id[label] for label in sorted_labels)
                if sorted_ids != state.tile_ids:
                    profiles.reorder_tiles(st.session_state, persona, sorted_ids)
                    state = profiles.layout_for(st.session_state, persona)
            st.caption("Drag tiles above to reorder. Buttons below are the keyboard fallback.")
        for index, tile_id in enumerate(state.tile_ids):
            definition = tiles.TILES_BY_ID[tile_id]
            label, up, down, remove = st.columns([3.4, 1, 1, 1.2])
            label.markdown(f"**{definition.label}**")
            up.button(
                "Move up", key=f"home_{persona.id}_{tile_id}_up",
                disabled=index == 0, on_click=_move_tile,
                args=(persona.id, tile_id, -1), width="stretch")
            down.button(
                "Move down", key=f"home_{persona.id}_{tile_id}_down",
                disabled=index == len(state.tile_ids) - 1, on_click=_move_tile,
                args=(persona.id, tile_id, 1), width="stretch")
            remove.button(
                "Remove", key=f"home_{persona.id}_{tile_id}_remove",
                on_click=profiles.remove_tile,
                args=(st.session_state, persona.id, tile_id), width="stretch")

        if state.retired_tile_ids:
            st.warning("Some saved tiles are no longer registered. They stay recoverable until "
                       "you remove or reset them.")
            for tile_id in state.retired_tile_ids:
                st.button(
                    f"Remove retired tile: {voice.column_name(tile_id)}",
                    key=f"home_{persona.id}_{tile_id}_retired_remove",
                    on_click=profiles.remove_tile,
                    args=(st.session_state, persona.id, tile_id))

        missing = tuple(tile_id for tile_id in tiles.TILES_BY_ID if tile_id not in state.tile_ids)
        if missing:
            add_choice = st.selectbox(
                "Tile to add", missing, key=f"home_{persona.id}_add_choice",
                format_func=lambda tile_id: tiles.TILES_BY_ID[tile_id].label)
            st.button(
                "Add tile", key=f"home_{persona.id}_add",
                on_click=profiles.add_tile,
                args=(st.session_state, persona.id, add_choice))
        else:
            st.caption("All registered tiles are already shown.")

        reset, save = st.columns(2)
        reset.button(
            "Reset persona defaults", key=f"home_{persona.id}_reset",
            on_click=_reset_persona, args=(persona.id,), width="stretch")
        save.button(
            "Save as current-session default", key=f"home_{persona.id}_save",
            on_click=_save_session_default, args=(persona.id,), width="stretch")
    return state


def _scope_option_label(option: tiles.ScopeOption,
                        persona: profiles.PersonaDefinition) -> str:
    filters = dict(option.filters)
    if not filters:
        return voice.scope_text(filters, persona)
    dimension = next(iter(filters))
    return f"{voice.column_name(dimension)} · {voice.scope_text(filters, persona)}"


def _control_band(definitions: tuple[tiles.TileDefinition, ...],
                  persona: profiles.PersonaDefinition):
    _restore_controls()
    c1, c2, c3, c4 = st.columns([1.7, 1.7, 2.4, 1.25], gap="small")
    window = c1.radio("Window", tuple(tiles.WINDOW_CONTROLS), horizontal=True,
                      key="home_window")
    basis = c2.radio("Compare", tuple(tiles.BASIS_CONTROLS), horizontal=True,
                     key="home_basis")
    options = tiles.scope_options()
    option_by_key = {option.key: option for option in options}
    if st.session_state.get("home_scope") not in option_by_key:
        st.session_state["home_scope"] = tiles.ALL_SCOPES
    scope_key = c3.selectbox(
        "Scope", tuple(option_by_key), key="home_scope",
        format_func=lambda key: _scope_option_label(option_by_key[key], persona),
        help="Scope by geography, specialty, or payer channel using registered values.")
    metric_ids = {definition.metric for definition in definitions}
    sources = tuple(source for source in sl.SOURCES
                    if any(source in sl.METRICS[metric]["sources"] for metric in metric_ids))
    variants = tuple(sorted({variant for metric in metric_ids
                             for variant in sl.METRICS[metric]["variants"]}))
    override_active = any(st.session_state.get(key) not in (None, "governed default")
                          for key in ("home_source", "home_variant"))
    with c4:
        with st.popover("Data options" + (" · override" if override_active else ""),
                        width="stretch"):
            source_pick = st.selectbox(
                "Prescription source", ("governed default", *sources), key="home_source",
                format_func=lambda value: "Recommended source" if value == "governed default"
                else sl.SOURCES[value]["name"])
            variant_pick = st.selectbox(
                "Sales type", ("governed default", *variants), key="home_variant",
                format_func=lambda value: "Recommended definition"
                if value == "governed default" else next(
                    (voice.variant_name(metric, value) for metric in sorted(metric_ids)
                     if value in sl.METRICS[metric]["variants"]), value))
            st.caption("Changes apply where available; fixed definitions stay in place and "
                       "are explained.")
    _remember_controls()
    return (window, basis, option_by_key[scope_key].filters,
            None if source_pick == "governed default" else source_pick,
            None if variant_pick == "governed default" else variant_pick)


def render_kpi_band(persona: profiles.PersonaDefinition) -> None:
    heading, customize = st.columns([5, 1.25], gap="small")
    heading.subheader("Business pulse")
    with customize:
        layout = _customize_tiles(persona)
    st.caption("Monthly performance at a glance. Tiles follow the controls above unless a "
               "fixed definition applies; any difference is explained in Actions.")
    definitions = tuple(tiles.TILES_BY_ID[tile_id] for tile_id in layout.tile_ids)
    if not definitions:
        st.info("No tiles are selected for this persona. Add one under Customize tiles.")
        return
    window, basis, scope, source, variant = _control_band(definitions, persona)
    for start in range(0, len(definitions), 3):
        row = definitions[start:start + 3]
        # Always reserve three equal slots so a partial final row never stretches
        # into visually different card widths.
        columns = st.columns(3, gap="medium")
        for column, definition in zip(columns, row):
            with column:
                with st.container(border=True):
                    _render_tile(definition, window, basis, scope, source, variant, persona)


def _render_digest_strip(persona: profiles.PersonaDefinition) -> None:
    """Show the three executive stories immediately on entry, then link through."""

    try:
        scope = dict(tiles.scope_from_key(
            st.session_state.get("home_scope", tiles.ALL_SCOPES)))
    except ValueError:
        scope = dict(persona.digest_scope)
    insights = saved_insights.session_store(st.session_state).all()
    watches, _ = digest_view._descriptive_watch_inputs(insights)
    try:
        artifact = _cached_home_digest(
            sl.data_version(), digest_service.governance_fingerprint(), persona.label,
            json.dumps(scope, sort_keys=True),
            json.dumps(watches, sort_keys=True, default=list),
        )
    except Exception:
        logging.getLogger(__name__).exception("home digest strip failed")
        st.caption("The attention summary is temporarily unavailable; open Digest to retry.")
        return

    heading, open_digest = st.columns([5, 1.2], gap="small")
    heading.subheader("What deserves attention")
    open_digest.button(
        "Open Digest", key="home_open_digest", on_click=common.goto,
        args=("Digest",), width="stretch")
    st.caption(
        f"Top {len(artifact.items)} scope-diverse stories from "
        f"{artifact.scanned_series} monthly series."
    )
    if not artifact.items:
        st.info("No signal has enough history to rank yet.")
        return

    expanded = None
    columns = st.columns(3, gap="medium")
    for index, (column, item) in enumerate(zip(columns, artifact.items), start=1):
        with column:
            presentation = digest_view.presentation_for(item, persona)
            # The full headline remains in the DOM and the browser clamps it to
            # two lines; opening the story always shows the complete wording.
            st.markdown(
                common.chip(item.candidate.artifact.tier)
                + f"&nbsp; {common.chip(presentation.chip, tooltip=voice.chip_tooltip(presentation.chip))}",
                unsafe_allow_html=True,
            )
            headline = presentation.headline
            st.markdown(
                '<div style="font-size:.86rem;font-weight:650;line-height:1.3;'
                'height:2.25rem;overflow:hidden;display:-webkit-box;'
                '-webkit-box-orient:vertical;-webkit-line-clamp:2;'
                'margin:0 0 .05rem 0">'
                f'{html.escape(headline)}</div>',
                unsafe_allow_html=True,
            )
            if st.button(
                    "Open story", key=f"home_digest_expand_{index}_{item.result_hash}",
                    width="stretch", help="Open the complete story and its evidence."):
                expanded = item
            sparkline = digest_view._sparkline_chart(item)
            if sparkline is not None:
                st.altair_chart(sparkline.properties(height=34), width="stretch")
    if expanded is not None:
        digest_view.show_digest_dialog(expanded, persona)


def render() -> None:
    intro, persona_control = st.columns([3.5, 1.5], gap="medium")
    with intro:
        st.title("Home")
        st.caption("Start with the state of the business, then explore any number in context.")
    with persona_control:
        persona = _persona_selector()
    _render_digest_strip(persona)
    render_kpi_band(persona)
    st.divider()
    st.subheader("Explore")
    st.caption("Ask about any available metric in plain language. If a question cannot be "
               "answered reliably, the app explains why and suggests a useful reframe.")
    ask.render_workspace()
