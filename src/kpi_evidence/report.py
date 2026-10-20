from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .contracts import (
    STATUS_INFO, Document, DocumentPage, DocumentStatus, ExtractedValue, ExtractionIssue, ExtractionMethod,
    KpiMapping, KpiResult, Observation, PlatformKpiValue, ReconRun, ReconTrial, SqlArtifact, ValidationStatus,
)
from .ids import canonical_json, sha256_text, utcnow

COMPARISON_COLUMNS: tuple[str, ...] = (
    "report_file", "source_type", "page", "object", "label_as_printed", "value_as_printed", "report_value", "unit",
    "period_label", "period_start", "period_end", "period_grain", "scope", "kpi_id", "kpi_label", "mapping_method",
    "definition_source", "definition", "platform_tables", "sql", "platform_value", "abs_diff", "rel_diff",
    "tolerance", "match_basis", "flag", "status", "status_label", "reason", "reused", "matched_alternative",
    "alternative_value", "metadata_confirmed", "result_id", "observation_id", "doc_id",
)
KPI_COLUMNS: tuple[str, ...] = (
    "kpi_id", "kpi_label", "definition_source", "priority", "verdict", "verdict_detail", "periods_compared",
    "periods_validated", "comparisons", "verified", "review", "blocked", "report_only", "first_period",
    "last_period", "unit", "definition", "platform_tables", "hint_table", "hint_value_sql", "hint_notes", "hint_row",
    "error",
)
DOCUMENT_COLUMNS: tuple[str, ...] = (
    "report_file", "format", "brand", "report_type", "period_label", "period_start", "period_end", "period_grain",
    "version", "status", "metadata_confirmed", "metadata_basis", "needs_confirmation", "pages", "extracted_values",
    "comparisons", "superseded_by", "error", "file_path", "doc_id",
)
EXTRACTION_COLUMNS: tuple[str, ...] = (
    "report_file", "source_type", "page", "object", "location", "label", "row_label", "col_header", "series",
    "category", "text_line", "value_as_printed", "raw_number", "number_format", "confidence", "flags", "kpi_id",
    "status", "in_focus", "reason", "value_id", "observation_id", "doc_id",
)
SQL_COLUMNS: tuple[str, ...] = (
    "kpi_id", "kpi_label", "definition_source", "definition", "choices", "scope", "period_grain", "governed_default",
    "alternative", "platform_tables",
    "value_sql", "components_sql", "checks_sql", "function_sql", "export_path", "comparisons", "artifact_id",
    "sql_sha",
)
ISSUE_COLUMNS: tuple[str, ...] = (
    "severity", "category", "report_file", "kpi_id", "item", "message", "remedy", "count", "doc_id", "result_id",
)
PLATFORM_COLUMNS: tuple[str, ...] = (
    "kpi_id", "kpi_label", "definition", "choices", "scope", "period_grain", "period_start", "period_end", "brand",
    "value", "unit", "components", "error", "computed_at", "artifact_id",
)
FRAME_NAMES: tuple[str, ...] = ("comparisons", "kpis", "documents", "extraction", "sql", "issues", "platform_values")
CHANGE_COLUMNS: tuple[str, ...] = (
    "change", "report_file", "kpi_id", "kpi_label", "period_end", "scope", "previous_status", "status",
    "previous_report_value", "report_value", "previous_platform_value", "platform_value", "previous_run_id",
    "result_id", "observation_id", "doc_id",
)
REUSED_TOOLTIP = ("Taken from an earlier run's saved result: the report, KPI definition, bindings, targets and "
                  "platform data are unchanged, so no platform SQL ran for it.")

UNCONFIRMED_ACTOR = "notebook:proposals-unconfirmed"

FLAG_BY_STATUS: dict[str, str] = {
    "VERIFIED": "GOOD", "REPORT_ONLY": "GOOD", "UNMAPPED_CAPTURED": "INFO",
    "VARIANCE_REVIEW": "REVIEW", "INCOMPATIBLE_DEFINITION": "REVIEW", "UNCERTAIN_EXTRACTION": "REVIEW",
    "AMBIGUOUS_MAPPING": "REVIEW", "REPORT_CONFLICT": "REVIEW",
    "MISSING_ACCESS": "BLOCKED", "PLATFORM_DATA_ISSUE": "BLOCKED", "BLOCKED": "BLOCKED",
}
FLAG_ORDER: tuple[str, ...] = ("REVIEW", "BLOCKED", "GOOD", "INFO")
FLAG_INFO: dict[str, tuple[str, str]] = {
    "GOOD": ("Good", "Matches the platform, or captured from a report KPI that has no platform table."),
    "REVIEW": ("Needs review", "Differs from the platform, uses another definition, or a person must confirm "
                               "the value, the KPI or the period."),
    "BLOCKED": ("Blocked", "The platform value could not be computed: no access, missing table or data, or an error."),
    "INFO": ("Other number", "A number that is not on the KPI list; kept as a candidate."),
}

SOURCE_BY_METHOD: dict[str, str] = {
    "NATIVE_CHART": "PowerPoint chart", "NATIVE_TABLE": "PowerPoint table", "EMBEDDED_WORKSHEET": "Embedded Excel",
    "WORKBOOK": "Excel workbook", "SLIDE_TEXT": "Slide text", "PDF_TABLE": "PDF table", "PDF_TEXT": "PDF text",
    "IMAGE_PLACEHOLDER": "Image (not read)", "AI_PARSE": "AI-parsed page", "REVIEWER_CORRECTION": "Reviewer correction",
}
SOURCE_BY_KIND: dict[str, str] = {
    "chart": "PowerPoint chart", "table": "PowerPoint table", "worksheet": "Excel workbook",
    "slide_text": "Slide text", "pdf_table": "PDF table", "pdf_text": "PDF text", "image": "Image (not read)",
    "ai_parse": "AI-parsed page",
}

VERDICTS: tuple[str, ...] = ("not validated", "matches alternative", "partly validated", "not computable",
                             "validated", "report only", "not compared")
VERDICT_INFO: dict[str, tuple[str, str]] = {
    "validated": ("GOOD", "Every report value in every compared period matches the platform under the primary "
                          "definition."),
    "partly validated": ("REVIEW", "Some report values match the platform under the primary definition; others in "
                                   "the same or other periods do not."),
    "matches alternative": ("REVIEW", "No period matches the primary definition, but a declared alternative "
                                      "(another candidate table or measure) reproduces the reports."),
    "not validated": ("REVIEW", "Compared with the platform; the gaps are not explained by any declared definition."),
    "not computable": ("BLOCKED", "The platform SQL could not run (missing table or column, no access, or no data)."),
    "report only": ("INFO", "No platform table is declared for this KPI; report values were captured as printed."),
    "not compared": ("INFO", "No report value was mapped to this KPI in these reports."),
}

_COMPARED = {"VERIFIED", "VARIANCE_REVIEW", "MISSING_ACCESS", "PLATFORM_DATA_ISSUE", "BLOCKED"}
_COMPILE_ERRORS = {"window_unsupported", "scope_unsupported", "binding_missing", "brand_unknown"}
_TEXT_METHODS = {"SLIDE_TEXT", "PDF_TEXT"}
_DECK_FORMATS = {"PPTX", "PPTM"}


def is_missing(value: Any) -> bool:
    """True for None, NaN, NaT and pandas NA."""
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    try:
        return bool(pd.isna(value)) if not isinstance(value, (list, dict, tuple, set)) else False
    except (TypeError, ValueError):
        return False


def clean(value: Any) -> Any:
    """``None`` for missing values, else the value unchanged."""
    return None if is_missing(value) else value


def fmt_value(value: Any, unit: str | None) -> str:
    """Plain display of a number in its unit: 5,736.36 / 9.13% / $1,200 (empty for missing)."""
    from .reconcile import fmt_value as _fmt

    value = clean(value)
    if value is None:
        return ""
    try:
        return _fmt(float(value), unit)
    except (TypeError, ValueError):
        return str(value)


def fmt_rel(value: Any) -> str:
    """Signed relative difference: +0.6% / -31.6% (empty for missing)."""
    value = clean(value)
    if value is None:
        return ""
    pct = round(float(value) * 100, 1)
    return "0.0%" if pct == 0 else f"{pct:+.1f}%"


def as_date(value: Any) -> date | None:
    """A ``date`` from a date, datetime, pandas Timestamp or ISO text; None otherwise."""
    value = clean(value)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except (TypeError, ValueError):
            return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def status_label(status: str | None) -> str:
    """Plain name of a validation status (from ``contracts.STATUS_INFO``)."""
    try:
        return STATUS_INFO[ValidationStatus(status)].label
    except (ValueError, KeyError):
        return str(status or "")


def status_tooltip(status: str | None) -> str:
    """One-sentence tooltip of a validation status (from ``contracts.STATUS_INFO``)."""
    try:
        return STATUS_INFO[ValidationStatus(status)].tooltip
    except (ValueError, KeyError):
        return ""


def flag_for(status: str | None) -> str:
    """GOOD | INFO | REVIEW | BLOCKED for a validation status (BLOCKED for unknown values)."""
    return FLAG_BY_STATUS.get(str(status or ""), "BLOCKED")


def source_type(method: Any, context: dict[str, Any] | None = None) -> str:
    """Plain source type ("PowerPoint chart", "PDF text", ...) from the extraction method and object kind."""
    key = getattr(method, "value", method)
    if key and str(key) in SOURCE_BY_METHOD:
        return SOURCE_BY_METHOD[str(key)]
    kind = str((context or {}).get("object_kind") or "")
    return SOURCE_BY_KIND.get(kind, "Unknown")


def scope_text(scope_key: str | None) -> str:
    """``total`` for the brand total, else ``region=West; territory=T1``."""
    try:
        scope = json.loads(scope_key or "{}")
    except (TypeError, ValueError):
        return str(scope_key)
    if not isinstance(scope, dict) or not scope:
        return "total"
    parts = []
    for dim, value in sorted(scope.items()):
        shown = ", ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)
        parts.append(f"{dim}={shown}")
    return "; ".join(parts)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_RELATION = re.compile(r"\b(?:FROM|JOIN)\s+((?:`[^`]+`|\"[^\"]+\"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:`[^`]+`|\"[^\"]+\"|[A-Za-z_][\w$]*))*)",
                       re.IGNORECASE)
_NOT_A_RELATION = re.compile(r"(?:\b(?:EXTRACT|TRIM|SUBSTRING|SUBSTR|POSITION|OVERLAY)\s*\([^()]*|\bDISTINCT\s+)$",
                             re.IGNORECASE)
_SQL_WORDS = {"select", "where", "lateral", "unnest", "values", "table"}


