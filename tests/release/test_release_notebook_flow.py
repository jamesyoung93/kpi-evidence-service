from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK = ROOT / "notebooks" / "check_reports.py"


def _cells() -> list[str]:
    return NOTEBOOK.read_text(encoding="utf-8").split("# COMMAND ----------")


def _code_cells() -> list[str]:
    out = []
    for cell in _cells():
        lines = [ln for ln in cell.strip().splitlines() if ln.strip() and ln.strip() != "# Databricks notebook source"]
        if lines and not all(ln.startswith("# MAGIC") for ln in lines):
            out.append(cell)
    return out


def _markdown() -> str:
    lines = NOTEBOOK.read_text(encoding="utf-8").splitlines()
    return " ".join(line[len("# MAGIC"):].strip() for line in lines if line.startswith("# MAGIC"))


class _Widgets:
    def __init__(self, values: dict[str, str]):
        self.values = dict(values)

    def text(self, name, default, label=None):
        self.values.setdefault(name, default)

    def dropdown(self, name, default, choices, label=None):
        self.values.setdefault(name, default)

    def get(self, name):
        return self.values[name]


class _Fs:
    def mkdirs(self, path):
        os.makedirs(path, exist_ok=True)


class _Library:
    def restartPython(self):
        return None


class _Dbutils:
    def __init__(self, values: dict[str, str]):
        self.widgets = _Widgets(values)
        self.fs = _Fs()
        self.library = _Library()


def test_no_cell_closes_the_check():
    code = "\n".join(_code_cells())
    assert "check.close()" not in code
    assert "save_html" in _code_cells()[-1]


def test_confirm_guidance_points_at_a_new_cell_and_the_run_cell():
    text = _markdown()
    assert 'check.confirm("deck.pptx"' in text
    assert "in a new cell" in text and "**Run the check** cell" in text
    cells = _cells()
    heading = next(i for i, c in enumerate(cells) if "## 3. Run the check" in c)
    assert "result = check.run(" in cells[heading + 1]


def test_header_says_state_lives_in_memory_unless_state_path_is_set():
    assert "lives in memory for this run only, unless you set `state_path`" in _markdown()


@pytest.mark.parametrize("value, expected", [
    ("/Volumes/c/s/v/kpi_evidence_1.0.0/", "/Volumes/c/s/v/kpi_evidence_1.0.0/requirements.txt"),
    ("/Volumes/c/s/v/kpi_evidence_1.0.0", "/Volumes/c/s/v/kpi_evidence_1.0.0/requirements.txt"),
    ("/Volumes/c/s/wheels/requirements.txt", "/Volumes/c/s/wheels/requirements.txt"),
    (" /Volumes/c/s/wheels/ ", "/Volumes/c/s/wheels/requirements.txt"),
])
def test_wheels_widget_takes_a_folder_or_a_requirements_file(value, expected):
    cell = next(c for c in _code_cells() if "wheels_requirements =" in c)
    ns = {"dbutils": _Dbutils({"wheels": value}), "print": lambda *a, **k: None}
    exec(compile(cell, "<wheels cell>", "exec"), ns)
    assert ns["wheels_requirements"] == expected


def test_wheels_widget_default_is_a_folder():
    widgets = _Dbutils({})
    exec(compile(_code_cells()[0], "<widgets cell>", "exec"), {"dbutils": widgets})
    default = widgets.widgets.values["wheels"]
    assert default.endswith("/") and not default.endswith(".txt")


def test_run_all_then_confirm_and_run_the_check_cell_again(tmp_path):
    reports = ROOT / "samples" / "reports" / "kestrel-update.pptx"
    widgets = {"reports": str(reports), "kpis": "", "brand": "", "catalog": "main", "gov_schema": "kpi_gov",
               "out_dir": str(tmp_path / "out"), "ai_mode": "off", "wheels": "/Volumes/c/s/wheels/",
               "focus": "", "max_priority": "", "state_path": ""}
    ns = {"__name__": "__main__", "dbutils": _Dbutils(widgets), "display": lambda *a, **k: None,
          "displayHTML": lambda *a, **k: None, "spark": None}
    cells = _code_cells()
    for cell in cells:
        if "restartPython" in cell or "wheels_requirements" in cell:
            continue
        exec(compile(cell, "<cell>", "exec"), ns)
    first = ns["result"]
    assert "kestrel-update.pptx" in set(first.issues["report_file"])
    exec(compile('check.confirm("kestrel-update.pptx", brand="Kestrel", period="June 2026")', "<new cell>", "exec"),
         ns)
    run_cell = next(c for c in cells if "result = check.run(" in c)
    exec(compile(run_cell, "<run cell>", "exec"), ns)
    docs = ns["check"].documents
    row = docs[docs["file_name"] == "kestrel-update.pptx"].iloc[0]
    assert row["status"] == "PROCESSED" and row["period_label"] == "June 2026"
