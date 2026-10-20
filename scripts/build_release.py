from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECONLOOP = "libs/reconloop"
NOTEBOOK = "notebooks/check_reports.py"
KPI_TABLE_FILES = (
    ("kpi_table_template.xlsx", "samples/kpi_table_example.xlsx"),
    ("kpi_table_template.csv", "samples/kpi_table_example.csv"),
    ("KPI_TABLE.md", "samples/KPI_TABLE.md"),
)
PYTHON_VERSIONS = ("3.12", "3.11")
ARCHITECTURES = ("x86_64", "aarch64")
GLIBC_MINORS = tuple(range(17, 36))
STAGE_SKIP = frozenset({"__pycache__", ".pytest_cache", "build", "dist", ".git", ".venv", ".mypy_cache"})
VOLUME = "/Volumes/<catalog>/<schema>/<volume>"
DEMO_CHECK = (
    "import json, kpi_evidence as ke; r = ke.ReportCheck.demo().run(); "
    "print(json.dumps(r.comparisons.flag.value_counts().to_dict())); "
    "r.save_html('x.html'); r.save_excel('x.xlsx')"
)

Runner = Callable[[Sequence[str]], None]


class ReleaseError(RuntimeError):
    """The release cannot be built."""


@dataclass
class ReleasePlan:
    """Everything a build will produce, computed without building."""

    root: Path
    version: str
    reconloop_version: str
    folder: Path
    archive: Path
    wheels: list[str]
    files: list[str]
    requirements: list[str]
    passes: list[tuple[str, str]]
    stage_items: list[str]


@dataclass
class BuildResult:
    """What a build produced."""

    folder: Path
    archive: Path
    wheels: list[Path]
    downloaded: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    download_errors: list[str] = field(default_factory=list)
    verified: dict | None = None

    @property
    def complete(self) -> bool:
        return not self.download_errors


def _run(cmd: Sequence[str]) -> None:
    subprocess.run(list(cmd), check=True)


def _load_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from exc


def requirement_name(spec: str) -> str:
    """Normalized distribution name of a requirement string."""
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    if not match:
        raise ReleaseError(f"cannot parse requirement {spec!r}")
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def wheel_file_name(name: str, version: str) -> str:
    """File name of a pure-Python wheel."""
    return f"{re.sub(r'[-_.]+', '_', name).lower()}-{version}-py3-none-any.whl"


def runtime_requirements(root: Path = ROOT) -> list[str]:
    """Runtime dependencies of both wheels, without the wheels themselves."""
    own = {"kpi-evidence", "reconloop"}
    specs: list[str] = []
    for path in (root / "pyproject.toml", root / RECONLOOP / "pyproject.toml"):
        for spec in _load_toml(path).get("project", {}).get("dependencies", []):
            if requirement_name(spec) not in own and spec not in specs:
                specs.append(spec)
    return specs


def platform_tags(arch: str) -> list[str]:
    """Linux platform tags a Databricks runtime accepts, oldest first."""
    return [f"manylinux2014_{arch}", *(f"manylinux_2_{minor}_{arch}" for minor in GLIBC_MINORS)]


def stage_items(root: Path = ROOT) -> list[str]:
    """Paths copied into the build tree: the wheel inputs, the notebook and reconloop."""
    data = _load_toml(root / "pyproject.toml")
    wheel = data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    items = ["pyproject.toml"]
    project = data.get("project", {})
    readme = project.get("readme")
    if isinstance(readme, str):
        items.append(readme)
    items += [str(name) for name in project.get("license-files", [])]
    items += list(wheel.get("only-include", []))
    items += [NOTEBOOK, RECONLOOP]
    items += [source for _, source in KPI_TABLE_FILES]
    unique: list[str] = []
    for item in items:
        if item not in unique:
            unique.append(item)
    return unique