def relations(sql: str | None) -> list[str]:
    """Tables and views a SQL text reads (FROM / JOIN targets, unique, in order of appearance)."""
    if not sql:
        return []
    text = _LINE_COMMENT.sub(" ", _STRING_LITERAL.sub("''", str(sql)))
    out: list[str] = []
    for m in _RELATION.finditer(text):
        if _NOT_A_RELATION.search(text[:m.start()]):
            continue
        name = re.sub(r"\s+", "", m.group(1)).replace("`", "").replace('"', "")
        if name.lower() in _SQL_WORDS:
            continue
        if name not in out:
            out.append(name)
    return out


def definition_source(kpi: Any, kpi_id: str | None) -> str:
    """``hint`` (KPI table row), ``registry`` (governed definition) or ``none`` (no KPI)."""
    if kpi is not None and str((kpi.raw or {}).get("definition_source") or "").lower() == "hint":
        return "hint"
    return "registry" if kpi_id else "none"


def governed_defaults(kpi: Any) -> dict[str, str]:
    """The primary (default) option of every declared parameter of a KPI ({} without a platform spec)."""
    if kpi is None or kpi.platform is None:
        return {}
    return {name: p.default for name, p in kpi.platform.params.items()}


def definition_text(kpi: Any, choices: dict[str, Any] | None, kpi_id: str | None = None) -> str:
    """KPI label plus the definition choices used, e.g. ``TRx market share - basket=il17_class (IL-17 class only)``."""
    if kpi is None:
        return str(kpi_id or "")
    if kpi.platform is None:
        return f"{kpi.label} - no platform definition (report only)"
    params = kpi.platform.params
    if not params:
        origin = "KPI table" if definition_source(kpi, kpi.kpi_id) == "hint" else "governed"
        return f"{kpi.label} - single {origin} definition"
    chosen = {**governed_defaults(kpi), **{str(k): str(v) for k, v in (choices or {}).items()}}
    parts = []
    for name, p in params.items():
        opt = chosen.get(name, p.default)
        text = f"{name}={opt}"
        if p.describe.get(opt):
            text += f" ({p.describe[opt]})"
        if opt != p.default:
            text += " [permitted variant]" if opt in p.permitted else " [alternative]"
        parts.append(text)
    return f"{kpi.label} - " + "; ".join(parts)


def _kpi_priority(kpi: Any) -> int | None:
    from .reconcile import kpi_priority

    return kpi_priority(kpi)


def _priority_key(priority: int | None) -> tuple[bool, int]:
    return priority is None, priority or 0


def hint_fields(kpi: Any) -> dict[str, Any]:
    """The KPI table row behind a hint KPI (table(s), value SQL, notes, row number) from ``KpiDefinition.raw``."""
    empty = {"table": "", "value_sql": "", "notes": "", "row": None}
    if kpi is None or definition_source(kpi, kpi.kpi_id) != "hint":
        return empty
    raw = kpi.raw or {}
    row = raw.get("hint") if isinstance(raw.get("hint"), dict) else {}

    def pick(*names: str) -> Any:
        for name in names:
            for source in (row, raw):
                value = source.get(name)
                if not is_missing(value) and value not in ("", [], {}):
                    return value
        return None

    table = pick("tables", "table")
    if isinstance(table, (list, tuple)):
        table = "; ".join(str(t) for t in table)
    value_sql = pick("value_sql")
    if value_sql is None:
        num, den = pick("numerator"), pick("denominator")
        if num is not None or den is not None:
            value_sql = f"({num or '?'}) / ({den or '?'})"
    number = raw.get("hint_row")
    try:
        number = int(number) if number is not None and not is_missing(number) else None
    except (TypeError, ValueError):
        number = None
    return {"table": str(table or ""), "value_sql": str(value_sql or ""),
            "notes": str(pick("notes", "hint_notes") or ""), "row": number}


def metadata_state(doc: Document | None) -> tuple[bool, str]:
    """(metadata_confirmed, plain basis)."""
    if doc is None:
        return False, "document not found"
    actor = str(doc.confirmed_by or "")
    if actor and actor != UNCONFIRMED_ACTOR:
        return True, f"confirmed by {actor}"
    if actor == UNCONFIRMED_ACTOR:
        return False, "proposed metadata used without confirmation"
    if doc.status == DocumentStatus.AWAITING_CONFIRMATION or doc.needs_confirmation:
        fields = ", ".join(str(f) for f in (doc.needs_confirmation or [])) or "metadata"
        return False, f"awaiting confirmation ({fields})"
    if doc.status in (DocumentStatus.UNSUPPORTED_FORMAT, DocumentStatus.ENCRYPTED, DocumentStatus.REGISTERED):
        return False, "not read"
    if doc.status == DocumentStatus.FAILED and doc.failed_stage in ("register", "extract"):
        return False, "not read (extraction failed)"
    return True, "proposed with confidence (no confirmation needed)"


def _proposal_value(doc: Document | None, name: str) -> Any:
    item = ((doc.metadata_proposals or {}).get(name) if doc is not None else None) or {}
    return item.get("value") if isinstance(item, dict) else None


def _page_word(doc: Document | None) -> str:
    return "slide" if doc is not None and getattr(doc.format, "value", doc.format) in _DECK_FORMATS else "page"


def page_of(value: ExtractedValue | None) -> int | None:
    """Slide or page number of an extracted value (from the column, else the locator)."""
    if value is None:
        return None
    if value.slide_or_page is not None:
        return int(value.slide_or_page)
    loc = value.locator or {}
    for key in ("slide", "page"):
        if loc.get(key) is not None:
            try:
                return int(loc[key])
            except (TypeError, ValueError):
                return None
    return None


def object_of(value: ExtractedValue | None) -> str:
    """Chart/table title, sheet name or text line of an extracted value."""
    if value is None:
        return ""
    ctx, loc = value.context or {}, value.locator or {}
    method = getattr(value.method, "value", value.method)
    if method in _TEXT_METHODS and ctx.get("text_line"):
        return str(ctx["text_line"])
    for candidate in (ctx.get("object_title"), loc.get("sheet"), ctx.get("text_line"), ctx.get("page_title")):
        if candidate:
            return str(candidate)
    return "Image" if method == "IMAGE_PLACEHOLDER" else ""


def context_label(ctx: dict[str, Any] | None) -> str:
    """The label printed next to a value (row label, series name or category)."""
    ctx = ctx or {}
    for key in ("row_label", "series_name", "category", "text_line"):
        if ctx.get(key):
            return str(ctx[key])
    return ""


def location_text(value: ExtractedValue | None, doc: Document | None = None) -> str:
    """Readable locator: ``slide 3, shape 'KPI table', row 2, column 3``."""
    if value is None:
        return ""
    loc = value.locator or {}
    parts = []
    page = page_of(value)
    if page is not None:
        parts.append(f"{_page_word(doc)} {page}")
    if loc.get("shape_name"):
        parts.append(f"shape '{loc['shape_name']}'")
    elif loc.get("shape_id") is not None:
        parts.append(f"shape {loc['shape_id']}")
    if loc.get("chart_index") is not None:
        parts.append(f"chart {loc['chart_index']}")
    if loc.get("table_index") is not None:
        parts.append(f"table {int(loc['table_index']) + 1}")
    if loc.get("series_index") is not None:
        parts.append(f"series {int(loc['series_index']) + 1}")
    if loc.get("point_index") is not None:
        parts.append(f"point {int(loc['point_index']) + 1}")
    if loc.get("sheet") and loc.get("cell"):
        parts.append(f"cell {loc['sheet']}!{loc['cell']}")
    elif loc.get("row") is not None and loc.get("col") is not None:
        parts.append(f"row {int(loc['row']) + 1}, column {int(loc['col']) + 1}")
    if loc.get("line") is not None:
        parts.append(f"line {int(loc['line']) + 1}")
    if loc.get("image_index") is not None:
        parts.append(f"image {int(loc['image_index']) + 1}")
    return ", ".join(parts)


def flags_text(flags: list | None) -> str:
    """``CODE: detail`` per flag, separated by ``; ``."""
    out = []
    for f in flags or []:
        if isinstance(f, dict) and f.get("code"):
            out.append(f"{f['code']}: {f['detail']}" if f.get("detail") else str(f["code"]))
    return "; ".join(out)


def alternative_text(explanation: dict[str, Any] | None) -> str | None:
    """The declared alternative that matched, with each option's description."""
    explanation = explanation or {}
    diff = explanation.get("differing_options")
    if isinstance(diff, dict) and diff:
        parts = []
        for name, change in diff.items():
            change = change if isinstance(change, dict) else {"to": change}
            text = f"{name}={change.get('to')}"
            if change.get("describe"):
                text += f" ({change['describe']})"
            parts.append(text)
        return ", ".join(parts)
    alt = explanation.get("alternative")
    return str(alt) if alt else None


def choices_text(choices: dict[str, Any] | None) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted((choices or {}).items())) or "(no options)"


def _same_choices(trial_choices: dict[str, Any] | None, start: dict[str, Any] | None) -> bool:
    a, b = dict(trial_choices or {}), dict(start or {})
    return a == b or all(a.get(k) == v for k, v in b.items())


def template_sql(artifact: SqlArtifact, as_of: date) -> str:
    """The artifact's SQL with ``{as_of}`` substituted (both components for a ratio)."""
    iso = as_of.isoformat()
    comps = artifact.components_sql or {}
    if "numerator" in comps and "denominator" in comps:
        return (f"-- numerator\n{str(comps['numerator']).replace('{as_of}', iso)};\n"
                f"-- denominator\n{str(comps['denominator']).replace('{as_of}', iso)}")
    return str(artifact.value_sql_template or "").replace("{as_of}", iso)


@dataclass
class EvidenceIndex:
    """Store records of one check, keyed for lookup (kept on the result so it works without the store)."""

    docs: dict[str, Document] = field(default_factory=dict)
    pages: dict[tuple[str, int], DocumentPage] = field(default_factory=dict)
    values: dict[str, ExtractedValue] = field(default_factory=dict)
    extraction_issues: list[ExtractionIssue] = field(default_factory=list)
    observations: dict[str, Observation] = field(default_factory=dict)
    mappings: dict[str, KpiMapping] = field(default_factory=dict)
    results: dict[str, KpiResult] = field(default_factory=dict)
    runs: dict[str, ReconRun] = field(default_factory=dict)
    trials: dict[str, list[ReconTrial]] = field(default_factory=dict)
    artifacts: dict[str, SqlArtifact] = field(default_factory=dict)
    series: list[PlatformKpiValue] = field(default_factory=list)
    kpis: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    focus: frozenset[str] | None = None
    reused: set[str] = field(default_factory=set)

    def value_of(self, result: KpiResult) -> ExtractedValue | None:
        obs = self.observations.get(result.observation_id)
        return self.values.get(obs.value_id) if obs is not None else None

    def page_of(self, result: KpiResult) -> DocumentPage | None:
        page = page_of(self.value_of(result))
        return self.pages.get((result.doc_id, page)) if page is not None else None


