"""Semantic layer: the governed foundation every answer is composed from.

One machine-readable definition per metric, with *named variants* as first-class
citizens, source registry with known limitations, and deterministic resolution
rules. The AI never improvises a definition; it resolves one, and discloses it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "data"
CONFIG_PATH = DATA_DIR / "governance_config.json"
GOVERNANCE_LOG = DATA_DIR / "governance_log.jsonl"
_AUDIT_HISTORY_LIMIT = 200

# --------------------------------------------------------------------------- #
# Source registry
# --------------------------------------------------------------------------- #
SOURCES = {
    "source_a": {
        "name": "Direct/DDD + specialty pharmacy feed",
        "kind": "transactional",
        "cadence": "monthly",
        "lag_months": 0,
        "account_grain": True,
        "grain": ["account_id", "month"],
        "notes": ["System of record at HCP-by-month grain for monthly synthetic data.",
                  "Details, call plan, samples, programs, and writer activity are collected here only."],
    },
    "source_b": {
        "name": "Projected retail panel",
        "kind": "panel-projected",
        "cadence": "monthly",
        "lag_months": 1,
        "account_grain": False,
        "grain": ["month", "territory", "district", "region", "specialty",
                  "payer_channel"],
        "notes": ["Panel-projected: known small multiplicative bias vs. direct/DDD feeds.",
                  "Latest month unavailable (1-month reporting lag).",
                  "Early history restated by the vendor."],
    },
    "referral": {
        "name": "Referral relationship feed",
        "kind": "observed-incomplete",
        "cadence": "monthly",
        "lag_months": 0,
        "account_grain": True,
        "grain": ["account_id", "month"],
        "completeness": {
            "entity": "account_id",
            "eligible_source": "accounts",
            "target": 0.80,
            "projected": False,
        },
        "notes": [
            "Observed-only receiving-HCP referral feed; it is not projected to full coverage.",
            "A zero is observed only for covered HCP-months; uncovered HCPs are unknown, not zero.",
            "Active referrers are de-duplicated within receiving HCP-month before aggregation.",
        ],
    },
}

# --------------------------------------------------------------------------- #
# Metric registry — variants are first-class, each with an owner and notes
# --------------------------------------------------------------------------- #
METRICS = {
    "trx": {
        "label": "TRx", "family": "prescriptions", "digest_family": "rx_volume",
        "kind": "additive", "additive": True,
        "sources": ["source_a", "source_b"], "default_source": "source_a",
        "default_variant": "units",
        "variants": {
            "units": {"column": "trx_units", "label": "TRx units",
                      "comparison_group": "volume", "format": "number",
                      "owner": "Commercial Analytics",
                      "notes": "Total prescriptions in unit equivalents."},
            "dollars": {"column": "trx_dollars", "label": "TRx dollars",
                        "comparison_group": "currency", "format": "currency",
                        "owner": "Finance",
                        "notes": "Gross prescription value before rebates."},
            "normalized": {"column": "trx_normalized",
                           "label": "TRx normalized equivalents",
                           "comparison_group": "volume", "format": "number",
                           "owner": "Commercial Analytics",
                           "notes": "Payer-channel normalized equivalent units."},
        },
    },
    "nrx": {
        "label": "NRx", "family": "new_prescriptions", "digest_family": "rx_volume",
        "kind": "additive", "additive": True,
        "sources": ["source_a", "source_b"], "default_source": "source_a",
        "default_variant": "units",
        "variants": {"units": {"column": "nrx", "label": "NRx", "format": "number",
                                "owner": "Brand Analytics",
                                "notes": "New prescriptions in the month."}},
    },
    "nbrx": {
        "label": "NBRx", "family": "new_to_brand", "digest_family": "rx_volume",
        "kind": "additive", "additive": True,
        "sources": ["source_a", "source_b"], "default_source": "source_a",
        "default_variant": "units",
        "variants": {"units": {"column": "nbrx", "label": "NBRx", "format": "number",
                                "owner": "Brand Analytics",
                                "notes": "New-to-brand prescriptions in the month."}},
    },
    "trx_share": {
        "label": "TRx market share", "family": "share", "kind": "ratio", "additive": False,
        "sources": ["source_a", "source_b"], "default_source": "source_a",
        "default_variant": "brand_market",
        "basket_registry": "market_baskets_v1",
        "variants": {
            # Backward-compatible governed broad-market definition. New
            # basket-aware surfaces resolve to one of the named variants below.
            "brand_market": {"numerator": "trx_units", "denominator": "market_trx",
                             "label": "TRx market share", "format": "percent",
                             "owner": "Brand Analytics", "basket": "advanced_therapy",
                             "comparison_group": "share",
                             "notes": "Brand TRx divided by the broad advanced-therapy market; descriptive only."},
            "il17_class": {"numerator": "trx_units", "denominator": "il17_class_trx",
                           "label": "TRx share · IL-17 class", "format": "percent",
                           "owner": "Brand Analytics", "basket": "il17_class",
                           "comparison_group": "share",
                           "notes": "Brand TRx divided by governed IL-17 class TRx."},
            "advanced_therapy": {
                "numerator": "trx_units", "denominator": "advanced_therapy_trx",
                "label": "TRx share · advanced therapy", "format": "percent",
                "owner": "Brand Analytics", "basket": "advanced_therapy",
                "comparison_group": "share",
                "notes": "Brand TRx divided by the governed broader advanced-therapy basket.",
            },
        },
    },
    "calls": {
        "label": "Details",
        "family": "field_effort", "kind": "additive",
        "additive": True,
        "sources": ["source_a"],
        "default_source": "source_a",
        "default_variant": "std",
        "variants": {"std": {"column": "calls", "label": "Details delivered",
                             "format": "number", "owner": "Sales Operations",
                             "notes": "Internally collected; uniform definition across regions."}},
    },
    "call_plan": {
        "label": "Call plan", "family": "field_effort", "kind": "additive",
        "additive": True, "sources": ["source_a"], "default_source": "source_a",
        "default_variant": "planned",
        "variants": {"planned": {"column": "call_plan", "label": "Planned details",
                                  "format": "number", "owner": "Sales Operations",
                                  "notes": "Governed monthly HCP detail plan."}},
    },
    "call_attainment": {
        "label": "Call-plan attainment", "family": "field_effort", "kind": "ratio",
        "additive": False, "sources": ["source_a"], "default_source": "source_a",
        "default_variant": "actual_plan",
        "variants": {"actual_plan": {"numerator": "calls", "denominator": "call_plan",
                                     "label": "Call-plan attainment", "format": "percent",
                                     "owner": "Sales Operations",
                                     "notes": "Details delivered divided by planned details."}},
    },
    "samples": {
        "label": "Samples dropped", "family": "field_effort", "kind": "additive", "additive": True,
        "sources": ["source_a"], "default_source": "source_a", "default_variant": "units",
        "variants": {"units": {"column": "samples", "label": "Sample units",
                                "format": "number", "owner": "Sales Operations",
                                "notes": "Recorded sample units."}},
    },
    "speaker_attendance": {
        "label": "Speaker attendance", "family": "programs", "kind": "additive", "additive": True,
        "sources": ["source_a"], "default_source": "source_a", "default_variant": "attendees",
        "variants": {"attendees": {"column": "speaker_attendance",
                                    "label": "Speaker-program attendance", "format": "number",
                                    "owner": "Marketing Operations",
                                    "notes": "Recorded HCP attendance."}},
    },
    "new_writers": {
        "label": "New writers", "family": "writers", "kind": "additive", "additive": True,
        "sources": ["source_a"], "default_source": "source_a", "default_variant": "strict",
        "coverage_notes": [
            "The first measured month is a non-comparable warm-up and remains undefined; new-writer status requires a prior observed month."
        ],
        "variants": {"strict": {"column": "new_writers", "label": "New writers",
                                 "format": "number", "owner": "Brand Analytics",
                                 "notes": "First observed brand prescription in the measured history."}},
    },
    "referrals_in": {
        "label": "Incoming referrals", "family": "referrals",
        "digest_family": "patient_flow", "kind": "additive", "additive": True,
        "monitoring_eligible": True,
        "sources": ["referral"], "default_source": "referral",
        "default_variant": "observed",
        "variants": {
            "observed": {"column": "referrals_in", "label": "Observed incoming referrals",
                         "format": "number", "owner": "Market Access Analytics",
                         "notes": "Observed incoming referral count; not projected for uncovered HCPs."},
        },
    },
    "active_referrers": {
        "label": "Active referrers", "family": "referrals",
        "digest_family": "patient_flow", "kind": "additive", "additive": True,
        "monitoring_eligible": True,
        "sources": ["referral"], "default_source": "referral",
        "default_variant": "observed",
        "variants": {
            "observed": {"column": "active_referrers", "label": "Observed active referrers",
                         "format": "number", "owner": "Market Access Analytics",
                         "notes": "Distinct active referrers within covered receiving-HCP/month cells; observed only."},
        },
    },
}

DIMENSIONS = ["territory", "district", "region", "specialty", "payer_channel"]
MATERIALITY_REL = 0.02  # divergence above 2% is surfaced (registry default)


@contextmanager
def _path_lock(path: Path, timeout_seconds: float = 5.0):
    """Small cross-process lock for governance read/modify/write sequences."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    descriptor = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30.0:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out acquiring governance lock: {lock_path}")
            time.sleep(0.01)
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Governance configuration — admin-set overrides of registry defaults.
# Every change is logged: governance changes are provenance too.
# --------------------------------------------------------------------------- #
def _governance_config() -> dict:
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _governance_settings(cfg: dict) -> dict:
    """Return public settings only; audit records never recursively contain history."""
    return json.loads(json.dumps({k: v for k, v in cfg.items()
                                  if k != "_audit_history"}))


