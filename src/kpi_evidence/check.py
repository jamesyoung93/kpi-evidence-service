from __future__ import annotations

import contextlib
import copy
import dataclasses
import glob
import logging
import re
import tempfile
import time
import warnings
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .contracts import Document, DocumentStatus, KpiResult
from .ids import canonical_json, json_safe, sha256_text, utcnow
from .registry import GRAINS, Registry, load_registry, normalize_label
from .settings import PACKAGE_DIR, Settings, demo_dir

REPORT_EXTENSIONS = (".pptx", ".pptm", ".pdf", ".xlsx", ".xlsm", ".png", ".jpg", ".jpeg", ".tif", ".tiff")
LEGACY_EXTENSIONS = (".ppt", ".pps", ".xls")
FOLDER_EXTENSIONS = REPORT_EXTENSIONS + LEGACY_EXTENSIONS

ACTOR_PROPOSALS = "notebook:proposals-unconfirmed"
ACTOR_USER = "notebook:user"

EXAMPLE_KPI_TABLE = "kpi_table_example.csv"
SCOPE_VALUE_CAP = 1000
RUN_STAGE = "check"
HISTORY_COLUMNS = ("run_id", "started", "documents", "comparisons", "reused", "focus")
_GLOB_CHARS = re.compile(r"[*?\[]")
_NOT_PROCESSABLE = {DocumentStatus.UNSUPPORTED_FORMAT, DocumentStatus.ENCRYPTED, DocumentStatus.SUPERSEDED}
_CLOSED = "this check is closed; create a new ReportCheck to continue"