def executed_sql(index: EvidenceIndex, result: KpiResult) -> str:
    """The SQL that ran for the result's platform value (Phase A trial at its period, start choices)."""
    period = as_date(result.period_end)
    trials = index.trials.get(result.recon_run_id or "", [])
    if period is not None and trials:
        run = index.runs.get(result.recon_run_id or "")
        start = dict(result.definition_choices or (run.start_choices if run is not None else {}) or {})
        phase_a = [t for t in trials if t.phase == "phase_a" and as_date(t.as_of) == period and t.sql]
        pick = [t for t in phase_a if _same_choices(t.choices, start)] or phase_a
        if pick:
            ok = [t for t in pick if t.error is None and t.value is not None]
            return str((ok or pick)[-1].sql)
    artifact = index.artifacts.get(result.sql_artifact_id or "")
    if artifact is not None and period is not None:
        return template_sql(artifact, period)
    return ""


def alternatives_for(index: EvidenceIndex, result: KpiResult) -> list[dict[str, Any]]:
    """Every other declared definition tested for the result's period (Phase B and grid), deduplicated."""
    period = as_date(result.period_end)
    if period is None:
        return []
    trials = index.trials.get(result.recon_run_id or "", [])
    start = dict(result.definition_choices or {})
    applied = (result.tolerance or {}).get("applied")
    kpi = index.kpis.get(result.kpi_id or "")
    seen: dict[str, dict[str, Any]] = {}
    for t in sorted(trials, key=lambda t: t.seq):
        if t.phase not in ("phase_b", "grid") or as_date(t.as_of) != period or _same_choices(t.choices, start):
            continue
        key = canonical_json(t.choices or {})
        prior = seen.get(key)
        if prior is not None and (prior["value"] is not None or t.value is None):
            continue
        value = clean(t.value)
        matches = None
        if value is not None and result.report_value is not None and applied is not None:
            matches = abs(float(value) - float(result.report_value)) <= float(applied) + 1e-12 * max(
                1.0, abs(float(result.report_value)))
        differs = {k: v for k, v in (t.choices or {}).items() if start.get(k) != v}
        seen[key] = {"choices": dict(t.choices or {}), "differs": differs,
                     "label": choices_text(differs) if differs else choices_text(t.choices),
                     "definition": definition_text(kpi, t.choices, result.kpi_id), "value": value,
                     "error": t.error, "matches_report": matches, "sql": t.sql or "", "phase": t.phase}
    return list(seen.values())


def matches_alternative(result: KpiResult) -> bool:
    """True when only a declared alternative definition reproduces the result's report value."""
    return str(result.reason_code or "").startswith("matches_alternative")


def alternative_sql(index: EvidenceIndex, result: KpiResult) -> str:
    """The SQL that computed the matching alternative value, or empty when there is none."""
    explanation = result.explanation or {}
    target = clean(explanation.get("alternative_value"))
    if target is None or not matches_alternative(result):
        return ""
    if explanation.get("alternative_sql"):
        return str(explanation["alternative_sql"])
    period = as_date(result.period_end)
    matched = explanation.get("matched_choices")
    trials = [t for t in index.trials.get(result.recon_run_id or "", [])
              if t.sql and t.phase in ("phase_b", "grid") and as_date(t.as_of) == period
              and isinstance(matched, dict) and dict(t.choices or {}) == matched]
    same = [t for t in trials if t.error is None and clean(t.value) is not None
            and math.isclose(float(t.value), float(target), rel_tol=1e-9, abs_tol=1e-12)]
    pick = sorted(same or trials, key=lambda t: t.seq)
    return str(pick[-1].sql) if pick else ""


def durable_path(path: Any) -> str:
    """The path, or empty when it is missing or inside the system temporary folder."""
    text = str(clean(path) or "").strip()
    if not text or text.startswith(("/Volumes/", "dbfs:/")):
        return text
    try:
        here = os.path.normcase(str(Path(text).resolve()))
        temp = os.path.normcase(str(Path(tempfile.gettempdir()).resolve()))
    except (OSError, ValueError):
        return text
    return "" if here == temp or here.startswith(temp.rstrip(os.sep) + os.sep) else text


@dataclass(repr=False)
class CheckResult:
    """The outcome of one check: comparison, KPI and supporting frames plus HTML / Excel / Spark outputs."""

    comparisons: pd.DataFrame
    kpis: pd.DataFrame
    documents: pd.DataFrame
    extraction: pd.DataFrame
    sql: pd.DataFrame
    issues: pd.DataFrame
    summary: dict
    platform_values: pd.DataFrame = field(default_factory=lambda: _frame([], PLATFORM_COLUMNS))
    _index: EvidenceIndex | None = field(default=None, compare=False)
    _previous: list[dict[str, Any]] = field(default_factory=list, compare=False)

    def __repr__(self) -> str:
        return self.text_summary()

    def text_summary(self) -> str:
        """A few plain lines: counts per tile and per verdict, and where to look next."""
        s = self.summary or {}
        tiles = s.get("tiles") or {}
        lines = [f"KPI check {s.get('run_id') or ''}".rstrip() + f": {len(self.comparisons)} comparison(s) from "
                 f"{s.get('documents', len(self.documents))} report file(s)",
                 "  " + ", ".join(f"{name}: {tiles.get(key, 0)}" for key, name in TILE_NAMES)]
        verdicts = s.get("verdicts") or {}
        if verdicts:
            lines.append("  KPI verdicts: " + ", ".join(f"{k} {v}" for k, v in verdicts.items()))
        if len(self.issues):
            lines.append(f"  {len(self.issues)} issue(s): see result.issues")
        lines.append("  result.comparisons (one row per comparison), result.save_html(path), result.save_excel(path)")
        return "\n".join(lines)

    def show(self) -> None:
        """Render the HTML report inline (Databricks ``displayHTML``, else IPython), else print a text summary."""
        html_text = self.to_html()
        display_html = _find_display_html()
        if display_html is not None:
            display_html(html_text)
            return
        try:
            from IPython import get_ipython
            from IPython.display import HTML, display
        except ImportError:
            print(self.text_summary())
            return
        if get_ipython() is None:
            print(self.text_summary())
            return
        display(HTML(html_text))

    def to_html(self, *, title: str | None = None) -> str:
        """The self-contained visual report (inline CSS/JS/SVG; no external resources)."""
        from .report_html import render

        return render(self, title=title)

    def save_html(self, path: str | Path, *, title: str | None = None) -> str:
        """Write the HTML report (parent folders are created) and return the path."""
        from .store.state_file import state_location

        target = state_location(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.to_html(title=title), encoding="utf-8")
        return str(target)

    def save_excel(self, path: str | Path) -> str:
        """Write the Excel workbook and return the path."""
        from .report_excel import write

        return write(self, path)

    def save_sql(self, folder: str | Path) -> list[str]:
        """Write one read-only .sql file per row of ``sql`` into ``folder`` and return the file paths."""
        from .store.state_file import state_location

        root = state_location(folder)
        written = []
        for row in self.sql.to_dict(orient="records"):
            kpi = _SAFE_NAME.sub("_", str(row.get("kpi_id") or "kpi"))
            target = root / kpi / sql_file_name(row)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(sql_file_text(row), encoding="utf-8")
            written.append(str(target))
        return written

    def frames(self) -> dict[str, pd.DataFrame]:
        """The report frames by name."""
        return {name: getattr(self, name) for name in FRAME_NAMES}

    def to_spark(self, spark: Any = None) -> dict[str, Any]:
        """Spark DataFrames of the report frames (explicit schemas; mixed object columns become strings)."""
        if spark is None:
            try:
                from pyspark.sql import SparkSession
            except ImportError as exc:
                raise RuntimeError("pyspark is not available; pass a SparkSession as to_spark(spark)") from exc
            spark = SparkSession.getActiveSession()
            if spark is None:
                raise RuntimeError("no active SparkSession; pass one as to_spark(spark)")
        out = {}
        for name, frame in self.frames().items():
            rows, schema = spark_rows(frame)
            out[name] = spark.createDataFrame(rows, schema=schema)
        return out

    def filter(self, *, flag: str | list[str] | None = None, kpi: str | list[str] | None = None,
               report: str | list[str] | None = None) -> pd.DataFrame:
        """Comparison rows by flag (GOOD/REVIEW/...), KPI (id or label) and/or report (file name, part of it, or doc id)."""
        df = self.comparisons
        if df.empty:
            return df.copy()
        mask = pd.Series(True, index=df.index)
        if flag is not None:
            flags = {str(f).upper() for f in ([flag] if isinstance(flag, str) else flag)}
            mask &= df["flag"].astype(str).str.upper().isin(flags)
        if kpi is not None:
            wanted = {str(k).lower() for k in ([kpi] if isinstance(kpi, str) else kpi)}
            mask &= (df["kpi_id"].fillna("").astype(str).str.lower().isin(wanted)
                     | df["kpi_label"].fillna("").astype(str).str.lower().isin(wanted))
        if report is not None:
            names = [str(r).lower() for r in ([report] if isinstance(report, str) else report)]
            files = df["report_file"].fillna("").astype(str).str.lower()
            ids = df["doc_id"].fillna("").astype(str).str.lower()
            hit = pd.Series(False, index=df.index)
            for name in names:
                hit |= files.str.contains(name, regex=False) | (ids == name)
            mask &= hit
        return df[mask].copy()

    def changes_since(self, previous_run_id: str | None = None) -> pd.DataFrame:
        """Comparisons new, changed or removed since a run (default: the latest earlier run on these reports)."""
        runs = self._previous or []
        if previous_run_id is None:
            start = 0 if runs else None
        else:
            start = next((i for i, r in enumerate(runs) if r.get("run_id") == previous_run_id), None)
            if start is None:
                known = ", ".join(str(r.get("run_id")) for r in runs) or "none"
                raise KeyError(f"no earlier run {previous_run_id!r} for these reports (earlier runs: {known})")
        before = _baseline(runs, start)
        run_id = runs[start].get("run_id") if start is not None else None
        rows: list[dict[str, Any]] = []
        current: set[str] = set()
        for row in (self.comparisons.to_dict(orient="records") if len(self.comparisons) else []):
            rid = str(row["result_id"])
            current.add(rid)
            old, old_run = before.get(rid, (None, run_id))
            if old is None:
                change = "new"
            elif (old.get("status") != row["status"] or not _same_number(old.get("report_value"), row["report_value"])
                  or not _same_number(old.get("platform_value"), row["platform_value"])):
                change = "changed"
            else:
                continue
            rows.append(_change_row(change, row, old or {}, old_run))
        focus = set((self.summary or {}).get("focus") or []) or None
        doc_ids = set(self.documents["doc_id"]) if "doc_id" in self.documents.columns else set()
        for rid, (old, old_run) in before.items():
            if rid in current or old.get("doc_id") not in doc_ids or (focus is not None and old.get("kpi_id") not in focus):
                continue
            rows.append(_change_row("removed", {"result_id": rid}, old, old_run))
        order = {"changed": 0, "new": 1, "removed": 2}
        rows.sort(key=lambda r: (order[r["change"]], str(r["kpi_id"] or ""), str(r["period_end"] or ""),
                                 str(r["report_file"] or "")))
        return _frame(rows, CHANGE_COLUMNS)

    def evidence(self, key: int | str) -> dict[str, Any]:
        """Everything behind one comparison: row index, ``result_id`` or ``observation_id``."""
        row = self._row_for(key)
        index = self._index or EvidenceIndex()
        result = index.results.get(str(row.get("result_id") or ""))
        out: dict[str, Any] = {"comparison": row, "status_tooltip": status_tooltip(row.get("status"))}
        if result is None:
            out.update(observation=None, extracted_value=None, page=None, recon_run=None, trials=[],
                       alternatives=[], sql_artifact=None, explanation={}, mapping=None)
            return out
        obs = index.observations.get(result.observation_id)
        value = index.value_of(result)
        page = index.page_of(result)
        run = index.runs.get(result.recon_run_id or "")
        artifact = index.artifacts.get(result.sql_artifact_id or "")
        mapping = index.mappings.get(result.observation_id)
        trials = sorted(index.trials.get(result.recon_run_id or "", []), key=lambda t: t.seq)
        out.update(
            result=_record(result),
            observation=_record(obs),
            extracted_value=None if value is None else {
                **_record(value), "location": location_text(value, index.docs.get(result.doc_id)),
                "flags_text": flags_text(value.flags)},
            page=None if page is None else {
                "page_number": page.page_number, "title": page.title, "text_excerpt": _excerpt(page.text),
                "notes_excerpt": _excerpt(page.notes), "hidden": page.hidden},
            recon_run=None if run is None else {
                "recon_run_id": run.recon_run_id, "kpi_id": run.kpi_id, "bindings": run.bindings,
                "start_choices": run.start_choices, "final_choices": run.final_choices,
                "phase_a_outcome": run.phase_a_outcome, "phase_b_outcome": run.phase_b_outcome,
                "judge": run.judge, "error": run.error, "platform_snapshot": run.platform_snapshot},
            trials=[{**_record(t), "for_this_period": as_date(t.as_of) == as_date(result.period_end)} for t in trials],
            alternatives=alternatives_for(index, result),
            sql_artifact=_record(artifact),
            explanation=dict(result.explanation or {}),
            mapping=_record(mapping),
        )
        return out

    def _row_for(self, key: int | str) -> dict[str, Any]:
        df = self.comparisons
        if isinstance(key, (int,)) and not isinstance(key, bool):
            if key in df.index:
                return _row_dict(df.loc[key])
            raise KeyError(f"no comparison row {key}")
        text = str(key)
        for column in ("result_id", "observation_id"):
            hit = df[df[column].astype(str) == text] if column in df.columns and not df.empty else df.iloc[0:0]
            if len(hit):
                return _row_dict(hit.iloc[0])
        raise KeyError(f"no comparison with result_id or observation_id {text!r}")