def plan_release(root: Path = ROOT, out_dir: Path | None = None, *, pythons: Sequence[str] = PYTHON_VERSIONS,
                 arches: Sequence[str] = ARCHITECTURES, wheelhouse: bool = True) -> ReleasePlan:
    """The release layout for the current pyproject versions."""
    root = Path(root)
    project = _load_toml(root / "pyproject.toml").get("project", {})
    recon = _load_toml(root / RECONLOOP / "pyproject.toml").get("project", {})
    version = project.get("version")
    if not version:
        raise ReleaseError("pyproject.toml has no [project].version")
    out = Path(out_dir) if out_dir else root / "release"
    name = f"kpi_evidence_{version}"
    wheels = [wheel_file_name("kpi_evidence", version), wheel_file_name("reconloop", recon.get("version", "0"))]
    files = [f"wheels/{w}" for w in wheels]
    if wheelhouse:
        files.append("wheelhouse/")
    files += [NOTEBOOK] + [target for target, _ in KPI_TABLE_FILES]
    files += ["INSTALL.md", "requirements.txt", "requirements-offline.txt"]
    passes = [(py, arch) for py in pythons for arch in arches] if wheelhouse else []
    return ReleasePlan(root, version, recon.get("version", "0"), out / name, out / f"{name}.zip", wheels, files,
                       runtime_requirements(root), passes, stage_items(root))


def _ignore(directory: str, names: list[str]) -> list[str]:
    return [n for n in names if n in STAGE_SKIP or n.endswith((".pyc", ".pyo")) or n.endswith(".egg-info")]


