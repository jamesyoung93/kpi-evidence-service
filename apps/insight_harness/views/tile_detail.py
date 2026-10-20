"""Large-format tile exploration using the same governed answer pipeline."""
from __future__ import annotations

import copy
from dataclasses import asdict, replace

import streamlit as st

from harness import baskets, drill, tile_runtime, tiles, triage, voice
from views import common


_SPLIT_OPTIONS = {
    "No split": None,
    "Region": "region",
    "District": "district",
    "Territory": "territory",
    "Specialty": "specialty",
    "Payer channel": "payer_channel",
}


def _dialog_scope_key(tile_id: str) -> str:
    return f"_tile_dialog_scope_{tile_id}"


def _set_scope(key: str, scope: dict) -> None:
    st.session_state[key] = dict(scope)


def _effective_spec(spec: tiles.SavedQuestionSpec, window: str, basis: str,
                    split_dimension: str | None) -> tiles.SavedQuestionSpec:
    if spec.question_class in (triage.RETRIEVAL, triage.COHORT):
        return spec
    if split_dimension:
        return tiles.require_valid_spec(replace(
            spec,
            window=window,
            basis=basis,
            question_class=triage.DIAGNOSTIC,
            breakdown_dimension=split_dimension,
            retrieval_template=None,
            viz_kind="line",
        ))
    return tiles.require_valid_spec(replace(spec, window=window, basis=basis))


def _render_geo_drill(tile_id: str, scope: dict, metric: str) -> dict:
    persona = common.active_persona()
    state_key = _dialog_scope_key(tile_id)
    if state_key not in st.session_state:
        st.session_state[state_key] = dict(scope)
    current = dict(st.session_state[state_key])

    st.markdown("**Geographic drill**")
    crumbs = drill.breadcrumbs(current)
    crumb_columns = st.columns(max(1, len(crumbs)))
    for index, ((_label, crumb_scope), column) in enumerate(zip(crumbs, crumb_columns)):
        label = voice.scope_text(crumb_scope, persona=persona)
        column.button(
            label,
            key=f"tile_dialog_{tile_id}_crumb_{index}",
            on_click=_set_scope,
            args=(state_key, crumb_scope),
            width="stretch",
            disabled=index == len(crumbs) - 1,
        )

    options = drill.child_options(current)
    if options:
        option_by_value = {option.value: option for option in options}
        dimension = options[0].dimension
        picked = st.selectbox(
            f"Drill to {voice.column_name(dimension)}",
            ("", *option_by_value),
            key=f"tile_dialog_{tile_id}_drill_{dimension}",
            format_func=lambda value: (
                f"Choose {voice.column_name(dimension).lower()}…" if not value
                else voice.scope_text({dimension: value}, persona=persona)),
        )
        if picked:
            selected = drill.select_child(current, dimension, picked)
            if selected != current:
                st.session_state[state_key] = selected
                current = selected
                # Dialogs are fragments: keep the modal open while redrawing
                # the next breadcrumb/child level instead of rerunning the app.
                st.rerun(scope="fragment")
    elif "territory" in current:
        floor = st.number_input(
            "Minimum trailing-twelve-month volume",
            min_value=0.0,
            value=1.0,
            step=1.0,
            key=f"tile_dialog_{tile_id}_hcp_floor",
            help="Applied before ranking; the floor is part of the disclosed retrieval recipe.",
        )
        account_metric = metric if metric in {"trx", "nrx", "nbrx"} else "trx"
        rows = drill.hcp_rows(current, account_metric, top_n=25, min_volume=float(floor))
        st.caption(
            f"Top {len(rows)} synthetic HCP records by trailing-twelve-month "
            f"{voice.metric_name(account_metric)} · minimum volume {floor:g}."
        )
        st.dataframe(
            voice.humanize_table(rows), hide_index=True, width="stretch")
    return current


@st.dialog("Explore governed tile", width="large")
def show_tile_dialog(tile_id: str, spec: tiles.SavedQuestionSpec, scope) -> None:
    """Render a modal-local re-scope/re-query path and the complete artifact."""

    definition = tiles.tile_definition(tile_id)
    initial_scope = dict(tiles.require_valid_scope(scope))
    st.subheader(definition.label)
    st.caption(
        "Window, comparison, split, and geographic drill are local to this dialog. "
        "Every change re-enters the governed answer pipeline."
    )

    controls = st.columns([1, 1, 1.25])
    if spec.question_class in (triage.RETRIEVAL, triage.COHORT):
        window, basis, split_label = spec.window, spec.basis, "No split"
        controls[0].caption("Window is governed by this recipe.")
        controls[1].caption("Comparison is governed by this recipe.")
        controls[2].caption("Split is governed by this recipe.")
    else:
        window = controls[0].segmented_control(
            "Window", tuple(tiles.WINDOW_CONTROLS), default=spec.window,
            key=f"tile_dialog_{tile_id}_window") or spec.window
        basis = controls[1].segmented_control(
            "Compare", tuple(tiles.BASIS_CONTROLS), default=spec.basis,
            key=f"tile_dialog_{tile_id}_basis") or spec.basis
        split_label = controls[2].selectbox(
            "Split by", tuple(_SPLIT_OPTIONS),
            key=f"tile_dialog_{tile_id}_split")

    current_scope = _render_geo_drill(tile_id, initial_scope, spec.metric)
    basket_resolution = None
    if spec.metric == "trx_share":
        stage = baskets.adoption_stage_for_scope(
            tiles.effective_spec_filters(spec, current_scope))
        governed = baskets.resolve_basket(stage)
        labels = {
            f"Adaptive · {baskets.BASKETS[governed.basket_id].label}": None,
            "IL-17 class": "il17_class",
            "Advanced therapy": "advanced_therapy",
        }
        choice = st.segmented_control(
            "Market basket", tuple(labels), default=next(iter(labels)),
            key=f"tile_dialog_{tile_id}_basket",
            help="Adaptive uses adoption stage; an explicit choice is stamped as an override.",
        ) or next(iter(labels))
        basket_resolution = baskets.resolve_basket(stage, labels[choice])
        spec = tiles.require_valid_spec(replace(
            spec, variant=basket_resolution.semantic_variant))
        st.caption(basket_resolution.disclosure)
    effective = _effective_spec(spec, window, basis, _SPLIT_OPTIONS[split_label])
    evaluation = tile_runtime.evaluate_spec(effective, scope=current_scope)
    artifact = evaluation.artifact
    if basket_resolution is not None:
        artifact = copy.deepcopy(artifact)
        artifact.extras["basket_resolution"] = asdict(basket_resolution)
        artifact.extras["basket_registry_fingerprint"] = baskets.registry_fingerprint()
        chart_months = (
            artifact.chart_df["month"].dropna().astype(str).drop_duplicates().tolist()
            if artifact.chart_df is not None and "month" in artifact.chart_df else None
        )
        artifact.extras["basket_reconciliation"] = baskets.reconciliation_for_scope(
            basket_resolution.basket_id,
            tiles.effective_spec_filters(effective, current_scope),
            chart_months,
            artifact.resolution.source if artifact.resolution is not None else "source_a",
        )
        if artifact.resolution is not None:
            artifact.resolution.reason = (
                f"{basket_resolution.reason}; {artifact.resolution.reason}")
        artifact.caveats.append(basket_resolution.disclosure)
    common.render_answer(
        artifact, key=f"tile_dialog_{tile_id}", allow_expand=False)
