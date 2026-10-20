from __future__ import annotations

import re

from ..contracts import ALL_TABLES, columns

_TYPES = {
    "duckdb": {"STRING": "VARCHAR", "JSON": "VARCHAR", "DOUBLE": "DOUBLE", "BIGINT": "BIGINT",
               "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP"},
    "databricks": {"STRING": "STRING", "JSON": "STRING", "DOUBLE": "DOUBLE", "BIGINT": "BIGINT",
                   "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP"},
}

TRANSACTIONAL_TABLES = frozenset({"review_decisions", "kpi_results", "published_kpis", "review_events"})


def sql_type(kind: str, dialect: str) -> str:
    return _TYPES[dialect][kind]


def check_constraints(cls: type) -> list[tuple[str, str]]:
    """[(constraint_name, boolean expression)] for every enum column of ``cls`` (allowed values from the enum)."""
    out = []
    for name, _kind, _nullable, enum_type in columns(cls):
        if enum_type is None:
            continue
        allowed = ", ".join("'" + e.value.replace("'", "''") + "'" for e in enum_type)
        out.append((f"{cls.TABLE}_{name}_valid", f"{name} IS NULL OR {name} IN ({allowed})"))
    return out


def create_table_sql(cls: type, qualified_name: str, dialect: str) -> str:
    cols = []
    for name, kind, nullable, _enum_type in columns(cls):
        null = "" if nullable else " NOT NULL"
        cols.append(f"  {name} {sql_type(kind, dialect)}{null}")
    body = ",\n".join(cols)
    if dialect == "duckdb":
        check_sql = "".join(f",\n  CHECK ({expr})" for _name, expr in check_constraints(cls))
        return f"CREATE TABLE IF NOT EXISTS {qualified_name} (\n{body}{check_sql}\n)"
    props = ["'delta.enableDeletionVectors' = 'true'"]
    if cls.TABLE in TRANSACTIONAL_TABLES:
        props.append("'delta.feature.catalogManaged' = 'supported'")
    return (f"CREATE TABLE IF NOT EXISTS {qualified_name} (\n{body}\n)\n"
            f"TBLPROPERTIES ({', '.join(props)})")


def check_constraints_sql(cls: type, qualified_name: str) -> list[str]:
    """Databricks: CHECK constraints are added with ALTER TABLE (idempotent drop + add)."""
    out = []
    for cname, expr in check_constraints(cls):
        out.append(f"ALTER TABLE {qualified_name} DROP CONSTRAINT IF EXISTS {cname}")
        out.append(f"ALTER TABLE {qualified_name} ADD CONSTRAINT {cname} CHECK ({expr})")
    return out


def add_column_sql(qualified_name: str, name: str, kind: str, dialect: str) -> str:
    """Add a contract column that an existing table lacks."""
    if dialect == "duckdb":
        return f"ALTER TABLE {qualified_name} ADD COLUMN {name} {sql_type(kind, dialect)}"
    return f"ALTER TABLE {qualified_name} ADD COLUMNS ({name} {sql_type(kind, dialect)})"


def normalize_expr(expr: str) -> str:
    """Comparable form of a constraint expression (case, whitespace and outer parentheses ignored)."""
    text = re.sub(r"\s+", " ", expr.strip()).lower()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def all_tables() -> tuple[type, ...]:
    return ALL_TABLES
