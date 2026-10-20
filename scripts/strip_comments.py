from __future__ import annotations

import argparse
import ast
import inspect
import io
import re
import sys
import tokenize
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK_HEADER = "# Databricks notebook source"
NOTEBOOK_MARKERS = ("# COMMAND ----------", "# MAGIC", "# DBTITLE")
CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
TYPE_IGNORE_RE = re.compile(r"#\s*type:\s*ignore(?:\[[^\]]*\])?")
PYTHON_DIRS = ("src/kpi_evidence", "notebooks", "scripts", "examples")
TEST_DIRS = ("tests",)
SQL_DIRS = ("src/sql",)
YAML_PATTERNS = ("registry/**/*.yaml", "databricks.yml", "resources/*.yml")
SKIP_DIRS = frozenset({"__pycache__", ".venv", ".git", ".pytest_cache", "build", "dist", "node_modules"})
SUMMARY_LIMIT = 100
ABBREVIATIONS = frozenset({"e.g", "i.e", "etc", "vs", "cf", "approx", "incl", "resp", "ca"})
BLOCK_SCALAR_RE = re.compile(r"(?:^\s*|[:\-?]\s+)[|>][-+]?[1-9]?[-+]?$")
DOC_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


class StripError(ValueError):
    """A rewrite that cannot be proven equivalent to the original."""


@dataclass
class Rewrite:
    """Rewritten text with what was removed."""

    text: str
    comments: int = 0
    docstrings_removed: int = 0
    docstrings_shortened: int = 0


@dataclass
class FileResult:
    """Outcome for one file."""

    path: Path
    kind: str
    changed: bool = False
    comments: int = 0
    docstrings: int = 0
    error: str | None = None

    def describe(self, root: Path) -> str:
        """One line for the console."""
        try:
            name = self.path.relative_to(root).as_posix()
        except ValueError:
            name = self.path.as_posix()
        if self.error:
            return f"{name}: error: {self.error}"
        parts = []
        if self.comments:
            parts.append(f"{self.comments} comment(s)")
        if self.docstrings:
            parts.append(f"{self.docstrings} docstring(s)")
        return f"{name}: {', '.join(parts) or 'layout only'}"


def summary_line(doc: str) -> str:
    """First sentence of a docstring, at most about 100 characters."""
    text = inspect.cleandoc(doc).strip()
    paragraph = text.split("\n\n", 1)[0]
    flat = " ".join(line.strip() for line in paragraph.splitlines()).strip()
    sentence = flat
    for match in re.finditer(r"[.!?](?=\s|$)", flat):
        words = flat[: match.start()].split()
        last = words[-1].lower() if words else ""
        if last in ABBREVIATIONS or (len(last) == 1 and last.isalpha()):
            continue
        sentence = flat[: match.end()]
        break
    if len(sentence) > SUMMARY_LIMIT + 10:
        window = sentence[:SUMMARY_LIMIT]
        cut = max(window.rfind(sep) for sep in ("; ", " (", " - ", ": ", ", "))
        if cut >= 20:
            sentence = sentence[:cut]
        else:
            cut = window.rfind(" ")
            sentence = window[:cut] if cut > 0 else window
    if sentence.count("``") % 2:
        sentence = sentence[: sentence.rfind("``")]
    if sentence.count("(") > sentence.count(")"):
        sentence = sentence[: sentence.rfind("(")]
    sentence = sentence.strip().rstrip(" ,;:-")
    if sentence and not sentence.endswith((".", "!", "?")):
        sentence += "."
    return sentence


def docstring_literal(text: str) -> str:
    """A source literal for a one-line docstring."""
    if "\\" in text or '"""' in text or text.endswith('"') or "\n" in text:
        return repr(text)
    return f'"""{text}"""'


def _lines(text: str) -> list[str]:
    return io.StringIO(text).readlines()


def _offsets(lines: Sequence[str]) -> list[int]:
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    return starts


def _char_col(line: str, byte_col: int) -> int:
    if line.isascii():
        return byte_col
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", "replace"))


def _is_notebook(text: str) -> bool:
    return text.startswith(NOTEBOOK_HEADER)


