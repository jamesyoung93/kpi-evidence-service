from __future__ import annotations

import tomllib
from pathlib import Path

from .extract.sources import load_sources
from .batch import select_pairs
from .metricspec import load_specs
from .storage import local_path


def _rel(base: Path, p: str) -> Path:
    return local_path(p, base)


def load_config(path: str | Path):
    path = local_path(path)
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    if "draft" in cfg and cfg["draft"].get("status") != "approved":
        raise ValueError("generated configuration needs independent review; set draft.status to approved after review")
    base = path.parent
    specs = load_specs(cfg)
    targets = load_sources(cfg["targets"], base).require_complete()
    return cfg, specs, targets


def build_runner(cfg: dict, path: str | Path, *, isolated=False):
    base = local_path(path).parent
    r = cfg.get("runner", {"type": "duckdb"})
    if r.get("type", "duckdb") == "duckdb":
        from .runners.duckdb_runner import DuckDBRunner
        return DuckDBRunner({k: _rel(base, v) for k, v in (r.get("tables") or {}).items()})
    if r["type"] == "spark":
        from pyspark.sql import SparkSession
        from .runners.spark_runner import SparkRunner
        spark = SparkSession.builder.getOrCreate()
        return SparkRunner(spark.newSession() if isolated else spark)
    raise ValueError(f"unknown runner type {r['type']}")


def run_config(path: str | Path, judge=None, out_dir: str | Path | None = None, log=print,
               *, reports: list[str] | None = None, metrics: list[str] | None = None,
               judge_factory=None, runner_factory=None, parallel=False, max_workers=4,
               precompute=False, max_candidates=128, max_queries=1000, task_wrapper=None):
    """Run every report x metric automatically with a judge (default: SearchJudge)."""
    from .guardrails import Policy
    from .judge import SearchJudge, StatedRuleJudge
    from .execution import ordered_map, worker_count, precompute_fit
    from .diagnostics import post_close
    from copy import deepcopy
    from .loop import reconcile
    from .record import write
    cfg, specs, targets = load_config(path)
    groups = select_pairs(specs, targets, reports=reports, metrics=metrics)
    from .coupled import load_groups, reconcile_coupled
    coupled = load_groups(cfg, specs)
    selected_pairs = {(r, m) for r, m, _ in groups}
    for group in coupled:
        for report in {r for r, _, _ in groups}:
            present = {m for r, m in selected_pairs if r == report} & set(group.metrics)
            if present and present != set(group.metrics):
                raise ValueError(f"select all members of coupled group {group.name!r} for report {report!r}")
    if judge is not None and judge_factory is not None:
        raise ValueError("provide judge or judge_factory, not both")
    worker_count(max_workers)
    if parallel and judge is not None and (type(judge) not in {SearchJudge, StatedRuleJudge}
            or (type(judge) is StatedRuleJudge and type(judge.fallback) is not SearchJudge)):
        raise ValueError("parallel custom/model judges require judge_factory for independent state")
    cache = None
    if cfg.get("cache"):
        from .runners.persistent import PersistentCache
        declaration = dict(cfg["cache"])
        declaration["path"] = local_path(declaration["path"], local_path(path).parent)
        cache = PersistentCache(**declaration)
    if parallel and runner_factory is None and cfg.get("runner", {}).get("type") == "spark":
        from pyspark.sql import SparkSession
        from .runners.spark_runner import SparkRunner
        spark = SparkSession.builder.getOrCreate()
        runner_factory = lambda: SparkRunner(spark.newSession())
    def make_runner():
        runner = runner_factory() if runner_factory else build_runner(cfg, path, isolated=parallel)
        if cache:
            from .runners.persistent import CachedRunner
            runner = CachedRunner(runner, cache)
        return runner
    policy = Policy(**cfg.get("policy", {}))
    judge = judge or SearchJudge()
    start = cfg.get("start", {})
    units = []
    completed = set()
    for group in coupled:
        for report in sorted({r for r, _, _ in groups}):
            if (report, group.metrics[0]) not in selected_pairs:
                continue
            ts = [t for t in targets if t.report == report and t.metric in group.metrics]
            units.append((report, group, ts))
            completed.update((report, m) for m in group.metrics)
    for report, metric, ts in groups:
        if (report, metric) in completed:
            continue
        units.append((report, metric, ts))
    def execute(unit, runner, emit):
        report, member, ts = unit
        if isinstance(member, str):
            pair_judge = judge_factory(report, member) if judge_factory else deepcopy(judge) if parallel else judge
            warming = precompute_fit(specs[member], ts, runner, max_candidates=max_candidates, max_queries=max_queries) if precompute else None
            result = [reconcile(specs[member], ts, runner, pair_judge, policy, start=start.get(member), log=emit)]
        else:
            warming = None
            result = reconcile_coupled(member, specs, ts, runner, policy=policy, start=start, log=emit)
        for record in result:
            post_close(record, specs[record.metric], [t for t in ts if t.metric == record.metric], runner,
                       cfg.get("diagnostics"), reference_targets=targets)
            if warming:
                record.diagnostics["precompute"] = warming
        return result
    def close_runner(runner):
        if hasattr(runner, "close"):
            runner.close()
    if parallel:
        def task(unit):
            runner, messages = make_runner(), []
            try:
                return execute(unit, runner, messages.append), messages
            finally:
                close_runner(runner)
        results = ordered_map(task_wrapper(task) if task_wrapper else task, units, max_workers)
        records = [r for group_records, _ in results for r in group_records]
        for _, messages in results:
            for message in messages:
                log(message)
    else:
        runner, records = make_runner(), []
        try:
            for unit in units:
                records.extend(execute(unit, runner, log))
        finally:
            close_runner(runner)
    if out_dir:
        write(records, out_dir)
    return sorted(records, key=lambda r: (r.report, r.metric))
