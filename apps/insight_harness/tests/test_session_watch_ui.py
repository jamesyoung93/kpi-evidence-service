"""Watch UI uses session-owned saved insights and never writes shared state."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

from harness import saved_insights, services, tiles


APP = str(Path(__file__).parent.parent / "app.py")


def _app():
    return AppTest.from_file(APP, default_timeout=60)


def _ask(at, question):
    next(item for item in at.text_input if item.label == "Question").set_value(question)
    return at.run()


def _button(at, label):
    return next(button for button in at.button if label in (button.label or ""))


def _exact_button(at, label):
    return next(button for button in at.button if button.label == label)


def _text(at):
    values = [item.value for item in at.markdown]
    values += [item.value for item in at.caption]
    return " ".join(values)


def test_watch_is_session_local_evaluated_and_removable():
    first = _ask(_app().run(), "What is TRx in the West region?")
    _exact_button(first, "👁").click().run()
    store = first.session_state[saved_insights.SESSION_STORE_KEY]
    assert len(store) == 1
    saved = store.all()[0]
    assert saved.metric == "trx"
    assert saved.filters == {"region": "West"}
    assert saved.source == "source_a"
    assert saved.variant == "units"
    assert services.load_watchlist() == []

    second = _app().run()
    second.radio(key="nav").set_value("Monitoring").run()
    assert saved.label not in _text(second)
    assert "Nothing watched yet" in _text(second)

    first.radio(key="nav").set_value("Monitoring").run()
    assert not first.exception
    assert saved.label in _text(first)
    _button(first, "Remove").click().run()
    assert len(first.session_state[saved_insights.SESSION_STORE_KEY]) == 0
    assert "Nothing watched yet" in _text(first)


def test_home_tile_actions_snapshot_controls_and_offer_json_download():
    at = _app().run()
    at.selectbox(key="home_source").set_value("source_b").run()
    at.selectbox(key="home_variant").set_value("normalized").run()
    at.radio(key="home_window").set_value("R3M").run()
    at.radio(key="home_basis").set_value("YoY").run()
    at.button(key="tile_trx_watch").click().run()
    saved = at.session_state[saved_insights.SESSION_STORE_KEY].all()[0]
    assert saved.catalog_tile_id == "trx"
    assert saved.source == "source_b" and saved.variant == "normalized"
    assert saved.window == "R3M" and saved.basis == "YoY"
    labels = [button.label for button in at.get("download_button")]
    assert "Download TRx JSON" in labels


def test_incompatible_global_overrides_never_crash_and_are_disclosed():
    at = _app().run()
    at.selectbox(key="home_variant").set_value("dollars").run()
    assert not at.exception
    assert "retained its governed variant" in _text(at)
    at.selectbox(key="home_source").set_value("source_b").run()
    assert not at.exception
    assert "retained its governed source" in _text(at)


def test_conflicting_global_scope_and_fixed_tile_scope_are_disclosed():
    at = _app().run()
    at.selectbox(key="home_scope").set_value("payer_channel::Medicaid").run()
    assert not at.exception
    assert "Selected Payer Channel (Medicaid)" in _text(at)
    assert "fixed Payer Channel (Commercial)" in _text(at)


def test_answer_watch_preserves_diagnostic_and_retrieval_identity():
    cases = (
        ("Which specialties account for the TRx change?", "Diagnostic", "specialty", None),
        ("List whitespace HCPs with no activity", "Retrieval", None, "whitespace"),
    )
    for question, question_class, breakdown, template in cases:
        at = _ask(_app().run(), question)
        _exact_button(at, "👁").click().run()
        saved = at.session_state[saved_insights.SESSION_STORE_KEY].all()[0]
        assert saved.question_class == question_class
        assert saved.spec.breakdown_dimension == breakdown
        assert saved.spec.retrieval_template == template
        at.radio(key="nav").set_value("Monitoring").run()
        assert not at.exception
        assert saved.label in _text(at)


def test_causal_design_is_not_watchable_without_event_identity_in_saved_spec():
    at = _ask(_app().run(), "What was the impact of the speaker program in the East?")
    assert not at.exception
    assert all(button.label != "👁" for button in at.button)


def test_hierarchy_scope_control_uses_registry_values():
    territory = tiles.dimension_values("territory")[0]
    at = _app().run()
    at.selectbox(key="home_scope").set_value(f"territory::{territory}").run()
    assert not at.exception
    assert at.selectbox(key="home_scope").value == f"territory::{territory}"
    assert any(territory in metric.label for metric in at.metric)


def test_monitoring_drillthrough_preserves_saved_resolution_and_basis():
    at = _app().run()
    at.selectbox(key="home_source").set_value("source_b").run()
    at.selectbox(key="home_variant").set_value("normalized").run()
    at.radio(key="home_basis").set_value("YoY").run()
    at.button(key="tile_trx_watch").click().run()
    at.radio(key="nav").set_value("Monitoring").run()
    saved = at.session_state[saved_insights.SESSION_STORE_KEY].all()[0]
    at.button(key=f"watch_{saved.id}").click().run()
    assert at.session_state["ask_src"] == "source_b"
    assert at.session_state["ask_var"] == "normalized"
    assert at.session_state["ask_basis"] == "same month last year"


def test_monitoring_ignores_saved_tiles_that_are_not_watched():
    at = _app().run()
    store = saved_insights.InMemorySavedInsightStore()
    store.add(saved_insights.create_saved_insight("trx", watched=False))
    at.session_state[saved_insights.SESSION_STORE_KEY] = store
    at.radio(key="nav").set_value("Monitoring").run()
    assert "Nothing watched yet" in _text(at)
