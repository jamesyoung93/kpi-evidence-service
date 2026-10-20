from __future__ import annotations

import re
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from kpi_evidence import platform
from kpi_evidence.contracts import STATUS_INFO, Document, StatusGroup, ValidationStatus
from kpi_evidence.registry import GRAINS, Registry, parse_kpi
from kpi_evidence.settings import Settings
from kpi_evidence.store.duckdb_store import DuckDBStore

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "samples" / "reports"


class FakeAnalysisException(Exception):
    def __init__(self, message: str, condition: str, sqlstate: str):
        super().__init__(message)
        self._condition, self._sqlstate = condition, sqlstate

    def getCondition(self) -> str:
        return self._condition

    def getSqlState(self) -> str:
        return self._sqlstate


class FakeSparkDataFrame:
    def __init__(self, frame: pd.DataFrame):
        self._frame = frame
        self.columns = [str(c) for c in frame.columns]

    def toPandas(self) -> pd.DataFrame:
        return self._frame.copy()

    def collect(self) -> list[tuple]:
        return [tuple(r) for r in self._frame.itertuples(index=False, name=None)]

    def take(self, n: int) -> list[tuple]:
        return self.collect()[:n]


class FakeSparkSession:
    def __init__(self, tmp_dir: Path, *, catalog: str = "main", governed_views: bool = True,
                 denied_schemas: tuple[str, ...] = ("platform_restricted",),
                 delta_versions: dict[str, int] | None = None):
        settings = Settings.from_env(local_dir=Path(tmp_dir) / "fake_spark", ai_mode="off",
                                     registry_dir=ROOT / "registry")
        self.duck = DuckDBStore(settings, ":memory:")
        self.duck.ensure_schema()
        platform.load_demo_platform(self.duck, settings)
        if governed_views:
            platform.create_governed_views(self.duck, settings)
        self.catalog = catalog
        self.denied = denied_schemas
        self.calls: list[tuple[str, dict | None]] = []
        self.delta_versions = delta_versions
        self._strip = re.compile(rf"\b{re.escape(catalog)}\.(?=[A-Za-z_]\w*\.[A-Za-z_]\w*)")

    def sql(self, sql: str, args: dict | None = None) -> FakeSparkDataFrame:
        self.calls.append((sql, args))
        text = self._strip.sub("", sql)
        for schema in self.denied:
            if re.search(rf"\b{re.escape(schema)}\.", text):
                raise FakeAnalysisException(
                    f"[INSUFFICIENT_PERMISSIONS] User does not have SELECT on {schema}.", "INSUFFICIENT_PERMISSIONS",
                    "42501")
        history = re.match(r"DESCRIBE HISTORY ([\w.]+) LIMIT 1$", sql.strip())
        if history and self.delta_versions is not None:
            return FakeSparkDataFrame(pd.DataFrame({"version": [self.delta_versions.get(history.group(1), 1)],
                                                    "operation": ["WRITE"]}))
        try:
            frame = self.duck.query(text, dict(args) if args else None)
        except Exception as exc:
            message = str(exc)
            if "does not exist" in message or "Catalog Error" in message:
                raise FakeAnalysisException(f"[TABLE_OR_VIEW_NOT_FOUND] {message}", "TABLE_OR_VIEW_NOT_FOUND",
                                            "42P01") from exc
            raise
        return FakeSparkDataFrame(frame)

    @property
    def statements(self) -> list[str]:
        return [c[0] for c in self.calls]


@dataclass
class StandinHintSet:
    kpis: dict[str, Any]
    messages: pd.DataFrame
    scope: list[tuple[str, str]] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)

    @property
    def default_brand(self) -> str | None:
        return self.brands[0] if len(self.brands) == 1 else None

    def overview(self) -> pd.DataFrame:
        rows = [{"kpi": k.label, "kpi_id": k.kpi_id, "used": True, "messages": ""} for k in self.kpis.values()]
        rows += [{"kpi": m["kpi"], "kpi_id": None, "used": False, "messages": m["message"]}
                 for m in self.messages.to_dict(orient="records") if m["level"] == "error"]
        return pd.DataFrame(rows, columns=["kpi", "kpi_id", "used", "messages"])


