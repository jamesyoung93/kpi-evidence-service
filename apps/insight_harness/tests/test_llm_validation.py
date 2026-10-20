"""Malformed model output must reject cleanly and preserve pipeline behavior."""
import json
from types import SimpleNamespace

import anthropic
import pytest

from harness import llm_translator, pipeline
from harness import semantic_layer as sl


def _raw(**overrides):
    payload = {
        "question_class": "Descriptive", "metric": "trx",
        "filters": {"region": "West"}, "trend": False,
        "dim_breakdown": None, "event_id": None, "template": None,
        "window": None, "compare_basis": None, "basket_id": None, "reason": "",
    }
    payload.update(overrides)
    return json.dumps(payload)


@pytest.mark.parametrize("field", [
    "question_class", "metric", "event_id", "basket_id",
    "dim_breakdown", "template", "compare_basis",
])
@pytest.mark.parametrize("value", [[], {}, True, 1],
                         ids=["array", "object", "boolean", "number"])
def test_non_string_identifiers_raise_translation_error(field, value):
    with pytest.raises(llm_translator.TranslationError) as caught:
        llm_translator._validate("What is TRx in the West region?",
                                 _raw(**{field: value}))
    assert caught.value.kind == "rejected"


@pytest.mark.parametrize("filters", [[], None, False, 0, "", ["West"]],
                         ids=["empty-array", "null", "boolean", "number",
                              "empty-string", "nonempty-array"])
def test_non_object_filters_are_rejected_instead_of_becoming_unfiltered(filters):
    with pytest.raises(llm_translator.TranslationError) as caught:
        llm_translator._validate("What is TRx in the West region?",
                                 _raw(filters=filters))
    assert caught.value.kind == "rejected"


def _mock_model_response(monkeypatch, raw):
    # Exercise translate() and its real validator; only the external SDK call
    # is replaced. No API key or live model is needed for this regression.
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text=raw)])
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_: response))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **_: client)


@pytest.mark.parametrize("overrides", [
    {"metric": ["trx"]}, {"question_class": {}},
    {"event_id": []}, {"basket_id": {}},
    {"filters": []}, {"filters": None}, {"filters": False},
], ids=["metric", "class", "event", "basket", "filters-array",
        "filters-null", "filters-boolean"])
def test_malformed_model_output_falls_back_to_correct_scoped_answer(monkeypatch, overrides):
    _mock_model_response(monkeypatch, _raw(**overrides))

    artifact = pipeline.answer("What is TRx in the West region?",
                               api_key="test-only-not-a-real-key")

    frame = sl.load_fact("source_a")
    expected = frame[(frame["region"] == "West")
                     & (frame["month"] == frame["month"].max())]["trx_units"].sum()
    assert artifact.value == pytest.approx(expected)
    assert artifact.extras["intent"].filters == {"region": "West"}
    assert artifact.extras["translation"]["translator"] == "rules"
    assert artifact.extras["translation"]["fallback_kind"] == "rejected"
    assert artifact.extras["translation"]["fallback_detail"]


def test_malformed_model_output_preserves_forecast_refusal(monkeypatch):
    _mock_model_response(monkeypatch, _raw(metric=["trx"]))

    artifact = pipeline.answer("Forecast TRx for next quarter",
                               api_key="test-only-not-a-real-key")

    assert artifact.tier == "Abstained"
    assert artifact.value is None
    assert artifact.extras["reframes"]
    assert artifact.extras["translation"]["fallback_kind"] == "rejected"
