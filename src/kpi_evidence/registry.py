from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .contracts import Brand, GoalListEntry, RegistryEntry, RegistryStatus, ReportType
from .ids import canonical_json, scope_key, sha256_text, utcnow

UNITS = ("count", "usd", "fraction")
AGGREGATIONS = ("sum", "ratio_of_sums", "point", "count")
GRAINS = ("month", "quarter", "r3m", "r6m", "r12m", "ytd")
DIMENSIONS = ("region", "district", "territory", "specialty", "payer_channel")
BINDINGS = ("gov", "platform", "restricted", "brand", "window", "scope")

_BINDING = re.compile(r"\$\{([A-Za-z_]+)\}")
_PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


@dataclass(frozen=True)
class ParamSpec:
    name: str
    hypothesis: str
    default: str
    options: dict[str, str]
    describe: dict[str, str] = field(default_factory=dict)
    keywords: dict[str, list[str]] = field(default_factory=dict)
    permitted: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlatformSpec:
    source_views: tuple[str, ...]
    template: str | None
    ratio: dict[str, Any] | None
    params: dict[str, ParamSpec]
    checks: dict[str, str]
    date_expr: str | None = None
    scope_columns: dict[str, str] = field(default_factory=dict)

    def sql_texts(self) -> list[str]:
        texts = [self.template] if self.template else [self.ratio["numerator"], self.ratio["denominator"]]
        texts += list(self.checks.values())
        for p in self.params.values():
            texts += [str(v) for v in p.options.values()]
        return texts


@dataclass(frozen=True)
class Tolerance:
    rel: float
    abs: float
    use_display_rounding: bool = True


@dataclass(frozen=True)
class KpiDefinition:
    kpi_id: str
    version: int
    status: RegistryStatus
    label: str
    definition: str
    unit: str
    aggregation: str
    grains: tuple[str, ...]
    dimensions: tuple[str, ...]
    synonyms: tuple[str, ...]
    mapping_description: str
    claim_class: str
    owner: str
    tolerance: Tolerance
    platform: PlatformSpec | None
    harness_link: dict[str, str] | None
    approved_by: str | None
    approved_at: date | None
    raw: dict[str, Any]
    source_file: str
    platform_effective: dict[str, Any] | None = None

    @property
    def has_platform_spec(self) -> bool:
        return self.platform is not None

    @property
    def is_causal(self) -> bool:
        return self.claim_class == "causal"

    def definition_hash(self) -> str:
        """Hash of everything that changes what the KPI *means* or how it is computed/judged."""
        payload = {k: self.raw.get(k) for k in ("kpi_id", "version", "unit", "aggregation", "grains",
                                                 "dimensions", "claim_class", "tolerance")}
        payload["platform"] = self.platform_effective if self.platform_effective is not None else self.raw.get("platform")
        return sha256_text(canonical_json(payload))


@dataclass
class Registry:
    kpis: dict[str, KpiDefinition]
    brands: dict[str, dict[str, Any]]
    report_types: dict[str, dict[str, Any]]
    goal_list: list[dict[str, Any]]
    policy: dict[str, Any]
    directory: Path
    param_groups: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def version(self) -> str:
        """Registry version = hash over every approved KPI definition hash (order-independent)."""
        hashes = sorted(f"{k.kpi_id}:{k.version}:{k.definition_hash()}" for k in self.approved())
        return sha256_text("|".join(hashes))[:16]

    def approved(self) -> list[KpiDefinition]:
        return [k for k in self.kpis.values() if k.status == RegistryStatus.APPROVED]

    def get(self, kpi_id: str) -> KpiDefinition:
        try:
            return self.kpis[kpi_id]
        except KeyError:
            raise KeyError(f"unknown KPI {kpi_id!r}") from None

    def brand_aliases(self) -> dict[str, str]:
        """normalized alias -> brand_id (includes the id and display name)."""
        out: dict[str, str] = {}
        for brand_id, b in self.brands.items():
            for alias in [brand_id, b.get("display_name", ""), *b.get("aliases", [])]:
                if alias:
                    out[normalize_label(alias)] = brand_id
        return out

    def assumed_definitions(self, report_type: str | None, kpi_id: str) -> dict[str, str]:
        """Owner-declared conventions of a report type for one KPI ({param: option}); {} when none."""
        if not report_type or report_type not in self.report_types:
            return {}
        assumed = (self.report_types[report_type].get("assumed_definitions") or {}).get(kpi_id) or {}
        return {str(k): str(v) for k, v in assumed.items()}

    def goal_kpis(self, brand_id: str) -> list[dict[str, Any]]:
        return [g for g in self.goal_list if g["brand_id"] == brand_id]

    def registry_rows(self) -> list[RegistryEntry]:
        now = utcnow()
        version = self.version
        return [RegistryEntry(
            kpi_id=k.kpi_id, kpi_version=k.version, definition_hash=k.definition_hash(), status=k.status,
            label=k.label, definition=k.definition, unit=k.unit, aggregation=k.aggregation,
            has_platform_spec=k.has_platform_spec, claim_class=k.claim_class, owner=k.owner,
            spec=k.raw, registry_version=version, loaded_at=now, approved_by=k.approved_by,
            approved_at=k.approved_at) for k in self.kpis.values()]

    def config_rows(self, actor: str = "registry-sync") -> tuple[list[Brand], list[GoalListEntry], list[ReportType]]:
        now = utcnow()
        brands = [Brand(brand_id=b, version=1, status=RegistryStatus.APPROVED, display_name=v.get("display_name", b),
                        aliases=list(v.get("aliases", [])), fiscal_year_start_month=int(v.get("fiscal_year_start_month", 1)),
                        updated_by=actor, updated_at=now) for b, v in self.brands.items()]
        goals = [GoalListEntry(brand_id=g["brand_id"], kpi_id=g["kpi_id"], scope_key=scope_key(g.get("scope")),
                               version=1, status=RegistryStatus.APPROVED, cadence=g.get("cadence", "monthly"),
                               expected_lag_periods=int(g.get("expected_lag_periods", 1)),
                               expected_report_types=list(g.get("expected_report_types", [])),
                               owner=g.get("owner", self.kpis[g["kpi_id"]].owner if g["kpi_id"] in self.kpis else ""),
                               updated_by=actor, updated_at=now) for g in self.goal_list]
        rtypes = [ReportType(report_type=r, version=1, status=RegistryStatus.APPROVED,
                             display_name=v.get("display_name", r), keywords=list(v.get("keywords", [])),
                             default_grain=v.get("default_grain", "month"), updated_by=actor, updated_at=now)
                  for r, v in self.report_types.items()]
        return brands, goals, rtypes


