"""The pharma golden set and generated-data contract are the trust anchor."""
import hashlib
from pathlib import Path

import pandas as pd

from data import generate_demo_data as generator
from harness import pipeline
from harness import semantic_layer as sl


def test_pharma_golden_set_passes_and_reproduces():
    result = pipeline.run_golden(record=False)
    failures = result[~result["pass"]]
    assert failures.empty, f"golden failures:\n{failures.to_string()}"
    non_reproducible = result[~result["reproducible"]]
    assert non_reproducible.empty, f"non-reproducible:\n{non_reproducible.to_string()}"
    assert len(result) >= 29


def test_generator_is_in_process_deterministic_and_reconciled():
    first = generator.build_demo(generator.SEED)
    second = generator.build_demo(generator.SEED)
    for left, right in zip(first, second):
        pd.testing.assert_frame_equal(left, right, check_exact=True)

    first_referral = generator.build_referrals(first[0], first[2], generator.SEED)
    second_referral = generator.build_referrals(second[0], second[2], generator.SEED)
    pd.testing.assert_frame_equal(first_referral, second_referral, check_exact=True)

    source_a, source_b, accounts = first
    assert not source_a.duplicated(["account_id", "month"]).any()
    assert set(source_a["account_id"]) == set(accounts["account_id"])
    assert "account_id" not in source_b
    assert source_b["month"].max() == "2026-05"


def test_checked_in_data_version_matches_csv_bytes_and_ground_truth():
    digest = hashlib.sha256()
    data_dir = Path(sl.DATA_DIR)
    for name in ("accounts.csv", "fact_referral.csv", "fact_source_a.csv",
                 "fact_source_b.csv"):
        digest.update((data_dir / name).read_bytes())
    assert digest.hexdigest()[:12] == sl.data_version() == sl.ground_truth()["data_version"]


def test_public_registry_is_wholesale_pharma():
    assert set(sl.METRICS) == {
        "trx", "nrx", "nbrx", "trx_share", "calls", "call_plan",
        "call_attainment", "samples", "speaker_attendance", "new_writers",
        "referrals_in", "active_referrers",
    }
    assert sl.DIMENSIONS == ["territory", "district", "region", "specialty",
                             "payer_channel"]
    assert set(sl.EVENTS) == {"speaker_launch", "formulary_win", "competitor_launch"}
    assert all(sl.METRICS[metric].get("digest_family") == "rx_volume"
               for metric in ("trx", "nrx", "nbrx"))
    registry_text = repr((sl.METRICS, sl.DIMENSIONS, sl.EVENTS)).lower()
    for legacy in ("revenue", "new_customers", "enterprise", "east_program", "west_shock"):
        assert legacy not in registry_text


def test_refusals_are_scoped_and_offer_pharma_reframes():
    for question in (
        "Forecast TRx for next quarter",
        "Why is morale down this quarter?",
        "What is our patient happiness index?",
        "What is TRx in E-CAR-01 and Endocrinology?",
    ):
        artifact = pipeline.answer(question)
        assert artifact.tier == "Abstained"
        assert len(artifact.headline.removeprefix("Declined: ")) > 40
        assert artifact.extras.get("reframes")
        assert all("revenue" not in reframe.lower() for reframe in artifact.extras["reframes"])
