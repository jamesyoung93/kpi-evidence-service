from __future__ import annotations

import glob
import math
import os
import re
import shutil
import subprocess
import sys

import pandas as pd
import pytest

from kpi_evidence import platform
from kpi_evidence.contracts import ReasonCode, ValidationStatus
from kpi_evidence.settings import Settings
from kpi_evidence.store.base import split_sql

pytestmark = pytest.mark.spark
REL = 1e-9


def _is_java_home(path: str | None) -> bool:
    return bool(path) and any(os.path.isfile(os.path.join(path, "bin", exe)) for exe in ("java.exe", "java"))


def _java_home() -> str | None:
    if _is_java_home(os.environ.get("JAVA_HOME")):
        return os.environ["JAVA_HOME"]
    java = shutil.which("java")
    if java:
        try:
            out = subprocess.run([java, "-XshowSettings:properties", "-version"], capture_output=True, text=True,
                                 timeout=60, stdin=subprocess.DEVNULL)
            match = re.search(r"^\s*java\.home\s*=\s*(.+?)\s*$", out.stderr + out.stdout, re.M)
            if match and _is_java_home(match.group(1)):
                return match.group(1)
        except (OSError, subprocess.SubprocessError):
            pass
    candidates = sorted(glob.glob("C:/Program Files/Java/jdk-*") + glob.glob("/usr/lib/jvm/*"))
    return next((c for c in reversed(candidates) if _is_java_home(c)), None)


@pytest.fixture(scope="module")
def spark(tmp_path_factory):
    pytest.importorskip("pyspark")
    java = _java_home()
    if java is None:
        pytest.skip("no Java runtime found (set JAVA_HOME)")
    os.environ["JAVA_HOME"] = java
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)
    from pyspark.sql import SparkSession

    try:
        session = (SparkSession.builder.master("local[2]").appName("kpi-evidence-dialect")
                   .config("spark.ui.enabled", "false")
                   .config("spark.sql.shuffle.partitions", "2")
                   .config("spark.sql.ansi.enabled", "true")
                   .config("spark.sql.session.timeZone", "UTC")
                   .config("spark.driver.memory", "1g")
                   .config("spark.sql.warehouse.dir", str(tmp_path_factory.mktemp("spark-warehouse")))
                   .getOrCreate())
        session.sparkContext.setLogLevel("OFF")
        session.sql("SELECT 1 AS value").collect()
    except Exception as exc:
        pytest.skip(f"local SparkSession could not start: {type(exc).__name__}: {str(exc)[:200]}")
    yield session
    session.stop()


def _spark_schema(frame: pd.DataFrame):
    from pyspark.sql import types as T

    fields = []
    for col in frame.columns:
        dtype = frame[col].dtype
        if pd.api.types.is_bool_dtype(dtype):
            kind = T.BooleanType()
        elif pd.api.types.is_integer_dtype(dtype):
            kind = T.LongType()
        elif pd.api.types.is_float_dtype(dtype):
            kind = T.DoubleType()
        elif pd.api.types.is_datetime64_any_dtype(dtype):
            kind = T.TimestampType()
        else:
            kind = T.StringType()
        fields.append(T.StructField(col, kind, True))
    return T.StructType(fields)


def _spark_frame(spark, frame: pd.DataFrame, directory):
    path = directory / f"{len(list(directory.iterdir()))}.csv"
    frame.to_csv(path, index=False, float_format="%.17g", date_format="%Y-%m-%d %H:%M:%S")
    return (spark.read.option("header", True).option("mode", "FAILFAST")
            .option("timestampFormat", "yyyy-MM-dd HH:mm:ss").schema(_spark_schema(frame)).csv(str(path)))


@pytest.fixture(scope="module")
def spark_settings(tmp_path_factory) -> Settings:
    return Settings.from_env(local_dir=tmp_path_factory.mktemp("spark-settings"), gov_schema="global_temp",
                             platform_schema="global_temp", restricted_schemas=("global_temp",), ai_mode="off")


@pytest.fixture(scope="module")
def spark_platform(spark, spark_settings, tmp_path_factory):
    directory = tmp_path_factory.mktemp("spark-demo-csv")
    for name, frame in platform.demo_platform_frames(spark_settings).items():
        _spark_frame(spark, frame, directory).createOrReplaceGlobalTempView(name)
    rendered = platform.governed_views_sql(spark_settings)
    script = re.sub(r"CREATE OR REPLACE VIEW global_temp\.", "CREATE OR REPLACE GLOBAL TEMPORARY VIEW ", rendered)
    assert script.count("GLOBAL TEMPORARY VIEW") == len(platform.view_names(rendered))
    for statement in split_sql(script):
        spark.sql(statement)
    return platform.view_names(rendered)


class SparkQueryStore:
    dialect = "databricks"

    def __init__(self, spark):
        self.spark = spark

    def query(self, sql, params=None):
        frame = self.spark.sql(sql)
        rows = [list(r) for r in frame.collect()]
        return pd.DataFrame(rows, columns=list(frame.columns))


