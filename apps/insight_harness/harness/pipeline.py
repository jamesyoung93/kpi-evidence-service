"""Pipeline orchestrator + evaluation harness.

pipeline.answer() is the single entry point: triage -> resolve -> engine ->
divergence -> caveats. The evaluation harness runs a golden question set with
ground truth computed through an INDEPENDENT code path, abstention tests
(refusing is a scored behavior), and reproducibility checks (same question
twice must hash identically).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import baskets, semantic_layer as sl
from . import services, tiles, triage
from .engines import basic, causal_advisor, cohort, decomposition
from .provenance import AnswerArtifact, TIER_ABSTAINED, _stable_hash

EVAL_HISTORY = Path(__file__).parent.parent / "data" / "eval_history.jsonl"
_EVAL_HISTORY_LOCK = threading.RLock()


def answer(question: str, source: str | None = None, variant: str | None = None,
           api_key: str | None = None, model: str | None = None,
           basis: str | None = None) -> AnswerArtifact:
    translation = {"translator": "rules", "validated": True}
    intent = None
    if api_key:
        from . import llm_translator
        t0 = time.perf_counter()
        try:
            intent, translation = llm_translator.translate(
                question, api_key, model or llm_translator.DEFAULT_MODEL)
        except llm_translator.TranslationError as e:
            # product-voice caption; the raw error stays in the artifact JSON for audit
            reason = ("The language model's translation didn't validate against the metric "
                      "registry, so the built-in parser answered this question."
                      if e.kind == "rejected" else
                      "The language model translator wasn't available for this question, "
                      "so the built-in parser answered it.")
            translation = {"translator": "rules", "validated": True,
                           "fallback_reason": reason, "fallback_detail": str(e),
                           "fallback_kind": e.kind}
        translation["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    if intent is None:
        intent = triage.parse(question)
    if basis and intent.question_class in (triage.DIAGNOSTIC, triage.DESCRIPTIVE):
        intent.compare_basis = basis
    return answer_intent(intent, source, variant, translation)


def answer_intent(intent: triage.Intent, source: str | None = None,
                  variant: str | None = None, translation: dict | None = None) -> AnswerArtifact:
    """Resolve → engine → divergence → caveats for an already-parsed intent.
    Every answer surface (Ask, drill-through, Causal Studio) goes through here,
    so every answer is a full artifact."""
    translation = translation or {"translator": "rules", "validated": True}

    if intent.question_class in (triage.OUT_OF_SCOPE, triage.PREDICTIVE) or (
            intent.question_class == triage.CAUSAL and intent.event_id is None):
        art = AnswerArtifact(intent.question, intent.question_class, TIER_ABSTAINED, "abstention",
                             headline="Declined: " + intent.reason)
        art.data_version = sl.data_version()
        art.extras["intent"] = intent
        art.extras["translation"] = translation
        art.extras["reframes"] = _reframes(intent)
        return art

    if intent.question_class == triage.DESCRIPTIVE \
            and intent.metric == "trx_share" and intent.basket_id:
        art = baskets.answer_basket_share(
            filters=intent.filters,
            months=intent.window.months if intent.window else None,
            basket_override=intent.basket_id,
            source=source or "source_a",
            question=intent.question,
        )
        if variant and variant != art.resolution.variant:
            art.caveats.append(
                f"Requested variant {variant!r} was not used because the question "
                f"explicitly selected governed basket {intent.basket_id!r}.")
        if art.tier != TIER_ABSTAINED:
            services.build_caveats(art, intent.filters)
        art.data_version = sl.data_version()
        art.extras["intent"] = intent
        art.extras["translation"] = translation
        return art

    if intent.question_class == triage.RETRIEVAL \
            and (intent.metric or "trx") in {"trx", "nrx", "nbrx"}:
        # Account templates are materialized from the reconciled source-A
        # account table.  Resolve to that exact capability regardless of Ask
        # overrides so the artifact can never claim panel or dollar provenance.
        res = sl.resolve(intent.metric or "trx", "source_a", "units")
        requested = []
        if source and source != "source_a":
            requested.append(f"source={source}")
        if variant and variant != "units":
            requested.append(f"variant={variant}")
        recipe_label = ("governed Top writers "
                        if intent.template == "top_writers" else "")
        res.reason = (
            f"{recipe_label}account-grain retrieval contract: source_a / units used"
            + (f"; requested override ({', '.join(requested)}) is not available "
               "for account retrieval" if requested else "")
        )
        res.alternates = []
    else:
        res = sl.resolve(intent.metric or "trx", source, variant)

    if intent.question_class == triage.COHORT:
        art = cohort.compare_top_hcps(intent.filters, question=intent.question)
    elif intent.question_class == triage.CAUSAL:
        art = causal_advisor.propose(intent, res)
    elif intent.question_class == triage.DIAGNOSTIC:
        art = decomposition.decompose(intent, res)
    elif intent.question_class == triage.RETRIEVAL:
        art = basic.retrieval(intent, res)
    else:
        art = basic.descriptive(intent, res)

    if art.tier != TIER_ABSTAINED:  # an engine may refuse (e.g. uncovered window)
        services.check_divergence(art, intent)
        services.build_caveats(art, intent.filters)
    art.data_version = sl.data_version()
    art.extras["intent"] = intent
    art.extras["translation"] = translation
    return art


def _reframes(intent: triage.Intent) -> list[str]:
    """Concrete re-askable questions offered with a scoped refusal. Each must
    round-trip through the parser to a supported intent."""
    kw = services.PRIMARY_KEYWORD.get(intent.metric or "trx", "TRx")
    if intent.question_class == triage.PREDICTIVE:
        return [f"Trend {kw} by month", f"Which regions account for the {kw} change?"]
    if intent.question_class == triage.CAUSAL:
        return [f"Which regions account for the {kw} change?", f"Trend {kw} by month"]
    return ["What is TRx in the West region?",
            "Which specialties account for the TRx change?"]


# --------------------------------------------------------------------------- #
# Pharma golden set — raw-column truth independent of production aggregators
# --------------------------------------------------------------------------- #
_VALUE_COLUMNS = {
    "trx": {"units": "trx_units", "dollars": "trx_dollars",
            "normalized": "trx_normalized"},
    "nrx": {"units": "nrx"}, "nbrx": {"units": "nbrx"},
    "calls": {"std": "calls"}, "call_plan": {"planned": "call_plan"},
    "samples": {"units": "samples"},
    "speaker_attendance": {"attendees": "speaker_attendance"},
    "new_writers": {"strict": "new_writers"},
    "referrals_in": {"observed": "referrals_in"},
    "active_referrers": {"observed": "active_referrers"},
}
_RATIO_COLUMNS = {"trx_share": {
                      "brand_market": ("trx_units", "market_trx"),
                      "il17_class": ("trx_units", "il17_class_trx"),
                      "advanced_therapy": ("trx_units", "advanced_therapy_trx"),
                  },
                  "call_attainment": {"actual_plan": ("calls", "call_plan")}}


def _q_months(year: int, quarter: int) -> list[str]:
    return [f"{year}-{month:02d}" for month in range(3 * quarter - 2,
                                                    3 * quarter + 1)]


def _independent_value(spec: dict, art: AnswerArtifact) -> float:
    frame = sl.load_fact(spec.get("source", "source_a")).copy()
    for dimension, value in spec.get("filters", {}).items():
        values = value if isinstance(value, (list, tuple)) else [value]
        frame = frame[frame[dimension].isin(values)]
    months = spec.get("months")
    if months is None:
        months = [frame["month"].max()]
    frame = frame[frame["month"].isin(months)]
    metric = spec["metric"]
    variant = spec.get("variant") or art.resolution.variant
    if metric in _RATIO_COLUMNS:
        numerator, denominator = _RATIO_COLUMNS[metric][variant]
        return float(frame[numerator].sum()) / float(frame[denominator].sum())
    return float(frame[_VALUE_COLUMNS[metric][variant]].sum())


def _source_contract() -> dict:
    truth = sl.ground_truth()["source_b_issues"]
    source_a, source_b = sl.load_fact("source_a"), sl.load_fact("source_b")
    ratios = {}
    for month in (truth["restated_months"][0], "2025-06"):
        for region, bias in truth["bias_by_region"].items():
            expected = bias * (truth["restatement_factor"]
                               if month in truth["restated_months"] else 1.0)
            for column in truth["affected_columns"]:
                base = source_a[(source_a["month"] == month)
                                & (source_a["region"] == region)][column].sum()
                panel = source_b[(source_b["month"] == month)
                                 & (source_b["region"] == region)][column].sum()
                ratios[f"{month}:{region}:{column}"] = {
                    "actual": float(panel / base), "expected": float(expected)}
    return {"latest_a": source_a["month"].max(), "latest_b": source_b["month"].max(),
            "ratios": ratios}


def _event_contract() -> dict:
    frame, truth = sl.load_fact("source_a"), sl.ground_truth()["events"]
    mismatches = {}
    for event_id, event in truth.items():
        expected = frame["month"] >= event["start"]
        for dimension, value in event["scope"].items():
            values = value if isinstance(value, list) else [value]
            expected &= frame[dimension].isin(values)
        actual = frame[event["treatment_flag"]].astype(bool)
        mismatches[event_id] = int((expected != actual).sum())
    return mismatches


def _data_contract() -> dict:
    frame, accounts = sl.load_fact("source_a"), sl.load_accounts()
    last12 = sl.months()[-12:]
    ttm = frame[frame["month"].isin(last12)].groupby("account_id")["trx_units"].sum()
    joined = accounts.set_index("account_id").join(ttm.rename("expected"))
    return {
        "duplicate_account_months": int(frame.duplicated(["account_id", "month"]).sum()),
        "account_ids_match": set(frame["account_id"]) == set(accounts["account_id"]),
        "max_ttm_error": float((joined["trx_ttm"] - joined["expected"]).abs().max()),
        "rx_order_violations": int(((frame["nbrx"] > frame["nrx"])
                                    | (frame["nrx"] > frame["trx_units"])).sum()),
        "market_violations": int((frame["market_trx"] < frame["trx_units"]).sum()),
    }


_GOLDEN_WATCH = [{"metric": "call_attainment", "filters": {"region": "West"},
                  "label": "Watched: West call-plan attainment"}]

GOLDEN = [
    {"id": "G01", "question": "What is TRx in the West region?", "type": "value",
     "metric": "trx", "filters": {"region": "West"}},
    {"id": "G02", "question": "What was NRx in Q1 2026 in Cardiology?", "type": "value",
     "metric": "nrx", "filters": {"specialty": "Cardiology"},
     "months": _q_months(2026, 1)},
    {"id": "G03", "question": "What was NBRx in June 2026 for Commercial?", "type": "value",
     "metric": "nbrx", "filters": {"payer_channel": "Commercial"},
     "months": ["2026-06"]},
    {"id": "G04", "question": "What is TRx in the North region?", "type": "value",
     "metric": "trx", "filters": {"region": "North"}, "variant": "dollars"},
    {"id": "G05", "question": "What is TRx in the East region?", "type": "value",
     "metric": "trx", "filters": {"region": "East"}, "variant": "normalized"},
    {"id": "G06", "question": "What is call plan in the South region?", "type": "value",
     "metric": "call_plan", "filters": {"region": "South"}},
    {"id": "G07", "question": "What is call-plan attainment in the East region?", "type": "value",
     "metric": "call_attainment", "filters": {"region": "East"}},
    {"id": "G08", "question": "What is TRx market share in West Cardiology?", "type": "value",
     "metric": "trx_share", "filters": {"region": "West", "specialty": "Cardiology"}},
    {"id": "G09", "question": "What is TRx in E-CAR-01?", "type": "value",
     "metric": "trx", "filters": {"territory": "E-CAR-01"}},
    {"id": "G10", "question": "What is TRx in North District 2?", "type": "value",
     "metric": "trx", "filters": {"district": "North District 2", "region": "North"}},
    {"id": "G11", "question": "Which payer channels account for the NRx change?", "type": "check",
     "check": lambda art: set(art.extras.get("tables", {})) == {"payer_channel"},
     "label": "requested pharma breakdown dimension is honored"},
    {"id": "G12", "question": "Which specialties account for the TRx market share change?",
     "type": "abstain"},
    {"id": "G13", "question": "What is TRx in E-CAR-01 and Endocrinology?",
     "type": "abstain"},
    {"id": "G14", "question": "List whitespace HCPs with no activity", "type": "check",
     "check": lambda art: art.table is not None and len(art.table) > 0
     and (art.table["decile"] >= 8).all() and (art.table["months_since_rx"] >= 3).all()
     and (art.table["months_since_activity"] >= 3).all() and (art.table["calls_90d"] == 0).all(),
     "label": "whitespace criteria hold row-by-row"},
    {"id": "G15", "question": "What was the impact of the speaker program?", "type": "causal",
     "event_id": "speaker_launch", "tolerance": 0.05},
    {"id": "G16", "question": "What was the impact of the formulary win in South Medicare?",
     "type": "causal", "event_id": "formulary_win", "tolerance": 0.05},
    {"id": "G17", "question": "What was the impact of the competitor launch in West Cardiology?",
     "type": "causal", "event_id": "competitor_launch", "tolerance": 0.06},
    {"id": "G18", "question": "What was TRx in Q2 2026?", "type": "divergence",
     "label": "source divergence uses a disclosed common window"},
    {"id": "G19", "question": "What was TRx in June 2026?", "type": "abstain",
     "source": "source_b"},
    {"id": "G20", "question": "Forecast TRx for next quarter", "type": "abstain"},
    {"id": "G21", "question": "What is our patient satisfaction score?", "type": "abstain"},
    {"id": "G22", "question": "Trend TRx last 6 months vs same month last year", "type": "check",
     "check": lambda art: art.chart_df is not None and len(art.chart_df) == 6
     and art.extras.get("comparison", {}).get("available") is True,
     "label": "TRx trend window and YoY reference are honored"},
    {"id": "G23", "question": "(watch) West call-plan attainment", "type": "watch",
     "check": lambda feed: len(feed) == 1 and feed.iloc[0]["metric"] == "Call-plan attainment",
     "label": "ratio watch is evaluated as a ratio, not a sum"},
    {"id": "G24", "question": "(contract) projected panel pathologies", "type": "contract",
     "probe": _source_contract,
     "check": lambda value: value["latest_a"] == "2026-06" and value["latest_b"] == "2026-05"
     and all(abs(pair["actual"] - pair["expected"]) < 1e-6
             for pair in value["ratios"].values()),
     "label": "panel bias, restatement, and lag match independent truth"},
    {"id": "G25", "question": "(contract) exact pharma event rows", "type": "contract",
     "probe": _event_contract, "check": lambda value: not any(value.values()),
     "label": "event scopes and treatment flags match exactly"},
    {"id": "G26", "question": "(contract) account grain and invariants", "type": "contract",
     "probe": _data_contract,
     "check": lambda value: value["duplicate_account_months"] == 0
     and value["account_ids_match"] and value["max_ttm_error"] < 0.0011
     and value["rx_order_violations"] == 0 and value["market_violations"] == 0,
     "label": "HCP grain reconciles and pharma invariants hold"},
    {"id": "G27", "question": "(tile) TRx · Latest · MoM", "type": "tile",
     "tile_id": "trx", "window": "Latest", "basis": "MoM", "region": tiles.ALL_REGIONS},
    {"id": "G28", "question": "(tile) NRx · R3M · YoY · South", "type": "tile",
     "tile_id": "nrx", "window": "R3M", "basis": "YoY", "region": "South"},
    {"id": "G29", "question": "(tile) TRx market share · R6M · QoQ", "type": "tile",
     "tile_id": "trx_share", "window": "R6M", "basis": "QoQ",
     "region": tiles.ALL_REGIONS},
    {"id": "G30", "question": "Top 15 accounts by NRx", "type": "check",
     "check": lambda art: art.resolution.metric == "nrx"
     and art.resolution.source == "source_a" and art.resolution.variant == "units"
     and art.table is not None and art.table["nrx_ttm"].is_monotonic_decreasing,
     "label": "NRx account retrieval ranks the NRx account field"},
    {"id": "G31", "question": "Top 15 accounts by TRx", "type": "check",
     "source": "source_b", "variant": "dollars",
     "check": lambda art: art.resolution.source == "source_a"
     and art.resolution.variant == "units" and "account-grain retrieval" in art.resolution.reason,
     "label": "account retrieval overrides clamp to truthful account-grain provenance"},
    {"id": "G32",
     "question": "Compare the activity mix of top 20 HCPs by NRx share with matched peers",
     "type": "check",
     "check": lambda art: art.tier == "Directional" and art.engine == "cohort"
     and art.extras.get("recipe", {}).get("version") == cohort.COHORT_RECIPE_VERSION
     and art.extras.get("matched_count", 0) > 0,
     "label": "money question routes to the governed deterministic cohort engine"},
]


def run_golden(record: bool = True) -> pd.DataFrame:
    rows = []
    for spec in GOLDEN:
        if spec["type"] == "tile":
            kwargs = {"window": spec["window"], "basis": spec["basis"],
                      "region": spec["region"]}
            question = tiles.canonical_question(spec["tile_id"], **kwargs)
            direct = answer_intent(tiles.intent_for(spec["tile_id"], **kwargs))
            asked, repeated = answer(question), answer(question)
            ok = (direct.result_hash == asked.result_hash
                  and direct.resolution == asked.resolution
                  and direct.data_version == asked.data_version == sl.data_version())
            rows.append({"id": spec["id"], "question": question, "class": "Tile parity",
                         "tier": direct.tier, "pass": bool(ok),
                         "reproducible": asked.result_hash == repeated.result_hash,
                         "detail": "tile and opened question share hash, resolution, and data"})
            continue
        if spec["type"] == "watch":
            first, second = services.watch_feed(_GOLDEN_WATCH), services.watch_feed(_GOLDEN_WATCH)
            rows.append({"id": spec["id"], "question": spec["question"],
                         "class": "Monitoring", "tier": "Verified",
                         "pass": bool(spec["check"](first)),
                         "reproducible": _stable_hash(first) == _stable_hash(second),
                         "detail": spec["label"]})
            continue
        if spec["type"] == "contract":
            first, second = spec["probe"](), spec["probe"]()
            rows.append({"id": spec["id"], "question": spec["question"],
                         "class": "Data contract", "tier": "Verified",
                         "pass": bool(spec["check"](first)),
                         "reproducible": _stable_hash(first) == _stable_hash(second),
                         "detail": spec["label"]})
            continue

        kwargs = {key: spec[key] for key in ("source", "variant") if key in spec}
        art1, art2 = answer(spec["question"], **kwargs), answer(spec["question"], **kwargs)
        reproducible = art1.result_hash == art2.result_hash
        if spec["type"] == "value":
            truth = _independent_value(spec, art1)
            error = abs(float(art1.value) - truth) if art1.value is not None else float("inf")
            ok = error <= max(1e-6, abs(truth) * 1e-9)
            detail = (f"expected {truth:,.6f}, got {art1.value:,.6f}"
                      if art1.value is not None else "no value")
        elif spec["type"] == "check":
            ok, detail = bool(spec["check"](art1)), spec["label"]
        elif spec["type"] == "causal":
            truth = sl.ground_truth()["events"][spec["event_id"]]["true_effect_pct"]
            ok = art1.value is not None and abs(art1.value - truth) <= spec["tolerance"]
            detail = (f"injected {truth * 100:+.1f}%, estimated "
                      f"{(art1.value or 0) * 100:+.1f}%")
        elif spec["type"] == "abstain":
            ok, detail = art1.tier == TIER_ABSTAINED, "correct behavior is a scoped refusal"
        elif spec["type"] == "divergence":
            source_forks = [fork for fork in art1.divergence
                            if fork["fork"].startswith("source")]
            ok = (bool(source_forks) and bool(art1.extras.get("coverage_gaps"))
                  and all(fork["common_window"] == ["2026-04", "2026-05"]
                          for fork in source_forks)
                  and all(fork["material"] == (abs(fork["rel_diff"]) > sl.materiality())
                          for fork in source_forks))
            detail = spec["label"]
        rows.append({"id": spec["id"], "question": spec["question"],
                     "class": art1.question_class, "tier": art1.tier,
                     "pass": bool(ok), "reproducible": reproducible, "detail": detail})
    result = pd.DataFrame(rows)
    if record:
        _record_run(result)
    return result


def _record_run(res: pd.DataFrame) -> None:
    refusals = res[res["tier"] == TIER_ABSTAINED]
    fb = services.feedback_history()
    votes = fb[fb["verdict"].isin(["correct", "wrong"])] if len(fb) else fb
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "data_version": sl.data_version(),
           "pass_rate": float(res["pass"].mean()),
           "reproducible_rate": float(res["reproducible"].mean()),
           "correct_refusal_rate": float(refusals["pass"].mean()) if len(refusals) else 1.0,
           "correction_rate": float((votes["verdict"] == "wrong").mean()) if len(votes) else 0.0,
           "n": int(len(res))}
    # One complete, durable JSONL record per evaluation, even when multiple
    # app workers or CLI processes finish a golden run at the same time.
    with _EVAL_HISTORY_LOCK, sl._path_lock(EVAL_HISTORY):
        EVAL_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with EVAL_HISTORY.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(rec, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def eval_history() -> pd.DataFrame:
    cols = ["ts", "data_version", "pass_rate", "reproducible_rate",
            "correct_refusal_rate", "correction_rate", "n"]
    if not EVAL_HISTORY.exists():
        return pd.DataFrame(columns=cols)
    rows = []
    for line in EVAL_HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
