from __future__ import annotations

from types import SimpleNamespace

import pytest

from kpi_evidence.contracts import ReasonCode, ValidationStatus
from kpi_evidence.platform import SparkSqlRunner, StatementFailed, StatementRunner, classify_error
from kpi_evidence.store.base import MetricQueryError


class FakeFrame:
    def __init__(self, columns, rows):
        self.columns, self._rows = columns, rows

    def take(self, n):
        return self._rows[:n]


class FakeSpark:
    def __init__(self, results):
        self.results, self.calls = results, []

    def sql(self, sql):
        self.calls.append(sql)
        outcome = self.results[sql]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_spark_runner_contract_and_cache():
    spark = FakeSpark({"ok": FakeFrame(["value"], [(5.5,)]), "null": FakeFrame(["value"], [(None,)]),
                       "two": FakeFrame(["value"], [(1,), (2,)]), "named": FakeFrame(["VALUE"], [(1,)]),
                       "empty": FakeFrame(["value"], [])})
    runner = SparkSqlRunner(spark)
    assert runner.run("ok") == 5.5 and runner.run("ok") == 5.5
    assert spark.calls.count("ok") == 1 and runner.calls == 1
    with pytest.raises(MetricQueryError, match="returned NULL"):
        runner.run("null")
    for bad in ("two", "named", "empty"):
        with pytest.raises(MetricQueryError, match="exactly one row"):
            runner.run(bad)


def test_spark_runner_decimal_and_row_objects():
    from decimal import Decimal

    class Row(tuple):
        def asDict(self):
            return {"value": self[0]}

    runner = SparkSqlRunner(FakeSpark({"q": FakeFrame(["value"], [Row((Decimal("0.844222"),))])}))
    assert runner.run("q") == pytest.approx(0.844222)


def _resp(state, columns=("value",), rows=None, error=None, statement_id="s1"):
    return SimpleNamespace(
        statement_id=statement_id,
        status=SimpleNamespace(state=SimpleNamespace(value=state), error=error, sql_state=None),
        manifest=SimpleNamespace(schema=SimpleNamespace(columns=[SimpleNamespace(name=c) for c in columns])),
        result=SimpleNamespace(data_array=rows) if rows is not None else None)


class FakeStatementAPI:
    def __init__(self, first, later=()):
        self.first, self.later = first, list(later)
        self.executed, self.polled, self.canceled = [], 0, []

    def execute_statement(self, **kwargs):
        self.executed.append(kwargs)
        return self.first

    def get_statement(self, statement_id):
        self.polled += 1
        return self.later.pop(0)

    def cancel_execution(self, statement_id):
        self.canceled.append(statement_id)


def test_statement_runner_success_and_polling():
    api = FakeStatementAPI(_resp("PENDING", rows=None), [_resp("RUNNING"), _resp("SUCCEEDED", rows=[["5783.040615"]])])
    runner = StatementRunner(SimpleNamespace(statement_execution=api), "wh-1", poll_seconds=0.0)
    assert runner.run("SELECT 1 AS value") == pytest.approx(5783.040615)
    assert api.polled == 2 and api.executed[0]["warehouse_id"] == "wh-1" and api.executed[0]["row_limit"] == 2
    assert runner.run("SELECT 1 AS value") == pytest.approx(5783.040615) and len(api.executed) == 1


def test_statement_runner_null_and_shape():
    api = FakeStatementAPI(_resp("SUCCEEDED", rows=[[None]]))
    with pytest.raises(MetricQueryError, match="returned NULL") as err:
        StatementRunner(SimpleNamespace(statement_execution=api), "wh").run("q")
    assert classify_error(err.value)[1] == ReasonCode.NULL_RESULT
    api = FakeStatementAPI(_resp("SUCCEEDED", columns=("v",), rows=[["1"]]))
    with pytest.raises(MetricQueryError, match="exactly one row"):
        StatementRunner(SimpleNamespace(statement_execution=api), "wh").run("q")


def test_statement_runner_failure_is_classifiable():
    error = SimpleNamespace(error_code=SimpleNamespace(value="BAD_REQUEST"),
                            message="[INSUFFICIENT_PERMISSIONS] Insufficient privileges: User does not have SELECT")
    api = FakeStatementAPI(_resp("FAILED", error=error))
    with pytest.raises(StatementFailed) as err:
        StatementRunner(SimpleNamespace(statement_execution=api), "wh").run("q")
    assert classify_error(err.value)[:2] == (ValidationStatus.MISSING_ACCESS, ReasonCode.PERMISSION_DENIED)


def test_statement_runner_timeout_cancels():
    api = FakeStatementAPI(_resp("PENDING"), [_resp("RUNNING")] * 50)
    runner = StatementRunner(SimpleNamespace(statement_execution=api), "wh", timeout_seconds=0.0, poll_seconds=0.0)
    with pytest.raises(TimeoutError):
        runner.run("q")
    assert api.canceled == ["s1"]
