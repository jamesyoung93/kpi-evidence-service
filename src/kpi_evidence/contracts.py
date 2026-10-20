from __future__ import annotations

import dataclasses
import json
import types
import typing
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, ClassVar


class StrEnum(str, Enum):
    """String enum that serializes as its value (Python 3.11 has enum.StrEnum, kept explicit)."""

    def __str__(self) -> str:
        return self.value


class DocumentFormat(StrEnum):
    PPTX = "PPTX"
    PPTM = "PPTM"
    XLSX = "XLSX"
    PDF = "PDF"
    PNG = "PNG"
    JPEG = "JPEG"
    TIFF = "TIFF"
    LEGACY_PPT = "LEGACY_PPT"
    ENCRYPTED_OOXML = "ENCRYPTED_OOXML"
    UNKNOWN = "UNKNOWN"


class DocumentStatus(StrEnum):
    """Lifecycle: REGISTERED -> EXTRACTED -> (AWAITING_CONFIRMATION ->) READY -> PROCESSED."""

    REGISTERED = "REGISTERED"
    EXTRACTED = "EXTRACTED"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    READY = "READY"
    PROCESSED = "PROCESSED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    ENCRYPTED = "ENCRYPTED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


class ExtractionMethod(StrEnum):
    NATIVE_CHART = "NATIVE_CHART"
    NATIVE_TABLE = "NATIVE_TABLE"
    EMBEDDED_WORKSHEET = "EMBEDDED_WORKSHEET"
    WORKBOOK = "WORKBOOK"
    SLIDE_TEXT = "SLIDE_TEXT"
    PDF_TABLE = "PDF_TABLE"
    PDF_TEXT = "PDF_TEXT"
    AI_PARSE = "AI_PARSE"
    IMAGE_PLACEHOLDER = "IMAGE_PLACEHOLDER"
    REVIEWER_CORRECTION = "REVIEWER_CORRECTION"


STRUCTURED_METHODS = frozenset({
    ExtractionMethod.NATIVE_CHART, ExtractionMethod.NATIVE_TABLE,
    ExtractionMethod.EMBEDDED_WORKSHEET, ExtractionMethod.WORKBOOK, ExtractionMethod.PDF_TABLE,
})


class ValidationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    REPORT_ONLY = "REPORT_ONLY"
    UNMAPPED_CAPTURED = "UNMAPPED_CAPTURED"
    VARIANCE_REVIEW = "VARIANCE_REVIEW"
    INCOMPATIBLE_DEFINITION = "INCOMPATIBLE_DEFINITION"
    UNCERTAIN_EXTRACTION = "UNCERTAIN_EXTRACTION"
    AMBIGUOUS_MAPPING = "AMBIGUOUS_MAPPING"
    REPORT_CONFLICT = "REPORT_CONFLICT"
    MISSING_ACCESS = "MISSING_ACCESS"
    PLATFORM_DATA_ISSUE = "PLATFORM_DATA_ISSUE"
    BLOCKED = "BLOCKED"


class StatusGroup(StrEnum):
    GOOD = "GOOD"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class StatusInfo:
    group: StatusGroup
    label: str
    tooltip: str
    sort: int


