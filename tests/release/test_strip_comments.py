from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def sc():
    spec = importlib.util.spec_from_file_location("kpi_release_strip_unit", ROOT / "scripts" / "strip_comments.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _strip(sc, source: str, **kwargs) -> str:
    out = sc.strip_python(source, **kwargs)
    sc.verify_python(source, out)
    return out


def test_hash_inside_string_is_kept(sc):
    assert _strip(sc, 'x = "a # b"  # trailing\ny = 1\n') == 'x = "a # b"\ny = 1\n'


def test_hash_inside_fstring_is_kept(sc):
    source = 'x = 1\ny = f"{x} # not a comment {x!r} {\'#\'}"  # c\n'
    assert _strip(sc, source) == 'x = 1\ny = f"{x} # not a comment {x!r} {\'#\'}"\n'


def test_escaped_quotes_are_respected(sc):
    source = "s = 'it\\'s # here'  # c\nt = \"say \\\"#\\\" now\"  # d\n"
    assert _strip(sc, source) == "s = 'it\\'s # here'\nt = \"say \\\"#\\\" now\"\n"


def test_triple_quoted_string_with_hash_lines_is_kept(sc):
    source = 'def f():\n    return """\n# kept\n\n\n\n-- also kept\n"""  # gone\n'
    out = _strip(sc, source)
    assert '"""\n# kept\n\n\n\n-- also kept\n"""' in out
    assert "gone" not in out


def test_full_line_comments_removed_and_blank_runs_collapsed(sc):
    source = "import os\n\n\n# one\n# two\n\n\n\nx = 1\n"
    assert _strip(sc, source) == "import os\n\n\nx = 1\n"


def test_comment_inside_brackets(sc):
    source = "values = [\n    1,  # first\n    # spacer\n    2,\n]\n"
    assert _strip(sc, source) == "values = [\n    1,\n    2,\n]\n"


def test_module_docstring_removed(sc):
    source = '"""Module text.\n\nMore text.\n"""\nfrom __future__ import annotations\n\nx = 1\n'
    assert _strip(sc, source) == "from __future__ import annotations\n\nx = 1\n"


def test_module_docstring_kept_short_when_doc_is_read(sc):
    source = '"""Tool title.\n\nUsage details.\n"""\nimport argparse\nparser = argparse.ArgumentParser(description=__doc__)\n'
    out = _strip(sc, source)
    assert out.startswith('"""Tool title."""\n')
    namespace: dict = {}
    exec(compile(out, "<t>", "exec"), namespace)
    assert namespace["parser"].description == "Tool title."


def test_public_docstring_reduced_to_first_sentence(sc):
    source = ('class Box:\n    """Holds things. Second sentence.\n\n    Details.\n    """\n\n'
              '    def put(self, item):\n        """Store one item, e.g. a value."""\n        return item\n')
    out = _strip(sc, source)
    assert '    """Holds things."""\n' in out
    assert '"""Store one item, e.g. a value."""' in out
    assert "Details" not in out


def test_private_docstring_removed(sc):
    source = 'def _helper(x):\n    """Private text."""\n\n    return x\n'
    assert _strip(sc, source) == "def _helper(x):\n    return x\n"


def test_docstring_only_body_gets_placeholder(sc):
    source = 'class _Marker(Exception):\n    """Only a docstring."""\n\n\ndef _noop():\n    """Nothing."""\n'
    out = _strip(sc, source)
    assert out == "class _Marker(Exception):\n    ...\n\n\ndef _noop():\n    ...\n"


def test_drop_docstrings_mode_removes_public_ones(sc):
    source = 'def test_x():\n    """Explains the test."""\n    assert True\n'
    assert _strip(sc, source, drop_docstrings=True) == "def test_x():\n    assert True\n"


def test_long_summary_is_cut_at_a_clause(sc):
    doc = ("Return the rows that match the filter; the filter is applied after every other step so callers "
           "see the full picture of what happened.")
    line = sc.summary_line(doc)
    assert line == "Return the rows that match the filter."
    assert len(line) <= 110


def test_summary_skips_abbreviations(sc):
    assert sc.summary_line("Accepts paths, e.g. a folder. Also bytes.") == "Accepts paths, e.g. a folder."


def test_notebook_markers_kept(sc):
    source = ("# Databricks notebook source\n# MAGIC %md\n# MAGIC # Title\n\n# COMMAND ----------\n\n"
              "# explain the next line\nx = 1  # inline\n\n# COMMAND ----------\n\n# MAGIC %pip install x\n")
    out = _strip(sc, source)
    assert out == ("# Databricks notebook source\n# MAGIC %md\n# MAGIC # Title\n\n# COMMAND ----------\n\n"
                   "x = 1\n\n# COMMAND ----------\n\n# MAGIC %pip install x\n")
    assert sc.remaining_comments(out) == []


def test_magic_lines_are_comments_outside_notebooks(sc):
    assert _strip(sc, "# MAGIC %md\nx = 1\n") == "x = 1\n"


def test_shebang_coding_and_type_ignore_kept(sc):
    source = "#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n# note\nimport os  # type: ignore[import]  # why\n"
    assert _strip(sc, source) == "#!/usr/bin/env python\n# -*- coding: utf-8 -*-\nimport os  # type: ignore[import]\n"


def test_crlf_line_endings_preserved(sc):
    source = "x = 1  # c\r\n# full\r\ny = 2\r\n"
    assert _strip(sc, source) == "x = 1\r\ny = 2\r\n"


def test_rewrite_is_idempotent(sc):
    source = (ROOT / "scripts" / "strip_comments.py").read_text(encoding="utf-8")
    once = sc.strip_python(source)
    assert sc.strip_python(once) == once


def test_verify_python_rejects_code_changes(sc):
    with pytest.raises(sc.StripError):
        sc.verify_python("x = 1\n", "x = 2\n")
    with pytest.raises(sc.StripError):
        sc.verify_python("x = 1\n", "x = (\n")


def test_verify_python_accepts_docstring_changes(sc):
    sc.verify_python('def f():\n    """Long.\n\n    More."""\n    return 1\n', 'def f():\n    """Long."""\n    return 1\n')


def test_sql_comment_inside_strings_kept(sc):
    source = ("-- header\n-- more\n\nSELECT '--not' AS a, \"x--y\" AS b, `c--d` AS c -- trailing\nFROM t;\n\n"
              "-- section\nSELECT 'it''s -- here' AS e;\n")
    out = sc.strip_sql(source)
    assert out == "SELECT '--not' AS a, \"x--y\" AS b, `c--d` AS c\nFROM t;\n\nSELECT 'it''s -- here' AS e;\n"
    sc.verify_sql(source, out)


def test_sql_multiline_string_keeps_blank_lines(sc):
    source = "SELECT 'a\n\n\n\nb' AS x; -- c\n"
    out = sc.strip_sql(source)
    assert out == "SELECT 'a\n\n\n\nb' AS x;\n"
    sc.verify_sql(source, out)


def test_verify_sql_rejects_statement_changes(sc):
    with pytest.raises(sc.StripError):
        sc.verify_sql("SELECT 1;\nSELECT 2;\n", "SELECT 1;\n")


def test_yaml_hash_inside_quotes_kept(sc):
    source = ("# header\nname: \"x # y\"  # trailing\nquoted: 'it''s # z' # c\nplain: a#b\nurl: http://h/p#frag\n"
              "block: |\n  # kept in block\n  text\nfolded: >-\n  one\n  # also kept\nlist: [1, 2] # c\n"
              "items:\n  - 'q # 1'  # c\n  - plain # c\n")
    out = sc.strip_yaml(source)
    assert yaml.safe_load(out) == yaml.safe_load(source)
    assert "# header" not in out and "trailing" not in out and "# c" not in out
    assert "x # y" in out and "it''s # z" in out and "# kept in block" in out and "# also kept" in out
    sc.verify_yaml(source, out)


def test_yaml_block_ends_at_dedent(sc):
    source = "a: |\n  body\nb: 1  # c\n# full\nc: 2\n"
    assert sc.strip_yaml(source) == "a: |\n  body\nb: 1\nc: 2\n"


def test_verify_yaml_rejects_changes(sc):
    with pytest.raises(sc.StripError):
        sc.verify_yaml("a: 1\n", "a: 2\n")


def _tree(base: Path) -> Path:
    (base / "src" / "kpi_evidence").mkdir(parents=True)
    (base / "src" / "kpi_evidence" / "mod.py").write_text('"""Doc."""\nx = 1  # c\n', encoding="utf-8")
    (base / "src" / "sql").mkdir(parents=True)
    (base / "src" / "sql" / "v.sql").write_text("-- c\nSELECT 1;\n", encoding="utf-8")
    (base / "registry").mkdir()
    (base / "registry" / "r.yaml").write_text("a: 1 # c\n", encoding="utf-8")
    (base / "resources").mkdir()
    (base / "resources" / "jobs.yml").write_text("# c\nb: 2\n", encoding="utf-8")
    (base / "databricks.yml").write_text("bundle: {name: x}  # c\n", encoding="utf-8")
    (base / "notebooks").mkdir()
    (base / "notebooks" / "nb.py").write_text("# Databricks notebook source\n# c\ny = 2\n", encoding="utf-8")
    (base / "tests").mkdir()
    (base / "tests" / "test_a.py").write_text("def test_a():\n    assert True  # c\n", encoding="utf-8")
    (base / "src" / "kpi_evidence" / "__pycache__").mkdir()
    (base / "src" / "kpi_evidence" / "__pycache__" / "junk.py").write_text("# c\n", encoding="utf-8")
    return base


def test_collect_files_scope(sc, tmp_path):
    base = _tree(tmp_path)
    found = {(kind, p.relative_to(base).as_posix()) for kind, p in sc.collect_files(base)}
    assert found == {("python", "src/kpi_evidence/mod.py"), ("python", "notebooks/nb.py"), ("sql", "src/sql/v.sql"),
                     ("yaml", "registry/r.yaml"), ("yaml", "resources/jobs.yml"), ("yaml", "databricks.yml")}
    with_tests = {p.relative_to(base).as_posix() for _, p in sc.collect_files(base, include_tests=True)}
    assert "tests/test_a.py" in with_tests


def test_main_check_write_check(sc, tmp_path, capsys):
    base = _tree(tmp_path)
    assert sc.main(["--check", "--root", str(base)]) == 1
    assert (base / "src" / "kpi_evidence" / "mod.py").read_text(encoding="utf-8") == '"""Doc."""\nx = 1  # c\n'
    assert sc.main(["--write", "--root", str(base)]) == 0
    assert sc.main(["--check", "--root", str(base)]) == 0
    assert (base / "src" / "kpi_evidence" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (base / "notebooks" / "nb.py").read_text(encoding="utf-8") == "# Databricks notebook source\ny = 2\n"
    assert (base / "src" / "sql" / "v.sql").read_text(encoding="utf-8") == "SELECT 1;\n"
    assert (base / "databricks.yml").read_text(encoding="utf-8") == "bundle: {name: x}\n"
    assert (base / "tests" / "test_a.py").read_text(encoding="utf-8").endswith("# c\n")
    assert "0 error(s)" in capsys.readouterr().out


def test_unparsable_file_is_reported_and_left_alone(sc, tmp_path):
    path = tmp_path / "broken.py"
    path.write_text("def f(:\n    pass  # c\n", encoding="utf-8")
    result = sc.process_file("python", path, write=True)
    assert result.error
    assert path.read_text(encoding="utf-8") == "def f(:\n    pass  # c\n"


def test_shipped_scripts_have_no_comments(sc):
    for name in ("strip_comments.py", "build_release.py"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert sc.remaining_comments(text) == [], name
        assert sc.strip_python(text) == text, name
