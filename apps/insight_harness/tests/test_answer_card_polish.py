"""Focused contracts for the shared answer-card presentation."""
from __future__ import annotations

import inspect

import pandas as pd
from streamlit.testing.v1 import AppTest

from harness import pipeline
from harness import voice
from views import common


def _render_answer(question: str) -> None:
    import streamlit as st

    from harness import pipeline as answer_pipeline
    from views import common as answer_common

    artifact = answer_pipeline.answer(question)
    st.session_state["_tested_artifact"] = artifact
    answer_common.render_answer(artifact, key="focused")


def test_hero_content_preserves_each_engine_class_semantics():
    descriptive = pipeline.answer("What is TRx in the West region?")
    value, label, body = common.hero_figure_content(descriptive)
    assert value == common.format_artifact_value(descriptive, descriptive.value)
    assert "TRx units · West · 2026-06" == label
    assert body == voice.tile_presentation(
        descriptive, persona="executive").headline

    diagnostic = pipeline.answer("Which specialties account for the TRx change?")
    value, label, body = common.hero_figure_content(diagnostic)
    assert value == common.format_native_delta(
        diagnostic.resolution.metric, diagnostic.resolution.variant, diagnostic.value)
    assert f'{diagnostic.extras["m0"]}–{diagnostic.extras["m1"]}' in label
    assert body == voice.tile_presentation(
        diagnostic, persona="executive").headline

    causal = pipeline.answer(
        "What was the impact of the competitor launch in West Cardiology?")
    value, label, body = common.hero_figure_content(causal)
    assert value == f'{causal.extras["estimate"]["did_pct"] * 100:+.1f}%'
    assert "West · Cardiology" in label
    assert body == voice.tile_presentation(causal, persona="executive").headline

    retrieval = pipeline.answer("List whitespace HCPs with no activity")
    value, label, body = common.hero_figure_content(retrieval)
    assert value == f"{len(retrieval.table):,} records"
    assert label.endswith("current account snapshot")
    assert body == voice.tile_presentation(
        retrieval, persona="executive").headline


def test_hero_metadata_uses_computed_window_and_causal_scope():
    partial = pipeline.answer(
        "Which regions account for the TRx change in Q2 2026?", source="source_b")
    _, partial_label, _ = common.hero_figure_content(partial)
    assert partial_label.endswith(
        f'{partial.extras["m0"]}–{partial.extras["m1"]}')
    assert "Q2 2026" not in partial_label

    causal = pipeline.answer("What was the impact of the speaker program?")
    _, causal_label, _ = common.hero_figure_content(causal)
    assert "East Cardiology 1 (E-CAR-01)" in causal_label
    assert "East Endocrinology 1 (E-END-01)" in causal_label
    assert "territory" not in causal_label.casefold()
    estimate = causal.extras["estimate"]
    assert causal_label.endswith(f'{estimate["pre"][0]}–{estimate["post"][-1]}')


def test_answer_card_has_one_export_menu_icon_actions_and_stamped_hash():
    at = AppTest.from_function(
        _render_answer,
        args=("List whitespace HCPs with no activity",),
        default_timeout=30,
    ).run()

    assert not at.exception
    artifact = at.session_state["_tested_artifact"]
    assert [item.proto.popover.label for item in at.get("popover")] == ["Download"]
    assert [item.label for item in at.get("download_button")] == [
        "Answer JSON", "Table CSV"]

    buttons = {item.label: item for item in at.button}
    assert buttons["👍"].help == "Mark this answer correct"
    assert buttons["🚩"].help == "Flag this number as wrong"
    assert buttons["👁"].help == (
        "Pin this metric and scope to the Watched list in Monitoring.")

    stamp = next(item.value for item in at.markdown if "translator:" in item.value)
    assert f"result hash: {artifact.result_hash}" in stamp
    assert all(getattr(item, "value", None) != artifact.result_hash for item in at.code)


def test_answer_card_markup_and_chart_polish_contracts():
    # Keep the bordered receipt explicit even if later visual CSS is retuned.
    assert "st.container(border=True)" in inspect.getsource(common.render_answer)

    chart = common.waterfall(
        pd.DataFrame(
            {
                "value": ["A", "B"],
                "period_start": [100.0, 100.0],
                "period_end": [112.0, 95.0],
                "delta": [12.0, -5.0],
            }
        ),
        "2026-05",
        "2026-06",
    ).to_dict()
    bar_marks = [layer["mark"] for layer in chart["layer"]
                 if isinstance(layer.get("mark"), dict)
                 and layer["mark"].get("type") == "bar"]
    assert bar_marks == [{"type": "bar", "cornerRadius": 2, "size": 24}]
    assert "Why two answers exist" in inspect.getsource(
        common._divergence_block)


def test_presentation_helpers_do_not_change_result_hashes_or_source_tables():
    descriptive = pipeline.answer("What is TRx in the West region?")
    cohort = pipeline.answer(
        "Compare the activity mix of top 20 HCPs by NRx share with matched peers")
    expected_hashes = {
        "descriptive": "0436bd948647",
        "cohort": "7874ccd5961a",
    }
    source_table = cohort.table.copy(deep=True)

    common.hero_figure_content(descriptive, persona="executive")
    common.hero_figure_content(cohort, persona="executive")
    rendered_table = voice.humanize_table(cohort.table, persona="executive")

    assert descriptive.result_hash == expected_hashes["descriptive"]
    assert cohort.result_hash == expected_hashes["cohort"]
    pd.testing.assert_frame_equal(cohort.table, source_table)
    assert rendered_table is not cohort.table


def test_reusable_hero_uses_44px_semibold_and_escapes_text(monkeypatch):
    rendered = {}

    def capture(markup: str, **kwargs) -> None:
        rendered["markup"] = markup
        rendered.update(kwargs)

    monkeypatch.setattr(common.st, "markdown", capture)
    common.render_hero_figure(
        "<44>", "TRx · <all>", "A <script> is text.", "Verified", "Descriptive")

    markup = rendered["markup"]
    assert "font-size:44px" in markup
    assert "font-weight:600" in markup
    assert '<section class="answer-hero"' in markup
    assert '<h3 class="answer-hero-value"' in markup
    assert 'aria-label="TRx · &lt;all&gt;: &lt;44&gt;"' in markup
    assert "&lt;44&gt;" in markup
    assert "&lt;script&gt;" in markup
    assert "<script>" not in markup
    assert rendered["unsafe_allow_html"] is True
