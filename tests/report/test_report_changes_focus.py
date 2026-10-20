from __future__ import annotations

import copy
import dataclasses

import pytest

from kpi_evidence.check import run_record


@pytest.fixture()
def runs(report_result):
    comps = report_result.comparisons
    full = run_record(comps)
    trx = run_record(comps[comps["kpi_id"] == "trx"])
    assert trx and len(trx) < len(full)
    return full, trx


def _result(report_result, previous, focus=None):
    summary = {**report_result.summary, "focus": focus}
    return dataclasses.replace(report_result, summary=summary, _previous=previous)


def test_unfocused_run_after_a_focused_run_reports_no_changes(report_result, runs):
    full, trx = runs
    previous = [{"run_id": "focused", "focus": ["trx"], "results": trx},
                {"run_id": "full", "focus": None, "results": full}]
    result = _result(report_result, previous)
    assert result.changes_since().empty
    assert result.changes_since("full").empty


def test_changes_outside_the_focus_name_the_run_they_are_compared_with(report_result, runs):
    full, trx = runs
    older = copy.deepcopy(full)
    other = [rid for rid, row in older.items() if row["kpi_id"] not in (None, "trx")
             and row["platform_value"] is not None]
    changed, removed = other[0], "gone-result"
    older[changed]["platform_value"] = float(older[changed]["platform_value"]) + 1000.0
    older[removed] = {**older[other[1]], "kpi_id": older[other[1]]["kpi_id"]}
    previous = [{"run_id": "focused", "focus": ["trx"], "results": trx},
                {"run_id": "full", "focus": None, "results": older}]
    out = _result(report_result, previous).changes_since()
    assert sorted(out["change"]) == ["changed", "removed"]
    by = out.set_index("change")
    assert by.loc["changed", "result_id"] == changed and by.loc["changed", "previous_run_id"] == "full"
    assert by.loc["removed", "result_id"] == removed and by.loc["removed", "previous_run_id"] == "full"


def test_kpis_no_earlier_run_covered_are_new(report_result, runs):
    _full, trx = runs
    previous = [{"run_id": "focused", "focus": ["trx"], "results": trx}]
    out = _result(report_result, previous).changes_since()
    assert set(out["change"]) == {"new"}
    assert len(out) == len(report_result.comparisons) - len(trx)
    assert set(out["previous_run_id"]) == {"focused"}


def test_older_focused_runs_fill_in_their_own_kpis(report_result, runs):
    full, trx = runs
    nrx = {rid: row for rid, row in full.items() if row["kpi_id"] == "nrx"}
    rest = {rid: row for rid, row in full.items() if row["kpi_id"] not in ("trx", "nrx")}
    assert nrx and rest
    previous = [{"run_id": "r3", "focus": ["trx"], "results": trx},
                {"run_id": "r2", "focus": ["nrx"], "results": nrx},
                {"run_id": "r1", "focus": None, "results": {**rest, **copy.deepcopy(nrx)}}]
    for row in previous[2]["results"].values():
        if row["kpi_id"] == "nrx":
            row["status"] = "OUTDATED"
    assert _result(report_result, previous).changes_since().empty


def test_a_focused_run_still_ignores_rows_outside_its_focus(report_result, runs):
    full, trx = runs
    comps = report_result.comparisons
    focused = dataclasses.replace(report_result, comparisons=comps[comps["kpi_id"] == "trx"].reset_index(drop=True),
                                  summary={**report_result.summary, "focus": ["trx"]},
                                  _previous=[{"run_id": "full", "focus": None, "results": full}])
    assert focused.changes_since().empty
