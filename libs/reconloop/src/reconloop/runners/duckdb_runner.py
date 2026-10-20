from __future__ import annotations

from pathlib import Path
from .base import scalar_value


class DuckDBRunner:
    def __init__(self, tables: dict[str, str | Path]):
        import duckdb
        self.con = duckdb.connect()
        for name, path in tables.items():
            p = str(path).replace("'", "''")
            identifier = '"' + name.replace('"', '""') + '"'
            reader = "read_parquet" if Path(path).suffix.lower() == ".parquet" else "read_csv_auto"
            self.con.execute(f"CREATE VIEW {identifier} AS SELECT * FROM {reader}('{p}')")
        self.cache: dict[str, float] = {}

    def run(self, sql: str) -> float:
        if sql not in self.cache:
            result = self.con.execute(sql)
            rows = result.fetchmany(2)
            if len(rows) != 1 or len(rows[0]) != 1 or result.description[0][0] != "value":
                raise ValueError("metric query must return exactly one row and one column named 'value'")
            self.cache[sql] = scalar_value(rows[0][0])
        return self.cache[sql]

    def close(self):
        self.con.close()
