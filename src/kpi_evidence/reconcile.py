from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date
from typing import Any

from reconloop import Policy, Target, reconcile
from reconloop import __version__ as RECONLOOP_VERSION
from reconloop.execution import precompute_fit
from reconloop.judge import LLMJudge, StatedRuleJudge
from reconloop.metricspec import MetricSpec

from .compile import WINDOW_CHECK, CompileError, CompiledKpi, compile_kpi, detect_stated_variants
from .contracts import (
    Document, DocumentStatus, ExtractedValue, ExtractionMethod, KpiMapping, KpiResult, MappingMethod,
    MappingStatus, MatchBasis, Observation, ReasonCode, ReconRun, ReconTrial, ValidationStatus, to_row,
)
from .ids import canonical_json, json_safe, sha256_text, stable_id, utcnow
from .registry import KpiDefinition, Registry
from .settings import Settings
from .store.base import AccessDenied, Store

GATE_CONFIDENCE = 0.6
SCALE_FACTORS = (100.0, 1_000.0, 1_000_000.0)
SCALE_BAND = 0.02
PHASE_B_POLICY = {"max_changes": 3}
CONFLICT_KEY = "conflict"
REUSED_KEY = "reused_from_run"
OUTSIDE_FOCUS = "outside this run's focus"
_NO_REUSE_STATUSES = {ValidationStatus.BLOCKED.value, ValidationStatus.MISSING_ACCESS.value}

_UNIT_ALIASES = {
    "percent": "fraction", "%": "fraction", "pct": "fraction", "share": "fraction", "ratio": "fraction",
    "currency": "usd", "$": "usd", "dollars": "usd", "dollar": "usd",
    "units": "count", "number": "count", "num": "count",
}
_UNKNOWN_UNITS = {"", "unknown", "none"}
_UNKNOWN_GRAINS = {None, "", "unknown"}


def _quiet(*_args: Any, **_kwargs: Any) -> None:
    return None


class TrialSink(list):
    """Shared list of compute() calls."""

    phase = "phase_a"

    def __deepcopy__(self, memo: dict) -> "TrialSink":
        return self


class RecordingSpec(MetricSpec):
    """A ``MetricSpec`` whose every ``compute`` call (value or error) is appended to ``self.sink``."""

    sink: TrialSink

    def compute(self, runner, choices: dict[str, str], as_of: str) -> tuple[float, dict]:
        entry = {"seq": len(self.sink), "phase": self.sink.phase, "choices": _full_choices(self, choices),
                 "as_of": as_of}
        try:
            value, components = super().compute(runner, choices, as_of)
        except Exception as exc:
            self.sink.append({**entry, "value": None, "error": f"{type(exc).__name__}: {exc}",
                              "sql": render_sql(self, choices, as_of)})
            raise
        self.sink.append({**entry, "value": value, "error": None, "sql": render_sql(self, choices, as_of)})
        return value, components


def recording(spec: MetricSpec) -> RecordingSpec:
    """Copy ``spec`` into a ``RecordingSpec`` with an empty sink."""
    rspec = RecordingSpec(**{f.name: getattr(spec, f.name) for f in fields(MetricSpec)})
    rspec.sink = TrialSink()
    return rspec


def _full_choices(spec: MetricSpec, choices: dict[str, str]) -> dict[str, str]:
    try:
        return spec.initial_choices(choices)
    except ValueError:
        return dict(choices)


def render_sql(spec: MetricSpec, choices: dict[str, str], as_of: str) -> str | None:
    """The SQL actually executed for one evaluation (both components for a ratio); None if unrenderable."""
    try:
        parts = spec.render_components(choices, as_of)
    except Exception:
        return None
    if "value" in parts:
        return parts["value"]
    return f"-- numerator\n{parts['numerator']};\n-- denominator\n{parts['denominator']}"


def read_session(store: Store):
    """A block of platform reads on one connection (``store.platform_access.session()``)."""
    access = getattr(store, "platform_access", None)
    return access.session() if access is not None else store.transaction()


def normalize_unit(unit: str | None) -> str | None:
    """Canonical unit name (``%`` -> fraction, ``$`` -> usd); None when unknown."""
    u = (unit or "").strip().lower()
    u = _UNIT_ALIASES.get(u, u)
    return None if u in _UNKNOWN_UNITS else u


def flag_codes(flags: list | None) -> set[str]:
    return {str(f.get("code")) for f in (flags or []) if isinstance(f, dict) and f.get("code")}


def allowed_gap(kpi: KpiDefinition, gold: float, half_unit: float) -> float:
    """Absolute agreement band: ``max(rel*|g|, abs, half_unit)`` from the registry and the observation."""
    half = half_unit if kpi.tolerance.use_display_rounding else 0.0
    return max(kpi.tolerance.rel * abs(gold), kpi.tolerance.abs, max(half or 0.0, 0.0))


def within(kpi: KpiDefinition, gold: float, computed: float | None, half_unit: float) -> bool:
    if computed is None or not math.isfinite(computed) or not math.isfinite(gold):
        return False
    slack = 1e-12 * max(1.0, abs(gold))
    return abs(computed - gold) <= allowed_gap(kpi, gold, half_unit) + slack


def match_basis(gold: float, computed: float, half_unit: float) -> MatchBasis:
    gap = abs(computed - gold)
    if gap <= 1e-9 * max(1.0, abs(gold)):
        return MatchBasis.EXACT
    if gap <= (half_unit or 0.0):
        return MatchBasis.WITHIN_ROUNDING
    return MatchBasis.WITHIN_TOLERANCE


def scale_factor(platform_value: float, report_value: float) -> float | None:
    """The power-of-ten factor platform/report sits near (100, 1000, 1e6 or inverses), else None."""
    if not report_value or not platform_value:
        return None
    ratio = abs(platform_value / report_value)
    for f in SCALE_FACTORS:
        for target in (f, 1.0 / f):
            if abs(ratio / target - 1.0) <= SCALE_BAND:
                return target
    return None


def fmt_value(value: float | None, unit: str | None) -> str:
    """Plain display: 2,476 / 5,783.04 / 84.42% / $1,200."""
    if value is None:
        return "n/a"
    if normalize_unit(unit) == "fraction":
        return _trim(f"{value * 100:,.2f}") + "%"
    text = _trim(f"{value:,.2f}")
    return f"${text}" if normalize_unit(unit) == "usd" else text


def _trim(text: str) -> str:
    return text.rstrip("0").rstrip(".") if "." in text else text


def tolerance_text(kpi: KpiDefinition, gold: float, half_unit: float) -> str:
    rel_part = kpi.tolerance.rel * abs(gold)
    half = half_unit if kpi.tolerance.use_display_rounding else 0.0
    applied = allowed_gap(kpi, gold, half_unit)
    if kpi.tolerance.rel > 0 and applied == rel_part:
        return _trim(f"{kpi.tolerance.rel * 100:.2f}") + "%"
    if half and applied == half:
        return f"±{fmt_value(half, kpi.unit)} display rounding"
    return f"±{fmt_value(kpi.tolerance.abs, kpi.unit)}"


