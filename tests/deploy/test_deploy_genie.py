from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kpi_evidence.contracts import Publication, PlatformKpiValue, RegistryEntry, columns

ROOT = Path(__file__).resolve().parents[2]
SPACE_PATH = ROOT / "src" / "genie" / "kpi_published.geniespace.json"
PREFIX = "__KPI_CATALOG__.__KPI_SCHEMA__."
_ID = re.compile(r"^[0-9a-f]{32}$")
EXPECTED_TABLES = ["kpi_registry", "kpi_status_overview", "platform_kpi_values", "published_kpis_current"]


@pytest.fixture(scope="module")
def space() -> dict:
    return json.loads(SPACE_PATH.read_text(encoding="utf-8"))


def _ids(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if "id" in node:
            out.append(node["id"])
        for v in node.values():
            _ids(v, out)
    elif isinstance(node, list):
        for v in node:
            _ids(v, out)
    return out


def _collections_with_ids(node, path="$"):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _collections_with_ids(v, f"{path}.{k}")
    elif isinstance(node, list):
        if node and all(isinstance(i, dict) and "id" in i for i in node):
            yield path, node
        for i, v in enumerate(node):
            yield from _collections_with_ids(v, f"{path}[{i}]")


def _strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node


def substitute(text: str, catalog: str, schema: str) -> str:
    return text.replace("__KPI_CATALOG__", catalog).replace("__KPI_SCHEMA__", schema)


def test_version_and_ids(space):
    assert space["version"] == 2
    ids = _ids(space)
    assert ids, "no ids found"
    for i in ids:
        assert _ID.match(i), f"id {i!r} is not 32 lowercase hex"
    assert len(ids) == len(set(ids)), "ids must be unique across questions and instruction types"


def test_collections_are_sorted(space):
    tables = space["data_sources"]["tables"]
    idents = [t["identifier"] for t in tables]
    assert idents == sorted(idents)
    for t in tables:
        names = [c["column_name"] for c in t.get("column_configs", [])]
        assert names == sorted(names), t["identifier"]
        assert len(names) == len(set(names)), t["identifier"]
    seen = 0
    for path, items in _collections_with_ids(space):
        ids = [i["id"] for i in items]
        assert ids == sorted(ids), f"{path} not sorted by id"
        seen += 1
    assert seen >= 3


def test_tables_and_placeholders(space):
    idents = [t["identifier"] for t in space["data_sources"]["tables"]]
    assert [i.removeprefix(PREFIX) for i in idents] == EXPECTED_TABLES
    assert all(i.startswith(PREFIX) for i in idents)
    text = json.dumps(space)
    assert text.count("__KPI_CATALOG__") == text.count(PREFIX)
    rendered = json.loads(substitute(text, "dev_cat", "dev_jy_kpi_evidence"))
    new_idents = [t["identifier"] for t in rendered["data_sources"]["tables"]]
    assert new_idents == sorted(new_idents)
    assert all(re.fullmatch(r"[a-z_]+\.[a-z_]+\.[a-z_]+", i) for i in new_idents)


def test_limits(space):
    assert len(space["instructions"]["text_instructions"]) == 1
    for s in _strings(space):
        assert len(s) <= 25_000
    assert len(space["data_sources"]["tables"]) <= 50
    for t in space["data_sources"]["tables"]:
        assert t.get("description"), t["identifier"]
    matched = sum(1 for t in space["data_sources"]["tables"] for c in t["column_configs"]
                  if c.get("enable_entity_matching"))
    assert matched <= 120


def test_text_instruction_forces_status_label_and_evidence(space):
    content = "".join(space["instructions"]["text_instructions"][0]["content"])
    for word in ("validation_status", "display_label", "evidence_uri", "evidence_locator",
                 "published_kpis_current", "platform_kpi_values", "Report-only", "VERIFIED"):
        assert word in content, word


def test_sample_questions(space):
    questions = ["".join(q["question"]) for q in space["config"]["sample_questions"]]
    assert "Which Kestrel KPIs matched the platform in May 2026?" in questions
    assert "Show report-only KPIs and their sources" in questions
    assert len(questions) <= 10


def _known_columns(view_store) -> dict[str, set[str]]:
    out = {}
    for table in EXPECTED_TABLES:
        df = view_store.query(f"SELECT * FROM {view_store.fq(table)} LIMIT 0")
        out[table] = set(df.columns)
    return out


def test_column_configs_name_real_columns(space, view_store):
    known = _known_columns(view_store)
    assert {c[0] for c in columns(Publication)} <= known["published_kpis_current"]
    assert {c[0] for c in columns(RegistryEntry)} == known["kpi_registry"]
    assert {c[0] for c in columns(PlatformKpiValue)} == known["platform_kpi_values"]
    for t in space["data_sources"]["tables"]:
        name = t["identifier"].removeprefix(PREFIX)
        for c in t["column_configs"]:
            assert c["column_name"] in known[name], f"{name}.{c['column_name']} does not exist"


def test_example_sql_runs_on_the_service_schema(space, view_store, kit):
    store = view_store
    store.insert([kit.make_publication()])
    params_for = {"brand": "KESTREL", "kpi_id": "trx", "period_end": "2026-05-31"}
    examples = space["instructions"]["example_question_sqls"]
    assert len(examples) >= 3
    local_prefix = store.settings.schema_prefix(store.settings.schema) + "."
    for ex in examples:
        sql = "".join(ex["sql"]).replace(PREFIX, local_prefix)
        assert "__KPI_" not in sql
        declared = {p["name"] for p in ex.get("parameters", [])}
        used = set(re.findall(r"(?<![:\w]):([A-Za-z_]\w*)", sql))
        assert used == declared, f"{ex['question']}: markers {used} vs parameters {declared}"
        for p in ex.get("parameters", []):
            assert p["type_hint"] == "STRING"
        df = store.query(sql, {k: params_for[k] for k in used})
        assert df is not None
    verified = next(ex for ex in examples if "matched the platform" in "".join(ex["question"]))
    sql = "".join(verified["sql"]).replace(PREFIX, local_prefix)
    rows = store.query(sql, {"brand": "KESTREL", "period_end": "2026-05-31"})
    assert len(rows) == 1 and rows.iloc[0]["display_label"] == "Matches platform"
