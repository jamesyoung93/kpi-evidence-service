from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

__all__ = ["CheckResult", "ReportCheck", "__version__", "load_kpi_table"]

_LAZY = {
    "ReportCheck": ("check", "ReportCheck"),
    "CheckResult": ("report", "CheckResult"),
    "load_kpi_table": ("hints", "load_kpi_table"),
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY:
        raise AttributeError(f"module 'kpi_evidence' has no attribute {name!r}")
    from importlib import import_module

    module_name, attr = _LAZY[name]
    value = getattr(import_module(f".{module_name}", __name__), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
