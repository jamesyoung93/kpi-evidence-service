from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK = ROOT / "notebooks" / "check_reports.py"


def _markdown() -> str:
    lines = NOTEBOOK.read_text(encoding="utf-8").splitlines()
    return " ".join(line[len("# MAGIC"):].strip() for line in lines if line.startswith("# MAGIC"))


def test_unreadable_metadata_text_matches_the_behavior():
    text = _markdown()
    assert "are still checked with the best guess" not in text
    assert "best guess when there is one" in text
    assert re.search(r"no guess at all, the report is not compared and is listed under issues", text)
    assert "awaiting confirmation" in text and 'check.confirm("deck.pptx"' in text


def test_install_text_points_at_files_a_release_folder_ships():
    text = _markdown()
    assert "INSTALL.md" in text and "requirements.txt" in text
    assert "scripts/publish_wheels.py" not in text and ".bundle/kpi-evidence-service" not in text
    assert "kpi_wheels" in text


def test_notebook_saves_the_sql_files_and_still_compiles():
    source = NOTEBOOK.read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("# MAGIC %"))
    ast.parse(code)
    assert "result.save_sql(" in source and "platform series" in _markdown()
