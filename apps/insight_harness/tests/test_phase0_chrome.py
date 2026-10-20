"""Focused coverage for the Phase 0 application chrome and model controls."""
from __future__ import annotations

import tomllib
from pathlib import Path

from streamlit.testing.v1 import AppTest

from harness import runtime_policy


ROOT = Path(__file__).parent.parent
APP = ROOT / "app.py"


def _app() -> AppTest:
    return AppTest.from_file(str(APP), default_timeout=60)


def _has_key(elements, key: str) -> bool:
    return any(element.key == key for element in elements)


def _captions(at: AppTest) -> list[str]:
    return [str(item.value) for item in at.sidebar.caption]


def test_streamlit_chrome_is_minimal_and_page_titles_are_responsive():
    with (ROOT / ".streamlit" / "config.toml").open("rb") as handle:
        config = tomllib.load(handle)
    assert config["client"]["toolbarMode"] == "minimal"

    source = APP.read_text(encoding="utf-8")
    assert "#MainMenu" in source
    assert "footer" in source
    assert '[data-testid="stToolbar"]' in source
    assert '[data-testid="stMainBlockContainer"] h1' in source
    assert "font-size: clamp(" in source


def test_keyless_sidebar_is_quiet_collapsed_and_fully_functional(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("INSIGHT_HARNESS_ALLOW_PUBLIC_LLM", raising=False)

    at = _app().run()

    assert not at.exception
    assert at.radio(key="nav").label == "Navigate"
    assert any(title.value == "Insight Harness" for title in at.sidebar.title)
    assert len([value for value in _captions(at)
                if value.startswith("Translator:")]) == 1
    assert "Translator: built-in parser · ready" in _captions(at)
    assert any(value.startswith("Data version") for value in _captions(at))

    connector = next(item for item in at.sidebar.expander
                     if item.label == "Connect a language model…")
    assert connector.proto.expanded is False
    assert _has_key(at.text_input, "api_key")
    assert not _has_key(at.selectbox, "llm_model")
    assert at.title  # Home renders without a key; the parser path remains usable.


def test_session_key_reveals_allowlisted_model_and_bounded_quota(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("INSIGHT_HARNESS_ALLOW_PUBLIC_LLM", raising=False)
    monkeypatch.setenv("INSIGHT_HARNESS_LLM_SESSION_LIMIT", "7")

    at = _app().run()
    at.text_input(key="api_key").set_value("sk-ant-test-placeholder").run()

    assert not at.exception
    assert "Translator: language model · session credential" in _captions(at)
    assert tuple(at.selectbox(key="llm_model").options) == runtime_policy.allowed_models()
    assert "Session model-call allowance: 7 of 7 remaining." in _captions(at)
    assert "sk-ant-test-placeholder" not in " ".join(_captions(at))


def test_deployment_key_stays_opt_in_and_preserves_quota(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deployment-placeholder")
    monkeypatch.delenv("INSIGHT_HARNESS_ALLOW_PUBLIC_LLM", raising=False)
    monkeypatch.setenv("INSIGHT_HARNESS_LLM_SESSION_LIMIT", "11")

    locked = _app().run()
    assert "Translator: built-in parser · ready" in _captions(locked)
    assert not _has_key(locked.selectbox, "llm_model")
    assert any("not enabled for anonymous sessions" in value
               for value in _captions(locked))

    monkeypatch.setenv("INSIGHT_HARNESS_ALLOW_PUBLIC_LLM", "true")
    enabled = _app().run()
    assert "Translator: language model · deployment credential" in _captions(enabled)
    assert tuple(enabled.selectbox(key="llm_model").options) == runtime_policy.allowed_models()
    assert "Session model-call allowance: 11 of 11 remaining." in _captions(enabled)
    rendered = " ".join(_captions(enabled))
    assert "sk-ant-deployment-placeholder" not in rendered
