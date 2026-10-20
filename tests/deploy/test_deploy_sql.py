from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from kpi_evidence.contracts import ALL_TABLES, Document, DocumentFormat, DocumentStatus, StatusGroup, STATUS_INFO, ValidationStatus
from kpi_evidence.store.base import split_sql

ROOT = Path(__file__).resolve().parents[2]
JOBS_SQL = ROOT / "src" / "sql" / "jobs"
PLACEHOLDERS = {
    "list_ready_documents.sql": {"{{catalog}}", "{{schema}}", "{{max_docs}}"},
    "consumer_views.sql": {"${schema}"},
    "grants.sql": {"${schema}", "${gov}", "${app_sp}", "${reviewer_group}", "${consumer_group}"},
}
GROUP_NAMES = {StatusGroup.GOOD: "Good", StatusGroup.REVIEW: "Needs review", StatusGroup.BLOCKED: "Blocked"}
_PLACEHOLDER = re.compile(r"\$\{[A-Za-z_]+\}|\{\{[A-Za-z_.]+\}\}")


def _strip_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _sql(name: str) -> str:
    return (JOBS_SQL / name).read_text(encoding="utf-8")


def render_list_ready(sql: str, schema: str, max_docs: int) -> str:
    sql = sql.replace("IDENTIFIER({{catalog}} || '.' || {{schema}} || '.documents')", f"{schema}.documents")
    return sql.replace("{{max_docs}}", str(max_docs))


def _doc(doc_id: str, status: DocumentStatus, minute: int) -> Document:
    ts = datetime(2026, 6, 5, 12, minute)
    return Document(doc_id=doc_id, file_name=f"{doc_id}.pptx", file_path=f"/v/inbox/{doc_id}.pptx", size_bytes=1,
                    format=DocumentFormat.PPTX, status=status, registered_at=ts, updated_at=ts)


def test_every_jobs_sql_file_is_known():
    assert {p.name for p in JOBS_SQL.glob("*.sql")} == set(PLACEHOLDERS)


@pytest.mark.parametrize("name", sorted(PLACEHOLDERS))
def test_sql_file_splits_into_statements_with_known_placeholders(name):
    sql = _sql(name)
    statements = split_sql(sql)
    assert statements, name
    for stmt in statements:
        assert stmt.strip() and not stmt.strip().startswith("--")
    used = set(_PLACEHOLDER.findall(_strip_comments(sql)))
    assert used == PLACEHOLDERS[name], f"{name}: placeholders {used}"
    assert "::" not in _strip_comments(sql), "no '::' casts (common dialect subset)"


def test_list_ready_is_one_select_returning_doc_id():
    statements = split_sql(_sql("list_ready_documents.sql"))
    assert len(statements) == 1
    stmt = statements[0]
    assert stmt.upper().startswith("SELECT DOC_ID")
    assert "IDENTIFIER({{catalog}} || '.' || {{schema}} || '.documents')" in stmt
    assert "status IN ('REGISTERED')" in stmt


def test_list_ready_parses_as_databricks_sql_with_named_markers():
    sqlglot = pytest.importorskip("sqlglot")
    stmt = split_sql(_sql("list_ready_documents.sql"))[0]
    for marker_form in (re.sub(r"\{\{(\w+)\}\}", r":\1", stmt), re.sub(r"\{\{(\w+)\}\}", r"'\1'", stmt)):
        parsed = sqlglot.parse_one(marker_form, read="databricks")
        assert parsed is not None


def test_list_ready_lists_registered_documents_in_arrival_order(mem_store):
    docs = [_doc("c", DocumentStatus.REGISTERED, 3), _doc("a", DocumentStatus.REGISTERED, 1),
            _doc("b", DocumentStatus.READY, 2), _doc("d", DocumentStatus.FAILED, 4),
            _doc("e", DocumentStatus.REGISTERED, 5)]
    mem_store.insert(docs)
    sql = render_list_ready(split_sql(_sql("list_ready_documents.sql"))[0], mem_store.settings.schema, 2)
    out = mem_store.query(sql)
    assert list(out.columns) == ["doc_id"]
    assert list(out["doc_id"]) == ["a", "c"]


