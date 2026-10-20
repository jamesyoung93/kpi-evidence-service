"""Cross-cutting services: divergence detection, caveat blocks, monitoring, telemetry."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import semantic_layer as sl
from .provenance import AnswerArtifact
from .runtime_policy import retain_raw_questions

FEEDBACK_LOG = Path(__file__).parent.parent / "data" / "feedback_log.jsonl"

# canonical wording per metric for machine-composed questions (drill-through,
# refusal reframes); every composed question must round-trip the parser
PRIMARY_KEYWORD = {"trx": "TRx", "nrx": "NRx",
                   "nbrx": "NBRx", "trx_share": "TRx market share",
                   "calls": "details", "call_plan": "call plan",
                   "call_attainment": "call-plan attainment",
                   "samples": "samples", "speaker_attendance": "speaker attendance",
                   "new_writers": "new writers", "referrals_in": "incoming referrals",
                   "active_referrers": "active referrers"}


def breakdown_question(metric: str, filters: dict, window_n: int | None = None,
                       basis: str | None = None) -> str:
    """The canonical diagnostic question for a monitored scope. Drill-through
    asks a real question so the step lands in history and reproduces. Optional
    tile controls stay in the question text, keeping navigation reproducible."""
    kw = PRIMARY_KEYWORD.get(metric, "TRx")
    target = next((d for d in ("specialty", "payer_channel", "territory", "district",
                               "region") if d not in filters),
                  "specialty")
    target_label = {"specialty": "specialties", "payer_channel": "payer channels",
                    "territory": "territories", "district": "districts",
                    "region": "regions"}[target]
    values: list = []
    for v in filters.values():
        values.extend(v if isinstance(v, (list, tuple)) else [v])
    where = f" in {' and '.join(values)}" if values else ""
    window = ""
    if window_n:
        unit = "month" if window_n == 1 else "months"
        window = f" over the last {window_n} {unit}"
    basis_text = {
        "prior_month": " vs prior month",
        "prior_quarter": " vs prior quarter",
        "yoy": " vs same month last year",
    }.get(basis, "")
    return f"Which {target_label} account for the {kw} change{where}{window}{basis_text}?"


# --------------------------------------------------------------------------- #
# Divergence: would the alternate source/variant move this answer materially?
# --------------------------------------------------------------------------- #
_BASIS_STEPS = {"prior_month": 1, "prior_quarter": 3, "yoy": 12}


def _record_coverage_gap(art: AnswerArtifact, source: str, requested: list[str],
                         common: list[str]) -> None:
    record = {
        "source": source,
        "source_label": sl.SOURCES[source]["name"],
        "requested_months": requested,
        "common_months": common,
        "reason": "reporting calendars differ; source divergence uses the common window",
    }
    gaps = art.extras.setdefault("coverage_gaps", [])
    if record not in gaps:
        gaps.append(record)


def check_divergence(art: AnswerArtifact, intent) -> None:
    """Recompute the artifact's own quantity under each registered alternate;
    flag material forks. The alternate must be computed the same way the
    answer was (level vs level, delta vs delta, same window) — a fork that
    can't be recomputed like-for-like is skipped, never approximated."""
    res = art.resolution
    if art.value is None or res is None or not res.alternates:
        return
    if art.engine not in ("descriptive", "decomposition"):
        # causal designs disclose variant forks via their computed sensitivity
        return
    base = art.value
    window = getattr(intent, "window", None)
    for kind, alt_id in res.alternates:
        source = alt_id if kind == "source" else res.source
        variant = alt_id if kind == "variant" else res.variant
        try:
            alternate_df = sl.apply_filters(sl.load_fact(source), intent.filters)
            if alternate_df.empty:
                continue
            alternate_months = sorted(alternate_df["month"].unique())
            common_window: list[str] = []
            note = ""

            if kind == "source":
                base_df = sl.apply_filters(sl.load_fact(res.source), intent.filters)
                base_months = sorted(base_df["month"].unique())
                common_calendar = sorted(set(base_months) & set(alternate_months))
                if not common_calendar:
                    continue

                if art.engine == "decomposition":
                    requested_end = art.extras["m1"]
                    eligible = [month for month in common_calendar if month <= requested_end]
                    if not eligible:
                        continue
                    common_end = eligible[-1]
                    steps = _BASIS_STEPS.get(intent.compare_basis or "prior_quarter", 3)
                    common_start = str(pd.Period(common_end, freq="M") - steps)
                    if common_start not in common_calendar:
                        continue
                    common_window = [common_start, common_end]
                    base_common = (
                        sl.aggregate_metric(base_df[base_df["month"] == common_end],
                                            res.metric, res.variant)
                        - sl.aggregate_metric(base_df[base_df["month"] == common_start],
                                              res.metric, res.variant))
                    alt_val = (
                        sl.aggregate_metric(alternate_df[alternate_df["month"] == common_end],
                                            res.metric, variant)
                        - sl.aggregate_metric(alternate_df[alternate_df["month"] == common_start],
                                              res.metric, variant))
                    requested = [art.extras["m0"], requested_end]
                    if common_window != requested:
                        _record_coverage_gap(art, source, requested, common_window)
                        note = f"common comparison window {common_start}–{common_end}"
                elif window and not intent.trend:
                    requested = list(window.months)
                    common_window = [month for month in requested if month in common_calendar]
                    if not common_window:
                        continue
                    base_common = sl.aggregate_metric(
                        base_df[base_df["month"].isin(common_window)], res.metric, res.variant)
                    alt_val = sl.aggregate_metric(
                        alternate_df[alternate_df["month"].isin(common_window)],
                        res.metric, variant)
                    if common_window != requested:
                        _record_coverage_gap(art, source, requested, common_window)
                        note = (f"common window {common_window[0]}–{common_window[-1]} "
                                "(coverage gap disclosed separately)")
                else:
                    requested_target = (window.months[-1] if window else base_months[-1])
                    eligible = [month for month in common_calendar if month <= requested_target]
                    if not eligible:
                        continue
                    target = eligible[-1]
                    common_window = [target]
                    base_common = sl.aggregate_metric(
                        base_df[base_df["month"] == target], res.metric, res.variant)
                    alt_val = sl.aggregate_metric(
                        alternate_df[alternate_df["month"] == target], res.metric, variant)
                    if target != requested_target:
                        _record_coverage_gap(art, source, [requested_target], [target])
                        note = (f"common month {target} "
                                "(coverage gap disclosed separately)")
                comparison_base = base_common
            else:
                # Variant forks share one source calendar, so the artifact's
                # exact period remains like-for-like.
                if art.engine == "decomposition":
                    m0, m1 = art.extras["m0"], art.extras["m1"]
                    if m0 not in alternate_months or m1 not in alternate_months:
                        continue
                    common_window = [m0, m1]
                    alt_val = (
                        sl.aggregate_metric(alternate_df[alternate_df["month"] == m1],
                                            res.metric, variant)
                        - sl.aggregate_metric(alternate_df[alternate_df["month"] == m0],
                                              res.metric, variant))
                elif window and not intent.trend:
                    common_window = [month for month in window.months
                                     if month in set(alternate_months)]
                    if not common_window:
                        continue
                    alt_val = sl.aggregate_metric(
                        alternate_df[alternate_df["month"].isin(common_window)],
                        res.metric, variant)
                else:
                    target = window.months[-1] if window else alternate_months[-1]
                    if target not in alternate_months:
                        eligible = [month for month in alternate_months if month <= target]
                        if not eligible:
                            continue
                        target = eligible[-1]
                    common_window = [target]
                    alt_val = sl.aggregate_metric(
                        alternate_df[alternate_df["month"] == target], res.metric, variant)
                comparison_base = base
        except (KeyError, IndexError, OSError, TypeError, ValueError):
            continue
        if pd.isna(comparison_base) or pd.isna(alt_val) or comparison_base == 0:
            continue
        rel = (alt_val - comparison_base) / comparison_base
        art.divergence.append({
            "fork": f"{kind}: {alt_id}",
            "label": (sl.SOURCES[alt_id]["name"] if kind == "source"
                      else sl.METRICS[res.metric]["variants"][alt_id]["label"]),
            "value": alt_val, "base_value": comparison_base,
            "common_window": common_window, "rel_diff": rel,
            "material": abs(rel) > sl.materiality(), "note": note,
        })