def _kept_comment(comment: str, row: int, notebook: bool) -> str | None:
    if row == 1 and comment.startswith("#!"):
        return comment
    if row <= 2 and CODING_RE.match(comment):
        return comment
    if notebook and ((comment == NOTEBOOK_HEADER and row == 1) or comment.startswith(NOTEBOOK_MARKERS)):
        return comment
    match = TYPE_IGNORE_RE.match(comment)
    if match:
        return match.group(0)
    return None


def _docstring(node: ast.AST) -> ast.Expr | None:
    body = getattr(node, "body", None)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[0]
    return None


def _is_noop(node: ast.stmt) -> bool:
    if isinstance(node, ast.Pass):
        return True
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and node.value.value is Ellipsis


def normalized_dump(source: str) -> str:
    """ast.dump of a module with every docstring and docstring-only placeholder removed."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, DOC_OWNERS):
            body = list(node.body)
            if _docstring(node) is not None:
                body = body[1:]
            if len(body) == 1 and _is_noop(body[0]):
                body = []
            node.body = body
    return ast.dump(tree)


def _scan_tree(tree: ast.AST) -> tuple[list[ast.AST], bool, bool]:
    owners: list[ast.AST] = []
    names: set[str] = set()
    attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, DOC_OWNERS):
            owners.append(node)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            attrs.add(node.attr)
    module_doc = "__doc__" in names
    any_doc = "__doc__" in attrs or "getdoc" in attrs or "getdoc" in names
    return owners, module_doc or any_doc, any_doc


def _comment_edits(source: str, lines: Sequence[str], starts: Sequence[int], notebook: bool) -> list[tuple[int, int, str]]:
    edits = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        row, col = tok.start
        kept = _kept_comment(tok.string, row, notebook)
        if kept == tok.string:
            continue
        line = lines[row - 1]
        base = starts[row - 1]
        before = line[:col]
        end = base + col + len(tok.string)
        if not before.strip():
            if kept:
                edits.append((base + col, end, kept))
            else:
                edits.append((base, base + len(line), ""))
        else:
            cut = len(before.rstrip())
            edits.append((base + cut, end, f"  {kept}" if kept else ""))
    return edits


def _docstring_edits(source: str, tree: ast.Module, lines: Sequence[str], starts: Sequence[int],
                     drop_all: bool) -> tuple[list[tuple[int, int, str]], int, int]:
    owners, keep_module, keep_all = _scan_tree(tree)
    edits = []
    removed = shortened = 0
    for owner in owners:
        doc = _docstring(owner)
        if doc is None:
            continue
        srow, erow = doc.lineno, doc.end_lineno
        scol = _char_col(lines[srow - 1], doc.col_offset)
        ecol = _char_col(lines[erow - 1], doc.end_col_offset)
        start, end = starts[srow - 1] + scol, starts[erow - 1] + ecol
        if isinstance(owner, ast.Module):
            short = keep_module
        elif keep_all:
            short = True
        else:
            short = not drop_all and not owner.name.startswith("_")
        if short:
            literal = docstring_literal(summary_line(doc.value.value))
            if source[start:end] != literal:
                edits.append((start, end, literal))
                shortened += 1
            continue
        removed += 1
        if not isinstance(owner, ast.Module) and len(owner.body) == 1:
            edits.append((start, end, "..."))
            continue
        prefix = lines[srow - 1][:scol]
        suffix = lines[erow - 1][ecol:]
        if not prefix.strip() and (not suffix.strip() or suffix.lstrip().startswith("#")):
            last = erow
            while last < len(lines) and not lines[last].strip():
                last += 1
            edits.append((starts[srow - 1], starts[last], ""))
            continue
        tail = suffix.lstrip()
        if tail.startswith(";"):
            end = starts[erow - 1] + ecol + (len(suffix) - len(tail)) + 1
            end += len(source[end:]) - len(source[end:].lstrip(" \t"))
        edits.append((start, end, ""))
    return edits, removed, shortened


def _apply(source: str, edits: Iterable[tuple[int, int, str]]) -> str:
    ordered = sorted(edits, key=lambda e: (e[0], -e[1]))
    kept: list[tuple[int, int, str]] = []
    for edit in ordered:
        if kept and edit[0] < kept[-1][1]:
            if edit[1] <= kept[-1][1]:
                continue
            raise StripError(f"overlapping edits at offset {edit[0]}")
        kept.append(edit)
    out = source
    for start, end, text in reversed(kept):
        out = out[:start] + text + out[end:]
    return out


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text[:4096] else "\n"


def tidy_python(text: str) -> str:
    """Drop leading and trailing blank lines, blank lines after block headers and runs over two."""
    lines = _lines(text)
    if not lines:
        return text
    protected: set[int] = set()
    headers: set[int] = set()
    previous = None
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.end[0] > tok.start[0] and tok.type not in (tokenize.NL, tokenize.NEWLINE):
            protected.update(range(tok.start[0] + 1, tok.end[0] + 1))
        if tok.type == tokenize.NEWLINE and previous is not None and previous.type == tokenize.OP \
                and previous.string == ":":
            headers.add(tok.start[0])
        if tok.type not in (tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
            previous = tok
    out: list[str] = []
    run = 0
    after_header = False
    for row, line in enumerate(lines, 1):
        if not line.strip() and row not in protected:
            if not out or after_header:
                continue
            run += 1
            if run > 2:
                continue
            out.append(line)
            continue
        run = 0
        out.append(line)
        after_header = row in headers
    while out and not out[-1].strip():
        out.pop()
    if not out:
        return ""
    newline = _newline(text)
    if not out[-1].endswith(("\n", "\r")):
        out[-1] += newline
    return "".join(out)


def rewrite_python(source: str, *, notebook: bool | None = None, drop_docstrings: bool = False) -> Rewrite:
    """Remove comments and reduce docstrings in Python source."""
    bom = source.startswith("﻿")
    body = source[1:] if bom else source
    notebook = _is_notebook(body) if notebook is None else notebook
    tree = ast.parse(body)
    lines = _lines(body)
    starts = _offsets(lines)
    comment_edits = _comment_edits(body, lines, starts, notebook)
    doc_edits, removed, shortened = _docstring_edits(body, tree, lines, starts, drop_docstrings)
    text = tidy_python(_apply(body, comment_edits + doc_edits))
    return Rewrite(("﻿" if bom else "") + text, len(comment_edits), removed, shortened)


def strip_python(source: str, *, notebook: bool | None = None, drop_docstrings: bool = False) -> str:
    """Python source without comments and with reduced docstrings."""
    return rewrite_python(source, notebook=notebook, drop_docstrings=drop_docstrings).text


def remaining_comments(source: str, *, notebook: bool | None = None) -> list[tuple[int, str]]:
    """Comments that are not functional markers."""
    body = source.lstrip("﻿")
    notebook = _is_notebook(body) if notebook is None else notebook
    found = []
    for tok in tokenize.generate_tokens(io.StringIO(body).readline):
        if tok.type == tokenize.COMMENT and _kept_comment(tok.string, tok.start[0], notebook) != tok.string:
            found.append((tok.start[0], tok.string))
    return found


def verify_python(before: str, after: str, name: str = "<source>") -> None:
    """Raise StripError unless only comments and docstrings differ."""
    try:
        compile(after.lstrip("﻿"), name, "exec")
    except SyntaxError as exc:
        raise StripError(f"rewritten source does not compile: {exc}") from exc
    if normalized_dump(before.lstrip("﻿")) != normalized_dump(after.lstrip("﻿")):
        raise StripError("syntax tree changed")


def _sql_scan(text: str) -> tuple[str, list[bool], int]:
    out: list[str] = []
    quoted_rows: list[bool] = [False]
    quote = None
    removed = 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            elif ch == "\n":
                quoted_rows.append(True)
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            end = n if j < 0 else j
            if end > i and text[end - 1] == "\r":
                end -= 1
            out.append("\x00")
            removed += 1
            i = end
            continue
        if ch == "\n":
            quoted_rows.append(False)
        out.append(ch)
        i += 1
    return "".join(out), quoted_rows, removed


def rewrite_sql(text: str) -> Rewrite:
    """Remove -- comments outside quotes."""
    if "\x00" in text:
        raise StripError("NUL character in SQL")
    scanned, quoted_rows, removed = _sql_scan(text)
    newline = _newline(text)
    out: list[str] = []
    run = 0
    for row, line in enumerate(scanned.split("\n")):
        in_quote = quoted_rows[row] if row < len(quoted_rows) else False
        if line.endswith("\r"):
            line = line[:-1]
        if "\x00" in line:
            head = line.split("\x00", 1)[0]
            if not head.strip():
                continue
            line = head.rstrip()
        if not line.strip() and not in_quote:
            run += 1
            if not out or run > 1:
                continue
            out.append("")
            continue
        run = 0
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return Rewrite(newline.join(out) + newline if out else "", removed)


def strip_sql(text: str) -> str:
    """SQL text without -- comments."""
    return rewrite_sql(text).text


def _split_sql():
    try:
        from kpi_evidence.store.base import split_sql
    except ImportError:
        sys.path.insert(0, str(ROOT / "src"))
        from kpi_evidence.store.base import split_sql
    return split_sql


def sql_statements(text: str) -> list[str]:
    """Statements as split by the package, whitespace normalized."""
    return [" ".join(s.split()) for s in _split_sql()(text)]


def verify_sql(before: str, after: str) -> None:
    """Raise StripError unless the statement lists are identical."""
    if sql_statements(before) != sql_statements(after):
        raise StripError("SQL statements changed")
    if _sql_scan(after)[2]:
        raise StripError("comments remain")


def _yaml_close(body: str, start: int, quote: str) -> int:
    i = start
    while i < len(body):
        ch = body[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if quote == "'" and body.startswith("''", i):
                i += 2
                continue
            return i
        i += 1
    return -1


def _yaml_quote_can_open(body: str, i: int) -> bool:
    j = i - 1
    while j >= 0 and body[j] in " \t":
        j -= 1
    if j < 0:
        return True
    prev = body[j]
    if prev in "[{,":
        return True
    return prev in ":-?" and i - j >= 2


def _yaml_scan(body: str, quote: str | None) -> tuple[int, str | None]:
    i = 0
    if quote:
        close = _yaml_close(body, 0, quote)
        if close < 0:
            return -1, quote
        i = close + 1
    while i < len(body):
        ch = body[i]
        if ch == "#" and (i == 0 or body[i - 1] in " \t"):
            return i, None
        if ch in ("'", '"') and _yaml_quote_can_open(body, i):
            close = _yaml_close(body, i + 1, ch)
            if close < 0:
                return -1, ch
            i = close + 1
            continue
        i += 1
    return -1, None


def _yaml_pass(text: str, collapse: bool) -> Rewrite:
    out: list[str] = []
    removed = 0
    quote: str | None = None
    block_parent: int | None = None
    block_indent: int | None = None
    run = 0
    for line in _lines(text):
        body = line.rstrip("\r\n")
        newline = line[len(body):]
        indent = len(body) - len(body.lstrip(" "))
        if block_parent is not None:
            if not body.strip():
                out.append(line)
                continue
            if block_indent is None and indent > block_parent:
                block_indent = indent
            if block_indent is not None and indent >= block_indent:
                out.append(line)
                continue
            block_parent = block_indent = None
        if quote is None and not body.strip():
            run += 1
            if collapse and (not out or run > 2):
                continue
            out.append(line)
            continue
        position, next_quote = _yaml_scan(body, quote)
        if position >= 0:
            removed += 1
            head = body[:position].rstrip()
            if not head.strip():
                continue
            body = head
            line = body + newline
        run = 0
        out.append(line)
        if next_quote is None and quote is None and BLOCK_SCALAR_RE.search(body.rstrip()):
            block_parent, block_indent = indent, None
        quote = next_quote
    if collapse:
        while out and not out[-1].strip():
            out.pop()
        if out and not out[-1].endswith("\n"):
            out[-1] += _newline(text)
    return Rewrite("".join(out), removed)


def _yaml_documents(text: str) -> list:
    import yaml

    return list(yaml.safe_load_all(text))


def rewrite_yaml(text: str) -> Rewrite:
    """Remove full-line and trailing comments outside quotes and block scalars."""
    original = _yaml_documents(text)
    for collapse in (True, False):
        result = _yaml_pass(text, collapse)
        if _yaml_documents(result.text) == original:
            return result
    raise StripError("YAML content changed")


def strip_yaml(text: str) -> str:
    """YAML text without comments."""
    return rewrite_yaml(text).text


def verify_yaml(before: str, after: str) -> None:
    """Raise StripError unless both texts load to the same data."""
    if _yaml_documents(before) != _yaml_documents(after):
        raise StripError("YAML content changed")


def _walk(base: Path, pattern: str) -> list[Path]:
    if not base.exists():
        return []
    if base.is_file():
        return [base]
    return sorted(p for p in base.rglob(pattern) if p.is_file() and not SKIP_DIRS.intersection(p.parts))


def collect_files(root: Path, *, include_tests: bool = False, extra_python: Sequence[str] = ()) -> list[tuple[str, Path]]:
    """Every file in scope as (kind, path); kind is python, test, sql or yaml."""
    root = Path(root)
    found: list[tuple[str, Path]] = []
    for rel in (*PYTHON_DIRS, *extra_python):
        found += [("python", p) for p in _walk(root / rel, "*.py")]
    if include_tests:
        for rel in TEST_DIRS:
            found += [("test", p) for p in _walk(root / rel, "*.py")]
    for rel in SQL_DIRS:
        found += [("sql", p) for p in _walk(root / rel, "*.sql")]
    for pattern in YAML_PATTERNS:
        found += [("yaml", p) for p in sorted(root.glob(pattern)) if p.is_file() and not SKIP_DIRS.intersection(p.parts)]
    seen: set[Path] = set()
    unique = []
    for kind, path in found:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            unique.append((kind, path))
    return unique


def rewrite_text(kind: str, text: str, name: str = "<source>") -> Rewrite:
    """Rewrite and verify one file's text."""
    if kind in ("python", "test"):
        result = rewrite_python(text, drop_docstrings=kind == "test")
        verify_python(text, result.text, name)
        if remaining_comments(result.text):
            raise StripError("comments remain")
        return result
    if kind == "sql":
        result = rewrite_sql(text)
        verify_sql(text, result.text)
        return result
    if kind == "yaml":
        result = rewrite_yaml(text)
        verify_yaml(text, result.text)
        if _yaml_pass(result.text, True).comments:
            raise StripError("comments remain")
        return result
    raise StripError(f"unknown kind {kind!r}")


