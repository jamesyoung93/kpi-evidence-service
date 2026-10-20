from __future__ import annotations

import glob
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PARKED_DIR = ROOT / "resources" / "parked"
OPTIONAL_FILE = PARKED_DIR / "optional.yml"
NOTEBOOK = ROOT / "notebooks" / "check_reports.py"

_VAR = re.compile(r"\$\{var\.([A-Za-z0-9_]+)\}")
_TARGET_STEP = re.compile(r"([A-Za-z0-9_]+)(?:\[([A-Za-z0-9_]+)=([A-Za-z0-9_-]+)\])?")


@dataclass
class Bundle:
    root: Path
    config: dict[str, Any]
    files: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path = ROOT) -> "Bundle":
        config = yaml.safe_load((root / "databricks.yml").read_text(encoding="utf-8"))
        files = {"databricks.yml": config}
        for pattern in config.get("include", []):
            for path in sorted(glob.glob(str(root / pattern))):
                rel = Path(path).relative_to(root).as_posix()
                files[rel] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(root=root, config=config, files=files)

    def resources(self) -> dict[str, dict[str, tuple[str, dict[str, Any]]]]:
        out: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
        for rel, doc in self.files.items():
            for rtype, items in ((doc or {}).get("resources") or {}).items():
                for key, body in (items or {}).items():
                    assert key not in out.setdefault(rtype, {}), f"{rtype}.{key} declared twice"
                    out[rtype][key] = (rel, body)
        return out

    def target_resources(self, target: str) -> dict[str, dict[str, dict[str, Any]]]:
        body = (self.config.get("targets") or {}).get(target) or {}
        return {t: dict(items or {}) for t, items in (body.get("resources") or {}).items()}

    def resource(self, rtype: str, key: str) -> dict[str, Any]:
        return self.resources()[rtype][key][1]

    def jobs(self) -> dict[str, dict[str, Any]]:
        return {k: body for k, (_f, body) in self.resources().get("jobs", {}).items()}

    def declaring_dir(self, rtype: str, key: str) -> Path:
        return (self.root / self.resources()[rtype][key][0]).parent

    def variables(self) -> dict[str, dict[str, Any]]:
        return self.config.get("variables") or {}

    def variable_values(self, target: str) -> dict[str, Any]:
        values = {k: v["default"] for k, v in self.variables().items() if isinstance(v, dict) and "default" in v}
        target_vars = ((self.config.get("targets") or {}).get(target) or {}).get("variables") or {}
        values.update(target_vars)
        return values

    def resolve_vars(self, text: str, target: str) -> str:
        values = self.variable_values(target)
        for _ in range(5):
            new = _VAR.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)
            if new == text:
                break
            text = new
        return text


def iter_strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from iter_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from iter_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def optional_blocks() -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load(OPTIONAL_FILE.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and doc, f"{OPTIONAL_FILE.name} must be a non-empty mapping"
    return doc


def optional_block(name: str) -> Any:
    blocks = optional_blocks()
    assert name in blocks, f"{OPTIONAL_FILE.name} has no block {name}"
    return blocks[name]["content"]


def optional_target(files: dict[str, Any], target: str) -> tuple[str, Any]:
    rel, sep, location = target.partition(": ")
    assert sep and rel and location, f"target {target!r}: expected '<file>: <location>'"
    assert rel in files, f"target {target!r}: {rel} is not a bundle file"
    node = files[rel]
    for step in location.split("."):
        match = _TARGET_STEP.fullmatch(step)
        assert match, f"target {target!r}: cannot read step {step!r}"
        key, sel_key, sel_value = match.groups()
        assert isinstance(node, dict) and key in node, f"target {target!r}: {key} not found"
        node = node[key]
        if sel_key is not None:
            hits = [item for item in node if isinstance(item, dict) and item.get(sel_key) == sel_value]
            assert len(hits) == 1, f"target {target!r}: {len(hits)} items with {sel_key}={sel_value}"
            node = hits[0]
    return rel, node


def iter_tasks(job: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any] | None]]:
    for task in job.get("tasks") or []:
        yield task, None
        nested = (task.get("for_each_task") or {}).get("task")
        if nested:
            yield nested, task


@pytest.fixture(scope="session")
def bundle() -> Bundle:
    return Bundle.load()


def render_local(sql: str, settings) -> str:
    return (sql.replace("${schema}", settings.schema_prefix(settings.schema))
               .replace("${gov}", settings.schema_prefix(settings.gov_schema)))


@pytest.fixture()
def view_store(mem_store):
    try:
        mem_store.ensure_views()
        names = set(mem_store.query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = :s",
            {"s": mem_store.settings.schema})["table_name"])
    except Exception as exc:
        pytest.skip(f"service views unavailable: {exc}")
    missing = {"published_kpis_current", "kpi_results_current", "review_open"} - names
    if missing:
        pytest.skip(f"service views missing: {sorted(missing)}")
    sql = (ROOT / "src" / "sql" / "jobs" / "consumer_views.sql").read_text(encoding="utf-8")
    mem_store.execute_script(render_local(sql, mem_store.settings))
    return mem_store


def make_result(**overrides: Any):
    from kpi_evidence.contracts import KpiResult, ValidationStatus

    base = dict(
        result_id="r1", result_version=1, observation_id="o1", doc_id="d1", brand="KESTREL", kpi_id="trx",
        kpi_version=1, definition_hash="h", scope_key="{}", period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31), period_grain="month", report_value=5910.0, unit="count",
        platform_value=5909.8, abs_diff=0.2, rel_diff=0.0, tolerance={}, match_basis=None,
        validation_status=ValidationStatus.VERIFIED, reason_code="match_under_governed_default",
        reason_detail="ok", explanation={}, definition_choices={}, extraction_method=None, recon_run_id=None,
        sql_artifact_id=None, dq_status="pass", is_current=True, created_at=datetime(2026, 6, 5, 12, 0))
    base.update(overrides)
    return KpiResult(**base)


def make_publication(**overrides: Any):
    from kpi_evidence.contracts import Publication, PublicationLabel, ReviewAction, ValidationStatus

    base = dict(
        publication_id="p1", brand="KESTREL", kpi_id="trx", kpi_version=1, scope_key="{}",
        period_start=date(2026, 5, 1), period_end=date(2026, 5, 31), period_grain="month", period_label="May 2026",
        value=5910.0, unit="count", display_label=PublicationLabel.MATCHES_PLATFORM,
        validation_status=ValidationStatus.VERIFIED, review_action=ReviewAction.ACCEPT, platform_value=5909.8,
        variance_rel=0.0, tolerance={}, definition_hash="h", sql_artifact_id=None, sql_sha=None,
        evidence_uri="/Volumes/c/s/reports/inbox/x.pptx", evidence_hash="d1", evidence_locator={"slide": 3},
        extraction_method=None, result_id="r1", decision_id="dec1", approved_by="system:auto-policy@v1",
        approved_at=datetime(2026, 6, 5, 12, 0), published_at=datetime(2026, 6, 5, 12, 0))
    base.update(overrides)
    return Publication(**base)


@pytest.fixture(scope="session")
def kit() -> SimpleNamespace:
    return SimpleNamespace(root=ROOT, parked_dir=PARKED_DIR, optional_file=OPTIONAL_FILE, notebook=NOTEBOOK,
                           iter_strings=iter_strings, iter_tasks=iter_tasks, optional_blocks=optional_blocks,
                           optional_block=optional_block, optional_target=optional_target,
                           render_local=render_local, make_result=make_result, make_publication=make_publication)
