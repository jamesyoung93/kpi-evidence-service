from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .hypotheses import CATALOG
from .metricspec import MetricSpec
from .schema import Change, CompoundChange, change_items


@dataclass
class Policy:
    require_named_hypothesis: bool = True
    require_declared_option: bool = True
    holdout_tolerance: float = 0.0005
    min_fit_gain: float = 1e-4
    max_changes: int = 6
    min_consistency_gain: float = 0.5
    max_move_size: int = 3

    def __post_init__(self):
        if type(self.max_changes) is not int or self.max_changes < 0:
            raise ValueError("max_changes must be a nonnegative integer")
        if type(self.max_move_size) is not int or self.max_move_size < 1:
            raise ValueError("max_move_size must be a positive integer")


def validate_proposal(change, spec, current, policy, n_accepted=0) -> str:
    """Validate the entire proposal before any candidate SQL is executed."""
    items = change_items(change)
    if not items or len(items) > policy.max_move_size:
        return "proposal exceeds max_move_size or contains no edits"
    if isinstance(change, CompoundChange) and (not isinstance(change.rationale, str) or not change.rationale.strip()):
        return "compound proposal needs a joint rationale"
    seen = set()
    for c in items:
        if not isinstance(c, Change):
            return "every compound edit must be a Change"
        if any(not isinstance(getattr(c, key), str) for key in ("param", "old", "new", "hypothesis", "rationale")):
            return "proposal fields must be strings"
        if c.param in seen:
            return "a compound proposal cannot edit a parameter twice"
        seen.add(c.param)
        if c.rationale.startswith("INVALID"):
            return "judge proposed an option outside the declared space"
        p = spec.params.get(c.param)
        if p is None or c.new not in p.options:
            return "option is not declared in the metric spec"
        if c.old != current[c.param]:
            return "proposal uses a stale old value"
        if c.new == c.old:
            return "proposal contains an unchanged parameter"
        if policy.require_named_hypothesis and c.hypothesis not in CATALOG:
            return f"hypothesis '{c.hypothesis}' is not in the catalog"
        if c.hypothesis != p.hypothesis:
            return f"hypothesis '{c.hypothesis}' does not match the parameter's declared reason '{p.hypothesis}'"
    if n_accepted + len(items) > policy.max_changes:
        return "too many changes; stop and review by hand"
    return ""


def accepted_changes(history) -> int:
    return sum(len(change_items(i.change)) for i in history if i.accepted and i.change)


def spread(rows: list[dict]) -> float:
    from statistics import pstdev
    rels = [r["rel"] for r in rows if r.get("split", "fit") == "fit"]
    if any(not isfinite(x) for x in rels):
        return float("inf")
    return pstdev(rels) if len(rels) > 1 else 0.0


def check(change: Change | CompoundChange, spec: MetricSpec, fit_before: float, fit_after: float,
          hold_before: float | None, hold_after: float | None, n_accepted: int, policy: Policy,
          rows_before: list[dict] | None = None, rows_after: list[dict] | None = None) -> str:
    """Return '' if the change is acceptable, otherwise the reason it is rejected."""
    if not isfinite(fit_after) or (hold_after is not None and not isfinite(hold_after)):
        return "relative variance is undefined for a nonzero result against a zero target"
    reason = validate_proposal(change, spec, {c.param: c.old for c in change_items(change)}, policy, n_accepted)
    if reason:
        return reason
    if fit_before - fit_after < policy.min_fit_gain:
        sb, sa = spread(rows_before or []), spread(rows_after or [])
        consistent = sb > 0 and (sb - sa) / sb >= policy.min_consistency_gain
        hold_ok = hold_before is None or hold_after is None or hold_after <= hold_before + 1e-9
        if not (consistent and hold_ok):
            return "does not reduce the fit variance or make the gap more consistent"
    if hold_before is not None and hold_after is not None and hold_after > hold_before + policy.holdout_tolerance:
        return "worsens the holdout; likely fitting noise"
    return ""