def tolerance_record(kpi: KpiDefinition | None, gold: float | None, half_unit: float) -> dict[str, Any]:
    if kpi is None:
        return {"rel": None, "abs": None, "half_unit": half_unit, "applied": None}
    applied = allowed_gap(kpi, gold, half_unit) if gold is not None and math.isfinite(gold) else None
    return {"rel": kpi.tolerance.rel, "abs": kpi.tolerance.abs, "half_unit": half_unit, "applied": applied,
            "use_display_rounding": kpi.tolerance.use_display_rounding}


def effective_status(result: KpiResult) -> str:
    """The verdict before a conflict overlay (REPORT_CONFLICT keeps the reconciliation verdict it replaced)."""
    if result.validation_status == ValidationStatus.REPORT_CONFLICT:
        prev = ((result.explanation or {}).get(CONFLICT_KEY) or {}).get("previous") or {}
        if prev.get("validation_status"):
            return str(prev["validation_status"])
    return result.validation_status.value


def mapping_kpis(mapping: KpiMapping | None) -> set[str]:
    """KPI ids a mapping names: the mapped KPI and every candidate."""
    if mapping is None:
        return set()
    out = {str(mapping.kpi_id)} if mapping.kpi_id else set()
    out |= {str(c["kpi_id"]) for c in (mapping.candidates or []) if isinstance(c, dict) and c.get("kpi_id")}
    return out


def in_focus(mapping: KpiMapping | None, focus: Any) -> bool:
    """True without a focus, else when the mapped KPI or any candidate KPI is in the focus set."""
    return focus is None or bool(mapping_kpis(mapping) & set(focus))


def kpi_priority(kpi: Any) -> int | None:
    """The KPI table priority of a KPI (1 = highest), None when not given."""
    value = (getattr(kpi, "raw", None) or {}).get("priority") if kpi is not None else None
    try:
        return None if value is None or value != value else int(value)
    except (TypeError, ValueError):
        return None


def reuse_enabled(settings: Settings) -> bool:
    """Saved-result reuse is on unless ``settings.extra["reuse_results"]`` is false."""
    return bool((settings.extra or {}).get("reuse_results", True))


def is_reused(result: KpiResult) -> bool:
    """True when the result was taken from an earlier run instead of being recomputed."""
    return bool((result.explanation or {}).get(REUSED_KEY))


def stored_form(result: KpiResult) -> KpiResult:
    """The result as it is stored (without the in-memory reuse marker)."""
    if REUSED_KEY not in (result.explanation or {}):
        return result
    return replace(result, explanation={k: v for k, v in result.explanation.items() if k != REUSED_KEY})


def classify_error(exc: BaseException) -> tuple[ValidationStatus, str, str]:
    """(status, reason_code, detail) for a platform error: ``platform.classify_error`` when available."""
    fn = None
    try:
        from . import platform as _platform

        fn = getattr(_platform, "classify_error", None)
    except Exception:
        fn = None
    if fn is not None:
        try:
            status, code, detail = fn(exc)
            return ValidationStatus(status), str(code), str(detail)
        except Exception:
            pass
    return fallback_classify_error(exc)


def fallback_classify_error(exc: BaseException) -> tuple[ValidationStatus, str, str]:
    """Minimal classifier used when ``kpi_evidence.platform`` is unavailable."""
    error_class = ""
    for attr in ("getErrorClass", "getCondition"):
        getter = getattr(exc, attr, None)
        if callable(getter):
            try:
                error_class = getter() or ""
            except Exception:
                error_class = ""
            if error_class:
                break
    detail = f"{type(exc).__name__}: {exc}"
    msg = f"{error_class} {detail}"
    if isinstance(exc, (AccessDenied, PermissionError)) or re.search(
            r"PERMISSION_DENIED|INSUFFICIENT_PERMISSIONS|privilege|not authorized|access denied", msg, re.I):
        return ValidationStatus.MISSING_ACCESS, ReasonCode.PERMISSION_DENIED, detail
    if "returned NULL" in msg:
        return ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.NULL_RESULT, detail
    if "quality check" in msg or "denominator is zero" in msg:
        return ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.NO_DATA_FOR_PERIOD, detail
    if re.search(r"TABLE_OR_VIEW_NOT_FOUND|UNRESOLVED_COLUMN|SCHEMA_NOT_FOUND|does not exist|Catalog Error|not found",
                 msg, re.I):
        return ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.PLATFORM_OBJECT_MISSING, detail
    return ValidationStatus.BLOCKED, ReasonCode.EXECUTION_ERROR, detail


@dataclass
class _Item:
    obs: Observation
    mapping: KpiMapping | None
    value: ExtractedValue | None

    @property
    def method(self) -> ExtractionMethod | None:
        return self.value.method if self.value is not None else None


@dataclass
class _Context:
    store: Store
    registry: Registry
    settings: Settings
    doc: Document
    run_id: str
    dq_status: dict[str, str]
    provider: Any = None
    now: Any = field(default_factory=utcnow)
    snapshots: dict[tuple, dict[str, Any]] = field(default_factory=dict)
    focus: frozenset[str] | None = None
    reuse: bool = False

    @property
    def is_current(self) -> bool:
        return self.doc.status != DocumentStatus.SUPERSEDED

    def brand(self, obs: Observation) -> str | None:
        return self.doc.brand or obs.brand

    def result(self, item: _Item, kpi: KpiDefinition | None, status: ValidationStatus, reason_code: str,
               reason_detail: str, **extra: Any) -> KpiResult:
        obs = item.obs
        definition_hash = kpi.definition_hash() if kpi is not None else None
        kpi_id = kpi.kpi_id if kpi is not None else None
        report_value = obs.value
        platform_value = extra.pop("platform_value", None)
        abs_diff = rel_diff = None
        if report_value is not None and platform_value is not None:
            abs_diff = abs(report_value - platform_value)
            rel_diff = (report_value - platform_value) / abs(platform_value) if platform_value else None
        return KpiResult(
            result_id=stable_id(obs.observation_id, kpi_id, definition_hash),
            result_version=1,
            observation_id=obs.observation_id,
            doc_id=self.doc.doc_id,
            brand=self.brand(obs),
            kpi_id=kpi_id,
            kpi_version=kpi.version if kpi is not None else None,
            definition_hash=definition_hash,
            scope_key=obs.scope_key or "{}",
            period_start=obs.period_start,
            period_end=obs.period_end,
            period_grain=obs.period_grain,
            report_value=report_value,
            unit=obs.unit,
            platform_value=platform_value,
            abs_diff=abs_diff,
            rel_diff=rel_diff,
            tolerance=extra.pop("tolerance", None) or tolerance_record(kpi, report_value, obs.display_half_unit),
            match_basis=extra.pop("match_basis", None),
            validation_status=status,
            reason_code=reason_code,
            reason_detail=reason_detail,
            explanation=json_safe(extra.pop("explanation", None) or {}),
            definition_choices=extra.pop("definition_choices", None) or {},
            extraction_method=item.method,
            recon_run_id=extra.pop("recon_run_id", None),
            sql_artifact_id=extra.pop("sql_artifact_id", None),
            dq_status=extra.pop("dq_status", None),
            is_current=self.is_current,
            created_at=self.now,
            run_id=self.run_id,
        )


