"""Observed-only referral metrics and computed completeness contracts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pandas as pd

from . import semantic_layer as sl


REFERRAL_SOURCE = "referral"
REFERRAL_METRICS = ("referrals_in", "active_referrers")


@dataclass(frozen=True)
class ReferralCoverage:
    observed_hcps: int
    eligible_hcps: int
    rate: float
    target: float
    projected: bool
    scope: dict

    @property
    def caveat(self) -> str:
        rate = f"{self.rate:.1%}" if pd.notna(self.rate) else "undefined"
        return (
            f"Referral feed completeness is {self.observed_hcps}/{self.eligible_hcps} "
            f"eligible HCPs ({rate}) in this scope. Metrics are observed-only, "
            "not projected; uncovered HCPs are unknown, not zero."
        )


def coverage(filters: Mapping | None = None) -> ReferralCoverage:
    report = sl.source_completeness(REFERRAL_SOURCE, dict(filters or {}))
    if report is None:  # pragma: no cover - registry contract guard
        raise RuntimeError("referral completeness contract is not registered")
    return ReferralCoverage(
        observed_hcps=report["observed"], eligible_hcps=report["expected"],
        rate=report["coverage"], target=report["target"],
        projected=report["projected"], scope=report["filters"],
    )


def account_activity(account_ids: Sequence[str] | None = None,
                     months: Sequence[str] | None = None,
                     filters: Mapping | None = None) -> pd.DataFrame:
    """Return HCP referral activity without converting uncovered rows to zero."""
    scope = dict(filters or {})
    accounts = sl.apply_filters(sl.load_accounts(), scope).copy()
    if account_ids is not None:
        requested = set(account_ids)
        accounts = accounts[accounts["account_id"].isin(requested)]
    fact = sl.apply_filters(sl.load_fact(REFERRAL_SOURCE), scope).copy()
    if months is not None:
        fact = fact[fact["month"].isin(list(months))]
    if account_ids is not None:
        fact = fact[fact["account_id"].isin(set(account_ids))]

    covered = set(fact["account_id"].unique())
    aggregate = fact.groupby("account_id", as_index=False)[list(REFERRAL_METRICS)].sum()
    columns = ["account_id", "npi", "name", "territory", "district", "region",
               "specialty", "payer_channel"]
    out = accounts[columns].merge(aggregate, how="left", on="account_id")
    out["referral_covered"] = out["account_id"].isin(covered)
    # Covered zeroes are observed zero. Uncovered values deliberately remain NaN.
    for metric in REFERRAL_METRICS:
        out.loc[out["referral_covered"] & out[metric].isna(), metric] = 0.0
    return out.sort_values("account_id").reset_index(drop=True)