def normalize_label(text: str) -> str:
    """Lowercase, unify dashes/ampersands, drop punctuation except % and $, collapse spaces."""
    t = str(text).lower().replace("–", "-").replace("—", "-").replace("&", " and ")
    t = re.sub(r"[^\w%$\s-]", " ", t)
    t = t.replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", t).strip()


def _parse_param(name: str, raw: dict[str, Any]) -> ParamSpec:
    options = {str(k): str(v) for k, v in (raw.get("options") or {}).items()}
    keywords = {str(k): [str(x) for x in v] for k, v in (raw.get("keywords") or {}).items()}
    return ParamSpec(name=name, hypothesis=str(raw["hypothesis"]), default=str(raw["default"]), options=options,
                     describe={str(k): str(v) for k, v in (raw.get("describe") or {}).items()},
                     keywords=keywords, permitted=tuple(str(x) for x in raw.get("permitted", [raw["default"]])))


_SHORTHAND_KEYS = ("view", "from", "value", "numerator", "denominator")
_GOV_REF = re.compile(r"\$\{gov\}\.([A-Za-z_][A-Za-z0-9_]*)")


def expand_platform(raw: Any, param_groups: dict[str, dict[str, Any]] | None = None) -> Any:
    """Expand the shorthand form and shared parameter groups into the full platform block."""
    if raw in (None, "none", "None", False) or not isinstance(raw, dict):
        return raw
    out = {k: v for k, v in raw.items() if k != "param_groups"}
    params: dict[str, Any] = {}
    for group in raw.get("param_groups") or []:
        if not param_groups or group not in param_groups:
            raise ValueError(f"unknown param group {group!r}")
        for name, spec in param_groups[group].items():
            if name in params:
                raise ValueError(f"parameter {name!r} declared by two param groups")
            params[name] = spec
    params.update(raw.get("params") or {})
    out["params"] = params
    if not any(k in raw for k in _SHORTHAND_KEYS):
        return out
    if "template" in raw or "ratio" in raw:
        raise ValueError("use either the shorthand (view/value) or template/ratio, not both")
    view = raw.get("view")
    if raw.get("from"):
        relation = raw["from"]
    elif view:
        relation = view if view.startswith("{") else f"${{gov}}.{view}"
    else:
        raise ValueError("shorthand needs 'view' or 'from'")
    brand_column = raw.get("brand_column", "brand")
    filters = f"{brand_column} = ${{brand}} AND ${{window}}" if brand_column else "${window}"
    if raw.get("scoped", True):
        filters += " AND ${scope}"
    if raw.get("where"):
        filters += f" AND {raw['where']}"
    for key in _SHORTHAND_KEYS + ("where", "scoped", "scale", "zero_denominator", "brand_column"):
        out.pop(key, None)
    if "value" in raw:
        out["template"] = f"SELECT {raw['value']} AS value\nFROM {relation}\nWHERE {filters}"
    else:
        if not ("numerator" in raw and "denominator" in raw):
            raise ValueError("shorthand needs 'value', or both 'numerator' and 'denominator'")
        out["ratio"] = {
            "numerator": f"SELECT {raw['numerator']} AS value FROM {relation} WHERE {filters}",
            "denominator": f"SELECT {raw['denominator']} AS value FROM {relation} WHERE {filters}",
            "scale": float(raw.get("scale", 1.0)),
            "zero_denominator": str(raw.get("zero_denominator", "error")),
        }
    if "checks" not in raw:
        out["checks"] = {"period_has_rows":
                         f"SELECT CASE WHEN COUNT(*) > 0 THEN 0 ELSE 1 END AS value FROM {relation} WHERE ${{window}}"}
    if "source_views" not in raw:
        texts = [relation] + [str(o) for p in params.values() for o in (p.get("options") or {}).values()]
        views: list[str] = []
        if view and not view.startswith("{"):
            views.append(view)
        for t in texts:
            views += _GOV_REF.findall(t)
        if raw.get("from") and not views:
            views.append(str(raw["from"]).split(".")[-1])
        out["source_views"] = list(dict.fromkeys(views))
    return out


