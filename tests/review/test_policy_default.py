from pathlib import Path

from kpi_evidence.registry import load_registry

ROOT = Path(__file__).resolve().parents[2]


def test_shipped_policy_keeps_auto_publication_off():
    policy = load_registry(ROOT / "registry").policy["auto_publish"]
    assert policy["verified"] is False
    assert policy["report_only_known_layout"] is False
