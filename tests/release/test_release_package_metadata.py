from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INTERNAL = re.compile(r"insight_harness|adversarial|findings?\b|DESIGN|NOTEBOOK\.md|DEPLOY\.md|UPSTREAM|"
                      r"docs/|libs/|scripts/|\.venv|-e \.|## Status|## Layout|parked", re.I)


@pytest.fixture(scope="module")
def br():
    spec = importlib.util.spec_from_file_location("kpi_release_build_meta", ROOT / "scripts" / "build_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _wheel(pyproject: dict) -> dict:
    return pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]


def test_package_description_is_the_short_release_text(pyproject):
    readme = pyproject["project"]["readme"]
    assert readme == "PACKAGE.md"
    text = (ROOT / readme).read_text(encoding="utf-8")
    assert INTERNAL.findall(text) == []
    for phrase in ("ke.ReportCheck(spark)", "check.run()", "GOOD", "REVIEW", "BLOCKED", "INFO",
                   "requirements.txt", "dbutils.library.restartPython()"):
        assert phrase in text, phrase
    assert "](" not in text, "relative links do not resolve in package metadata"


def test_license_file_is_declared_and_present(pyproject):
    assert pyproject["project"]["license"] == "MIT"
    assert pyproject["project"]["license-files"] == ["LICENSE"]
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License") and "Copyright (c) 2026 James Young" in text
    requires = pyproject["build-system"]["requires"]
    assert any(re.fullmatch(r"hatchling>=1\.(2[7-9]|[3-9]\d)(\.\d+)?", r) for r in requires)


def test_no_extra_for_the_parked_app(pyproject):
    assert "app" not in pyproject["project"].get("optional-dependencies", {})


def test_wheel_inputs_exist_map_into_the_package_and_skip_placeholders(pyproject):
    wheel = _wheel(pyproject)
    assert "force-include" not in wheel, "force-included folders ignore the exclude patterns"
    assert "**/.gitkeep" in wheel["exclude"]
    sources = wheel["sources"]
    for item in wheel["only-include"]:
        assert (ROOT / item).exists(), item
        assert any(item == s or item.startswith(s + "/") for s in sources), item
    assert all(v == "kpi_evidence" or v.startswith("kpi_evidence/") for v in sources.values())


def test_stage_items_carry_the_package_text_and_license(br):
    items = br.stage_items(ROOT)
    assert "PACKAGE.md" in items and "LICENSE" in items
    assert [i for i in items if not (ROOT / i).exists()] == []


def test_install_text_recommends_requirements_txt(br, tmp_path):
    plan = br.plan_release(ROOT, tmp_path)
    text = br.install_text(plan, ["kpi_evidence-0.0.1-py3-none-any.whl", "reconloop-0.0.2-py3-none-any.whl"],
                           {("3.12", "x86_64"): []})
    base = f"/Volumes/<catalog>/<schema>/<volume>/kpi_evidence_{plan.version}"
    first = text.index(f"%pip install -r {base}/requirements.txt")
    offline = text.index(f"%pip install -r {base}/requirements-offline.txt")
    assert first < offline
    assert "full pinned environment" in text and "clean or serverless compute" in text
    assert f"set its `wheels` widget to `{base}/`" in text


def test_demo_data_fallback_follows_the_wheel_sources():
    from kpi_evidence import settings

    platform = settings._wheel_source(ROOT, "kpi_evidence/_demo/platform")
    reports = settings._wheel_source(ROOT, "kpi_evidence/_demo/reports")
    assert (platform / "fact_source_a.csv").is_file()
    assert reports == ROOT / "samples" / "reports"
    assert settings._wheel_source(ROOT, "other_package/data") is None
    assert "insight_harness" not in Path(settings.__file__).read_text(encoding="utf-8")


def test_vendor_manifest_is_neutral():
    path = ROOT / "src" / "kpi_evidence" / "extract" / "_vendor" / "VENDOR.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert set(manifest) == {"package", "upstream", "commit", "file", "upstream_version", "upstream_sha256",
                             "sha256", "policy"}
    assert not re.search(r"tests/|scripts/|author|LICENSE", manifest["policy"])


def test_registry_readme_is_user_facing():
    text = (ROOT / "registry" / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.S)
    assert blocks and not [line for block in blocks for line in block.splitlines() if "#" in line]
    assert not re.search(r"\bapp\b|Setup page|the spec|verified when|docs/|samples/KPI_TABLE", text)
