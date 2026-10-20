from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from views import home


ROOT = Path(__file__).resolve().parents[1]


def test_home_leads_with_three_digest_stories_and_links_through():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=45).run()
    story_buttons = [button for button in at.button
                     if str(button.key or "").startswith("home_digest_expand_")]
    assert len(story_buttons) == 3
    assert at.button(key="home_open_digest")

    at.button(key="home_open_digest").click().run()
    assert at.radio(key="nav").value == "Digest"


def test_home_digest_strip_uses_css_clamping_without_mutating_the_headline():
    module_source = (ROOT / "views" / "home.py").read_text(encoding="utf-8")
    start = module_source.index("def _render_digest_strip")
    end = module_source.index("\ndef render(", start)
    source = module_source[start:end]
    assert "-webkit-line-clamp:2" in source
    assert "headline[:" not in source
    assert "artifact.result_hash" not in source


def test_home_digest_story_expands_the_complete_artifact():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=45).run()
    story = next(button for button in at.button
                 if str(button.key or "").startswith("home_digest_expand_"))
    story.click().run()
    assert any(expander.label == "Why this surfaced" for expander in at.expander)
    assert any("Evidence stamp" in caption.value for caption in at.caption)