STATUS_INFO: dict[ValidationStatus, StatusInfo] = {
    ValidationStatus.VERIFIED: StatusInfo(StatusGroup.GOOD, "Matches platform",
        "The platform number, computed with the KPI's definition, agrees with the report within tolerance.", 10),
    ValidationStatus.REPORT_ONLY: StatusInfo(StatusGroup.GOOD, "Captured (report only)",
        "No platform table holds this KPI. The report value was captured with its source location; it is not independently verified.", 20),
    ValidationStatus.UNMAPPED_CAPTURED: StatusInfo(StatusGroup.GOOD, "Captured (not on the KPI list)",
        "An extractable number that is not a registered KPI. Kept as a candidate for the registry.", 30),
    ValidationStatus.VARIANCE_REVIEW: StatusInfo(StatusGroup.REVIEW, "Variance to review",
        "The platform number differs from the report by more than the KPI's tolerance.", 40),
    ValidationStatus.INCOMPATIBLE_DEFINITION: StatusInfo(StatusGroup.REVIEW, "Definition differs",
        "The report seems to use a different definition, unit or scale than the KPI's definition.", 50),
    ValidationStatus.UNCERTAIN_EXTRACTION: StatusInfo(StatusGroup.REVIEW, "Check the extracted value",
        "The value, its period or its scope could not be read with confidence from the report.", 60),
    ValidationStatus.AMBIGUOUS_MAPPING: StatusInfo(StatusGroup.REVIEW, "Confirm which KPI",
        "The label could match more than one KPI, or none with confidence.", 70),
    ValidationStatus.REPORT_CONFLICT: StatusInfo(StatusGroup.REVIEW, "Reports disagree",
        "Another current report gives a different value for the same KPI, period and scope.", 80),
    ValidationStatus.MISSING_ACCESS: StatusInfo(StatusGroup.BLOCKED, "No access to platform data",
        "The KPI's platform source is declared but this run cannot read it. Request access.", 90),
    ValidationStatus.PLATFORM_DATA_ISSUE: StatusInfo(StatusGroup.BLOCKED, "Platform data issue",
        "The platform source failed a data-quality check, is missing, or has no complete data for this period.", 100),
    ValidationStatus.BLOCKED: StatusInfo(StatusGroup.BLOCKED, "Could not evaluate",
        "The check did not run to completion. See the error; nothing was silently dropped.", 110),
}


