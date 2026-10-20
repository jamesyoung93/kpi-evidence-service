from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

_enabled = False


def configure(settings) -> bool:
    """Enable tracing for this process when requested and available."""
    global _enabled
    _enabled = False
    if not getattr(settings, "tracing", False):
        return False
    try:
        import mlflow

        if settings.mlflow_experiment:
            mlflow.set_experiment(settings.mlflow_experiment)
        _enabled = True
    except Exception:
        _enabled = False
    return _enabled


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    if not _enabled:
        yield None
        return
    import mlflow

    with mlflow.start_span(name=name) as s:
        try:
            s.set_attributes({k: v for k, v in attributes.items() if isinstance(v, (str, int, float, bool))})
        except Exception:
            pass
        yield s
