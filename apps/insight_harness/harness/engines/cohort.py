"""Deterministic top-HCP versus governed matched-peer comparison engine.

The engine is deliberately directional: it describes activity-mix gaps after
matching; it does not attribute high NRx share to those activities.  Selection,
matching, floors, incomplete referral coverage, and every recipe version are
carried in the artifact and therefore in its reproducibility hash.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

import pandas as pd

from .. import referrals, semantic_layer as sl
from ..provenance import AnswerArtifact, TIER_ABSTAINED, TIER_DIRECTIONAL, _stable_hash


COHORT_RECIPE_VERSION = "top_nrx_share_matched_peers_v1"


@dataclass(frozen=True)
class CohortRecipe:
    version: str = COHORT_RECIPE_VERSION
    top_n: int = 20
    min_nrx_ttm: float = 24.0
    min_market_nrx_ttm: float = 120.0
    activity_months: int = 3
    decile_bands: tuple[tuple[int, int], ...] = ((1, 5), (6, 10))
    exact_match_fields: tuple[str, ...] = ("region", "specialty", "decile_band")
    distance_metric: str = "market_nrx_ttm"
    without_replacement: bool = True

    def __post_init__(self) -> None:
        if self.top_n < 1 or self.top_n > 100:
            raise ValueError("top_n must be between 1 and 100")
        if self.min_nrx_ttm < 0 or self.min_market_nrx_ttm <= 0:
            raise ValueError("cohort floors must be non-negative with a positive denominator")
        if self.activity_months < 1 or self.activity_months > 12:
            raise ValueError("activity_months must be between 1 and 12")


DEFAULT_RECIPE = CohortRecipe()


def recipe_fingerprint(recipe: CohortRecipe = DEFAULT_RECIPE) -> str:
    return _stable_hash(asdict(recipe))


def _decile_band(decile: int, recipe: CohortRecipe) -> str:
    for lower, upper in recipe.decile_bands:
        if lower <= int(decile) <= upper:
            return f"{lower}-{upper}"
    raise ValueError(f"decile {decile} is outside the governed bands")


def _eligible_accounts(filters: Mapping, recipe: CohortRecipe) -> pd.DataFrame:
    accounts = sl.apply_filters(sl.load_accounts(), dict(filters)).copy()
    required = {
        "account_id", "npi", "name", "region", "specialty", "decile",
        "trx_ttm", "nrx_ttm", "market_nrx_ttm", "nrx_share_ttm",
    }
    missing = required - set(accounts.columns)
    if missing:
        raise ValueError("cohort inputs unavailable: " + ", ".join(sorted(missing)))
    accounts = accounts[
        (accounts["nrx_ttm"] >= recipe.min_nrx_ttm)
        & (accounts["market_nrx_ttm"] >= recipe.min_market_nrx_ttm)
        & accounts["nrx_share_ttm"].notna()
    ].copy()
    accounts["decile_band"] = accounts["decile"].map(
        lambda value: _decile_band(int(value), recipe))
    return accounts


def _select_top(eligible: pd.DataFrame, recipe: CohortRecipe) -> pd.DataFrame:
    out = eligible.sort_values(
        ["nrx_share_ttm", "nrx_ttm", "account_id"],
        ascending=[False, False, True], kind="mergesort",
    ).head(recipe.top_n).copy()
    out.insert(0, "rank", range(1, len(out) + 1))
    return out


def _match_peers(eligible: pd.DataFrame, selected: pd.DataFrame,
                 recipe: CohortRecipe) -> tuple[pd.DataFrame, list[str]]:
    selected_ids = set(selected["account_id"])
    pool = eligible[~eligible["account_id"].isin(selected_ids)].copy()
    used: set[str] = set()
    rows: list[dict] = []
    unmatched: list[str] = []

    for target in selected.sort_values("rank").itertuples(index=False):
        candidates = pool.copy()
        for field in recipe.exact_match_fields:
            candidates = candidates[candidates[field] == getattr(target, field)]
        if recipe.without_replacement:
            candidates = candidates[~candidates["account_id"].isin(used)]
        if candidates.empty:
            unmatched.append(str(target.account_id))
            continue
        candidates = candidates.assign(
            _distance=(candidates[recipe.distance_metric] -
                       float(getattr(target, recipe.distance_metric))).abs()
        ).sort_values(["_distance", "account_id"], kind="mergesort")
        peer = candidates.iloc[0]
        used.add(str(peer["account_id"]))
        rows.append({
            "top_rank": int(target.rank),
            "top_account_id": str(target.account_id),
            "top_npi": str(target.npi),
            "peer_account_id": str(peer["account_id"]),
            "peer_npi": str(peer["npi"]),
            "region": str(target.region),
            "specialty": str(target.specialty),
            "decile_band": str(target.decile_band),
            "top_nrx_share_ttm": float(target.nrx_share_ttm),
            "peer_nrx_share_ttm": float(peer["nrx_share_ttm"]),
            "top_market_nrx_ttm": float(target.market_nrx_ttm),
            "peer_market_nrx_ttm": float(peer["market_nrx_ttm"]),
            "match_distance": float(peer["_distance"]),
        })
    return pd.DataFrame(rows), unmatched


def _activity_frame(account_ids: list[str], months: list[str]) -> pd.DataFrame:
    frame = sl.load_fact("source_a")
    frame = frame[
        frame["account_id"].isin(account_ids) & frame["month"].isin(months)
    ]
    activity = frame.groupby("account_id", as_index=False).agg(
        calls=("calls", "sum"),
        call_plan=("call_plan", "sum"),
        samples=("samples", "sum"),
        speaker_attendance=("speaker_attendance", "sum"),
        nrx=("nrx", "sum"),
    )
    referral = referrals.account_activity(account_ids=account_ids, months=months)[
        ["account_id", "referral_covered", "referrals_in", "active_referrers"]
    ]
    return activity.merge(referral, how="left", on="account_id").sort_values(
        "account_id").reset_index(drop=True)


def _safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else float("nan")


def _profile(activity: pd.DataFrame, ids: list[str]) -> dict[str, tuple[float, int]]:
    cohort = activity[activity["account_id"].isin(ids)]
    n_hcps = int(cohort["account_id"].nunique())
    covered = cohort[cohort["referral_covered"].fillna(False)]
    n_covered = int(covered["account_id"].nunique())
    return {
        "calls_per_hcp_90d": (_safe_ratio(cohort["calls"].sum(), n_hcps), n_hcps),
        "samples_per_hcp_90d": (_safe_ratio(cohort["samples"].sum(), n_hcps), n_hcps),
        "speaker_attendance_per_hcp_90d": (
            _safe_ratio(cohort["speaker_attendance"].sum(), n_hcps), n_hcps),
        "call_attainment": (
            _safe_ratio(cohort["calls"].sum(), cohort["call_plan"].sum()), n_hcps),
        "referrals_in_per_covered_hcp_90d": (
            _safe_ratio(covered["referrals_in"].sum(), n_covered), n_covered),
        "active_referrers_per_covered_hcp_90d": (
            _safe_ratio(covered["active_referrers"].sum(), n_covered), n_covered),
    }


_ACTIVITY_LABELS = {
    "calls_per_hcp_90d": "Details per HCP · R3M",
    "samples_per_hcp_90d": "Samples per HCP · R3M",
    "speaker_attendance_per_hcp_90d": "Speaker attendance per HCP · R3M",
    "call_attainment": "Call-plan attainment · R3M",
    "referrals_in_per_covered_hcp_90d": "Incoming referrals per covered HCP · R3M",
    "active_referrers_per_covered_hcp_90d": "Active referrers per covered HCP · R3M",
}


def _comparison_table(top_profile: dict, peer_profile: dict) -> pd.DataFrame:
    rows = []
    for metric, label in _ACTIVITY_LABELS.items():
        top_value, top_n = top_profile[metric]
        peer_value, peer_n = peer_profile[metric]
        gap = top_value - peer_value
        relative_gap = gap / abs(peer_value) if pd.notna(peer_value) and peer_value else float("nan")
        rows.append({
            "metric": metric, "label": label,
            "top_hcps": float(top_value), "matched_peers": float(peer_value),
            "absolute_gap": float(gap), "relative_gap": float(relative_gap),
            "top_observed_n": int(top_n), "peer_observed_n": int(peer_n),
            "value_format": "percent" if metric == "call_attainment" else "number",
        })
    out = pd.DataFrame(rows)
    out["_rank"] = out["relative_gap"].abs().fillna(out["absolute_gap"].abs())
    out = out.sort_values(["_rank", "metric"], ascending=[False, True],
                          kind="mergesort").drop(columns="_rank").reset_index(drop=True)
    out.insert(0, "gap_rank", range(1, len(out) + 1))
    return out


def compare_top_hcps(filters: Mapping | None = None,
                     recipe: CohortRecipe = DEFAULT_RECIPE,
                     question: str | None = None) -> AnswerArtifact:
    """Build a reproducible, Directional top-share versus peer artifact."""
    scope = dict(filters or {})
    eligible = _eligible_accounts(scope, recipe)
    question = question or \
        "How does the activity mix of top NRx-share HCPs compare with matched peers?"
    if eligible.empty:
        artifact = AnswerArtifact(
            question, "Cohort comparison", TIER_ABSTAINED, "cohort",
            headline=("Declined: no HCPs meet the governed NRx and market-denominator "
                      f"floors in {sl.scope_string(scope)}."),
        )
        artifact.data_version = sl.data_version()
        artifact.extras["recipe"] = asdict(recipe)
        return artifact

    selected = _select_top(eligible, recipe)
    pairs, unmatched = _match_peers(eligible, selected, recipe)
    if pairs.empty:
        artifact = AnswerArtifact(
            question, "Cohort comparison", TIER_ABSTAINED, "cohort",
            headline="Declined: no governed exact-match peers are available in this scope.",
        )
        artifact.data_version = sl.data_version()
        artifact.extras.update({"recipe": asdict(recipe), "unmatched": unmatched})
        return artifact

    matched_top_ids = pairs["top_account_id"].tolist()
    peer_ids = pairs["peer_account_id"].tolist()
    months = sl.months("source_a")[-recipe.activity_months:]
    activity = _activity_frame(matched_top_ids + peer_ids, months)
    table = _comparison_table(
        _profile(activity, matched_top_ids), _profile(activity, peer_ids))
    chart = table.melt(
        id_vars=["gap_rank", "metric", "label", "value_format"],
        value_vars=["top_hcps", "matched_peers"],
        var_name="cohort", value_name="value",
    ).sort_values(["gap_rank", "cohort"]).reset_index(drop=True)

    selected_columns = [
        "rank", "account_id", "npi", "name", "region", "specialty", "decile",
        "decile_band", "nrx_ttm", "market_nrx_ttm", "nrx_share_ttm",
    ]
    selected_table = selected[selected_columns].reset_index(drop=True)
    recipe_hash = recipe_fingerprint(recipe)
    input_hash = _stable_hash({
        "data_version": sl.data_version(), "recipe_hash": recipe_hash,
        "scope": scope, "eligible_accounts": eligible[
            ["account_id", "region", "specialty", "decile_band", "nrx_ttm",
             "market_nrx_ttm", "nrx_share_ttm"]
        ].sort_values("account_id").to_dict(orient="records"),
        "activity_months": months,
    })
    top_referral_n = int(table.loc[
        table["metric"] == "referrals_in_per_covered_hcp_90d", "top_observed_n"
    ].iloc[0])
    peer_referral_n = int(table.loc[
        table["metric"] == "referrals_in_per_covered_hcp_90d", "peer_observed_n"
    ].iloc[0])
    cohort_referral_caveat = (
        f"Referral activity is observed for {top_referral_n}/{len(matched_top_ids)} "
        f"matched top HCPs and {peer_referral_n}/{len(peer_ids)} peers; uncovered "
        "HCPs are excluded from referral-rate denominators, not treated as zero."
    )
    governed_resolution = sl.resolve("nrx", source="source_a", variant="units")
    governed_resolution.reason = (
        "governed cohort recipe selects on source-A trailing NRx share and "
        "matches peers exactly on region, specialty, and decile band; activity "
        "source details are disclosed in artifact extras"
    )
    governed_resolution.alternates = []
    artifact = AnswerArtifact(
        question, "Cohort comparison", TIER_DIRECTIONAL, "cohort",
        headline=(f"Top {len(selected)} HCPs by trailing-12-month NRx share; "
                  f"{len(pairs)} exact governed peer matches. Largest activity gaps "
                  "are directional, not causal."),
        table=table, chart_df=chart,
        resolution=governed_resolution,
        code=(
            "eligible = accounts[(nrx_ttm >= min_nrx_ttm) & "
            "(market_nrx_ttm >= min_market_nrx_ttm)]\n"
            "top = stable_sort(eligible, nrx_share_ttm DESC, nrx_ttm DESC, account_id).head(top_n)\n"
            "peers = exact_match(top, region, specialty, decile_band; "
            "distance=market_nrx_ttm; without_replacement=True)\n"
            "profile = aggregate_last_3_months(top, peers)"
        ),
    )
    artifact.caveats.extend([
        "Directional, correlational comparison only. Selecting on NRx share and then comparing activity cannot establish that activity caused share.",
        (f"Selection floors: NRx TTM ≥ {recipe.min_nrx_ttm:g} and market NRx TTM ≥ "
         f"{recipe.min_market_nrx_ttm:g}; top {recipe.top_n} requested."),
        ("Peers match exactly on region, specialty, and governed decile band, then "
         "minimize market-NRx opportunity distance without replacement."),
        cohort_referral_caveat,
    ])
    if unmatched:
        artifact.caveats.append(
            f"{len(unmatched)} selected HCP(s) had no unused exact-match peer and are "
            "excluded from the activity comparison; IDs are disclosed in extras.")
    artifact.extras.update({
        "recipe": asdict(recipe),
        "recipe_hash": recipe_hash,
        "input_hash": input_hash,
        "selection": selected_table,
        "peer_matches": pairs,
        "unmatched_top_account_ids": unmatched,
        "selected_count": len(selected),
        "matched_count": len(pairs),
        "activity_months": months,
        "scope": scope,
        "source_contract": {
            "selection_metric": "nrx_share_ttm",
            "selection_source": "source_a",
            "activity_source": "source_a + referral",
        },
        "referral_coverage": {
            "top_observed": top_referral_n, "top_matched": len(matched_top_ids),
            "peer_observed": peer_referral_n, "peer_matched": len(peer_ids),
        },
    })
    artifact.data_version = sl.data_version()
    return artifact