def _strip_module():
    path = Path(__file__).with_name("strip_comments.py")
    spec = importlib.util.spec_from_file_location("kpi_release_strip_comments", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def stage_tree(plan: ReleasePlan, dest: Path) -> int:
    """Copy the build inputs into dest and strip their comments; returns the files rewritten."""
    for item in plan.stage_items:
        source = plan.root / item
        target = dest / item
        if not source.exists():
            raise ReleaseError(f"missing build input {item}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=_ignore)
        else:
            shutil.copy2(source, target)
    strip = _strip_module()
    results = strip.run(dest, write=True, extra_python=(f"{RECONLOOP}/src",))
    errors = [r.describe(dest) for r in results if r.error]
    if errors:
        raise ReleaseError("comment stripping failed: " + "; ".join(errors))
    return sum(1 for r in results if r.changed)


def _pip_command() -> list[str] | None:
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip"]
    if shutil.which("uv"):
        return ["uv", "tool", "run", "pip"]
    return None


def build_command(project: Path, out: Path) -> list[str]:
    """uv build when available, else pip wheel without dependencies."""
    if shutil.which("uv"):
        return ["uv", "build", "--wheel", "--out-dir", str(out), str(project)]
    pip = _pip_command()
    if pip is None:
        raise ReleaseError("neither uv nor pip is available to build wheels")
    return [*pip, "wheel", "--no-deps", "-w", str(out), str(project)]


def build_wheels(stage: Path, out: Path, runner: Runner = _run) -> list[Path]:
    """Build the kpi_evidence and reconloop wheels from the stripped tree into out."""
    out.mkdir(parents=True, exist_ok=True)
    built: list[Path] = []
    for project, dist in ((stage, "kpi_evidence"), (stage / RECONLOOP, "reconloop")):
        with tempfile.TemporaryDirectory(prefix="kpi_release_wheel_") as tmp:
            try:
                runner(build_command(project, Path(tmp)))
            except (subprocess.CalledProcessError, OSError) as exc:
                raise ReleaseError(f"building {dist} failed: {exc}") from exc
            found = sorted(p for p in Path(tmp).glob("*.whl") if p.name.split("-", 1)[0].lower() == dist)
            if len(found) != 1:
                raise ReleaseError(f"expected one {dist} wheel, found {[p.name for p in found]}")
            target = out / found[0].name
            shutil.move(str(found[0]), target)
            built.append(target)
    return built


def download_command(pip: Sequence[str], requirements: Path, dest: Path, python: str, arch: str) -> list[str]:
    """pip download of binary wheels for one Python version and architecture."""
    cmd = [*pip, "download", "--only-binary=:all:", "--implementation", "cp", "--python-version", python,
           "--disable-pip-version-check", "--quiet", "-d", str(dest), "-r", str(requirements)]
    for tag in platform_tags(arch):
        cmd += ["--platform", tag]
    return cmd


def download_wheelhouse(requirements: Sequence[str], passes: Sequence[tuple[str, str]], dest: Path,
                        work: Path, runner: Runner = _run) -> tuple[dict[tuple[str, str], list[str]], list[str]]:
    """Download every dependency wheel per pass into dest; returns files per pass and errors."""
    downloaded: dict[tuple[str, str], list[str]] = {}
    errors: list[str] = []
    if not passes:
        return downloaded, errors
    dest.mkdir(parents=True, exist_ok=True)
    pip = _pip_command()
    if pip is None:
        return downloaded, ["pip is not available (install pip or uv)"]
    req_file = work / "requirements-download.txt"
    req_file.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    for python, arch in passes:
        pass_dir = work / f"download-{python}-{arch}"
        pass_dir.mkdir(parents=True, exist_ok=True)
        try:
            runner(download_command(pip, req_file, pass_dir, python, arch))
        except (subprocess.CalledProcessError, OSError) as exc:
            errors.append(f"Python {python} {arch}: {exc}")
            continue
        names = sorted(p.name for p in pass_dir.glob("*.whl"))
        for name in names:
            if not (dest / name).exists():
                shutil.copy2(pass_dir / name, dest / name)
        downloaded[(python, arch)] = names
    return downloaded, errors


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_identity(name: str) -> tuple[str, str]:
    parts = name[:-4].split("-")
    return re.sub(r"[-_.]+", "-", parts[0]).lower(), parts[1]


def _marker(seen: set[tuple[str, str]], all_passes: set[tuple[str, str]]) -> str:
    pythons = sorted({py for py, _ in all_passes})
    arches = sorted({arch for _, arch in all_passes})
    seen_pythons = sorted({py for py, _ in seen})
    seen_arches = sorted({arch for _, arch in seen})
    if seen == {(py, arch) for py in seen_pythons for arch in arches}:
        return " or ".join(f'python_version == "{py}"' for py in seen_pythons)
    if seen == {(py, arch) for py in pythons for arch in seen_arches}:
        return " or ".join(f'platform_machine == "{arch}"' for arch in seen_arches)
    return " or ".join(f'(python_version == "{py}" and platform_machine == "{arch}")' for py, arch in sorted(seen))


def offline_requirements(wheel_dir: Path, wheelhouse: Path, downloaded: dict[tuple[str, str], list[str]]) -> str:
    """Pinned requirements with sha256 hashes for every file in the release."""
    lines = ["--no-index", "--find-links wheels", "--find-links wheelhouse"]
    for wheel in sorted(wheel_dir.glob("*.whl")):
        dist, ver = _wheel_identity(wheel.name)
        lines.append(f"{dist}=={ver} --hash=sha256:{_sha256(wheel)}")
    all_passes = set(downloaded)
    versions: dict[str, dict[str, set[tuple[str, str]]]] = {}
    files: dict[tuple[str, str], set[str]] = {}
    for key, names in downloaded.items():
        for name in names:
            dist, ver = _wheel_identity(name)
            versions.setdefault(dist, {}).setdefault(ver, set()).add(key)
            files.setdefault((dist, ver), set()).add(name)
    for dist in sorted(versions):
        for ver in sorted(versions[dist]):
            hashes = " ".join(f"--hash=sha256:{_sha256(wheelhouse / n)}" for n in sorted(files[(dist, ver)]))
            seen = versions[dist][ver]
            marker = "" if seen == all_passes else f" ; {_marker(seen, all_passes)}"
            lines.append(f"{dist}=={ver}{marker} {hashes}")
    return "\n".join(lines) + "\n"


def install_requirements(version: str) -> str:
    """requirements.txt for an offline notebook install from the release folder."""
    return f"--no-index\n--find-links wheels\n--find-links wheelhouse\nkpi_evidence=={version}\n"


def install_text(plan: ReleasePlan, wheels: Sequence[str], downloaded: dict[tuple[str, str], list[str]]) -> str:
    """INSTALL.md for the release folder."""
    base = f"{VOLUME}/kpi_evidence_{plan.version}"
    ordered = sorted(wheels, key=lambda n: 0 if n.startswith("reconloop") else 1)
    wheel_paths = " ".join(f"{base}/wheels/{w}" for w in ordered)
    pythons = sorted({py for py, _ in downloaded}) or ["none"]
    arches = sorted({arch for _, arch in downloaded}) or ["none"]
    python_any = " or ".join(pythons)
    python_list = ", ".join(pythons)
    arch_list = ", ".join(arches)
    wheel_list = ", ".join(f"`{w}`" for w in ordered)
    return f"""# kpi_evidence {plan.version}: install in Databricks

## 1. Upload

Upload this folder to a Unity Catalog volume, keeping its name:

    {base}/

Catalog Explorer > the volume > Upload to this volume, or with the Databricks CLI:

    databricks fs cp -r kpi_evidence_{plan.version} dbfs:{base}

## 2. Install (no internet access needed)

In a notebook on serverless compute or a Unity Catalog cluster (Python {python_any}):

    %pip install -r {base}/requirements.txt

Then, in the next cell:

    dbutils.library.restartPython()

This installs the two wheels and only the dependencies the runtime does not already have, so the
runtime keeps its own pandas and numpy. The same install without the file:

    %pip install --no-index --find-links {base}/wheelhouse {wheel_paths}

`requirements-offline.txt` is a full pinned environment: every package in the wheelhouse at an exact
version with its sha256 hash. It replaces the runtime's own pandas, numpy and protobuf, so use it only
on clean or serverless compute:

    %pip install -r {base}/requirements-offline.txt

## 3. Run

    import kpi_evidence as ke
    check = ke.ReportCheck(spark)
    check.add_reports("{VOLUME}/reports/")
    check.set_kpis("{VOLUME}/kpi_table.xlsx")
    result = check.run()
    result.show()

Save the report with `result.save_html("{VOLUME}/out/kpi_check.html")` and
`result.save_excel("{VOLUME}/out/kpi_check.xlsx")`.

## Online install

When the workspace can reach PyPI or a package mirror:

    %pip install {wheel_paths}

## Contents

| Path | Content |
|---|---|
| `wheels/` | {wheel_list} |
| `wheelhouse/` | every dependency as Linux wheels ({arch_list}; Python {python_list}) |
| `notebooks/check_reports.py` | the notebook: Workspace > Import > File; set its `wheels` widget to `{base}/` |
| `kpi_table_template.xlsx`, `kpi_table_template.csv` | the KPI table to fill in (example rows included) |
| `KPI_TABLE.md` | the KPI table columns and rules |
| `requirements.txt` | the recommended offline install |
| `requirements-offline.txt` | a full pinned environment with sha256 hashes, for clean or serverless compute only |
"""


def _kpi_table_doc(text: str) -> str:
    return text.replace("samples/kpi_table_example", "kpi_table_template").replace("kpi_table_example", "kpi_table_template")


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def summary(folder: Path, archive: Path | None = None) -> str:
    """Files of the release folder with sizes and the total."""
    rows = []
    total = 0
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        size = path.stat().st_size
        total += size
        rows.append(f"  {_human(size):>10}  {path.relative_to(folder).as_posix()}")
    rows.append(f"  {_human(total):>10}  total ({len(rows)} files) in {folder}")
    if archive is not None and archive.exists():
        rows.append(f"  {_human(archive.stat().st_size):>10}  {archive}")
    return "\n".join(rows)


def write_archive(folder: Path, archive: Path) -> Path:
    """Zip the release folder with the folder name as the top-level entry."""
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            handle.write(path, f"{folder.name}/{path.relative_to(folder).as_posix()}")
    return archive


def _venv_python(venv: Path) -> Path:
    windows = venv / "Scripts" / "python.exe"
    return windows if windows.exists() else venv / "bin" / "python"


def verify_wheels(wheels: Sequence[Path], work: Path) -> dict:
    """Install the wheels in a fresh venv with PyPI dependencies and run the demo outside the repo."""
    if not shutil.which("uv"):
        raise ReleaseError("verification needs uv")
    work.mkdir(parents=True, exist_ok=True)
    venv = work / "venv"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    subprocess.run(["uv", "venv", "--python", "3.12", str(venv)], check=True, env=env)
    python = _venv_python(venv)
    subprocess.run(["uv", "pip", "install", "--python", str(python), *map(str, wheels)], check=True, env=env)
    run_dir = work / "run"
    run_dir.mkdir(exist_ok=True)
    done = subprocess.run([str(python), "-c", DEMO_CHECK], cwd=run_dir, env=env, check=True,
                          capture_output=True, text=True)
    counts = json.loads(done.stdout.strip().splitlines()[-1])
    return {"flags": counts, "html": str(run_dir / "x.html"), "excel": str(run_dir / "x.xlsx")}


def build_release(root: Path = ROOT, out_dir: Path | None = None, *, pythons: Sequence[str] = PYTHON_VERSIONS,
                  arches: Sequence[str] = ARCHITECTURES, wheelhouse: bool = True, verify: bool = False,
                  runner: Runner = _run) -> BuildResult:
    """Build the release folder and its zip archive."""
    plan = plan_release(root, out_dir, pythons=pythons, arches=arches, wheelhouse=wheelhouse)
    folder = plan.folder
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="kpi_release_") as tmp:
        work = Path(tmp)
        stage = work / "stage"
        rewritten = stage_tree(plan, stage)
        print(f"stripped comments from {rewritten} file(s) in the build copy")
        wheels = build_wheels(stage, folder / "wheels", runner)
        print("built " + ", ".join(w.name for w in wheels))
        notebook = folder / NOTEBOOK
        notebook.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stage / NOTEBOOK, notebook)
        for target, source in KPI_TABLE_FILES:
            if target.endswith(".md"):
                text = (stage / source).read_text(encoding="utf-8")
                (folder / target).write_text(_kpi_table_doc(text), encoding="utf-8", newline="\n")
            else:
                shutil.copy2(stage / source, folder / target)
        downloaded, errors = download_wheelhouse(plan.requirements, plan.passes, folder / "wheelhouse", work, runner)
    (folder / "requirements.txt").write_text(install_requirements(plan.version), encoding="utf-8", newline="\n")
    (folder / "requirements-offline.txt").write_text(
        offline_requirements(folder / "wheels", folder / "wheelhouse", downloaded),
        encoding="utf-8", newline="\n")
    (folder / "INSTALL.md").write_text(install_text(plan, [w.name for w in wheels], downloaded),
                                       encoding="utf-8", newline="\n")
    result = BuildResult(folder, write_archive(folder, plan.archive), wheels, downloaded, errors)
    if verify:
        with tempfile.TemporaryDirectory(prefix="kpi_release_verify_") as tmp:
            result.verified = verify_wheels(wheels, Path(tmp))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Build the offline Databricks release folder and zip.")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--out", type=Path, default=None, help="output folder (default: <root>/release)")
    parser.add_argument("--python", action="append", dest="pythons", help="Python version for the wheelhouse (repeatable)")
    parser.add_argument("--arch", action="append", dest="arches", help="Linux architecture for the wheelhouse (repeatable)")
    parser.add_argument("--no-wheelhouse", action="store_true", help="skip downloading dependency wheels")
    parser.add_argument("--verify", action="store_true", help="install the wheels in a fresh venv and run the demo")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    args = parser.parse_args(argv)
    pythons = tuple(args.pythons or PYTHON_VERSIONS)
    arches = tuple(args.arches or ARCHITECTURES)
    try:
        if args.dry_run:
            plan = plan_release(args.root.resolve(), args.out, pythons=pythons, arches=arches,
                                wheelhouse=not args.no_wheelhouse)
            print(f"folder:  {plan.folder}")
            print(f"archive: {plan.archive}")
            for item in plan.files:
                print(f"  {item}")
            print("wheelhouse requirements: " + ", ".join(plan.requirements))
            print("wheelhouse targets: " + ", ".join(f"Python {p} {a}" for p, a in plan.passes))
            return 0
        result = build_release(args.root.resolve(), args.out, pythons=pythons, arches=arches,
                               wheelhouse=not args.no_wheelhouse, verify=args.verify)
    except (ReleaseError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(summary(result.folder, result.archive))
    for key, names in sorted(result.downloaded.items()):
        print(f"wheelhouse Python {key[0]} {key[1]}: {len(names)} wheel(s)")
    if result.verified:
        print("fresh venv demo flags: " + json.dumps(result.verified["flags"]))
    if result.download_errors:
        print("WHEELHOUSE INCOMPLETE - the offline install will fail until it is rebuilt with network access:",
              file=sys.stderr)
        for line in result.download_errors:
            print(f"  {line}", file=sys.stderr)
        print("Everything else was built; the online install in INSTALL.md works.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