def extraction_gate(obs: Observation, method: ExtractionMethod | None) -> str | None:
    """Reason code when the extracted value must not be reconciled, else None."""
    codes = flag_codes(obs.flags)
    if "IMAGE_NOT_PARSED" in codes or method == ExtractionMethod.IMAGE_PLACEHOLDER:
        return ReasonCode.IMAGE_NOT_PARSED
    if obs.value is None or not math.isfinite(obs.value):
        return ReasonCode.LOW_CONFIDENCE_VALUE
    if obs.confidence is None or obs.confidence < GATE_CONFIDENCE:
        return ReasonCode.LOW_CONFIDENCE_VALUE
    if obs.period_end is None or obs.period_grain in _UNKNOWN_GRAINS:
        return ReasonCode.PERIOD_UNKNOWN
    if "SCOPE_UNKNOWN" in codes:
        return ReasonCode.SCOPE_UNKNOWN
    return None


def _value_clean(gate: str | None) -> bool:
    return gate in (None, ReasonCode.PERIOD_UNKNOWN, ReasonCode.SCOPE_UNKNOWN)


_GATE_DETAIL = {
    ReasonCode.IMAGE_NOT_PARSED: "The number sits in an image that was not read; check the source slide or page.",
    ReasonCode.LOW_CONFIDENCE_VALUE: "The value could not be read with confidence (extraction confidence {conf}); check it in the source.",
    ReasonCode.PERIOD_UNKNOWN: "The period this value refers to could not be determined; confirm the period.",
    ReasonCode.SCOPE_UNKNOWN: "The scope (region, territory or segment) of this value could not be determined; confirm it.",
}


def _ambiguous_reason(mapping: KpiMapping) -> str:
    reason = (mapping.reason or "").strip().lower()
    for code in (ReasonCode.MEANING_FLAGGED, ReasonCode.MODEL_PROPOSAL_ONLY, ReasonCode.MULTIPLE_CANDIDATES):
        if reason.startswith(code):
            return code
    if mapping.method == MappingMethod.MODEL:
        return ReasonCode.MODEL_PROPOSAL_ONLY
    return ReasonCode.MULTIPLE_CANDIDATES


def _ambiguous_detail(ctx: _Context, obs: Observation, names: list[str], suggested: KpiDefinition | None) -> str:
    if len(names) > 1:
        return f"'{obs.label}' could be one of: {', '.join(names)}. Confirm which KPI it is."
    only = ctx.registry.kpis.get(names[0]) if names else suggested
    if only is None:
        if names:
            return f"'{obs.label}' may not be {names[0]}. Confirm whether it is this KPI."
        return f"'{obs.label}' could not be matched to a KPI with confidence. Confirm which KPI it is, if any."
    unit = normalize_unit(obs.unit)
    if unit is not None and unit != only.unit:
        return (f"'{obs.label}' may not be {only.kpi_id}: the report gives it as '{unit}' but {only.label} is "
                f"measured as '{only.unit}'. Confirm whether it is this KPI.")
    return f"'{obs.label}' may not be {only.kpi_id}. Confirm whether it is this KPI."


def _pre_reconcile(ctx: _Context, item: _Item) -> KpiResult | KpiDefinition:
    obs, mapping = item.obs, item.mapping
    gate = extraction_gate(obs, item.method)
    status = mapping.status if mapping is not None else MappingStatus.NO_MATCH
    candidates = list(mapping.candidates or []) if mapping is not None else []
    if status == MappingStatus.NO_MATCH and _value_clean(gate):
        return ctx.result(item, None, ValidationStatus.UNMAPPED_CAPTURED, ReasonCode.NO_REGISTRY_MATCH,
                          f"'{obs.label}' does not match any KPI; the number was kept as a candidate.",
                          explanation={"label": obs.label, "candidates": candidates})
    if status == MappingStatus.AMBIGUOUS:
        suggested = ctx.registry.kpis.get(mapping.kpi_id) if mapping.kpi_id else None
        names = sorted({str(c.get("kpi_id")) for c in candidates if isinstance(c, dict) and c.get("kpi_id")})
        return ctx.result(item, suggested, ValidationStatus.AMBIGUOUS_MAPPING, _ambiguous_reason(mapping),
                          _ambiguous_detail(ctx, obs, names, suggested),
                          explanation={"label": obs.label, "candidates": candidates,
                                       "suggested_kpi_id": mapping.kpi_id, "mapping_reason": mapping.reason})
    kpi = ctx.registry.kpis.get(mapping.kpi_id) if mapping is not None and mapping.kpi_id else None
    if gate is not None:
        detail = _GATE_DETAIL[gate].format(conf=f"{obs.confidence:.2f}" if obs.confidence is not None else "n/a")
        return ctx.result(item, kpi, ValidationStatus.UNCERTAIN_EXTRACTION, gate, detail,
                          explanation={"flags": obs.flags or []})
    if kpi is None:
        return ctx.result(item, None, ValidationStatus.BLOCKED, ReasonCode.EXECUTION_ERROR,
                          f"The mapping points to KPI '{mapping.kpi_id}', which is not in the registry.",
                          explanation={"mapping_kpi_id": mapping.kpi_id})
    if kpi.is_causal:
        return ctx.result(item, kpi, ValidationStatus.REPORT_ONLY, ReasonCode.CAUSAL_CLAIM,
                          "Causal claim captured as stated; causal effects are never verified against platform data.")
    if kpi.platform is None:
        return ctx.result(item, kpi, ValidationStatus.REPORT_ONLY, ReasonCode.NO_PLATFORM_DEFINITION,
                          "No platform table holds this KPI; the report value was captured with its source location.")
    unit = normalize_unit(obs.unit)
    if unit is not None and unit != kpi.unit:
        return ctx.result(item, kpi, ValidationStatus.INCOMPATIBLE_DEFINITION, ReasonCode.UNIT_MISMATCH,
                          f"The report gives this value as '{unit}' but {kpi.label} is measured as '{kpi.unit}'.",
                          explanation={"report_unit": obs.unit, "kpi_unit": kpi.unit})
    return kpi


@dataclass
class _PhaseB:
    record: Any = None
    error: str | None = None
    grid: dict[str, Any] = field(default_factory=dict)
    judge: str | None = None
    policy: dict[str, Any] = field(default_factory=dict)
    judge_calls: list[dict[str, Any]] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)


class _SafeComplete:
    def __init__(self, fn):
        self.fn = fn
        self.calls: list[dict[str, Any]] = []

    def __call__(self, prompt: str) -> str:
        entry = {"prompt_sha": sha256_text(prompt)[:16]}
        try:
            reply = str(self.fn(prompt) or "")
        except Exception as exc:
            self.calls.append({**entry, "error": f"{type(exc).__name__}: {exc}"})
            return ""
        self.calls.append({**entry, "reply": reply[:4000]})
        return reply


class _FallbackJudge:
    def __init__(self, primary, fallback, name: str):
        self.primary, self.fallback, self.name = primary, fallback, name

    def propose(self, state, runner):
        change = self.primary.propose(state, runner)
        return change if change is not None else self.fallback.propose(state, runner)


_NO_COMPLETION_PROVIDERS = {"off", "local"}


def _make_judge(settings: Settings, provider: Any) -> tuple[Any, str, _SafeComplete | None]:
    complete = getattr(provider, "complete", None) if provider is not None else None
    provider_name = str(getattr(provider, "name", "") or type(provider).__name__)
    if settings.ai_mode != "off" and callable(complete) and provider_name not in _NO_COMPLETION_PROVIDERS:
        traced = _SafeComplete(complete)
        judge = _FallbackJudge(LLMJudge(traced), StatedRuleJudge(), f"llm:{provider_name}+stated_rule")
        return judge, judge.name, traced
    return StatedRuleJudge(), StatedRuleJudge.name, None


