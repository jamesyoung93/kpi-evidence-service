from dataclasses import dataclass, field
from itertools import product
from math import isfinite, prod

from .batch import select_pairs
from .compare import evaluate, score, summarize_result
from .guardrails import Policy, validate_proposal
from .hypotheses import CATALOG
from .schema import Change, CompoundChange, Iteration, ReconRecord


@dataclass
class Identity:
    name: str
    left: str
    right: str
    factor: float = 1.0
    absolute_tolerance: float = 1e-8
    relative_tolerance: float = 1e-6

    def __post_init__(self):
        if not self.name or not isfinite(self.factor) or self.factor == 0:
            raise ValueError("identity needs a name and a finite nonzero conversion factor")
        if any(not isfinite(v) or v < 0 for v in (self.absolute_tolerance, self.relative_tolerance)):
            raise ValueError("identity tolerances must be finite and nonnegative")


@dataclass
class CoupledGroup:
    name: str
    metrics: list[str]
    identities: list[Identity]
    parameters: dict[str, list[str]] = field(default_factory=dict)
    shared: dict[str, list[str]] = field(default_factory=dict)
    max_candidates: int = 1000


def load_groups(config, specs):
    groups, used = [], set()
    for name, value in config.get("coupled", {}).items():
        group = CoupledGroup(name=name, **{k: v for k, v in value.items() if k != "identities"},
                             identities=[Identity(**i) for i in value.get("identities", [])])
        validate_group(group, specs)
        if used & set(group.metrics):
            raise ValueError("a metric cannot belong to overlapping coupled groups")
        used.update(group.metrics)
        groups.append(group)
    return groups


def _reference(reference, group, specs, *, parameter=False):
    parts = reference.rsplit(".", 1)
    if len(parts) != 2 or parts[0] not in group.metrics:
        raise ValueError(f"invalid coupled reference: {reference!r}")
    metric, field = parts
    if parameter:
        if field not in specs[metric].params:
            raise ValueError(f"unknown shared parameter: {reference}")
    elif field not in {"value", "numerator", "denominator"} or (field != "value" and specs[metric].ratio is None):
        raise ValueError(f"invalid metric component: {reference}")
    return metric, field


def validate_group(group, specs):
    if len(group.metrics) < 2 or len(set(group.metrics)) != len(group.metrics) or set(group.metrics) - specs.keys():
        raise ValueError("a coupled group needs at least two distinct configured metrics")
    if not group.identities and not group.shared:
        raise ValueError("a coupled group needs identities or shared choices")
    if type(group.max_candidates) is not int or not 1 <= group.max_candidates <= 100_000:
        raise ValueError("max_candidates must be between 1 and 100000")
    if len({i.name for i in group.identities}) != len(group.identities):
        raise ValueError("identity names must be unique")
    for identity in group.identities:
        _reference(identity.left, group, specs)
        _reference(identity.right, group, specs)
    for metric, names in group.parameters.items():
        if metric not in group.metrics or len(set(names)) != len(names) or set(names) - specs[metric].params.keys():
            raise ValueError("search parameters must be distinct configured names within the group")
    for refs in group.shared.values():
        if len(refs) < 2 or len(set(refs)) != len(refs):
            raise ValueError("shared choices need at least two distinct parameter references")
        for ref in refs:
            _reference(ref, group, specs, parameter=True)


def _shared_ok(group, choices):
    return all(len({choices[m][p] for m, p in (ref.rsplit(".", 1) for ref in refs)}) == 1
               for refs in group.shared.values())


