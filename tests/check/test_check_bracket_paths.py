from __future__ import annotations

import shutil

import pytest

from check_standins import REPORTS
from kpi_evidence.check import ReportCheck

DECK = REPORTS / "Kestrel_Monthly_Brand_Review_2026-06_v2.pptx"


@pytest.fixture()
def bracket_folder(tmp_path):
    folder = tmp_path / "Reports [Q2 2026]"
    folder.mkdir()
    shutil.copyfile(DECK, folder / "Deck [final].pptx")
    return folder


@pytest.mark.parametrize("target", ["folder", "file", "glob"])
def test_existing_paths_with_brackets_are_added(bracket_folder, target):
    path = {"folder": bracket_folder, "file": bracket_folder / "Deck [final].pptx",
            "glob": bracket_folder / "*.pptx"}[target]
    rows = ReportCheck().add_reports(str(path))
    assert rows["file_name"].tolist() == ["Deck [final].pptx"]


def test_a_missing_bracket_path_is_still_reported(bracket_folder):
    with pytest.raises(FileNotFoundError):
        ReportCheck().add_reports(str(bracket_folder / "missing [x].pptx"))
