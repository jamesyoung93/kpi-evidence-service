"""Deployment policy for optional model access and privileged administration.

The analytical harness is fully functional without any secret.  These helpers
keep public deployments secure-by-default: a deployment-owned language-model
key is never spent for anonymous visitors unless the owner opts in, model IDs
are allowlisted, and governance writes require a server-configured token.
"""
from __future__ import annotations

import hmac
import os


DEFAULT_MODELS = (
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "claude-opus-4-8",
)


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def allowed_models() -> tuple[str, ...]:
    configured = tuple(
        dict.fromkeys(
            value.strip() for value in
            os.environ.get("INSIGHT_HARNESS_LLM_MODELS", "").split(",")
            if value.strip()
        )
    )
    return configured or DEFAULT_MODELS


def deployment_llm_enabled() -> bool:
    """Whether anonymous sessions may use a deployment-owned Anthropic key."""

    return _enabled("INSIGHT_HARNESS_ALLOW_PUBLIC_LLM")


def session_model_call_limit() -> int:
    try:
        configured = int(os.environ.get("INSIGHT_HARNESS_LLM_SESSION_LIMIT", "25"))
    except ValueError:
        configured = 25
    return min(500, max(1, configured))


def governance_admin_configured() -> bool:
    return bool(os.environ.get("INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN"))


def valid_governance_token(candidate: str | None) -> bool:
    expected = os.environ.get("INSIGHT_HARNESS_GOVERNANCE_ADMIN_TOKEN", "")
    return bool(expected and candidate and hmac.compare_digest(candidate, expected))


def retain_raw_questions() -> bool:
    """Raw feedback questions are opt-in because they may contain sensitive text."""

    return _enabled("INSIGHT_HARNESS_LOG_RAW_QUESTIONS")
