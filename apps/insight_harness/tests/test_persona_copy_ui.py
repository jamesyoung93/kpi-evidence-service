"""Rendered copy guardrails across every operational persona.

The pure voice tests own exact prose fixtures.  This suite checks that the
actual Streamlit surfaces keep machine identifiers and analyst jargon behind
the appropriate detail controls when those adapters are wired together.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from harness import profiles, services


APP = str(Path(__file__).parent.parent / "app.py")
RAW_SCOPE = re.compile(
    r"\b(?:region|district|territory|specialty|payer_channel)\s*=\s*[^\s,;]+",
    re.IGNORECASE,
)
SNAKE_TOKEN = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
PASSIVE_MOVEMENT = re.compile(
    r"\bwas\s+[+-]?[\d,.]+%\s+(?:above|below)\b", re.IGNORECASE,
)
RELATIVE_RATIO = re.compile(
    r"(?:differs?\s+by\s+[+-]?[\d,.]+%|"
    r"[+-]?[\d,.]+%\s+relative\s+difference)",
    re.IGNORECASE,
)
HEX_STAMP = re.compile(r"\b[0-9a-f]{12,64}\b", re.IGNORECASE)
CARD_JARGON = re.compile(r"\b(?:governed|artifact|deterministic)\b", re.IGNORECASE)


def _plain(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def _node_strings(node, *, include_help: bool = True) -> list[str]:
    out: list[str] = []
    widget_without_visible_raw_value = type(node).__name__ in {
        "Selectbox", "Radio", "Multiselect", "TextInput", "Checkbox", "Slider",
    }
    attrs = ("label", "delta") if widget_without_visible_raw_value \
        else ("value", "label", "delta")
    for attr in attrs:
        try:
            value = getattr(node, attr, None)
        except (KeyError, RuntimeError):
            value = None
        if value not in (None, ""):
            out.append(_plain(value))
    if include_help:
        try:
            value = getattr(node, "help", None)
        except (KeyError, RuntimeError):
            value = None
        if value:
            out.append(_plain(value))
    try:
        options = getattr(node, "options", None)
    except (KeyError, RuntimeError):
        options = None
    if options:
        out.extend(_plain(value) for value in options)
    return [value for value in out if value]


def _surface_strings(at: AppTest) -> list[str]:
    strings: list[str] = []
    names = (
        "title", "header", "subheader", "markdown", "caption", "info",
        "warning", "error", "success", "metric", "button", "download_button",
        "selectbox", "radio", "multiselect", "text_input", "checkbox", "slider",
        "expander",
    )
    for name in names:
        for node in at.get(name):
            strings.extend(_node_strings(node))
    for frame in at.dataframe:
        value = frame.value
        if isinstance(value, pd.DataFrame):
            strings.extend(_plain(column) for column in value.columns)
            for column in value.columns:
                if value[column].dtype == object:
                    strings.extend(_plain(cell) for cell in value[column].dropna())
    return [value for value in strings if value]


def _face_strings(node) -> list[str]:
    """Walk visible nodes while treating expanders/popovers as one-click detail."""

    type_name = type(node).__name__
    node_type = getattr(node, "type", None)
    if type_name == "Expander":
        return [_plain(getattr(node, "label", ""))]
    if node_type in {"popover", "dialog"}:
        return []
    children = getattr(node, "children", None)
    if children:
        out: list[str] = []
        values = children.values() if hasattr(children, "values") else children
        for child in values:
            out.extend(_face_strings(child))
        return out
    return _node_strings(node, include_help=False)


def _violations(text: str) -> list[str]:
    hits = []
    for label, pattern in (
        ("raw scope", RAW_SCOPE),
        ("snake_case", SNAKE_TOKEN),
        ("passive movement", PASSIVE_MOVEMENT),
        ("relative ratio comparison", RELATIVE_RATIO),
    ):
        match = pattern.search(text)
        if match:
            hits.append(f"{label}: {match.group(0)!r} in {text[:180]!r}")
    return hits


def test_home_digest_monitoring_copy_is_human_for_every_persona():
    at = AppTest.from_file(APP, default_timeout=180).run()
    failures: list[str] = []
    for persona_id in profiles.PERSONAS_BY_ID:
        at.radio(key="nav").set_value("Home").run()
        at.selectbox(key="home_persona").set_value(persona_id).run()
        for page in ("Home", "Digest", "Monitoring"):
            if page != "Home":
                at.radio(key="nav").set_value(page).run()
            if at.exception:
                failures.append(f"{persona_id}/{page}: {at.exception}")
                continue
            for text in _surface_strings(at):
                failures.extend(
                    f"{persona_id}/{page}: {hit}" for hit in _violations(text)
                )
    assert not failures, "\n".join(failures)


def test_digest_card_faces_hide_evidence_and_ranking_methodology():
    at = AppTest.from_file(APP, default_timeout=180).run()
    at.radio(key="nav").set_value("Digest").run()
    assert not at.exception

    # The sidebar's data-version stamp is an allowed operational identifier;
    # this assertion is specifically about the main Digest/card faces.
    face = " ".join(_face_strings(at._tree.children[0]))
    assert not HEX_STAMP.search(face)
    assert not CARD_JARGON.search(face)
    assert services.PRIORITY_SCORE_FORMULA not in face
    assert "45% standardized" not in face

    labels = {item.label for item in at.expander}
    assert "How ranking works" in labels
    assert "Evidence" in labels
