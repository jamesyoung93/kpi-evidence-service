from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def br():
    spec = importlib.util.spec_from_file_location("kpi_release_build_unit", ROOT / "scripts" / "build_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pyproject(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_plan_lists_expected_artifacts(br, tmp_path):
    version = _pyproject(ROOT / "pyproject.toml")["project"]["version"]
    recon = _pyproject(ROOT / "libs" / "reconloop" / "pyproject.toml")["project"]["version"]
    plan = br.plan_release(ROOT, tmp_path)
    assert plan.folder == tmp_path / f"kpi_evidence_{version}"
    assert plan.archive == tmp_path / f"kpi_evidence_{version}.zip"
    assert plan.files == [
        f"wheels/kpi_evidence-{version}-py3-none-any.whl",
        f"wheels/reconloop-{recon}-py3-none-any.whl",
        "wheelhouse/",
        "notebooks/check_reports.py",
        "kpi_table_template.xlsx",
        "kpi_table_template.csv",
        "KPI_TABLE.md",
        "INSTALL.md",
        "requirements.txt",
        "requirements-offline.txt",
    ]
    assert plan.passes == [("3.12", "x86_64"), ("3.12", "aarch64"), ("3.11", "x86_64"), ("3.11", "aarch64")]


def test_plan_without_wheelhouse(br, tmp_path):
    plan = br.plan_release(ROOT, tmp_path, wheelhouse=False)
    assert "wheelhouse/" not in plan.files and plan.passes == []


def test_requirements_cover_runtime_dependencies(br):
    expected = {br.requirement_name(s) for s in _pyproject(ROOT / "pyproject.toml")["project"]["dependencies"]}
    expected |= {br.requirement_name(s) for s in _pyproject(ROOT / "libs" / "reconloop" / "pyproject.toml")["project"]["dependencies"]}
    expected -= {"reconloop", "kpi-evidence"}
    names = {br.requirement_name(s) for s in br.runtime_requirements(ROOT)}
    assert names == expected
    assert "reconloop" not in names and "pandas" in names


def test_stage_items_exist_and_include_packaged_data(br):
    items = br.stage_items(ROOT)
    for required in ("pyproject.toml", "src/kpi_evidence", "src/sql", "registry", "samples/reports",
                     "notebooks/check_reports.py", "libs/reconloop", "samples/kpi_table_example.csv"):
        assert required in items
    assert [i for i in items if not (ROOT / i).exists()] == []


def test_platform_tags_oldest_first(br):
    tags = br.platform_tags("aarch64")
    assert tags[:2] == ["manylinux2014_aarch64", "manylinux_2_17_aarch64"]
    assert "manylinux_2_28_aarch64" in tags and "manylinux_2_35_aarch64" in tags
    assert all(t.endswith("_aarch64") for t in tags)


def test_download_command(br, tmp_path):
    cmd = br.download_command(["pip"], tmp_path / "r.txt", tmp_path / "d", "3.11", "x86_64")
    assert cmd[:2] == ["pip", "download"]
    assert "--only-binary=:all:" in cmd
    assert cmd[cmd.index("--python-version") + 1] == "3.11"
    assert cmd[cmd.index("--implementation") + 1] == "cp"
    platforms = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--platform"]
    assert platforms == br.platform_tags("x86_64")


def test_offline_requirements_pins_and_markers(br, tmp_path):
    wheels, house = tmp_path / "wheels", tmp_path / "wheelhouse"
    wheels.mkdir()
    house.mkdir()
    (wheels / "kpi_evidence-1.0-py3-none-any.whl").write_bytes(b"k")
    names = {
        ("3.12", "x86_64"): ["numpy-2.5.0-cp312-cp312-manylinux_2_28_x86_64.whl", "six-1.17.0-py2.py3-none-any.whl"],
        ("3.11", "x86_64"): ["numpy-2.4.0-cp311-cp311-manylinux_2_28_x86_64.whl", "six-1.17.0-py2.py3-none-any.whl"],
    }
    for files in names.values():
        for name in files:
            (house / name).write_bytes(name.encode())
    text = br.offline_requirements(wheels, house, names)
    lines = text.splitlines()
    assert lines[:3] == ["--no-index", "--find-links wheels", "--find-links wheelhouse"]
    assert any(line.startswith("kpi-evidence==1.0 --hash=sha256:") for line in lines)
    assert any(line.startswith("six==1.17.0 --hash=sha256:") for line in lines)
    numpy = sorted(line for line in lines if line.startswith("numpy=="))
    assert len(numpy) == 2
    assert numpy[0].startswith('numpy==2.4.0 ; python_version == "3.11" --hash=sha256:')
    assert numpy[1].startswith('numpy==2.5.0 ; python_version == "3.12" --hash=sha256:')


def test_offline_requirements_arch_marker(br, tmp_path):
    wheels, house = tmp_path / "wheels", tmp_path / "wheelhouse"
    wheels.mkdir()
    house.mkdir()
    names = {
        ("3.12", "x86_64"): ["lib-1.0-cp312-cp312-manylinux_2_17_x86_64.whl"],
        ("3.12", "aarch64"): [],
    }
    (house / names[("3.12", "x86_64")][0]).write_bytes(b"x")
    text = br.offline_requirements(wheels, house, names)
    assert 'lib==1.0 ; platform_machine == "x86_64" --hash=sha256:' in text


def test_install_requirements_are_relative(br):
    text = br.install_requirements("9.9.9")
    assert text.splitlines() == ["--no-index", "--find-links wheels", "--find-links wheelhouse", "kpi_evidence==9.9.9"]


def test_install_text(br, tmp_path):
    plan = br.plan_release(ROOT, tmp_path)
    wheels = ["kpi_evidence-0.0.1-py3-none-any.whl", "reconloop-0.0.2-py3-none-any.whl"]
    text = br.install_text(plan, wheels, {("3.12", "x86_64"): []})
    base = f"/Volumes/<catalog>/<schema>/<volume>/kpi_evidence_{plan.version}"
    assert (f"%pip install --no-index --find-links {base}/wheelhouse {base}/wheels/reconloop-0.0.2-py3-none-any.whl "
            f"{base}/wheels/kpi_evidence-0.0.1-py3-none-any.whl") in text
    assert "dbutils.library.restartPython()" in text
    for line in ("import kpi_evidence as ke", "check = ke.ReportCheck(spark)", "check.add_reports(", "check.set_kpis(",
                 "result = check.run()", "result.show()"):
        assert line in text
    assert f"%pip install {base}/wheels/reconloop-0.0.2-py3-none-any.whl" in text


def test_dry_run_cli(br, capsys):
    assert br.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "wheels/kpi_evidence-" in out and "requirements-offline.txt" in out


def _fake_runner(version: str, recon: str, fail_download: bool = False):
    calls = []

    def runner(cmd):
        calls.append(list(cmd))
        if "build" in cmd or "wheel" in cmd:
            out = Path(cmd[cmd.index("--out-dir") + 1] if "--out-dir" in cmd else cmd[cmd.index("-w") + 1])
            project = Path(cmd[-1])
            name = f"reconloop-{recon}" if project.name == "reconloop" else f"kpi_evidence-{version}"
            (out / f"{name}-py3-none-any.whl").write_bytes(name.encode())
            return
        if "download" in cmd:
            if fail_download:
                raise subprocess.CalledProcessError(1, cmd)
            dest = Path(cmd[cmd.index("-d") + 1])
            py = cmd[cmd.index("--python-version") + 1].replace(".", "")
            arch = "aarch64" if any("aarch64" in part for part in cmd) else "x86_64"
            (dest / f"numpy-2.0.0-cp{py}-cp{py}-manylinux_2_28_{arch}.whl").write_bytes(b"n" + py.encode() + arch.encode())
            (dest / "six-1.17.0-py2.py3-none-any.whl").write_bytes(b"six")
            return
        raise AssertionError(cmd)

    runner.calls = calls
    return runner


def _snapshot() -> dict[str, bytes]:
    paths = ("notebooks/check_reports.py", "src/kpi_evidence/settings.py", "registry/policy.yaml",
             "src/sql/service_views.sql", "libs/reconloop/pyproject.toml")
    return {p: (ROOT / p).read_bytes() for p in paths}


def test_build_release_with_fake_tools(br, tmp_path):
    plan = br.plan_release(ROOT, tmp_path)
    runner = _fake_runner(plan.version, plan.reconloop_version)
    before = _snapshot()
    result = br.build_release(ROOT, tmp_path, runner=runner)
    assert _snapshot() == before
    assert result.complete
    for item in plan.files:
        assert (plan.folder / item.rstrip("/")).exists(), item
    assert len(list((plan.folder / "wheelhouse").glob("*.whl"))) == 5
    notebook = (plan.folder / "notebooks" / "check_reports.py").read_text(encoding="utf-8")
    assert notebook.startswith("# Databricks notebook source")
    strip = br._strip_module()
    assert strip.remaining_comments(notebook) == []
    assert "# MAGIC %pip install" in notebook
    doc = (plan.folder / "KPI_TABLE.md").read_text(encoding="utf-8")
    assert "kpi_table_template.csv" in doc and "kpi_table_example" not in doc
    offline = (plan.folder / "requirements-offline.txt").read_text(encoding="utf-8")
    assert "six==1.17.0 --hash=sha256:" in offline
    with zipfile.ZipFile(plan.archive) as archive:
        names = archive.namelist()
    assert f"{plan.folder.name}/INSTALL.md" in names
    assert all(n.startswith(f"{plan.folder.name}/") for n in names)


def test_failed_download_still_builds_the_rest(br, tmp_path):
    plan = br.plan_release(ROOT, tmp_path)
    runner = _fake_runner(plan.version, plan.reconloop_version, fail_download=True)
    result = br.build_release(ROOT, tmp_path, pythons=("3.12",), arches=("x86_64",), runner=runner)
    assert not result.complete and result.download_errors
    for item in ("INSTALL.md", "requirements.txt", "requirements-offline.txt", "notebooks/check_reports.py",
                 f"wheels/kpi_evidence-{plan.version}-py3-none-any.whl"):
        assert (plan.folder / item).exists(), item
    assert plan.archive.exists()
