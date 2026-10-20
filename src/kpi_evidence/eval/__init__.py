from __future__ import annotations

from typing import Any

__all__ = ["truth", "synth_reports", "metrics", "evaluate", "generate_all", "compute_metrics", "run_evaluation"]


def __getattr__(name: str) -> Any:
    import importlib

    if name in ("truth", "synth_reports", "metrics", "evaluate"):
        return importlib.import_module(f"{__name__}.{name}")
    if name == "generate_all":
        return importlib.import_module(f"{__name__}.synth_reports").generate_all
    if name == "compute_metrics":
        return importlib.import_module(f"{__name__}.metrics").compute_metrics
    if name == "run_evaluation":
        return importlib.import_module(f"{__name__}.evaluate").run_evaluation
    raise AttributeError(name)