@contextlib.contextmanager
def _timed(timings: dict[str, float], name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round(timings.get(name, 0.0) + time.perf_counter() - started, 3)


class _LogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def _engine_log(notes: list[dict[str, Any]]) -> Iterator[None]:
    logger = logging.getLogger("kpi_evidence")
    collector = _LogCollector()
    logger.addHandler(collector)
    try:
        yield
    finally:
        logger.removeHandler(collector)
        by_logger: dict[str, list[str]] = {}
        for record in collector.records:
            by_logger.setdefault(record.name, []).append(record.getMessage()[:300])
        for name, texts in by_logger.items():
            distinct = list(dict.fromkeys(texts))
            text = "; ".join(distinct[:5]) + (f" (+{len(distinct) - 5} more)" if len(distinct) > 5 else "")
            row = _message("engine note", text, severity="info", item=name.rsplit(".", 1)[-1])
            row["count"] = len(texts)
            notes.append(row)


def _message(category: str, message: str, *, report_file: str = "", remedy: str = "", severity: str = "warning",
             item: str = "", doc_id: str | None = None) -> dict[str, Any]:
    return {"severity": severity, "category": category, "report_file": report_file, "kpi_id": None, "item": item,
            "message": message, "remedy": remedy, "count": 1, "doc_id": doc_id, "result_id": None}


class _TrackedProvider:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.requests = 0
        self.failed = 0
        self.errors: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def _record(self, requests: int, failed: int, error: Any) -> None:
        self.requests += requests
        self.failed += failed
        text = str(error or "").strip()
        if failed and text and len(self.errors) < 5:
            self.errors.append(text.splitlines()[0][:300])

    def _call(self, fn: Any, *args: Any) -> Any:
        if hasattr(self._inner, "last_error"):
            self._inner.last_error = None
        try:
            return fn(*args)
        except Exception as exc:
            self._record(1, 1, f"{type(exc).__name__}: {exc}")
            raise

    def map_labels(self, requests: Any, labels: Any) -> Any:
        results = self._call(self._inner.map_labels, requests, labels)
        errors = [r.error for r in results if getattr(r, "error", None)]
        self._record(len(results), len(errors), errors[0] if errors else None)
        return results

    def complete(self, prompt: str) -> str:
        reply = self._call(self._inner.complete, prompt)
        error = getattr(self._inner, "last_error", None)
        self._record(1, int(bool(error)), error)
        return reply

    def propose_metadata(self, text: str, fields: list[str]) -> dict:
        proposals = self._call(self._inner.propose_metadata, text, fields)
        error = getattr(self._inner, "last_error", None)
        self._record(1, int(bool(error)), error)
        return proposals

    def status(self, mode: str) -> dict[str, Any]:
        if not self.requests:
            outcome = "not used"
        elif not self.failed:
            outcome = "ok"
        else:
            outcome = "failed" if self.failed == self.requests else "partly failed"
        return {"mode": mode, "outcome": outcome, "requests": self.requests, "failed": self.failed,
                "error": self.errors[0] if self.errors else None}


def _is_bytes(value: Any) -> bool:
    return isinstance(value, (bytes, bytearray, memoryview)) or callable(getattr(value, "read", None))


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    data = value.read()
    return data.encode("utf-8") if isinstance(data, str) else bytes(data)


def _local_path(text: str) -> Path:
    if text.lower().startswith("dbfs:/"):
        text = text[5:]
    return Path(text).expanduser()


def _glob_pattern(path: Path) -> str:
    parts = path.parts
    for i in range(len(parts) - 1, 0, -1):
        head = Path(*parts[:i])
        if head.is_dir():
            return str(Path(glob.escape(str(head)), *parts[i:]))
    return str(path)


def example_kpi_table_path() -> Path | None:
    """The bundled example KPI table (next to the demo reports, packaged data, or the repo's samples/)."""
    candidates: list[Path] = []
    try:
        candidates.append(demo_dir("reports").parent / EXAMPLE_KPI_TABLE)
    except FileNotFoundError:
        pass
    candidates.append(PACKAGE_DIR / "_demo" / EXAMPLE_KPI_TABLE)
    candidates.append(PACKAGE_DIR.parents[1] / "samples" / EXAMPLE_KPI_TABLE)
    return next((p for p in candidates if p.is_file()), None)


def empty_registry(settings: Settings) -> Registry:
    """A registry without KPIs, brands or goals that keeps the bundled policy and report types."""
    try:
        bundled = load_registry(settings.resolved_registry_dir())
    except FileNotFoundError:
        bundled = None
    policy = copy.deepcopy(bundled.policy) if bundled is not None else {}
    meta = policy.setdefault("metadata", {})
    meta["required_fields"] = ["brand", "period_end"]
    report_types = {} if bundled is None else {
        name: {k: v for k, v in spec.items() if k != "assumed_definitions"} for name, spec in bundled.report_types.items()}
    return Registry(kpis={}, brands={}, report_types=report_types, goal_list=[], policy=policy,
                    directory=bundled.directory if bundled is not None else Path("."),
                    param_groups=dict(bundled.param_groups) if bundled is not None else {})


def _new_brand_id(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_") or text


def with_brands(registry: Registry, brands: Iterable[str]) -> Registry:
    """``registry`` plus a minimal entry for every brand it does not know yet (by id or alias)."""
    aliases = registry.brand_aliases()
    extra: dict[str, dict[str, Any]] = {}
    for brand in brands:
        text = str(brand or "").strip()
        if not text or text in registry.brands or normalize_label(text) in aliases:
            continue
        brand_id = _new_brand_id(text)
        if brand_id in registry.brands or brand_id in extra:
            continue
        extra[brand_id] = {"brand_id": brand_id, "display_name": text, "aliases": [text],
                           "fiscal_year_start_month": 1}
    if not extra:
        return registry
    return dataclasses.replace(registry, brands={**registry.brands, **extra})


def brand_id_for(registry: Registry, brand: str | None) -> str | None:
    """The registry brand id for an id, display name or alias (an UPPER_SNAKE id when unknown)."""
    if brand is None or not str(brand).strip():
        return None
    text = str(brand).strip()
    if text in registry.brands:
        return text
    return registry.brand_aliases().get(normalize_label(text)) or _new_brand_id(text)


def period_fields(period: Any) -> dict[str, Any]:
    """Metadata fields for a period given as text ("May 2026", "Q2 2026") or a date (the period end)."""
    from .periods import parse_period

    if period is None:
        return {}
    if isinstance(period, datetime):
        return {"period_end": period.date()}
    if isinstance(period, date):
        return {"period_end": period}
    parsed = parse_period(str(period))
    if parsed is None:
        raise ValueError(f"could not read a period from {period!r}; use e.g. 'May 2026', 'Q2 2026' or a date")
    if parsed.grain not in GRAINS:
        raise ValueError(f"period {period!r} reads as a {parsed.grain!r} period; supported grains are {GRAINS}")
    return {"period_end": parsed.end, "period_start": parsed.start, "period_grain": parsed.grain,
            "period_label": str(period).strip()}


def resolve_kpi(registry: Registry, name: Any) -> str:
    """The KPI id for an id, name, label or alias, resolved the way the KPI table resolves them."""
    from .hints import kpi_slug

    text = str(name).strip()
    if text in registry.kpis:
        return text
    slug = kpi_slug(text)
    if slug in registry.kpis:
        return slug
    key = normalize_label(text)
    hits = sorted({k.kpi_id for k in registry.kpis.values()
                   if key in {normalize_label(s) for s in (k.label, *k.synonyms)}})
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ValueError(f"{name!r} matches several KPIs ({', '.join(hits)}); use the KPI id")
    raise ValueError(f"unknown KPI {name!r} in the focus; known KPI ids: {', '.join(sorted(registry.kpis)) or 'none'}")


def messages_frame(messages: Any) -> pd.DataFrame:
    """Messages (DataFrame, dicts, dataclasses or strings) as one DataFrame."""
    if isinstance(messages, pd.DataFrame):
        return messages.copy()
    rows = []
    for m in messages or []:
        if isinstance(m, Mapping):
            rows.append(dict(m))
        elif dataclasses.is_dataclass(m) and not isinstance(m, type):
            rows.append(dataclasses.asdict(m))
        else:
            rows.append({"message": str(m)})
    return pd.DataFrame(rows)


def kpi_table_report(hint_set: Any) -> pd.DataFrame:
    """One row per KPI table row (used or skipped, with its messages)."""
    overview = getattr(hint_set, "overview", None)
    if callable(overview):
        return overview()
    return messages_frame(getattr(hint_set, "messages", None))


def run_record(comparisons: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Per-comparison facts a run keeps for ``changes_since`` (status, values, KPI, period, file)."""
    out: dict[str, dict[str, Any]] = {}
    for row in (comparisons.to_dict(orient="records") if len(comparisons) else []):
        end = _as_date(row.get("period_end"))
        out[str(row["result_id"])] = {
            "status": _clean(row.get("status")), "report_value": _clean(row.get("report_value")),
            "platform_value": _clean(row.get("platform_value")), "kpi_id": _clean(row.get("kpi_id")),
            "kpi_label": _clean(row.get("kpi_label")), "period_end": end.isoformat() if end else None,
            "scope": _clean(row.get("scope")), "report_file": _clean(row.get("report_file")),
            "observation_id": _clean(row.get("observation_id")), "doc_id": _clean(row.get("doc_id"))}
    return out


def _clean(value: Any) -> Any:
    if value is None or isinstance(value, (list, dict, tuple, set)):
        return value
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):
        return value


def _as_date(value: Any) -> date | None:
    value = _clean(value)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    try:
        return date.fromisoformat(str(value)[:10]) if value is not None else None
    except ValueError:
        return None


@dataclass
class _Entry:
    doc_id: str
    file_name: str
    source: str
    order: int
    overrides: dict[str, Any] = field(default_factory=dict)


class ReportCheck:
    """Check report files (PowerPoint, PDF, Excel, images) against platform tables, in a notebook."""

    def __init__(self, spark: Any = None, *, settings: Settings | None = None, registry: Any = "auto",
                 brand: str | None = None, persist: bool = False, warehouse_id: str | None = None,
                 catalog: str | None = None, gov_schema: str | None = None, ai_mode: str | None = None,
                 dimension_values: Mapping[str, Iterable[str]] | None = None, load_demo_platform: bool = False,
                 strict_metadata: bool = False, state_path: str | Path | None = None, reuse: bool = True):
        self.spark = spark
        self.mode = "spark" if spark is not None else ("warehouse" if warehouse_id else "local")
        if load_demo_platform and self.mode != "local":
            raise ValueError("load_demo_platform loads synthetic tables into a local DuckDB only; in a workspace "
                             "use the kpi_bootstrap job with load_demo=true")
        if persist and state_path is not None:
            raise ValueError("use persist=True (Delta service tables) or state_path (a DuckDB state file), not both")
        self.persist = bool(persist)
        self.state_path = str(state_path) if state_path is not None else None
        self.reuse = bool(reuse)
        self.brand = str(brand).strip() if brand else None
        self.strict_metadata = bool(strict_metadata)
        self.load_demo_platform = bool(load_demo_platform)
        self.dimension_values = {str(k): {str(v) for v in vals} for k, vals in (dimension_values or {}).items()}
        self._tmp = None if self.persist else tempfile.TemporaryDirectory(prefix="kpi_evidence_check_",
                                                                         ignore_cleanup_errors=True)
        self.settings = self._build_settings(settings, catalog=catalog, gov_schema=gov_schema, ai_mode=ai_mode,
                                             warehouse_id=warehouse_id)
        self.store, self.platform = self._open_backends(warehouse_id)
        self._base_registry = self._resolve_registry(registry)
        self._entries: dict[str, _Entry] = {}
        self._hints: Any = None
        self._kpi_source: str | None = None
        self._kpi_messages: list[dict[str, Any]] = []
        self._setup_messages: list[dict[str, Any]] = []
        self._lint_messages: list[dict[str, Any]] = []
        self._use_registry = True
        self._schema_ready = False
        self._demo_loaded = False
        self._synced_registry: str | None = None
        self._applied: dict[str, dict[str, Any]] = {}
        self._engine_notes: list[dict[str, Any]] = []
        self._ai_tracker: _TrackedProvider | None = None
        self._failed = False
        self._closed = False
        self.last_result: Any = None

    @classmethod
    def demo(cls, *, kpi_table: bool = True, **kwargs: Any) -> "ReportCheck":
        """A check over the bundled synthetic reports, platform, registry and (optionally) example KPI table."""
        kwargs.setdefault("load_demo_platform", True)
        check = cls(**kwargs)
        check.add_reports(demo_dir("reports"))
        if kpi_table:
            path = example_kpi_table_path()
            if path is None:
                note = (f"the example KPI table {EXAMPLE_KPI_TABLE} was not found next to the demo reports or in "
                        "samples/; the demo runs on the bundled registry only")
                warnings.warn(note, stacklevel=2)
                check._kpi_messages.append(_message("KPI table", note, item=EXAMPLE_KPI_TABLE))
            else:
                check.set_kpis(path)
        return check

    def _build_settings(self, settings: Settings | None, *, catalog: str | None, gov_schema: str | None,
                        ai_mode: str | None, warehouse_id: str | None) -> Settings:
        explicit = {"catalog": catalog, "gov_schema": gov_schema, "warehouse_id": warehouse_id}
        explicit = {k: v for k, v in explicit.items() if v}
        if settings is None:
            base = Settings.from_env(**explicit, ai_mode=ai_mode or "off")
        else:
            base = dataclasses.replace(settings, **explicit, **({"ai_mode": ai_mode} if ai_mode else {}))
        remote = self.mode != "local"
        backend = "databricks" if (remote and self.persist) else "local"
        out = dataclasses.replace(base, backend=backend, platform_backend="databricks" if remote else None,
                                  extra={**(base.extra or {}), "reuse_results": self.reuse})
        if not self.persist:
            out.local_dir = Path(self._tmp.name)
        if backend == "local" and not self.persist:
            out.volume_path = None
        return Settings.from_env(**{f.name: getattr(out, f.name) for f in dataclasses.fields(Settings)})

    def _open_backends(self, warehouse_id: str | None) -> tuple[Any, Any]:
        from .store.duckdb_store import DuckDBStore
        from .store.state_file import pull_state

        s = self.settings
        if self.state_path is not None:
            pull_state(self.state_path, s.local_db_path())
            state = DuckDBStore(s, s.local_db_path(), keep_open=True)
        elif self.persist and self.mode == "local":
            state = DuckDBStore(s, s.local_db_path())
        elif not self.persist:
            state = DuckDBStore(s, ":memory:")
        else:
            state = None
        if self.mode == "local":
            return state, state.platform_access
        from . import platform_access
        from .store.databricks_store import DatabricksStore

        if self.mode == "spark":
            access = platform_access.DatabricksPlatform(s, spark=self.spark)
            store = state if state is not None else DatabricksStore(s, spark=self.spark)
        else:
            access = platform_access.warehouse_platform(s, warehouse_id=warehouse_id)
            store = state if state is not None else DatabricksStore(s, warehouse_id=warehouse_id)
        store.attach_platform(access)
        return store, access

    def _resolve_registry(self, registry: Any) -> Registry:
        if isinstance(registry, Registry):
            return registry
        if registry is None or (isinstance(registry, str) and registry.lower() in ("none", "empty")):
            return empty_registry(self.settings)
        if isinstance(registry, str) and registry.lower() == "bundled":
            return load_registry(self.settings.resolved_registry_dir())
        if isinstance(registry, str) and registry.lower() == "auto":
            if self.mode == "local" and self.load_demo_platform:
                return load_registry(self.settings.resolved_registry_dir())
            return empty_registry(self.settings)
        return load_registry(Path(registry))

    def add_reports(self, sources: Any, *, brand: str | None = None, report_type: str | None = None,
                    period: Any = None) -> pd.DataFrame:
        """Register report files (paths, folders, globs, Volume paths, name/bytes pairs); returns one row per file."""
        from .intake import register_file

        self._require_open()
        overrides = self._clean_overrides({"brand": brand, "report_type": report_type, "period": period})
        self._ensure_state()
        rows = []
        for name, blob, source_path in self._resolve_sources(sources):
            doc = register_file(self.store, self.settings, blob=blob, file_name=name, source_path=source_path)
            entry = self._entries.get(doc.doc_id)
            note = "already added" if entry is not None else "added"
            if entry is None:
                entry = _Entry(doc_id=doc.doc_id, file_name=doc.file_name, source=source_path or "<bytes>",
                               order=len(self._entries))
                self._entries[doc.doc_id] = entry
            entry.overrides.update(overrides)
            rows.append({"file_name": doc.file_name, "doc_id": doc.doc_id, "format": doc.format.value,
                         "status": doc.status.value, "source": entry.source, "note": note,
                         "remedy": doc.error if doc.status in (DocumentStatus.UNSUPPORTED_FORMAT,
                                                               DocumentStatus.ENCRYPTED) else None})
        return pd.DataFrame(rows, columns=["file_name", "doc_id", "format", "status", "source", "note", "remedy"])

    def _resolve_sources(self, sources: Any) -> Iterator[tuple[str, bytes, str | None]]:
        if isinstance(sources, Mapping):
            for name, blob in sources.items():
                yield str(name), _as_bytes(blob), None
            return
        if isinstance(sources, tuple) and len(sources) == 2 and isinstance(sources[0], str) and _is_bytes(sources[1]):
            yield sources[0], _as_bytes(sources[1]), None
            return
        if isinstance(sources, (str, Path)):
            yield from self._resolve_path(str(sources))
            return
        if isinstance(sources, Iterable) and not _is_bytes(sources):
            for item in sources:
                yield from self._resolve_sources(item)
            return
        raise TypeError(f"unsupported report source {type(sources).__name__}: pass a path, folder, glob, "
                        "(name, bytes) pair, {name: bytes} dict or a list of these")

    def _resolve_path(self, text: str) -> Iterator[tuple[str, bytes, str | None]]:
        path = _local_path(text)
        if path.is_dir():
            files = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in FOLDER_EXTENSIONS
                           and not p.name.startswith(("~$", ".")))
            if not files:
                warnings.warn(f"folder {text!r} holds no report files ({', '.join(REPORT_EXTENSIONS)})", stacklevel=3)
        elif path.is_file():
            files = [path]
        elif _GLOB_CHARS.search(str(path)):
            matches = sorted(Path(p) for p in glob.glob(_glob_pattern(path), recursive=True))
            files = [p for p in matches if p.is_file() and p.suffix.lower() in FOLDER_EXTENSIONS]
            if not matches:
                raise FileNotFoundError(f"no files match {text!r}")
            if not files:
                warnings.warn(f"{text!r} matches no report files ({', '.join(REPORT_EXTENSIONS)})", stacklevel=3)
        else:
            raise FileNotFoundError(f"no such report file or folder: {text!r}")
        for p in files:
            yield p.name, p.read_bytes(), str(p.resolve())

    def _clean_overrides(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        from .intake import METADATA_FIELDS

        allowed = set(METADATA_FIELDS) | {"period"}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"unknown metadata fields {unknown}; use {sorted(allowed)}")
        out: dict[str, Any] = {}
        for name, value in fields.items():
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            if name == "period":
                out.update(period_fields(value))
            elif name == "report_type" and self._base_registry.report_types and value not in self._base_registry.report_types:
                raise ValueError(f"unknown report type {value!r}; known: {sorted(self._base_registry.report_types)}")
            elif name == "period_grain" and value not in GRAINS:
                raise ValueError(f"unknown period grain {value!r}; use one of {GRAINS}")
            else:
                out[name] = value.strip() if isinstance(value, str) else value
        return out

    def set_kpis(self, table: Any, *, sheet: Any = None, use_registry: bool = True) -> pd.DataFrame:
        """Load a KPI table of hints (CSV/XLSX path, pandas or Spark DataFrame, list of dicts); returns its rows."""
        from . import hints as _hints

        frame = _hints.load_kpi_table(table, sheet=sheet, spark=self.spark)
        base = self._base_registry if use_registry else empty_registry(self.settings)
        only = next(iter(base.brands)) if len(base.brands) == 1 else None
        hint_set = _hints.parse_hints(frame, base_registry=base, default_brand=self.brand or only)
        self._hints, self._use_registry = hint_set, bool(use_registry)
        self._kpi_source = str(table) if isinstance(table, (str, Path)) else type(table).__name__
        self._kpi_messages = self._catalog_messages(hint_set)
        return kpi_table_report(hint_set)

    def _catalog_messages(self, hint_set: Any) -> list[dict[str, Any]]:
        if self.mode == "local":
            return []
        tables = list(dict.fromkeys(t for r in getattr(hint_set, "rows", None) or [] if getattr(r, "accepted", False)
                                    for t in getattr(r, "tables", None) or [] if str(t).count(".") == 1))
        if not tables:
            return []
        current = self._session_catalog()
        where = f"the session's current catalog ({current})" if current else "the session's current catalog"
        shown = ", ".join(tables[:5]) + (f" and {len(tables) - 5} more" if len(tables) > 5 else "")
        note = (f"The KPI table names {len(tables)} table(s) without a catalog ({shown}); on Databricks they resolve "
                f"against {where}, not the catalog setting ({self.settings.platform_catalog or self.settings.catalog}).")
        warnings.warn(note, stacklevel=3)
        return [_message("KPI table", note, item="table", remedy="Write each table as catalog.schema.table.")]

    def _session_catalog(self) -> str | None:
        try:
            frame = self.platform.query("SELECT current_catalog() AS catalog")
            value = frame.iloc[0, 0] if len(frame) else None
        except Exception:
            return None
        return str(value) if value else None

    def confirm(self, report: str, **fields: Any) -> None:
        """State metadata for one report (by file name or doc id); applied on the next run."""
        entry = self._find_entry(report)
        if not fields:
            raise ValueError("confirm needs at least one field, e.g. period='May 2026'")
        entry.overrides.update(self._clean_overrides(fields))

    def _find_entry(self, report: str) -> _Entry:
        key = str(report).strip()
        if key in self._entries:
            return self._entries[key]
        name = Path(key.replace("\\", "/")).name.lower()
        hits = [e for e in self._entries.values()
                if e.file_name.lower() == name or (len(key) >= 8 and e.doc_id.startswith(key.lower()))]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            known = ", ".join(sorted(e.file_name for e in self._entries.values())) or "none yet"
            raise KeyError(f"no report {report!r} in this check (reports: {known})")
        raise ValueError(f"{report!r} matches several reports; use the doc id: {[e.doc_id[:12] for e in hits]}")

    @property
    def documents(self) -> pd.DataFrame:
        """The reports of this check with their metadata, status and who settled the metadata."""
        from .intake import document_grain

        self._require_open()
        rows = []
        registry = self._effective_registry()
        for entry in self._ordered_entries():
            doc = self.store.get(Document, doc_id=entry.doc_id)
            if doc is None:
                continue
            rows.append({
                "file_name": doc.file_name, "format": doc.format.value, "status": doc.status.value,
                "brand": doc.brand, "report_type": doc.report_type, "period_label": doc.period_label,
                "period_start": doc.period_start, "period_end": doc.period_end,
                "period_grain": document_grain(doc, registry) if doc.period_end else None,
                "version": doc.version, "needs_confirmation": list(doc.needs_confirmation or []),
                "metadata": self.metadata_state(doc), "overrides": dict(entry.overrides) or None,
                "source": entry.source, "error": doc.error, "doc_id": doc.doc_id})
        return pd.DataFrame(rows, columns=["file_name", "format", "status", "brand", "report_type", "period_label",
                                           "period_start", "period_end", "period_grain", "version",
                                           "needs_confirmation", "metadata", "overrides", "source", "error", "doc_id"])

    @staticmethod
    def metadata_state(doc: Document) -> str:
        """How the document's metadata was settled (plain words for the report)."""
        if doc.confirmed_by == ACTOR_PROPOSALS:
            return "proposed, not confirmed"
        if doc.confirmed_by:
            return "confirmed by a person"
        if doc.status == DocumentStatus.AWAITING_CONFIRMATION:
            return "needs confirmation"
        if doc.status in (DocumentStatus.REGISTERED, DocumentStatus.UNSUPPORTED_FORMAT, DocumentStatus.ENCRYPTED):
            return "not read"
        return "read from the report"

    def _ordered_entries(self) -> list[_Entry]:
        return sorted(self._entries.values(), key=lambda e: (e.file_name.lower(), e.order))

    def default_brand(self) -> str | None:
        """Brand used when a report's brand is missing: the argument, the KPI table's brand, or the only brand."""
        if self.brand:
            return self.brand
        table_brand = getattr(self._hints, "default_brand", None)
        if table_brand:
            return str(table_brand)
        if len(self._base_registry.brands) == 1:
            return next(iter(self._base_registry.brands))
        return None

    def _effective_registry(self) -> Registry:
        registry = self._base_registry if self._use_registry else empty_registry(self.settings)
        if self._hints is not None:
            from . import hints as _hints

            registry = _hints.merge_registry(registry, self._hints)
        brands = [self.default_brand(), *(e.overrides.get("brand") for e in self._entries.values())]
        return with_brands(registry, [b for b in brands if b])

    def _registry_signature(self, registry: Registry) -> str:
        payload = {"kpis": {k: v.raw for k, v in sorted(registry.kpis.items())},
                   "status": {k: v.status.value for k, v in registry.kpis.items()},
                   "brands": registry.brands, "report_types": sorted(registry.report_types),
                   "policy": registry.policy}
        return sha256_text(canonical_json(json_safe(payload)))

    def _sync_registry(self, registry: Registry) -> None:
        from . import pipeline

        signature = self._registry_signature(registry)
        if signature == self._synced_registry:
            return
        self._lint_messages = []
        if self._hints is None:
            pipeline.sync_registry(self.store, registry)
        else:
            from . import hints as _hints

            for problem in _hints.lint_for_hints(registry) or []:
                self._lint_messages.append(_message("KPI table", str(problem), item="definition lint",
                                                    remedy="Fix the KPI table row and call set_kpis again."))
            self.store.upsert(registry.registry_rows())
            brands, goals, rtypes = registry.config_rows()
            self.store.insert_new(brands)
            self.store.insert_new(goals)
            self.store.insert_new(rtypes)
        self._synced_registry = signature

    def focus_kpis(self, focus: Any = None, max_priority: int | None = None, *,
                   registry: Registry | None = None) -> frozenset[str] | None:
        """The KPI ids a run with this focus compares (None = every KPI)."""
        from .reconcile import kpi_priority

        if focus is None and max_priority is None:
            return None
        registry = registry or self._effective_registry()
        ids: set[str] = set()
        if focus is not None:
            for item in ([focus] if isinstance(focus, str) else list(focus)):
                ids.add(resolve_kpi(registry, item))
        if max_priority is not None:
            limit = int(max_priority)
            ids |= {k for k, kpi in registry.kpis.items()
                    if kpi_priority(kpi) is not None and kpi_priority(kpi) <= limit}
        if not ids:
            raise ValueError("the focus names no KPI" if max_priority is None else
                             f"no KPI has a priority of {max_priority} or better; fill the KPI table's priority column")
        return frozenset(ids)

    def run(self, *, materialize: bool = True, focus: Any = None, max_priority: int | None = None) -> Any:
        """Extract, interpret, map and compare every report (focus KPIs only when given); returns the report."""
        self._require_open()
        focus_ids = self.focus_kpis(focus, max_priority)
        try:
            result = self._run(materialize, focus_ids)
        except BaseException:
            self._failed = True
            raise
        self._failed = False
        self.save_state()
        return result

    def _run(self, materialize: bool, focus_ids: frozenset[str] | None) -> Any:
        from . import pipeline
        from . import report as _report

        started_at = utcnow()
        started = time.perf_counter()
        timings: dict[str, float] = {}
        messages: list[dict[str, Any]] = []
        registry = self._effective_registry()
        with _timed(timings, "setup"):
            self._bootstrap(registry)
        messages += self._setup_messages + self._kpi_messages + self._lint_messages
        run_id = pipeline.new_run_id()
        entries = self._ordered_entries()
        with _timed(timings, "extract"):
            for entry in entries:
                self._extract(registry, entry, run_id)
        with _timed(timings, "metadata"):
            for entry in entries:
                self._settle_metadata(registry, entry, messages)
        with _timed(timings, "platform_checks"):
            views = self.governed_views()
            dims = self._collect_dimension_values(views, messages)
            dq = self._run_dq(registry, views, run_id, messages)
        notes: list[dict[str, Any]] = []
        self._ai_tracker = None
        with _timed(timings, "process"), _engine_log(notes):
            processed, reused = self._process(registry, entries, run_id, dims, dq, materialize, focus_ids)
        if processed:
            self._engine_notes = notes
        messages += self._engine_notes
        ai_status = self._ai_status()
        messages += self._ai_messages(ai_status)
        timings["documents_processed"] = len(processed)
        timings["total_before_report"] = round(time.perf_counter() - started, 3)
        doc_ids = [e.doc_id for e in entries]
        reused |= self._carried_results(doc_ids, processed)
        result = _report.build_result(self.store, registry, self.settings, doc_ids, hints=self._hints, run_id=run_id,
                                      platform_description=self.platform_description(views), timings=timings,
                                      extra_issues=messages, focus_kpis=focus_ids, reused_ids=reused,
                                      previous_runs=self._previous_runs(doc_ids))
        if isinstance(getattr(result, "summary", None), dict):
            result.summary.update(ai_mode=ai_status["mode"], ai=ai_status)
        self._record_run(run_id, started_at, doc_ids, result, focus_ids)
        self.last_result = result
        return result

    def _ai_status(self) -> dict[str, Any]:
        mode = str(self.settings.ai_mode or "off")
        if self._ai_tracker is None:
            return {"mode": mode, "outcome": "off" if mode == "off" else "not used", "requests": 0, "failed": 0,
                    "error": None}
        return self._ai_tracker.status(mode)

    @staticmethod
    def _ai_messages(status: dict[str, Any]) -> list[dict[str, Any]]:
        if not status["failed"]:
            return []
        text = (f"{status['failed']} of {status['requests']} AI request(s) failed with ai_mode {status['mode']}, so "
                "those KPI matches and definition searches ran without AI, as with ai_mode off.")
        if status["error"]:
            text += f" First error: {status['error']}"
        return [_message("AI", text, item=status["mode"],
                         remedy="Check that the AI model can be called from this workspace and identity "
                                "(ai_query runs only on Databricks), or set ai_mode to off.")]

    def _bootstrap(self, registry: Registry) -> None:
        from . import platform

        self._ensure_state()
        if self.load_demo_platform and not self._demo_loaded:
            platform.load_demo_platform(self.store, self.settings)
            platform.create_governed_views(self.store, self.settings)
            self._demo_loaded = True
        self._sync_registry(registry)

    def _ensure_state(self) -> None:
        if self._schema_ready:
            return
        self.store.ensure_schema()
        try:
            self.store.ensure_views()
        except Exception as exc:
            self._setup_messages.append(_message("setup", f"Service views were not created: {type(exc).__name__}: "
                                                 f"{exc}", severity="info", item="service views"))
        self._schema_ready = True

    def _extract(self, registry: Registry, entry: _Entry, run_id: str) -> None:
        from . import pipeline

        doc = self.store.get(Document, doc_id=entry.doc_id)
        if doc is None:
            return
        if doc.status == DocumentStatus.REGISTERED or (doc.status == DocumentStatus.FAILED and doc.failed_stage == "extract"):
            pipeline.extract_stage(self.store, registry, self.settings, doc, run_id)

    def _settle_metadata(self, registry: Registry, entry: _Entry, messages: list[dict[str, Any]]) -> None:
        from .intake import confirm_metadata

        doc = self.store.get(Document, doc_id=entry.doc_id)
        if doc is None or doc.status in (DocumentStatus.UNSUPPORTED_FORMAT, DocumentStatus.ENCRYPTED,
                                         DocumentStatus.FAILED, DocumentStatus.REGISTERED):
            return
        if entry.overrides:
            if self._applied.get(entry.doc_id) == entry.overrides and doc.confirmed_by == ACTOR_USER:
                return
            fields = dict(entry.overrides)
            if "brand" in fields:
                fields["brand"] = brand_id_for(registry, fields["brand"])
            if doc.brand is None and "brand" not in fields and self.default_brand():
                proposal = ((doc.metadata_proposals or {}).get("brand") or {}).get("value")
                fields["brand"] = proposal or brand_id_for(registry, self.default_brand())
            try:
                confirm_metadata(self.store, registry, self.settings, entry.doc_id, fields, ACTOR_USER)
                self._applied[entry.doc_id] = dict(entry.overrides)
            except (ValueError, KeyError) as exc:
                messages.append(_message("metadata not applied", f"The stated metadata could not be applied: {exc}",
                                         report_file=doc.file_name, severity="error", doc_id=doc.doc_id,
                                         item=", ".join(sorted(entry.overrides)),
                                         remedy=f"check.confirm({doc.file_name!r}, ...) with valid values, then run "
                                                "again."))
            return
        if doc.status != DocumentStatus.AWAITING_CONFIRMATION or self.strict_metadata:
            return
        fields, missing = self._best_proposals(registry, doc)
        if missing:
            if list(doc.needs_confirmation or []) != missing:
                doc.needs_confirmation = missing
                self.store.upsert([doc])
            return
        try:
            confirm_metadata(self.store, registry, self.settings, entry.doc_id, fields, ACTOR_PROPOSALS)
        except (ValueError, KeyError) as exc:
            messages.append(_message("metadata not applied", f"The proposed metadata could not be used: {exc}",
                                     report_file=doc.file_name, doc_id=doc.doc_id,
                                     remedy=f"check.confirm({doc.file_name!r}, brand=..., period=...), then run "
                                            "again."))

    def _best_proposals(self, registry: Registry, doc: Document) -> tuple[dict[str, Any], list[str]]:
        proposals = doc.metadata_proposals or {}
        required = ((registry.policy or {}).get("metadata") or {}).get("required_fields") or [
            "brand", "report_type", "period_end"]
        fields: dict[str, Any] = {}
        missing: list[str] = []
        for name in required:
            if getattr(doc, name, None) not in (None, ""):
                continue
            value = (proposals.get(name) or {}).get("value")
            if value is None and name == "brand":
                value = brand_id_for(registry, self.default_brand())
            if value is None:
                missing.append(name)
            else:
                fields[name] = value
        return fields, missing

    def governed_views(self) -> list[str]:
        """Governed views (``kpi_gov``) the platform can read; DQ and dimension values need them."""
        from . import platform

        found = []
        for view in platform.GOVERNED_VIEWS:
            try:
                self.platform.query(f"SELECT 1 AS ok FROM {platform.gov_view_fq(self.settings, view)} WHERE 1 = 0")
                found.append(view)
            except Exception:
                continue
        return found

    def _collect_dimension_values(self, views: list[str], messages: list[dict[str, Any]]) -> dict[str, set[str]]:
        from . import platform

        dims: dict[str, set[str]] = {}

        def add(dim: str, values: Iterable[Any]) -> None:
            clean = [str(v).strip() for v in values if v is not None and not pd.isna(v) and str(v).strip()]
            dims.setdefault(dim, set()).update(clean[:SCOPE_VALUE_CAP])

        if platform.DIMENSION_SOURCE_VIEW in views:
            try:
                for dim, values in platform.dimension_values(self.store, self.settings).items():
                    add(dim, values)
            except Exception as exc:
                messages.append(_message("scope values", f"Governed-view dimension values unavailable: "
                                         f"{type(exc).__name__}: {exc}"[:500], item=platform.DIMENSION_SOURCE_VIEW))
        if self._hints is not None:
            from . import hints as _hints

            try:
                queries = [(str(dim), str(sql)) for dim, sql in _hints.scope_values_sql(self._hints) or []]
            except Exception as exc:
                queries = []
                messages.append(_message("scope values", f"Scope columns could not be listed: {exc}",
                                         item="scope_columns"))
            for dim, sql in queries:
                try:
                    frame = self.platform.query(sql)
                    add(dim, frame.iloc[:, 0].tolist() if frame.shape[1] else [])
                except Exception as exc:
                    messages.append(_message(
                        "scope values", f"Values for scope {dim!r} could not be read: {type(exc).__name__}: {exc}"[:500],
                        item=dim, remedy="Check the KPI table's scope_columns and table names."))
        for dim, values in self.dimension_values.items():
            add(dim, values)
        return dims

    def _run_dq(self, registry: Registry, views: list[str], run_id: str,
                messages: list[dict[str, Any]]) -> dict[str, str]:
        from . import pipeline

        if not views:
            return {}
        try:
            return dict(pipeline.run_dq(self.store, registry, self.settings, run_id) or {})
        except Exception as exc:
            messages.append(_message("data quality", f"Data-quality checks did not run: {type(exc).__name__}: {exc}"[:500],
                                     item="governed views"))
            return {}

    def _process(self, registry: Registry, entries: list[_Entry], run_id: str, dims: dict[str, set[str]],
                 dq: dict[str, str], materialize: bool, focus: frozenset[str] | None) -> tuple[set[str], set[str]]:
        from . import ai, pipeline
        from .reconcile import is_reused

        ai_store = self.store
        if self.settings.ai_mode == "ai_query" and self.mode != "local":
            ai_store = getattr(self.platform, "store", self.store)
        provider = ai.get_provider(self.settings, store=ai_store)
        if self.settings.ai_mode != "off":
            provider = self._ai_tracker = _TrackedProvider(provider)
        processed: set[str] = set()
        reused: set[str] = set()
        for entry in entries:
            doc = self.store.get(Document, doc_id=entry.doc_id)
            if doc is None or doc.status in _NOT_PROCESSABLE:
                continue
            if doc.status == DocumentStatus.FAILED and doc.failed_stage == "process":
                doc.status, doc.failed_stage, doc.error = DocumentStatus.READY, None, None
                self.store.upsert([doc])
            if doc.status in (DocumentStatus.READY, DocumentStatus.PROCESSED):
                results = pipeline.process_stage(self.store, registry, self.settings, doc, run_id, dq_status=dq,
                                                 dimension_values=dims, provider=provider,
                                                 materialize_sql=materialize, focus_kpis=focus)
                processed.add(entry.doc_id)
                reused |= {r.result_id for r in results if is_reused(r)}
        return processed, reused

    def _carried_results(self, doc_ids: list[str], processed: set[str]) -> set[str]:
        rest = [d for d in doc_ids if d not in processed]
        if not rest:
            return set()
        marks = ", ".join(f":d{i}" for i in range(len(rest)))
        frame = self.store.read_df(KpiResult, f"doc_id IN ({marks}) AND is_current = TRUE AND recon_run_id IS NOT NULL",
                                   {f"d{i}": d for i, d in enumerate(rest)}, columns_sql="result_id")
        return {str(r) for r in frame["result_id"].tolist()}

    def _run_rows(self) -> list[Any]:
        from .contracts import RunLog

        return sorted(self.store.read(RunLog, "stage = :s", {"s": RUN_STAGE}), key=lambda r: (r.started_at, r.run_id))

    def _previous_runs(self, doc_ids: list[str]) -> list[dict[str, Any]]:
        wanted = set(doc_ids)
        out = []
        for row in reversed(self._run_rows()):
            counts = dict(row.counts or {})
            if wanted & set(counts.get("doc_ids") or []):
                out.append({**counts, "run_id": row.run_id, "started": row.started_at})
        return out

    def _record_run(self, run_id: str, started_at: datetime, doc_ids: list[str], result: Any,
                    focus: frozenset[str] | None) -> None:
        from . import __version__
        from .contracts import RunLog, RunStatus

        comparisons = getattr(result, "comparisons", pd.DataFrame())
        counts = {"documents": len(doc_ids), "comparisons": int(len(comparisons)),
                  "reused": int((getattr(result, "summary", None) or {}).get("reused") or 0),
                  "focus": sorted(focus) if focus is not None else None, "doc_ids": list(doc_ids),
                  "results": run_record(comparisons)}
        self.store.upsert([RunLog(run_id=run_id, stage=RUN_STAGE, unit="*", status=RunStatus.SUCCEEDED,
                                  started_at=started_at, finished_at=utcnow(), code_version=__version__,
                                  counts=counts)])

    def history(self) -> pd.DataFrame:
        """Past runs recorded in this check's state, oldest first."""
        self._require_open()
        self._ensure_state()
        rows = []
        for row in self._run_rows():
            counts = row.counts or {}
            rows.append({"run_id": row.run_id, "started": row.started_at, "documents": counts.get("documents"),
                         "comparisons": counts.get("comparisons"), "reused": counts.get("reused"),
                         "focus": ", ".join(counts.get("focus") or []) or None})
        return pd.DataFrame(rows, columns=list(HISTORY_COLUMNS))

    def save_state(self) -> str | None:
        """Write the working state back to ``state_path`` through a temp file; None without a state path."""
        from .store.state_file import push_state

        self._require_open()
        if self.state_path is None:
            return None
        self.store.close()
        local = self.settings.local_db_path()
        if not local.exists():
            return None
        return push_state(local, self.state_path)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError(_CLOSED)

    def close(self, *, save: bool = True) -> None:
        """Save the state (unless the last run failed) and release the working store and files."""
        if self._closed:
            return
        try:
            if save and not self._failed:
                self.save_state()
        finally:
            self._closed = True
            closer = getattr(self.store, "close", None)
            if callable(closer):
                with contextlib.suppress(Exception):
                    closer()
            if self._tmp is not None:
                self._tmp.cleanup()

    def __enter__(self) -> "ReportCheck":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close(save=exc_type is None)

    def platform_description(self, views: list[str] | None = None) -> str:
        """Plain description of where the KPI SQL ran (shown in the report)."""
        s = self.settings
        if self.mode == "local":
            text = "local DuckDB with the synthetic demo platform" if self.load_demo_platform else "local DuckDB"
        elif self.mode == "spark":
            text = f"Databricks through the notebook Spark session (catalog {s.platform_catalog or s.catalog})"
        else:
            text = f"Databricks SQL warehouse {s.warehouse_id} (catalog {s.platform_catalog or s.catalog})"
        if views is not None:
            prefix = s.platform_prefix(s.gov_schema)
            text += f"; governed views in {prefix}: {', '.join(views)}" if views else f"; no governed views in {prefix}"
        return text

    def __repr__(self) -> str:
        kpis = f", KPI table {self._kpi_source}" if self._kpi_source else ""
        state = f", state {self.state_path}" if self.state_path else ""
        return f"ReportCheck({self.mode}, {len(self._entries)} report(s){kpis}{state})"
