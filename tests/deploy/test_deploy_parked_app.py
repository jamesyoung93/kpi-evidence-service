from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

from kpi_evidence.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "apps" / "insight_harness"
PARKED = ROOT / "resources" / "parked"
PARKED_APP = PARKED / "app.yml.disabled"
PARKED_CONSUME = PARKED / "consume.yml.disabled"
PARKED_APP_YAML = PARKED / "insight_harness.app.yaml"
APP_ONLY_ENV = {"KPI_EVIDENCE_REVIEW_ENABLED"}
RESOURCE_TYPES = {"sql_warehouse", "uc_securable", "job", "genie_space", "experiment", "secret",
                  "serving_endpoint", "postgres", "database"}


@pytest.fixture(scope="module")
def build_script():
    spec = importlib.util.spec_from_file_location("kpi_parked_build_app_bundle",
                                                  ROOT / "scripts" / "parked" / "build_app_bundle.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["kpi_parked_build_app_bundle"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def app_yaml() -> dict:
    return yaml.safe_load(PARKED_APP_YAML.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def parked_resources() -> dict:
    return yaml.safe_load(PARKED_APP.read_text(encoding="utf-8"))["resources"]


@pytest.fixture()
def app(parked_resources) -> dict:
    return parked_resources["apps"]["kpi_review"]


def _bundle_env(app) -> dict[str, tuple[str, str]]:
    out = {}
    for e in app["config"]["env"]:
        kind = "value_from" if "value_from" in e else "value"
        assert set(e) == {"name", kind}, e
        out[e["name"]] = (kind, e[kind])
    return out


def _app_yaml_env(app_yaml) -> dict[str, tuple[str, str]]:
    out = {}
    for e in app_yaml["env"]:
        kind = "valueFrom" if "valueFrom" in e else "value"
        assert set(e) == {"name", kind}, e
        out[e["name"]] = ("value_from" if kind == "valueFrom" else "value", e[kind])
    return out


def test_app_yaml_command_and_env_match_the_bundle(app_yaml, app):
    assert app_yaml["command"] == ["streamlit", "run", "app.py"] == app["config"]["command"]
    bundle_env, yaml_env = _bundle_env(app), _app_yaml_env(app_yaml)
    assert list(bundle_env) == list(yaml_env), "same env names in the same order"
    for name, (kind, value) in bundle_env.items():
        assert yaml_env[name][0] == kind, name
        if kind == "value_from":
            assert yaml_env[name][1] == value, f"{name}: valueFrom {yaml_env[name][1]} != {value}"


def test_value_from_keys_are_app_resource_names(app_yaml, app):
    names = [r["name"] for r in app["resources"]]
    assert len(names) == len(set(names))
    refs = {v for kind, v in _bundle_env(app).values() if kind == "value_from"}
    refs |= {e["valueFrom"] for e in app_yaml["env"] if "valueFrom" in e}
    assert refs <= set(names), refs - set(names)
    assert set(names) - refs == {"ai-model"}


def test_app_resources(app):
    res = {r["name"]: r for r in app["resources"]}
    for r in res.values():
        kinds = set(r) & RESOURCE_TYPES
        assert len(kinds) == 1, r
    assert res["sql-warehouse"]["sql_warehouse"] == {"id": "${var.warehouse_id}", "permission": "CAN_USE"}
    vol = res["reports-volume"]["uc_securable"]
    assert vol["securable_type"] == "VOLUME" and vol["permission"] == "WRITE_VOLUME"
    assert vol["securable_full_name"].endswith(".${resources.volumes.reports.name}")
    assert res["pipeline-job"]["job"] == {"id": "${resources.jobs.kpi_pipeline.id}", "permission": "CAN_MANAGE_RUN"}
    genie = res["genie-space"]["genie_space"]
    assert genie["space_id"] == "${resources.genie_spaces.kpi_published.id}" and genie["permission"] == "CAN_RUN"
    model = res["ai-model"]["uc_securable"]
    assert model == {"securable_type": "MODEL_SERVICE", "securable_full_name": "${var.ai_model}", "permission": "EXECUTE"}
    assert not any("serving_endpoint" in r for r in res.values()), "Unity Gateway: no serving_endpoint resource"
    exp = res["mlflow-experiment"]["experiment"]
    assert exp == {"experiment_id": "${resources.experiments.kpi_evidence.id}", "permission": "CAN_EDIT"}


def test_parked_app_declares_only_the_app(parked_resources):
    assert set(parked_resources) == {"apps"}, "the MLflow experiment moved to resources/jobs.yml"


def test_parked_references_resolve_after_switching_on(bundle, kit):
    import re

    declared = set(bundle.variables()) | set(kit.optional_block("parked_variables"))
    active = {(t, k) for t, items in bundle.resources().items() for k in items}
    parked = set()
    for path in (PARKED_APP, PARKED_CONSUME):
        for rtype, items in yaml.safe_load(path.read_text(encoding="utf-8"))["resources"].items():
            parked |= {(rtype, key) for key in items}
    assert parked == {("apps", "kpi_review"), ("dashboards", "kpi_coverage"), ("genie_spaces", "kpi_published")}
    for path in (PARKED_APP, PARKED_CONSUME):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for where, text in kit.iter_strings(doc):
            for name in re.findall(r"\$\{var\.([A-Za-z0-9_]+)\}", text):
                assert name in declared, f"{path.name}:{where} uses ${{var.{name}}}"
            for rtype, key in re.findall(r"\$\{resources\.([a-z_]+)\.([A-Za-z0-9_]+)\.", text):
                assert (rtype, key) in active | parked, f"{path.name}:{where} -> resources.{rtype}.{key}"
            if where.endswith(("file_path", "source_code_path")):
                assert (ROOT / "resources" / text).resolve().exists(), f"{path.name}:{where} -> {text}"


def test_identity_and_permissions(app):
    assert app["user_api_scopes"] == ["genie"]
    assert app["source_code_path"] == "../apps/insight_harness"
    assert (ROOT / "resources" / app["source_code_path"]).resolve() == APP_DIR.resolve()
    levels = {p["group_name"]: p["level"] for p in app["permissions"]}
    assert levels == {"${var.admin_group}": "CAN_MANAGE", "${var.consumer_group}": "CAN_USE"}


def test_kpi_evidence_env_names_are_settings_fields(app):
    fields = {f.name for f in dataclasses.fields(Settings)}
    env = _bundle_env(app)
    for name in env:
        if name.startswith("KPI_EVIDENCE_") and name not in APP_ONLY_ENV:
            field = name.removeprefix("KPI_EVIDENCE_").lower()
            assert field in fields, f"{name} does not map to Settings.{field}"
    assert env["KPI_EVIDENCE_BACKEND"] == ("value", "databricks")
    assert env["INSIGHT_HARNESS_BACKEND"] == ("value", "databricks")
    assert env["KPI_EVIDENCE_WAREHOUSE_ID"] == ("value_from", "sql-warehouse")
    assert env["KPI_EVIDENCE_VOLUME_PATH"] == ("value_from", "reports-volume")
    assert env["KPI_EVIDENCE_SCHEMA"] == ("value", "${resources.schemas.kpi_evidence.name}")


def test_settings_accept_the_app_yaml_environment(app_yaml, monkeypatch):
    for e in app_yaml["env"]:
        monkeypatch.setenv(e["name"], str(e.get("value", f"resolved-{e.get('valueFrom')}")))
    s = Settings.from_env()
    assert s.backend == "databricks" and not s.is_local
    assert s.schema == "kpi_evidence" and s.gov_schema == "kpi_gov"
    assert s.ai_model.startswith("system.ai.")
    assert s.resolved_volume_path() == "resolved-reports-volume"


def test_parked_build_script_still_finds_the_project_root(build_script):
    assert build_script.ROOT == ROOT
    assert build_script.MARKER.startswith("# Generated by scripts/parked/build_app_bundle.py")

UPSTREAM = ["streamlit>=1.50,<2", "pandas>=2.2,<4", "numpy>=1.26,<3", "altair>=5.0,<7", "anthropic>=0.40,<1",
            "streamlit-sortables==0.3.1"]


def test_compose_requirements_pins_once_and_lists_wheels_last(build_script):
    wheels = ["reconloop-0.5.0-py3-none-any.whl", "kpi_evidence-0.1.0-py3-none-any.whl"]
    lines = build_script.compose_requirements(UPSTREAM + ["# comment", "", "Streamlit_Sortables==0.3.1"], wheels,
                                              wheel_hashes={wheels[0]: "ab" * 32})
    assert lines[0].startswith(build_script.MARKER)
    reqs = [l for l in lines if not l.startswith("#")]
    names = [build_script.requirement_name(l) for l in reqs if build_script.requirement_name(l)]
    assert len(names) == len(set(names)), names
    for pin in ("streamlit==1.59.1", "pandas==3.0.3", "altair==6.2.2", "databricks-sdk>=0.72"):
        assert pin in reqs
    assert "numpy>=1.26,<3" in reqs
    assert reqs[-2:] == [f"./vendor_wheels/{w}" for w in wheels]
    assert f"# {wheels[0]} sha256={'ab' * 32}" in lines
    assert "mlflow-skinny>=3.4" not in reqs
    assert "mlflow-skinny>=3.4" in build_script.compose_requirements(UPSTREAM, wheels, with_mlflow=True)


def test_compose_from_the_real_upstream_file(build_script):
    text = build_script.upstream_requirements(APP_DIR)
    lines = build_script.compose_requirements(text.splitlines(), ["x-1-py3-none-any.whl"])
    assert "streamlit==1.59.1" in lines and "pandas==3.0.3" in lines
    assert not any(l.startswith("numpy==2.5") for l in lines)


def test_requirement_name(build_script):
    rn = build_script.requirement_name
    assert rn("Databricks_SDK>=0.72") == "databricks-sdk"
    assert rn("streamlit==1.59.1 ; python_version>='3.11'") == "streamlit"
    assert rn("./vendor_wheels/x.whl") is None and rn("-r other.txt") is None and rn("# x") is None


def _fake_app(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    app_dir = root / "apps" / "insight_harness"
    app_dir.mkdir(parents=True)
    (app_dir / "requirements.txt").write_text("\n".join(UPSTREAM) + "\n", encoding="utf-8")
    (root / "libs" / "reconloop").mkdir(parents=True)
    return root


def _fake_runner(calls):
    def run(cmd):
        calls.append(list(cmd))
        out_dir = Path(cmd[cmd.index("--out-dir") + 1] if "--out-dir" in cmd else cmd[cmd.index("-w") + 1])
        project = Path(cmd[-1])
        name = "reconloop-0.5.0" if project.name == "reconloop" else "kpi_evidence-0.1.0"
        (out_dir / f"{name}-py3-none-any.whl").write_bytes(b"wheel " + name.encode())
        (project / "build").mkdir(exist_ok=True)
    return run


def test_main_builds_wheels_and_installs_reversibly(build_script, tmp_path, monkeypatch):
    monkeypatch.setattr(build_script.shutil, "which", lambda name: "uv")
    root = _fake_app(tmp_path)
    app_dir = root / "apps" / "insight_harness"
    upstream_text = (app_dir / "requirements.txt").read_text(encoding="utf-8")
    stale = app_dir / "vendor_wheels"
    stale.mkdir()
    (stale / "kpi_evidence-0.0.9-py3-none-any.whl").write_bytes(b"old")
    calls: list[list[str]] = []

    assert build_script.main(["--root", str(root)], runner=_fake_runner(calls)) == 0
    assert [c[:3] for c in calls] == [["uv", "build", "--wheel"]] * 2
    assert Path(calls[0][-1]).name == "reconloop" and Path(calls[1][-1]) == root.resolve()
    wheels = sorted(p.name for p in stale.glob("*.whl"))
    assert wheels == ["kpi_evidence-0.1.0-py3-none-any.whl", "reconloop-0.5.0-py3-none-any.whl"]
    assert not (root / "libs" / "reconloop" / "build").exists(), "build debris removed from the vendored tree"
    generated = (app_dir / "requirements-databricks.txt").read_text(encoding="utf-8").splitlines()
    assert [l for l in generated if l.startswith("./")] == [
        "./vendor_wheels/reconloop-0.5.0-py3-none-any.whl", "./vendor_wheels/kpi_evidence-0.1.0-py3-none-any.whl"]
    assert (app_dir / "requirements.txt").read_text(encoding="utf-8") == upstream_text, "untouched without --install"

    for _ in range(2):
        assert build_script.main(["--root", str(root), "--install"], runner=_fake_runner(calls)) == 0
        installed = (app_dir / "requirements.txt").read_text(encoding="utf-8")
        assert installed.startswith(build_script.MARKER)
        assert installed.count("streamlit==1.59.1") == 1
        assert (app_dir / "requirements-upstream.txt").read_text(encoding="utf-8") == upstream_text

    assert build_script.main(["--root", str(root), "--restore"]) == 0
    assert (app_dir / "requirements.txt").read_text(encoding="utf-8") == upstream_text
    assert not (app_dir / "requirements-upstream.txt").exists()
    assert build_script.main(["--root", str(root), "--restore"]) == 0


def test_refuses_to_treat_a_generated_file_as_upstream(build_script, tmp_path):
    root = _fake_app(tmp_path)
    app_dir = root / "apps" / "insight_harness"
    (app_dir / "requirements.txt").write_text(build_script.MARKER + "\nstreamlit==1.59.1\n", encoding="utf-8")
    with pytest.raises(build_script.BuildError, match="generated file"):
        build_script.upstream_requirements(app_dir)


def test_build_failures_are_explicit(build_script, tmp_path, monkeypatch):
    monkeypatch.setattr(build_script.shutil, "which", lambda name: None)
    root = _fake_app(tmp_path)
    vendor = root / "apps" / "insight_harness" / "vendor_wheels"
    vendor.mkdir()
    cmd_seen = []

    def no_output(cmd):
        cmd_seen.append(cmd)

    with pytest.raises(build_script.BuildError, match="expected one reconloop wheel"):
        build_script.build_wheel(root / "libs" / "reconloop", "reconloop", vendor, runner=no_output)
    assert cmd_seen[0][1:4] == ["-m", "pip", "wheel"] and "--no-deps" in cmd_seen[0]

    def broken(cmd):
        raise build_script.subprocess.CalledProcessError(1, cmd)

    with pytest.raises(build_script.BuildError, match="failed"):
        build_script.build_wheel(root / "libs" / "reconloop", "reconloop", vendor, runner=broken)
    with pytest.raises(build_script.BuildError, match="no wheel"):
        build_script.main(["--root", str(root), "--skip-build"])
