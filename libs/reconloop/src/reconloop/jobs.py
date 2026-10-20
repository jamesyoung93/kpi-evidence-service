from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from .config import run_config
from .extract.pdf import ExtractionError


def run_job(config, out_dir, **kwargs):
    from .storage import local_path
    config, out_dir = local_path(config), local_path(out_dir)
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12]
    out = Path(out_dir) / run_id
    out.mkdir(parents=True, exist_ok=False)
    manifest = dict(run_id=run_id, started_at=started.isoformat(), status="running", output_dir=str(out))
    try:
        manifest["config_sha256"] = sha256(Path(config).read_bytes()).hexdigest()
        records = run_config(config, out_dir=out, **kwargs)
        counts = {s: sum(r.status == s for r in records) for s in ("reconciled", "explained_residual", "unresolved")}
        manifest.update(counts=counts, status="complete" if all(r.status == "reconciled" for r in records) else "needs_review")
    except ExtractionError as exc:
        exc.result.write(out / "extraction")
        manifest.update(status="needs_review", error=str(exc), phase="extraction")
    except Exception as exc:
        manifest.update(status="failed", error=str(exc))
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    (out / "run.json").write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
    return manifest


def batch_run(configs, out_dir, *, max_workers=4, **kwargs):
    """Bounded driver-side config fan-out; each batch member retains its own manifest."""
    from .execution import ordered_map, worker_count
    from .storage import local_path
    worker_count(max_workers)
    if not configs:
        raise ValueError("batch needs at least one config")
    if kwargs.get("parallel"):
        raise ValueError("choose config fan-out or metric parallelism; nested pools are disabled")
    if "judge" in kwargs or "runner_factory" in kwargs:
        raise ValueError("batch workers cannot share a judge or runner factory; use judge_factory and configured runners")
    out = local_path(out_dir) / ("batch-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:12])
    out.mkdir(parents=True, exist_ok=False)
    def run(config):
        try:
            return run_job(config, out, **kwargs)
        except Exception as exc:
            return dict(status="failed", config=str(config), error=str(exc))
    runs = ordered_map(run, list(configs), max_workers)
    status = "failed" if any(r["status"] == "failed" for r in runs) else "needs_review" if any(r["status"] != "complete" for r in runs) else "complete"
    manifest = dict(status=status, runs=runs, output_dir=str(out), max_workers=max_workers)
    (out / "batch.json").write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
    return manifest
