from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "kpi_evidence"
MODULES = sorted(
    [PACKAGE / name for name in ("cli.py", "platform.py", "review.py", "serving.py", "standardize.py", "settings.py")]
    + [PACKAGE / "store" / "databricks_store.py"]
    + list((PACKAGE / "extract").glob("*.py"))
    + list((PACKAGE / "eval").glob("*.py"))
)
REFERENCES = re.compile(r"DESIGN|NOTEBOOK\.md|DEPLOY\.md|docs/|\bsections? \d|\bmap \d+|\bR\d+\b|insight_harness")
BRITISH = re.compile(r"labell|neighbour|summaris|cancelled|minimis|licence|colour|behaviour|recognis", re.I)
ALLOWED = ("totalled",)


def _strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
    return out


def _names(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((node.lineno, node.name))
        elif isinstance(node, ast.Name):
            out.append((node.lineno, node.id))
        elif isinstance(node, ast.alias):
            out.append((getattr(node, "lineno", 0), node.name))
    return out


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.relative_to(PACKAGE).as_posix())
def test_strings_name_no_internal_documents(path):
    hits = [f"{line}: {text[:80]}" for line, text in _strings(path) if REFERENCES.search(text)]
    assert hits == []


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.relative_to(PACKAGE).as_posix())
def test_strings_and_names_use_american_spelling(path):
    texts = _strings(path) + _names(path)
    hits = [f"{line}: {text[:80]}" for line, text in texts
            if BRITISH.search(text) and not any(word in text for word in ALLOWED)]
    assert hits == []


def test_job_stage_help_names_no_document():
    from kpi_evidence import cli

    parser = cli.build_parser()
    help_text = parser.format_help()
    assert "Databricks job stage" in help_text
    assert not REFERENCES.search(help_text)


def test_labeled_number_finder_is_exported():
    from kpi_evidence.extract import _common

    assert callable(_common.find_labeled_numbers)
    assert not hasattr(_common, "find_labelled_numbers")
