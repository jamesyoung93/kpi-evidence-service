from __future__ import annotations

from typing import Protocol
from math import isfinite


def scalar_value(value) -> float:
    if value is None:
        raise ValueError("metric query returned NULL; explicitly use COALESCE if zero is intended")
    value = float(value)
    if not isfinite(value):
        raise ValueError("metric query must return a finite value")
    return value


class Runner(Protocol):
    def run(self, sql: str) -> float:
        """Execute a query that returns a single row with a column named `value`."""
        ...