def _cell(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _date_expr(column: str, fmt: str) -> str:
    if fmt.lower() in ("yyyy-mm", "yyyy-mm-01"):
        return f"CAST({column} || '-01' AS DATE)"
    return column


def standin_load_kpi_table(table: Any, sheet: Any = None, spark: Any = None) -> pd.DataFrame:
    if isinstance(table, pd.DataFrame):
        frame = table.copy()
    elif isinstance(table, list):
        frame = pd.DataFrame(table)
    else:
        frame = pd.read_csv(table, dtype=str, keep_default_na=False)
    frame.columns = [str(c).strip().lower() for c in frame.columns]
    return frame


def standin_parse_hints(frame: pd.DataFrame, *, base_registry: Registry | None = None,
                        default_brand: str | None = None) -> StandinHintSet:
    kpis: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []
    scope: set[tuple[str, str]] = set()
    brands: list[str] = []
    for number, row in enumerate(frame.to_dict(orient="records"), start=1):
        name = _cell(row, "kpi")
        if not name:
            messages.append({"row": number, "kpi": "", "level": "error", "column": "kpi",
                             "message": "kpi is blank; the row is skipped"})
            continue
        if ";" in _cell(row, "value_sql"):
            messages.append({"row": number, "kpi": name, "level": "error", "column": "value_sql",
                             "message": "value_sql may not contain ';'"})
            continue
        if _cell(row, "brand"):
            brand = re.sub(r"[^A-Z0-9]+", "_", _cell(row, "brand").upper()).strip("_")
            if brand not in brands:
                brands.append(brand)
        kpi_id = _slug(name)
        scope_cols = {}
        for part in filter(None, (p.strip() for p in _cell(row, "scope_columns").split(";"))):
            dim, _, col = part.partition("=")
            scope_cols[dim.strip()] = col.strip()
        raw: dict[str, Any] = {
            "kpi_id": kpi_id, "version": 1, "status": "APPROVED", "approved_by": "kpi-table (hint)", "label": name,
            "definition": _cell(row, "notes") or f"{name} (KPI table hint)", "unit": _cell(row, "unit") or "count",
            "aggregation": "sum", "grains": list(GRAINS), "dimensions": sorted(scope_cols),
            "synonyms": [name, *filter(None, (a.strip() for a in _cell(row, "aliases").split(";")))],
            "tolerance": {"rel": 0.005, "abs": 0.0}, "definition_source": "hint",
        }
        table = _cell(row, "table").split(";")[0].strip()
        if table:
            plat: dict[str, Any] = {"from": table, "value": _cell(row, "value_sql"),
                                    "brand_column": _cell(row, "brand_column") or None,
                                    "date_expr": _date_expr(_cell(row, "date_column"), _cell(row, "date_format")),
                                    "scope_columns": scope_cols, "source_views": [table.split(".")[-1]]}
            if _cell(row, "filters"):
                plat["where"] = _cell(row, "filters")
            raw["platform"] = plat
            for dim, col in scope_cols.items():
                scope.add((dim, f"SELECT DISTINCT {col} AS v FROM {table} LIMIT 1000"))
        else:
            raw["platform"] = "none"
        kpis[kpi_id] = parse_kpi(raw, source_file="kpi_table")
    frame_messages = pd.DataFrame(messages, columns=["row", "kpi", "level", "column", "message"])
    return StandinHintSet(kpis=kpis, messages=frame_messages, scope=sorted(scope), brands=brands)


def standin_merge_registry(base: Registry, hints: StandinHintSet) -> Registry:
    from dataclasses import replace

    brands = dict(base.brands)
    for brand in hints.brands:
        if brand not in brands and brand.lower() not in base.brand_aliases():
            brands[brand] = {"brand_id": brand, "display_name": brand.title(), "aliases": [brand.lower()]}
    return replace(base, kpis={**base.kpis, **hints.kpis}, brands=brands)


def standin_lint_for_hints(registry: Registry) -> list[str]:
    return []


def standin_scope_values_sql(hints: StandinHintSet) -> list[tuple[str, str]]:
    return list(hints.scope)


def make_hints_module() -> types.ModuleType:
    mod = types.ModuleType("kpi_evidence.hints")
    mod.load_kpi_table = standin_load_kpi_table
    mod.parse_hints = standin_parse_hints
    mod.merge_registry = standin_merge_registry
    mod.lint_for_hints = standin_lint_for_hints
    mod.scope_values_sql = standin_scope_values_sql
    mod.HintSet = StandinHintSet
    return mod


@dataclass
class StandinResult:
    comparisons: pd.DataFrame
    documents: pd.DataFrame
    issues: pd.DataFrame
    kpis: pd.DataFrame
    summary: dict[str, Any]
    call: dict[str, Any]

    def save_html(self, path) -> str:
        Path(path).write_text(f"<html><body>{len(self.comparisons)} comparisons</body></html>", encoding="utf-8")
        return str(path)

    def save_excel(self, path) -> str:
        self.comparisons.to_excel(path, index=False)
        return str(path)

    def filter(self, *, flag=None, kpi=None, report=None) -> pd.DataFrame:
        out = self.comparisons
        if flag is not None and not out.empty:
            out = out[out["flag"] == flag]
        return out


def _flag(status: str) -> str:
    vs = ValidationStatus(status)
    if vs == ValidationStatus.UNMAPPED_CAPTURED:
        return "INFO"
    return STATUS_INFO[vs].group.value if STATUS_INFO[vs].group != StatusGroup.GOOD else "GOOD"


def standin_build_result(store, registry, settings, doc_ids, *, hints=None, run_id=None, platform_description="",
                         timings=None, extra_issues=None, focus_kpis=None, reused_ids=None,
                         previous_runs=None) -> StandinResult:
    ids = list(doc_ids)
    marks = ", ".join(f":d{i}" for i in range(len(ids))) or "NULL"
    params = {f"d{i}": d for i, d in enumerate(ids)}
    results = store.read_df("kpi_results", f"is_current = TRUE AND doc_id IN ({marks})", params,
                            order_by="doc_id, result_id")
    if not results.empty:
        results["flag"] = [_flag(s) for s in results["validation_status"]]
    docs = [store.get(Document, doc_id=d) for d in ids]
    documents = pd.DataFrame([{"file_name": d.file_name, "status": d.status.value, "confirmed_by": d.confirmed_by,
                               "doc_id": d.doc_id} for d in docs if d is not None])
    issues = pd.DataFrame([{"kind": "document", "report_file": d.file_name, "detail": d.status.value}
                           for d in docs if d is not None and d.status.value in ("AWAITING_CONFIRMATION", "FAILED")]
                          + [dict(m) for m in extra_issues or []])
    call = {"doc_ids": ids, "hints": hints, "run_id": run_id, "platform_description": platform_description,
            "timings": dict(timings or {}), "registry": registry, "focus_kpis": focus_kpis,
            "reused_ids": set(reused_ids or [])}
    return StandinResult(comparisons=results, documents=documents, issues=issues, kpis=pd.DataFrame(),
                         summary={"run_id": run_id}, call=call)


def make_report_module() -> types.ModuleType:
    mod = types.ModuleType("kpi_evidence.report")
    mod.build_result = standin_build_result
    mod.CheckResult = StandinResult
    return mod


def install_standins(monkeypatch, *, hints: bool = True, report: bool = True) -> None:
    import sys

    import kpi_evidence

    for name, factory, wanted in (("hints", make_hints_module, hints), ("report", make_report_module, report)):
        if not wanted:
            continue
        mod = factory()
        monkeypatch.setitem(sys.modules, f"kpi_evidence.{name}", mod)
        monkeypatch.setattr(kpi_evidence, name, mod, raising=False)


NOTEBOOK_KPI_ROWS = [
    {"kpi": "TRx", "aliases": "total prescriptions; trx units", "table": "main.platform_demo.rx_account_monthly",
     "value_sql": "SUM(trx_units)", "date_column": "month", "date_format": "yyyy-MM", "brand_column": "",
     "scope_columns": "region=region", "unit": "count", "brand": "KESTREL"},
    {"kpi": "NRx", "aliases": "new prescriptions", "table": "main.platform_demo.rx_account_monthly",
     "value_sql": "SUM(nrx)", "date_column": "month", "date_format": "yyyy-MM", "brand_column": "",
     "scope_columns": "region=region", "unit": "count", "brand": "KESTREL"},
    {"kpi": "Calls", "aliases": "calls (details delivered); details delivered",
     "table": "main.platform_demo.rx_account_monthly", "value_sql": "SUM(calls)", "date_column": "month",
     "date_format": "yyyy-MM", "brand_column": "", "scope_columns": "", "unit": "count", "brand": "KESTREL"},
    {"kpi": "Brand awareness", "aliases": "brand awareness (aided); aided awareness", "table": "", "value_sql": "",
     "date_column": "", "date_format": "", "brand_column": "", "scope_columns": "", "unit": "fraction",
     "brand": "KESTREL"},
]