def _parse_platform(raw: Any) -> PlatformSpec | None:
    if raw in (None, "none", "None", False):
        return None
    if not isinstance(raw, dict):
        raise ValueError("platform must be 'none' or a mapping")
    params = {n: _parse_param(n, p) for n, p in (raw.get("params") or {}).items()}
    return PlatformSpec(source_views=tuple(raw.get("source_views", [])), template=raw.get("template"),
                        ratio=raw.get("ratio"), params=params,
                        checks={str(k): str(v) for k, v in (raw.get("checks") or {}).items()},
                        date_expr=raw.get("date_expr") or None,
                        scope_columns={str(k): str(v) for k, v in (raw.get("scope_columns") or {}).items()})


def parse_kpi(raw: dict[str, Any], source_file: str = "",
              param_groups: dict[str, dict[str, Any]] | None = None) -> KpiDefinition:
    tol = raw.get("tolerance") or {}
    effective = expand_platform(raw.get("platform"), param_groups)
    approved_at = raw.get("approved_at")
    if isinstance(approved_at, str):
        approved_at = date.fromisoformat(approved_at)
    return KpiDefinition(
        kpi_id=str(raw["kpi_id"]), version=int(raw["version"]), status=RegistryStatus(raw.get("status", "DRAFT")),
        label=str(raw["label"]), definition=" ".join(str(raw.get("definition", "")).split()),
        unit=str(raw["unit"]), aggregation=str(raw["aggregation"]), grains=tuple(raw.get("grains", ["month"])),
        dimensions=tuple(raw.get("dimensions", [])), synonyms=tuple(str(s) for s in raw.get("synonyms", [])),
        mapping_description=str(raw.get("mapping_description", "")), claim_class=str(raw.get("claim_class", "descriptive")),
        owner=str(raw.get("owner", "")),
        tolerance=Tolerance(rel=float(tol.get("rel", 0.0)), abs=float(tol.get("abs", 0.0)),
                            use_display_rounding=bool(tol.get("use_display_rounding", True))),
        platform=_parse_platform(effective), harness_link=raw.get("harness_link"),
        approved_by=raw.get("approved_by"), approved_at=approved_at, raw=raw, source_file=source_file,
        platform_effective=effective if isinstance(effective, dict) else None)


