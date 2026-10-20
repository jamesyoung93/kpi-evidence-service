from __future__ import annotations

import io
import json
import urllib.request

import pytest

from kpi_evidence import ai
from kpi_evidence.ai import GatewayChatProvider, ProviderError, resolve_bearer_token
from kpi_evidence.settings import Settings

PAT = "dapi-workspace-pat"
KEY = "sk-model-key"


@pytest.fixture()
def both_tokens(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", PAT)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.setattr(ai, "_sdk_bearer_token", lambda host: None)


@pytest.mark.parametrize("url", ["http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "https://localhost/v1",
                                 "https://api.example-llm.com/v1", "http://adb-1.2.azuredatabricks.net/x"])
def test_other_hosts_get_the_api_key(both_tokens, url):
    assert resolve_bearer_token(url) == KEY


def test_https_databricks_hosts_get_the_workspace_token(both_tokens, monkeypatch):
    assert resolve_bearer_token("https://adb-1.2.azuredatabricks.net/ai-gateway/mlflow/v1") == PAT
    monkeypatch.setenv("DATABRICKS_HOST", "https://workspace.internal.example")
    assert resolve_bearer_token("https://workspace.internal.example/serving-endpoints") == PAT
    assert resolve_bearer_token("http://workspace.internal.example/serving-endpoints") == KEY


def test_a_local_gateway_without_an_api_key_is_an_error(both_tokens, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        resolve_bearer_token("http://localhost:11434/v1")


def test_local_gateway_requests_carry_the_api_key(both_tokens, monkeypatch):
    seen = {}

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        seen["auth"], seen["url"] = req.get_header("Authorization"), req.full_url
        body = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        return _Response(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    provider = GatewayChatProvider(Settings(ai_mode="gateway", ai_model="llama3",
                                            ai_gateway_base_url="http://localhost:11434/v1"), retry_delay_s=0.0)
    provider.chat([{"role": "user", "content": "hi"}])
    assert seen == {"auth": f"Bearer {KEY}", "url": "http://localhost:11434/v1/chat/completions"}
