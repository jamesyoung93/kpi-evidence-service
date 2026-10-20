from __future__ import annotations

from .compare import classify_residual, evaluate, score, split_targets, summarize_result
from .guardrails import Policy, check, validate_proposal, accepted_changes
from .hypotheses import CATALOG, RESIDUAL_HINTS
from .metricspec import MetricSpec
from .schema import Change, CompoundChange, Iteration, ReconRecord, Target, change_items, change_key
from .batch import select_pairs


class ReconSession:
    def __init__(self, specs: dict[str, MetricSpec], targets: list[Target], runner, policy: Policy | None = None,
                 start: dict[str, dict[str, str]] | None = None):
        self.specs, self.targets, self.runner = specs, targets, runner
        self.policy = policy or Policy()
        self.start = start or {}
        self._pairs = [(r, m) for r, m, _ in select_pairs(specs, targets)]
        self.records: list[ReconRecord] = []
        self.spec_changes: list[dict] = []
        self._cur = None
        self._deferred = {}
        self.diagnostic_config = {}

    @classmethod
    def from_config(cls, path: str, runner=None):
        from .config import load_config, build_runner
        cfg, specs, targets = load_config(path)
        if cfg.get("coupled"):
            raise ValueError("coupled constraints require run_config/reconcile_coupled; independent interactive edits cannot preserve group identities")
        session = cls(specs, targets, runner or build_runner(cfg, path), Policy(**cfg.get("policy", {})), cfg.get("start"))
        session.diagnostic_config = cfg.get("diagnostics", {})
        return session

    def pairs(self) -> list[tuple[str, str]]:
        """All (report, metric) pairs that have targets and a spec."""
        return list(self._pairs)

    def pending_pairs(self, report: str | None = None) -> list[tuple[str, str]]:
        """Pairs without a closed record; the active pair stays pending until close()."""
        if report is not None and report not in {r for r, _ in self._pairs}:
            raise ValueError(f"unknown report: {report}")
        closed = {(r.report, r.metric) for r in self.records} | self._deferred.keys()
        return [(r, m) for r, m in self._pairs if (r, m) not in closed and (report is None or r == report)]

    def open_next(self, report: str | None = None) -> str:
        """Open the next unfinished KPI; refuses to discard an active pair."""
        if self._cur is not None:
            raise RuntimeError("close the current pair before opening another")
        pending = self.pending_pairs(report)
        if pending:
            return self.open(*pending[0])
        return "NO PENDING PAIRS; DEFERRED PAIRS REMAIN" if self.deferred_pairs(report) else "ALL PAIRS CLOSED"

    def progress(self) -> list[dict]:
        """Report-wide progress without exposing holdout values."""
        closed = {(r.report, r.metric): r.status for r in self.records}
        active = (self._cur["report"], self._cur["spec"].name) if self._cur else None
        return [dict(report=r, metric=m, status="active" if (r, m) == active else
                     "deferred" if (r, m) in self._deferred else closed.get((r, m), "pending"),
                     **({"reason": self._deferred[(r, m)]["reason"], "depends_on": self._deferred[(r, m)]["depends_on"]}
                        if (r, m) in self._deferred else {}))
                for r, m in self._pairs]

    def deferred_pairs(self, report=None):
        return [pair for pair in self._pairs if pair in self._deferred and (report is None or pair[0] == report)]

    def defer(self, reason, *, depends_on=()):
        """Park an active pair without losing accepted/rejected steps or edit budgets."""
        from datetime import datetime, timezone
        c = self._need()
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("deferral requires a nonempty reason")
        pair = (c["report"], c["spec"].name)
        dependencies = [tuple(p) for p in depends_on]
        if len(set(dependencies)) != len(dependencies) or any(p not in self._pairs or p == pair for p in dependencies):
            raise ValueError("dependencies must be distinct other configured pairs")
        graph = {p: state["depends_on"] for p, state in self._deferred.items()} | {pair: dependencies}
        def cycle(node, trail):
            if node in trail:
                return True
            return any(cycle(child, trail | {node}) for child in graph.get(node, []))
        if cycle(pair, set()):
            raise ValueError("deferral dependencies cannot form a cycle")
        c.setdefault("lifecycle", []).append(dict(action="deferred", reason=reason, depends_on=dependencies,
                                                  at=datetime.now(timezone.utc).isoformat()))
        self._deferred[pair] = dict(state=c, reason=reason, depends_on=dependencies)
        self._cur = None
        return f"DEFERRED {pair[0]} / {pair[1]}: {reason}"

    def resume(self, report, metric):
        from datetime import datetime, timezone
        if self._cur is not None:
            raise RuntimeError("close or defer the active pair before resuming another")
        pair = (report, metric)
        parked = self._deferred.get(pair)
        if parked is None:
            raise ValueError("pair is not deferred")
        resolved = {(r.report, r.metric) for r in self.records if r.status == "reconciled"}
        if set(parked["depends_on"]) - resolved:
            raise RuntimeError("dependent pairs must reconcile before this pair can resume")
        self._cur = self._deferred.pop(pair)["state"]
        self._cur.setdefault("lifecycle", []).append(dict(action="resumed", at=datetime.now(timezone.utc).isoformat()))
        return self.status()

    def checkpoint(self, path, *, source_version):
        from .checkpoint import save_session
        return save_session(self, path, source_version=source_version)

    def restore(self, path, *, source_version):
        from .checkpoint import restore_session
        restore_session(self, path, source_version=source_version)
        return self.progress()

    def open(self, report: str, metric: str, start: dict[str, str] | None = None) -> str:
        if self._cur is not None:
            raise RuntimeError("close the current pair before opening another")
        if any((r.report, r.metric) == (report, metric) for r in self.records):
            raise ValueError("pair already closed; use a new session to rerun it")
        if (report, metric) in self._deferred:
            raise ValueError("pair is deferred; use resume to preserve its progress")
        spec = self.specs[metric]
        ts = [t for t in self.targets if t.report == report and t.metric == metric]
        if not ts:
            raise ValueError(f"no targets for {report} / {metric}")
        fit, hold = split_targets(ts, metric)
        params = spec.initial_choices(self.start.get(metric) if start is None else start)
        rows, f, h = self._measure(spec, params, fit, hold)
        self._cur = dict(spec=spec, report=report, fit=fit, hold=hold, params=params, start=dict(params),
                         history=[Iteration(step=0, params=dict(params), fit_variance=f, holdout_variance=h, per_target=rows)],
                         tried=set(), tried_moves=set())
        return self.status()

    def status(self) -> str:
        """Plain-text state for the agent: gap, shape, stated rule, allowed choices, history."""
        c = self._need()
        spec, last = c["spec"], self._last()
        fit_rows = [r for r in last.per_target if r["split"] == "fit"]
        pattern = classify_residual(fit_rows, spec.tolerance)
        lines = [f"RECONCILING {c['report']} / {spec.name}",
                 f"Stated rule in the report: {c['fit'][0].stated_rule or '(none given)'}",
                 f"Current definition: {spec.describe(c['params'])}",
                 f"Fit variance: {last.fit_variance:.2%} (tolerance {spec.tolerance:.2%})",
                 f"Gap shape: {pattern}" + (f" -> usually {', '.join(RESIDUAL_HINTS[pattern])}" if pattern in RESIDUAL_HINTS else ""),
                 "Per period (fit):"]
        lines += [f"  {r['as_of']}: gold {r['gold']:,.6g} | computed {r['computed']:,.6g} | {r['rel']:+.2%}" for r in fit_rows]
        lines.append("Allowed choices (param [reason]: options):")
        for k, p in spec.params.items():
            opts = ", ".join(o + ("*" if o == c["params"][k] else "") for o in p.options)
            lines.append(f"  {k} [{p.hypothesis}]: {opts}")
        lines.append("Already tried: " + (", ".join(f"{a}={b}" for a, b in sorted(c["tried"])) or "none"))
        lines.append(f"Compound proposals: try_changes([...], rationale); up to {self.policy.max_move_size} edits atomically.")
        for name, move in spec.moves.items():
            lines.append(f"  Declared move {name}: {move.options}; {move.rationale}")
        lines.append("Reasons (hypothesis keys): " + ", ".join(CATALOG))
        lines.append("DONE: fit is within tolerance; close() records the final holdout check." if last.fit_variance <= spec.tolerance else
                     "NEXT: call try_change(param, option, hypothesis, rationale), or close() if nothing defensible is left.")
        return "\n".join(lines)

    def try_change(self, param: str, option: str, hypothesis: str, rationale: str, by: str = "agent") -> str:
        c = self._need()
        change = Change(param=param, old=c["params"].get(param, ""), new=option, hypothesis=hypothesis,
                        rationale=rationale, proposed_by=by)
        return self._try(change)

    def try_changes(self, changes: list[dict], rationale: str, by: str = "agent") -> str:
        """Each edit has param, option, hypothesis, and rationale; all commit or none do."""
        c = self._need()
        edits = [Change(param=d["param"], old=c["params"].get(d["param"], ""), new=d["option"],
                        hypothesis=d["hypothesis"], rationale=d["rationale"], proposed_by=by) for d in changes]
        return self._try(CompoundChange(edits, rationale, by))

    def _try(self, change) -> str:
        c = self._need()
        spec, last = c["spec"], self._last()
        n_acc = accepted_changes(c["history"])
        reason = validate_proposal(change, spec, c["params"], self.policy, n_acc)
        tf, th, rows = last.fit_variance, last.holdout_variance, last.per_target
        trial = dict(c["params"])
        if not reason:
            trial.update({edit.param: edit.new for edit in change_items(change)})
            try:
                rows, tf, th = self._measure(spec, trial, c["fit"], c["hold"])
            except Exception as exc:
                tf, th, rows = last.fit_variance, last.holdout_variance, last.per_target
                reason = f"execution failed: {exc}"
            else:
                reason = check(change, spec, last.fit_variance, tf, last.holdout_variance, th, n_acc, self.policy, last.per_target, rows)
        c["history"].append(Iteration(step=len(c["history"]), params=trial, fit_variance=tf, holdout_variance=th,
                                      per_target=rows, change=change, accepted=(reason == ""), reject_reason=reason))
        items = change_items(change)
        if len(items) == 1:
            c["tried"].add((items[0].param, items[0].new))
        c["tried_moves"].add(change_key(change))
        if reason:
            return f"REJECTED: {reason}\n\n" + self.status()
        c["params"] = trial
        c["tried"].clear()
        c["tried_moves"].clear()
        description = "; ".join(f"{e.param} {e.old} -> {e.new} ({e.hypothesis})" for e in items)
        return f"ACCEPTED: {description}. Fit now {tf:.2%}.\n\n" + self.status()

    def add_option(self, param: str, name: str, sql_fragment: str, rationale: str, by: str = "agent") -> str:
        """Propose a NEW definitional choice (real data engineering)."""
        c = self._need()
        p = c["spec"].params[param]
        if name in p.options:
            return f"option '{name}' already exists"
        p.options[name] = sql_fragment
        self.spec_changes.append(dict(metric=c["spec"].name, param=param, option=name, sql=sql_fragment,
                                      hypothesis=p.hypothesis, rationale=rationale, by=by, status="needs_review"))
        return f"Added option {param}={name} (needs human review). Now call try_change('{param}', '{name}', '{p.hypothesis}', ...)."

    def close(self) -> ReconRecord:
        c = self._need()
        spec, final = c["spec"], self._last()
        status, residual = summarize_result(final, spec.tolerance)
        new_opts = [s for s in self.spec_changes if s["metric"] == spec.name and c["params"].get(s["param"]) == s["option"]]
        if new_opts:
            residual.note += " Uses agent-added option(s) awaiting review: " + ", ".join(f"{s['param']}={s['option']}" for s in new_opts) + "."
        rec = ReconRecord(report=c["report"], metric=spec.name, start_params=c["start"], final_params=dict(c["params"]),
                          final_query=spec.render(c["params"], c["fit"][0].as_of), tolerance=spec.tolerance, status=status,
                          iterations=c["history"], residual=residual, lifecycle=c.get("lifecycle", []))
        from .diagnostics import post_close
        post_close(rec, spec, c["fit"] + c["hold"], self.runner, self.diagnostic_config, reference_targets=self.targets)
        self.records.append(rec)
        self._cur = None
        return rec

    def report(self, out_dir: str = "reconloop_out") -> str:
        from .record import write
        import json
        md = write(self.records, out_dir)
        (md.parent / "session_progress.json").write_text(json.dumps(self.progress(), indent=2), encoding="utf-8")
        pending, deferred = len(self.pending_pairs()), len(self._deferred)
        if pending or deferred:
            with md.open("a", encoding="utf-8") as stream:
                stream.write(f"\nSession queue: {pending} pending/active, {deferred} deferred. Records above cover closed pairs only.\n")
        if self.spec_changes:
            with (md.parent / "spec_changes_for_review.json").open("w", encoding="utf-8") as f:
                json.dump(self.spec_changes, f, indent=2)
        return str(md)

    def _measure(self, spec, params, fit, hold):
        rf = evaluate(self.runner, spec, params, fit)
        rh = evaluate(self.runner, spec, params, hold) if hold else []
        return rf + rh, score(rf), (score(rh) if hold else None)

    def _last(self) -> Iteration:
        return next(i for i in reversed(self._cur["history"]) if i.accepted is not False)

    def _need(self):
        if self._cur is None:
            raise RuntimeError("call open(report, metric) first")
        return self._cur
