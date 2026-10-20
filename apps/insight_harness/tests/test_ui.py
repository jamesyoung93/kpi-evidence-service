"""Headless end-to-end coverage for the public Streamlit application.

These tests deliberately assert user-visible contracts and exact analytical
resolution.  They avoid depending on incidental card ordering or demo values,
which are allowed to move when the deterministic source data is regenerated.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from harness import pipeline, profiles, runtime_policy, saved_insights, services, tiles, triage
from harness import semantic_layer as sl
from harness.provenance import TIER_ABSTAINED


REPO = Path(__file__).parent.parent
APP = str(REPO / "app.py")

PAGES = (
    "Home",
    "Digest",
    "Monitoring",
    "Causal Studio",
    "Semantic Layer",
    "Reliability",
    "How answers are produced",
)


def app(timeout: int = 45) -> AppTest:
    return AppTest.from_file(APP, default_timeout=timeout)


def ask(at: AppTest, question: str) -> AppTest:
    next(item for item in at.text_input if item.label == "Question").set_value(question)
    return at.run()


def rendered_text(at: AppTest) -> str:
    elements = []
    for name in (
        "title", "header", "subheader", "markdown", "caption", "info",
        "warning", "error", "success",
    ):
        elements.extend(at.get(name))
    return " ".join(str(item.value) for item in elements)


def button_labeled(at: AppTest, label: str):
    matches = [button for button in at.button if label in (button.label or "")]
    assert matches, f"no button containing {label!r}; have {[b.label for b in at.button]}"
    return matches[0]


def answer_artifact(at: AppTest):
    history = at.session_state["history"]
    assert history, "the question did not create a session-history artifact"
    return history[-1]["artifact"]


def chart_count(at: AppTest) -> int:
    return len(at.get("vega_lite_chart") or at.get("arrow_vega_lite_chart"))


# ---------------------------------------------------------------------------
# Navigation, home defaults, hierarchy, and session-local customization
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("page", PAGES)
def test_every_public_page_renders_without_an_exception(page):
    at = app(timeout=90).run()
    at.radio(key="nav").set_value(page).run()
    assert not at.exception, f"{page} raised: {at.exception}"


def test_home_starts_with_live_persona_kpis_and_hierarchy_scope_control():
    at = app().run()
    persona = profiles.PERSONAS_BY_ID["executive"]
    values = {metric.label: metric.value for metric in at.metric}
    expected = {
        tiles.TILES_BY_ID[tile_id].label for tile_id in persona.default_tile_ids
        if tiles.TILES_BY_ID[tile_id].viz_kind != "table" and tile_id != "trx_share"
    }

    assert expected <= set(values)
    assert all(values[label] not in (None, "", "—") for label in expected)
    share_labels = [label for label in values
                    if label.startswith("TRx market share")
                    and "advanced-therapy market" in label]
    assert len(share_labels) == 1
    assert values[share_labels[0]] not in (None, "", "—")
    assert "Top HCP activity gaps" in rendered_text(at)
    assert at.selectbox(key="home_persona").value == persona.id
    assert at.radio(key="home_window").value == persona.default_window
    assert at.radio(key="home_basis").value == persona.default_basis
    assert at.selectbox(key="home_scope").value == tiles.scope_key(persona.default_scope)

    scope_labels = set(at.selectbox(key="home_scope").options)
    for prefix in ("Region ·", "District ·", "Territory ·", "Specialty ·", "Payer channel ·"):
        assert any(str(option).startswith(prefix) for option in scope_labels)
    assert next(item for item in at.text_input if item.label == "Question").value == ""


def test_each_persona_applies_its_registered_scope_window_and_basis_defaults():
    at = app().run()
    for persona_id in (
        "sales_rep", "district_manager", "brand_marketing", "market_access", "executive",
    ):
        at.selectbox(key="home_persona").set_value(persona_id).run()
        persona = profiles.PERSONAS_BY_ID[persona_id]
        assert at.radio(key="home_window").value == persona.default_window
        assert at.radio(key="home_basis").value == persona.default_basis
        assert at.selectbox(key="home_scope").value == tiles.scope_key(persona.default_scope)

    assert dict(profiles.PERSONAS_BY_ID["sales_rep"].default_scope).keys() == {"territory"}
    assert dict(profiles.PERSONAS_BY_ID["district_manager"].default_scope).keys() == {"district"}
    assert dict(profiles.PERSONAS_BY_ID["executive"].default_scope) == {}


def test_home_scope_recomputes_tiles_at_region_and_specialty_grain():
    at = app().run()
    national = next(metric.value for metric in at.metric if metric.label == "TRx")

    at.selectbox(key="home_scope").set_value("region::East").run()
    east = next(metric.value for metric in at.metric if metric.label == "TRx · East")
    assert east != national

    at.selectbox(key="home_scope").set_value("specialty::Cardiology").run()
    cardiology = next(metric.value for metric in at.metric
                      if metric.label == "TRx · Cardiology")
    assert cardiology != national
    assert not at.exception


def test_tile_customization_is_session_local_and_resettable():
    at = app().run()
    assert "Details" not in {metric.label for metric in at.metric}

    at.selectbox(key="home_executive_add_choice").set_value("calls").run()
    at.button(key="home_executive_add").click().run()
    assert "Details" in {metric.label for metric in at.metric}

    at.radio(key="home_window").set_value("R3M").run()
    at.button(key="home_executive_save").click().run()
    at.selectbox(key="home_persona").set_value("sales_rep").run()
    at.selectbox(key="home_persona").set_value("executive").run()
    assert at.radio(key="home_window").value == "R3M"
    assert "Details" in {metric.label for metric in at.metric}

    fresh = app().run()
    assert "Details" not in {metric.label for metric in fresh.metric}
    assert fresh.radio(key="home_window").value == profiles.PERSONAS_BY_ID[
        "executive"].default_window

    at.button(key="home_executive_reset").click().run()
    assert "Details" not in {metric.label for metric in at.metric}
    assert at.radio(key="home_window").value == profiles.PERSONAS_BY_ID[
        "executive"].default_window


def test_every_default_kpi_has_named_watch_download_open_and_breakdown_actions():
    at = app().run()
    persona = profiles.PERSONAS_BY_ID["executive"]
    button_keys = {button.key for button in at.button}
    downloads = {button.key: button.label for button in at.get("download_button")}

    for tile_id in persona.default_tile_ids:
        label = tiles.TILES_BY_ID[tile_id].label
        assert f"tile_{tile_id}_open" in button_keys
        assert f"tile_{tile_id}_expand" in button_keys
        if tiles.TILES_BY_ID[tile_id].question_class != triage.COHORT:
            assert f"tile_{tile_id}_watch" in button_keys
            assert f"tile_{tile_id}_breakdown" in button_keys
        assert downloads[f"tile_{tile_id}_download"] == f"Download {label} JSON"


def test_incompatible_global_overrides_disclose_fallbacks_without_crashing_tiles():
    at = app().run()
    at.selectbox(key="home_source").set_value("source_b").run()
    at.selectbox(key="home_variant").set_value("dollars").run()

    assert not at.exception
    executive = profiles.PERSONAS_BY_ID["executive"]
    expected_metrics = sum(
        tiles.TILES_BY_ID[tile_id].viz_kind != "table"
        for tile_id in executive.default_tile_ids)
    assert len(at.metric) == expected_metrics
    assert "Top HCP activity gaps" in rendered_text(at)
    assert "Override note:" in rendered_text(at)


# ---------------------------------------------------------------------------
# Exact tile/watch/drill-through identity
# ---------------------------------------------------------------------------
def test_tile_breakdown_preserves_scope_window_basis_source_and_variant_exactly():
    at = app().run()
    at.radio(key="home_window").set_value("R3M").run()
    at.radio(key="home_basis").set_value("QoQ").run()
    at.selectbox(key="home_scope").set_value("region::East").run()
    at.selectbox(key="home_source").set_value("source_b").run()
    at.selectbox(key="home_variant").set_value("units").run()
    at.button(key="tile_trx_breakdown").click().run()

    artifact = answer_artifact(at)
    intent = artifact.extras["intent"]
    assert at.session_state["nav"] == "Home"
    assert at.session_state["ask_src"] == "source_b"
    assert at.session_state["ask_var"] == "units"
    assert at.session_state["ask_basis"] == "prior quarter"
    assert artifact.engine == "decomposition"
    assert artifact.resolution.source == "source_b"
    assert artifact.resolution.variant == "units"
    assert intent.filters == {"region": "East"}
    assert intent.compare_basis == "prior_quarter"
    assert intent.window.kind == "last_n" and len(intent.window.months) == 3


def test_session_saved_watch_replays_exact_resolution_and_never_uses_shared_watch_file():
    at = app().run()
    at.radio(key="home_window").set_value("R3M").run()
    at.radio(key="home_basis").set_value("QoQ").run()
    at.selectbox(key="home_scope").set_value("region::East").run()
    at.selectbox(key="home_source").set_value("source_b").run()
    at.selectbox(key="home_variant").set_value("units").run()
    at.button(key="tile_trx_watch").click().run()

    store = at.session_state[saved_insights.SESSION_STORE_KEY]
    (insight,) = store.all()
    assert isinstance(store, saved_insights.InMemorySavedInsightStore)
    assert dict(insight.spec.filters) == {"region": "East"}
    assert insight.spec.source == "source_b"
    assert insight.spec.variant == "units"
    assert insight.spec.window == "R3M"
    assert insight.spec.basis == "QoQ"
    assert services.load_watchlist() == []

    at.radio(key="nav").set_value("Monitoring").run()
    assert insight.label in rendered_text(at)
    at.button(key=f"watch_{insight.id}").click().run()
    artifact = answer_artifact(at)
    assert at.session_state["ask_src"] == insight.source
    assert at.session_state["ask_var"] == insight.variant
    assert at.session_state["ask_basis"] == "prior quarter"
    assert artifact.resolution.source == insight.source
    assert artifact.resolution.variant == insight.variant
    assert artifact.extras["intent"].filters == {"region": "East"}

    at.radio(key="nav").set_value("Monitoring").run()
    at.button(key=f"unwatch_{insight.id}").click().run()
    assert store.all() == ()
    assert "Nothing watched yet" in rendered_text(at)

    fresh = app().run()
    fresh.radio(key="nav").set_value("Monitoring").run()
    assert "Nothing watched yet" in rendered_text(fresh)


# ---------------------------------------------------------------------------
# Custom questions, refusals, artifacts, and memory
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question,question_class,engine,metric,event_id,visible",
    (
        ("What is TRx in the West region?", "Descriptive", "descriptive", "trx", None,
         "TRx"),
        ("Trend calls by month in the North region", "Descriptive", "descriptive", "calls",
         None, "Details"),
        ("Which specialties account for the TRx change?", "Diagnostic", "decomposition",
         "trx", None, "Largest single contributor"),
        ("List whitespace HCPs with no activity", "Retrieval", "retrieval", "trx", None,
         "whitespace HCP"),
        ("What was the impact of the speaker program in the East?", "Causal",
         "causal_advisor", "trx", "speaker_launch", "difference-in-differences"),
        ("What was the impact of the competitor launch in West Cardiology?", "Causal",
         "causal_advisor", "trx", "competitor_launch", "difference-in-differences"),
    ),
)
def test_each_supported_question_class_round_trips_through_the_ui(
        question, question_class, engine, metric, event_id, visible):
    at = ask(app().run(), question)
    artifact = answer_artifact(at)

    assert not at.exception
    assert artifact.question_class == question_class
    assert artifact.engine == engine
    assert artifact.resolution.metric == metric
    assert artifact.extras["intent"].event_id == event_id
    assert visible in rendered_text(at)


@pytest.mark.parametrize(
    "question,question_class",
    (
        ("Forecast TRx for next quarter", "Predictive"),
        ("Why is morale down this quarter?", "Causal"),
        ("What is our customer happiness index?", "Out of scope"),
    ),
)
def test_unsupported_questions_render_exportable_scoped_refusals(question, question_class):
    at = ask(app().run(), question)
    artifact = answer_artifact(at)

    assert not at.exception
    assert artifact.tier == TIER_ABSTAINED
    assert artifact.question_class == question_class
    assert "Scoped refusal" in rendered_text(at)
    assert any(item.label == "Answer JSON" for item in at.get("download_button"))


def test_refusal_escapes_question_html_and_reframe_chip_runs_a_real_answer():
    malicious = "What is <script>alert('x')</script>?"
    at = ask(app().run(), malicious)
    refusal_blocks = [item.value for item in at.markdown if "Scoped refusal" in item.value]
    assert refusal_blocks and "&lt;script&gt;" in refusal_blocks[0]
    assert "<script>" not in refusal_blocks[0]

    at = ask(app().run(), "Forecast TRx for next quarter")
    reframe = next(button for button in at.button if "_reframe_" in str(button.key))
    reframe.click().run()
    assert not at.exception
    assert answer_artifact(at).tier != TIER_ABSTAINED


def test_answer_downloads_cover_json_and_retrieval_csv():
    at = ask(app().run(), "What is TRx in the West region?")
    labels = [item.label for item in at.get("download_button")]
    assert "Answer JSON" in labels

    at = ask(app().run(), "List whitespace HCPs with no activity")
    labels = [item.label for item in at.get("download_button")]
    assert "Answer JSON" in labels
    assert "Table CSV" in labels


def test_diagnostic_question_adds_a_waterfall_and_honors_breakdown_dimension():
    at = app().run()
    baseline_charts = chart_count(at)
    at = ask(at, "Which specialties account for the TRx change?")
    artifact = answer_artifact(at)

    assert artifact.extras["intent"].dim_breakdown == "specialty"
    assert "specialty" in artifact.extras["tables"]
    assert chart_count(at) > baseline_charts


def test_history_records_multiple_questions_and_replays_the_exact_artifact():
    questions = (
        "What is TRx in the West region?",
        "Trend calls by month in the North region",
    )
    at = app().run()
    for question in questions:
        at = ask(at, question)
    history = at.session_state["history"]
    first_hash = history[0]["hash"]

    assert [entry["question"] for entry in history] == list(questions)
    button_labeled(at, questions[0]).click().run()
    assert not at.exception
    assert first_hash in rendered_text(at)


def test_question_and_answer_context_survive_navigation_round_trip():
    question = "What is TRx in the West region?"
    at = ask(app().run(), question)
    result_hash = answer_artifact(at).result_hash
    at.radio(key="nav").set_value("Semantic Layer").run()
    at.radio(key="nav").set_value("Home").run()

    assert next(item for item in at.text_input if item.label == "Question").value == question
    assert result_hash in rendered_text(at)


def test_comparison_basis_selector_recomputes_diagnostic_answer():
    at = ask(app().run(), "Which specialties account for the TRx change?")
    prior_hash = answer_artifact(at).result_hash
    at.selectbox(key="ask_basis").set_value("same month last year").run()
    artifact = answer_artifact(at)

    assert not at.exception
    assert artifact.result_hash != prior_hash
    assert artifact.extras["intent"].compare_basis == "yoy"
    assert "same month last year" in artifact.headline


# ---------------------------------------------------------------------------
# Monitoring and Causal Studio navigation
# ---------------------------------------------------------------------------
def test_monitoring_breakdown_opens_a_real_diagnostic_answer():
    at = app().run()
    at.radio(key="nav").set_value("Monitoring").run()
    button_labeled(at, "Break this down").click().run()

    assert at.session_state["nav"] == "Home"
    assert answer_artifact(at).engine == "decomposition"
    assert "Largest single contributor" in rendered_text(at)


def test_decomposition_drills_into_the_matching_registered_causal_event():
    at = ask(app().run(), "Which specialties account for the TRx change?")
    button_labeled(at, "Test attribution in Causal Studio").click().run()

    assert at.session_state["nav"] == "Causal Studio"
    assert at.selectbox(key="studio_event").value == "competitor_launch"
    assert "Competitor launch" in rendered_text(at)
    assert "Assumption checks" in rendered_text(at)


def test_causal_signoff_is_locked_without_session_admin_authentication(monkeypatch):
    for configured_token in (None, "correct-token"):
        if configured_token is None:
            monkeypatch.delenv("INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN", raising=False)
        else:
            monkeypatch.setenv("INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN", configured_token)

        at = app().run()
        at.radio(key="nav").set_value("Causal Studio").run()
        at.selectbox(key="studio_event").set_value("formulary_win").run()
        button_labeled(at, "Propose a design").click().run()

        assert "Analyst sign-off is locked" in rendered_text(at)
        assert not [button for button in at.button
                    if button.label == "Mark as analyst-reviewed"]
        assert services.feedback_history().empty


def test_authenticated_causal_signoff_and_context_survive_navigation(monkeypatch):
    monkeypatch.setenv("INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN", "correct-token")
    at = app().run()
    at.radio(key="nav").set_value("Semantic Layer").run()
    at.text_input(key="governance_admin_token").set_value("correct-token").run()
    assert at.session_state["_governance_admin_credential"] == "correct-token"

    at.radio(key="nav").set_value("Causal Studio").run()
    at.selectbox(key="studio_event").set_value("formulary_win").run()
    button_labeled(at, "Propose a design").click().run()
    artifact = at.session_state["studio_art"]
    assert artifact.extras["intent"].event_id == "formulary_win"
    assert "Medicare Part D formulary win" in rendered_text(at)

    button_labeled(at, "Mark as analyst-reviewed").click().run()
    assert "Analyst-reviewed" in rendered_text(at)
    history = services.feedback_history()
    reviews = history.loc[
        (history["verdict"] == "analyst_reviewed")
        & (history["result_hash"] == artifact.result_hash)
    ]
    assert len(reviews) == 1
    assert reviews.iloc[0]["note"] == "authenticated-admin"

    at.radio(key="nav").set_value("Monitoring").run()
    at.radio(key="nav").set_value("Causal Studio").run()
    assert at.selectbox(key="studio_event").value == "formulary_win"
    assert "Medicare Part D formulary win" in rendered_text(at)


# ---------------------------------------------------------------------------
# Digest: complete export, exact drill-through, and session isolation
# ---------------------------------------------------------------------------
def test_digest_renders_diverse_ranked_items_and_complete_plus_item_downloads():
    at = app(timeout=90).run()
    at.radio(key="nav").set_value("Digest").run()

    assert not at.exception
    assert len(at.subheader) == 3
    assert len({item.value for item in at.subheader}) == 3
    labels = [item.label for item in at.get("download_button")]
    assert labels.count("Download complete digest") == 1
    assert labels.count("Download details") == 3
    assert sum(button.label == "Break this down" for button in at.button) == 3
    assert "Reviewed" in rendered_text(at) and "metric families" in rendered_text(at)


def test_digest_drillthrough_preserves_the_candidate_resolution():
    at = app(timeout=90).run()
    at.radio(key="nav").set_value("Digest").run()
    next(button for button in at.button if button.label == "Break this down").click().run()
    artifact = answer_artifact(at)

    assert at.session_state["nav"] == "Home"
    assert at.session_state["ask_src"] in sl.SOURCES
    assert at.session_state["ask_var"] in sl.METRICS[artifact.resolution.metric]["variants"]
    assert artifact.resolution.source == at.session_state["ask_src"]
    assert artifact.resolution.variant == at.session_state["ask_var"]
    # A default anomaly has no explicit comparison basis to preserve.  In that
    # case drill-through must clear any stale override and leave Ask at its
    # disclosed engine default; watched candidates retain their explicit basis.
    basis_from_control = {
        "prior month": "prior_month",
        "prior quarter": "prior_quarter",
        "same month last year": "yoy",
    }.get(at.session_state["ask_basis"])
    assert artifact.extras["intent"].compare_basis == basis_from_control


def test_digest_history_store_is_owned_by_each_app_session():
    first = app(timeout=90).run()
    first.radio(key="nav").set_value("Digest").run()
    first_store = first.session_state["_digest_session_history_store"]
    assert len(first_store.load()) == 1

    second = app(timeout=90).run()
    second.radio(key="nav").set_value("Digest").run()
    second_store = second.session_state["_digest_session_history_store"]
    assert len(second_store.load()) == 1
    assert second_store is not first_store


# ---------------------------------------------------------------------------
# Secure optional-model policy and governance administration
# ---------------------------------------------------------------------------
def test_no_key_exposes_bounded_parser_and_hides_model_controls(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("INSIGHT_HARNESS_ALLOW_PUBLIC_LLM", raising=False)
    at = app().run()
    text = rendered_text(at)

    assert "Translator: built-in parser · ready" in text
    assert "Add your own Anthropic API key" in text
    assert not [item for item in at.selectbox if item.key == "llm_model"]
    assert any(item.key == "api_key" for item in at.text_input)


def test_session_key_enables_translation_without_exposing_the_secret(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    secret = "sk-ant-test-placeholder"
    at = app().run()
    at.text_input(key="api_key").set_value(secret).run()
    text = rendered_text(at)

    assert "Translator: language model · session credential" in text
    assert "credential entered for this app session" in text
    assert tuple(at.selectbox(key="llm_model").options) == runtime_policy.allowed_models()
    assert secret not in text


def test_deployment_key_is_secure_by_default_and_requires_explicit_public_opt_in(monkeypatch):
    secret = "sk-ant-deployment-placeholder"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.delenv("INSIGHT_HARNESS_ALLOW_PUBLIC_LLM", raising=False)
    locked = app().run()
    locked_text = rendered_text(locked)
    assert "Translator: built-in parser · ready" in locked_text
    assert "not enabled for anonymous sessions" in locked_text
    assert not [item for item in locked.selectbox if item.key == "llm_model"]
    assert secret not in locked_text

    monkeypatch.setenv("INSIGHT_HARNESS_ALLOW_PUBLIC_LLM", "true")
    enabled = app().run()
    enabled_text = rendered_text(enabled)
    assert "Translator: language model · deployment credential" in enabled_text
    assert "explicitly enabled by the deployment owner" in enabled_text
    assert tuple(enabled.selectbox(key="llm_model").options) == runtime_policy.allowed_models()
    assert secret not in enabled_text


def test_governance_controls_are_absent_until_a_valid_admin_token(monkeypatch):
    monkeypatch.delenv("INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN", raising=False)
    at = app().run()
    at.radio(key="nav").set_value("Semantic Layer").run()
    assert "Administration is disabled" in rendered_text(at)
    assert not at.number_input
    assert not [button for button in at.button if button.label == "Apply governance changes"]

    monkeypatch.setenv("INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN", "correct-token")
    at = app().run()
    at.radio(key="nav").set_value("Semantic Layer").run()
    at.text_input(key="governance_admin_token").set_value("wrong-token").run()
    assert "not valid" in rendered_text(at)
    assert not at.number_input

    at.text_input(key="governance_admin_token").set_value("correct-token").run()
    assert "Administrator controls unlocked" in rendered_text(at)
    assert at.number_input
    assert button_labeled(at, "Apply governance changes")


def test_authorized_governance_change_updates_registry_and_audit_log(monkeypatch):
    monkeypatch.setenv("INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN", "correct-token")
    at = app().run()
    at.radio(key="nav").set_value("Semantic Layer").run()
    at.text_input(key="governance_admin_token").set_value("correct-token").run()
    at.number_input[0].set_value(10.0).run()
    button_labeled(at, "Apply governance changes").click().run()

    assert not at.exception
    assert sl.materiality() == pytest.approx(0.10)
    assert sl.governance_log()
    assert sl.governance_log()[-1]["actor"] == "authenticated-admin"
    assert "10.0%" in rendered_text(at)


def test_feedback_vote_is_session_deduplicated_by_exact_result_hash():
    at = ask(app().run(), "What is TRx in the West region?")
    result_hash = answer_artifact(at).result_hash

    correct = button_labeled(at, "👍")
    wrong = button_labeled(at, "🚩")
    assert not correct.disabled
    assert not wrong.disabled

    correct.click().run()
    feedback = services.feedback_history()
    matching = feedback.loc[feedback["result_hash"] == result_hash]
    assert len(matching) == 1
    assert matching.iloc[0]["verdict"] == "correct"
    assert at.session_state["_feedback_votes"][result_hash] == "correct"

    # The click writes the vote after this run's buttons were constructed;
    # the next ordinary rerun must render both choices disabled.
    at.run()
    assert button_labeled(at, "👍").disabled
    assert button_labeled(at, "🚩").disabled

    # Ordinary reruns and navigation must not reopen voting for the same answer.
    at.radio(key="nav").set_value("Reliability").run()
    at.radio(key="nav").set_value("Home").run()
    assert button_labeled(at, "👍").disabled
    assert button_labeled(at, "🚩").disabled
    assert len(services.feedback_history().loc[
        services.feedback_history()["result_hash"] == result_hash
    ]) == 1


# ---------------------------------------------------------------------------
# Public reliability record
# ---------------------------------------------------------------------------
def test_accuracy_check_passes_through_the_ui():
    at = app(timeout=180).run()
    at.radio(key="nav").set_value("Reliability").run()

    assert not at.exception
    values = {metric.label: metric.value for metric in at.metric}
    assert values["Pass rate"] == "100%"
    assert values["Reproducible"] == "100%"
    assert values["Correct refusals"] == "100%"