def _group_key(ctx: _Context, item: _Item, kpi: KpiDefinition) -> tuple:
    permitted, other = detect_stated_variants(kpi, item.obs.stated_rule or "")
    return (kpi.kpi_id, ctx.brand(item.obs) or "", item.obs.scope_key or "{}", item.obs.period_grain,
            canonical_json(permitted), canonical_json(other))


def _phase_b_tolerance(kpi: KpiDefinition, items: list[_Item]) -> float:
    tol = kpi.tolerance.rel
    for it in items:
        v = it.obs.value
        if v:
            half = it.obs.display_half_unit if kpi.tolerance.use_display_rounding else 0.0
            tol = max(tol, max(kpi.tolerance.abs, half or 0.0) / abs(v))
    return max(tol, 1e-9)


def _targets(ctx: _Context, items: list[_Item], compiled: CompiledKpi, stated_text: str) -> list[Target]:
    periods = sorted({it.obs.period_end for it in items})
    holdout: set[date] = set()
    if len(periods) >= 4:
        holdout = set(periods[-(2 if len(periods) >= 6 else 1):])
    out = []
    for it in items:
        o = it.obs
        page = it.value.slide_or_page if it.value is not None else None
        out.append(Target(
            target_id=o.observation_id, report=ctx.doc.doc_id, metric=compiled.spec.name,
            as_of=o.period_end.isoformat(), value=float(o.value), unit=normalize_unit(o.unit) or "",
            stated_rule=stated_text,
            source_file=ctx.doc.file_name, page="" if page is None else str(page),
            split="holdout" if o.period_end in holdout else "fit",
            confidence=min(max(float(o.confidence), 0.0), 1.0),
            extraction_method=it.method.value if it.method else ""))
    return out


def _phase_a(rspec: RecordingSpec, targets: list[Target], runner, start: dict[str, str] | None, session):
    rspec.sink.phase = "phase_a"
    try:
        with session():
            rec = reconcile(rspec, targets, runner, judge=None, start=start, max_iter=0, log=_quiet)
        return [rec], {r["target_id"]: r for r in rec.iterations[0].per_target}, {}
    except Exception as exc:
        if len(targets) == 1:
            return [], {}, {targets[0].target_id: exc}
    records, rows, errors = [], {}, {}
    for t in targets:
        try:
            with session():
                rec = reconcile(rspec, [replace(t, split="fit")], runner, judge=None, start=start, max_iter=0,
                                log=_quiet)
        except Exception as exc:
            errors[t.target_id] = exc
            continue
        records.append(rec)
        rows[t.target_id] = dict(rec.iterations[0].per_target[0], split=t.split)
    return records, rows, errors


def _phase_b(ctx: _Context, rspec: RecordingSpec, targets: list[Target], runner,
             start: dict[str, str] | None) -> _PhaseB:
    if not any(t.split == "fit" for t in targets):
        targets = [replace(t, split="fit") for t in targets]
    judge, name, traced = _make_judge(ctx.settings, ctx.provider)
    policy = Policy(**PHASE_B_POLICY)
    out = _PhaseB(judge=name, policy=asdict(policy), targets=targets)
    rspec.sink.phase = "phase_b"
    try:
        out.record = reconcile(rspec, targets, runner, judge=judge, policy=policy, start=start,
                               max_iter=ctx.settings.phase_b_max_iter, log=_quiet)
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
    rspec.sink.phase = "grid"
    try:
        out.grid = precompute_fit(rspec, targets, runner, max_candidates=ctx.settings.grid_max_candidates)
    except Exception as exc:
        out.grid = {"grid_skipped": "budget" if "budget" in str(exc) else "error", "detail": str(exc)}
    if traced is not None:
        out.judge_calls = traced.calls
    return out


def _final_iteration(record):
    return next(i for i in reversed(record.iterations) if i.accepted is not False)


def _differing(compiled: CompiledKpi, start: dict[str, str], final: dict[str, str]) -> dict[str, Any]:
    out = {}
    for name, opt in final.items():
        if start.get(name) != opt:
            p = compiled.spec.params[name]
            out[name] = {"from": start.get(name), "to": opt, "hypothesis": p.hypothesis,
                         "describe": p.describe.get(opt, "")}
    return out


def _accepted_changes(record) -> list[dict[str, Any]]:
    edits = []
    for it in record.iterations[1:]:
        if not it.accepted or it.change is None:
            continue
        for c in getattr(it.change, "changes", None) or [it.change]:
            edits.append({"param": c.param, "old": c.old, "new": c.new, "hypothesis": c.hypothesis,
                          "rationale": c.rationale, "proposed_by": c.proposed_by})
    return edits


def _grid_matches(kpi: KpiDefinition, sink: TrialSink, target: Target, half: float) -> list[dict[str, str]]:
    seen, out = set(), []
    for t in sink:
        if t["phase"] != "grid" or t["as_of"] != target.as_of or t["value"] is None:
            continue
        if within(kpi, target.value, t["value"], half):
            key = canonical_json(t["choices"])
            if key not in seen:
                seen.add(key)
                out.append(dict(t["choices"]))
    return out


def _variance_reason(phase_b: _PhaseB, gold: float, computed: float) -> str:
    if gold == 0 and computed != 0:
        return ReasonCode.ZERO_TARGET
    n_fit = len({t.as_of for t in phase_b.targets if t.split == "fit"})
    if n_fit < 2:
        return ReasonCode.SINGLE_PERIOD_GAP
    if phase_b.record is None:
        return ReasonCode.UNRESOLVED
    pattern = phase_b.record.residual.pattern
    if pattern == "level_shift":
        return ReasonCode.SYSTEMATIC_GAP
    if pattern == "within_tolerance":
        return ReasonCode.SINGLE_PERIOD_GAP
    return f"{ReasonCode.UNRESOLVED}_{pattern}"


def _variance_detail(kpi: KpiDefinition, gold: float, computed: float, half: float) -> str:
    tol = tolerance_text(kpi, gold, half)
    if computed == 0:
        return (f"Report {fmt_value(gold, kpi.unit)} but the platform has 0 for this period "
                f"(tolerance {tol}).")
    rel = (gold - computed) / abs(computed)
    direction = "above" if rel > 0 else "below"
    return (f"Report {abs(rel) * 100:.1f}% {direction} the platform "
            f"(platform {fmt_value(computed, kpi.unit)}; tolerance {tol}).")


def snapshot_names(kpi: KpiDefinition, compiled: CompiledKpi | None = None) -> list[str]:
    """Every source of a KPI: its declared source views plus each relation its rendered SQL reads."""
    names = list(kpi.platform.source_views) if kpi.platform is not None else []
    if compiled is not None:
        names += compiled.relations()
    return list(dict.fromkeys(names))


def _platform_snapshot(ctx: _Context, kpi: KpiDefinition, compiled: CompiledKpi | None = None) -> dict[str, Any]:
    views = tuple(snapshot_names(kpi, compiled))
    if views not in ctx.snapshots:
        try:
            from . import platform as _platform

            ctx.snapshots[views] = dict(_platform.platform_snapshot(ctx.store, ctx.settings, list(views)) or {})
        except Exception:
            ctx.snapshots[views] = {}
    return ctx.snapshots[views]


