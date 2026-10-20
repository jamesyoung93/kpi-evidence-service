from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KPI = "kpi_evidence-0.1.0+20261019120000-py3-none-any.whl"
RECON = "reconloop-0.5.0+20261019120000-py3-none-any.whl"


@pytest.fixture(scope="module")
def pw():
    spec = importlib.util.spec_from_file_location("kpi_publish_wheels_unit", ROOT / "scripts" / "publish_wheels.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["kpi_publish_wheels_unit"] = module
    spec.loader.exec_module(module)
    return module


def _wheel(folder: Path, name: str, mtime: float, content: bytes | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(content if content is not None else name.encode())
    os.utime(path, (mtime, mtime))
    return path


def test_wheel_distribution(pw):
    assert pw.wheel_distribution(KPI) == "kpi_evidence"
    assert pw.wheel_distribution("KPI_Evidence-0.1.0-py3-none-any.whl") == "kpi_evidence"
    assert pw.wheel_distribution(RECON) == "reconloop"
    assert pw.wheel_distribution("requirements.txt") is None
    assert pw.DISTRIBUTIONS == ("reconloop", "kpi_evidence")


def test_select_newest_wheel_per_distribution(pw, tmp_path):
    src = tmp_path / ".internal"
    _wheel(src, "kpi_evidence-0.1.0+20261001000000-py3-none-any.whl", 1_000)
    newest_kpi = _wheel(src / "nested", KPI, 2_000)
    _wheel(src, "reconloop-0.5.0+20261001000000-py3-none-any.whl", 1_500)
    newest_recon = _wheel(src, RECON, 1_600)
    _wheel(src, "other_pkg-1.0-py3-none-any.whl", 9_000)
    assert pw.select_wheels(src) == {"kpi_evidence": newest_kpi, "reconloop": newest_recon}


def test_missing_source_or_wheel_is_explicit(pw, tmp_path):
    with pytest.raises(pw.PublishError, match="does not exist"):
        pw.select_wheels(tmp_path / "nope")
    _wheel(tmp_path / "src", KPI, 1_000)
    with pytest.raises(pw.PublishError, match=r"no wheel for \['reconloop'\].*kpi_evidence-0\.1\.0"):
        pw.select_wheels(tmp_path / "src")


def test_requirements_text_uses_absolute_volume_paths(pw):
    text = pw.requirements_text("/Volumes/main/kpi_evidence/wheels/", [RECON, KPI])
    lines = text.splitlines()
    assert lines[0] == pw.MARKER
    assert lines[-2:] == [f"/Volumes/main/kpi_evidence/wheels/{RECON}", f"/Volumes/main/kpi_evidence/wheels/{KPI}"]
    assert "%pip install -r /Volumes/main/kpi_evidence/wheels/requirements.txt" in text
    with pytest.raises(pw.PublishError, match="absolute"):
        pw.requirements_text("wheels", [KPI])


def test_check_volume_dir_catches_git_bash_path_conversion(pw):
    assert pw.check_volume_dir("/Volumes/main/kpi_evidence/wheels") == "/Volumes/main/kpi_evidence/wheels"
    with pytest.raises(pw.PublishError, match="MSYS_NO_PATHCONV=1"):
        pw.check_volume_dir("C:/Program Files/Git/Volumes/main/kpi_evidence/wheels")
    for bad in ("/tmp/wheels", "/Volumes/main", "wheels"):
        with pytest.raises(pw.PublishError, match="volume folder"):
            pw.check_volume_dir(bad)


def test_resolve_workspace_path(pw, monkeypatch, tmp_path):
    existing = tmp_path / "x"
    existing.mkdir()
    assert pw.resolve_workspace_path(str(existing)) == existing
    assert pw.resolve_workspace_path("relative/dir") == Path("relative/dir")
    assert pw.resolve_workspace_path("/Volumes/c/s/wheels") == Path("/Volumes/c/s/wheels")
    real_exists = Path.exists
    wanted = "/Workspace/Users/me/.bundle/kpi/dev/artifacts/.internal"
    monkeypatch.setattr(Path, "exists", lambda self: self.as_posix() == wanted or real_exists(self))
    assert pw.resolve_workspace_path("/Users/me/.bundle/kpi/dev/artifacts/.internal").as_posix() == wanted
    assert pw.resolve_workspace_path(wanted).as_posix() == wanted


def test_copy_publishes_wheels_requirements_and_removes_stale_copies(pw, tmp_path):
    src, dest = tmp_path / ".internal", tmp_path / "wheels"
    _wheel(src, KPI, 2_000, b"new kpi")
    _wheel(src, RECON, 2_000, b"new recon")
    _wheel(dest, "kpi_evidence-0.1.0+20261001000000-py3-none-any.whl", 1_000)
    _wheel(dest, KPI, 1_000, b"old bytes, same name")
    (dest / "notes.txt").write_text("keep me", encoding="utf-8")
    result = pw.copy_wheels(src, dest, volume_dir="/Volumes/main/kpi_evidence/wheels")
    assert result["wheels"] == [RECON, KPI]
    assert result["removed"] == ["kpi_evidence-0.1.0+20261001000000-py3-none-any.whl"]
    assert sorted(p.name for p in dest.iterdir()) == sorted([KPI, RECON, "notes.txt", "requirements.txt"])
    assert (dest / KPI).read_bytes() == b"new kpi"
    req = (dest / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert req[-2:] == [f"/Volumes/main/kpi_evidence/wheels/{RECON}", f"/Volumes/main/kpi_evidence/wheels/{KPI}"]


def test_copy_validates_the_destination_before_writing_anything(pw, tmp_path):
    src = tmp_path / ".internal"
    _wheel(src, KPI, 2_000)
    _wheel(src, RECON, 2_000)
    with pytest.raises(pw.PublishError, match="absolute"):
        pw.copy_wheels(src, tmp_path / "dest", volume_dir="relative/wheels")
    assert not (tmp_path / "dest").exists(), "no half-published folder"


def test_copy_is_idempotent(pw, tmp_path):
    src, dest = tmp_path / ".internal", tmp_path / "wheels"
    _wheel(src, KPI, 2_000)
    _wheel(src, RECON, 2_000)
    first = pw.copy_wheels(src, dest, volume_dir="/Volumes/c/s/wheels")
    text = (dest / "requirements.txt").read_text(encoding="utf-8")
    second = pw.copy_wheels(src, dest, volume_dir="/Volumes/c/s/wheels")
    assert first["wheels"] == second["wheels"] and second["removed"] == []
    assert (dest / "requirements.txt").read_text(encoding="utf-8") == text


def test_main_copy_reports_and_raises_on_errors(pw, tmp_path, capsys):
    src, dest = tmp_path / ".internal", tmp_path / "wheels"
    _wheel(src, KPI, 2_000)
    _wheel(src, RECON, 2_000)
    assert pw.main(["copy", "--source", str(src), "--dest", str(dest), "--catalog=main"]) == 0
    out = capsys.readouterr().out
    assert f"published {KPI}" in out and "wrote" in out and "%pip install -r" in out
    with pytest.raises(pw.PublishError):
        pw.main(["copy", "--source", str(tmp_path / "missing"), "--dest", str(dest)])


def _fake_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "libs" / "reconloop").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='kpi_evidence'\n", encoding="utf-8")
    return root


def _fake_runner(calls):
    def run(cmd):
        calls.append(list(cmd))
        out_dir = Path(cmd[cmd.index("--out-dir") + 1] if "--out-dir" in cmd else cmd[cmd.index("-w") + 1])
        project = Path(cmd[-1])
        name = "reconloop-0.5.0" if project.name == "reconloop" else "kpi_evidence-0.1.0"
        (out_dir / f"{name}-py3-none-any.whl").write_bytes(name.encode())
        (project / "build").mkdir(exist_ok=True)
        (project / "src" / f"{project.name}.egg-info").mkdir(parents=True, exist_ok=True)
    return run


def test_main_build_writes_wheels_requirements_and_upload_commands(pw, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pw.shutil, "which", lambda name: "uv")
    root, out = _fake_project(tmp_path), tmp_path / "out"
    out.mkdir()
    (out / "kpi_evidence-0.0.9-py3-none-any.whl").write_bytes(b"stale")
    calls: list[list[str]] = []
    argv = ["build", "--volume-dir", "/Volumes/main/kpi_evidence/wheels", "--out", str(out), "--root", str(root)]
    assert pw.main(argv, runner=_fake_runner(calls)) == 0
    assert [c[:3] for c in calls] == [["uv", "build", "--wheel"]] * 2
    assert Path(calls[0][-1]).name == "reconloop" and Path(calls[1][-1]) == root.resolve()
    assert sorted(p.name for p in out.iterdir()) == [
        "kpi_evidence-0.1.0-py3-none-any.whl", "reconloop-0.5.0-py3-none-any.whl", "requirements.txt"]
    assert not (root / "libs" / "reconloop" / "build").exists(), "build debris removed from the vendored tree"
    assert not (root / "libs" / "reconloop" / "src" / "reconloop.egg-info").exists()
    assert (root / "src" / "proj.egg-info").exists(), "the project root itself is never cleaned"
    req = (out / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert req[-2:] == ["/Volumes/main/kpi_evidence/wheels/reconloop-0.5.0-py3-none-any.whl",
                        "/Volumes/main/kpi_evidence/wheels/kpi_evidence-0.1.0-py3-none-any.whl"]
    printed = capsys.readouterr().out
    assert printed.count("databricks fs cp --overwrite") == 3
    assert '"dbfs:/Volumes/main/kpi_evidence/wheels/requirements.txt"' in printed
    assert "%pip install -r /Volumes/main/kpi_evidence/wheels/requirements.txt" in printed


def test_build_keeps_debris_that_existed_before(pw, tmp_path, monkeypatch):
    monkeypatch.setattr(pw.shutil, "which", lambda name: "uv")
    root = _fake_project(tmp_path)
    old_info = root / "libs" / "reconloop" / "src" / "reconloop.egg-info"
    old_info.mkdir(parents=True)
    pw.build_wheel(root / "libs" / "reconloop", "reconloop", tmp_path, runner=_fake_runner([]))
    assert old_info.exists() and not (root / "libs" / "reconloop" / "build").exists()


def test_build_rejects_a_non_volume_folder_before_building(pw, tmp_path):
    calls: list = []
    with pytest.raises(pw.PublishError, match="MSYS_NO_PATHCONV"):
        pw.main(["build", "--volume-dir", "C:/Program Files/Git/Volumes/c/s/v", "--out", str(tmp_path)],
                runner=_fake_runner(calls))
    assert calls == []


def test_build_failures_are_explicit(pw, tmp_path, monkeypatch):
    monkeypatch.setattr(pw.shutil, "which", lambda name: None)
    root = _fake_project(tmp_path)
    seen = []

    def no_output(cmd):
        seen.append(cmd)

    with pytest.raises(pw.PublishError, match="expected one reconloop wheel"):
        pw.build_wheel(root / "libs" / "reconloop", "reconloop", tmp_path, runner=no_output)
    assert seen[0][1:4] == ["-m", "pip", "wheel"] and "--no-deps" in seen[0]

    def broken(cmd):
        raise pw.subprocess.CalledProcessError(1, cmd)

    with pytest.raises(pw.PublishError, match="failed"):
        pw.build_wheel(root / "libs" / "reconloop", "reconloop", tmp_path, runner=broken)


def test_script_is_standard_library_only(pw):
    import ast

    tree = ast.parse((ROOT / "scripts" / "publish_wheels.py").read_text(encoding="utf-8"))
    modules = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    modules |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}, modules - set(sys.stdlib_module_names)
