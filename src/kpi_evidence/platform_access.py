from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Iterator
from typing import Any

import pandas as pd

from .settings import Settings


class PlatformAccess:
    """Read (and, for the demo platform, write) platform objects."""

    dialect: str = "duckdb"

    def query(self, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        raise NotImplementedError

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    def execute_script(self, script: str) -> None:
        from .store.base import split_sql

        for statement in split_sql(script):
            self.execute(statement)

    def write_dataframe(self, qualified_table: str, frame: pd.DataFrame, mode: str = "overwrite") -> int:
        raise NotImplementedError

    def runner(self) -> Any:
        """A reconloop runner: ``run(sql) -> float`` (one row, one column named 'value', finite)."""
        raise NotImplementedError

    @contextlib.contextmanager
    def session(self) -> Iterator[None]:
        """A block of reads that may share one connection (no writes inside)."""
        yield

    def describe(self) -> str:
        return type(self).__name__


class StorePlatform(PlatformAccess):
    """The platform is the store's own database (local demo, scheduled Databricks jobs)."""

    def __init__(self, store: Any):
        self.store = store
        self.dialect = store.dialect

    def query(self, sql, params=None):
        return self.store.query(sql, params)

    def execute(self, sql, params=None):
        self.store.execute(sql, params)

    def execute_script(self, script):
        self.store.execute_script(script)

    def write_dataframe(self, qualified_table, frame, mode="overwrite"):
        return self.store.write_dataframe(qualified_table, frame, mode=mode)

    def runner(self):
        return self.store.runner()

    @contextlib.contextmanager
    def session(self):
        with self.store.transaction():
            yield

    def describe(self) -> str:
        return f"same database as the state store ({self.dialect})"


class DatabricksPlatform(StorePlatform):
    """Live Unity Catalog tables through a Spark session (notebook, job) or a SQL warehouse."""

    def __init__(self, settings: Settings, *, spark: Any | None = None, client: Any | None = None,
                 warehouse_id: str | None = None):
        from .store.databricks_store import DatabricksStore

        platform_settings = dataclasses.replace(settings, backend="databricks",
                                                catalog=settings.platform_catalog or settings.catalog)
        super().__init__(DatabricksStore(platform_settings, spark=spark, client=client, warehouse_id=warehouse_id))
        self.dialect = "databricks"

    @contextlib.contextmanager
    def session(self):
        yield

    def describe(self) -> str:
        mode = getattr(self.store, "mode", "?")
        return f"Databricks ({mode})"


def spark_platform(spark: Any, settings: Settings) -> DatabricksPlatform:
    return DatabricksPlatform(settings, spark=spark)


def warehouse_platform(settings: Settings, *, client: Any | None = None, warehouse_id: str | None = None
                       ) -> DatabricksPlatform:
    return DatabricksPlatform(settings, client=client, warehouse_id=warehouse_id)