def test_governed_views_run_on_spark(spark, spark_platform):
    assert spark_platform == ["gv_rx_account_monthly", "gv_rx_panel_monthly", "gv_referrals_monthly"]
    counts = {v: spark.sql(f"SELECT COUNT(*) AS n FROM global_temp.{v}").collect()[0][0] for v in spark_platform}
    assert counts == {"gv_rx_account_monthly": 5760, "gv_rx_panel_monthly": 1104, "gv_referrals_monthly": 4608}
    loads = spark.sql("SELECT COUNT(DISTINCT load_id) AS n FROM global_temp.gv_rx_panel_monthly "
                      "WHERE month = '2024-07'").collect()[0][0]
    assert loads == 1


def test_every_compiled_kpi_matches_duckdb(spark, spark_platform, spark_settings, platform_registry, all_cases,
                                           duckdb_unrestricted_outcomes, case_tools):
    from kpi_evidence.platform import SparkSqlRunner, classify_error

    runner = SparkSqlRunner(spark)
    mismatches, compared, errors_matched = [], 0, 0
    for case in all_cases:
        expected = duckdb_unrestricted_outcomes[case.key]
        got = case_tools["evaluate_case"](platform_registry, spark_settings, runner, case)
        if expected.error is not None or got.error is not None:
            same = (expected.error is not None and got.error is not None
                    and classify_error(expected.error)[:2] == classify_error(got.error)[:2])
            if not same:
                mismatches.append((case.key, repr(expected.error)[:120], repr(got.error)[:120]))
            errors_matched += same
            continue
        compared += 1
        if not math.isclose(got.value, expected.value, rel_tol=REL, abs_tol=1e-12):
            mismatches.append((case.key, expected.value, got.value))
        compiled = case_tools["compile_case"](platform_registry, spark_settings, case)
        if compiled.spec.ratio is not None:
            rendered = runner.run(compiled.spec.render(dict(case.choices), case.as_of))
            if not math.isclose(rendered, expected.value, rel_tol=REL, abs_tol=1e-12):
                mismatches.append((case.key + "#render", expected.value, rendered))
    assert not mismatches, mismatches[:10]
    assert compared >= 140 and errors_matched >= 4


def test_null_and_access_semantics_on_spark(spark, spark_platform, spark_settings, platform_registry, case_tools):
    from kpi_evidence.platform import SparkSqlRunner, classify_error

    runner = SparkSqlRunner(spark)
    warm_up = case_tools["KpiCase"]("new_writers", "month", "default", (), "2024-07-31")
    out = case_tools["evaluate_case"](platform_registry, spark_settings, runner, warm_up)
    assert classify_error(out.error)[:2] == (ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.NULL_RESULT)
    try:
        spark.sql("SELECT SUM(trx_units) AS value FROM global_temp.gv_missing").collect()
    except Exception as exc:
        assert classify_error(exc)[:2] == (ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.PLATFORM_OBJECT_MISSING)
    else:
        raise AssertionError("expected an AnalysisException")
    try:
        spark.sql("SELECT no_such_column AS value FROM global_temp.gv_rx_account_monthly").collect()
    except Exception as exc:
        assert classify_error(exc)[:2] == (ValidationStatus.PLATFORM_DATA_ISSUE, ReasonCode.PLATFORM_OBJECT_MISSING)
    else:
        raise AssertionError("expected an AnalysisException")


def test_dq_checks_and_dimensions_match_duckdb(spark, spark_platform, spark_settings, platform_store, platform_settings):
    spark_store = SparkQueryStore(spark)
    on_spark = platform.evaluate_dq_checks(spark_store, spark_settings, "spark")
    on_duckdb = platform.evaluate_dq_checks(platform_store, platform_settings, "duckdb")

    def summary(rows, settings):
        base = {platform.base_table_fq(settings, t): t for t in platform.BASE_TABLE_DQ_CHECKS}
        return {(base.get(r.object_name, r.object_name), r.check_name): (r.passed, r.violations, r.severity) for r in rows}

    assert summary(on_spark, spark_settings) == summary(on_duckdb, platform_settings)
    assert platform.dimension_values(spark_store, spark_settings) == platform.dimension_values(platform_store,
                                                                                               platform_settings)


def test_window_completeness_checks_match_duckdb(spark, spark_platform, spark_settings, platform_registry, all_cases,
                                                 platform_store, platform_settings, case_tools):
    from kpi_evidence.platform import SparkSqlRunner, classify_error

    def outcome(settings, runner, case):
        compiled = case_tools["compile_case"](platform_registry, settings, case)
        try:
            return compiled.checked_spec().compute(runner, dict(case.choices), case.as_of)[0], None
        except Exception as exc:
            return None, exc

    spark_runner, duck_runner = SparkSqlRunner(spark), case_tools["UnrestrictedRunner"](platform_store)
    mismatches, incomplete = [], 0
    for case in [c for c in all_cases if c.grain != "month"]:
        got, got_error = outcome(spark_settings, spark_runner, case)
        expected, expected_error = outcome(platform_settings, duck_runner, case)
        if got_error is not None or expected_error is not None:
            same = (got_error is not None and expected_error is not None
                    and classify_error(got_error)[:2] == classify_error(expected_error)[:2])
            if not same:
                mismatches.append((case.key, repr(expected_error)[:120], repr(got_error)[:120]))
            incomplete += same and "complete_window" in str(expected_error)
        elif not math.isclose(got, expected, rel_tol=REL, abs_tol=1e-12):
            mismatches.append((case.key, expected, got))
    assert not mismatches, mismatches[:10]
    assert incomplete >= 4