def _same_observation(old: KpiResult, item: _Item) -> bool:
    obs = item.obs
    half = (old.tolerance or {}).get("half_unit")
    return (_num_eq(old.report_value, obs.value) and old.unit == obs.unit and old.period_end == obs.period_end
            and old.period_grain == obs.period_grain and (old.scope_key or "{}") == (obs.scope_key or "{}")
            and _num_eq(None if half is None else float(half), obs.display_half_unit)
            and old.extraction_method == item.method)


def _reusable_results(ctx: _Context, rid: str, kpi: KpiDefinition, items: list[_Item], snapshot: dict[str, Any],
                      dq: str) -> list[KpiResult] | None:
    from .platform import is_available

    if not ctx.reuse or not is_available(snapshot):
        return None
    run = ctx.store.get(ReconRun, recon_run_id=rid)
    if run is None or run.reconloop_version != RECONLOOP_VERSION:
        return None
    if run.judge is not None and run.judge != _make_judge(ctx.settings, ctx.provider)[1]:
        return None
    stored = {r.observation_id: r for r in ctx.store.read(KpiResult, "recon_run_id = :r", {"r": rid})}
    out = []
    for it in items:
        old = stored.get(it.obs.observation_id)
        if old is None or old.result_id != stable_id(it.obs.observation_id, kpi.kpi_id, kpi.definition_hash()):
            return None
        primary = str((old.explanation or {}).get("primary_status") or "")
        if not _same_observation(old, it) or effective_status(old) in _NO_REUSE_STATUSES or primary in _NO_REUSE_STATUSES:
            return None
        explanation = {**(old.explanation or {}), REUSED_KEY: run.run_id or rid}
        out.append(replace(old, explanation=explanation, dq_status=dq, is_current=ctx.is_current))
    return out


def _dq_for(dq_status: dict[str, str], views: tuple[str, ...]) -> tuple[str, list[str]]:
    by_name = {str(k).split(".")[-1].lower(): str(v).lower() for k, v in (dq_status or {}).items()}
    states = [by_name.get(str(v).split(".")[-1].lower(), "not_checked") for v in views]
    failed = [v for v, state in zip(views, states) if state == "fail"]
    if failed:
        return "fail", failed
    return ("pass" if states and all(state == "pass" for state in states) else "not_checked"), []


def _reconcile_group(ctx: _Context, kpi: KpiDefinition, items: list[_Item]) -> list[KpiResult]:
    obs0 = items[0].obs
    brand = ctx.brand(obs0)
    scope = json.loads(obs0.scope_key or "{}")
    grain = obs0.period_grain
    stated_text = " | ".join(sorted({o.stated_rule.strip() for o in (i.obs for i in items) if (o.stated_rule or "").strip()}))
    try:
        compiled = compile_kpi(kpi, brand=brand, scope=scope, grain=grain, settings=ctx.settings,
                               stated_text=stated_text, reconloop_tolerance=_phase_b_tolerance(kpi, items),
                               assumed_choices=ctx.registry.assumed_definitions(ctx.doc.report_type, kpi.kpi_id))
    except CompileError as exc:
        status = (ValidationStatus.UNCERTAIN_EXTRACTION if exc.reason_code == "brand_unknown"
                  else ValidationStatus.BLOCKED if exc.reason_code == "binding_missing"
                  else ValidationStatus.INCOMPATIBLE_DEFINITION)
        return [ctx.result(it, kpi, status, exc.reason_code, f"The platform definition cannot be applied: {exc}.")
                for it in items]
    dq, failed_views = _dq_for(ctx.dq_status, kpi.platform.source_views)
    if failed_views:
        views = ", ".join(failed_views)
        return [ctx.result(it, kpi, ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.DQ_FAILED,
                           f"Governed view {views} failed a hard data-quality check, so no platform value is used.",
                           dq_status=dq, explanation={"failed_views": failed_views})
                for it in items]

    from .materialize import artifact_id_for

    targets = _targets(ctx, items, compiled, stated_text)
    start_choices = dict(compiled.start_choices)
    snapshot = _platform_snapshot(ctx, kpi, compiled)
    trial_spec = compiled.trial_spec()
    rid = stable_id(ctx.doc.doc_id, kpi.kpi_id, compiled.definition_hash, compiled.bindings_hash,
                    sorted([t.target_id, t.value, t.as_of, t.split] for t in targets), start_choices, snapshot,
                    trial_spec.checks)
    stored = _reusable_results(ctx, rid, kpi, items, snapshot, dq)
    if stored is not None:
        return stored
    started = utcnow()
    rspec = recording(compiled.spec)
    checked = recording(trial_spec)
    checked.sink = rspec.sink
    runner = ctx.store.platform_access.runner()
    start = start_choices if start_choices != compiled.governed_defaults else None
    by_target = {t.target_id: t for t in targets}
    item_by_target = {it.obs.observation_id: it for it in items}

    a_records, rows, errors = _phase_a(checked, targets, runner, start, lambda: read_session(ctx.store))

    verdicts: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for t in targets:
        it = item_by_target[t.target_id]
        half = it.obs.display_half_unit or 0.0
        if t.target_id in errors:
            status, code, detail = classify_error(errors[t.target_id])
            explanation = {"error": detail}
            missing = _missing_months(detail)
            if missing is not None:
                explanation = {"error": f"the platform source has no data for {_months_text(missing)} of the period",
                               "error_detail": detail}
            verdicts[t.target_id] = {"status": status, "code": code, "explanation": explanation,
                                     "detail": _error_sentence(status, code, detail, t.as_of)}
            continue
        computed = rows[t.target_id]["computed"]
        factor = scale_factor(computed, t.value)
        if factor is not None:
            verdicts[t.target_id] = {
                "status": ValidationStatus.INCOMPATIBLE_DEFINITION, "code": ReasonCode.SCALE_MISMATCH,
                "platform_value": computed, "explanation": {"platform_over_report": computed / t.value,
                                                            "scale_factor": factor},
                "detail": (f"The platform value {fmt_value(computed, kpi.unit)} is about {_factor_text(factor)} the "
                           f"report value {fmt_value(t.value, kpi.unit)}; the report likely uses a different scale.")}
        elif within(kpi, t.value, computed, half):
            stated = bool(compiled.stated_variants)
            assumed_only = stated and set(compiled.stated_variants) <= set(compiled.assumed_variants)
            basis = match_basis(t.value, computed, half)
            verdicts[t.target_id] = {
                "status": ValidationStatus.VERIFIED,
                "code": (ReasonCode.MATCH_REPORT_TYPE_DEFINITION if assumed_only
                         else ReasonCode.MATCH_STATED_VARIANT if stated else ReasonCode.MATCH_GOVERNED_DEFAULT),
                "platform_value": computed, "match_basis": basis,
                "explanation": {"definition": compiled.describe(start_choices),
                                "stated_variants": compiled.stated_variants},
                "detail": _verified_detail(kpi, t.value, computed, half, basis, compiled, stated)}
        else:
            pending.append(t.target_id)

    error_grid: dict[str, Any] = {}
    if errors and has_declared_alternatives(rspec):
        error_grid = _error_grid(ctx, checked, [by_target[tid] for tid in errors], runner)
        for tid in errors:
            half = item_by_target[tid].obs.display_half_unit or 0.0
            matches = _alternative_matches(kpi, rspec.sink, by_target[tid], half, start_choices)
            if matches:
                verdicts[tid] = _alternative_after_error(kpi, compiled, start_choices, matches, verdicts[tid],
                                                         error_grid)

    phase_b: _PhaseB | None = None
    if pending:
        usable = [t for t in targets if t.target_id in rows
                  and verdicts.get(t.target_id, {}).get("code") != ReasonCode.SCALE_MISMATCH]
        with read_session(ctx.store):
            phase_b = _phase_b(ctx, checked, usable, runner, start)
        final_rows: dict[str, dict] = {}
        final_params = start_choices
        if phase_b.record is not None:
            final_it = _final_iteration(phase_b.record)
            final_rows = {r["target_id"]: r for r in final_it.per_target}
            final_params = dict(phase_b.record.final_params)
        moved = final_params != start_choices
        for tid in pending:
            t, it = by_target[tid], item_by_target[tid]
            half = it.obs.display_half_unit or 0.0
            computed = rows[tid]["computed"]
            alt = final_rows.get(tid, {}).get("computed")
            evidence = {"phase_b_status": phase_b.record.status if phase_b.record else None,
                        "phase_b_error": phase_b.error, "judge": phase_b.judge,
                        "declared_definitions_matching": _grid_matches(kpi, rspec.sink, t, half),
                        "grid": {k: v for k, v in phase_b.grid.items() if k != "failures"},
                        "stated_other": compiled.stated_other}
            if moved and within(kpi, t.value, alt, half):
                diff = _differing(compiled, start_choices, final_params)
                label = ", ".join(f"{k}={v['to']}" for k, v in diff.items())
                verdicts[tid] = {
                    "status": ValidationStatus.INCOMPATIBLE_DEFINITION, "code": ReasonCode.MATCHES_ALTERNATIVE,
                    "platform_value": computed,
                    "explanation": {**evidence, "alternative": label, "matched_choices": final_params,
                                    "differing_options": diff, "alternative_value": alt,
                                    "accepted_changes": _accepted_changes(phase_b.record)},
                    "detail": (f"The report matches the platform only under a different definition ({label}: "
                               f"{fmt_value(alt, kpi.unit)}); {definition_name(kpi)} gives "
                               f"{fmt_value(computed, kpi.unit)}.")}
            else:
                residual = asdict(phase_b.record.residual) if phase_b.record else None
                incomplete = _incomplete_alternatives(compiled, rspec.sink, t, start_choices)
                verdicts[tid] = {
                    "status": ValidationStatus.VARIANCE_REVIEW,
                    "code": _variance_reason(phase_b, t.value, computed),
                    "platform_value": computed,
                    "explanation": {**evidence, "residual": residual,
                                    "best_candidate": final_params if moved else None,
                                    "incomplete_alternatives": incomplete},
                    "detail": _variance_detail(kpi, t.value, computed, half) + "".join(
                        f" The alternative {name} was not compared: it has no data for {months} of the period."
                        for name, months in incomplete.items())}

    finished = utcnow()
    run = _recon_run(ctx, rid, kpi, compiled, targets, start_choices, a_records, rows, errors, phase_b,
                     verdicts, snapshot, started, finished, stated_text, error_grid=error_grid)
    trials = [_trial(rid, entry, ctx.now) for entry in rspec.sink]
    _write_run(ctx.store, run, trials)

    artifact_id = artifact_id_for(kpi.kpi_id, compiled.definition_hash, compiled.bindings_hash, start_choices)
    results = []
    for t in targets:
        it, v = item_by_target[t.target_id], verdicts[t.target_id]
        pv = v.get("platform_value")
        results.append(ctx.result(
            it, kpi, v["status"], v["code"], v["detail"], platform_value=pv, match_basis=v.get("match_basis"),
            explanation=v.get("explanation"), definition_choices=start_choices, recon_run_id=rid,
            sql_artifact_id=artifact_id if pv is not None else None, dq_status=dq))
    return results