def load_registry(directory: str | Path | None = None) -> Registry:
    from .settings import default_registry_dir

    d = Path(directory) if directory else default_registry_dir()
    groups_path = d / "param_groups.yaml"
    param_groups = ((yaml.safe_load(groups_path.read_text(encoding="utf-8")) or {}).get("param_groups", {})
                    if groups_path.exists() else {})
    kpis: dict[str, KpiDefinition] = {}
    for path in sorted((d / "kpis").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        kpi = parse_kpi(raw, source_file=str(path.name), param_groups=param_groups)
        if kpi.kpi_id in kpis:
            raise ValueError(f"duplicate kpi_id {kpi.kpi_id!r} in {path.name}")
        kpis[kpi.kpi_id] = kpi

    def _load(name: str, key: str) -> Any:
        p = d / name
        return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get(key, []) if p.exists() else []

    brands = {b["brand_id"]: b for b in _load("brands.yaml", "brands")}
    rtypes = {r["report_type"]: r for r in _load("report_types.yaml", "report_types")}
    goal_list = list(_load("goal_list.yaml", "goal_list"))
    policy_path = d / "policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}
    return Registry(kpis=kpis, brands=brands, report_types=rtypes, goal_list=goal_list, policy=policy or {},
                    directory=d, param_groups=param_groups)


def lint(registry: Registry) -> list[str]:
    """Return human-readable problems; an empty list means the registry is consistent."""
    from reconloop.hypotheses import CATALOG

    problems: list[str] = []
    seen_synonyms: dict[str, str] = {}
    for k in registry.kpis.values():
        where = f"{k.kpi_id} ({k.source_file})"
        if not re.fullmatch(r"[a-z][a-z0-9_]*", k.kpi_id):
            problems.append(f"{where}: kpi_id must be lower_snake_case")
        if k.unit not in UNITS:
            problems.append(f"{where}: unit {k.unit!r} not in {UNITS}")
        if k.aggregation not in AGGREGATIONS:
            problems.append(f"{where}: aggregation {k.aggregation!r} not in {AGGREGATIONS}")
        for g in k.grains:
            if g not in GRAINS:
                problems.append(f"{where}: grain {g!r} not in {GRAINS}")
        for dim in k.dimensions:
            if dim not in DIMENSIONS:
                problems.append(f"{where}: dimension {dim!r} not in {DIMENSIONS}")
        if k.tolerance.rel < 0 or k.tolerance.abs < 0:
            problems.append(f"{where}: tolerance must be >= 0")
        if k.is_causal and k.has_platform_spec:
            problems.append(f"{where}: causal claims cannot have a platform spec (they are never VERIFIED)")
        for syn in k.synonyms:
            n = normalize_label(syn)
            if n in seen_synonyms and seen_synonyms[n] != k.kpi_id:
                problems.append(f"{where}: synonym {syn!r} also used by {seen_synonyms[n]}")
            seen_synonyms.setdefault(n, k.kpi_id)
        if k.status == RegistryStatus.APPROVED and not k.approved_by:
            problems.append(f"{where}: APPROVED needs approved_by")
        p = k.platform
        if p is None:
            continue
        if bool(p.template) == bool(p.ratio):
            problems.append(f"{where}: exactly one of template or ratio is required")
        if p.ratio and not {"numerator", "denominator"} <= set(p.ratio):
            problems.append(f"{where}: ratio needs numerator and denominator")
        for name, param in p.params.items():
            if param.hypothesis not in CATALOG:
                problems.append(f"{where}: param {name} hypothesis {param.hypothesis!r} not in reconloop CATALOG")
            if param.default not in param.options:
                problems.append(f"{where}: param {name} default {param.default!r} is not an option")
            for opt in param.permitted:
                if opt not in param.options:
                    problems.append(f"{where}: param {name} permitted option {opt!r} is not an option")
            if param.default not in param.permitted:
                problems.append(f"{where}: param {name} default must be permitted")
            for opt in param.keywords:
                if opt not in param.options:
                    problems.append(f"{where}: param {name} keywords for unknown option {opt!r}")
        main_sql = [p.template] if p.template else [p.ratio["numerator"], p.ratio["denominator"]]
        used = set()
        for text in main_sql:
            used |= set(_PLACEHOLDER.findall(_BINDING.sub("", text)))
        declared = set(p.params) | {"as_of"}
        if used - declared:
            problems.append(f"{where}: undeclared placeholders {sorted(used - declared)}")
        for name in p.params:
            if name not in used:
                problems.append(f"{where}: param {name} is not used by the value SQL")
        for text in p.sql_texts():
            for b in _BINDING.findall(text):
                if b not in BINDINGS:
                    problems.append(f"{where}: unknown binding ${{{b}}}")
            for opt_text in [text]:
                if "{{" in opt_text or "}}" in opt_text:
                    problems.append(f"{where}: literal braces are not allowed in registry SQL")
        for name, check in p.checks.items():
            extra = set(_PLACEHOLDER.findall(_BINDING.sub("", check))) - declared
            if extra:
                problems.append(f"{where}: check {name} uses undeclared placeholders {sorted(extra)}")
    for rtype, spec in registry.report_types.items():
        for kpi_id, assumed in (spec.get("assumed_definitions") or {}).items():
            k = registry.kpis.get(kpi_id)
            if k is None or k.platform is None:
                problems.append(f"report_types.{rtype}: assumed_definitions for unknown or report-only KPI {kpi_id!r}")
                continue
            for param, option in (assumed or {}).items():
                ps = k.platform.params.get(param)
                if ps is None or option not in ps.options:
                    problems.append(f"report_types.{rtype}: {kpi_id}.{param}={option!r} is not a declared option")
                elif option not in ps.permitted:
                    problems.append(f"report_types.{rtype}: {kpi_id}.{param}={option!r} must be a permitted option "
                                    "(declare it permitted in the KPI before a report type may assume it)")
    for g in registry.goal_list:
        if g.get("kpi_id") not in registry.kpis:
            problems.append(f"goal_list: unknown kpi_id {g.get('kpi_id')!r}")
        if g.get("brand_id") not in registry.brands:
            problems.append(f"goal_list: unknown brand_id {g.get('brand_id')!r}")
    return problems