# --------------------------------------------------------------------------- #
# Caveats: built from registry metadata, never from model musing
# --------------------------------------------------------------------------- #
def build_caveats(art: AnswerArtifact, filters: dict | None = None) -> None:
    res = art.resolution
    if res is None:
        return
    src = sl.SOURCES[res.source]
    # Registered limitations are authoritative. Dropping a limitation by list
    # position can hide the exact pathology relevant to an older time window.
    art.caveats.extend(src["notes"] if src["kind"] == "panel-projected" else [])
    completeness = sl.source_completeness(res.source, filters)
    if completeness is not None:
        coverage = completeness["coverage"]
        coverage_text = f"{coverage:.1%}" if pd.notna(coverage) else "undefined"
        art.caveats.extend(src["notes"])
        art.caveats.append(
            f"Computed scope completeness: {completeness['observed']}/"
            f"{completeness['expected']} eligible HCPs ({coverage_text}). "
            "Values are observed-only and are not projected; uncovered HCPs "
            "are unknown, not zero."
        )
        art.extras["source_completeness"] = completeness
    variants = sl.METRICS[res.metric]["variants"]
    art.caveats.extend(sl.METRICS[res.metric].get("coverage_notes", []))
    if len(variants) > 1:
        others = [v["label"] for k, v in variants.items() if k != res.variant]
        art.caveats.append(f"Named variants exist for this metric ({', '.join(others)}); "
                           f"answer shown uses the governed default unless overridden.")
    if any(d["material"] for d in art.divergence):
        art.caveats.append("A materially different answer exists under an alternate "
                           "source/definition — see 'Same question, different answer'.")
    if art.extras.get("coverage_gaps"):
        art.caveats.append(
            "Source calendars differ. Coverage is disclosed separately, and source "
            "divergence is computed only on the common period.")


