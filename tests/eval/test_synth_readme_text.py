from __future__ import annotations

import re

from kpi_evidence.eval import synth_reports as sr


def test_generated_readme_is_neutral_and_needs_no_rewrite_in_place(synth_set):
    text = (synth_set["dir"] / sr.README_FILE).read_text(encoding="utf-8")
    assert text.startswith("# Synthetic report set (labeled)")
    assert "synthetic platform data" in text
    assert not re.search(r"insight_harness|seed-42|DESIGN|python -c|labelled|truth\s+tests", text)


def test_committed_readme_matches_the_generator(synth_set):
    from kpi_evidence.settings import demo_dir

    committed = demo_dir("reports") / sr.README_FILE
    assert committed.read_bytes() == (synth_set["dir"] / sr.README_FILE).read_bytes()