def has_declared_alternatives(spec: MetricSpec) -> bool:
    """True when some declared parameter offers more than one option (candidate tables, measures, ...)."""
    return any(len(p.options) > 1 for p in spec.params.values())


def _error_grid(ctx: _Context, rspec: RecordingSpec, targets: list[Target], runner) -> dict[str, Any]:
    rspec.sink.phase = "grid"
    summary: dict[str, Any] = {}
    for t in {t.as_of: t for t in targets}.values():
        try:
            out = precompute_fit(rspec, [replace(t, split="fit")], runner,
                                 max_candidates=ctx.settings.grid_max_candidates)
            summary[t.as_of] = {"candidates": out["candidates"], "failures": len(out["failures"])}
        except Exception as exc:
            summary[t.as_of] = {"grid_skipped": "budget" if "budget" in str(exc) else "error", "detail": str(exc)}
    return summary


def _alternative_matches(kpi: KpiDefinition, sink: TrialSink, target: Target, half: float,
                         start: dict[str, str]) -> list[dict[str, Any]]:
    start_key = canonical_json(start)
    seen, out = set(), []
    for t in sink:
        if t["phase"] != "grid" or t["as_of"] != target.as_of or t["value"] is None:
            continue
        key = canonical_json(t["choices"])
        if key == start_key or key in seen or not within(kpi, target.value, t["value"], half):
            continue
        seen.add(key)
        out.append(t)
    return out


_INCOMPLETE_WINDOW = re.compile(rf"quality check '?{WINDOW_CHECK}\w*'? failed: (\d+)")


def _missing_months(detail: str) -> int | None:
    m = _INCOMPLETE_WINDOW.search(detail or "")
    return int(m.group(1)) if m else None


def _months_text(count: int) -> str:
    return f"{count} month" if count == 1 else f"{count} months"


def _incomplete_alternatives(compiled: CompiledKpi, sink: TrialSink, target: Target,
                             start: dict[str, str]) -> dict[str, str]:
    found: dict[frozenset, tuple[str, int]] = {}
    for t in sink:
        missing = _missing_months(t.get("error") or "")
        if t["as_of"] != target.as_of or missing is None:
            continue
        diff = _differing(compiled, start, t["choices"])
        name = ", ".join(f"{k}={v['to']}" + (f" ({v['describe']})" if v["describe"] else "") for k, v in diff.items())
        if name:
            found.setdefault(frozenset((k, v["to"]) for k, v in diff.items()), (name, missing))
    out: dict[str, str] = {}
    kept: list[frozenset] = []
    for key in sorted(found, key=len):
        if not any(k <= key for k in kept):
            kept.append(key)
            out[found[key][0]] = _months_text(found[key][1])
    return out