# --------------------------------------------------------------------------- #
# Watchlists: user-pinned metric+scope, evaluated with the same materiality
# logic as the anomaly feed
# --------------------------------------------------------------------------- #
WATCHLIST_PATH = Path(__file__).parent.parent / "data" / "watchlist.json"


def load_watchlist() -> list:
    try:
        data = json.loads(WATCHLIST_PATH.read_text())
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def add_watch(metric: str, filters: dict, label: str) -> bool:
    """Pin a metric+scope. Returns False when it is already watched."""
    watches = load_watchlist()
    if any(w.get("metric") == metric and w.get("filters") == filters for w in watches):
        return False
    watches.append({"metric": metric, "filters": filters, "label": label,
                    "added": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    WATCHLIST_PATH.parent.mkdir(exist_ok=True)
    WATCHLIST_PATH.write_text(json.dumps(watches, indent=2))
    return True


def remove_watch(metric: str, filters: dict) -> None:
    """Remove by identity (same equality add_watch dedupes on), so a stale or
    concurrently-modified list can never make Remove hit the wrong entry."""
    watches = [w for w in load_watchlist()
               if not (w.get("metric") == metric and w.get("filters") == filters)]
    WATCHLIST_PATH.parent.mkdir(exist_ok=True)
    WATCHLIST_PATH.write_text(json.dumps(watches, indent=2))


def watch_feed(watches: list, z_threshold: float = 2.0) -> pd.DataFrame:
    """Evaluate each watched scope: latest month vs its trailing mean, flagged
    with the same z logic the anomaly feed uses."""
    rows = []
    for w in watches:
        metric = w.get("metric")
        filters = w.get("filters", {})
        if metric not in sl.METRICS:
            # keep the row visible so the watch can still be removed
            rows.append({"label": w.get("label") or str(metric), "metric": str(metric),
                         "scope": sl.scope_string(filters), "latest": None,
                         "trailing_mean": None, "z": None, "impact": None, "flagged": False,
                         "status": "unregistered", "metric_id": metric, "filters": filters})
            continue
        resolution = sl.resolve(metric, w.get("source"), w.get("variant"))
        df = sl.apply_filters(sl.load_fact(resolution.source), filters)
        s = sl.monthly_metric(df, metric, resolution.variant).dropna() if len(df) else pd.Series(dtype=float)
        scope = sl.scope_string(filters)
        label = w.get("label") or f"{sl.METRICS[metric]['label']} · {scope}"
        if len(s) < 5:
            rows.append({"label": label, "metric": sl.METRICS[metric]["label"], "scope": scope,
                         "latest": float(s.iloc[-1]) if len(s) else None, "trailing_mean": None,
                         "z": None, "impact": None, "flagged": False,
                         "status": "insufficient_history" if len(s) else "no_data",
                         "metric_id": metric, "filters": filters,
                         "source": resolution.source, "variant": resolution.variant})
            continue
        hist = s.iloc[-7:-1]
        mu, sd = hist.mean(), hist.std()
        z = (s.iloc[-1] - mu) / sd if sd else 0.0
        rows.append({"label": label, "metric": sl.METRICS[metric]["label"], "scope": scope,
                     "latest": round(float(s.iloc[-1]), 1), "trailing_mean": round(float(mu), 1),
                     "z": round(float(z), 2), "impact": round(abs(float(s.iloc[-1] - mu)), 1),
                     "flagged": bool(abs(z) >= z_threshold),
                     "status": "flagged" if abs(z) >= z_threshold else "ok",
                     "metric_id": metric, "filters": filters,
                     "source": resolution.source, "variant": resolution.variant})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Monitoring: impact-ranked anomaly feed (materiality filter, not p-value spam)
# --------------------------------------------------------------------------- #
PRIORITY_SCORE_VERSION = "v2"
PRIORITY_SCORE_WEIGHTS = {"standardized": 0.45, "relative": 0.20, "business_scale": 0.35}
PRIORITY_Z_SATURATION = 4.0
PRIORITY_RELATIVE_SATURATION = 0.50
PRIORITY_SCALE_SATURATION = 0.10
LOW_BASE_TRAILING_MEAN = 5.0
LOW_BASE_RATIO_DENOMINATOR = 25.0
ANOMALY_CLUSTER_OVERLAP = 0.80
PRIORITY_SCORE_FORMULA = (
    "0.45 × min(|z| / 4, 1) + 0.20 × min(|relative movement| / 50%, 1) "
    "+ 0.35 × min((|business movement| / national monthly volume) / 10%, 1)"
)

# These are registered numerator/derived-metric relationships, not learned
# correlations.  A movement in details and the corresponding attainment ratio
# is one commercial story, so it is surfaced once with the derived view attached.
CORRELATED_METRIC_GROUPS = (
    frozenset({"calls", "call_attainment"}),
)


def priority_components_v2(*, z: float, relative_change: float,
                           business_delta: float, national_monthly_volume: float,
                           low_base: bool = False) -> dict[str, float | bool]:
    """Return the bounded, disclosed components of priority score v2.

    ``business_delta`` is a native-unit movement for additive metrics.  For a
    ratio it is the implied numerator movement (ratio-point change multiplied
    by the scoped denominator).  The scale reference is the corresponding
    national monthly volume.  Relative and scale terms are deliberately zeroed
    on low bases; a tiny count can still surface on standardized movement, but
    a large percentage cannot make it look commercially large.
    """

    z_term = min(abs(float(z)) / PRIORITY_Z_SATURATION, 1.0)
    relative_term = 0.0 if low_base else min(
        abs(float(relative_change)) / PRIORITY_RELATIVE_SATURATION, 1.0)
    reference = abs(float(national_monthly_volume))
    scale_share = abs(float(business_delta)) / reference if reference else 0.0
    scale_term = 0.0 if low_base else min(
        scale_share / PRIORITY_SCALE_SATURATION, 1.0)
    score = (
        PRIORITY_SCORE_WEIGHTS["standardized"] * z_term
        + PRIORITY_SCORE_WEIGHTS["relative"] * relative_term
        + PRIORITY_SCORE_WEIGHTS["business_scale"] * scale_term
    )
    return {
        "standardized": round(z_term, 8),
        "relative": round(relative_term, 8),
        "business_scale": round(scale_term, 8),
        "business_scale_share": round(scale_share, 8),
        "low_base_guard": bool(low_base),
        "score": round(score, 8),
    }


def priority_score_v2(*, z: float, relative_change: float,
                      business_delta: float, national_monthly_volume: float,
                      low_base: bool = False) -> float:
    """Convenience wrapper returning only the final [0, 1] priority score."""

    return float(priority_components_v2(
        z=z, relative_change=relative_change, business_delta=business_delta,
        national_monthly_volume=national_monthly_volume, low_base=low_base,
    )["score"])


def _ratio_denominator(frame: pd.DataFrame, metric: str, variant: str,
                       months: list[str]) -> float:
    definition = sl.METRICS[metric]["variants"][variant]
    denominator = definition.get("denominator")
    if not denominator or denominator not in frame.columns:
        return 0.0
    selected = frame[frame["month"].isin(months)]
    monthly = selected.groupby("month", sort=True)[denominator].sum()
    return float(monthly.mean()) if len(monthly) else 0.0


def _same_story_metrics(left: str, right: str) -> bool:
    if left == right:
        return True
    return any(left in group and right in group for group in CORRELATED_METRIC_GROUPS)


def _row_overlap(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _same_signal_signature(left: dict, right: dict) -> bool:
    """Catch the same aggregate movement projected through two dimensions.

    Cross-dimensional scopes need not contain exactly the same accounts (an
    account belongs to both a region and a payer channel), but two flags with
    the same metric and identical latest/baseline/movement facts are still one
    executive story.  Tight numeric tolerances keep this deterministic and
    avoid grouping merely similar movements.
    """

    if left["metric_id"] != right["metric_id"]:
        return False
    return all(abs(float(left[key]) - float(right[key])) <= 1e-9 for key in (
        "latest", "trailing_mean", "native_delta", "z"))


def _cluster_anomaly_rows(rows: list[dict]) -> list[dict]:
    """Collapse duplicate scopes and registered derived-metric pairs.

    Clustering is a deterministic connected-components pass over the actual
    account sets behind each flag.  Only identical metrics or explicitly
    registered metric relationships can connect, preventing coincident
    movements in unrelated measures from being merged into one story.
    """

    if not rows:
        return []
    parents = list(range(len(rows)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parents[max(a, b)] = min(a, b)

    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            if not _same_story_metrics(rows[left]["metric_id"], rows[right]["metric_id"]):
                continue
            if (_row_overlap(rows[left]["_row_ids"], rows[right]["_row_ids"])
                    >= ANOMALY_CLUSTER_OVERLAP
                    or _same_signal_signature(rows[left], rows[right])):
                union(left, right)

    groups: dict[int, list[int]] = {}
    for index in range(len(rows)):
        groups.setdefault(find(index), []).append(index)

    clustered: list[dict] = []
    for indices in groups.values():
        calls = [index for index in indices if rows[index]["metric_id"] == "calls"]
        eligible = calls or indices  # details lead their derived attainment story
        representative_index = sorted(
            eligible,
            key=lambda index: (-rows[index]["impact_score"], -abs(rows[index]["z"]),
                               rows[index]["metric_id"], rows[index]["scope"]),
        )[0]
        representative = dict(rows[representative_index])
        related = [rows[index] for index in sorted(
            indices,
            key=lambda index: (rows[index]["metric"], rows[index]["scope"],
                               rows[index]["metric_id"]),
        ) if index != representative_index]
        representative["cluster_size"] = len(indices)
        representative["also_visible_as"] = tuple(
            f"{row['metric']} · {row['scope']}" for row in related)
        representative["related_signals"] = tuple({
            key: row[key] for key in (
                "metric", "metric_id", "scope", "latest", "trailing_mean",
                "native_delta", "relative_change", "z", "value_format",
            )
        } for row in related)
        cluster_members = sorted(
            f"{rows[index]['metric_id']}|{rows[index]['scope']}" for index in indices)
        representative["cluster_id"] = hashlib.sha256(
            "||".join(cluster_members).encode("utf-8")).hexdigest()[:12]
        representative.pop("_row_ids", None)
        clustered.append(representative)
    return sorted(clustered, key=lambda row: (
        -row["impact_score"], -abs(row["z"]), row["metric_id"], row["scope"]))


def anomaly_feed(z_threshold: float = 2.0) -> pd.DataFrame:
    rows = []
    metrics = ("trx", "nrx", "nbrx", "trx_share", "calls", "call_plan",
               "call_attainment", "samples", "speaker_attendance", "new_writers",
               "referrals_in", "active_referrers")
    for metric in metrics:
        source = sl.METRICS[metric]["default_source"]
        df = sl.load_fact(source)
        latest = sorted(df["month"].unique())[-1]
        variant = sl.default_variant(metric)
        for dim in ("region", "specialty", "payer_channel"):
            for val in sorted(df[dim].unique()):
                scoped = df[df[dim] == val]
                s = sl.monthly_metric(scoped, metric, variant).dropna()
                hist = s.iloc[-7:-1]
                if len(hist) < 4:
                    continue
                mu, sd = hist.mean(), hist.std()
                z = (s.iloc[-1] - mu) / sd if sd else 0.0
                if abs(z) >= z_threshold:
                    latest_value = float(s.iloc[-1])
                    trailing_mean = float(mu)
                    native_delta = latest_value - trailing_mean
                    relative_change = native_delta / abs(trailing_mean) if trailing_mean else 0.0
                    value_format = sl.METRICS[metric]["variants"][variant].get(
                        "format", "number")
                    history_months = [str(month) for month in s.index[-7:-1]]
                    metric_kind = sl.METRICS[metric].get("kind")
                    if metric_kind == "ratio":
                        scoped_denominator = _ratio_denominator(
                            scoped, metric, variant, history_months)
                        national_volume = _ratio_denominator(
                            df, metric, variant, history_months)
                        business_delta = abs(native_delta) * abs(scoped_denominator)
                        low_base = scoped_denominator < LOW_BASE_RATIO_DENOMINATOR
                        low_base_reason = (
                            f"trailing denominator {scoped_denominator:,.1f} is below "
                            f"{LOW_BASE_RATIO_DENOMINATOR:,.0f}"
                            if low_base else ""
                        )
                    else:
                        national_series = sl.monthly_metric(df, metric, variant).dropna()
                        national_history = national_series[
                            national_series.index.astype(str).isin(history_months)]
                        national_volume = float(national_history.mean()) \
                            if len(national_history) else 0.0
                        business_delta = abs(native_delta)
                        low_base = value_format == "number" and \
                            abs(trailing_mean) < LOW_BASE_TRAILING_MEAN
                        low_base_reason = (
                            f"trailing mean {trailing_mean:,.1f} is below "
                            f"{LOW_BASE_TRAILING_MEAN:,.0f}"
                            if low_base else ""
                        )
                    components = priority_components_v2(
                        z=float(z), relative_change=relative_change,
                        business_delta=business_delta,
                        national_monthly_volume=national_volume,
                        low_base=low_base,
                    )
                    history_values = [float(value) for value in hist]
                    row_ids = frozenset(
                        str(value) for value in (
                            scoped["account_id"].dropna().unique()
                            if "account_id" in scoped.columns else scoped.index
                        )
                    )
                    rows.append({"month": latest, "metric": sl.METRICS[metric]["label"],
                                 "scope": f"{dim}={val}", "latest": latest_value,
                                 "trailing_mean": trailing_mean, "z": round(float(z), 2),
                                 "impact": abs(native_delta),
                                 "native_delta": native_delta,
                                 "relative_change": relative_change,
                                 "impact_score": components["score"],
                                 "priority_version": PRIORITY_SCORE_VERSION,
                                 "priority_components": components,
                                 "business_delta": business_delta,
                                 "national_monthly_volume": national_volume,
                                 "low_base": low_base,
                                 "low_base_reason": low_base_reason,
                                 "typical_min": min(history_values),
                                 "typical_max": max(history_values),
                                 "value_format": value_format,
                                 "direction": "up" if native_delta > 0 else
                                              "down" if native_delta < 0 else "flat",
                                 "status": "flagged",
                                 "metric_id": metric, "dim": dim, "value": val,
                                 "_row_ids": row_ids})
    out = pd.DataFrame(_cluster_anomaly_rows(rows))
    return out.sort_values(["impact_score", "z"], ascending=[False, False]) \
        .reset_index(drop=True) if len(out) else out


# --------------------------------------------------------------------------- #
# Telemetry: corrections are the evidence base for scope expansion
# --------------------------------------------------------------------------- #
_FEEDBACK_LOCK = threading.RLock()
_PROCESS_TELEMETRY_HASH_KEY = secrets.token_bytes(32)


def _question_hash(question: str) -> str:
    configured = os.environ.get("INSIGHT_HARNESS_TELEMETRY_HASH_KEY")
    key = configured.encode("utf-8") if configured else _PROCESS_TELEMETRY_HASH_KEY
    return hmac.new(key, question.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def log_feedback(art: AnswerArtifact, verdict: str, note: str = "") -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec = {"ts": ts, "question_hash": _question_hash(art.question),
           "class": art.question_class, "tier": art.tier,
           "engine": art.engine, "result_hash": art.result_hash,
           "data_version": art.data_version, "verdict": verdict, "note": note}
    if retain_raw_questions():
        rec["question"] = art.question
    with _FEEDBACK_LOCK, sl._path_lock(FEEDBACK_LOG):
        FEEDBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FEEDBACK_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return ts


def log_review(art: AnswerArtifact) -> str:
    """Analyst sign-off on a Hypothesis-tier design is provenance too."""
    return log_feedback(art, "analyst_reviewed")


def feedback_history() -> pd.DataFrame:
    if not FEEDBACK_LOG.exists():
        return pd.DataFrame(columns=["ts", "question_hash", "class", "tier", "verdict", "note"])
    rows = []
    for line in FEEDBACK_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # one truncated line must not take down the record
    return pd.DataFrame(rows)