def _identities(group, measurements):
    def value(row, component):
        return row["computed"] if component == "value" else row["components"][component]
    evidence = []
    for identity in group.identities:
        lm, lf = identity.left.rsplit(".", 1)
        rm, rf = identity.right.rsplit(".", 1)
        right = {row["as_of"]: row for row in measurements[rm]}
        for row in measurements[lm]:
            lvalue, rvalue = value(row, lf), value(right[row["as_of"]], rf) * identity.factor
            limit = identity.absolute_tolerance + identity.relative_tolerance * max(abs(lvalue), abs(rvalue))
            difference = lvalue - rvalue
            passed = all(isfinite(v) for v in (lvalue, rvalue, difference, limit)) and abs(difference) <= limit
            evidence.append(dict(name=identity.name, as_of=row["as_of"], split=row["split"],
                                 left=identity.left, right=identity.right, left_value=lvalue,
                                 right_value=rvalue, factor=identity.factor, difference=difference,
                                 tolerance=limit, passed=passed))
    return evidence


def _proposal(metric, spec, before, after):
    changes = [Change(p, before[p], option, spec.params[p].hypothesis,
                      "Joint fit search under declared cross-metric identities.", "coupled_search")
               for p, option in after.items() if before[p] != option]
    if not changes:
        return None
    return changes[0] if len(changes) == 1 else CompoundChange(changes, "These choices satisfy the group definition together.", "coupled_search")