def _short_error(detail: str, limit: int = 140) -> str:
    text = (detail or "").strip().splitlines()[0] if (detail or "").strip() else "unknown error"
    head, sep, rest = text.partition(": ")
    if sep and " " not in head:
        text = rest
    text = text.rstrip(" .!")
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _alternative_after_error(kpi: KpiDefinition, compiled: CompiledKpi, start: dict[str, str],
                             matches: list[dict[str, Any]], prior: dict[str, Any],
                             error_grid: dict[str, Any]) -> dict[str, Any]:
    first = matches[0]
    diff = _differing(compiled, start, first["choices"])
    label = ", ".join(f"{k}={v['to']}" for k, v in diff.items())
    name = ", ".join(f"{k}={v['to']}" + (f" ({v['describe']})" if v["describe"] else "") for k, v in diff.items())
    error = str((prior.get("explanation") or {}).get("error", ""))
    status = prior["status"]
    return {
        "status": ValidationStatus.INCOMPATIBLE_DEFINITION, "code": ReasonCode.MATCHES_ALTERNATIVE,
        "explanation": {"error": error, "primary_error": error,
                        "primary_status": status.value if isinstance(status, ValidationStatus) else str(status),
                        "primary_reason_code": prior.get("code"), "alternative": label,
                        "matched_choices": dict(first["choices"]), "differing_options": diff,
                        "alternative_value": first["value"], "alternative_sql": first.get("sql"),
                        "declared_definitions_matching": [dict(m["choices"]) for m in matches],
                        "grid": error_grid},
        "detail": (f"The primary definition could not run ({_short_error(error)}); the alternative {name} "
                   f"reproduces the report: {fmt_value(first['value'], kpi.unit)}.")}


def _factor_text(factor: float) -> str:
    return f"{factor:,.0f}x" if factor >= 1 else f"1/{1 / factor:,.0f} of"


def _verified_detail(kpi, gold, computed, half, basis, compiled, stated) -> str:
    how = {MatchBasis.EXACT: "exactly",
           MatchBasis.WITHIN_ROUNDING: "within the rounding of the printed value",
           MatchBasis.WITHIN_TOLERANCE: f"within tolerance ({tolerance_text(kpi, gold, half)})"}[basis]
    if stated and set(compiled.stated_variants) <= set(compiled.assumed_variants):
        under = f"the definition this report type uses ({compiled.describe(compiled.start_choices)})"
    elif stated:
        under = f"the definition the report states ({compiled.describe(compiled.start_choices)})"
    else:
        under = definition_name(kpi)
    return f"Report {fmt_value(gold, kpi.unit)} matches the platform ({fmt_value(computed, kpi.unit)}) {how} under {under}."


def definition_name(kpi: KpiDefinition) -> str:
    """The name reason sentences use for the KPI's own definition."""
    if str((kpi.raw or {}).get("definition_source") or "").lower() == "hint":
        return "the primary KPI table definition"
    return "the governed definition"


def _error_sentence(status: ValidationStatus, code: str, detail: str, as_of: str) -> str:
    if status == ValidationStatus.MISSING_ACCESS:
        return "The pipeline cannot read the platform source for this KPI (permission denied); request access."
    if code == ReasonCode.NULL_RESULT:
        return f"The platform has no value for the period ending {as_of} (the query returned NULL)."
    if code == ReasonCode.NO_DATA_FOR_PERIOD:
        missing = _missing_months(detail)
        if missing is not None:
            return (f"The platform source has no data for {_months_text(missing)} of the period ending {as_of}, so it "
                    "cannot give a value for the whole period. Compare again once every month is loaded.")
        return f"The platform has no usable data for the period ending {as_of}. {detail}"
    if code == ReasonCode.PLATFORM_OBJECT_MISSING:
        return f"A platform object this KPI needs is missing. {detail}"
    return f"The platform check could not run. {detail}"


def _record_dict(record) -> dict[str, Any]:
    return json_safe(record.to_dict())


def _recon_run(ctx, rid, kpi, compiled, targets, start_choices, a_records, rows, errors, phase_b, verdicts,
               snapshot, started, finished, stated_text, error_grid=None) -> ReconRun:
    statuses = [v["status"] for v in verdicts.values()]
    if not rows:
        outcome = "error"
    elif all(s == ValidationStatus.VERIFIED for s in statuses):
        outcome = "verified"
    else:
        outcome = "not_verified"
    if phase_b is None:
        b_outcome, final = None, start_choices
    elif phase_b.record is None:
        b_outcome, final = "error", start_choices
    else:
        b_outcome, final = phase_b.record.status, dict(phase_b.record.final_params)
    record = {
        "phase_a": [_record_dict(r) for r in a_records],
        "phase_a_errors": {tid: f"{type(e).__name__}: {e}" for tid, e in errors.items()},
        "phase_b": _record_dict(phase_b.record) if phase_b and phase_b.record else None,
        "phase_b_error": phase_b.error if phase_b else None,
        "grid": json_safe(phase_b.grid) if phase_b else None,
        "judge_calls": phase_b.judge_calls if phase_b else [],
        "targets": [asdict(t) for t in targets],
        "stated_text": stated_text,
        "stated_variants": compiled.stated_variants,
        "stated_other": compiled.stated_other,
        "verdicts": {tid: {"status": v["status"].value, "reason_code": v["code"]} for tid, v in verdicts.items()},
    }
    if error_grid:
        record["error_grid"] = json_safe(error_grid)
    error = "; ".join(f"{tid[:12]}: {type(e).__name__}: {e}" for tid, e in errors.items()) or None
    return ReconRun(
        recon_run_id=rid, doc_id=ctx.doc.doc_id, kpi_id=kpi.kpi_id, kpi_version=kpi.version,
        definition_hash=compiled.definition_hash, bindings=compiled.bindings, bindings_hash=compiled.bindings_hash,
        start_choices=start_choices, final_choices=final, phase_a_outcome=outcome, phase_b_outcome=b_outcome,
        reconloop_version=RECONLOOP_VERSION, judge=phase_b.judge if phase_b else None,
        policy=phase_b.policy if phase_b else {}, record=json_safe(record), platform_snapshot=snapshot,
        started_at=started, finished_at=finished, run_id=ctx.run_id, error=error[:4000] if error else None)


def _trial(rid: str, entry: dict[str, Any], now) -> ReconTrial:
    sql = entry.get("sql")
    value = entry.get("value")
    return ReconTrial(
        trial_id=stable_id(rid, entry["seq"]), recon_run_id=rid, seq=int(entry["seq"]), phase=entry["phase"],
        choices=entry["choices"], as_of=date.fromisoformat(entry["as_of"]),
        value=value if value is None or math.isfinite(value) else None, error=entry.get("error"),
        sql=sql, sql_sha=sha256_text(sql) if sql else None, created_at=now)


def _write_run(store: Store, run: ReconRun, trials: list[ReconTrial]) -> None:
    old = store.get(ReconRun, recon_run_id=run.recon_run_id)
    if old is not None and canonical_json([old.record, old.error, old.judge, old.policy]) == canonical_json(
            [run.record, run.error, run.judge, run.policy]):
        return
    with store.transaction():
        store.upsert([run])
        store.replace(ReconTrial, {"recon_run_id": run.recon_run_id}, trials)


