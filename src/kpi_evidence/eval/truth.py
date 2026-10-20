from __future__ import annotations

import calendar
import functools
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

import pandas as pd

DIMENSIONS = ("region", "district", "territory", "specialty", "payer_channel")
_ROLLING = {"month": 1, "quarter": 3, "r3m": 3, "r6m": 6, "r12m": 12}

EXFACTORY_FACTOR = 1.04
REFERRAL_COVERAGE = 0.8


class TruthError(ValueError):
    """An unsupported KPI, grain, scope dimension or definition choice."""


@dataclass(frozen=True)
class PlatformData:
    account: pd.DataFrame
    panel: pd.DataFrame
    referrals: pd.DataFrame

    @property
    def latest_month(self) -> str:
        return str(self.account["month"].max())


def _platform_dir(data_dir: str | Path | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    from ..settings import demo_dir

    return demo_dir("platform")


@functools.lru_cache(maxsize=4)
def _load_cached(directory: str) -> PlatformData:
    d = Path(directory)
    account = pd.read_csv(d / "fact_source_a.csv", dtype={"npi": str, "month": str})
    panel = pd.read_csv(d / "fact_source_b.csv", dtype={"month": str})
    referrals = pd.read_csv(d / "fact_referral.csv", dtype={"npi": str, "month": str})
    account["is_writer"] = (account["trx_units"] > 0).astype(int)
    return PlatformData(account=account, panel=panel, referrals=referrals)


def load_platform(data_dir: str | Path | None = None) -> PlatformData:
    """Read the synthetic platform CSVs (cached per directory)."""
    return _load_cached(str(_platform_dir(data_dir).resolve()))


def to_date(value: date | str) -> date:
    """Accept a date, 'YYYY-MM-DD' or 'YYYY-MM' (the month end is used for the latter)."""
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) == 7:
        y, m = int(text[:4]), int(text[5:7])
        return month_end(y, m)
    return date.fromisoformat(text[:10])


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def window_months(period_end: date | str, grain: str = "month") -> list[str]:
    """'YYYY-MM' keys of the months in the window ending at ``period_end`` (oldest first)."""
    end = to_date(period_end)
    if grain == "ytd":
        return [f"{end.year:04d}-{m:02d}" for m in range(1, end.month + 1)]
    if grain not in _ROLLING:
        raise TruthError(f"unsupported grain {grain!r}")
    out = []
    y, m = end.year, end.month
    for _ in range(_ROLLING[grain]):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return sorted(out)


def period_start(period_end: date | str, grain: str = "month") -> date:
    first = window_months(period_end, grain)[0]
    return date(int(first[:4]), int(first[5:7]), 1)


def _select(frame: pd.DataFrame, period_end: date | str, grain: str, scope: dict[str, Any] | None) -> pd.DataFrame:
    months = window_months(period_end, grain)
    mask = frame["month"].isin(months)
    for dim, value in sorted((scope or {}).items()):
        if dim not in DIMENSIONS:
            raise TruthError(f"unsupported scope dimension {dim!r}")
        if dim not in frame.columns:
            raise TruthError(f"scope dimension {dim!r} is not available in this source")
        values = list(value) if isinstance(value, (list, tuple, set)) else [value]
        mask &= frame[dim].isin([str(v) for v in values])
    return frame.loc[mask]


def _sum(series: pd.Series) -> float | None:
    value = series.sum(min_count=1)
    return None if pd.isna(value) else float(value)


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _source(data: PlatformData, source: str) -> pd.DataFrame:
    if source == "account_feed":
        return data.account
    if source == "panel":
        return data.panel
    raise TruthError(f"unknown source {source!r} (account_feed | panel)")


def trx(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
        measure: str = "units", source: str = "account_feed", data: PlatformData | None = None) -> float | None:
    """Total prescriptions."""
    column = {"units": "trx_units", "normalized": "trx_normalized"}.get(measure)
    if column is None:
        raise TruthError(f"unknown trx measure {measure!r}")
    frame = _select(_source(data or load_platform(), source), period_end, grain, scope)
    return _sum(frame[column])


def nrx(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
        source: str = "account_feed", data: PlatformData | None = None) -> float | None:
    """New prescriptions (first fills)."""
    return _sum(_select(_source(data or load_platform(), source), period_end, grain, scope)["nrx"])


def nbrx(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
         source: str = "account_feed", data: PlatformData | None = None) -> float | None:
    """New-to-brand prescriptions."""
    return _sum(_select(_source(data or load_platform(), source), period_end, grain, scope)["nbrx"])


def trx_share(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
              basket: str = "advanced_therapy", source: str = "account_feed",
              data: PlatformData | None = None) -> float | None:
    """Brand TRx / market TRx (ratio of sums)."""
    column = {"advanced_therapy": "advanced_therapy_trx", "il17_class": "il17_class_trx"}.get(basket)
    if column is None:
        raise TruthError(f"unknown basket {basket!r}")
    frame = _select(_source(data or load_platform(), source), period_end, grain, scope)
    return _ratio(_sum(frame["trx_units"]), _sum(frame[column]))


