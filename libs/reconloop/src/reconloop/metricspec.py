from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from math import isfinite
from copy import deepcopy


@dataclass
class Param:
    name: str
    hypothesis: str
    default: str
    options: dict[str, str | dict]
    describe: dict[str, str] = field(default_factory=dict)
    keywords: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class RatioSpec:
    """Two scalar query templates; component values are retained in every evaluation."""
    numerator: str
    denominator: str
    scale: float = 1.0
    zero_denominator: str = "error"

    def __post_init__(self):
        if not self.numerator.strip() or not self.denominator.strip():
            raise ValueError("ratio needs numerator and denominator queries")
        if not isfinite(self.scale) or self.scale <= 0:
            raise ValueError("ratio scale must be finite and positive")
        if self.zero_denominator not in {"error", "zero"}:
            raise ValueError("zero_denominator must be error or zero")


@dataclass
class Move:
    """An explicitly declared compound candidate for deterministic search."""
    options: dict[str, str]
    rationale: str


@dataclass
class MetricSpec:
    name: str
    template: str
    params: dict[str, Param]
    unit: str = ""
    tolerance: float = 0.005
    ratio: RatioSpec | None = None
    moves: dict[str, Move] = field(default_factory=dict)
    checks: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not isfinite(self.tolerance) or self.tolerance < 0:
            raise ValueError("tolerance must be finite and nonnegative")
        if bool(self.template.strip()) == bool(self.ratio):
            raise ValueError("provide exactly one scalar template or ratio specification")
        self.initial_choices()
        for name, move in self.moves.items():
            if len(move.options) < 2 or not move.rationale.strip():
                raise ValueError(f"move {name!r} needs at least two parameters and a rationale")
            self.initial_choices(move.options)

    def defaults(self) -> dict[str, str]:
        return {k: p.default for k, p in self.params.items()}

    def initial_choices(self, start: dict[str, str] | None = None) -> dict[str, str]:
        choices = self.defaults() | (start or {})
        if choices.keys() - self.params.keys():
            raise ValueError("start contains unknown parameters")
        for key, option in choices.items():
            if option not in self.params[key].options:
                raise ValueError(f"{key}: '{option}' is not an allowed option")
        return choices

    def _fragments(self, choices: dict[str, str], as_of: str) -> dict[str, str]:
        from .sql import fragment
        if date.fromisoformat(as_of).isoformat() != as_of:
            raise ValueError("as_of must be an ISO date (YYYY-MM-DD)")
        choices = self.initial_choices(choices)
        frags = {"as_of": as_of}
        for k, p in self.params.items():
            opt = choices.get(k, p.default)
            if opt not in p.options:
                raise ValueError(f"{self.name}: '{opt}' is not an allowed option for '{k}' ({list(p.options)})")
            frags[k] = fragment(p.options[opt], as_of)
        return frags

    def render_components(self, choices: dict[str, str], as_of: str) -> dict[str, str]:
        frags = self._fragments(choices, as_of)
        if self.ratio:
            return {"numerator": self.ratio.numerator.format(**frags),
                    "denominator": self.ratio.denominator.format(**frags)}
        return {"value": self.template.format(**frags)}

    def render(self, choices: dict[str, str], as_of: str) -> str:
        queries = self.render_components(choices, as_of)
        if not self.ratio:
            return queries["value"]
        quotient = f"(1.0 * n.value / NULLIF(d.value, 0)) * {self.ratio.scale!r}"
        if self.ratio.zero_denominator == "zero":
            quotient = f"CASE WHEN d.value = 0 THEN 0.0 ELSE {quotient} END"
        return (f"SELECT {quotient} AS value FROM ({queries['numerator']}) n "
                f"CROSS JOIN ({queries['denominator']}) d")

    def compute(self, runner, choices: dict[str, str], as_of: str) -> tuple[float, dict]:
        frags = self._fragments(choices, as_of)
        for name, query in self.checks.items():
            violations = runner.run(query.format(**frags))
            if violations != 0:
                raise ValueError(f"quality check {name!r} failed: {violations:g} violations")
        queries = self.render_components(choices, as_of)
        values = {key: runner.run(sql) for key, sql in queries.items()}
        if not self.ratio:
            return values["value"], {}
        n, d = values["numerator"], values["denominator"]
        if d == 0 and self.ratio.zero_denominator == "error":
            raise ValueError(f"{self.name}: ratio denominator is zero at {as_of}")
        value = (n / d * self.ratio.scale) if d else 0.0
        if not isfinite(value):
            raise ValueError("ratio result must be finite")
        return value, dict(values, scale=self.ratio.scale, queries=queries)

    def describe(self, choices: dict[str, str]) -> str:
        parts = []
        for k, p in self.params.items():
            opt = choices.get(k, p.default)
            parts.append(f"{k}={opt}" + (f" ({p.describe[opt]})" if opt in p.describe else ""))
        return "; ".join(parts)


def load_specs(cfg: dict) -> dict[str, MetricSpec]:
    specs = {}
    for name, m in (cfg.get("metrics") or {}).items():
        params = {}
        declarations = {}
        for group in m.get("param_groups", []):
            if group not in cfg.get("param_groups", {}):
                raise ValueError(f"{name}: unknown parameter group {group!r}")
            for pname, declaration in cfg["param_groups"][group].items():
                if pname in declarations:
                    raise ValueError(f"{name}: duplicate shared parameter {pname!r}")
                declarations[pname] = deepcopy(declaration)
        declarations.update(deepcopy(m.get("params") or {}))
        for pname, p in declarations.items():
            params[pname] = Param(name=pname, hypothesis=p["hypothesis"], default=p["default"],
                                  options=dict(p["options"]), describe=dict(p.get("describe", {})),
                                  keywords={k: list(v) for k, v in (p.get("keywords") or {}).items()})
        specs[name] = MetricSpec(name=name, template=m.get("template", ""), params=params,
                                 unit=m.get("unit", ""), tolerance=float(m.get("tolerance", 0.005)),
                                 ratio=RatioSpec(**m["ratio"]) if "ratio" in m else None,
                                 moves={key: Move(**v) for key, v in m.get("moves", {}).items()},
                                 checks=dict(m.get("checks", {})))
    return specs
