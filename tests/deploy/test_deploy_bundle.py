from __future__ import annotations

import copy
import fnmatch
import glob
import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RESOURCE_REF = re.compile(r"\$\{resources\.([a-z_]+)\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)\}")
VAR_REF = re.compile(r"\$\{var\.([A-Za-z0-9_]+)\}")
ANY_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)\}")
DYNAMIC_REF = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
ALLOWED_BUILTIN_REFS = {"bundle.name", "bundle.target", "workspace.root_path", "workspace.artifact_path",
                        "workspace.current_user.userName"}
COMMON_WHEEL_PARAMS = {"backend", "catalog", "schema", "gov_schema", "volume_path", "run_id"}
PIPELINE_ORDER = ["register", "list_ready", "extract", "process"]
EXPECTED_JOBS = {"kpi_wheels", "kpi_check", "kpi_bootstrap", "kpi_pipeline", "kpi_dq_and_eval"}
WHEEL_BLOCKS = ("check_wheel_variant", "parked_publication_tasks")
OPTIONAL_BLOCKS = ["parked_variables", "parked_dev_variables", "check_wheel_variant", "parked_bootstrap_parameters",
                   "file_arrival_trigger", "parked_publication_tasks"]
OPTIONAL_HOW = {"append to the list": list, "merge into the mapping": dict}
OPTIONAL_REPLACES = {"check_wheel_variant": {"tasks"}}

PARKED_ENTRY_POINTS = {"kpi-evidence-publish": "kpi_evidence.cli:publish_entry"}
JOB_ENTRY_POINTS = {"kpi-evidence-bootstrap", "kpi-evidence-register", "kpi-evidence-extract", "kpi-evidence-process",
                    "kpi-evidence-dq", "kpi-evidence-evaluate", "kpi-evidence-check"}


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _active_wheel_tasks(bundle, kit):
    for job_key, job in bundle.jobs().items():
        for task, parent in kit.iter_tasks(job):
            if "python_wheel_task" in task:
                yield job_key, task, parent


def _block_wheel_tasks(kit):
    check = kit.optional_block("check_wheel_variant")
    for task in check["tasks"]:
        yield "check_wheel_variant", task
    for task in kit.optional_block("parked_publication_tasks"):
        if "python_wheel_task" in task:
            yield "parked_publication_tasks", task


