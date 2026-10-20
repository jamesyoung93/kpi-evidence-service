"""Shared test fixtures. Ensures the repo root is importable regardless of
where pytest is invoked from, and keeps the suite hermetic: no test may reach
the network, and no test may write state into the repo's data directory.
"""
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

os.environ.pop("ANTHROPIC_API_KEY", None)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect every mutable state file to a per-test temp dir."""
    from harness import digest_store, pipeline, services
    from harness import semantic_layer as sl
    monkeypatch.setattr(services, "FEEDBACK_LOG", tmp_path / "feedback_log.jsonl")
    monkeypatch.setattr(services, "WATCHLIST_PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(sl, "CONFIG_PATH", tmp_path / "governance_config.json")
    monkeypatch.setattr(sl, "GOVERNANCE_LOG", tmp_path / "governance_log.jsonl")
    monkeypatch.setattr(pipeline, "EVAL_HISTORY", tmp_path / "eval_history.jsonl")
    monkeypatch.setattr(digest_store, "DIGEST_HISTORY_PATH", tmp_path / "digest_history.jsonl")