def _baseline(runs: list[dict[str, Any]], start: int | None) -> dict[str, tuple[dict[str, Any], Any]]:
    before: dict[str, tuple[dict[str, Any], Any]] = {}
    if start is None:
        return before
    covered: set[str] = set()
    for run in runs[start:]:
        for rid, old in (run.get("results") or {}).items():
            if rid not in before and old.get("kpi_id") not in covered:
                before[rid] = (old, run.get("run_id"))
        focus = run.get("focus")
        if focus is None:
            break
        covered |= {str(k) for k in focus}
    return before


def _same_number(a: Any, b: Any) -> bool:
    a, b = clean(a), clean(b)
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(float(a), float(b), rel_tol=1e-12, abs_tol=1e-12)


def _change_row(change: str, row: dict[str, Any], old: dict[str, Any], run_id: Any) -> dict[str, Any]:
    def pick(name: str) -> Any:
        value = clean(row.get(name))
        return old.get(name) if value is None else value

    return {"change": change, "report_file": pick("report_file"), "kpi_id": pick("kpi_id"),
            "kpi_label": pick("kpi_label"), "period_end": as_date(pick("period_end")), "scope": pick("scope"),
            "previous_status": old.get("status"), "status": clean(row.get("status")),
            "previous_report_value": old.get("report_value"), "report_value": clean(row.get("report_value")),
            "previous_platform_value": old.get("platform_value"), "platform_value": clean(row.get("platform_value")),
            "previous_run_id": run_id, "result_id": row.get("result_id"), "observation_id": pick("observation_id"),
            "doc_id": pick("doc_id")}


TILE_NAMES: tuple[tuple[str, str], ...] = (
    ("matches_platform", "Matches platform"), ("captured", "Captured"), ("needs_review", "Needs review"),
    ("blocked", "Blocked"), ("other_numbers", "Other numbers"),
)


def _find_display_html():
    try:
        from IPython import get_ipython

        shell = get_ipython()
        fn = (getattr(shell, "user_ns", None) or {}).get("displayHTML") if shell is not None else None
        if callable(fn):
            return fn
    except ImportError:
        pass
    import builtins
    import sys

    main = sys.modules.get("__main__")
    for holder in (main, builtins):
        fn = getattr(holder, "displayHTML", None)
        if callable(fn):
            return fn
    return None


def _excerpt(text: str | None, limit: int = 600) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _record(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    from dataclasses import fields as dc_fields
    from enum import Enum

    out = {}
    for f in dc_fields(obj):
        value = getattr(obj, f.name)
        out[f.name] = value.value if isinstance(value, Enum) else value
    return out


def _row_dict(series: pd.Series) -> dict[str, Any]:
    return {k: clean(v) for k, v in series.to_dict().items()}


def _spark_kind(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "DOUBLE"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "TIMESTAMP"
    present = [v for v in series.tolist() if not is_missing(v)]
    if present and all(isinstance(v, bool) for v in present):
        return "BOOLEAN"
    if present and all(isinstance(v, datetime) for v in present):
        return "TIMESTAMP"
    if present and all(isinstance(v, date) and not isinstance(v, datetime) for v in present):
        return "DATE"
    if present and all(isinstance(v, int) and not isinstance(v, bool) for v in present):
        return "BIGINT"
    return "STRING"


def _spark_value(value: Any, kind: str) -> Any:
    if is_missing(value):
        return None
    if kind == "STRING":
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, default=str, sort_keys=True)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)
    if kind == "DOUBLE":
        number = float(value)
        return number if math.isfinite(number) else None
    if kind == "BIGINT":
        return int(value)
    if kind == "BOOLEAN":
        return bool(value)
    if kind == "TIMESTAMP":
        return value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    if kind == "DATE":
        return as_date(value)
    return value


def spark_rows(frame: pd.DataFrame) -> tuple[list[tuple], str]:
    """(rows of plain Python values, DDL schema) for ``spark.createDataFrame(rows, schema=ddl)``."""
    kinds = {col: _spark_kind(frame[col]) for col in frame.columns}
    schema = ", ".join(f"`{col}` {kinds[col]}" for col in frame.columns)
    rows = [tuple(_spark_value(v, kinds[col]) for col, v in zip(frame.columns, values, strict=True))
            for values in frame.itertuples(index=False, name=None)]
    return rows, schema


def _chunks(items: list[Any], size: int = 200):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _read_in(store: Any, cls: type, column: str, ids: Any, errors: list[str], *, extra: str = "") -> list[Any]:
    wanted = sorted({str(i) for i in ids if i})
    out: list[Any] = []
    for chunk in _chunks(wanted):
        marks = ", ".join(f":p{i}" for i in range(len(chunk)))
        where = f"{column} IN ({marks})" + (f" AND {extra}" if extra else "")
        try:
            out += store.read(cls, where, {f"p{i}": v for i, v in enumerate(chunk)})
        except Exception as exc:
            errors.append(f"{cls.TABLE}: {type(exc).__name__}: {exc}")
            return out
    return out


def load_index(store: Any, registry: Any, doc_ids: list[str] | None, *, focus_kpis: Any = None) -> EvidenceIndex:
    """Read every record the report needs for ``doc_ids`` (all documents when None; focus KPIs only when given)."""
    from .reconcile import in_focus

    index = EvidenceIndex(focus=None if focus_kpis is None else frozenset(str(k) for k in focus_kpis))
    errors = index.errors
    if doc_ids is None:
        try:
            docs = store.read(Document)
        except Exception as exc:
            errors.append(f"documents: {type(exc).__name__}: {exc}")
            docs = []
    else:
        docs = _read_in(store, Document, "doc_id", doc_ids, errors)
    index.docs = {d.doc_id: d for d in docs}
    ids = list(index.docs) if doc_ids is None else list(dict.fromkeys(doc_ids))
    index.pages = {(p.doc_id, int(p.page_number)): p for p in _read_in(store, DocumentPage, "doc_id", ids, errors)}
    index.values = {v.value_id: v for v in _read_in(store, ExtractedValue, "doc_id", ids, errors)}
    index.extraction_issues = sorted(_read_in(store, ExtractionIssue, "doc_id", ids, errors),
                                     key=lambda i: (i.doc_id, i.seq))
    index.observations = {o.observation_id: o for o in _read_in(store, Observation, "doc_id", ids, errors)}
    version = getattr(registry, "version", None)
    by_obs: dict[str, KpiMapping] = {}
    for m in _read_in(store, KpiMapping, "observation_id", list(index.observations), errors):
        current = by_obs.get(m.observation_id)
        if current is None or (m.registry_version == version and current.registry_version != version) or (
                (m.registry_version == version) == (current.registry_version == version)
                and (m.decided_at or datetime.min) > (current.decided_at or datetime.min)):
            by_obs[m.observation_id] = m
    index.mappings = by_obs
    results = _read_in(store, KpiResult, "doc_id", ids, errors, extra="is_current = TRUE")
    if index.focus is not None:
        results = [r for r in results
                   if r.kpi_id in index.focus or in_focus(index.mappings.get(r.observation_id), index.focus)]
    index.results = {r.result_id: r for r in results}
    run_ids = {r.recon_run_id for r in results if r.recon_run_id}
    index.runs = {r.recon_run_id: r for r in _read_in(store, ReconRun, "recon_run_id", run_ids, errors)}
    trials: dict[str, list[ReconTrial]] = defaultdict(list)
    for t in _read_in(store, ReconTrial, "recon_run_id", run_ids, errors):
        trials[t.recon_run_id].append(t)
    index.trials = dict(trials)
    artifact_ids = {r.sql_artifact_id for r in results if r.sql_artifact_id}
    index.artifacts = {a.artifact_id: a for a in _read_in(store, SqlArtifact, "artifact_id", artifact_ids, errors)}
    index.series = _read_in(store, PlatformKpiValue, "sql_artifact_id", artifact_ids, errors)
    kpis = getattr(registry, "kpis", None) or {}
    index.kpis = dict(kpis)
    return index