def _num_eq(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


def same_inputs(old: KpiResult, new: KpiResult) -> bool:
    """Inputs that define a result version: values, verdict (before any conflict overlay) and definition."""
    return (_num_eq(old.report_value, new.report_value) and _num_eq(old.platform_value, new.platform_value)
            and effective_status(old) == effective_status(new)
            and canonical_json(old.definition_choices or {}) == canonical_json(new.definition_choices or {}))


def read_results(store: Store, result_ids: list[str]) -> dict[str, KpiResult]:
    out: dict[str, KpiResult] = {}
    for i in range(0, len(result_ids), 200):
        chunk = result_ids[i:i + 200]
        marks = ", ".join(f":r{j}" for j in range(len(chunk)))
        for r in store.read(KpiResult, f"result_id IN ({marks})", {f"r{j}": v for j, v in enumerate(chunk)}):
            out[r.result_id] = r
    return out


def write_results(store: Store, results: list[KpiResult], *, stale_doc_id: str | None = None,
                  focus_kpis: Any = None, observation_ids: Any = None) -> list[KpiResult]:
    """Version and upsert results; optionally mark this document's other current results (in focus) not current."""
    prev = read_results(store, [r.result_id for r in results])
    out, changed = [], []
    for r in results:
        old = prev.get(r.result_id)
        if old is not None and is_reused(r):
            if to_row(stored_form(r)) != to_row(old):
                changed.append(stored_form(r))
            out.append(r)
            continue
        if old is None:
            r.result_version = 1
        elif same_inputs(old, r):
            if old.validation_status == ValidationStatus.REPORT_CONFLICT:
                if old.is_current != r.is_current:
                    old.is_current = r.is_current
                    changed.append(old)
                out.append(old)
                continue
            r.result_version, r.created_at, r.run_id = old.result_version, old.created_at, old.run_id
        else:
            r.result_version = old.result_version + 1
        out.append(r)
        changed.append(stored_form(r))
    if changed:
        store.upsert(changed)
    if stale_doc_id:
        keep = {r.result_id for r in results}
        evaluated = {r.observation_id for r in results}
        focus = None if focus_kpis is None else set(focus_kpis)
        known = None if observation_ids is None else set(observation_ids)

        def in_scope(r: KpiResult) -> bool:
            return (focus is None or r.kpi_id in focus or r.observation_id in evaluated
                    or (known is not None and r.observation_id not in known))

        stale = [r for r in store.read(KpiResult, "doc_id = :d AND is_current = TRUE", {"d": stale_doc_id})
                 if r.result_id not in keep and r.extraction_method != ExtractionMethod.REVIEWER_CORRECTION
                 and in_scope(r)]
        for r in stale:
            r.is_current = False
        if stale:
            store.upsert(stale)
    return out


def _values_for(store: Store, doc_id: str, value_ids: set[str] | None = None) -> dict[str, ExtractedValue]:
    try:
        if value_ids is None:
            rows = store.read(ExtractedValue, "doc_id = :d", {"d": doc_id})
        else:
            ids = sorted(value_ids)
            marks = ", ".join(f":v{i}" for i in range(len(ids))) or "NULL"
            rows = store.read(ExtractedValue, f"value_id IN ({marks})", {f"v{i}": v for i, v in enumerate(ids)})
    except Exception:
        return {}
    return {v.value_id: v for v in rows}


def _mappings_by_observation(mappings: list[KpiMapping], registry_version: str) -> dict[str, KpiMapping]:
    out: dict[str, KpiMapping] = {}
    for m in mappings or []:
        current = out.get(m.observation_id)
        if current is None or (m.registry_version == registry_version and current.registry_version != registry_version):
            out[m.observation_id] = m
    return out


def _evaluate(ctx: _Context, observations: list[Observation], mappings: list[KpiMapping],
              values: dict[str, ExtractedValue]) -> list[KpiResult]:
    by_obs = _mappings_by_observation(mappings, ctx.registry.version)
    unique = [o for o in {o.observation_id: o for o in observations}.values()
              if in_focus(by_obs.get(o.observation_id), ctx.focus)]
    order = [o.observation_id for o in unique]
    final: dict[str, KpiResult] = {}
    groups: dict[tuple, list[_Item]] = defaultdict(list)
    group_kpi: dict[tuple, KpiDefinition] = {}
    for obs in unique:
        item = _Item(obs, by_obs.get(obs.observation_id), values.get(obs.value_id))
        try:
            outcome = _pre_reconcile(ctx, item)
        except Exception as exc:
            final[obs.observation_id] = ctx.result(item, None, ValidationStatus.BLOCKED, ReasonCode.EXECUTION_ERROR,
                                                   f"The observation could not be evaluated: {type(exc).__name__}: {exc}.")
            continue
        if isinstance(outcome, KpiResult):
            final[obs.observation_id] = outcome
            continue
        key = _group_key(ctx, item, outcome)
        groups[key].append(item)
        group_kpi[key] = outcome
    for key, items in groups.items():
        kpi = group_kpi[key]
        try:
            results = _reconcile_group(ctx, kpi, items)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            results = [ctx.result(it, kpi, ValidationStatus.BLOCKED, ReasonCode.EXECUTION_ERROR,
                                  f"The reconciliation could not run: {detail}", explanation={"error": detail})
                       for it in items]
        for r in results:
            final[r.observation_id] = r
    return [final[oid] for oid in order]


def evaluate_document(store: Store, registry: Registry, settings: Settings, doc: Document,
                      observations: list[Observation], mappings: list[KpiMapping], dq_status: dict[str, str] | None,
                      run_id: str, provider: Any = None, focus_kpis: Any = None) -> list[KpiResult]:
    """Reconcile the observations of ``doc`` (only those in ``focus_kpis`` when given) and write the results."""
    focus = None if focus_kpis is None else frozenset(str(k) for k in focus_kpis)
    ctx = _Context(store=store, registry=registry, settings=settings, doc=doc, run_id=run_id,
                   dq_status=dict(dq_status or {}), provider=provider, focus=focus, reuse=reuse_enabled(settings))
    results = _evaluate(ctx, observations, mappings, _values_for(store, doc.doc_id))
    return write_results(store, results, stale_doc_id=doc.doc_id, focus_kpis=focus,
                         observation_ids=[o.observation_id for o in observations])


def _current_dq_status(store: Store, settings: Settings) -> dict[str, str]:
    try:
        from . import platform as _platform

        return dict(_platform.dq_status_by_view(store, settings) or {})
    except Exception:
        return {}


def recheck_observation(store: Store, registry: Registry, settings: Settings, observation: Observation,
                        kpi_id: str | None, run_id: str) -> KpiResult:
    """Re-run the verdict rules for one (corrected) observation against ``kpi_id`` (review corrections)."""
    doc = store.get(Document, doc_id=observation.doc_id)
    if doc is None:
        raise KeyError(f"unknown document {observation.doc_id}")
    kpi = registry.get(kpi_id) if kpi_id else None
    mapping = KpiMapping(
        observation_id=observation.observation_id, registry_version=registry.version,
        status=MappingStatus.ACCEPTED if kpi else MappingStatus.NO_MATCH,
        method=MappingMethod.REVIEWER if kpi else None, kpi_id=kpi.kpi_id if kpi else None,
        kpi_version=kpi.version if kpi else None,
        candidates=[{"kpi_id": kpi.kpi_id, "method": MappingMethod.REVIEWER.value, "confidence": 1.0}] if kpi else [],
        reason="reviewer correction", decided_by="system:recheck@v1", decided_at=utcnow())
    ctx = _Context(store=store, registry=registry, settings=settings, doc=doc, run_id=run_id,
                   dq_status=_current_dq_status(store, settings))
    values = _values_for(store, doc.doc_id, {observation.value_id})
    results = _evaluate(ctx, [observation], [mapping], values)
    return write_results(store, results)[0]
