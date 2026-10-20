from __future__ import annotations

from datetime import date

import pytest

from kpi_evidence.contracts import ValidationStatus as S
from kpi_evidence.review import decide
from kpi_evidence.serving import (COVERAGE_COLUMNS, HARNESS_COLUMNS, coverage, default_as_of, document_board,
                                  expected_periods, harness_published, harness_scope_key, published_current,
                                  status_counts)

MAR, APR, MAY = date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31)


def _cell(cov, kpi_id, end):
    rows = cov[(cov["kpi_id"] == kpi_id) & (cov["expected_period_end"] == end)]
    assert len(rows) == 1, (kpi_id, end)
    return rows.iloc[0]


@pytest.fixture()
def world(seeder, store, registry, settings):
    deck = seeder.doc("Kestrel_Monthly_Brand_Review_2026-06.pptx", period_end=MAY)
    trx = {end: seeder.seed("trx", S.VERIFIED, doc=deck, period_end=end, report_value=5900.0 + i,
                            platform_value=5900.0 + i)
           for i, end in enumerate((MAR, APR, MAY))}
    seeder.seed("trx", S.VARIANCE_REVIEW, doc=deck, period_end=MAY, scope={"region": "West"}, report_value=1300,
                platform_value=1265.7)
    decide(store, registry, settings, trx[MAY].result.result_id, "ACCEPT", "", "rev@example.com")
    seeder.seed("nrx", S.VARIANCE_REVIEW, doc=deck, period_end=date(2026, 2, 28))
    seeder.seed("exfactory_units", S.MISSING_ACCESS, doc=deck, period_end=MAY, platform_value=None)
    seeder.seed("samples", S.VARIANCE_REVIEW, doc=deck, period_end=MAY, report_value=1060, platform_value=1000)
    seeder.seed("brand_awareness", S.REPORT_ONLY, doc=deck, period_end=MAR, grain="quarter", unit="fraction",
                report_value=0.62, platform_value=None, period_label="Q1 2026")
    other = seeder.doc("Other.pptx", brand="OTHER")
    seeder.seed("active_referrers", S.VERIFIED, doc=other, brand="OTHER", period_end=MAY)
    return {"deck": deck, "trx": trx}


def test_expected_periods_monthly_and_quarterly():
    assert [p.end for p in expected_periods(date(2026, 6, 30), "monthly", 1)] == [MAR, APR, MAY]
    assert [p.label for p in expected_periods(date(2026, 6, 15), "monthly", 0)] == ["April 2026", "May 2026",
                                                                                     "June 2026"]
    quarters = expected_periods(date(2026, 6, 30), "quarterly", 1)
    assert [p.end for p in quarters] == [date(2025, 9, 30), date(2025, 12, 31), MAR]
    assert quarters[-1].label == "Q1 2026" and quarters[-1].start == date(2026, 1, 1)
    assert [p.end for p in expected_periods(date(2026, 8, 15), "quarterly", 1)][-1] == MAR
    assert [p.end for p in expected_periods(date(2026, 1, 31), "monthly", 1)] == [
        date(2025, 10, 31), date(2025, 11, 30), date(2025, 12, 31)]


def test_coverage_states(world, store, registry):
    cov = coverage(store, registry, "KESTREL", as_of=date(2026, 6, 30))
    assert list(cov.columns) == list(COVERAGE_COLUMNS)
    assert len(cov) == 3 * len(registry.goal_kpis("KESTREL"))
    may = _cell(cov, "trx", MAY)
    assert may["coverage_state"] == "FOUND" and bool(may["published"]) and may["scope_key"] == "{}"
    assert may["cell_label"] == "Matches platform" and may["validation_status"] == "VERIFIED"
    assert may["published_value"] == 5902.0 and may["platform_value"] == 5902.0
    mar = _cell(cov, "trx", MAR)
    assert mar["coverage_state"] == "FOUND" and not bool(mar["published"]) and mar["cell_label"] == "Matches platform"
    assert mar["file_name"] == "Kestrel_Monthly_Brand_Review_2026-06.pptx"
    for end in (MAR, APR, MAY):
        nrx = _cell(cov, "nrx", end)
        assert nrx["coverage_state"] == "STALE" and nrx["cell_label"] == "Stale"
        assert nrx["latest_period_found"] == date(2026, 2, 28)
    assert {_cell(cov, "exfactory_units", e)["coverage_state"] for e in (MAR, APR, MAY)} == {"NO_ACCESS"}
    assert _cell(cov, "exfactory_units", MAY)["cell_label"] == "No access"
    samples = _cell(cov, "samples", MAY)
    assert samples["coverage_state"] == "FOUND" and samples["cell_label"] == "Needs review"
    assert _cell(cov, "samples", MAR)["coverage_state"] == "NOT_FOUND"
    ref = _cell(cov, "active_referrers", MAY)
    assert ref["coverage_state"] == "NOT_FOUND" and ref["cell_label"] == "Not found"
    assert bool(ref["has_platform_spec"]) is registry.kpis["active_referrers"].has_platform_spec
    assert ref["owner"] == registry.kpis["active_referrers"].owner
    q1 = _cell(cov, "brand_awareness", MAR)
    assert q1["coverage_state"] == "FOUND" and q1["validation_status"] == "REPORT_ONLY"
    assert q1["expected_period_label"] == "Q1 2026" and q1["cell_label"] == "Captured (report only)"
    assert _cell(cov, "brand_awareness", date(2025, 12, 31))["coverage_state"] == "NOT_FOUND"
    assert not bool(_cell(cov, "brand_awareness", MAR)["has_platform_spec"])


