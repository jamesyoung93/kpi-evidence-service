from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from check_standins import NOTEBOOK_KPI_ROWS, REPORTS, FakeSparkSession
from kpi_evidence import platform as platform_module
from kpi_evidence import report as report_module
from kpi_evidence.check import ReportCheck, example_kpi_table_path
from kpi_evidence.contracts import KpiResult
from kpi_evidence.hints import load_kpi_table
from kpi_evidence.reconcile import OUTSIDE_FOCUS
from kpi_evidence.store import state_file

DECK = REPORTS / "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx"
SUBSET = ("trx", "nrx", "calls", "samples", "referrals_in", "brand_awareness")


class _Counting:
    def __init__(self, inner, calls):
        self.inner, self.calls = inner, calls

    def run(self, sql):
        self.calls.append(sql)
        return self.inner.run(sql)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _count(check: ReportCheck) -> list[str]:
    calls: list[str] = []
    original = check.platform.runner
    check.platform.runner = lambda: _Counting(original(), calls)
    return calls


def _kpi_rows(**changes: dict[str, str]) -> pd.DataFrame:
    frame = load_kpi_table(example_kpi_table_path())
    frame = frame[frame["kpi"].isin(SUBSET)].reset_index(drop=True)
    for kpi, values in changes.items():
        for column, value in values.items():
            frame.loc[frame["kpi"] == kpi, column] = value
    return frame


def _check(state_path: Path | None = None, kpis: pd.DataFrame | None = None, **kwargs) -> ReportCheck:
    check = ReportCheck(load_demo_platform=True, state_path=state_path, **kwargs)
    check.add_reports(DECK)
    check.set_kpis(_kpi_rows() if kpis is None else kpis, use_registry=False)
    return check


def _values(result) -> pd.DataFrame:
    return result.comparisons.set_index("result_id")[["status", "report_value", "platform_value"]].sort_index()


def _computed(result) -> pd.DataFrame:
    rows = result.comparisons
    return rows[rows["platform_value"].notna() | rows["alternative_value"].notna()]


@pytest.fixture(scope="module")
def first_session(tmp_path_factory):
    state = tmp_path_factory.mktemp("volume") / "kpi_state.duckdb"
    check = _check(state)
    calls = _count(check)
    result = check.run()
    check.close()
    return state, result, calls


def test_second_session_reuses_everything(first_session):
    state, first, first_calls = first_session
    assert state.is_file() and not state.with_name(state.name + ".tmp").exists()
    assert first_calls and first.summary["reused"] == 0 and not first.comparisons["reused"].any()
    with ReportCheck(load_demo_platform=True, state_path=state) as check:
        check.add_reports(DECK)
        check.set_kpis(_kpi_rows(), use_registry=False)
        calls = _count(check)
        second = check.run()
        history = check.history()
    assert calls == []
    pd.testing.assert_frame_equal(_values(first), _values(second))
    computed = _computed(second)
    assert len(computed) > 10 and computed["reused"].all()
    assert second.summary["reused"] == int(second.comparisons["reused"].sum()) == len(computed)
    assert list(history.columns) == ["run_id", "started", "documents", "comparisons", "reused", "focus"]
    assert len(history) >= 2 and history.iloc[-1]["run_id"] == second.summary["run_id"]
    assert history.iloc[-1]["reused"] == len(computed)
    assert second.changes_since().empty
    assert "reused from earlier runs" in second.to_html() and 'class="chip info" title="Taken from an earlier' in \
        second.to_html()


def test_changed_kpi_row_recomputes_only_that_kpi(first_session, tmp_path):
    state, first, _ = first_session
    copy = tmp_path / "state.duckdb"
    copy.write_bytes(state.read_bytes())
    with _check(copy, _kpi_rows(nrx={"tolerance": "1%"})) as check:
        calls = _count(check)
        result = check.run()
    computed = _computed(result)
    assert calls
    assert set(computed.loc[~computed["reused"], "kpi_id"]) == {"nrx"}
    assert computed.loc[computed["kpi_id"] != "nrx", "reused"].all()


def test_changed_platform_data_recomputes_the_affected_kpis(first_session, tmp_path, monkeypatch):
    state, _first, _ = first_session
    copy = tmp_path / "state.duckdb"
    copy.write_bytes(state.read_bytes())
    original = platform_module.demo_platform_frames

    def changed_frames(settings=None, data_dir=None):
        frames = {k: v.copy() for k, v in original(settings, data_dir).items()}
        referrals = frames["referrals_monthly"]
        referrals.loc[referrals.index[-1], "referrals_in"] = referrals["referrals_in"].iloc[-1] + 7
        return frames

    monkeypatch.setattr(platform_module, "demo_platform_frames", changed_frames)
    with _check(copy) as check:
        calls = _count(check)
        result = check.run()
    computed = _computed(result)
    assert calls and all("referrals_monthly" in sql for sql in calls)
    assert set(computed.loc[~computed["reused"], "kpi_id"]) == {"referrals_in"}


