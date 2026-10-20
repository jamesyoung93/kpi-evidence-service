"""Governed geographic drill helpers for tiles and answer artifacts.

Drill is deliberately expressed as a re-scope operation.  The UI may move
from national to region, district, and territory, but every selected scope is
validated against the registered source before it can be sent back through
the normal answer pipeline.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from . import semantic_layer as sl


GEO_HIERARCHY = ("region", "district", "territory")
_ACCOUNT_METRICS = {
    "trx": "trx_ttm",
    "nrx": "nrx_ttm",
    "nbrx": "nbrx_ttm",
}


@dataclass(frozen=True)
class DrillOption:
    """One valid child scope under the current geographic selection."""

    dimension: str
    value: str
    filters: tuple[tuple[str, str], ...]
    account_count: int

    @property
    def label(self) -> str:
        return f"{self.value} · {self.account_count:,} HCPs"


def _normalized(scope: Mapping | None) -> dict[str, str]:
    if not scope:
        return {}
    normalized: dict[str, str] = {}
    for dimension, value in scope.items():
        if dimension not in sl.DIMENSIONS:
            raise ValueError(f"unregistered scope dimension: {dimension!r}")
        if isinstance(value, (list, tuple, set, frozenset)):
            values = tuple(dict.fromkeys(str(item) for item in value))
            if len(values) != 1:
                raise ValueError("geographic drill requires one value per dimension")
            value = values[0]
        normalized[str(dimension)] = str(value)
    return normalized


def next_dimension(scope: Mapping | None = None) -> str | None:
    """Return the next drill level, independent of non-geographic filters."""

    selected = _normalized(scope)
    for dimension in GEO_HIERARCHY:
        if dimension not in selected:
            return dimension
    return None


def breadcrumbs(scope: Mapping | None = None) -> tuple[tuple[str, dict[str, str]], ...]:
    """Return clickable breadcrumb labels and the scope each crumb represents."""

    selected = _normalized(scope)
    crumbs: list[tuple[str, dict[str, str]]] = [("National", {
        dimension: value for dimension, value in selected.items()
        if dimension not in GEO_HIERARCHY
    })]
    carried = dict(crumbs[0][1])
    for dimension in GEO_HIERARCHY:
        if dimension not in selected:
            break
        carried[dimension] = selected[dimension]
        label = f"{dimension.title()}: {selected[dimension]}"
        crumbs.append((label, dict(carried)))
    return tuple(crumbs)


def child_options(scope: Mapping | None = None) -> tuple[DrillOption, ...]:
    """List registered children of the current scope in stable business order."""

    selected = _normalized(scope)
    dimension = next_dimension(selected)
    if dimension is None:
        return ()
    frame = sl.apply_filters(sl.load_fact("source_a"), selected)
    if frame.empty:
        return ()
    counts = (frame[[dimension, "account_id"]].drop_duplicates()
              .groupby(dimension, dropna=False)["account_id"].nunique())
    options: list[DrillOption] = []
    for raw_value, raw_count in counts.sort_index().items():
        value = str(raw_value)
        scoped = dict(selected)
        scoped[dimension] = value
        filters = tuple((key, scoped[key]) for key in sl.DIMENSIONS if key in scoped)
        options.append(DrillOption(dimension, value, filters, int(raw_count)))
    return tuple(options)


def select_child(scope: Mapping | None, dimension: str, value: str) -> dict[str, str]:
    """Validate and return a selected child; guessed or stale values are refused."""

    selected = _normalized(scope)
    expected = next_dimension(selected)
    if dimension != expected:
        raise ValueError(f"next drill dimension is {expected!r}, not {dimension!r}")
    match = next((option for option in child_options(selected)
                  if option.value == str(value)), None)
    if match is None:
        raise ValueError(f"{value!r} is not a registered {dimension} child of this scope")
    return dict(match.filters)


def hcp_rows(scope: Mapping | None, metric: str = "trx", *, top_n: int = 25,
             min_volume: float = 0.0) -> pd.DataFrame:
    """Return a deterministic, governed territory-level HCP ranking.

    The account source stores trailing-twelve-month prescription measures.  A
    minimum-volume floor is applied before ranking and disclosed by callers.
    Territory scope is required so an accidental national export cannot be
    presented as the endpoint of a geographic drill.
    """

    selected = _normalized(scope)
    if "territory" not in selected:
        raise ValueError("HCP drill requires a selected territory")
    if metric not in _ACCOUNT_METRICS:
        raise ValueError("HCP drill is registered for TRx, NRx, and NBRx only")
    if top_n < 1 or top_n > 100:
        raise ValueError("top_n must be between 1 and 100")
    if min_volume < 0:
        raise ValueError("min_volume cannot be negative")

    accounts = sl.apply_filters(sl.load_accounts(), selected)
    ranking = _ACCOUNT_METRICS[metric]
    accounts = accounts[pd.to_numeric(accounts[ranking], errors="coerce") >= min_volume]
    identity = "npi" if "npi" in accounts.columns else "account_id"
    columns = [identity, "name", "specialty", "territory", "district", "region",
               "payer_channel", "trx_ttm", "nrx_ttm", "nbrx_ttm", "decile",
               "calls_90d", "call_plan_90d"]
    available = [column for column in columns if column in accounts.columns]
    return (accounts.sort_values([ranking, identity], ascending=[False, True])
            .head(top_n)[available].reset_index(drop=True))