def test_list_ready_caps_the_fan_out_at_500(mem_store):
    sql = render_list_ready(split_sql(_sql("list_ready_documents.sql"))[0], mem_store.settings.schema, 100000)
    mem_store.insert([_doc(f"{i:04d}", DocumentStatus.REGISTERED, i % 60) for i in range(520)])
    assert len(mem_store.query(sql)) == 500


def test_consumer_view_columns(view_store):
    cols = list(view_store.query(f"SELECT * FROM {view_store.fq('kpi_status_overview')} LIMIT 0").columns)
    for col in ("brand", "kpi_id", "kpi_label", "scope_key", "found_in_reports", "latest_status",
                "latest_status_label", "latest_status_group", "published_value", "published_label",
                "published_evidence_uri", "has_platform_source"):
        assert col in cols


def test_consumer_view_reports_status_and_coverage(view_store, registry, kit):
    store = view_store
    store.upsert(registry.registry_rows())
    _brands, goals, _types = registry.config_rows()
    store.insert_new(goals)
    goal_kpis = [g.kpi_id for g in goals]
    statuses = list(ValidationStatus)
    assert len(goal_kpis) > len(statuses), "need one spare goal KPI for the not-found case"
    results = [kit.make_result(result_id=f"r{i}", observation_id=f"o{i}", kpi_id=kpi, validation_status=status)
               for i, (kpi, status) in enumerate(zip(goal_kpis, statuses))]
    results.append(kit.make_result(result_id="old", observation_id="old", kpi_id=goal_kpis[0],
                                   validation_status=ValidationStatus.VARIANCE_REVIEW,
                                   period_end=datetime(2026, 4, 30).date()))
    results.append(kit.make_result(result_id="gone", observation_id="gone", kpi_id=goal_kpis[1],
                                   validation_status=ValidationStatus.BLOCKED, is_current=False,
                                   period_end=datetime(2026, 7, 31).date()))
    store.insert(results)
    store.insert([kit.make_publication(kpi_id=goal_kpis[0])])

    df = store.query(f"SELECT * FROM {store.fq('kpi_status_overview')}").set_index("kpi_id")
    assert len(df) == len(goals)
    for kpi, status in zip(goal_kpis, statuses):
        info = STATUS_INFO[status]
        row = df.loc[kpi]
        assert bool(row["found_in_reports"]) is True
        assert row["latest_status"] == status.value
        assert row["latest_status_label"] == info.label
        assert row["latest_status_group"] == GROUP_NAMES[info.group]
    missing = df.loc[goal_kpis[len(statuses)]]
    assert bool(missing["found_in_reports"]) is False
    assert missing["latest_status_group"] == "Not found"
    first = df.loc[goal_kpis[0]]
    assert first["published_value"] == pytest.approx(5910.0) and first["published_label"] == "Matches platform"
    assert first["kpi_label"] == registry.kpis[goal_kpis[0]].label


def test_grants_target_known_objects_and_backtick_principals(mem_store):
    known = {cls.TABLE for cls in ALL_TABLES}
    views = re.findall(r"CREATE OR REPLACE VIEW \$\{schema\}\.(\w+)",
                       (ROOT / "src" / "sql" / "service_views.sql").read_text(encoding="utf-8")
                       + _sql("consumer_views.sql"))
    known |= set(views)
    statements = split_sql(_sql("grants.sql"))
    assert len(statements) >= 10
    for stmt in statements:
        assert stmt.startswith("GRANT "), stmt
        principal = re.search(r" TO (.+)$", stmt).group(1)
        assert re.fullmatch(r"`\$\{(app_sp|reviewer_group|consumer_group)\}`", principal), stmt
        for name in re.findall(r"ON TABLE \$\{schema\}\.(\w+)", stmt):
            assert name in known, f"grant on unknown object {name}"
    text = " ".join(statements)
    for table in ("published_kpis_current", "kpi_registry", "platform_kpi_values", "kpi_status_overview"):
        assert f"{table} TO `${{consumer_group}}`" in text
    assert "MODIFY ON SCHEMA ${schema} TO `${app_sp}`" in text
    assert "MODIFY" not in text.replace("MODIFY ON SCHEMA ${schema} TO `${app_sp}`", "")
