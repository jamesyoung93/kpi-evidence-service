from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from harness import tiles, triage
from views import tile_detail


ROOT = Path(__file__).resolve().parents[1]


def test_dialog_local_controls_preserve_governed_spec_contract():
    base = tiles.question_spec("trx", window="Latest", basis="MoM")
    changed = tile_detail._effective_spec(base, "R6M", "YoY", None)
    assert changed.window == "R6M"
    assert changed.basis == "YoY"
    assert changed.question_class == triage.DESCRIPTIVE

    split = tile_detail._effective_spec(base, "R3M", "QoQ", "specialty")
    assert split.question_class == triage.DIAGNOSTIC
    assert split.breakdown_dimension == "specialty"
    assert split.window == "R3M"
    assert split.basis == "QoQ"


def test_dialog_does_not_mutate_retrieval_specs():
    retrieval = tiles.question_spec("whitespace_hcps")
    assert tile_detail._effective_spec(
        retrieval, "R12M", "YoY", "region") == retrieval


def test_home_tile_expand_opens_the_governed_dialog():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    at.button(key="tile_trx_expand").click().run()
    assert at.selectbox(key="tile_dialog_trx_split").value == "No split"
    assert at.button(key="tile_dialog_trx_crumb_0")
