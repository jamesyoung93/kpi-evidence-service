from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JOBS_FILE = ROOT / "resources" / "jobs.yml"
DEPLOY_GUIDE = ROOT / "docs" / "DEPLOY.md"
NOTEBOOK_README = ROOT / "notebooks" / "README.md"
REPORTS_VOLUME = "/Volumes/${var.catalog}/${resources.schemas.kpi_evidence.name}/${resources.volumes.reports.name}"
WHEELS_VOLUME = "/Volumes/${var.catalog}/${resources.schemas.kpi_evidence.name}/${resources.volumes.wheels.name}"
CHECK_WIDGETS = ["reports", "kpis", "out_dir", "brand", "catalog", "gov_schema", "ai_mode", "wheels"]
CHECK_ENTRY_ARGS = {"reports", "kpis", "out_dir", "catalog", "gov_schema", "brand", "ai_mode"}
_WIDGET = re.compile(r"dbutils\.widgets\.(?:text|dropdown|combobox|multiselect)\(\s*(?:name\s*=\s*)?[\"']([A-Za-z0-9_]+)[\"']")


@pytest.fixture(scope="module")
def publish_script():
    spec = importlib.util.spec_from_file_location("kpi_publish_wheels_script", ROOT / "scripts" / "publish_wheels.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["kpi_publish_wheels_script"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def check_job(bundle) -> dict:
    return bundle.jobs()["kpi_check"]


@pytest.fixture()
def wheels_job(bundle) -> dict:
    return bundle.jobs()["kpi_wheels"]


def test_check_job_is_one_serverless_notebook_task(check_job):
    (task,) = check_job["tasks"]
    assert task["task_key"] == "check"
    nb = task["notebook_task"]
    assert nb["notebook_path"] == "../notebooks/check_reports.py"
    for key in ("environment_key", "job_cluster_key", "existing_cluster_id", "new_cluster", "libraries"):
        assert key not in task, key
    assert "environments" not in check_job, "only the optional wheel variant needs an environment"
    assert task["max_retries"] == 0 and task["timeout_seconds"] > 0


def test_check_job_parameters_and_widget_mapping(check_job):
    params = {p["name"]: p["default"] for p in check_job["parameters"]}
    assert list(params) == CHECK_WIDGETS
    base = check_job["tasks"][0]["notebook_task"]["base_parameters"]
    assert list(base) == CHECK_WIDGETS
    for name, value in base.items():
        assert value == f"{{{{job.parameters.{name}}}}}", name
    assert params["reports"] == f"{REPORTS_VOLUME}/check/reports/"
    assert params["out_dir"] == f"{REPORTS_VOLUME}/check/out/"
    assert params["kpis"] == "" and params["brand"] == ""
    assert params["ai_mode"] == "off"
    assert params["catalog"] == "${var.catalog}"
    assert params["gov_schema"] == "${resources.schemas.kpi_gov.name}"
    assert params["wheels"] == f"{WHEELS_VOLUME}/requirements.txt"


def test_check_job_schedule_is_paused_and_cheap(check_job):
    sched = check_job["schedule"]
    assert sched["pause_status"] == "PAUSED"
    assert sched["timezone_id"] == "UTC" and sched["quartz_cron_expression"].count(" ") == 5
    assert check_job["max_concurrent_runs"] == 1
    assert check_job["performance_target"] == "STANDARD"


def test_wheel_variant_block(check_job, kit):
    variant = kit.optional_block("check_wheel_variant")
    assert set(variant) == {"environments", "tasks"}
    (task,) = variant["tasks"]
    assert task["task_key"] == "check"
    assert task["environment_key"] == variant["environments"][0]["environment_key"]
    wt = task["python_wheel_task"]
    assert wt["package_name"] == "kpi_evidence" and wt["entry_point"] == "kpi-evidence-check"
    named = wt["named_parameters"]
    assert set(named) == CHECK_ENTRY_ARGS | {"run_id"}
    job_params = {p["name"] for p in check_job["parameters"]}
    for name in CHECK_ENTRY_ARGS:
        assert named[name] == f"{{{{job.parameters.{name}}}}}" and name in job_params, name


def test_deploy_guide_explains_the_default_choice():
    text = DEPLOY_GUIDE.read_text(encoding="utf-8")
    section = text.split("## 7.", 1)[1].split("## 8.", 1)[0]
    for phrase in ("Why a notebook task is the default", "check_wheel_variant", "run_as", "kpi_wheels",
                   "resources/parked/optional.yml"):
        assert phrase in section, phrase


def test_wheels_job_copies_from_the_artifact_path_into_the_wheels_volume(wheels_job):
    (task,) = wheels_job["tasks"]
    spt = task["spark_python_task"]
    assert spt["python_file"] == "../scripts/publish_wheels.py"
    assert (JOBS_FILE.parent / spt["python_file"]).resolve().is_file()
    assert spt["parameters"] == ["copy", "--source", "{{job.parameters.source}}", "--dest", "{{job.parameters.dest}}"]
    params = {p["name"]: p["default"] for p in wheels_job["parameters"]}
    assert params == {"source": "${workspace.artifact_path}/.internal", "dest": WHEELS_VOLUME}
    envs = {e["environment_key"]: e for e in wheels_job["environments"]}
    assert task["environment_key"] in envs


def test_check_job_installs_from_where_kpi_wheels_publishes(check_job, wheels_job, publish_script):
    dest = {p["name"]: p["default"] for p in wheels_job["parameters"]}["dest"]
    wheels = {p["name"]: p["default"] for p in check_job["parameters"]}["wheels"]
    assert wheels == f"{dest}/{publish_script.REQUIREMENTS}"


def test_wheels_job_arguments_parse_in_the_script(wheels_job, publish_script, tmp_path, capsys):
    source, dest = tmp_path / "artifacts" / ".internal", tmp_path / "Volumes" / "wheels"
    source.mkdir(parents=True)
    for name in ("reconloop-0.5.0+20261019120000-py3-none-any.whl", "kpi_evidence-0.1.0+20261019120000-py3-none-any.whl"):
        (source / name).write_bytes(b"wheel")
    values = {"{{job.parameters.source}}": str(source), "{{job.parameters.dest}}": str(dest)}
    argv = [values.get(a, a) for a in wheels_job["tasks"][0]["spark_python_task"]["parameters"]]
    assert publish_script.main(argv) == 0
    assert sorted(p.name for p in dest.iterdir()) == [
        "kpi_evidence-0.1.0+20261019120000-py3-none-any.whl", "reconloop-0.5.0+20261019120000-py3-none-any.whl",
        "requirements.txt"]
    assert "%pip install -r" in capsys.readouterr().out


def _notebook_text(kit) -> str:
    assert kit.notebook.exists(), "notebooks/check_reports.py is missing"
    return kit.notebook.read_text(encoding="utf-8")


def test_notebook_defines_every_widget_the_job_passes(kit):
    text = _notebook_text(kit)
    assert text.startswith("# Databricks notebook source"), "Databricks source format, so sync imports it as a notebook"
    widgets = set(_WIDGET.findall(text))
    missing = [w for w in CHECK_WIDGETS if w not in widgets]
    assert not missing, (f"kpi_check passes {missing} but the notebook defines no widget for them "
                         f"(widgets found: {sorted(widgets)}); align resources/jobs.yml and the notebook")


def test_notebook_installs_the_package_with_pip(kit):
    text = _notebook_text(kit)
    assert "%pip install" in text or "pip install" in text, "the notebook must install the wheels itself"


def test_notebook_readme_covers_import_widgets_and_outputs():
    text = NOTEBOOK_README.read_text(encoding="utf-8")
    for widget in CHECK_WIDGETS:
        assert f"`{widget}`" in text, widget
    for phrase in ("# Databricks notebook source", "Import", "%pip install -r", "out_dir", "kpi_check",
                   "save_html", "save_excel", "persist=True"):
        assert phrase in text, phrase


def test_wheel_variant_parameters_drive_check_entry(check_job, kit, monkeypatch, tmp_path):
    from kpi_evidence import check as check_module
    from kpi_evidence import cli
    from kpi_evidence.store import databricks_store

    calls: dict = {}

    class FakeResult:
        documents = comparisons = kpis = issues = None

        def save_html(self, path):
            Path(path).write_text("<html></html>", encoding="utf-8")
            return path

        def save_excel(self, path):
            Path(path).write_bytes(b"xlsx")
            return path

    class FakeCheck:
        def __init__(self, spark=None, **kwargs):
            calls["init"] = dict(kwargs, spark=spark)

        def add_reports(self, sources, **_kw):
            calls["reports"] = sources

        def set_kpis(self, table, **_kw):
            calls["kpis"] = table

        def run(self, **_kw):
            return FakeResult()

    monkeypatch.setattr(check_module, "ReportCheck", FakeCheck)
    monkeypatch.setattr(databricks_store, "active_spark", lambda: None)
    out_dir = tmp_path / "out"
    values = {"reports": "/Volumes/c/s/reports/check/reports/", "kpis": "/Volumes/c/s/reports/check/kpi_table.csv",
              "out_dir": str(out_dir), "brand": "", "catalog": "c", "gov_schema": "kpi_gov", "ai_mode": "off",
              "wheels": "/Volumes/c/s/wheels/requirements.txt"}
    assert set(values) == {p["name"] for p in check_job["parameters"]}
    named = kit.optional_block("check_wheel_variant")["tasks"][0]["python_wheel_task"]["named_parameters"]
    rendered = {k: re.sub(r"\{\{job\.parameters\.(\w+)\}\}", lambda m: values[m.group(1)], v) for k, v in named.items()}
    rendered["run_id"] = "123"
    argv = [f"--{k}={v}" for k, v in rendered.items()]
    argv += [f"--{k}={v}" for k, v in values.items() if k not in rendered]
    assert cli.check_entry(argv) == 0
    assert calls["reports"] == ["/Volumes/c/s/reports/check/reports/"]
    assert calls["kpis"] == "/Volumes/c/s/reports/check/kpi_table.csv"
    init = calls["init"]
    assert init["catalog"] == "c" and init["gov_schema"] == "kpi_gov" and init["ai_mode"] == "off"
    assert init["brand"] is None
    written = sorted(p.suffix for p in out_dir.iterdir())
    assert written == [".html", ".xlsx"]
