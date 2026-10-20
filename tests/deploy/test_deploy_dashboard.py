from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kpi_evidence.contracts import STATUS_INFO, StatusGroup, ValidationStatus

ROOT = Path(__file__).resolve().parents[2]
DASH_PATH = ROOT / "src" / "dashboards" / "kpi_coverage.lvdash.json"
GROUP_NAMES = {StatusGroup.GOOD: "Good", StatusGroup.REVIEW: "Needs review", StatusGroup.BLOCKED: "Blocked"}
_FIELD_COL = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


@pytest.fixture(scope="module")
def dash() -> dict:
    return json.loads(DASH_PATH.read_text(encoding="utf-8"))


def _datasets(dash) -> dict[str, str]:
    return {d["name"]: "".join(d["queryLines"]) for d in dash["datasets"]}


def _widgets(dash):
    for page in dash["pages"]:
        for item in page["layout"]:
            yield page, item


def test_structure(dash):
    assert set(dash) == {"datasets", "pages"}
    assert len(dash["pages"]) == 1
    names = [d["name"] for d in dash["datasets"]]
    assert len(names) == len(set(names))
    for d in dash["datasets"]:
        assert d["displayName"] and d["queryLines"]
        assert "${" not in "".join(d["queryLines"]), "no bundle placeholders inside the dashboard file"
    sql = " ".join(_datasets(dash).values())
    for table in ("published_kpis_current", "kpi_results_current", "review_open", "dq_results"):
        assert re.search(rf"FROM {table}\b", sql), table
    kinds = {item["widget"].get("spec", {}).get("widgetType") for _p, item in _widgets(dash)}
    assert {"counter", "bar", "table"} <= kinds


def test_layout_fits_the_six_column_grid_without_overlap(dash):
    cells = set()
    for _page, item in _widgets(dash):
        pos = item["position"]
        assert pos["x"] >= 0 and pos["width"] >= 1 and pos["x"] + pos["width"] <= 6, item["widget"]["name"]
        for x in range(pos["x"], pos["x"] + pos["width"]):
            for y in range(pos["y"], pos["y"] + pos["height"]):
                assert (x, y) not in cells, f"{item['widget']['name']} overlaps at {(x, y)}"
                cells.add((x, y))
    names = [item["widget"]["name"] for _p, item in _widgets(dash)]
    assert len(names) == len(set(names))


def test_widget_encodings_use_declared_fields(dash):
    datasets = _datasets(dash)
    for _page, item in _widgets(dash):
        widget = item["widget"]
        if "multilineTextboxSpec" in widget:
            continue
        (query,) = widget["queries"]
        q = query["query"]
        assert q["datasetName"] in datasets, widget["name"]
        fields = {f["name"] for f in q["fields"]}
        enc = widget["spec"]["encodings"]
        used = [c["fieldName"] for c in enc["columns"]] if "columns" in enc else [e["fieldName"] for e in enc.values()]
        assert set(used) <= fields, f"{widget['name']}: {set(used) - fields}"
        assert widget["spec"]["frame"]["title"]


def _run(store, sql):
    store.execute(f"USE {store.settings.schema}")
    return store.query(sql)


def test_datasets_run_and_expose_the_columns_widgets_read(dash, view_store):
    datasets = _datasets(dash)
    cols = {name: set(_run(view_store, sql).columns) for name, sql in datasets.items()}
    for _page, item in _widgets(dash):
        widget = item["widget"]
        for query in widget.get("queries", []):
            q = query["query"]
            for f in q["fields"]:
                for col in _FIELD_COL.findall(f["expression"]):
                    if col != "*":
                        assert col in cols[q["datasetName"]], f"{widget['name']}: {col} not in {q['datasetName']}"


def test_status_counts_use_plain_group_names(dash, view_store, kit):
    store = view_store
    store.insert([kit.make_result(result_id=f"r{i}", observation_id=f"o{i}", validation_status=s)
                  for i, s in enumerate(ValidationStatus)])
    df = _run(store, _datasets(dash)["ds_results_by_status"])
    assert int(df["results"].sum()) == len(ValidationStatus)
    by_label = {row.status_label: row.group_name for row in df.itertuples()}
    for status, info in STATUS_INFO.items():
        assert by_label[info.label] == GROUP_NAMES[info.group], status


def test_review_and_dq_datasets(dash, view_store, kit):
    from datetime import datetime

    from kpi_evidence.contracts import DqResult

    store = view_store
    store.insert([kit.make_result(result_id="rv", observation_id="ov", validation_status=ValidationStatus.VARIANCE_REVIEW),
                  kit.make_result(result_id="ok", observation_id="ok", validation_status=ValidationStatus.VERIFIED)])
    review = _run(store, _datasets(dash)["ds_review_open"])
    assert list(review["status_label"]) == ["Variance to review"]
    old, new = datetime(2026, 6, 1, 5, 30), datetime(2026, 6, 2, 5, 30)
    store.insert([
        DqResult("run1", "gv_rx_account_monthly", "unique_grain", "hard", False, 3, "dupes", old),
        DqResult("run2", "gv_rx_account_monthly", "unique_grain", "hard", True, 0, "", new),
        DqResult("run2", "gv_rx_panel_monthly", "fresh", "soft", False, 1, "late", new),
        DqResult("run2", "gv_referrals_monthly", "complete_months", "hard", False, 2, "gap", new),
    ])
    dq = _run(store, _datasets(dash)["ds_dq_latest"])
    assert len(dq) == 3
    assert int(dq["hard_failure"].sum()) == 1
