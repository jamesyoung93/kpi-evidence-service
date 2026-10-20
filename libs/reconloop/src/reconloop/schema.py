from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from math import isfinite
from typing import Any


@dataclass
class Target:
    """One figure taken from a trusted (gold-standard) report."""
    target_id: str
    report: str
    metric: str
    as_of: str
    value: float
    unit: str = ""
    stated_rule: str = ""
    source_file: str = ""
    page: str = ""
    split: str = "fit"
    confidence: float = 1.0
    extraction_method: str = ""
    source_region: str = ""
    value_lower: float | None = None
    value_upper: float | None = None

    def __post_init__(self):
        if not all((self.target_id, self.report, self.metric)):
            raise ValueError("target_id, report and metric must be nonempty")
        if date.fromisoformat(self.as_of).isoformat() != self.as_of:
            raise ValueError("as_of must be an ISO date (YYYY-MM-DD)")
        if not isfinite(self.value):
            raise ValueError("target value must be finite")
        if self.split not in {"fit", "holdout"}:
            raise ValueError("split must be 'fit' or 'holdout'")
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        if (self.value_lower is None) != (self.value_upper is None):
            raise ValueError("provide both estimate bounds or neither")
        if self.value_lower is not None and (not isfinite(self.value_lower) or not isfinite(self.value_upper)
                or not self.value_lower <= self.value <= self.value_upper):
            raise ValueError("estimate bounds must be finite and contain the target value")


@dataclass
class Change:
    """One proposed edit to a metric's definition, with the business reason for it."""
    param: str
    old: str
    new: str
    hypothesis: str
    rationale: str = ""
    proposed_by: str = ""


@dataclass
class CompoundChange:
    """A bounded set of definition edits evaluated and accepted atomically."""
    changes: list[Change]
    rationale: str
    proposed_by: str = ""


def change_items(change: Change | CompoundChange) -> list[Change]:
    return change.changes if isinstance(change, CompoundChange) else [change]


def change_key(change: Change | CompoundChange) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((c.param, c.new) for c in change_items(change)))


@dataclass
class Iteration:
    step: int
    params: dict[str, str]
    fit_variance: float
    holdout_variance: float | None
    per_target: list[dict[str, Any]]
    change: Change | CompoundChange | None = None
    accepted: bool | None = None
    reject_reason: str = ""


@dataclass
class Residual:
    """What is left once the loop stops, and the most likely explanation."""
    fit_variance: float
    holdout_variance: float | None
    pattern: str
    likely_causes: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class ReconRecord:
    """The show-your-work artifact for one report x metric."""
    report: str
    metric: str
    start_params: dict[str, str]
    final_params: dict[str, str]
    final_query: str
    tolerance: float
    status: str
    iterations: list[Iteration]
    residual: Residual
    coupling: dict[str, Any] | None = None
    diagnostics: dict[str, Any] | None = None
    lifecycle: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
