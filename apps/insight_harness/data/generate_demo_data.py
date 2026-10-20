"""Generate the deterministic, internally reconciled pharma demo dataset.

The benchmark has three deliberately different source products:

* ``source_a`` is account-month grain and is the system of record for
  prescriptions, field effort, samples, programs, and writer activity.
* ``source_b`` is an aggregated projected retail panel with an exact,
  registered regional projection factor, a one-month lag, and an exact early
  history restatement.  It never pretends to have account or field-effort
  grain.
* ``referral`` is an observed-only receiving-HCP/month relationship feed with
  deterministic 80% account coverage.  Missing accounts are unknown, never
  silently treated as zero or projected to full coverage.

All random state is local to :func:`build_demo`.  Calling it repeatedly with
the same seed returns byte-stable frames; no module-global RNG can leak state
between test runs or notebook calls.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
SEED = 42

MONTHS = pd.period_range("2024-07", "2026-06", freq="M").astype(str).tolist()
REGIONS = ("North", "South", "East", "West")
SPECIALTIES = ("Primary Care", "Cardiology", "Endocrinology")
PAYER_CHANNELS = ("Commercial", "Medicare Part D", "Medicaid", "Cash")

REGION_CODE = {"North": "N", "South": "S", "East": "E", "West": "W"}
SPECIALTY_CODE = {"Primary Care": "PCP", "Cardiology": "CAR",
                  "Endocrinology": "END"}

SOURCE_B_BIAS = {"North": 1.025, "South": 1.035, "East": 1.030, "West": 1.032}
SOURCE_B_RESTATED = tuple(MONTHS[:3])
SOURCE_B_RESTATEMENT_FACTOR = 1.015
SOURCE_B_LAG = 1

SPEAKER_START = "2025-10"
SPEAKER_TERRITORIES = ("E-CAR-01", "E-END-01")
SPEAKER_CONTROLS = ("N-CAR-01", "N-END-01")
SPEAKER_LIFT = 0.08

FORMULARY_START = "2026-01"
FORMULARY_LIFT = 0.10

COMPETITOR_START = "2026-04"
COMPETITOR_SHOCK = -0.22

RX_COLUMNS = ("trx_units", "trx_dollars", "trx_normalized", "nrx", "nbrx",
              "market_trx", "market_nrx", "il17_competitor_a_trx",
              "il17_competitor_b_trx", "advanced_other_trx",
              "il17_class_trx", "advanced_therapy_trx")
PANEL_KEYS = ("month", "territory", "district", "region", "specialty",
              "payer_channel")

REFERRAL_COVERAGE_TARGET = 0.80
REFERRAL_SOURCE_SEED_OFFSET = 101
RECENT_ADOPTER_MONTHS = 6


def month_index(month: str) -> int:
    return MONTHS.index(month)


def _territory(region: str, specialty: str, number: int) -> str:
    return f"{REGION_CODE[region]}-{SPECIALTY_CODE[specialty]}-{number:02d}"


def build_universe(rng: np.random.Generator) -> pd.DataFrame:
    """Create a stable 240-HCP universe and its governed hierarchy."""
    rows: list[dict] = []
    serial = 0
    specialty_base = {"Primary Care": 17.0, "Cardiology": 29.0,
                      "Endocrinology": 23.0}
    for region in REGIONS:
        for local in range(60):
            serial += 1
            specialty = SPECIALTIES[local // 20]
            territory_number = local % 2 + 1
            payer = PAYER_CHANNELS[(local + REGIONS.index(region)) % len(PAYER_CHANNELS)]
            baseline = specialty_base[specialty] * rng.lognormal(0.0, 0.38)
            first_idx = 0 if rng.random() < 0.72 else int(rng.integers(1, len(MONTHS)))
            rows.append({
                "account_id": f"HCP{serial:04d}",
                # Reserved synthetic-demo range.  These are deliberately not
                # represented as real National Provider Identifiers.
                "npi": f"9999{serial:06d}",
                "name": f"Dr. Morgan {serial:03d}",
                "territory": _territory(region, specialty, territory_number),
                "district": f"{region} District {territory_number}",
                "region": region,
                "specialty": specialty,
                "payer_channel": payer,
                "baseline_monthly_trx": baseline,
                "first_writer_idx": first_idx,
                "price_per_trx": {"Primary Care": 88.0, "Cardiology": 126.0,
                                  "Endocrinology": 108.0}[specialty]
                                  * {"Commercial": 1.08, "Medicare Part D": 0.96,
                                     "Medicaid": 0.82, "Cash": 1.0}[payer],
            })

    universe = pd.DataFrame(rows)
    # Stable baseline deciles select a deterministic lapsed high-value cohort.
    universe["baseline_decile"] = pd.qcut(
        universe["baseline_monthly_trx"].rank(method="first"), 10,
        labels=False) + 1
    universe["lapse_idx"] = pd.Series([pd.NA] * len(universe), dtype="Int64")
    high_value = universe.index[universe["baseline_decile"] >= 8].tolist()
    for order, idx in enumerate(high_value):
        if order % 3 == 0:
            universe.loc[idx, "lapse_idx"] = 18 + (order % 4)
            universe.loc[idx, "first_writer_idx"] = 0
    # Registered causal cohorts and their controls have complete, stable
    # writer history. This keeps the benchmark's counterfactual identifiable;
    # new-writer and lapse behavior remains in the rest of the universe.
    protected = (
        universe["territory"].isin(SPEAKER_TERRITORIES + SPEAKER_CONTROLS)
        | ((universe["region"].isin(["North", "South", "East"]))
           & (universe["payer_channel"] == "Medicare Part D"))
        | (universe["specialty"] == "Cardiology")
    )
    universe.loc[protected, "first_writer_idx"] = 0
    universe.loc[protected, "lapse_idx"] = pd.NA
    whitespace_candidates = universe[(universe["baseline_decile"] >= 8) & ~protected] \
        .sort_values("baseline_monthly_trx", ascending=False).head(12).index
    universe.loc[whitespace_candidates, "first_writer_idx"] = 0
    universe.loc[whitespace_candidates, "lapse_idx"] = 21
    return universe


def _event_multiplier(row: pd.Series, month: str) -> tuple[float, dict[str, int]]:
    speaker = int(row["territory"] in SPEAKER_TERRITORIES and month >= SPEAKER_START)
    formulary = int(row["region"] == "South"
                    and row["payer_channel"] == "Medicare Part D"
                    and month >= FORMULARY_START)
    competitor = int(row["region"] == "West"
                     and row["specialty"] == "Cardiology"
                     and month >= COMPETITOR_START)
    multiplier = ((1.0 + SPEAKER_LIFT) ** speaker
                  * (1.0 + FORMULARY_LIFT) ** formulary
                  * (1.0 + COMPETITOR_SHOCK) ** competitor)
    return multiplier, {
        "speaker_launch_active": speaker,
        "formulary_win_active": formulary,
        "competitor_launch_active": competitor,
    }


def build_fact(universe: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Build source A at one row per HCP per month."""
    rows: list[dict] = []
    region_mult = {"North": 1.06, "South": 0.94, "East": 1.00, "West": 1.03}
    base_share = {"Primary Care": 0.086, "Cardiology": 0.112,
                  "Endocrinology": 0.101}
    il17_share = {"Primary Care": 0.255, "Cardiology": 0.315,
                  "Endocrinology": 0.285}

    for mi, month in enumerate(MONTHS):
        trend = 1.0 + 0.0035 * mi
        seasonality = 1.07 if month[-2:] in ("10", "11", "12") else 1.0
        for _, hcp in universe.iterrows():
            started = mi >= int(hcp["first_writer_idx"])
            lapse_idx = hcp["lapse_idx"]
            lapsed = pd.notna(lapse_idx) and mi >= int(lapse_idx)
            potential = (float(hcp["baseline_monthly_trx"])
                         * region_mult[hcp["region"]] * trend * seasonality)
            event_multiplier, event_flags = _event_multiplier(hcp, month)
            trx = potential * event_multiplier if started and not lapsed else 0.0

            nrx_rate = {"Primary Care": 0.235, "Cardiology": 0.165,
                        "Endocrinology": 0.205}[hcp["specialty"]]
            nrx = min(trx, trx * nrx_rate)
            nbrx_rate = {"Commercial": 0.71, "Medicare Part D": 0.68,
                         "Medicaid": 0.75, "Cash": 0.64}[hcp["payer_channel"]]
            nbrx = min(nrx, nrx * nbrx_rate)

            # The market denominator follows the untreated local market. Brand
            # interventions therefore move share rather than moving numerator
            # and denominator in lockstep.
            market_trx = potential / base_share[hcp["specialty"]]
            market_nrx = market_trx * {
                "Primary Care": 0.225, "Cardiology": 0.185,
                "Endocrinology": 0.205,
            }[hcp["specialty"]]
            il17_class_trx = potential / il17_share[hcp["specialty"]]
            non_brand_class_trx = max(il17_class_trx - trx, 0.0)
            competitor_a = non_brand_class_trx * 0.56
            competitor_b = non_brand_class_trx - competitor_a
            advanced_other = max(market_trx - il17_class_trx, 0.0)
            # Reconcile the persisted six-decimal member cells exactly. This
            # avoids a one-micro-unit basket mismatch after CSV round-tripping.
            trx_value = round(trx, 6)
            competitor_a_value = round(competitor_a, 6)
            competitor_b_value = round(competitor_b, 6)
            advanced_other_value = round(advanced_other, 6)
            il17_class_value = round(
                trx_value + competitor_a_value + competitor_b_value, 6)
            advanced_therapy_value = round(
                il17_class_value + advanced_other_value, 6)
            normalized = trx * ({"Commercial": 1.0, "Medicare Part D": 0.98,
                                 "Medicaid": 0.95, "Cash": 1.02}[hcp["payer_channel"]])

            plan = 2.0 + 0.68 * int(hcp["baseline_decile"])
            if lapsed:
                calls = 0
                samples = 0
            else:
                calls = int(rng.poisson(plan * (0.94 + 0.015 * np.sin(mi / 2))))
                samples = int(rng.poisson(max(calls * 2.2, 0.0)))
            speaker_lambda = 1.8 if event_flags["speaker_launch_active"] else 0.12
            attendance = 0 if lapsed else int(rng.poisson(speaker_lambda))

            rows.append({
                "account_id": hcp["account_id"],
                "npi": hcp["npi"],
                "month": month,
                "territory": hcp["territory"],
                "district": hcp["district"],
                "region": hcp["region"],
                "specialty": hcp["specialty"],
                "payer_channel": hcp["payer_channel"],
                "trx_units": trx_value,
                "trx_dollars": round(trx * float(hcp["price_per_trx"]), 6),
                "trx_normalized": round(normalized, 6),
                "nrx": round(max(0.0, nrx), 6),
                "nbrx": round(max(0.0, nbrx), 6),
                "market_trx": advanced_therapy_value,
                "market_nrx": round(market_nrx, 6),
                "il17_competitor_a_trx": competitor_a_value,
                "il17_competitor_b_trx": competitor_b_value,
                "advanced_other_trx": advanced_other_value,
                "il17_class_trx": il17_class_value,
                "advanced_therapy_trx": advanced_therapy_value,
                "calls": calls,
                "call_plan": round(plan, 6),
                "samples": samples,
                "speaker_attendance": attendance,
                # The first measured month has no prior observation and cannot
                # distinguish incumbent from newly adopting writers. Preserve
                # it as unknown instead of manufacturing a launch-sized spike.
                "new_writers": (
                    float("nan") if mi == 0
                    else int(started and mi == int(hcp["first_writer_idx"]))
                ),
                **event_flags,
            })
    return pd.DataFrame(rows)


