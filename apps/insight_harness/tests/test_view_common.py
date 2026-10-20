"""Unit formatting checks for governed display semantics."""
from types import SimpleNamespace

from views import common


def _artifact(metric: str, variant: str):
    return SimpleNamespace(resolution=SimpleNamespace(metric=metric, variant=variant))


def test_ratio_values_and_movements_render_as_percent_and_percentage_points():
    art = _artifact("call_attainment", "ratio")
    assert common.format_artifact_value(art, 0.8) == "80.0%"
    assert common.format_comparison_delta(
        art, {"available": True, "delta_pp": 2.5}) == "+2.5 pp"
    assert common.format_native_delta("call_attainment", "ratio", 0.025) == "+2.5 pp"


def test_additive_and_currency_values_keep_native_units():
    assert common.format_metric_value("trx", "units", 1234) == "1,234.0"
    assert common.format_metric_value("trx", "dollars", 1234) == "$1,234.0"
    assert common.format_native_delta("trx", "units", -12.5) == "-12.5"
    assert common.format_native_delta("trx", "dollars", 12.5) == "+$12.5"