def _strip_comments_module():
    spec = importlib.util.spec_from_file_location("kpi_strip_comments_script", ROOT / "scripts" / "strip_comments.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["kpi_strip_comments_script"] = module
    spec.loader.exec_module(module)
    return module


def test_every_bundle_yaml_parses(bundle):
    assert set(bundle.files) == {"databricks.yml", "resources/uc.yml", "resources/jobs.yml"}
    for rel, doc in bundle.files.items():
        assert isinstance(doc, dict) and doc, rel


def test_bundle_yaml_has_no_comments(bundle, kit):
    strip = _strip_comments_module()
    for path in [ROOT / rel for rel in bundle.files] + [kit.optional_file]:
        text = path.read_text(encoding="utf-8")
        assert strip.rewrite_yaml(text).comments == 0, f"{path.relative_to(ROOT).as_posix()} has comments"


def test_bundle_header_engine_and_includes(bundle):
    cfg = bundle.config
    assert cfg["bundle"]["name"] == "kpi-evidence-service"
    assert cfg["bundle"]["engine"] == "direct"
    assert cfg["include"] == ["resources/*.yml"]
    for rel, doc in bundle.files.items():
        if rel != "databricks.yml":
            assert set(doc) == {"resources"}, rel


def test_parked_files_exist_parse_and_are_not_included(bundle, kit):
    parked = sorted(p.name for p in kit.parked_dir.iterdir() if p.is_file())
    assert parked == ["README.md", "app.yml.disabled", "consume.yml.disabled", "insight_harness.app.yaml",
                      "optional.yml"]
    included = {Path(p).resolve() for pattern in bundle.config["include"] for p in glob.glob(str(ROOT / pattern))}
    assert included == {(ROOT / rel).resolve() for rel in bundle.files if rel != "databricks.yml"}
    assert not any("parked" in p.parts for p in included), "a parked file is included"
    assert [name for name in parked if name.endswith(".yml")] == ["optional.yml"]
    assert not {"bundle", "include", "resources", "targets", "variables"} & set(kit.optional_blocks()), \
        "optional.yml must not read as bundle configuration"
    for name in ("app.yml.disabled", "consume.yml.disabled", "insight_harness.app.yaml"):
        doc = yaml.safe_load((kit.parked_dir / name).read_text(encoding="utf-8"))
        assert isinstance(doc, dict) and doc, name
        assert (kit.parked_dir / name).read_text(encoding="utf-8").startswith("# PARKED (2026-10-19)"), name
    assert not (ROOT / "apps" / "insight_harness" / "app.yaml").exists(), "app.yaml was moved to resources/parked/"


def test_include_globs_cannot_reach_the_parked_folder(bundle):
    for pattern in bundle.config["include"]:
        assert "**" not in pattern, "a recursive include would pick up resources/parked/"
        assert PurePosixPath("resources/jobs.yml").match(pattern)
        assert not PurePosixPath("resources/parked/app.yml").match(pattern)
        assert not PurePosixPath("resources/parked/optional.yml").match(pattern)


def test_no_app_genie_dashboard_or_model_binding_in_the_bundle(bundle, kit):
    base = bundle.resources()
    for rtype in ("apps", "genie_spaces", "dashboards", "serving_endpoints"):
        assert rtype not in base, f"{rtype} must stay parked"
        for target in bundle.config["targets"]:
            assert rtype not in bundle.target_resources(target), f"targets.{target} declares {rtype}"
    for rel, doc in bundle.files.items():
        for path, text in kit.iter_strings(doc):
            assert "MODEL_SERVICE" not in text, f"{rel}:{path}"
            assert "resources.apps." not in text and "service_principal_client_id" not in text, f"{rel}:{path}"
            assert "genie_spaces" not in text and "dashboards." not in text, f"{rel}:{path}"


def test_every_schedule_and_trigger_is_paused(bundle):
    for key, job in bundle.jobs().items():
        assert "trigger" not in job and "triggers" not in job and "continuous" not in job, \
            f"{key}: no trigger by default (nothing runs on its own)"
        if "schedule" in job:
            assert job["schedule"]["pause_status"] == "PAUSED", key


def test_variables(bundle, kit):
    v = bundle.variables()
    assert "default" not in v["catalog"]
    assert v["schema"]["default"] == "kpi_evidence"
    assert v["gov_schema"]["default"] == "kpi_gov"
    assert v["warehouse_id"]["lookup"] == {"warehouse": "${var.warehouse_name}"}
    assert v["ai_mode"]["default"] == "off"
    assert v["ai_model"]["default"].startswith("system.ai.")
    for name in ("notify_email", "admin_group", "user_group", "load_demo", "prod_run_as_sp", "platform_schema"):
        assert name in v, name
    parked = kit.optional_block("parked_variables")
    assert set(parked) == {"app_name", "reviewer_group", "consumer_group", "genie_title", "genie_description"}
    assert not set(parked) & set(v), "parked variables must stay out of databricks.yml"
    assert re.fullmatch(r"[a-z0-9-]{2,30}", parked["app_name"]["default"])
    dev = kit.optional_block("parked_dev_variables")
    assert set(dev) == {"app_name", "reviewer_group", "consumer_group"}
    assert re.fullmatch(r"[a-z0-9-]{2,30}", dev["app_name"]) and dev["app_name"] != parked["app_name"]["default"]
    assert dev["reviewer_group"] == dev["consumer_group"] == "users"
    assert not set(dev) & set(bundle.config["targets"]["dev"]["variables"])


def test_artifacts_build_both_wheels(bundle):
    arts = bundle.config["artifacts"]
    assert set(arts) == {"kpi_evidence_whl", "reconloop_whl"}
    for art in arts.values():
        assert art["type"] == "whl"
        assert art["build"] == "uv build --wheel"
        assert art["dynamic_version"] is True
        assert (ROOT / art["path"] / "pyproject.toml").is_file()
    assert arts["kpi_evidence_whl"]["path"] == "."
    assert arts["reconloop_whl"]["path"] == "libs/reconloop"


def test_targets(bundle):
    targets = bundle.config["targets"]
    assert set(targets) == {"dev", "prod"}
    dev, prod = targets["dev"], targets["prod"]
    assert dev["mode"] == "development" and dev["default"] is True
    assert prod["mode"] == "production"
    assert prod["workspace"]["root_path"].startswith("/Workspace/Shared/")
    assert prod["run_as"] == {"service_principal_name": "${var.prod_run_as_sp}"}
    assert "platform_demo" in bundle.target_resources("dev")["schemas"]
    assert "platform_demo" not in bundle.resources().get("schemas", {})
    assert "resources" not in prod
    assert bundle.variable_values("dev")["load_demo"] == "true"
    assert bundle.variable_values("prod")["load_demo"] == "false"
    assert bundle.variable_values("dev")["user_group"] == "users"


def test_uc_resources_and_grants(bundle):
    schemas = bundle.resources()["schemas"]
    assert set(schemas) == {"kpi_evidence", "kpi_gov"}
    assert bundle.resource("schemas", "kpi_evidence")["name"] == "${var.schema}"
    assert bundle.resource("schemas", "kpi_gov")["name"] == "${var.gov_schema}"
    volumes = bundle.resources()["volumes"]
    assert set(volumes) == {"reports", "wheels"}
    for key in volumes:
        vol = bundle.resource("volumes", key)
        assert vol["name"] == key and vol["volume_type"] == "MANAGED"
        assert vol["schema_name"] == "${resources.schemas.kpi_evidence.name}"

    def grants(rtype, key):
        return {g["principal"]: set(g["privileges"]) for g in bundle.resource(rtype, key).get("grants", [])}

    assert grants("schemas", "kpi_evidence")["${var.user_group}"] == {"USE_SCHEMA", "SELECT", "MODIFY"}
    assert grants("schemas", "kpi_gov")["${var.user_group}"] == {"USE_SCHEMA", "SELECT", "EXECUTE"}
    assert grants("volumes", "reports")["${var.user_group}"] == {"READ_VOLUME", "WRITE_VOLUME"}
    assert grants("volumes", "wheels") == {"${var.user_group}": {"READ_VOLUME"}}
    for key in ("kpi_evidence", "kpi_gov"):
        assert grants("schemas", key)["${var.admin_group}"] == {"ALL_PRIVILEGES"}


def _block_trees(kit):
    return {name: kit.optional_block(name) for name in
            ("check_wheel_variant", "file_arrival_trigger", "parked_publication_tasks", "parked_bootstrap_parameters")}


def test_var_references_are_declared(bundle, kit):
    declared = set(bundle.variables())
    for rel, doc in bundle.files.items():
        for path, text in kit.iter_strings(doc):
            for name in VAR_REF.findall(text):
                assert name in declared, f"{rel}:{path} uses undeclared ${{var.{name}}}"
    parked_vars = set(kit.optional_block("parked_variables"))
    for block, tree in _block_trees(kit).items():
        for path, text in kit.iter_strings(tree):
            for name in VAR_REF.findall(text):
                assert name in declared | parked_vars, f"block {block}:{path} uses unknown ${{var.{name}}}"


def test_only_known_reference_namespaces(bundle, kit):
    trees = dict(bundle.files, **{f"block:{k}": v for k, v in _block_trees(kit).items()})
    for rel, doc in trees.items():
        for path, text in kit.iter_strings(doc):
            for ref in ANY_REF.findall(text):
                ok = ref.startswith(("var.", "resources.")) or ref in ALLOWED_BUILTIN_REFS
                assert ok, f"{rel}:{path} has unexpected reference ${{{ref}}}"


def test_resource_references_resolve(bundle, kit):
    base = {(t, k) for t, items in bundle.resources().items() for k in items}
    for rel, doc in bundle.files.items():
        for path, text in kit.iter_strings(doc.get("resources") or {}):
            for rtype, key, fld in RESOURCE_REF.findall(text):
                assert (rtype, key) in base, f"{rel}:{path} -> resources.{rtype}.{key} is not a base resource"
                assert fld in {"id", "name"}, f"{rel}:{path} -> unexpected field {fld}"
    for target, body in bundle.config["targets"].items():
        local = {(t, k) for t, items in bundle.target_resources(target).items() for k in items}
        for path, text in kit.iter_strings(body.get("resources") or {}):
            for rtype, key, _fld in RESOURCE_REF.findall(text):
                assert (rtype, key) in base | local, f"targets.{target}.{path} -> resources.{rtype}.{key}"
    for block in ("check_wheel_variant", "file_arrival_trigger"):
        for path, text in kit.iter_strings(_block_trees(kit)[block]):
            for rtype, key, _fld in RESOURCE_REF.findall(text):
                assert (rtype, key) in base, f"block {block}:{path} -> resources.{rtype}.{key}"


def test_target_job_overrides_match_base_tasks(bundle):
    jobs = bundle.jobs()
    for target in bundle.config["targets"]:
        for key, override in bundle.target_resources(target).get("jobs", {}).items():
            assert key in jobs, f"targets.{target} overrides unknown job {key}"
            base_keys = {t["task_key"] for t in jobs[key].get("tasks", [])}
            for task in override.get("tasks", []):
                assert task["task_key"] in base_keys, f"{target}: {key}.{task['task_key']} is not a base task"


def test_principals_are_not_repeated_per_resource(bundle):
    for target in bundle.config["targets"]:
        for rtype, items in bundle.resources().items():
            for key, (_rel, body) in items.items():
                for list_key, who in (("permissions", "group_name"), ("grants", "principal")):
                    entries = body.get(list_key) or []
                    names = [bundle.resolve_vars(e[who], target) for e in entries if who in e]
                    assert len(names) == len(set(names)), f"{target}: {rtype}.{key}.{list_key} repeats {names}"


def test_optional_blocks_file_shape(bundle, kit):
    blocks = kit.optional_blocks()
    assert list(blocks) == OPTIONAL_BLOCKS
    for name, entry in blocks.items():
        assert set(entry) == {"target", "how", "content"}, name
        assert entry["how"] in OPTIONAL_HOW, f"{name}: how = {entry['how']!r}"
        rel, node = kit.optional_target(bundle.files, entry["target"])
        expected = OPTIONAL_HOW[entry["how"]]
        assert isinstance(node, expected), f"{name}: {entry['target']} is not a {expected.__name__}"
        assert isinstance(entry["content"], expected) and entry["content"], f"{name}: content must be a {expected.__name__}"
        if expected is dict:
            clash = set(entry["content"]) & set(node)
            assert clash == OPTIONAL_REPLACES.get(name, set()), f"{name}: {sorted(clash)} already set at the target"
    targets = {name: entry["target"] for name, entry in blocks.items()}
    assert targets["parked_variables"] == "databricks.yml: variables"
    assert targets["parked_dev_variables"] == "databricks.yml: targets.dev.variables"
    assert targets["check_wheel_variant"] == "resources/jobs.yml: resources.jobs.kpi_check"
    assert targets["parked_bootstrap_parameters"] == \
        "resources/jobs.yml: resources.jobs.kpi_bootstrap.tasks[task_key=bootstrap].python_wheel_task.named_parameters"
    assert targets["file_arrival_trigger"] == "resources/jobs.yml: resources.jobs.kpi_pipeline"
    assert targets["parked_publication_tasks"] == "resources/jobs.yml: resources.jobs.kpi_pipeline.tasks"


def test_optional_blocks_apply_to_valid_jobs(bundle, kit):
    files = copy.deepcopy(bundle.files)
    for entry in kit.optional_blocks().values():
        _rel, node = kit.optional_target(files, entry["target"])
        content = copy.deepcopy(entry["content"])
        if entry["how"] == "append to the list":
            node.extend(content)
        else:
            node.update(content)
    config = files["databricks.yml"]
    declared = set(config["variables"])
    assert set(config["targets"]["dev"]["variables"]) <= declared
    for rel, doc in files.items():
        for path, text in kit.iter_strings(doc):
            for name in VAR_REF.findall(text):
                assert name in declared, f"switched on, {rel}:{path} uses undeclared ${{var.{name}}}"
    jobs = files["resources/jobs.yml"]["resources"]["jobs"]
    for job_key, job in jobs.items():
        keys = [t["task_key"] for t in job["tasks"]]
        assert len(keys) == len(set(keys)), f"switched on, {job_key} repeats a task_key"
        envs = {e["environment_key"] for e in job.get("environments", [])}
        params = {p["name"] for p in job.get("parameters", [])}
        sql_tasks = {t["task_key"] for t in job["tasks"] if "sql_task" in t}
        for task, parent in kit.iter_tasks(job):
            for dep in task.get("depends_on", []):
                assert dep["task_key"] in keys, f"switched on, {job_key}.{task['task_key']} depends on {dep['task_key']}"
            if "environment_key" in task:
                assert task["environment_key"] in envs, f"switched on, {job_key}.{task['task_key']}"
            _check_dynamic_refs(job_key, task, parent, params, sql_tasks)
    (check,) = jobs["kpi_check"]["tasks"]
    assert "python_wheel_task" in check and "notebook_task" not in check
    pipeline = jobs["kpi_pipeline"]
    assert [t["task_key"] for t in pipeline["tasks"]] == PIPELINE_ORDER + ["publish", "refresh_dashboard"]
    assert pipeline["trigger"]["pause_status"] == "UNPAUSED"
    bootstrap = next(t for t in jobs["kpi_bootstrap"]["tasks"] if t["task_key"] == "bootstrap")
    assert {"app_sp", "genie_space_id"} <= set(bootstrap["python_wheel_task"]["named_parameters"])


def test_expected_jobs(bundle):
    assert set(bundle.jobs()) == EXPECTED_JOBS
    for key, job in bundle.jobs().items():
        levels = {p["group_name"]: p["level"] for p in job["permissions"]}
        assert levels.get("${var.admin_group}") == "CAN_MANAGE", key
        assert levels.get("${var.user_group}") == "CAN_VIEW", key
        assert job["tags"]["project"] == "kpi-evidence-service", key
        assert job["email_notifications"]["on_failure"] == ["${var.notify_email}"], key
        assert job["timeout_seconds"] > 0, key


def test_wheel_entry_points_exist_in_pyproject_or_are_parked(bundle, kit):
    project = _pyproject()["project"]
    scripts = project["scripts"]
    seen = set()
    tasks = [(job_key, task) for job_key, task, _p in _active_wheel_tasks(bundle, kit)]
    tasks += list(_block_wheel_tasks(kit))
    for where, task in tasks:
        wt = task["python_wheel_task"]
        assert wt["package_name"] == project["name"] == "kpi_evidence"
        entry = wt["entry_point"]
        if entry in scripts:
            assert scripts[entry].startswith("kpi_evidence.cli:"), entry
        else:
            assert entry in PARKED_ENTRY_POINTS, f"{where}.{task['task_key']}: {entry} not in [project.scripts]"
            assert where == "parked_publication_tasks", f"{entry} is parked but used by {where}"
        assert "parameters" not in wt, "use named_parameters (key-value) so job parameters push down"
        seen.add(entry)
    assert seen == JOB_ENTRY_POINTS | set(PARKED_ENTRY_POINTS)
    for entry in JOB_ENTRY_POINTS:
        assert scripts[entry] == f"kpi_evidence.cli:{entry.removeprefix('kpi-evidence-')}_entry", entry


def test_console_scripts_are_prefixed_and_skip_parked_modules():
    scripts = _pyproject()["project"]["scripts"]
    assert set(scripts) == JOB_ENTRY_POINTS | {"kpi-evidence"}
    assert scripts["kpi-evidence"] == "kpi_evidence.cli:main"
    assert not set(scripts) & set(PARKED_ENTRY_POINTS)
    assert not set(scripts.values()) & set(PARKED_ENTRY_POINTS.values())
    import importlib

    from kpi_evidence import cli

    for target in scripts.values():
        module, func = target.split(":")
        assert callable(getattr(importlib.import_module(module), func)), target
    assert callable(getattr(cli, PARKED_ENTRY_POINTS["kpi-evidence-publish"].split(":")[1]))


def test_wheel_tasks_have_environment_and_common_parameters(bundle, kit):
    for job_key, task, _parent in _active_wheel_tasks(bundle, kit):
        envs = {e["environment_key"]: e for e in bundle.jobs()[job_key]["environments"]}
        assert task["environment_key"] in envs, f"{job_key}.{task['task_key']}: serverless wheel tasks need environment_key"
        params = task["python_wheel_task"]["named_parameters"]
        assert COMMON_WHEEL_PARAMS <= set(params), f"{job_key}.{task['task_key']} misses {COMMON_WHEEL_PARAMS - set(params)}"
        assert params["backend"] == "databricks"
        assert params["run_id"] == "{{job.run_id}}"
    for block, task in _block_wheel_tasks(kit):
        params = task["python_wheel_task"]["named_parameters"]
        assert params["run_id"] == "{{job.run_id}}", block
        if block == "parked_publication_tasks":
            assert COMMON_WHEEL_PARAMS <= set(params), block


def test_environments_are_serverless_v5(bundle, kit):
    for job_key, job in bundle.jobs().items():
        for env in job.get("environments", []):
            spec = env["spec"]
            assert spec["environment_version"] == "5", job_key
            deps = spec.get("dependencies", [])
            assert not any("pyspark" in d for d in deps), "pyspark breaks serverless sessions"
            if job_key != "kpi_wheels":
                assert "../dist/*.whl" in deps and "../libs/reconloop/dist/*.whl" in deps, job_key
    assert "dependencies" not in bundle.jobs()["kpi_wheels"]["environments"][0]["spec"], "stdlib only"
    variant_env = kit.optional_block("check_wheel_variant")["environments"][0]["spec"]
    assert variant_env["environment_version"] == "5"
    assert variant_env["dependencies"] == ["../libs/reconloop/dist/*.whl", "../dist/*.whl"]


def test_environment_wheel_globs_match_artifact_outputs(bundle):
    jobs_dir = bundle.declaring_dir("jobs", "kpi_pipeline")
    artifact_dists = {(ROOT / a["path"] / "dist").resolve() for a in bundle.config["artifacts"].values()}
    for job in bundle.jobs().values():
        for env in job.get("environments", []):
            for dep in env["spec"].get("dependencies", []):
                if dep.endswith(".whl"):
                    assert (jobs_dir / dep).parent.resolve() in artifact_dists, f"{dep} is not an artifact's dist/"


def _check_dynamic_refs(where, task, parent, params, sql_tasks):
    allowed_fixed = {"job.run_id", "job.trigger.file_arrival.location"}
    body = {k: v for k, v in task.items() if k != "for_each_task"}
    for text in _strings(body):
        for ref in DYNAMIC_REF.findall(text):
            if ref.startswith("job.parameters."):
                assert ref.split(".", 2)[2] in params, f"{where}.{task['task_key']}: {ref} not a job parameter"
            elif ref.startswith("input."):
                assert parent is not None, f"{where}.{task['task_key']}: {{{{input.*}}}} outside for_each"
            elif ref.startswith("tasks."):
                m = re.fullmatch(r"tasks\.([A-Za-z0-9_]+)\.output\.rows", ref)
                assert m and m.group(1) in sql_tasks, f"{where}: {ref}"
            else:
                assert ref in allowed_fixed, f"{where}.{task['task_key']}: unexpected {{{{{ref}}}}}"


def _strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def test_dynamic_value_references(bundle, kit):
    for job_key, job in bundle.jobs().items():
        params = {p["name"] for p in job.get("parameters", [])}
        sql_tasks = {t["task_key"] for t in job.get("tasks", []) if "sql_task" in t}
        for task, parent in kit.iter_tasks(job):
            _check_dynamic_refs(job_key, task, parent, params, sql_tasks)
            each = task.get("for_each_task")
            if each:
                m = re.fullmatch(r"\{\{tasks\.([A-Za-z0-9_]+)\.output\.rows\}\}", each["inputs"])
                assert m and m.group(1) in sql_tasks
    jobs = bundle.jobs()
    check_params = {p["name"] for p in jobs["kpi_check"]["parameters"]}
    for task in kit.optional_block("check_wheel_variant")["tasks"]:
        _check_dynamic_refs("check_wheel_variant", task, None, check_params, set())
    pipeline_params = {p["name"] for p in jobs["kpi_pipeline"]["parameters"]}
    for task in kit.optional_block("parked_publication_tasks"):
        _check_dynamic_refs("parked_publication_tasks", task, None, pipeline_params, set())


def test_depends_on_refer_to_existing_tasks(bundle):
    for job_key, job in bundle.jobs().items():
        keys = [t["task_key"] for t in job["tasks"]]
        assert len(keys) == len(set(keys)), job_key
        for task in job["tasks"]:
            for dep in task.get("depends_on", []):
                assert dep["task_key"] in keys, f"{job_key}.{task['task_key']} depends on unknown {dep['task_key']}"


def test_pipeline_dag_is_manual_and_publishes_nothing(bundle, kit):
    job = bundle.jobs()["kpi_pipeline"]
    tasks = {t["task_key"]: t for t in job["tasks"]}
    assert list(tasks) == PIPELINE_ORDER
    for prev, nxt in zip(PIPELINE_ORDER, PIPELINE_ORDER[1:]):
        assert tasks[nxt]["depends_on"] == [{"task_key": prev}], nxt
    assert "trigger" not in job and "schedule" not in job
    assert job["max_concurrent_runs"] == 1 and job["queue"] == {"enabled": True}
    assert tasks["register"]["python_wheel_task"]["entry_point"] == "kpi-evidence-register"
    assert tasks["list_ready"]["sql_task"]["file"]["path"] == "../src/sql/jobs/list_ready_documents.sql"
    each = tasks["extract"]["for_each_task"]
    assert each["inputs"] == "{{tasks.list_ready.output.rows}}" and each["concurrency"] == 4
    assert each["task"]["python_wheel_task"]["entry_point"] == "kpi-evidence-extract"
    assert each["task"]["python_wheel_task"]["named_parameters"]["doc_id"] == "{{input.doc_id}}"
    assert tasks["process"]["run_if"] == "ALL_DONE"
    for key in ("register", "list_ready", "process"):
        assert tasks[key]["max_retries"] >= 1, key
    params = {p["name"]: p["default"] for p in job["parameters"]}
    assert params["schema"] == "${resources.schemas.kpi_evidence.name}"
    assert params["ai_mode"] == "${var.ai_mode}" and params["ai_model"] == "${var.ai_model}"

    parked = {t["task_key"]: t for t in kit.optional_block("parked_publication_tasks")}
    assert list(parked) == ["publish", "refresh_dashboard"]
    pub = parked["publish"]
    assert pub["depends_on"] == [{"task_key": "process"}] and "run_if" not in pub
    assert pub["disable_auto_optimization"] is True and pub["max_retries"] == 0
    assert parked["refresh_dashboard"]["dashboard_task"]["dashboard_id"] == "${resources.dashboards.kpi_coverage.id}"
    assert parked["refresh_dashboard"]["run_if"] == "ALL_DONE"


def test_file_arrival_trigger_block(kit):
    trig = kit.optional_block("file_arrival_trigger")["trigger"]
    assert trig["pause_status"] == "UNPAUSED"
    fa = trig["file_arrival"]
    assert fa["url"] == "/Volumes/${var.catalog}/${resources.schemas.kpi_evidence.name}/${resources.volumes.reports.name}/inbox/"
    assert fa["min_time_between_triggers_seconds"] == 60 and fa["wait_after_last_change_seconds"] == 120


def test_bootstrap_job(bundle, kit):
    job = bundle.jobs()["kpi_bootstrap"]
    params = {p["name"]: p["default"] for p in job["parameters"]}
    assert params["load_demo"] == "${var.load_demo}" and params["seed_samples"] == "false"
    tasks = {t["task_key"]: t for t in job["tasks"]}
    named = tasks["bootstrap"]["python_wheel_task"]["named_parameters"]
    assert set(named) == COMMON_WHEEL_PARAMS | {"platform_schema", "load_demo", "seed_samples"}
    assert named["load_demo"] == "{{job.parameters.load_demo}}"
    verify = tasks["verify_list_ready"]
    assert verify["depends_on"] == [{"task_key": "bootstrap"}]
    assert verify["sql_task"]["file"]["path"] == "../src/sql/jobs/list_ready_documents.sql"
    dev_override = bundle.target_resources("dev")["jobs"]["kpi_bootstrap"]["tasks"][0]
    assert dev_override["python_wheel_task"]["named_parameters"]["platform_schema"] == \
        "${resources.schemas.platform_demo.name}"
    parked = kit.optional_block("parked_bootstrap_parameters")
    assert parked == {
        "app_sp": "${resources.apps.kpi_review.service_principal_client_id}",
        "admin_group": "${var.admin_group}", "reviewer_group": "${var.reviewer_group}",
        "consumer_group": "${var.consumer_group}", "genie_space_id": "${resources.genie_spaces.kpi_published.id}"}
    assert not set(parked) & set(named)


def test_nightly_dq_and_eval_job(bundle):
    job = bundle.jobs()["kpi_dq_and_eval"]
    assert job["schedule"]["pause_status"] == "PAUSED" and job["schedule"]["timezone_id"] == "UTC"
    tasks = {t["task_key"]: t for t in job["tasks"]}
    assert tasks["dq"]["python_wheel_task"]["entry_point"] == "kpi-evidence-dq"
    ev = tasks["evaluate"]
    assert ev["depends_on"] == [{"task_key": "dq"}] and ev["run_if"] == "ALL_DONE"
    assert ev["python_wheel_task"]["named_parameters"]["mlflow_experiment"] == "${resources.experiments.kpi_evidence.id}"
    env = {e["environment_key"]: e for e in job["environments"]}[ev["environment_key"]]
    assert any(d.startswith("mlflow-skinny") for d in env["spec"]["dependencies"])
    assert bundle.resources()["experiments"]["kpi_evidence"][0] == "resources/jobs.yml"


def test_sql_tasks_use_the_warehouse_and_job_parameters(bundle, kit):
    for job_key, job in bundle.jobs().items():
        for task, _parent in kit.iter_tasks(job):
            if "sql_task" in task:
                sql = task["sql_task"]
                assert sql["warehouse_id"] == "${var.warehouse_id}"
                assert set(sql["parameters"]) == {"catalog", "schema", "max_docs"}


PATH_KEYS = ("file_path", "source_code_path", "python_file", "notebook_path")


def _referenced_paths(bundle, kit):
    for rtype, items in bundle.resources().items():
        for key, (rel, body) in items.items():
            base = (ROOT / rel).parent
            for path, text in kit.iter_strings(body):
                leaf = path.rsplit(".", 1)[-1]
                if leaf in PATH_KEYS or path.endswith("file.path"):
                    yield f"{rtype}.{key}.{path}", leaf, (base / text).resolve()


def test_referenced_files_exist(bundle, kit):
    checked = 0
    for where, leaf, target in _referenced_paths(bundle, kit):
        if leaf == "notebook_path":
            continue
        assert target.exists(), f"{where} -> {target} does not exist"
        checked += 1
    assert checked >= 3


def test_optional_block_wheel_globs_match_artifact_outputs(bundle, kit):
    jobs_dir = bundle.declaring_dir("jobs", "kpi_check")
    artifact_dists = {(ROOT / a["path"] / "dist").resolve() for a in bundle.config["artifacts"].values()}
    for env in kit.optional_block("check_wheel_variant")["environments"]:
        for dep in env["spec"]["dependencies"]:
            assert (jobs_dir / dep).parent.resolve() in artifact_dists, f"check_wheel_variant: {dep}"


def test_notebook_file_exists(bundle, kit):
    notebooks = [t for _w, leaf, t in _referenced_paths(bundle, kit) if leaf == "notebook_path"]
    assert notebooks == [kit.notebook.resolve()]
    assert kit.notebook.exists(), "the kpi_check job and the bundle sync refer to notebooks/check_reports.py"
    assert kit.notebook.read_text(encoding="utf-8").startswith("# Databricks notebook source")


def test_sync_ships_the_notebook_and_skips_the_parked_app(bundle):
    sync = bundle.config["sync"]
    assert sync["include"] == ["notebooks/*.py"]
    assert sync["exclude"] == ["apps/**"]
    assert not any(fnmatch.fnmatch("notebooks/check_reports.py", pat) for pat in sync["exclude"])
    ignore = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert not any(line and fnmatch.fnmatch("notebooks/check_reports.py", line.rstrip("/")) for line in ignore)
    assert not any(fnmatch.fnmatch("notebooks/", line) for line in ignore if line)
    assert not any(fnmatch.fnmatch("scripts/publish_wheels.py", pat) for pat in sync["exclude"])


def test_deploy_guide_covers_the_contract(bundle, kit):
    guide = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
    for must in ("2026-11-01", "Unity Gateway", "bundle validate", "kpi_wheels", "kpi_check", "kpi_bootstrap",
                 "%pip install", "persist=True", "SELECT", "ai_query", "system.ai", "resources/parked/",
                 "Unverified items", "check_entry", "databricks fs cp", "notebooks/check_reports.py",
                 "base_parameters", "insight_harness", "auto_publish", "KPI table", "resources/parked/optional.yml"):
        assert must in guide, must
    params_section = guide.split("## 14.", 1)[1].split("## 15.", 1)[0]
    tasks = [task for _j, task, _p in _active_wheel_tasks(bundle, kit)] + [t for _b, t in _block_wheel_tasks(kit)]
    for task in tasks:
        wt = task["python_wheel_task"]
        assert f"`{wt['entry_point']}`" in params_section, wt["entry_point"]
        for name in wt["named_parameters"]:
            assert f"`{name}`" in params_section, f"DEPLOY.md section 14 does not list {name}"
    for name in ("--reports", "--kpis", "--out_dir", "--catalog", "--gov_schema", "--brand"):
        assert f"`{name}`" in params_section, name


def test_guides_point_to_the_optional_blocks_file(kit):
    guide = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
    readme = (kit.parked_dir / "README.md").read_text(encoding="utf-8")
    for name, text in (("docs/DEPLOY.md", guide), ("resources/parked/README.md", readme)):
        assert "uncomment" not in text.lower(), f"{name} still says to uncomment a block"
        assert "# BEGIN" not in text and "# END" not in text, name
        assert "optional.yml" in text, name
    for block in OPTIONAL_BLOCKS:
        assert f"`{block}`" in guide, f"docs/DEPLOY.md does not name the optional block {block}"


def test_databricks_cli_validate_note():
    try:
        out = subprocess.run(["databricks", "--version"], capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, OSError):
        pytest.skip("Databricks CLI not installed: run `databricks bundle validate -t dev` on a machine with the CLI")
    assert out.returncode == 0