def build_source_b(source_a: pd.DataFrame) -> pd.DataFrame:
    """Aggregate and project source A with exact registered pathologies."""
    panel = source_a.groupby(list(PANEL_KEYS), as_index=False)[list(RX_COLUMNS)].sum()
    factors = panel["region"].map(SOURCE_B_BIAS).astype(float)
    factors = factors.where(~panel["month"].isin(SOURCE_B_RESTATED),
                            factors * SOURCE_B_RESTATEMENT_FACTOR)
    for column in RX_COLUMNS:
        panel[column] = (panel[column].astype(float) * factors).round(6)
    panel = panel[~panel["month"].isin(MONTHS[-SOURCE_B_LAG:])].reset_index(drop=True)
    return panel


def _months_since_last(mask: pd.Series) -> int:
    active = [MONTHS.index(month) for month in mask.index[mask.astype(bool)].tolist()]
    return len(MONTHS) if not active else len(MONTHS) - 1 - max(active)


def build_accounts(source_a: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    """Derive the current HCP universe from the same account-month fact."""
    recent12 = set(MONTHS[-12:])
    recent3 = set(MONTHS[-3:])
    rows: list[dict] = []
    names = universe.set_index("account_id")["name"].to_dict()
    for account_id, group in source_a.groupby("account_id", sort=True):
        group = group.sort_values("month")
        static = group.iloc[0]
        ttm = group[group["month"].isin(recent12)]
        q90 = group[group["month"].isin(recent3)]
        by_month = group.set_index("month")
        activity = ((by_month["calls"] > 0) | (by_month["samples"] > 0)
                    | (by_month["speaker_attendance"] > 0))
        positive_months = group.loc[group["trx_units"] > 0, "month"]
        first_brand_month = str(positive_months.iloc[0]) if len(positive_months) else None
        months_since_adoption = (
            len(MONTHS) - 1 - MONTHS.index(first_brand_month)
            if first_brand_month is not None else None
        )
        adoption_stage = (
            "never_adopter" if first_brand_month is None else
            "recent_adopter" if months_since_adoption < RECENT_ADOPTER_MONTHS else
            "established"
        )
        rows.append({
            "account_id": account_id,
            "npi": static["npi"],
            "name": names[account_id],
            "territory": static["territory"],
            "district": static["district"],
            "region": static["region"],
            "specialty": static["specialty"],
            "payer_channel": static["payer_channel"],
            "trx_ttm": round(float(ttm["trx_units"].sum()), 3),
            "nrx_ttm": round(float(ttm["nrx"].sum()), 3),
            "market_nrx_ttm": round(float(ttm["market_nrx"].sum()), 3),
            "nrx_share_ttm": round(
                float(ttm["nrx"].sum()) / float(ttm["market_nrx"].sum()), 8)
                if float(ttm["market_nrx"].sum()) else float("nan"),
            "nbrx_ttm": round(float(ttm["nbrx"].sum()), 3),
            "months_since_rx": _months_since_last(by_month["trx_units"] > 0),
            "months_since_activity": _months_since_last(activity),
            "calls_90d": int(q90["calls"].sum()),
            "call_plan_90d": round(float(q90["call_plan"].sum()), 3),
            "first_brand_month": first_brand_month,
            "adoption_stage": adoption_stage,
        })
    accounts = pd.DataFrame(rows)
    accounts["decile"] = pd.qcut(accounts["trx_ttm"].rank(method="first"), 10,
                                  labels=False) + 1
    return accounts


def build_referrals(source_a: pd.DataFrame, accounts: pd.DataFrame,
                    seed: int = SEED) -> pd.DataFrame:
    """Build an observed-only referral feed at receiving-HCP/month grain.

    Exactly four of every five synthetic HCPs are covered, evenly distributed
    across the stable account sequence.  A row exists for every covered
    account-month, including observed zeroes.  Uncovered accounts have no row
    and therefore remain unknown.  ``active_referrers`` is additive because a
    synthetic referrer is assigned to only one receiving HCP in each month.
    """
    rng = np.random.default_rng(seed + REFERRAL_SOURCE_SEED_OFFSET)
    covered_ids = set(
        accounts.loc[
            accounts["account_id"].str[-4:].astype(int).mod(5).ne(0),
            "account_id",
        ]
    )
    expected = int(round(len(accounts) * REFERRAL_COVERAGE_TARGET))
    if len(covered_ids) != expected:
        raise AssertionError(
            f"referral coverage contract drifted: {len(covered_ids)} != {expected}")

    covered = source_a[source_a["account_id"].isin(covered_ids)].copy()
    referral_lambda = (
        0.35 + covered["nrx"].astype(float) * 0.075
        + covered["speaker_attendance"].astype(float) * 0.20
    )
    covered["referrals_in"] = rng.poisson(referral_lambda).astype(int)
    # A referring HCP may send multiple patients.  The stable binomial draw
    # yields a distinct-referrer count bounded by incoming referrals.
    covered["active_referrers"] = [
        max(1, int(rng.binomial(int(count), 0.72))) if count else 0
        for count in covered["referrals_in"]
    ]
    columns = [
        "account_id", "npi", "month", "territory", "district", "region",
        "specialty", "payer_channel", "referrals_in", "active_referrers",
    ]
    return covered[columns].sort_values(["month", "account_id"]).reset_index(drop=True)


def build_demo(seed: int = SEED) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    universe = build_universe(rng)
    source_a = build_fact(universe, rng)
    source_b = build_source_b(source_a)
    accounts = build_accounts(source_a, universe)
    return source_a, source_b, accounts


def _ground_truth(version: str) -> dict:
    return {
        "data_version": version,
        "seed": SEED,
        "months": MONTHS,
        "grain": {
            "source_a": ["account_id", "month"],
            "source_b": list(PANEL_KEYS),
            "accounts": ["account_id"],
            "referral": ["account_id", "month"],
        },
        "events": {
            "speaker_launch": {
                "start": SPEAKER_START,
                "scope": {"territory": list(SPEAKER_TERRITORIES)},
                "control_scope": {"territory": list(SPEAKER_CONTROLS)},
                "true_effect_pct": SPEAKER_LIFT,
                "metrics": ["trx", "nrx", "nbrx"],
                "treatment_flag": "speaker_launch_active",
            },
            "formulary_win": {
                "start": FORMULARY_START,
                "scope": {"region": "South", "payer_channel": "Medicare Part D"},
                "control_scope": {"region": ["North", "East"],
                                  "payer_channel": "Medicare Part D"},
                "true_effect_pct": FORMULARY_LIFT,
                "metrics": ["trx", "nrx"],
                "treatment_flag": "formulary_win_active",
            },
            "competitor_launch": {
                "start": COMPETITOR_START,
                "scope": {"region": "West", "specialty": "Cardiology"},
                "control_scope": {"region": ["North", "South", "East"],
                                  "specialty": "Cardiology"},
                "true_effect_pct": COMPETITOR_SHOCK,
                "metrics": ["trx", "nrx", "nbrx"],
                "treatment_flag": "competitor_launch_active",
            },
        },
        "source_b_issues": {
            "bias_by_region": SOURCE_B_BIAS,
            "lag_months": SOURCE_B_LAG,
            "restated_months": list(SOURCE_B_RESTATED),
            "restatement_factor": SOURCE_B_RESTATEMENT_FACTOR,
            "affected_columns": list(RX_COLUMNS),
        },
        "referral_source": {
            "coverage_target": REFERRAL_COVERAGE_TARGET,
            "expected_accounts": 240,
            "observed_accounts": 192,
            "coverage_rate": 0.80,
            "coverage_rule": "synthetic account sequence excludes every fifth HCP",
            "zero_semantics": "zero is observed only for covered account-months",
            "projection": "none; observed-only metrics",
        },
        "market_baskets": {
            "il17_class": {
                "members": ["brand", "competitor_a", "competitor_b"],
                "denominator": "il17_class_trx",
            },
            "advanced_therapy": {
                "members": ["brand", "competitor_a", "competitor_b",
                            "advanced_other"],
                "denominator": "advanced_therapy_trx",
            },
        },
        "synthetic_identifiers": {
            "npi": "10-digit demo identifier in reserved 9999xxxxxx pattern; not a real NPI",
        },
        "metric_coverage": {
            "new_writers": {
                "warmup_month": MONTHS[0],
                "semantics": "undefined because no prior measured month exists",
            },
        },
        "variant_definitions": {
            "trx": {"units": "total prescriptions",
                    "dollars": "gross prescription value",
                    "normalized": "payer-channel normalized equivalent units"},
            "trx_share": {"numerator": "brand TRx units",
                          "denominator": "total market TRx units"},
            "call_attainment": {"numerator": "details delivered",
                                "denominator": "call plan"},
        },
        "invariants": ["nbrx <= nrx <= trx_units", "market_trx >= trx_units",
                       "market_nrx >= nrx",
                       "advanced_therapy_trx >= il17_class_trx >= trx_units",
                       "new_writers is undefined in the first measured month",
                       "one source_a row per account_id/month",
                       "accounts are derived from source_a",
                       "referral source covers exactly 80% of HCPs and is not projected"],
    }


def main() -> None:
    source_a, source_b, accounts = build_demo(SEED)
    referral = build_referrals(source_a, accounts, SEED)
    source_a.to_csv(HERE / "fact_source_a.csv", index=False, encoding="utf-8",
                    lineterminator="\n")
    source_b.to_csv(HERE / "fact_source_b.csv", index=False, encoding="utf-8",
                    lineterminator="\n")
    accounts.to_csv(HERE / "accounts.csv", index=False, encoding="utf-8",
                    lineterminator="\n")
    referral.to_csv(HERE / "fact_referral.csv", index=False, encoding="utf-8",
                    lineterminator="\n")

    digest = hashlib.sha256()
    for path in (HERE / "accounts.csv", HERE / "fact_referral.csv",
                 HERE / "fact_source_a.csv", HERE / "fact_source_b.csv"):
        digest.update(path.read_bytes())
    version = digest.hexdigest()[:12]
    (HERE / "ground_truth.json").write_text(
        json.dumps(_ground_truth(version), indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print(f"data written; version={version}; rows a={len(source_a)} "
          f"b={len(source_b)} referral={len(referral)} accounts={len(accounts)}")


if __name__ == "__main__":
    main()