def build_result(store: Any, registry: Any, settings: Any, doc_ids: list[str] | None, *, hints: Any = None,
                 run_id: str | None = None, platform_description: str = "",
                 timings: dict[str, Any] | None = None, extra_issues: Any = None, focus_kpis: Any = None,
                 reused_ids: Any = None, previous_runs: list[dict[str, Any]] | None = None) -> CheckResult:
    """Assemble the ``CheckResult`` for the documents of one check from the state store."""
    index = load_index(store, registry, doc_ids, focus_kpis=focus_kpis)
    index.reused = {str(r) for r in (reused_ids or [])}
    issues: list[dict[str, Any]] = []
    frames: dict[str, pd.DataFrame] = {}
    builders = (("comparisons", lambda: comparisons_frame(index)),
                ("documents", lambda: documents_frame(index)),
                ("extraction", lambda: extraction_frame(index)),
                ("sql", lambda: sql_frame(index)),
                ("platform_values", lambda: platform_frame(index)))
    columns = {"comparisons": COMPARISON_COLUMNS, "documents": DOCUMENT_COLUMNS, "extraction": EXTRACTION_COLUMNS,
               "sql": SQL_COLUMNS, "kpis": KPI_COLUMNS, "platform_values": PLATFORM_COLUMNS}
    for name, build in builders:
        frames[name] = _safe_frame(name, build, columns[name], issues)
    frames["kpis"] = _safe_frame("kpis", lambda: kpis_frame(index, registry), KPI_COLUMNS, issues)
    try:
        issues = issue_rows(index, hints) + issues
    except Exception as exc:
        issues.append(_issue("error", "report", item="issues", message=f"Issues could not be listed: "
                                                                        f"{type(exc).__name__}: {exc}"))
    issues += [_extra_issue(m) for m in (extra_issues or [])]
    issues_df = _frame(_sort_issues(issues), ISSUE_COLUMNS)
    summary = summary_dict(index, frames, issues_df, hints=hints, run_id=run_id,
                           platform_description=platform_description, timings=timings, settings=settings,
                           state_saved=getattr(store, "path", None) != ":memory:")
    return CheckResult(comparisons=frames["comparisons"], kpis=frames["kpis"], documents=frames["documents"],
                       extraction=frames["extraction"], sql=frames["sql"], issues=issues_df, summary=summary,
                       platform_values=frames["platform_values"], _index=index, _previous=list(previous_runs or []))


def _extra_issue(message: Any) -> dict[str, Any]:
    row = dict(message) if isinstance(message, dict) else {"message": str(message)}
    out = _issue(str(row.get("severity") or "warning"), str(row.get("category") or "check"))
    out.update({k: clean(row.get(k)) for k in ISSUE_COLUMNS if k in row and clean(row.get(k)) is not None})
    try:
        out["count"] = int(out.get("count") or 1)
    except (TypeError, ValueError):
        out["count"] = 1
    for key in ("report_file", "item", "message", "remedy"):
        out[key] = "" if out.get(key) is None else str(out[key])
    return out


def _safe_frame(name: str, build, columns: tuple[str, ...], issues: list[dict[str, Any]]) -> pd.DataFrame:
    try:
        return build()
    except Exception as exc:
        issues.append(_issue("error", "report", item=name,
                             message=f"The {name} table could not be built: {type(exc).__name__}: {exc}",
                             remedy="Report this error; the other tables are complete."))
        return _frame([], columns)


def _frame(rows: list[dict[str, Any]], columns: tuple[str, ...], *, int_columns: tuple[str, ...] = ()) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=list(columns))
    for col in int_columns:
        if col in df.columns:
            df[col] = pd.array([None if is_missing(v) else int(v) for v in df[col].tolist()], dtype="Int64")
    return df.reset_index(drop=True)


def _tolerance_text(kpi: Any, result: KpiResult) -> str:
    if kpi is None or result.report_value is None or (result.platform_value is None
                                                      and not (result.explanation or {}).get("alternative_value")):
        return ""
    from .reconcile import tolerance_text

    try:
        half = float((result.tolerance or {}).get("half_unit") or 0.0)
        return tolerance_text(kpi, float(result.report_value), half)
    except Exception:
        return ""


def _mapping_method(mapping: KpiMapping | None) -> str:
    if mapping is None:
        return ""
    method = getattr(mapping.method, "value", mapping.method)
    status = getattr(mapping.status, "value", mapping.status)
    return str(method or status or "")


def comparison_row(index: EvidenceIndex, result: KpiResult) -> dict[str, Any]:
    """One comparison row for a current result."""
    doc = index.docs.get(result.doc_id)
    obs = index.observations.get(result.observation_id)
    value = index.value_of(result)
    ctx = (value.context if value is not None else {}) or {}
    kpi = index.kpis.get(result.kpi_id or "")
    status = getattr(result.validation_status, "value", result.validation_status)
    explanation = result.explanation or {}
    sql = executed_sql(index, result)
    method = result.extraction_method or (value.method if value is not None else None)
    choices = result.definition_choices or ({} if kpi is None else governed_defaults(kpi))
    return {
        "report_file": doc.file_name if doc is not None else "",
        "source_type": source_type(method, ctx),
        "page": page_of(value),
        "object": object_of(value),
        "label_as_printed": (obs.label if obs is not None and obs.label else context_label(ctx)),
        "value_as_printed": value.raw_text if value is not None else None,
        "report_value": result.report_value,
        "unit": result.unit or (obs.unit if obs is not None else None),
        "period_label": obs.period_label if obs is not None else None,
        "period_start": as_date(result.period_start),
        "period_end": as_date(result.period_end),
        "period_grain": result.period_grain,
        "scope": scope_text(result.scope_key),
        "kpi_id": result.kpi_id,
        "kpi_label": kpi.label if kpi is not None else (result.kpi_id or ""),
        "mapping_method": _mapping_method(index.mappings.get(result.observation_id)),
        "definition_source": definition_source(kpi, result.kpi_id),
        "definition": definition_text(kpi, choices, result.kpi_id) if result.kpi_id else "",
        "platform_tables": "; ".join(relations(sql)),
        "sql": sql,
        "platform_value": result.platform_value,
        "abs_diff": result.abs_diff,
        "rel_diff": result.rel_diff,
        "tolerance": _tolerance_text(kpi, result),
        "match_basis": getattr(result.match_basis, "value", result.match_basis),
        "flag": flag_for(status),
        "status": status,
        "status_label": status_label(status),
        "reason": result.reason_detail,
        "reused": result.result_id in index.reused,
        "matched_alternative": alternative_text(explanation),
        "alternative_value": clean(explanation.get("alternative_value")),
        "metadata_confirmed": metadata_state(doc)[0],
        "result_id": result.result_id,
        "observation_id": result.observation_id,
        "doc_id": result.doc_id,
        "_priority": _kpi_priority(kpi),
    }


def _comparison_sort_key(row: dict[str, Any]) -> tuple:
    flag = FLAG_ORDER.index(row["flag"]) if row["flag"] in FLAG_ORDER else len(FLAG_ORDER)
    kpi = str(row["kpi_label"] or row["kpi_id"] or "")
    period = row["period_end"]
    page = row["page"]
    return (*_priority_key(row.get("_priority")), flag, kpi == "", kpi.lower(), period is None, period or date.min,
            str(row["report_file"]).lower(), page is None, page or 0, str(row["object"]), str(row["label_as_printed"]))


