"""Focused contracts for glanceable Home tiles."""
from __future__ import annotations

import math
from types import SimpleNamespace

import pandas as pd
import pytest

from views import common, home


def test_sparkline_domain_follows_all_visible_series_with_padding():
    frame = pd.DataFrame({
        "month": ["2026-01", "2026-02", "2026-03"],
        "primary": [98.0, 100.0, 102.0],
        "reference": [97.0, 99.0, float("nan")],
    })

    assert home._sparkline_domain(frame) == pytest.approx([96.6, 102.4])


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([0.0, 0.0], [-1.0, 1.0]),
        ([0.10, 0.10], [0.092, 0.108]),
        ([-12.0, -10.0], [-12.16, -9.84]),
    ],
)
def test_sparkline_domain_handles_flat_zero_ratio_and_negative_series(values, expected):
    frame = pd.DataFrame({"month": ["2026-01", "2026-02"], "primary": values})

    assert home._sparkline_domain(frame) == pytest.approx(expected)


def test_sparkline_domain_ignores_nan_and_infinity_and_refuses_no_finite_data():
    partial = pd.DataFrame({
        "month": ["2026-01", "2026-02", "2026-03"],
        "primary": [math.nan, math.inf, 4.0],
    })
    empty = pd.DataFrame({"month": ["2026-01"], "primary": [math.nan]})

    assert home._sparkline_domain(partial) == pytest.approx([3.68, 4.32])
    assert home._sparkline_domain(empty) is None


def test_sparkline_chart_has_explicit_domain_no_points_and_no_legend():
    frame = pd.DataFrame({
        "month": ["2026-01", "2026-02"],
        "primary": [100.0, 102.0],
        "reference": [99.0, 101.0],
    })

    spec = home._sparkline_chart(frame).to_dict()

    assert spec["mark"]["point"] is False
    assert spec["encoding"]["color"]["legend"] is None
    assert spec["encoding"]["y"]["scale"]["zero"] is False
    assert spec["encoding"]["y"]["scale"]["nice"] is False
    assert spec["encoding"]["y"]["scale"]["domain"] == pytest.approx([98.76, 102.24])


def test_expanded_answer_chart_uses_same_explicit_visible_data_domain():
    frame = pd.DataFrame({
        "month": ["2026-01", "2026-02"],
        "primary": [100.0, 102.0],
        "reference": [99.0, 101.0],
    })

    spec = common._line_chart_spec(frame).to_dict()

    assert spec["mark"]["point"] is False
    assert spec["encoding"]["y"]["scale"]["zero"] is False
    assert spec["encoding"]["y"]["scale"]["nice"] is False
    assert spec["encoding"]["y"]["scale"]["domain"] == pytest.approx([98.76, 102.24])


def test_zero_baseline_count_tile_uses_native_delta_instead_of_blank_delta():
    art = SimpleNamespace(
        resolution=SimpleNamespace(metric="new_writers", variant="strict"),
        extras={"comparison": {
            "available": True,
            "reference_value": 0.0,
            "delta": 2.0,
            "delta_pct": None,
            "basis_label": "vs same month last year",
        }},
    )

    assert home._format_delta(art) == "+2.0 vs same month last year"