class CoverageState(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    STALE = "STALE"
    NO_ACCESS = "NO_ACCESS"


class MappingMethod(StrEnum):
    SYNONYM = "SYNONYM"
    MEMORY = "MEMORY"
    MODEL = "MODEL"
    REVIEWER = "REVIEWER"


class MappingStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    AMBIGUOUS = "AMBIGUOUS"
    NO_MATCH = "NO_MATCH"


class MatchBasis(StrEnum):
    EXACT = "EXACT"
    WITHIN_ROUNDING = "WITHIN_ROUNDING"
    WITHIN_TOLERANCE = "WITHIN_TOLERANCE"


class ReviewAction(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    CORRECT = "CORRECT"


class DecisionSource(StrEnum):
    HUMAN = "HUMAN"
    AUTO_POLICY = "AUTO_POLICY"


class PublicationLabel(StrEnum):
    MATCHES_PLATFORM = "Matches platform"
    ACCEPTED_VARIANCE = "Accepted variance"
    REPORT_ONLY = "Report-only"
    CORRECTED = "Corrected by reviewer"
    CAPTURED = "Captured candidate"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class RegistryStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    RETIRED = "RETIRED"


class ReasonCode:
    """Known reason codes for kpi_results.reason_code (free text is allowed after a colon)."""

    MATCH_GOVERNED_DEFAULT = "match_under_governed_default"
    MATCH_STATED_VARIANT = "match_under_stated_permitted_variant"
    MATCH_REPORT_TYPE_DEFINITION = "match_under_report_type_definition"
    NO_PLATFORM_DEFINITION = "no_platform_definition"
    CAUSAL_CLAIM = "causal_claim"
    NO_REGISTRY_MATCH = "no_registry_match"
    UNIT_MISMATCH = "unit_mismatch"
    SCALE_MISMATCH = "scale_mismatch"
    MATCHES_ALTERNATIVE = "matches_alternative_definition"
    SINGLE_PERIOD_GAP = "single_period_gap_unexplained"
    SYSTEMATIC_GAP = "systematic_gap"
    UNRESOLVED = "unresolved"
    ZERO_TARGET = "zero_target"
    LOW_CONFIDENCE_VALUE = "low_confidence_value"
    PERIOD_UNKNOWN = "period_unknown"
    SCOPE_UNKNOWN = "scope_unknown"
    IMAGE_NOT_PARSED = "image_not_parsed"
    MULTIPLE_CANDIDATES = "multiple_candidates"
    MODEL_PROPOSAL_ONLY = "model_proposal_only"
    MEANING_FLAGGED = "meaning_flagged"
    PLATFORM_OBJECT_MISSING = "platform_object_missing"
    PERMISSION_DENIED = "permission_denied"
    DQ_FAILED = "dq_failed"
    NO_DATA_FOR_PERIOD = "no_data_for_period"
    NULL_RESULT = "null_result"
    EXECUTION_ERROR = "execution_error"
    CONFLICTING_REPORTS = "conflicting_reports"
    WINDOW_UNSUPPORTED = "window_unsupported"


Json = dict
JsonList = list


@dataclass
class Document:
    TABLE: ClassVar[str] = "documents"
    KEY: ClassVar[tuple[str, ...]] = ("doc_id",)

    doc_id: str
    file_name: str
    file_path: str
    size_bytes: int
    format: DocumentFormat
    status: DocumentStatus
    registered_at: datetime
    updated_at: datetime
    brand: str | None = None
    report_type: str | None = None
    period_label: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    publication_date: date | None = None
    version: int = 1
    report_family: str | None = None
    supersedes_doc_id: str | None = None
    superseded_by_doc_id: str | None = None
    metadata_proposals: Json = field(default_factory=dict)
    needs_confirmation: JsonList = field(default_factory=list)
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    uploaded_by: str | None = None
    sniff_detail: str | None = None
    failed_stage: str | None = None
    error: str | None = None
    last_run_id: str | None = None


@dataclass
class DocumentPage:
    """One slide or page: its title and text (for metadata proposals, stated rules and evidence)."""

    TABLE: ClassVar[str] = "document_pages"
    KEY: ClassVar[tuple[str, ...]] = ("doc_id", "page_number")

    doc_id: str
    page_number: int
    title: str | None
    text: str
    notes: str | None
    hidden: bool
    image_count: int
    page_width: float | None
    page_height: float | None
    shapes: JsonList
    created_at: datetime


@dataclass
class ExtractedValue:
    """Bronze: a number exactly as stored or printed in the source, with its location."""

    TABLE: ClassVar[str] = "extracted_values"
    KEY: ClassVar[tuple[str, ...]] = ("value_id",)

    value_id: str
    doc_id: str
    method: ExtractionMethod
    extractor: str
    extractor_version: str
    locator: Json
    slide_or_page: int | None
    context: Json
    raw_text: str | None
    raw_number: float | None
    number_format: str | None
    flags: JsonList
    confidence: float
    created_at: datetime
    run_id: str | None = None
    source_record_id: str | None = None


@dataclass
class ExtractionIssue:
    """A problem an extractor reported for one document, such as a page or formula value it could not read."""

    TABLE: ClassVar[str] = "extraction_issues"
    KEY: ClassVar[tuple[str, ...]] = ("doc_id", "seq")

    doc_id: str
    seq: int
    code: str
    severity: str
    page: int | None
    part: str | None
    detail: str | None
    run_id: str | None = None


@dataclass
class Observation:
    """Silver: one interpreted KPI-like value: label, value, unit, period and scope."""

    TABLE: ClassVar[str] = "observations"
    KEY: ClassVar[tuple[str, ...]] = ("observation_id",)

    observation_id: str
    value_id: str
    doc_id: str
    interpretation_version: str
    label: str
    label_context: str
    value: float | None
    unit: str | None
    display_half_unit: float
    period_start: date | None
    period_end: date | None
    period_grain: str | None
    period_label: str | None
    period_source: str | None
    period_confidence: float
    scope: Json
    scope_key: str
    brand: str | None
    fingerprint: str
    stated_rule: str
    flags: JsonList
    confidence: float
    created_at: datetime
    run_id: str | None = None


@dataclass
class MappingProposal:
    """Audit log of every mapping candidate (deterministic or model)."""

    TABLE: ClassVar[str] = "mapping_proposals"
    KEY: ClassVar[tuple[str, ...]] = ("proposal_id",)

    proposal_id: str
    observation_id: str
    method: MappingMethod
    kpi_id: str | None
    confidence: float
    rationale: str
    registry_version: str
    created_at: datetime
    model: str | None = None
    prompt_sha: str | None = None
    raw_response: str | None = None
    run_id: str | None = None


@dataclass
class KpiMapping:
    """The mapping decision used downstream (one per observation and registry version)."""

    TABLE: ClassVar[str] = "kpi_mappings"
    KEY: ClassVar[tuple[str, ...]] = ("observation_id", "registry_version")

    observation_id: str
    registry_version: str
    status: MappingStatus
    method: MappingMethod | None
    kpi_id: str | None
    kpi_version: int | None
    candidates: JsonList
    reason: str
    decided_by: str
    decided_at: datetime


@dataclass
class MappingMemory:
    """Reviewer-approved mapping, reused for the same layout fingerprint and label (informs; never validates)."""

    TABLE: ClassVar[str] = "mapping_memory"
    KEY: ClassVar[tuple[str, ...]] = ("memory_id",)

    memory_id: str
    fingerprint: str
    normalized_label: str
    kpi_id: str
    approved_by: str
    approved_at: datetime
    source_decision_id: str | None = None
    active: bool = True


@dataclass
class RegistryEntry:
    """Delta mirror of one registry KPI version."""

    TABLE: ClassVar[str] = "kpi_registry"
    KEY: ClassVar[tuple[str, ...]] = ("kpi_id", "kpi_version")

    kpi_id: str
    kpi_version: int
    definition_hash: str
    status: RegistryStatus
    label: str
    definition: str
    unit: str
    aggregation: str
    has_platform_spec: bool
    claim_class: str
    owner: str
    spec: Json
    registry_version: str
    loaded_at: datetime
    approved_by: str | None = None
    approved_at: date | None = None


@dataclass
class ReconRun:
    """One reconciliation of one KPI for one (document, scope, definition) group of observations."""

    TABLE: ClassVar[str] = "recon_runs"
    KEY: ClassVar[tuple[str, ...]] = ("recon_run_id",)

    recon_run_id: str
    doc_id: str
    kpi_id: str
    kpi_version: int
    definition_hash: str
    bindings: Json
    bindings_hash: str
    start_choices: Json
    final_choices: Json
    phase_a_outcome: str
    phase_b_outcome: str | None
    reconloop_version: str
    judge: str | None
    policy: Json
    record: Json
    platform_snapshot: Json
    started_at: datetime
    finished_at: datetime
    run_id: str | None = None
    error: str | None = None


@dataclass
class ReconTrial:
    """Every tested configuration (every MetricSpec.compute call), including failures."""

    TABLE: ClassVar[str] = "recon_trials"
    KEY: ClassVar[tuple[str, ...]] = ("trial_id",)

    trial_id: str
    recon_run_id: str
    seq: int
    phase: str
    choices: Json
    as_of: date
    value: float | None
    error: str | None
    sql: str | None
    sql_sha: str | None
    created_at: datetime


@dataclass
class KpiResult:
    """The validation outcome for one observation against one KPI definition."""

    TABLE: ClassVar[str] = "kpi_results"
    KEY: ClassVar[tuple[str, ...]] = ("result_id",)

    result_id: str
    result_version: int
    observation_id: str
    doc_id: str
    brand: str | None
    kpi_id: str | None
    kpi_version: int | None
    definition_hash: str | None
    scope_key: str
    period_start: date | None
    period_end: date | None
    period_grain: str | None
    report_value: float | None
    unit: str | None
    platform_value: float | None
    abs_diff: float | None
    rel_diff: float | None
    tolerance: Json
    match_basis: MatchBasis | None
    validation_status: ValidationStatus
    reason_code: str
    reason_detail: str
    explanation: Json
    definition_choices: Json
    extraction_method: ExtractionMethod | None
    recon_run_id: str | None
    sql_artifact_id: str | None
    dq_status: str | None
    is_current: bool
    created_at: datetime
    run_id: str | None = None


@dataclass
class SqlArtifact:
    """Deterministic, re-runnable SQL that produced a platform value."""

    TABLE: ClassVar[str] = "sql_artifacts"
    KEY: ClassVar[tuple[str, ...]] = ("artifact_id",)

    artifact_id: str
    kpi_id: str
    kpi_version: int
    definition_hash: str
    bindings: Json
    choices: Json
    value_sql_template: str
    components_sql: Json
    checks_sql: Json
    function_sql: str | None
    sql_sha: str
    export_path: str | None
    created_at: datetime


@dataclass
class PlatformKpiValue:
    """The platform-computed time series for an approved definition (the 'deterministic table')."""

    TABLE: ClassVar[str] = "platform_kpi_values"
    KEY: ClassVar[tuple[str, ...]] = ("sql_artifact_id", "period_grain", "period_end")

    kpi_id: str
    definition_hash: str
    scope_key: str
    period_grain: str
    period_end: date
    period_start: date
    brand: str | None
    value: float | None
    components: Json
    sql_artifact_id: str
    platform_snapshot: Json
    computed_at: datetime
    error: str | None = None


@dataclass
class ReviewEvent:
    TABLE: ClassVar[str] = "review_events"
    KEY: ClassVar[tuple[str, ...]] = ("event_id",)

    event_id: str
    result_id: str | None
    doc_id: str | None
    event: str
    actor: str
    event_at: datetime
    detail: Json = field(default_factory=dict)
    request_id: str | None = None


@dataclass
class ReviewDecision:
    """Append-only decision log."""

    TABLE: ClassVar[str] = "review_decisions"
    KEY: ClassVar[tuple[str, ...]] = ("decision_id",)

    decision_id: str
    result_id: str
    result_version: int
    action: ReviewAction
    rationale: str
    decided_by: str
    decision_source: DecisionSource
    decided_at: datetime
    evidence_hash: str
    corrected_value: float | None = None
    corrected_kpi_id: str | None = None
    policy_version: str | None = None
    request_id: str | None = None


@dataclass
class Publication:
    """Append-only publication log."""

    TABLE: ClassVar[str] = "published_kpis"
    KEY: ClassVar[tuple[str, ...]] = ("publication_id",)

    publication_id: str
    brand: str
    kpi_id: str
    kpi_version: int
    scope_key: str
    period_start: date | None
    period_end: date | None
    period_grain: str | None
    period_label: str | None
    value: float
    unit: str | None
    display_label: PublicationLabel
    validation_status: ValidationStatus
    review_action: ReviewAction
    platform_value: float | None
    variance_rel: float | None
    tolerance: Json
    definition_hash: str | None
    sql_artifact_id: str | None
    sql_sha: str | None
    evidence_uri: str
    evidence_hash: str
    evidence_locator: Json
    extraction_method: ExtractionMethod | None
    result_id: str
    decision_id: str
    approved_by: str
    approved_at: datetime
    published_at: datetime
    harness_metric_id: str | None = None
    harness_variant_id: str | None = None
    harness_source: str | None = None
    retracted: bool = False
    superseded_by: str | None = None
    rationale: str | None = None


@dataclass
class RunLog:
    TABLE: ClassVar[str] = "run_log"
    KEY: ClassVar[tuple[str, ...]] = ("run_id", "stage", "unit")

    run_id: str
    stage: str
    unit: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    code_version: str
    counts: Json = field(default_factory=dict)
    error: str | None = None


@dataclass
class DqResult:
    TABLE: ClassVar[str] = "dq_results"
    KEY: ClassVar[tuple[str, ...]] = ("check_run_id", "object_name", "check_name")

    check_run_id: str
    object_name: str
    check_name: str
    severity: str
    passed: bool
    violations: int
    detail: str
    checked_at: datetime


@dataclass
class EvalRun:
    TABLE: ClassVar[str] = "eval_runs"
    KEY: ClassVar[tuple[str, ...]] = ("eval_id",)

    eval_id: str
    dataset: str
    metrics: Json
    details: Json
    code_version: str
    ran_at: datetime


@dataclass
class Brand:
    TABLE: ClassVar[str] = "brands"
    KEY: ClassVar[tuple[str, ...]] = ("brand_id", "version")

    brand_id: str
    version: int
    status: RegistryStatus
    display_name: str
    aliases: JsonList
    fiscal_year_start_month: int
    updated_by: str
    updated_at: datetime


@dataclass
class GoalListEntry:
    TABLE: ClassVar[str] = "kpi_goal_list"
    KEY: ClassVar[tuple[str, ...]] = ("brand_id", "kpi_id", "scope_key", "version")

    brand_id: str
    kpi_id: str
    scope_key: str
    version: int
    status: RegistryStatus
    cadence: str
    expected_lag_periods: int
    expected_report_types: JsonList
    owner: str
    updated_by: str
    updated_at: datetime


@dataclass
class ReportType:
    TABLE: ClassVar[str] = "report_types"
    KEY: ClassVar[tuple[str, ...]] = ("report_type", "version")

    report_type: str
    version: int
    status: RegistryStatus
    display_name: str
    keywords: JsonList
    default_grain: str
    updated_by: str
    updated_at: datetime


@dataclass
class Reviewer:
    TABLE: ClassVar[str] = "reviewers"
    KEY: ClassVar[tuple[str, ...]] = ("email", "brand_id")

    email: str
    brand_id: str
    role: str
    active: bool
    updated_by: str
    updated_at: datetime


ALL_TABLES: tuple[type, ...] = (
    Document, DocumentPage, ExtractedValue, ExtractionIssue, Observation, MappingProposal, KpiMapping, MappingMemory,
    RegistryEntry, ReconRun, ReconTrial, KpiResult, SqlArtifact, PlatformKpiValue, ReviewEvent, ReviewDecision,
    Publication, RunLog, DqResult, EvalRun, Brand, GoalListEntry, ReportType, Reviewer,
)


def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def column_kind(annotation: Any) -> tuple[str, bool, type | None]:
    """Return (sql_kind, nullable, enum_type) for a dataclass field annotation."""
    nullable = False
    args = typing.get_args(annotation)
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType) and type(None) in args:
        nullable = True
        annotation = next(a for a in args if a is not type(None))
        origin = typing.get_origin(annotation)
    if annotation in (dict, list) or origin in (dict, list):
        return "JSON", nullable, None
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "STRING", nullable, annotation
    if annotation is str:
        return "STRING", nullable, None
    if annotation is bool:
        return "BOOLEAN", nullable, None
    if annotation is int:
        return "BIGINT", nullable, None
    if annotation is float:
        return "DOUBLE", nullable, None
    if annotation is datetime:
        return "TIMESTAMP", nullable, None
    if annotation is date:
        return "DATE", nullable, None
    raise TypeError(f"unsupported annotation {annotation!r}")


def columns(cls: type) -> list[tuple[str, str, bool, type | None]]:
    """[(name, sql_kind, nullable, enum_type)] in field order."""
    hints = _hints(cls)
    return [(f.name, *column_kind(hints[f.name])) for f in dataclasses.fields(cls)]


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return str(value)


def to_json_text(value: Any) -> str:
    from .ids import json_safe

    return json.dumps(json_safe(value), sort_keys=True, default=_json_default, allow_nan=False)


def to_row(obj: Any) -> dict[str, Any]:
    """Dataclass instance -> flat dict ready for a store write (enums as values, JSON as text)."""
    from .ids import json_safe

    row: dict[str, Any] = {}
    for name, kind, _nullable, _enum in columns(type(obj)):
        value = getattr(obj, name)
        if value is None:
            row[name] = None
        elif kind == "JSON":
            row[name] = to_json_text(value)
        elif isinstance(value, Enum):
            row[name] = value.value
        elif kind == "DOUBLE":
            row[name] = json_safe(float(value))
        else:
            row[name] = value
    return row


def from_row(cls: type, row: dict[str, Any]) -> Any:
    """Flat dict from a store read -> dataclass instance (tolerates pandas NaN/NaT for nulls)."""
    import math

    values: dict[str, Any] = {}
    for name, kind, nullable, enum_type in columns(cls):
        raw = row.get(name)
        if raw is not None and isinstance(raw, float) and math.isnan(raw) and kind != "DOUBLE":
            raw = None
        if raw is not None and type(raw).__name__ in ("NaTType", "NAType"):
            raw = None
        if raw is None:
            values[name] = None
            continue
        if kind == "JSON":
            values[name] = json.loads(raw) if isinstance(raw, str) else raw
        elif enum_type is not None:
            values[name] = enum_type(raw)
        elif kind == "DATE":
            if isinstance(raw, datetime):
                raw = raw.date()
            elif isinstance(raw, str):
                raw = date.fromisoformat(raw[:10])
            elif hasattr(raw, "date") and not isinstance(raw, date):
                raw = raw.date()
            values[name] = raw
        elif kind == "TIMESTAMP":
            if isinstance(raw, str):
                raw = datetime.fromisoformat(raw)
            elif hasattr(raw, "to_pydatetime"):
                raw = raw.to_pydatetime()
            values[name] = raw
        elif kind == "DOUBLE":
            values[name] = None if (isinstance(raw, float) and math.isnan(raw)) else float(raw)
        elif kind == "BIGINT":
            values[name] = int(raw)
        elif kind == "BOOLEAN":
            values[name] = bool(raw)
        else:
            values[name] = str(raw)
    return cls(**values)
