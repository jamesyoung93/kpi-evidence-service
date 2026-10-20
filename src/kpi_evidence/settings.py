from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent


def _repo_root() -> Path | None:
    for parent in PACKAGE_DIR.parents:
        if (parent / "pyproject.toml").exists() and (parent / "registry").is_dir():
            return parent
    return None


def default_registry_dir() -> Path:
    packaged = PACKAGE_DIR / "_registry"
    if packaged.is_dir():
        return packaged
    root = _repo_root()
    if root is None:
        raise FileNotFoundError("registry directory not found; set KPI_EVIDENCE_REGISTRY_DIR")
    return root / "registry"


def default_sql_dir() -> Path:
    packaged = PACKAGE_DIR / "_sql"
    if packaged.is_dir():
        return packaged
    root = _repo_root()
    if root is None:
        raise FileNotFoundError("sql directory not found")
    return root / "src" / "sql"


def _wheel_source(root: Path, target: str) -> Path | None:
    import tomllib

    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    wheel = data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    for source, dest in sorted(wheel.get("sources", {}).items(), key=lambda item: -len(item[1])):
        dest = dest.rstrip("/")
        if target == dest or target.startswith(dest + "/"):
            return root / source / target[len(dest):].lstrip("/")
    return None


def demo_dir(kind: str) -> Path:
    """kind = 'platform' (synthetic platform CSVs) or 'reports' (synthetic report files)."""
    packaged = PACKAGE_DIR / "_demo" / kind
    if packaged.is_dir():
        return packaged
    root = _repo_root()
    source = _wheel_source(root, f"kpi_evidence/_demo/{kind}") if root is not None else None
    if source is None:
        raise FileNotFoundError(f"demo {kind} directory not found")
    return source


AI_MODES = ("off", "local", "ai_query", "gateway")


@dataclass
class Settings:
    backend: str = "local"
    catalog: str = "main"
    platform_backend: str | None = None
    platform_catalog: str | None = None
    schema: str = "kpi_evidence"
    gov_schema: str = "kpi_gov"
    platform_schema: str = "platform_demo"
    restricted_schemas: tuple[str, ...] = ("platform_restricted",)
    local_dir: Path = field(default_factory=lambda: Path(".kpi_evidence"))
    volume_path: str | None = None
    warehouse_id: str | None = None
    registry_dir: Path | None = None
    ai_mode: str = "local"
    ai_model: str = "system.ai.claude-sonnet-4-6"
    ai_gateway_base_url: str | None = None
    genie_space_id: str | None = None
    genie_drafting_space_id: str | None = None
    pipeline_job_id: str | None = None
    mlflow_experiment: str | None = None
    tracing: bool = False
    metadata_confidence_threshold: float = 0.8
    mapping_model_floor: float = 0.6
    phase_b_max_iter: int = 8
    grid_max_candidates: int = 16
    reviewer_identity: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides: Any) -> "Settings":
        env = os.environ
        values: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            key = "KPI_EVIDENCE_" + f.name.upper()
            if key in env and f.name not in overrides:
                values[f.name] = _coerce(f.type, env[key])
        values.update({k: v for k, v in overrides.items() if v is not None})
        s = cls(**values)
        s.local_dir = Path(s.local_dir)
        if s.registry_dir is not None:
            s.registry_dir = Path(s.registry_dir)
        if s.ai_mode not in AI_MODES:
            raise ValueError(f"ai_mode must be one of {AI_MODES}, got {s.ai_mode!r}")
        if s.backend not in ("local", "databricks"):
            raise ValueError("backend must be 'local' or 'databricks'")
        if s.platform_backend not in (None, "local", "databricks"):
            raise ValueError("platform_backend must be None, 'local' or 'databricks'")
        return s

    @property
    def is_local(self) -> bool:
        return self.backend == "local"

    def resolved_registry_dir(self) -> Path:
        return Path(self.registry_dir) if self.registry_dir else default_registry_dir()

    def resolved_volume_path(self) -> str:
        if self.volume_path:
            return self.volume_path
        if self.is_local:
            return str((self.local_dir / "volume").resolve())
        return f"/Volumes/{self.catalog}/{self.schema}/reports"

    def local_db_path(self) -> Path:
        return self.local_dir / "service.duckdb"

    def qualified(self, schema: str, name: str) -> str:
        """Fully qualified object name for the *state* backend (service tables)."""
        if self.is_local:
            return f"{schema}.{name}"
        return f"{self.catalog}.{schema}.{name}"

    def table(self, name: str) -> str:
        return self.qualified(self.schema, name)

    def schema_prefix(self, schema: str) -> str:
        """Schema prefix for the *state* backend (service tables and service views)."""
        return schema if self.is_local else f"{self.catalog}.{schema}"

    @property
    def effective_platform_backend(self) -> str:
        return self.platform_backend or self.backend

    @property
    def platform_is_databricks(self) -> bool:
        return self.effective_platform_backend == "databricks"

    def platform_prefix(self, schema: str) -> str:
        """Schema prefix for platform objects (governed views, base tables, restricted sources)."""
        if not self.platform_is_databricks:
            return schema
        return f"{self.platform_catalog or self.catalog}.{schema}"

    def platform_qualified(self, schema: str, name: str) -> str:
        return f"{self.platform_prefix(schema)}.{name}"


def _coerce(annotation: Any, raw: str) -> Any:
    text = str(annotation)
    if "bool" in text:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if "float" in text:
        return float(raw)
    if "int" in text and "Path" not in text:
        try:
            return int(raw)
        except ValueError:
            return raw
    if "tuple" in text:
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    if "dict" in text:
        import json

        return json.loads(raw)
    return raw
