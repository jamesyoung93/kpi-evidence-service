from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import __version__

COMMON_PARAMS = ("backend", "catalog", "schema", "gov_schema", "volume_path", "run_id", "ai_mode", "ai_model")
_PRINCIPAL = re.compile(r"^[A-Za-z0-9 ._@+-]+$")
_UNRESOLVED = re.compile(r"^\{\{.*\}\}$")


def _print(text: str) -> None:
    print(text, flush=True)


def _flag(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _value(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or _UNRESOLVED.match(text):
        return None
    return text


def summary_text(result: Any) -> str:
    """A short plain-text summary of a ``CheckResult`` (counts by flag, KPI verdicts, issues)."""
    lines = []
    docs = getattr(result, "documents", None)
    comps = getattr(result, "comparisons", None)
    n_docs = len(docs) if docs is not None else 0
    n_comp = len(comps) if comps is not None else 0
    lines.append(f"KPI check: {n_docs} report(s), {n_comp} comparison(s)")
    if comps is not None and n_comp and "flag" in comps.columns:
        counts = comps["flag"].value_counts()
        order = ["REVIEW", "BLOCKED", "GOOD", "INFO"]
        names = {"REVIEW": "needs review", "BLOCKED": "blocked", "GOOD": "good", "INFO": "other numbers"}
        lines.append("  " + " | ".join(f"{names.get(f, f)} {int(counts[f])}" for f in order if f in counts))
    kpis = getattr(result, "kpis", None)
    if kpis is not None and len(kpis) and {"kpi_id", "verdict"} <= set(kpis.columns):
        verdicts = [f"{r.kpi_id}: {r.verdict}" for r in kpis.itertuples() if str(r.verdict) != "not compared"]
        if verdicts:
            lines.append("  KPI verdicts: " + "; ".join(verdicts))
    issues = getattr(result, "issues", None)
    if issues is not None and len(issues):
        lines.append(f"  issues: {len(issues)} (see result.issues or the report)")
    return "\n".join(lines)


def _save_outputs(result: Any, html: str | None, excel: str | None) -> list[str]:
    written = []
    for path, method in ((html, "save_html"), (excel, "save_excel")):
        if path:
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            written.append(str(getattr(result, method)(str(path))))
    return written


def cmd_check(args: argparse.Namespace) -> int:
    """Run a ReportCheck on local files (or the demo set) and print a short summary."""
    from .check import ReportCheck

    if not args.paths and not args.demo:
        _print("error: give report files/folders, or --demo for the bundled synthetic reports")
        return 2
    options = {"brand": args.brand, "ai_mode": args.ai_mode, "strict_metadata": args.strict_metadata,
               "warehouse_id": args.warehouse_id, "catalog": args.catalog, "gov_schema": args.gov_schema}
    if args.registry:
        options["registry"] = args.registry
    if args.demo and not args.paths:
        check = ReportCheck.demo(kpi_table=not args.kpis, **options)
    else:
        check = ReportCheck(load_demo_platform=args.demo, **options)
        check.add_reports(args.paths)
    if args.kpis:
        messages = check.set_kpis(args.kpis, sheet=args.sheet)
        _print(f"KPI table: {len(messages)} row(s) read from {args.kpis}")
    result = check.run(materialize=not args.no_materialize)
    _print(summary_text(result))
    for path in _save_outputs(result, args.html, args.excel):
        _print(f"wrote {path}")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    """Lint the registry (and optionally a KPI table merged into it); exit 1 when problems are found."""
    from .check import messages_frame
    from .registry import lint, load_registry
    from .settings import Settings

    settings = Settings.from_env()
    registry = load_registry(args.registry or settings.resolved_registry_dir())
    problems = [f"registry: {p}" for p in lint(registry)]
    if args.kpis:
        from . import hints as _hints

        frame = _hints.load_kpi_table(args.kpis, sheet=args.sheet)
        hint_set = _hints.parse_hints(frame, base_registry=registry)
        merged = _hints.merge_registry(registry, hint_set)
        problems += [f"kpi table: {p}" for p in _hints.lint_for_hints(merged) or []]
        messages = messages_frame(getattr(hint_set, "messages", None))
        if not messages.empty:
            _print("KPI table messages:\n" + messages.to_string(index=False))
            level = next((c for c in ("severity", "level", "status") if c in messages.columns), None)
            if level is not None:
                bad = messages[messages[level].astype(str).str.lower().isin(["error", "invalid"])]
                problems += [f"kpi table row: {r}" for r in bad.astype(str).agg(" | ".join, axis=1)]
    _print(f"registry {registry.directory}: {len(registry.kpis)} KPI(s), version {registry.version}")
    for p in problems:
        _print(f"  - {p}")
    _print("lint: OK" if not problems else f"lint: {len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Run the bundled demo and write kpi_check_demo.html / .xlsx into ``--out``."""
    from .check import ReportCheck

    out = Path(args.out).expanduser()
    result = ReportCheck.demo(kpi_table=not args.no_kpi_table).run()
    _print(summary_text(result))
    for path in _save_outputs(result, str(out / "kpi_check_demo.html"), str(out / "kpi_check_demo.xlsx")):
        _print(f"wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kpi-evidence", description="Check KPI numbers in report files against "
                                     "platform tables and write a comparison report (nothing is published).")
    parser.add_argument("--version", action="version", version=f"kpi_evidence {__version__}")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="check report files and print a summary")
    check.add_argument("paths", nargs="*", help="report files, folders or globs")
    check.add_argument("--kpis", help="KPI table of hints (CSV or XLSX)")
    check.add_argument("--sheet", help="sheet name of an XLSX KPI table")
    check.add_argument("--html", help="write the HTML report here")
    check.add_argument("--excel", help="write the Excel workbook here")
    check.add_argument("--brand", help="brand for reports that do not name one")
    check.add_argument("--demo", action="store_true", help="use the synthetic demo platform and registry "
                       "(without paths: also the bundled sample reports)")
    check.add_argument("--registry", help="registry directory (default: auto)")
    check.add_argument("--warehouse-id", dest="warehouse_id", help="run KPI SQL on this Databricks SQL warehouse")
    check.add_argument("--catalog")
    check.add_argument("--gov-schema", dest="gov_schema")
    check.add_argument("--ai-mode", dest="ai_mode", choices=("off", "local", "ai_query", "gateway"))
    check.add_argument("--strict-metadata", dest="strict_metadata", action="store_true",
                       help="skip reports whose brand/period need confirmation instead of using the proposals")
    check.add_argument("--no-materialize", dest="no_materialize", action="store_true",
                       help="skip the platform time series and SQL artifact exports")
    check.set_defaults(func=cmd_check)

    lint_p = sub.add_parser("lint", help="lint the KPI registry (and a KPI table)")
    lint_p.add_argument("--registry", help="registry directory (default: the bundled one)")
    lint_p.add_argument("--kpis", help="KPI table to validate against the registry")
    lint_p.add_argument("--sheet")
    lint_p.set_defaults(func=cmd_lint)

    demo = sub.add_parser("demo", help="run the bundled demo and write the HTML/Excel report")
    demo.add_argument("--out", default="kpi_evidence_demo", help="output folder (default: ./kpi_evidence_demo)")
    demo.add_argument("--no-kpi-table", dest="no_kpi_table", action="store_true",
                      help="registry only, without the example KPI table")
    demo.set_defaults(func=cmd_demo)

    for name in JOB_ENTRIES:
        sub.add_parser(name, help=f"Databricks job stage {name} (--key=value parameters)",
                       add_help=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """``kpi-evidence`` console script: dispatch to a subcommand (job stages take ``--key=value``)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in JOB_ENTRIES:
        return JOB_ENTRIES[argv[0]](argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return int(args.func(args) or 0)


def parse_job_args(argv: Sequence[str] | None, *extra: str) -> argparse.Namespace:
    """``--key=value`` job parameters (common + ``extra``); unknown keys are ignored (pushed-down params)."""
    parser = argparse.ArgumentParser(add_help=False)
    for name in dict.fromkeys(COMMON_PARAMS + extra):
        flags = [f"--{name}"] + ([f"--{name.replace('_', '-')}"] if "_" in name else [])
        parser.add_argument(*flags, dest=name, default=None)
    args, _unknown = parser.parse_known_args(list(sys.argv[1:] if argv is None else argv))
    return argparse.Namespace(**{k: _value(v) for k, v in vars(args).items()})


def job_settings(args: argparse.Namespace) -> Any:
    """Settings from job parameters (given values override ``KPI_EVIDENCE_*`` environment variables)."""
    from .settings import Settings

    names = ("backend", "catalog", "schema", "gov_schema", "volume_path", "ai_mode", "ai_model", "platform_schema",
             "mlflow_experiment", "warehouse_id")
    return Settings.from_env(**{n: getattr(args, n, None) for n in names if getattr(args, n, None)})


def open_job_store(settings: Any) -> Any:
    """The service store of a job task: DuckDB locally, Delta through the task's Spark session otherwise."""
    if settings.is_local:
        from .store.duckdb_store import DuckDBStore

        return DuckDBStore(settings)
    from .store.databricks_store import DatabricksStore

    return DatabricksStore.from_runtime(settings)


def _job_context(argv: Sequence[str] | None, *extra: str) -> tuple[argparse.Namespace, Any, Any, Any, str]:
    from . import pipeline, tracing
    from .registry import load_registry

    args = parse_job_args(argv, *extra)
    settings = job_settings(args)
    tracing.configure(settings)
    store = open_job_store(settings)
    registry = load_registry(settings.resolved_registry_dir())
    return args, settings, store, registry, args.run_id or pipeline.new_run_id()


def _report(stage: str, payload: dict[str, Any]) -> None:
    _print(f"{stage}: " + json.dumps(payload, default=str, sort_keys=True))


def _sql_file(*parts: str) -> str:
    from .settings import default_sql_dir

    return default_sql_dir().joinpath(*parts).read_text(encoding="utf-8")


def apply_consumer_views(store: Any, settings: Any) -> list[str]:
    """Create the consumer views (``src/sql/jobs/consumer_views.sql``) after the service views."""
    from .store.base import check_ident, split_sql

    prefix = settings.schema_prefix(settings.schema)
    for part in prefix.split("."):
        check_ident(part)
    script = _sql_file("jobs", "consumer_views.sql").replace("${schema}", prefix)
    store.execute_script(script)
    return re.findall(r"CREATE OR REPLACE VIEW\s+([\w.]+)", " ".join(split_sql(script)))


def apply_grants(store: Any, settings: Any, *, app_sp: str | None, reviewer_group: str | None,
                 consumer_group: str | None) -> dict[str, int]:
    """Run ``src/sql/jobs/grants.sql`` with validated identifiers and principals."""
    from .store.base import check_ident, split_sql

    bindings = {"schema": settings.schema_prefix(settings.schema), "gov": settings.schema_prefix(settings.gov_schema)}
    for value in bindings.values():
        for part in value.split("."):
            check_ident(part)
    principals = {"app_sp": app_sp, "reviewer_group": reviewer_group, "consumer_group": consumer_group}
    for name, value in principals.items():
        if value is not None and (not _PRINCIPAL.match(value) or "`" in value):
            raise ValueError(f"invalid principal for {name}: {value!r}")
    applied = skipped = 0
    for statement in split_sql(_sql_file("jobs", "grants.sql")):
        names = set(re.findall(r"\$\{(\w+)\}", statement))
        if any(principals.get(n) is None for n in names & set(principals)):
            skipped += 1
            continue
        text = statement
        for name, value in {**bindings, **{k: v for k, v in principals.items() if v}}.items():
            text = text.replace("${" + name + "}", value)
        store.execute(text)
        applied += 1
    return {"applied": applied, "skipped": skipped}


def update_genie_space(settings: Any, space_id: str | None, client: Any = None) -> dict[str, Any]:
    """Replace ``__KPI_CATALOG__`` / ``__KPI_SCHEMA__`` in the deployed Genie agent."""
    from .store.base import check_ident

    if not space_id:
        return {"skipped": "no genie_space_id"}
    try:
        catalog, schema = check_ident(settings.catalog), check_ident(settings.schema)
        if client is None:
            from databricks.sdk import WorkspaceClient

            client = WorkspaceClient()
        space = client.genie.get_space(space_id, include_serialized_space=True)
        text = getattr(space, "serialized_space", None) or ""
        if "__KPI_CATALOG__" not in text and "__KPI_SCHEMA__" not in text:
            return {"updated": False, "reason": "no placeholders left"}
        new_text = text.replace("__KPI_CATALOG__", catalog).replace("__KPI_SCHEMA__", schema)
        client.genie.update_space(space_id, serialized_space=new_text)
        return {"updated": True}
    except Exception as exc:
        return {"updated": False, "error": f"{type(exc).__name__}: {exc}"[:500]}


def ensure_volume_dirs(settings: Any) -> list[str]:
    """Create ``inbox/`` and ``artifacts/`` in the reports volume (local folder, FUSE or Files API)."""
    from .store.files import files_for

    fs = files_for(settings)
    out = []
    for rel in ("inbox", "artifacts"):
        path = fs.full_path(rel)
        if fs.mode in ("local", "fuse"):
            os.makedirs(path, exist_ok=True)
        else:
            fs.client.files.create_directory(path)
        out.append(path)
    return out


def seed_samples(settings: Any) -> list[str]:
    """Copy the bundled sample reports into ``inbox/YYYY/MM/DD/<sha12>_<name>`` (fires the pipeline trigger)."""
    from .check import REPORT_EXTENSIONS
    from .ids import sha256_bytes, utcnow
    from .settings import demo_dir
    from .store.files import files_for, safe_name

    fs = files_for(settings)
    now = utcnow()
    written = []
    for path in sorted(p for p in demo_dir("reports").iterdir() if p.suffix.lower() in REPORT_EXTENSIONS):
        blob = path.read_bytes()
        rel = f"inbox/{now:%Y/%m/%d}/{sha256_bytes(blob)[:12]}_{safe_name(path.name)}"
        written.append(fs.write_bytes(rel, blob))
    return written


def bootstrap_entry(argv: Sequence[str] | None = None) -> int:
    """kpi_bootstrap: tables, demo platform (optional), governed/service/consumer views, registry sync."""
    from . import pipeline

    args, settings, store, registry, run_id = _job_context(
        argv, "platform_schema", "app_sp", "admin_group", "reviewer_group", "consumer_group", "genie_space_id",
        "load_demo", "seed_samples")
    out = pipeline.bootstrap(store, registry, settings, load_demo=_flag(args.load_demo), run_id=run_id)
    out["consumer_views"] = apply_consumer_views(store, settings)
    if not settings.is_local:
        out["grants"] = apply_grants(store, settings, app_sp=args.app_sp, reviewer_group=args.reviewer_group,
                                     consumer_group=args.consumer_group)
    out["genie"] = update_genie_space(settings, args.genie_space_id)
    out["volume"] = ensure_volume_dirs(settings)
    if _flag(args.seed_samples):
        out["seeded"] = seed_samples(settings)
    _report("bootstrap", out)
    return 0


def register_entry(argv: Sequence[str] | None = None) -> int:
    """kpi_pipeline.register: hash and record every new file under ``inbox/`` (same bytes = no-op)."""
    from . import pipeline

    args, settings, store, _registry, run_id = _job_context(argv, "trigger_location")
    docs = pipeline.register_inbox(store, settings, run_id)
    _report("register", {"run_id": run_id, "registered": len(docs), "trigger_location": args.trigger_location,
                         "documents": [d.file_name for d in docs]})
    return 0


def extract_entry(argv: Sequence[str] | None = None) -> int:
    """kpi_pipeline.extract (for each): extraction + metadata proposals for one ``--doc_id``."""
    from . import pipeline
    from .contracts import Document

    args, settings, store, registry, run_id = _job_context(argv, "doc_id")
    if not args.doc_id:
        raise ValueError("extract needs --doc_id (the for-each input {{input.doc_id}})")
    doc = store.get(Document, doc_id=args.doc_id)
    if doc is None:
        raise KeyError(f"unknown document {args.doc_id}")
    doc = pipeline.extract_stage(store, registry, settings, doc, run_id)
    _report("extract", {"run_id": run_id, "doc_id": doc.doc_id, "file_name": doc.file_name, "status": doc.status.value,
                        "error": doc.error})
    return 0


def process_entry(argv: Sequence[str] | None = None) -> int:
    """kpi_pipeline.process: DQ checks first."""
    from . import pipeline, platform
    from .contracts import Document, DocumentStatus

    _args, settings, store, registry, run_id = _job_context(argv)
    dq = pipeline.run_dq(store, registry, settings, run_id)
    try:
        dims = platform.dimension_values(store, settings)
    except Exception as exc:
        _print(f"process: dimension values unavailable ({type(exc).__name__}: {exc})")
        dims = {}
    docs = store.read(Document, "status = :s", {"s": DocumentStatus.READY.value}, order_by="registered_at, file_name")
    counts: dict[str, int] = {}
    for doc in docs:
        for r in pipeline.process_stage(store, registry, settings, doc, run_id, dq_status=dq, dimension_values=dims):
            counts[r.validation_status.value] = counts.get(r.validation_status.value, 0) + 1
    _report("process", {"run_id": run_id, "documents": len(docs), "results": counts, "dq": dq})
    return 0


def publish_entry(argv: Sequence[str] | None = None) -> int:
    """kpi_pipeline.publish: the versioned auto-publication policy (off by default: publishes nothing)."""
    from . import pipeline

    _args, settings, store, registry, run_id = _job_context(argv)
    published = pipeline.publish_stage(store, registry, settings, run_id)
    _report("publish", {"run_id": run_id, "published": published})
    return 0


def dq_entry(argv: Sequence[str] | None = None) -> int:
    """kpi_dq_and_eval.dq: data-quality checks on every governed view."""
    from . import pipeline

    _args, settings, store, registry, run_id = _job_context(argv)
    status = pipeline.run_dq(store, registry, settings, run_id)
    _report("dq", {"run_id": run_id, "views": status})
    return 0


def evaluate_entry(argv: Sequence[str] | None = None) -> int:
    """kpi_dq_and_eval.evaluate: score the pipeline on the labeled synthetic report set."""
    from .eval.evaluate import run_evaluation

    args, settings, store, _registry, _run_id = _job_context(argv, "mlflow_experiment")
    if settings.is_local:
        row = run_evaluation(settings)
    else:
        with tempfile.TemporaryDirectory(prefix="kpi_evidence_eval_", ignore_cleanup_errors=True) as tmp:
            local = dataclasses.replace(settings, backend="local", platform_backend=None, local_dir=Path(tmp),
                                        volume_path=None,
                                        ai_mode=settings.ai_mode if settings.ai_mode in ("off", "local") else "off")
            row = run_evaluation(local)
        store.insert([row])
    metrics = row.metrics or {}
    _report("evaluate", {"eval_id": row.eval_id, "dataset": row.dataset,
                         "metrics": {k: v for k, v in metrics.items() if isinstance(v, (int, float))}})
    return 0


def check_entry(argv: Sequence[str] | None = None) -> int:
    """Scheduled check (wheel task)."""
    from .check import ReportCheck
    from .ids import utcnow
    from .store.databricks_store import active_spark

    args = parse_job_args(argv, "reports", "kpis", "out_dir", "brand", "strict_metadata")
    if not args.reports or not args.out_dir:
        raise ValueError("check needs --reports <files or folder> and --out_dir <folder for the report>")
    check = ReportCheck(active_spark(), catalog=args.catalog, gov_schema=args.gov_schema, brand=args.brand,
                        ai_mode=args.ai_mode, strict_metadata=_flag(args.strict_metadata))
    check.add_reports([p.strip() for p in args.reports.split(",") if p.strip()])
    if args.kpis:
        check.set_kpis(args.kpis)
    result = check.run()
    stamp = f"{utcnow():%Y%m%dT%H%M%SZ}"
    out = Path(args.out_dir)
    written = _save_outputs(result, str(out / f"kpi_check_{stamp}.html"), str(out / f"kpi_check_{stamp}.xlsx"))
    _print(summary_text(result))
    _report("check", {"written": written})
    return 0


JOB_ENTRIES: dict[str, Callable[[Sequence[str] | None], int]] = {
    "bootstrap": bootstrap_entry,
    "register": register_entry,
    "extract": extract_entry,
    "process": process_entry,
    "publish": publish_entry,
    "dq": dq_entry,
    "evaluate": evaluate_entry,
    "run-check": check_entry,
}


if __name__ == "__main__":
    sys.exit(main())