def _atomic_write_config(cfg: dict) -> None:
    """Durably write the config payload before atomically publishing it."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + f".{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, CONFIG_PATH)
        # Best-effort directory sync where the platform permits opening one.
        try:
            descriptor = os.open(CONFIG_PATH.parent,
                                 os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            descriptor = None
        if descriptor is not None:
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        tmp_path.unlink(missing_ok=True)


def materiality() -> float:
    v = _governance_config().get("materiality_rel")
    return float(v) if isinstance(v, (int, float)) and 0.005 <= v <= 0.20 else MATERIALITY_REL


def default_variant(metric: str) -> str:
    v = _governance_config().get("default_variants", {}).get(metric)
    return v if v in METRICS[metric]["variants"] else METRICS[metric]["default_variant"]


def set_governance(materiality_rel: float | None = None,
                   default_variants: dict | None = None,
                   actor: str | None = None) -> dict:
    """Apply and log a governance change; returns what actually changed."""
    with _path_lock(CONFIG_PATH):
        cfg = _governance_config()
        before = _governance_settings(cfg)
        change: dict = {}
        current_materiality = cfg.get("materiality_rel", MATERIALITY_REL)
        if not isinstance(current_materiality, (int, float)):
            current_materiality = MATERIALITY_REL
        if materiality_rel is not None and 0.005 <= materiality_rel <= 0.20 \
                and float(materiality_rel) != float(current_materiality):
            cfg["materiality_rel"] = float(materiality_rel)
            change["materiality_rel"] = float(materiality_rel)
        configured_variants = cfg.setdefault("default_variants", {})
        for metric, variant in (default_variants or {}).items():
            current = configured_variants.get(metric, METRICS.get(metric, {}).get("default_variant"))
            if metric in METRICS and variant in METRICS[metric]["variants"] and variant != current:
                configured_variants[metric] = variant
                change.setdefault("default_variants", {})[metric] = variant
        if not configured_variants:
            cfg.pop("default_variants", None)
        if not change:
            return change

        after = _governance_settings(cfg)
        rec = {
            "change_id": uuid.uuid4().hex,
            "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "actor": actor or "unspecified",
            "before": before,
            "after": after,
            "change": change,
        }
        history = cfg.get("_audit_history", [])
        if not isinstance(history, list):
            history = []
        cfg["_audit_history"] = [item for item in history if isinstance(item, dict)]
        cfg["_audit_history"].append(rec)
        cfg["_audit_history"] = cfg["_audit_history"][-_AUDIT_HISTORY_LIMIT:]

        # The embedded record and settings become visible in one atomic replace,
        # so a crash can never publish an unaudited governance change.
        _atomic_write_config(cfg)

        # Keep JSONL for append-friendly operations/export. It is a mirror: the
        # embedded bounded history above remains authoritative if this append is
        # interrupted or the mirror is lost.
        try:
            GOVERNANCE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with GOVERNANCE_LOG.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(rec, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass
        return change


def governance_log() -> list[dict]:
    """Merge the export mirror with authoritative audit records in config."""
    candidates: list[dict] = []
    try:
        lines = GOVERNANCE_LOG.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                candidates.append(record)
        except json.JSONDecodeError:
            continue
    embedded = _governance_config().get("_audit_history", [])
    if isinstance(embedded, list):
        candidates.extend(record for record in embedded if isinstance(record, dict))

    out: list[dict] = []
    seen: set[str] = set()
    for record in candidates:
        key = str(record.get("change_id") or json.dumps(record, sort_keys=True))
        if key not in seen:
            seen.add(key)
            out.append(record)
    # Python's stable sort preserves append order for legacy second-resolution
    # records that share a timestamp; new records use microsecond resolution.
    return sorted(out, key=lambda record: str(record.get("ts", "")))

# keyword -> metric mapping used by the built-in parser and by the
# language-model translator's registry context
METRIC_KEYWORDS = {
    "trx market share": "trx_share", "market share": "trx_share", "share": "trx_share",
    "total prescriptions": "trx", "total scripts": "trx", "prescriptions": "trx",
    "rx volume": "trx", "trx": "trx", "scripts": "trx",
    "new-to-brand": "nbrx", "new to brand": "nbrx", "nbrx": "nbrx",
    "new prescriptions": "nrx", "nrx": "nrx",
    "call plan attainment": "call_attainment", "call-plan attainment": "call_attainment",
    "attainment": "call_attainment",
    "planned calls": "call_plan", "call plan": "call_plan",
    "details": "calls", "detail calls": "calls", "calls": "calls",
    "samples": "samples", "sample units": "samples",
    "speaker attendance": "speaker_attendance", "speaker programs": "speaker_attendance",
    "new writers": "new_writers", "writers": "new_writers",
    "active referrers": "active_referrers", "referring hcps": "active_referrers",
    "incoming referrals": "referrals_in", "referrals in": "referrals_in",
    "referrals": "referrals_in",
}

# --------------------------------------------------------------------------- #
# Event registry — feeds the causal design advisor
# --------------------------------------------------------------------------- #
EVENTS = {
    "speaker_launch": {
        "name": "Speaker-program launch (East territory cluster)", "start": "2025-10",
        "scope": {"territory": ["E-CAR-01", "E-END-01"]},
        "control_scope": {"territory": ["N-CAR-01", "N-END-01"]},
        "metrics": ["trx", "nrx", "nbrx"], "default_metric": "trx",
        "keywords": ["east", "speaker", "speaker program", "program launch"],
        "notes": "Two registered East territories with matched North-territory controls.",
    },
    "formulary_win": {
        "name": "Medicare Part D formulary win (South)", "start": "2026-01",
        "scope": {"region": "South", "payer_channel": "Medicare Part D"},
        "control_scope": {"region": ["North", "East"],
                          "payer_channel": "Medicare Part D"},
        "metrics": ["trx", "nrx"], "default_metric": "trx",
        "keywords": ["south", "formulary", "medicare", "payer win"],
        "notes": "South Medicare Part D only; controls retain the same payer channel.",
    },
    "competitor_launch": {
        "name": "Competitor launch (West / Cardiology)", "start": "2026-04",
        "scope": {"region": "West", "specialty": "Cardiology"},
        "control_scope": {"region": ["North", "South", "East"],
                          "specialty": "Cardiology"},
        "metrics": ["trx", "nrx", "nbrx"], "default_metric": "trx",
        "keywords": ["west", "competitor", "cardiology", "competitor launch"],
        "notes": "Short post-period; controls retain Cardiology and exclude West.",
    },
}


@dataclass
class Resolution:
    """The disclosed outcome of source/variant resolution for a question."""
    metric: str
    variant: str
    source: str
    reason: str
    alternates: list = field(default_factory=list)  # (kind, id) pairs


def resolve(metric: str, source: str | None = None, variant: str | None = None) -> Resolution:
    m = METRICS[metric]
    # overrides apply only when registered for this metric; anything else clamps
    # to the governed default and the clamp is disclosed in the resolution reason
    source_ok = source in m["sources"] if source else False
    variant_ok = variant in m["variants"] if variant else False
    chosen_source = source if source_ok else m["default_source"]
    chosen_variant = variant if variant_ok else default_variant(metric)
    chosen_group = m["variants"][chosen_variant].get("comparison_group")
    alternates = [("variant", v) for v, spec in m["variants"].items()
                  if v != chosen_variant
                  and (chosen_group is None or spec.get("comparison_group") == chosen_group)]
    alternates += [("source", s) for s in m["sources"] if s != chosen_source]
    clamped = [name for name, requested, ok in
               (("source", source, source_ok), ("variant", variant, variant_ok))
               if requested and not ok]
    if (source_ok or variant_ok) and not clamped:
        reason = "user override"
    elif source_ok or variant_ok:
        reason = (f"user override ({'source' if source_ok else 'variant'}); requested "
                  f"{' and '.join(clamped)} not registered for this metric — governed "
                  f"default used")
    elif clamped:
        reason = (f"requested {' and '.join(clamped)} not registered for this metric; "
                  f"governed default used ({m['variants'][chosen_variant]['owner']}-owned "
                  f"variant on {SOURCES[chosen_source]['name']})")
    else:
        reason = (f"default governed resolution ({m['variants'][chosen_variant]['owner']}-owned "
                  f"variant on {SOURCES[chosen_source]['name']})")
    return Resolution(metric, chosen_variant, chosen_source, reason, alternates)


@lru_cache(maxsize=8)
def load_fact(source: str) -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / f"fact_{source}.csv")


@lru_cache(maxsize=1)
def load_accounts() -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / "accounts.csv")


@lru_cache(maxsize=1)
def ground_truth() -> dict:
    return json.loads((DATA_DIR / "ground_truth.json").read_text())


@lru_cache(maxsize=1)
def data_version() -> str:
    h = hashlib.sha256()
    for f in sorted(DATA_DIR.glob("*.csv")):
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def months(source: str = "source_a") -> list[str]:
    return sorted(load_fact(source)["month"].unique())


def apply_filters(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """Filter values may be a scalar or a list of registered values."""
    for dim, val in filters.items():
        if dim in df.columns:
            df = df[df[dim].isin(val)] if isinstance(val, (list, tuple)) else df[df[dim] == val]
    return df


def source_completeness(source: str, filters: dict | None = None) -> dict | None:
    """Compute scoped entity coverage for an incomplete registered source.

    The numerator is observed entities, not non-zero rows: a covered HCP with
    an observed zero remains covered.  The denominator comes from the governed
    eligible account universe under the exact same scope.
    """
    contract = SOURCES[source].get("completeness")
    if not contract:
        return None
    scope = filters or {}
    eligible = apply_filters(load_accounts(), scope)
    observed = apply_filters(load_fact(source), scope)
    entity = contract["entity"]
    expected_count = int(eligible[entity].nunique()) if entity in eligible else 0
    observed_count = int(observed[entity].nunique()) if entity in observed else 0
    coverage = observed_count / expected_count if expected_count else float("nan")
    return {
        "source": source,
        "entity": entity,
        "observed": observed_count,
        "expected": expected_count,
        "coverage": coverage,
        "target": float(contract["target"]),
        "projected": bool(contract.get("projected", False)),
        "filters": dict(scope),
    }


def scope_string(filters: dict) -> str:
    parts = [f"{k} in [{', '.join(v)}]" if isinstance(v, (list, tuple)) else f"{k}={v}"
             for k, v in filters.items()]
    return ", ".join(parts) or "all scopes"


def column_for(metric: str, variant: str) -> str:
    spec = METRICS[metric]["variants"][variant]
    return spec.get("column") or spec["numerator"]


def metric_kind(metric: str) -> str:
    return METRICS[metric].get("kind", "additive")


def aggregate_metric(df: pd.DataFrame, metric: str, variant: str) -> float:
    """Aggregate additive and ratio metrics without ever summing percentages.

    Undefined ratios remain NaN.  Reporting zero would turn missing exposure
    into an observed zero rate, which is a materially different claim.
    """
    spec = METRICS[metric]["variants"][variant]
    if metric_kind(metric) == "ratio":
        numerator_values = df[spec["numerator"]]
        denominator_values = df[spec["denominator"]]
        if not numerator_values.notna().any() or not denominator_values.notna().any():
            return float("nan")
        denominator = float(denominator_values.sum(min_count=1))
        return float(numerator_values.sum(min_count=1)) / denominator \
            if denominator else float("nan")
    values = df[spec["column"]]
    return float(values.sum(min_count=1)) if values.notna().any() else float("nan")


def monthly_metric(df: pd.DataFrame, metric: str, variant: str) -> pd.Series:
    spec = METRICS[metric]["variants"][variant]
    if metric_kind(metric) == "ratio":
        numerator = df.groupby("month")[spec["numerator"]].sum(min_count=1)
        denominator = df.groupby("month")[spec["denominator"]].sum(min_count=1)
        return numerator.div(denominator.where(denominator != 0)).sort_index()
    return df.groupby("month")[spec["column"]].sum(min_count=1).sort_index()
