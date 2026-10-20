from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kpi_evidence import platform
from kpi_evidence.compile import compile_kpi
from kpi_evidence.registry import load_registry
from kpi_evidence.settings import Settings
from kpi_evidence.store.base import scalar_value
from kpi_evidence.store.duckdb_store import DuckDBStore

ROOT = Path(__file__).resolve().parents[2]
JUNE = "2026-06-30"
MAY = "2026-05-31"


@dataclass(frozen=True)
class KpiCase:
    kpi_id: str
    grain: str
    variant: str
    choices: tuple[tuple[str, str], ...]
    as_of: str
    scope: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        scope = ",".join(f"{k}={v}" for k, v in self.scope)
        return f"{self.kpi_id}|{self.grain}|{self.variant}|{self.as_of}|{scope}"

    @property
    def uses_panel(self) -> bool:
        return dict(self.choices).get("source") == "panel"


@dataclass
class CaseOutcome:
    value: float | None = None
    components: dict = field(default_factory=dict)
    error: BaseException | None = None


def kpi_cases(registry) -> list[KpiCase]:
    cases = []
    for kpi in sorted(registry.kpis.values(), key=lambda k: k.kpi_id):
        if kpi.platform is None:
            continue
        defaults = {name: p.default for name, p in kpi.platform.params.items()}
        variants = [("default", defaults)]
        for name, p in kpi.platform.params.items():
            variants += [(f"{name}={opt}", {**defaults, name: opt}) for opt in p.options if opt != p.default]
        for grain in kpi.grains:
            for label, choices in variants:
                for as_of in ([JUNE, MAY] if choices.get("source") == "panel" else [JUNE]):
                    cases.append(KpiCase(kpi.kpi_id, grain, label, tuple(sorted(choices.items())), as_of))
    return cases


def compile_case(registry, settings: Settings, case: KpiCase):
    return compile_kpi(registry.get(case.kpi_id), brand=platform.DEMO_BRAND, scope=dict(case.scope),
                       grain=case.grain, settings=settings)


def evaluate_case(registry, settings: Settings, runner, case: KpiCase) -> CaseOutcome:
    compiled = compile_case(registry, settings, case)
    try:
        value, components = compiled.spec.compute(runner, dict(case.choices), case.as_of)
    except Exception as exc:
        return CaseOutcome(error=exc)
    return CaseOutcome(value=value, components=components)


class UnrestrictedRunner:
    def __init__(self, store: DuckDBStore):
        self.store = store

    def run(self, sql: str) -> float:
        return scalar_value(self.store.query(sql))


@pytest.fixture(scope="session")
def platform_registry():
    return load_registry(ROOT / "registry")


@pytest.fixture(scope="session")
def platform_settings(tmp_path_factory) -> Settings:
    return Settings.from_env(local_dir=tmp_path_factory.mktemp("platform") / ".kpi_evidence", ai_mode="off",
                             registry_dir=ROOT / "registry")


@pytest.fixture(scope="session")
def platform_store(platform_settings) -> DuckDBStore:
    store = DuckDBStore(platform_settings, ":memory:")
    store.ensure_schema()
    platform.load_demo_platform(store, platform_settings)
    platform.create_governed_views(store, platform_settings)
    return store


@pytest.fixture()
def fresh_platform(mem_store) -> DuckDBStore:
    platform.load_demo_platform(mem_store, mem_store.settings)
    platform.create_governed_views(mem_store, mem_store.settings)
    return mem_store


@pytest.fixture(scope="session")
def all_cases(platform_registry) -> list[KpiCase]:
    return kpi_cases(platform_registry)


@pytest.fixture(scope="session")
def duckdb_outcomes(platform_registry, platform_settings, platform_store, all_cases) -> dict[str, CaseOutcome]:
    runner = platform_store.runner()
    return {c.key: evaluate_case(platform_registry, platform_settings, runner, c) for c in all_cases}


@pytest.fixture(scope="session")
def duckdb_unrestricted_outcomes(platform_registry, platform_settings, platform_store, all_cases) -> dict[str, CaseOutcome]:
    runner = UnrestrictedRunner(platform_store)
    return {c.key: evaluate_case(platform_registry, platform_settings, runner, c) for c in all_cases}


@pytest.fixture(scope="session")
def case_tools():
    return {"kpi_cases": kpi_cases, "compile_case": compile_case, "evaluate_case": evaluate_case,
            "KpiCase": KpiCase, "UnrestrictedRunner": UnrestrictedRunner, "JUNE": JUNE, "MAY": MAY}
