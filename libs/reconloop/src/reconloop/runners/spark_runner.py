from __future__ import annotations
from .base import scalar_value


class SparkRunner:
    def __init__(self, spark):
        self.spark = spark
        self.cache: dict[str, float] = {}

    def run(self, sql: str) -> float:
        if sql not in self.cache:
            result = self.spark.sql(sql)
            rows = result.take(2)
            if len(rows) != 1 or result.columns != ["value"]:
                raise ValueError("metric query must return exactly one row and one column named 'value'")
            self.cache[sql] = scalar_value(rows[0]["value"])
        return self.cache[sql]