def process_file(kind: str, path: Path, *, write: bool) -> FileResult:
    """Check or rewrite one file; never writes an unverified result."""
    result = FileResult(Path(path), kind)
    try:
        raw = Path(path).read_bytes()
        text = raw.decode("utf-8")
        rewritten = rewrite_text(kind, text, str(path))
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    result.changed = rewritten.text != text
    result.comments = rewritten.comments
    result.docstrings = rewritten.docstrings_removed + rewritten.docstrings_shortened
    if write and result.changed:
        Path(path).write_bytes(rewritten.text.encode("utf-8"))
    return result


def run(root: Path, *, write: bool, include_tests: bool = False, extra_python: Sequence[str] = ()) -> list[FileResult]:
    """Process every file in scope under root."""
    return [process_file(kind, path, write=write)
            for kind, path in collect_files(Path(root), include_tests=include_tests, extra_python=extra_python)]


def main(argv: Sequence[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Remove comments from the shipped source and prove nothing else changed.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="list files that still contain comments (exit 1 if any)")
    mode.add_argument("--write", action="store_true", help="rewrite the files in place")
    parser.add_argument("--root", type=Path, default=ROOT, help="tree to process (default: this repository)")
    parser.add_argument("--include-tests", action="store_true", help="also process tests/")
    parser.add_argument("--extra", action="append", default=[], help="additional Python folder relative to the root")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    results = run(root, write=args.write, include_tests=args.include_tests, extra_python=args.extra)
    errors = [r for r in results if r.error]
    changed = [r for r in results if r.changed and not r.error]
    for item in changed:
        print(("rewrote " if args.write else "") + item.describe(root))
    for item in errors:
        print(item.describe(root), file=sys.stderr)
    verb = "rewritten" if args.write else "with comments or long docstrings"
    print(f"{len(results)} file(s) scanned, {len(changed)} {verb}, {len(errors)} error(s)")
    if errors:
        return 2
    return 1 if changed and args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
