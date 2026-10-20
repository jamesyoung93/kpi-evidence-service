"""Public deployment policy is secure by default and explicitly configurable."""
from harness import runtime_policy


def test_deployment_model_key_is_opt_in(monkeypatch):
    monkeypatch.delenv("INSIGHT_HARNESS_ALLOW_PUBLIC_LLM", raising=False)
    assert not runtime_policy.deployment_llm_enabled()
    monkeypatch.setenv("INSIGHT_HARNESS_ALLOW_PUBLIC_LLM", "true")
    assert runtime_policy.deployment_llm_enabled()


def test_model_allowlist_and_quota_are_bounded(monkeypatch):
    monkeypatch.delenv("INSIGHT_HARNESS_LLM_MODELS", raising=False)
    assert runtime_policy.allowed_models() == runtime_policy.DEFAULT_MODELS
    monkeypatch.setenv("INSIGHT_HARNESS_LLM_MODELS", "model-a, model-b, model-a")
    assert runtime_policy.allowed_models() == ("model-a", "model-b")

    monkeypatch.setenv("INSIGHT_HARNESS_LLM_SESSION_LIMIT", "0")
    assert runtime_policy.session_model_call_limit() == 1
    monkeypatch.setenv("INSIGHT_HARNESS_LLM_SESSION_LIMIT", "9999")
    assert runtime_policy.session_model_call_limit() == 500


def test_governance_token_uses_server_configuration(monkeypatch):
    monkeypatch.delenv("INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN", raising=False)
    assert not runtime_policy.governance_admin_configured()
    assert not runtime_policy.valid_governance_token("anything")

    monkeypatch.setenv("INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN", "correct horse")
    assert runtime_policy.governance_admin_configured()
    assert runtime_policy.valid_governance_token("correct horse")
    assert not runtime_policy.valid_governance_token("wrong")