def test_coverage_default_as_of_is_latest_document_period(world, seeder, store, registry):
    assert default_as_of(store, "KESTREL") == MAY
    cov = coverage(store, registry, "KESTREL")
    assert sorted(set(cov.loc[cov["cadence"] == "monthly", "expected_period_end"])) == [
        date(2026, 2, 28), MAR, APR]
    seeder.doc("Kestrel_Executive_Summary_Q2-2026.pdf", period_end=date(2026, 6, 30))
    cov = coverage(store, registry, "KESTREL")
    assert sorted(set(cov.loc[cov["cadence"] == "monthly", "expected_period_end"])) == [MAR, APR, MAY]


def test_default_as_of_falls_back_to_today(store):
    assert default_as_of(store, "NOBODY") is not None


def test_coverage_result_without_brand_inherits_document_brand(seeder, store, registry):
    deck = seeder.doc("deck.pptx")
    seeder.seed("nbrx", S.VERIFIED, doc=deck, brand=None, period_end=MAY)
    cov = coverage(store, registry, "KESTREL", as_of=date(2026, 6, 30))
    assert _cell(cov, "nbrx", MAY)["coverage_state"] == "FOUND"


def test_document_board_and_status_counts(world, store, registry, settings):
    deck = world["deck"]
    board = document_board(store, deck.doc_id, registry)
    assert len(board) == 8
    assert list(board["status"])[:3] == ["VERIFIED"] * 3 and board.iloc[-1]["status"] == "MISSING_ACCESS"
    trx_may = board[board["result_id"] == world["trx"][MAY].result.result_id].iloc[0]
    assert bool(trx_may["decided"]) and bool(trx_may["published"]) and trx_may["status_label"] == "Matches platform"
    assert trx_may["group"] == "GOOD" and trx_may["kpi_label"] == registry.kpis["trx"].label
    assert status_counts(store, deck.doc_id) == {"GOOD": 4, "REVIEW": 3, "BLOCKED": 1}
    assert status_counts(store, brand="OTHER") == {"GOOD": 1, "REVIEW": 0, "BLOCKED": 0}
    assert status_counts(store) == {"GOOD": 5, "REVIEW": 3, "BLOCKED": 1}
    assert document_board(store, "missing").empty


def test_harness_published_frame(seeder, store, registry, settings):
    west = seeder.seed("trx", S.VERIFIED, scope={"region": "West"}, report_value=1265.7, platform_value=1265.7)
    decide(store, registry, settings, west.result.result_id, "ACCEPT", "", "rev@example.com")
    frame = harness_published(store, registry, "KESTREL")
    assert list(frame.columns) == list(HARNESS_COLUMNS)
    row = frame.iloc[0]
    assert row["scope_key"] == '{"region": "West"}'
    assert row["period_start"] == "2026-05" and row["period_end"] == "2026-05"
    assert (row["metric_id"], row["variant_id"], row["platform_source"]) == ("trx", "units", "source_a")
    assert row["review_decision"] == "accept" and row["aggregation"] == "sum"
    assert row["tolerance_rel"] == 0.005 and row["display_precision"] == 1.0 and row["publication_seq"] == 1
    assert row["display_label"] == "Matches platform"
    assert harness_published(store, registry, "NOBODY").empty
    assert len(published_current(store, "KESTREL")) == 1


def test_harness_scope_key_collapses_the_geography_hierarchy():
    assert harness_scope_key("{}") == "{}"
    assert harness_scope_key('{"district":"D1","region":"West"}') == '{"district": "D1"}'
    assert harness_scope_key('{"district":"D1","region":"West","territory":"T7"}') == '{"territory": "T7"}'
    assert harness_scope_key('{"payer_channel":"Commercial","region":"West"}') == \
        '{"payer_channel": "Commercial", "region": "West"}'