def reconcile_coupled(group, specs, targets, runner, *, policy=None, start=None, log=print):
    validate_group(group, specs)
    policy, start = policy or Policy(), start or {}
    selected = select_pairs(specs, targets, metrics=group.metrics)
    if len({report for report, _, _ in selected}) != 1 or {m for _, m, _ in selected} != set(group.metrics):
        raise ValueError("coupled targets must contain every group metric for exactly one report")
    report = selected[0][0]
    data = {m: ts for _, m, ts in selected}
    coverage = [{(t.as_of, t.split) for t in ts} for ts in data.values()]
    if any(len({t.as_of for t in ts}) != len(ts) for ts in data.values()) or any(c != coverage[0] for c in coverage[1:]):
        raise ValueError("coupled metrics must have one target per date and identical date/split coverage")
    fit = {m: [t for t in data[m] if t.split == "fit"] for m in group.metrics}
    hold = {m: [t for t in data[m] if t.split == "holdout"] for m in group.metrics}
    initial = {m: specs[m].initial_choices(start.get(m)) for m in group.metrics}
    dimensions = [(m, p, list(specs[m].params[p].options)) for m in group.metrics
                  for p in group.parameters.get(m, list(specs[m].params))]
    size = prod(len(options) for _, _, options in dimensions)
    if size > group.max_candidates:
        raise ValueError(f"coupled search space has {size} candidates, exceeds {group.max_candidates}; narrow parameters")
    for m, p, _ in dimensions:
        if specs[m].params[p].hypothesis not in CATALOG and policy.require_named_hypothesis:
            raise ValueError(f"{m}.{p}: unknown declared hypothesis")
    measure = lambda choices, subset: {m: evaluate(runner, specs[m], choices[m], subset[m]) for m in group.metrics}
    baseline_fit = measure(initial, fit)
    baseline_evidence = _identities(group, baseline_fit)
    baseline_scores = {m: score(rows) for m, rows in baseline_fit.items()}
    baseline_valid = _shared_ok(group, initial) and all(e["passed"] for e in baseline_evidence)
    baseline_done = baseline_valid and all(baseline_scores[m] <= specs[m].tolerance for m in group.metrics)
    best, best_rank, attempts = None, None, []
    if not baseline_done:
        for values in product(*(options for _, _, options in dimensions)):
            choices = {m: dict(initial[m]) for m in group.metrics}
            for (m, p, _), option in zip(dimensions, values):
                choices[m][p] = option
            edits = sum(initial[m][p] != choices[m][p] for m in group.metrics for p in initial[m])
            if not edits or edits > policy.max_changes or not _shared_ok(group, choices):
                continue
            proposals = {m: _proposal(m, specs[m], initial[m], choices[m]) for m in group.metrics}
            if any(validate_proposal(c, specs[m], initial[m], policy) for m, c in proposals.items() if c):
                continue
            attempt = dict(params=choices)
            try:
                rows = measure(choices, fit)
                evidence = _identities(group, rows)
                scores = {m: score(rows[m]) for m in group.metrics}
                valid = all(e["passed"] for e in evidence) and all(isfinite(s) for s in scores.values())
                attempt.update(fit=scores, identities=evidence, eligible=valid)
                if valid:
                    rank = (sum(scores[m] > specs[m].tolerance for m in group.metrics),
                            sum(scores.values()) / len(scores), edits)
                    if best_rank is None or rank < best_rank:
                        best, best_rank = (choices, rows, scores, evidence, proposals), rank
            except Exception as exc:
                attempt.update(eligible=False, error=str(exc))
            attempts.append(attempt)
    baseline_hold = measure(initial, hold)
    baseline_hold_evidence = _identities(group, baseline_hold)
    reason, accepted = "", False
    trial_hold, trial_hold_evidence = {}, []
    if best is not None:
        choices, trial_fit, trial_scores, trial_evidence, proposals = best
        before, after = sum(baseline_scores.values()), sum(trial_scores.values())
        repairs_identity = not baseline_valid and all(trial_scores[m] <= baseline_scores[m] + policy.min_fit_gain for m in group.metrics)
        if before - after < policy.min_fit_gain and not repairs_identity:
            reason = "joint proposal does not improve fit or repair an identity without worsening member fits"
        else:
            try:
                trial_hold = measure(choices, hold)
                trial_hold_evidence = _identities(group, trial_hold)
                if any(not e["passed"] for e in trial_hold_evidence):
                    reason = "joint proposal violates a holdout identity"
                elif any(not isfinite(score(trial_hold[m])) for m in group.metrics if hold[m]):
                    reason = "joint proposal has undefined holdout variance"
                elif any(score(trial_hold[m]) > score(baseline_hold[m]) + policy.holdout_tolerance for m in group.metrics if hold[m]):
                    reason = "joint proposal worsens a member's holdout"
            except Exception as exc:
                reason = f"joint holdout execution failed: {exc}"
        accepted = not reason
    final_params = best[0] if accepted else initial
    final_constraints = (best[3] + trial_hold_evidence) if accepted else (baseline_evidence + baseline_hold_evidence)
    audit = dict(group=group.name, metrics=group.metrics, search_space=size, fit_candidates=attempts,
                 fit_selected=best[0] if best else None, accepted=accepted if best else None,
                 reject_reason=reason, final_identities=final_constraints,
                 shared_choices_satisfied=_shared_ok(group, final_params),
                 candidate_holdout_identities=trial_hold_evidence,
                 selection="fit_only; one holdout decision; no retries")
    records = []
    for m in group.metrics:
        bf, bh = score(baseline_fit[m]), score(baseline_hold[m]) if hold[m] else None
        history = [Iteration(0, initial[m], bf, bh, baseline_fit[m] + baseline_hold[m])]
        if best:
            history.append(Iteration(1, best[0][m], score(best[1][m]),
                                     score(trial_hold[m]) if hold[m] and m in trial_hold else None,
                                     best[1][m] + trial_hold.get(m, []), change=best[4][m],
                                     accepted=accepted, reject_reason=reason))
        final = next(i for i in reversed(history) if i.accepted is not False)
        status, residual = summarize_result(final, specs[m].tolerance)
        records.append(ReconRecord(report, m, initial[m], final_params[m],
                                   specs[m].render(final_params[m], data[m][0].as_of), specs[m].tolerance,
                                   status, history, residual, coupling=audit))
    valid_group = (_shared_ok(group, final_params) and all(e["passed"] for e in final_constraints)
                   and all(r.status == "reconciled" for r in records))
    if not valid_group:
        for record in records:
            record.status = "unresolved"
            record.residual.note += f" Coupled group {group.name!r} is not jointly reconciled; review all members."
    log(f"[{report} / group {group.name}] {len(attempts)} fit candidates; "
        + ("jointly reconciled" if valid_group else "unresolved") + (f"; {reason}" if reason else ""))
    from .diagnostics import diagnose_record
    for record in records:
        diagnose_record(record)
    return records
