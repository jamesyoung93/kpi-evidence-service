from datetime import date

import pytest

from kpi_evidence.periods import parse_period

REFERENCE = date(2026, 5, 31)


@pytest.mark.parametrize("text, grain, start", [
    ("1M", "month", date(2026, 5, 1)),
    ("3M", "r3m", date(2026, 3, 1)),
    ("6 M", "r6m", date(2025, 12, 1)),
    ("12M", "r12m", date(2025, 6, 1)),
    ("3m", "r3m", date(2026, 3, 1)),
])
def test_bare_window_headers_resolve_against_the_reference(text, grain, start):
    p = parse_period(text, reference=REFERENCE)
    assert p is not None and p.grain == grain and p.start == start and p.end == REFERENCE


@pytest.mark.parametrize("text", ["24M", "2M", "12M sales"])
def test_other_month_counts_are_not_windows(text):
    p = parse_period(text, reference=REFERENCE)
    assert p is None or p.grain not in ("r3m", "r6m", "r12m")


def test_bare_window_needs_a_reference():
    assert parse_period("3M") is None