def calls(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
          data: PlatformData | None = None) -> float | None:
    """Details delivered (account feed only)."""
    return _sum(_select((data or load_platform()).account, period_end, grain, scope)["calls"])


def call_plan(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
              data: PlatformData | None = None) -> float | None:
    """Planned calls."""
    return _sum(_select((data or load_platform()).account, period_end, grain, scope)["call_plan"])


def call_attainment(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
                    aggregation: str = "ratio_of_sums", eligibility: str = "all_hcps",
                    data: PlatformData | None = None) -> float | None:
    """Calls / plan over account-months with a plan."""
    frame = _select((data or load_platform()).account, period_end, grain, scope)
    frame = frame.loc[frame["call_plan"] > 0]
    if eligibility == "writers_only":
        frame = frame.loc[frame["is_writer"] == 1]
    elif eligibility != "all_hcps":
        raise TruthError(f"unknown eligibility {eligibility!r}")
    if aggregation == "ratio_of_sums":
        return _ratio(_sum(frame["calls"]), _sum(frame["call_plan"]))
    if aggregation == "mean_of_ratios":
        return None if frame.empty else float((frame["calls"] / frame["call_plan"]).mean())
    raise TruthError(f"unknown aggregation {aggregation!r}")


def samples(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
            data: PlatformData | None = None) -> float | None:
    """Sample units dropped."""
    return _sum(_select((data or load_platform()).account, period_end, grain, scope)["samples"])


def speaker_attendance(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
                       measure: str = "attendances", data: PlatformData | None = None) -> float | None:
    """measure: attendances (governed; an HCP attending twice counts twice) | distinct_hcps."""
    frame = _select((data or load_platform()).account, period_end, grain, scope)
    if measure == "attendances":
        return _sum(frame["speaker_attendance"])
    if measure == "distinct_hcps":
        return float(frame.loc[frame["speaker_attendance"] > 0, "account_id"].nunique())
    raise TruthError(f"unknown speaker attendance measure {measure!r}")


def new_writers(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
                data: PlatformData | None = None) -> float | None:
    """HCPs writing for the first time; None (undefined) when the window holds only the warm-up month."""
    return _sum(_select((data or load_platform()).account, period_end, grain, scope)["new_writers"])


def referrals_in(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
                 projection: str = "observed", data: PlatformData | None = None) -> float | None:
    """Incoming referrals."""
    observed = _sum(_select((data or load_platform()).referrals, period_end, grain, scope)["referrals_in"])
    if projection == "observed":
        return observed
    if projection == "projected":
        return None if observed is None else observed / REFERRAL_COVERAGE
    raise TruthError(f"unknown referral projection {projection!r}")


def active_referrers(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
                     data: PlatformData | None = None) -> float | None:
    """Active referrers (additive by synthetic construction)."""
    return _sum(_select((data or load_platform()).referrals, period_end, grain, scope)["active_referrers"])


def exfactory_units(period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None, *,
                    data: PlatformData | None = None) -> float | None:
    """Simulated restricted ex-factory feed: per month round(SUM(trx_units) x 1.04), summed over the window."""
    if scope:
        raise TruthError("exfactory_units has no scope dimensions")
    frame = _select((data or load_platform()).account, period_end, grain, None)
    if frame.empty:
        return None
    monthly = frame.groupby("month")["trx_units"].sum()
    return float(sum(round(v * EXFACTORY_FACTOR) for v in monthly))


KPI_FUNCTIONS: dict[str, Callable[..., float | None]] = {
    "trx": trx, "nrx": nrx, "nbrx": nbrx, "trx_share": trx_share, "calls": calls, "call_plan": call_plan,
    "call_attainment": call_attainment, "samples": samples, "speaker_attendance": speaker_attendance,
    "new_writers": new_writers, "referrals_in": referrals_in, "active_referrers": active_referrers,
    "exfactory_units": exfactory_units,
}


def kpi_value(kpi_id: str, period_end: date | str, grain: str = "month", scope: dict[str, Any] | None = None,
              *, data: PlatformData | None = None, **choices: str) -> float | None:
    """Truth value of one KPI under the given definition choices (governed defaults when omitted)."""
    fn = KPI_FUNCTIONS.get(kpi_id)
    if fn is None:
        raise TruthError(f"no platform truth for KPI {kpi_id!r} (report-only or unknown)")
    return fn(period_end, grain, scope, data=data, **choices)


def dimension_values(data: PlatformData | None = None) -> dict[str, set[str]]:
    """Distinct values per scope dimension in the account feed."""
    d = data or load_platform()
    return {dim: set(map(str, d.account[dim].dropna().unique())) for dim in DIMENSIONS}