def test_failed_run_keeps_the_previous_state_file(first_session, tmp_path, monkeypatch):
    state, _first, _ = first_session
    copy = tmp_path / "state.duckdb"
    copy.write_bytes(state.read_bytes())
    before = copy.read_bytes()
    check = _check(copy)

    def broken(*args, **kwargs):
        raise RuntimeError("report failed")

    monkeypatch.setattr(report_module, "build_result", broken)
    with pytest.raises(RuntimeError, match="report failed"):
        check.run()
    check.close()
    assert copy.read_bytes() == before
    assert not copy.with_name(copy.name + ".tmp").exists()


def test_failed_copy_out_keeps_the_previous_state_file(tmp_path, monkeypatch):
    target = tmp_path / "state.duckdb"
    target.write_bytes(b"previous state")
    local = tmp_path / "local.duckdb"
    local.write_bytes(b"new state")
    real_copy = state_file.shutil.copyfile

    def failing_copy(src, dst, *args, **kwargs):
        real_copy(src, dst)
        raise OSError("disk full")

    monkeypatch.setattr(state_file.shutil, "copyfile", failing_copy)
    with pytest.raises(OSError, match="disk full"):
        state_file.push_state(local, target)
    assert target.read_bytes() == b"previous state"
    assert not (tmp_path / "state.duckdb.tmp").exists()


def test_state_path_on_a_volume_folder_uses_sequential_copies(tmp_path, monkeypatch):
    volume = tmp_path / "Volumes" / "main" / "brand" / "state"
    state = volume / "kpi_state.duckdb"
    events: list[tuple[str, str, str]] = []
    real_copy, real_replace = state_file.shutil.copyfile, state_file.os.replace

    def copy(src, dst, *args, **kwargs):
        events.append(("copy", Path(src).name, Path(dst).name))
        return real_copy(src, dst)

    def no_rename(src, dst):
        events.append(("rename", Path(src).name, Path(dst).name))
        raise OSError("rename is not supported on this volume")

    monkeypatch.setattr(state_file.shutil, "copyfile", copy)
    monkeypatch.setattr(state_file.os, "replace", no_rename)
    check = ReportCheck(load_demo_platform=True, state_path=state)
    assert Path(check.store.path).parent != volume and not volume.exists()
    check.add_reports(DECK)
    assert check.save_state() == str(state)
    assert events == [("copy", "service.duckdb", "kpi_state.duckdb.tmp"),
                      ("rename", "kpi_state.duckdb.tmp", "kpi_state.duckdb"),
                      ("copy", "kpi_state.duckdb.tmp", "kpi_state.duckdb")]
    assert state.is_file() and not (volume / "kpi_state.duckdb.tmp").exists()
    check.close()
    events.clear()
    again = ReportCheck(load_demo_platform=True, state_path=f"{state}")
    assert events == [("copy", "kpi_state.duckdb", "service.duckdb")]
    assert len(again.store.read(KpiResult)) == 0 and again.store.query(
        "SELECT COUNT(*) AS n FROM kpi_evidence.documents").iloc[0, 0] == 1
    again.close(save=False)
    assert state_file.state_location("dbfs:/Volumes/main/brand/state/x.duckdb") == Path("/Volumes/main/brand/state/x.duckdb")


def test_state_path_and_persist_are_exclusive(tmp_path):
    with pytest.raises(ValueError, match="not both"):
        ReportCheck(persist=True, state_path=tmp_path / "x.duckdb")


def test_closing_before_any_run_with_a_new_state_path(tmp_path):
    spark = FakeSparkSession(tmp_path, governed_views=False)
    state = tmp_path / "Volumes" / "main" / "brand" / "state" / "new.duckdb"
    check = ReportCheck(spark, state_path=state)
    check.close()
    with ReportCheck(spark, state_path=state) as again:
        again.add_reports(DECK)
        again.set_kpis(pd.DataFrame(NOTEBOOK_KPI_ROWS))
        again.run(materialize=False)
    assert state.is_file()


@pytest.fixture(scope="module")
def focused():
    check = _check()
    full = check.run()
    trx = check.run(focus=["trx"])
    return check, full, trx


