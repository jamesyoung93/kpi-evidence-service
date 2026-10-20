# Release

## Cut a release

1. Set the version in `pyproject.toml` (`[project].version`) and in `src/kpi_evidence/__init__.py` (`__version__`).
2. Run the tests: `python -m pytest -q -m "not spark"`.
3. Build and verify:

   ```bash
   python scripts/build_release.py --verify
   ```

4. Hand over `release/kpi_evidence_<version>.zip`. The install steps are in its `INSTALL.md`.

Build machine: Python 3.11 or later with the dev dependencies installed, `uv` on the PATH, and access to PyPI.

| Option | Effect |
|---|---|
| `--dry-run` | print the planned files and exit |
| `--verify` | install the two wheels in a fresh Python 3.12 venv (dependencies from PyPI) and run `ReportCheck.demo()` outside the repository |
| `--no-wheelhouse` | skip the dependency download |
| `--python 3.12 --arch x86_64` | limit the wheelhouse to these targets (repeatable) |
| `--out <dir>` | output folder instead of `release/` |
| `--root <dir>` | repository to build from (default: the one that holds the script) |

Exit codes: `0` complete, `1` built but the wheelhouse download failed (the offline install will not work), `2` build failed.

## What the build does

1. Copies the wheel inputs to a temporary folder: the package, `src/sql`, `registry`, the demo platform data, the
   sample reports, the KPI table example, the notebook and `libs/reconloop`.
2. Removes comments there with `scripts/strip_comments.py`, and proves each file is otherwise unchanged (Python
   syntax tree, SQL statement list, YAML data). The repository is not modified.
3. Builds the `kpi_evidence` and `reconloop` wheels with `uv build --wheel`.
4. Downloads every runtime dependency as Linux wheels (manylinux, x86_64 and aarch64, CPython 3.11 and 3.12)
   with `pip download --only-binary=:all:`.
5. Writes the release folder and its zip.

## Release folder

| Path | Content |
|---|---|
| `wheels/` | `kpi_evidence` and `reconloop` wheels, comments removed |
| `wheelhouse/` | every runtime dependency as Linux wheels, for an install without internet access |
| `notebooks/check_reports.py` | the Databricks notebook (source format) |
| `kpi_table_template.xlsx`, `kpi_table_template.csv` | the KPI table, with example rows |
| `KPI_TABLE.md` | KPI table columns and rules |
| `INSTALL.md` | upload, install, restart, run |
| `requirements.txt` | offline install from the folder: `%pip install -r <folder>/requirements.txt` |
| `requirements-offline.txt` | every file pinned with its sha256 hash |

## Comment check

```bash
python scripts/strip_comments.py --check
python scripts/strip_comments.py --write --root <copy of the repository>
```

`--check` exits with `1` while files in scope still contain comments, `--include-tests` adds `tests/`, and
`--extra <dir>` adds another Python folder. Files whose rewrite cannot be proven equivalent are reported and left
unchanged (exit code `2`).