def comparisons_frame(index: EvidenceIndex) -> pd.DataFrame:
    """One row per current result, sorted: priority, flag (REVIEW, BLOCKED, GOOD, INFO), KPI, period, report, page."""
    rows = sorted((comparison_row(index, r) for r in index.results.values()), key=_comparison_sort_key)
    df = _frame(rows, COMPARISON_COLUMNS, int_columns=("page",))
    for col in ("report_value", "platform_value", "abs_diff", "rel_diff", "alternative_value"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    df["metadata_confirmed"] = df["metadata_confirmed"].astype(bool)
    df["reused"] = df["reused"].astype(bool)
    return df


def _effective(result: KpiResult) -> str:
    from .reconcile import effective_status

    try:
        return effective_status(result)
    except Exception:
        return getattr(result.validation_status, "value", str(result.validation_status))


def _is_compared(result: KpiResult, effective: str) -> bool:
    if effective in _COMPARED or result.platform_value is not None:
        return True
    return bool((result.explanation or {}).get("alternative_value") is not None
                or str(result.reason_code or "").startswith("matches_alternative"))


def _period_key(result: KpiResult) -> tuple:
    return (result.period_grain or "", as_date(result.period_end), result.scope_key or "{}")


def _period_text(key: tuple, scoped: bool = False) -> str:
    grain, end, scope = key
    shown = end.isoformat() if end else "?"
    extra = scope_text(scope)
    if extra == "total":
        return f"{grain} {shown}" + (" total" if scoped else "")
    return f"{grain} {shown} ({extra})"


def _unvalidated_text(key: tuple, matched: int, total: int, scoped: bool = False) -> str:
    text = _period_text(key, scoped)
    return f"{text} ({matched} of {total} values match)" if matched else text


def _period_unit(keys: Any) -> tuple[str, bool]:
    keys = list(keys)
    scoped = len({k[:2] for k in keys}) < len(keys)
    return ("period and scope combination(s)" if scoped else "period(s)"), scoped


def kpi_verdict(kpi: Any, results: list[KpiResult]) -> dict[str, Any]:
    """Verdict, plain detail sentence, period counts and error for one KPI."""
    primary = "primary" if definition_source(kpi, getattr(kpi, "kpi_id", None)) == "hint" else "governed"
    out = {"verdict": "not compared", "verdict_detail": VERDICT_INFO["not compared"][1], "periods_compared": 0,
           "periods_validated": 0, "error": ""}
    if not results:
        return out
    effective = {r.result_id: _effective(r) for r in results}
    compared = [r for r in results if _is_compared(r, effective[r.result_id])]
    periods: dict[tuple, list[KpiResult]] = defaultdict(list)
    for r in compared:
        periods[_period_key(r)].append(r)
    matching = {k: sum(1 for r in rs if effective[r.result_id] == "VERIFIED") for k, rs in periods.items()}
    validated = {k for k, rs in periods.items() if matching[k] == len(rs)}
    alternative = {k for k, rs in periods.items() if k not in validated and any(
        str(r.reason_code or "").startswith("matches_alternative") for r in rs)}
    blocked = {k for k, rs in periods.items() if all(flag_for(effective[r.result_id]) == "BLOCKED" for r in rs)}
    errors = []
    for r in results:
        if flag_for(effective[r.result_id]) == "BLOCKED" or r.reason_code in _COMPILE_ERRORS:
            text = str((r.explanation or {}).get("error") or r.reason_detail or r.reason_code)
            if text and text not in errors:
                errors.append(text)
    out["error"] = " | ".join(errors[:3])
    m, v = len(periods), len(validated)
    unit, scoped = _period_unit(periods)
    out.update(periods_compared=m, periods_validated=v)
    conflicts = sum(1 for r in results if getattr(r.validation_status, "value", "") == "REPORT_CONFLICT")
    conflict_note = f" {conflicts} report value(s) disagree with another report." if conflicts else ""
    if m == 0:
        if (kpi is not None and kpi.platform is None) or all(effective[r.result_id] == "REPORT_ONLY" for r in results):
            n = sum(1 for r in results if effective[r.result_id] == "REPORT_ONLY")
            out.update(verdict="report only",
                       verdict_detail=f"No platform table is declared; {n} report value(s) captured as printed.")
        elif errors:
            out.update(verdict="not computable", verdict_detail=f"The platform definition could not be applied: {errors[0]}")
        else:
            out.update(verdict="not compared",
                       verdict_detail=f"{len(results)} report value(s) mapped, none compared with the platform "
                                      "(see the status of each row).")
        return out
    if v == m:
        out.update(verdict="validated",
                   verdict_detail=f"Validated in {v} of {m} {unit} under the {primary} definition.{conflict_note}")
    elif v > 0 or any(matching.values()):
        missing = ", ".join(_unvalidated_text(k, matching[k], len(periods[k]), scoped)
                            for k in sorted(set(periods) - validated, key=_sort_period)[:4])
        alt = f" A declared alternative matches {len(alternative)} of them." if alternative else ""
        out.update(verdict="partly validated",
                   verdict_detail=f"Validated in {v} of {m} {unit}; not in {missing}.{alt}{conflict_note}")
    elif alternative:
        names = sorted({alternative_text(r.explanation) or "" for k in alternative for r in periods[k]} - {""})
        out.update(verdict="matches alternative",
                   verdict_detail=f"No period matches the {primary} definition; a declared alternative "
                                  f"({', '.join(names) or 'see rows'}) reproduces {len(alternative)} of {m} {unit}."
                                  f"{conflict_note}")
    elif len(blocked) == m:
        out.update(verdict="not computable",
                   verdict_detail=f"The platform SQL could not run for {m} {unit}: {errors[0] if errors else 'see rows'}")
    else:
        out.update(verdict="not validated",
                   verdict_detail=f"Compared in {m} {unit}; none match and no declared definition explains "
                                  f"the gap.{conflict_note}")
    return out


def _sort_period(key: tuple) -> tuple:
    return (key[1] is None, key[1] or date.min, key[0], key[2])


def kpis_frame(index: EvidenceIndex, registry: Any) -> pd.DataFrame:
    """One row per KPI definition used (compared KPIs, approved registry KPIs and every KPI table hint)."""
    by_kpi: dict[str, list[KpiResult]] = defaultdict(list)
    for r in index.results.values():
        if r.kpi_id:
            by_kpi[r.kpi_id].append(r)
    kpis = getattr(registry, "kpis", None) or {}
    ids = list(by_kpi)
    for kpi_id, kpi in kpis.items():
        status = getattr(kpi.status, "value", kpi.status)
        if kpi_id not in by_kpi and (status == "APPROVED" or definition_source(kpi, kpi_id) == "hint"):
            ids.append(kpi_id)
    if index.focus is not None:
        ids = [k for k in ids if k in index.focus]
    rows = []
    for kpi_id in ids:
        kpi = kpis.get(kpi_id)
        results = by_kpi.get(kpi_id, [])
        verdict = kpi_verdict(kpi, results)
        statuses = [getattr(r.validation_status, "value", "") for r in results]
        periods = [as_date(r.period_end) for r in results if as_date(r.period_end) is not None]
        tables: list[str] = []
        for r in results:
            for name in relations(executed_sql(index, r)):
                if name not in tables:
                    tables.append(name)
        hint = hint_fields(kpi)
        rows.append({
            "kpi_id": kpi_id, "kpi_label": kpi.label if kpi is not None else kpi_id,
            "definition_source": definition_source(kpi, kpi_id), "priority": _kpi_priority(kpi), **verdict,
            "comparisons": len(results), "verified": statuses.count("VERIFIED"),
            "review": sum(1 for s in statuses if flag_for(s) == "REVIEW"),
            "blocked": sum(1 for s in statuses if flag_for(s) == "BLOCKED"),
            "report_only": statuses.count("REPORT_ONLY"),
            "first_period": min(periods) if periods else None, "last_period": max(periods) if periods else None,
            "unit": kpi.unit if kpi is not None else "",
            "definition": definition_text(kpi, governed_defaults(kpi), kpi_id),
            "platform_tables": "; ".join(tables) or ("; ".join(kpi.platform.source_views)
                                                     if kpi is not None and kpi.platform is not None else ""),
            "hint_table": hint["table"], "hint_value_sql": hint["value_sql"], "hint_notes": hint["notes"],
            "hint_row": hint["row"],
        })
    rows.sort(key=lambda r: (*_priority_key(r["priority"]), VERDICTS.index(r["verdict"]), str(r["kpi_label"]).lower()))
    return _frame(rows, KPI_COLUMNS, int_columns=("priority", "periods_compared", "periods_validated", "comparisons",
                                                  "verified", "review", "blocked", "report_only", "hint_row"))


def documents_frame(index: EvidenceIndex) -> pd.DataFrame:
    """One row per document of the check (file, format, metadata, status, confirmation, counts)."""
    pages: dict[str, int] = defaultdict(int)
    for doc_id, _n in index.pages:
        pages[doc_id] += 1
    values: dict[str, int] = defaultdict(int)
    for v in index.values.values():
        values[v.doc_id] += 1
    results: dict[str, int] = defaultdict(int)
    for r in index.results.values():
        results[r.doc_id] += 1
    rows = []
    for doc in sorted(index.docs.values(), key=lambda d: (d.file_name.lower(), d.version or 0)):
        confirmed, basis = metadata_state(doc)
        superseded = index.docs.get(doc.superseded_by_doc_id or "")
        rows.append({
            "report_file": doc.file_name, "format": getattr(doc.format, "value", doc.format), "brand": doc.brand,
            "report_type": doc.report_type, "period_label": doc.period_label,
            "period_start": as_date(doc.period_start), "period_end": as_date(doc.period_end),
            "period_grain": _proposal_value(doc, "period_grain"), "version": doc.version,
            "status": getattr(doc.status, "value", doc.status), "metadata_confirmed": confirmed,
            "metadata_basis": basis, "needs_confirmation": ", ".join(str(f) for f in doc.needs_confirmation or []),
            "pages": pages.get(doc.doc_id, 0), "extracted_values": values.get(doc.doc_id, 0),
            "comparisons": results.get(doc.doc_id, 0),
            "superseded_by": (superseded.file_name if superseded is not None else doc.superseded_by_doc_id) or "",
            "error": doc.error or "", "file_path": doc.file_path, "doc_id": doc.doc_id,
        })
    df = _frame(rows, DOCUMENT_COLUMNS, int_columns=("version", "pages", "extracted_values", "comparisons"))
    df["metadata_confirmed"] = df["metadata_confirmed"].astype(bool)
    return df


def not_compared_reason(doc: Document | None, docs: dict[str, Document] | None = None) -> str | None:
    """Why a document's values were not compared, from its state; None once it was processed."""
    if doc is None:
        return None
    status = getattr(doc.status, "value", doc.status)
    if status == "SUPERSEDED":
        newer = (docs or {}).get(doc.superseded_by_doc_id or "")
        return f"Not compared: superseded by {newer.file_name if newer is not None else 'a newer version'}."
    if status == "AWAITING_CONFIRMATION":
        fields = ", ".join(str(f) for f in doc.needs_confirmation or []) or "metadata"
        return f"Not compared: the report is awaiting confirmation ({fields})."
    if status == "FAILED":
        return f"Not compared: the document failed at the {doc.failed_stage or 'unknown'} stage."
    if status in ("REGISTERED", "EXTRACTED", "READY"):
        return "Not compared: the document was not processed."
    return None


def extraction_frame(index: EvidenceIndex) -> pd.DataFrame:
    """Every extracted value of the check's documents with its location, context and focus as readable columns."""
    from .reconcile import OUTSIDE_FOCUS, in_focus

    obs_by_value = {o.value_id: o for o in index.observations.values()}
    result_by_obs = {r.observation_id: r for r in index.results.values()}
    rows = []
    for v in index.values.values():
        doc = index.docs.get(v.doc_id)
        ctx = v.context or {}
        obs = obs_by_value.get(v.value_id)
        result = result_by_obs.get(obs.observation_id) if obs is not None else None
        mapping = index.mappings.get(obs.observation_id) if obs is not None else None
        focused = index.focus is None or mapping is None or in_focus(mapping, index.focus)
        shown = result if focused else None
        kpi_id = shown.kpi_id if shown is not None else (None if focused else mapping.kpi_id)
        if shown is not None:
            reason = shown.reason_detail
        else:
            reason = not_compared_reason(doc, index.docs) or (None if focused else OUTSIDE_FOCUS)
        rows.append({
            "report_file": doc.file_name if doc is not None else "", "source_type": source_type(v.method, ctx),
            "page": page_of(v), "object": object_of(v), "location": location_text(v, doc),
            "label": obs.label if obs is not None else context_label(ctx), "row_label": ctx.get("row_label"),
            "col_header": ctx.get("col_header"), "series": ctx.get("series_name"), "category": ctx.get("category"),
            "text_line": ctx.get("text_line"), "value_as_printed": v.raw_text, "raw_number": v.raw_number,
            "number_format": v.number_format, "confidence": v.confidence, "flags": flags_text(v.flags),
            "kpi_id": kpi_id,
            "status": getattr(shown.validation_status, "value", None) if shown is not None else None,
            "in_focus": focused, "reason": reason,
            "value_id": v.value_id, "observation_id": obs.observation_id if obs is not None else None,
            "doc_id": v.doc_id,
        })
    rows.sort(key=lambda r: (str(r["report_file"]).lower(), r["page"] is None, r["page"] or 0, str(r["location"])))
    df = _frame(rows, EXTRACTION_COLUMNS, int_columns=("page",))
    for col in ("raw_number", "confidence"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    for col in ("row_label", "col_header", "series", "category", "text_line"):
        df[col] = [None if is_missing(x) else str(x) for x in df[col].tolist()]
    df["in_focus"] = df["in_focus"].astype(bool)
    return df


def _components_text(artifact: SqlArtifact) -> str:
    comps = artifact.components_sql or {}
    if set(comps) <= {"value"}:
        return ""
    return "\n".join(f"-- {name}\n{sql};" for name, sql in comps.items())


def _alternative_rows(index: EvidenceIndex) -> list[dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for r in index.results.values():
        sql = alternative_sql(index, r) if r.kpi_id else ""
        if not sql:
            continue
        row = found.get((r.kpi_id, sql))
        if row is not None:
            row["comparisons"] += 1
            continue
        kpi = index.kpis.get(r.kpi_id)
        choices = dict((r.explanation or {}).get("matched_choices") or {})
        defaults = governed_defaults(kpi)
        found[(r.kpi_id, sql)] = {
            "kpi_id": r.kpi_id, "kpi_label": kpi.label if kpi is not None else r.kpi_id,
            "definition_source": definition_source(kpi, r.kpi_id), "definition": definition_text(kpi, choices, r.kpi_id),
            "choices": choices_text(choices), "scope": scope_text(r.scope_key), "period_grain": r.period_grain,
            "governed_default": bool(defaults) and all(choices.get(k) == v for k, v in defaults.items()),
            "alternative": True, "platform_tables": "; ".join(relations(sql)), "value_sql": sql,
            "components_sql": "", "checks_sql": "", "function_sql": "", "export_path": "", "comparisons": 1,
            "artifact_id": "", "sql_sha": sha256_text(sql),
        }
    return list(found.values())


def sql_frame(index: EvidenceIndex) -> pd.DataFrame:
    """Every SQL artifact used by a comparison, plus the SQL of each matching alternative, with its texts."""
    uses: dict[str, int] = defaultdict(int)
    for r in index.results.values():
        if r.sql_artifact_id:
            uses[r.sql_artifact_id] += 1
    rows = []
    for a in index.artifacts.values():
        kpi = index.kpis.get(a.kpi_id)
        defaults = governed_defaults(kpi)
        checks = "\n".join(f"-- check: {name}\n{sql};" for name, sql in (a.checks_sql or {}).items())
        rows.append({
            "kpi_id": a.kpi_id, "kpi_label": kpi.label if kpi is not None else a.kpi_id,
            "definition_source": definition_source(kpi, a.kpi_id), "definition": definition_text(kpi, a.choices, a.kpi_id),
            "choices": choices_text(a.choices), "scope": scope_text(canonical_json((a.bindings or {}).get("scope") or {})),
            "period_grain": (a.bindings or {}).get("grain"),
            "governed_default": all((a.choices or {}).get(k) == v for k, v in defaults.items()), "alternative": False,
            "platform_tables": "; ".join(relations(a.value_sql_template)), "value_sql": a.value_sql_template,
            "components_sql": _components_text(a), "checks_sql": checks, "function_sql": a.function_sql or "",
            "export_path": durable_path(a.export_path), "comparisons": uses.get(a.artifact_id, 0),
            "artifact_id": a.artifact_id, "sql_sha": a.sql_sha,
        })
    rows += _alternative_rows(index)
    rows.sort(key=lambda r: (str(r["kpi_label"]).lower(), r["alternative"], not r["governed_default"], r["choices"],
                             str(r["period_grain"]), r["scope"] != "total", r["scope"], str(r["value_sql"] or "")))
    df = _frame(rows, SQL_COLUMNS, int_columns=("comparisons",))
    df["governed_default"] = df["governed_default"].astype(bool)
    df["alternative"] = df["alternative"].astype(bool)
    return df


def platform_frame(index: EvidenceIndex) -> pd.DataFrame:
    """The materialized platform series of the check's SQL artifacts: one row per artifact, grain and period."""
    rows = []
    for pkv in index.series:
        artifact = index.artifacts.get(pkv.sql_artifact_id)
        kpi = index.kpis.get(pkv.kpi_id)
        choices = artifact.choices if artifact is not None else None
        rows.append({
            "kpi_id": pkv.kpi_id, "kpi_label": kpi.label if kpi is not None else pkv.kpi_id,
            "definition": definition_text(kpi, choices, pkv.kpi_id), "choices": choices_text(choices),
            "scope": scope_text(pkv.scope_key), "period_grain": pkv.period_grain,
            "period_start": as_date(pkv.period_start), "period_end": as_date(pkv.period_end), "brand": pkv.brand,
            "value": clean(pkv.value), "unit": kpi.unit if kpi is not None else None,
            "components": pkv.components or None, "error": pkv.error or "", "computed_at": pkv.computed_at,
            "artifact_id": pkv.sql_artifact_id,
        })
    rows.sort(key=lambda r: (str(r["kpi_label"]).lower(), r["choices"], r["scope"] != "total", r["scope"],
                             str(r["period_grain"]), r["period_end"] or date.min))
    df = _frame(rows, PLATFORM_COLUMNS)
    df["value"] = pd.to_numeric(df["value"], errors="coerce").astype("float64")
    return df


def _comment_line(label: str, value: Any) -> str:
    return f"-- {label}: " + " ".join(str("" if clean(value) is None else value).split())


def sql_file_name(row: dict[str, Any]) -> str:
    """File name of the .sql file for a row of ``CheckResult.sql``."""
    kpi = _SAFE_NAME.sub("_", str(row.get("kpi_id") or "kpi"))
    if clean(row.get("alternative")):
        return f"{kpi}_alternative_{str(row.get('sql_sha') or '')[:12]}.sql"
    return f"{kpi}_{str(row.get('artifact_id') or '')[:16]}.sql"


def sql_file_text(row: dict[str, Any]) -> str:
    """The read-only text of one .sql file for a row of ``CheckResult.sql`` (value, components, checks)."""
    alternative = bool(clean(row.get("alternative")))
    value = str(clean(row.get("value_sql")) or "").rstrip().rstrip(";") + ";"
    sections = [(title, str(clean(row.get(column)) or "").strip())
                for title, column in (("Components (the pipeline computes the value from these)", "components_sql"),
                                      ("Checks (each must return 0 before the value is trusted)", "checks_sql"))]
    lines = [
        _comment_line("KPI", f"{row.get('kpi_id')} ({row.get('kpi_label')})"),
        _comment_line("Definition", row.get("definition")),
        _comment_line("Definition source", row.get("definition_source")),
        _comment_line("Choices", row.get("choices")),
        _comment_line("Scope", f"{row.get('scope')}; grain {row.get('period_grain')}"),
    ]
    if alternative:
        lines.append(_comment_line("Matched alternative", f"the declared alternative that reproduced the report "
                                                          f"value, as it ran with the period end filled in; "
                                                          f"sql_sha {row.get('sql_sha')}"))
    else:
        lines.append(_comment_line("Artifact", f"{row.get('artifact_id')}, sql_sha {row.get('sql_sha')}"))
    if "{as_of}" in value or any("{as_of}" in body for _title, body in sections):
        lines.append("-- Replace {as_of} with the measurement period end (YYYY-MM-DD) before running.")
    lines += ["", "-- Value", value]
    for title, body in sections:
        if body:
            lines += ["", f"-- {title}", body if body.endswith(";") else body + ";"]
    return "\n".join(lines) + "\n"


def _issue(severity: str, category: str, *, report_file: str = "", kpi_id: str | None = None, item: str = "",
           message: str = "", remedy: str = "", count: int = 1, doc_id: str | None = None,
           result_id: str | None = None) -> dict[str, Any]:
    return {"severity": severity, "category": category, "report_file": report_file, "kpi_id": kpi_id, "item": item,
            "message": message, "remedy": remedy, "count": count, "doc_id": doc_id, "result_id": result_id}


_SEVERITY_ORDER = ("error", "warning", "info")
_BLOCKED_REMEDY = {
    "MISSING_ACCESS": "Ask for SELECT on the tables in the SQL for the identity running the notebook, then run again.",
    "PLATFORM_DATA_ISSUE": "Check that the table and columns exist and hold data for the period; fix the KPI table "
                           "row or the source, then run again.",
    "BLOCKED": "Read the error, fix its cause, then run again.",
}


def _confirm_hint(doc: Document) -> str:
    fields = [f for f in (doc.needs_confirmation or []) if f] or ["brand", "period"]
    names = list(dict.fromkeys("period" if str(f).startswith("period") else str(f) for f in fields))
    args = ", ".join(f"{f}=..." for f in names)
    return f"check.confirm({doc.file_name!r}, {args}) and run again."


def document_issues(index: EvidenceIndex) -> list[dict[str, Any]]:
    """Failed, unsupported, encrypted, unprocessed and metadata-unconfirmed documents."""
    out = []
    for doc in sorted(index.docs.values(), key=lambda d: d.file_name.lower()):
        status = getattr(doc.status, "value", doc.status)
        common = {"report_file": doc.file_name, "doc_id": doc.doc_id}
        if status == "FAILED":
            out.append(_issue("error", "document failed", item=f"stage {doc.failed_stage or '?'}",
                              message=doc.error or "The document failed without an error message.",
                              remedy="Fix the file or the cause of the error and add the report again.", **common))
        elif status == "UNSUPPORTED_FORMAT":
            out.append(_issue("error", "unsupported format", item=doc.sniff_detail or "",
                              message=doc.error or f"{doc.file_name} is not a supported report format.",
                              remedy="Save it as .pptx, .pdf or .xlsx and add it again.", **common))
        elif status == "ENCRYPTED":
            out.append(_issue("error", "encrypted file", item=doc.sniff_detail or "",
                              message=doc.error or f"{doc.file_name} is encrypted and cannot be read.",
                              remedy="Remove the sensitivity-label encryption or export the report to PDF.", **common))
        elif status == "AWAITING_CONFIRMATION":
            fields = ", ".join(doc.needs_confirmation or []) or "metadata"
            out.append(_issue("warning", "awaiting confirmation", item=fields,
                              message=f"Not compared: the {fields} could not be read with confidence.",
                              remedy=_confirm_hint(doc), **common))
        elif status in ("REGISTERED", "EXTRACTED"):
            out.append(_issue("warning", "not processed", item=status,
                              message=f"The document was not processed (status {status}).",
                              remedy="Run the check again.", **common))
        elif doc.confirmed_by == UNCONFIRMED_ACTOR:
            used = ", ".join(f"{k}={getattr(doc, k)}" for k in ("brand", "report_type", "period_label")
                             if getattr(doc, k, None))
            out.append(_issue("warning", "metadata not confirmed", item=used,
                              message="Compared using proposed metadata that no person confirmed"
                                      + (f" ({used})." if used else "."),
                              remedy=_confirm_hint(doc), **common))
    return out


_EXTRACTION_REMEDY = {
    "FORMULA_RESULT_MISSING": "Open the workbook in Excel so it calculates the formulas, save it and add it again.",
    "WORKSHEET_FORMULA_ERROR": "Fix the formula in the workbook, save it and add it again.",
    "WORKSHEET_ERROR_VALUE": "Fix the cell in the workbook, save it and add it again.",
}
_EXTRACTION_DEFAULT_REMEDY = ("Numbers at this location may be missing from the comparisons; check them in the file, "
                              "or export the report again and add the new file.")
_EXTRACTION_SHOWN = ("error", "warning")


def _extraction_place(doc: Document | None, item: ExtractionIssue) -> str:
    if item.page is None:
        return ""
    workbook = doc is not None and getattr(doc.format, "value", doc.format) == "XLSX"
    return f"{'sheet' if workbook else _page_word(doc)} {item.page}"


def extraction_issues(index: EvidenceIndex) -> list[dict[str, Any]]:
    """Warnings and errors the extractors reported, one row per document, code and severity."""
    groups: dict[tuple[str, str, str], list[ExtractionIssue]] = defaultdict(list)
    for item in index.extraction_issues:
        severity = str(item.severity or "").lower()
        if severity in _EXTRACTION_SHOWN:
            groups[(item.doc_id, item.code, severity)].append(item)
    out = []
    for (doc_id, code, severity), items in sorted(groups.items()):
        doc = index.docs.get(doc_id)
        places = list(dict.fromkeys(p for p in (_extraction_place(doc, i) for i in items) if p))
        details = list(dict.fromkeys(str(i.detail) for i in items if i.detail))
        shown = "; ".join(details[:3]) + (f"; and {len(details) - 3} more" if len(details) > 3 else "")
        where = ", ".join(places[:5]) + (", ..." if len(places) > 5 else "")
        out.append(_issue(severity, "extraction issue", report_file=doc.file_name if doc is not None else "",
                          item=code + (f" ({where})" if where else ""), message=shown or code,
                          remedy=_EXTRACTION_REMEDY.get(code, _EXTRACTION_DEFAULT_REMEDY), count=len(items),
                          doc_id=doc_id))
    return out


def hint_issues(hints: Any) -> list[dict[str, Any]]:
    """KPI table validation messages (``HintSet.messages`` or a DataFrame of messages)."""
    messages = getattr(hints, "messages", None) if hints is not None else None
    if messages is None and isinstance(hints, pd.DataFrame):
        messages = hints
    if messages is None:
        return []
    if not isinstance(messages, pd.DataFrame):
        try:
            messages = pd.DataFrame(messages)
        except Exception:
            return [_issue("warning", "KPI table", message=str(messages))]
    out = []
    for raw in messages.to_dict(orient="records"):
        row = {str(k).lower(): clean(v) for k, v in raw.items()}
        message = row.get("message") or row.get("error") or row.get("detail")
        if message is None:
            message = "; ".join(f"{k}={v}" for k, v in row.items() if v is not None)
        severity = str(row.get("severity") or row.get("level") or ("error" if row.get("error") else "warning")).lower()
        item_parts = [f"row {row['row']}" if row.get("row") is not None else "",
                      str(row.get("column") or row.get("field") or "")]
        out.append(_issue(severity if severity in _SEVERITY_ORDER else "warning", "KPI table",
                          kpi_id=row.get("kpi") or row.get("kpi_id"), item=", ".join(p for p in item_parts if p),
                          message=str(message), remedy=str(row.get("remedy") or "Fix the KPI table row and run again.")))
    return out


def blocked_issues(index: EvidenceIndex) -> list[dict[str, Any]]:
    """BLOCKED-group results, one issue per (document, KPI, status, reason) with a count."""
    groups: dict[tuple, list[KpiResult]] = defaultdict(list)
    for r in index.results.values():
        status = getattr(r.validation_status, "value", r.validation_status)
        if flag_for(status) == "BLOCKED":
            groups[(r.doc_id, r.kpi_id, status, r.reason_code)].append(r)
    out = []
    for (doc_id, kpi_id, status, reason), results in sorted(groups.items(), key=lambda kv: [str(x) for x in kv[0]]):
        doc = index.docs.get(doc_id)
        first = results[0]
        error = str((first.explanation or {}).get("error") or "")
        message = first.reason_detail + (f" ({error})" if error and error not in first.reason_detail else "")
        out.append(_issue("error", status_label(status), report_file=doc.file_name if doc else "", kpi_id=kpi_id,
                          item=reason or "", message=message, remedy=_BLOCKED_REMEDY.get(status, ""),
                          count=len(results), doc_id=doc_id, result_id=first.result_id))
    return out


def issue_rows(index: EvidenceIndex, hints: Any = None) -> list[dict[str, Any]]:
    """Everything to fix: documents, extraction issues, KPI table messages, blocked results and unreadable tables."""
    rows = document_issues(index) + extraction_issues(index) + hint_issues(hints) + blocked_issues(index)
    for err in index.errors:
        rows.append(_issue("error", "report", item=err.split(":", 1)[0],
                           message=f"A state table could not be read: {err}",
                           remedy="The report is incomplete for this table; check the state store."))
    return rows


def _sort_issues(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(r: dict[str, Any]) -> tuple:
        sev = _SEVERITY_ORDER.index(r["severity"]) if r["severity"] in _SEVERITY_ORDER else len(_SEVERITY_ORDER)
        return (sev, str(r["category"]), str(r["report_file"]).lower(), str(r["kpi_id"] or ""))

    return sorted(rows, key=key)


def _ai_failures(index: EvidenceIndex) -> int:
    failed = 0
    for mapping in index.mappings.values():
        if any(isinstance(c, dict) and c.get("source") == "model" and c.get("note") == "error"
               for c in (mapping.candidates or [])):
            failed += 1
    return failed


_AI_CATEGORY = re.compile(r"ai(?:\b|_)", re.IGNORECASE)


def summary_dict(index: EvidenceIndex, frames: dict[str, pd.DataFrame], issues: pd.DataFrame, *, hints: Any,
                 run_id: str | None, platform_description: str, timings: dict[str, Any] | None,
                 settings: Any = None, state_saved: bool = False) -> dict[str, Any]:
    """Counts by flag / status / document, tiles, verdicts, run facts and timings."""
    comps = frames["comparisons"]
    by_flag = {f: int((comps["flag"] == f).sum()) if not comps.empty else 0 for f in FLAG_ORDER}
    statuses = comps["status"].value_counts().to_dict() if not comps.empty else {}
    by_status = {s: int(statuses.get(s, 0)) for s in FLAG_BY_STATUS if statuses.get(s)}
    by_document: dict[str, dict[str, int]] = {}
    docs = frames["documents"]
    names = {}
    for row in docs.to_dict(orient="records"):
        name = row["report_file"]
        if name in names.values():
            name = f"{name} ({str(row['doc_id'])[:8]})"
        names[row["doc_id"]] = name
        sub = comps[comps["doc_id"] == row["doc_id"]] if not comps.empty else comps
        counts = {f: int((sub["flag"] == f).sum()) if not sub.empty else 0 for f in FLAG_ORDER}
        by_document[name] = {**counts, "total": int(len(sub)), "status": row["status"]}
    kpis = frames["kpis"]
    verdicts = {v: int((kpis["verdict"] == v).sum()) for v in VERDICTS if not kpis.empty and (kpis["verdict"] == v).any()}
    hint_kpis = int((kpis["definition_source"] == "hint").sum()) if not kpis.empty else 0
    hint_rows = None
    for attr in ("rows", "frame", "table", "hints"):
        value = getattr(hints, attr, None) if hints is not None else None
        if isinstance(value, (pd.DataFrame, list, tuple)):
            hint_rows = int(len(value))
            break
    if hint_kpis and not kpis.empty:
        kpi_source = "KPI table" if (kpis["definition_source"] != "hint").sum() == 0 else "KPI table + registry"
    else:
        kpi_source = "registry"
    tiles = {
        "matches_platform": int(statuses.get("VERIFIED", 0)), "captured": int(statuses.get("REPORT_ONLY", 0)),
        "needs_review": by_flag["REVIEW"], "blocked": by_flag["BLOCKED"],
        "other_numbers": int(statuses.get("UNMAPPED_CAPTURED", 0)),
    }
    return {
        "run_id": run_id, "generated_at": utcnow().isoformat(),
        "platform": platform_description or "", "kpi_source": kpi_source, "hint_kpis": hint_kpis,
        "hint_rows": hint_rows, "hint_messages": int((issues["category"] == "KPI table").sum()) if len(issues) else 0,
        "documents": int(len(docs)), "reports": [r["report_file"] for r in docs.to_dict(orient="records")],
        "comparisons": int(len(comps)), "by_flag": by_flag, "by_status": by_status, "by_document": by_document,
        "tiles": tiles, "kpis": int(len(kpis)), "verdicts": verdicts, "issues": int(len(issues)),
        "focus": sorted(index.focus) if index.focus is not None else None,
        "reused": int(comps["reused"].sum()) if not comps.empty else 0,
        "state_saved": bool(state_saved),
        "ai_mode": getattr(settings, "ai_mode", None),
        "ai_failures": _ai_failures(index),
        "ai_issues": sum(1 for c in (issues["category"].tolist() if len(issues) else []) if _AI_CATEGORY.match(str(c))),
        "timings": dict(timings or {}),
        "brand": sorted({d.brand for d in index.docs.values() if d.brand}),
        "state_errors": list(index.errors),
    }