def test_focus_by_id_compares_only_that_kpi(focused):
    check, full, trx = focused
    assert set(trx.comparisons["kpi_id"].dropna()) == {"trx"}
    assert list(trx.kpis["kpi_id"]) == ["trx"] and trx.summary["focus"] == ["trx"]
    outside = trx.extraction[~trx.extraction["in_focus"]]
    assert len(outside) and set(outside["reason"]) == {OUTSIDE_FOCUS}
    assert "nrx" in set(outside["kpi_id"])
    assert trx.extraction.loc[trx.extraction["in_focus"], "reason"].notna().any()
    assert "Focus" in trx.to_html()


def test_focused_run_keeps_the_other_results_current(focused):
    check, full, _trx = focused
    current = check.store.read(KpiResult, "kpi_id = 'nrx' AND is_current = TRUE")
    assert {r.result_id for r in current} == set(full.comparisons.loc[full.comparisons["kpi_id"] == "nrx", "result_id"])
    again = check.run()
    pd.testing.assert_frame_equal(_values(full), _values(again))


def test_focus_by_name_alias_and_priority(focused):
    check, _full, _trx = focused
    assert check.focus_kpis("Total prescriptions") == {"trx"}
    assert check.focus_kpis(["New prescriptions", "Samples dropped"]) == {"nrx", "samples"}
    assert check.focus_kpis(max_priority=1) == {"trx", "nrx"}
    assert check.focus_kpis(["referrals_in"], max_priority=1) == {"trx", "nrx", "referrals_in"}
    with pytest.raises(ValueError, match="unknown KPI"):
        check.focus_kpis("trxx")
    result = check.run(max_priority=1)
    assert set(result.comparisons["kpi_id"].dropna()) == {"trx", "nrx"}


def test_reports_sort_by_priority_first(focused):
    _check_, full, _trx = focused
    priorities = full.kpis["priority"]
    ranked = [p for p in priorities.tolist() if not pd.isna(p)]
    assert ranked == sorted(ranked) and priorities.isna().tolist() == sorted(priorities.isna().tolist())
    first_rows = full.comparisons.head(5)["kpi_id"]
    assert set(first_rows) <= {"trx", "nrx"}


def test_demo_focus_runs_fewer_platform_queries():
    full = ReportCheck.demo()
    full_calls = _count(full)
    everything = full.run()
    focused_check = ReportCheck.demo()
    focus_calls = _count(focused_check)
    trx = focused_check.run(focus=["trx"])
    assert set(trx.comparisons["kpi_id"].dropna()) == {"trx"}
    assert 0 < len(focus_calls) < len(full_calls) / 3
    assert len(trx.comparisons) < len(everything.comparisons)


def test_check_messages_are_part_of_the_report(tmp_path):
    check = _check()
    check.add_reports(REPORTS / "kestrel-update.pptx", period="June 2026", brand="KESTREL")
    check._entries[next(e.doc_id for e in check._entries.values() if e.file_name == "kestrel-update.pptx")
                   ].overrides["period_grain"] = "fortnight"
    result = check.run()
    rows = result.issues[result.issues["category"] == "metadata not applied"]
    assert len(rows) == 1 and rows.iloc[0]["severity"] == "error"
    assert result.summary["issues"] == len(result.issues)
    assert "metadata not applied" in result.to_html()


def test_notebook_mode_reuses_saved_results_by_delta_version(tmp_path):
    versions = {"main.platform_demo.rx_account_monthly": 3}
    spark = FakeSparkSession(tmp_path, governed_views=False, delta_versions=versions)
    state = tmp_path / "Volumes" / "main" / "brand" / "state" / "kpi_check.duckdb"

    def session():
        check = ReportCheck(spark, state_path=state)
        check.add_reports(DECK)
        check.set_kpis(pd.DataFrame(NOTEBOOK_KPI_ROWS))
        return check

    def kpi_sql(start: int) -> list[str]:
        return [s for s in spark.statements[start:] if "SUM(" in s]

    with session() as check:
        first = check.run(materialize=False)
    assert kpi_sql(0) and not first.comparisons["reused"].any()
    mark = len(spark.statements)
    with session() as check:
        second = check.run(materialize=False)
        assert len(check.history()) == 2
    assert kpi_sql(mark) == []
    assert any(s == "DESCRIBE HISTORY main.platform_demo.rx_account_monthly LIMIT 1" for s in spark.statements[mark:])
    pd.testing.assert_frame_equal(_values(first), _values(second))
    assert _computed(second)["reused"].all()
    versions["main.platform_demo.rx_account_monthly"] = 4
    mark = len(spark.statements)
    with session() as check:
        third = check.run(materialize=False)
    assert kpi_sql(mark) and not _computed(third)["reused"].any()
