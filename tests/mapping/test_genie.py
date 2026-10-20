from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from databricks.sdk.service._internal import Wait
from databricks.sdk.service.dashboards import MessageStatus

from kpi_evidence.genie import DRAFT_NOTE, GenieClient, sql_is_read_only
from kpi_evidence.settings import Settings


class FakeGenie:
    def __init__(self, statuses, *, attachments=None, error=None, delete_fails=False, start_fails=False,
                 rows=(("TRx", "5783.041"),), use_wait=False):
        self.statuses = list(statuses)
        self.attachments = attachments if attachments is not None else [
            NS(attachment_id="a1", text=None, query=NS(query="SELECT 1 AS value", description="one")),
            NS(attachment_id="a2", text=NS(content="TRx for June is 5,783."), query=None)]
        self.error = error
        self.delete_fails, self.start_fails, self.use_wait = delete_fails, start_fails, use_wait
        self.rows = rows
        self.started, self.polls, self.deleted, self.result_calls = [], 0, [], 0

    def start_conversation(self, space_id, content):
        if self.start_fails:
            raise RuntimeError("403 no CAN_RUN on the space")
        self.started.append((space_id, content))
        if self.use_wait:
            return Wait(lambda **kw: None, response=NS(conversation_id="c1", message_id="m1"),
                        conversation_id="c1", message_id="m1", space_id=space_id)
        return NS(conversation_id="c1", message_id="m1")

    def get_message(self, space_id, conversation_id, message_id):
        self.polls += 1
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return NS(status=status, attachments=self.attachments, error=self.error)

    def get_message_attachment_query_result(self, space_id, conversation_id, message_id, attachment_id):
        self.result_calls += 1
        cols = [NS(name="kpi_id"), NS(name="value")]
        return NS(statement_response=NS(manifest=NS(schema=NS(columns=cols), truncated=False),
                                        result=NS(data_array=[list(r) for r in self.rows], next_chunk_index=None)))

    def delete_conversation(self, space_id, conversation_id):
        self.deleted.append(conversation_id)
        if self.delete_fails:
            raise RuntimeError("delete failed")


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def __call__(self):
        return self.now


def _client(fake, timeout_s=600.0, **settings):
    clock = Clock()
    s = Settings(genie_space_id="space01", genie_drafting_space_id="draft01", **settings)
    return GenieClient(s, client=NS(genie=fake), timeout_s=timeout_s, sleep=clock.sleep, clock=clock), clock


def test_completed_answer_with_rows_and_cleanup():
    fake = FakeGenie(["SUBMITTED", "ASKING_AI", MessageStatus.COMPLETED])
    client, clock = _client(fake)
    ans = client.ask("What is published TRx for June 2026?")
    assert ans.ok and ans.status == "COMPLETED" and ans.error is None
    assert (ans.sql, ans.sql_description, ans.text) == ("SELECT 1 AS value", "one", "TRx for June is 5,783.")
    assert ans.rows == [{"kpi_id": "TRx", "value": "5783.041"}] and not ans.truncated
    assert (ans.conversation_id, ans.message_id, ans.space_id) == ("c1", "m1", "space01")
    assert clock.sleeps == [1.0, 2.0] and fake.deleted == ["c1"]


def test_backoff_caps_at_five_seconds():
    fake = FakeGenie(["SUBMITTED"] * 6 + ["COMPLETED"])
    client, clock = _client(fake)
    assert client.ask("q").status == "COMPLETED"
    assert clock.sleeps == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0]


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED", MessageStatus.CANCELLED])
def test_terminal_states_stop_polling(status):
    err = NS(error="warehouse stopped", type=NS(value="SQL_EXECUTION_EXCEPTION")) if status == "FAILED" else None
    fake = FakeGenie([status, "COMPLETED"], error=err)
    client, _clock = _client(fake)
    ans = client.ask("q")
    assert ans.status == getattr(status, "value", status) and fake.polls == 1
    assert not ans.ok and ans.error
    assert fake.result_calls == 0 and fake.deleted == ["c1"]
    if status == "FAILED":
        assert ans.error == "SQL_EXECUTION_EXCEPTION: warehouse stopped"


def test_timeout_is_explicit():
    fake = FakeGenie(["EXECUTING_QUERY"])
    client, clock = _client(fake, timeout_s=10.0)
    ans = client.ask("q")
    assert ans.status == "TIMEOUT" and "10 s" in ans.error
    assert sum(clock.sleeps) == pytest.approx(10.0) and fake.deleted == ["c1"]


def test_transient_poll_errors_then_give_up():
    class Flaky(FakeGenie):
        def get_message(self, **kw):
            self.polls += 1
            raise ConnectionError("reset")

    fake = Flaky(["SUBMITTED"])
    client, _clock = _client(fake)
    ans = client.ask("q")
    assert ans.status == "ERROR" and "3 times" in ans.error and fake.polls == 3


def test_not_configured_and_bad_space_never_call_genie():
    fake = FakeGenie(["COMPLETED"])
    client = GenieClient(Settings(), client=NS(genie=fake))
    assert client.ask("q").status == "NOT_CONFIGURED"
    assert client.draft_sql("Active referrers", "Distinct referring HCPs").status == "NOT_CONFIGURED"
    assert client.ask("q", space_id="../../etc").status == "ERROR"
    assert fake.started == []


def test_start_failure_and_delete_failure_are_tolerated():
    client, _ = _client(FakeGenie(["COMPLETED"], start_fails=True))
    ans = client.ask("q")
    assert ans.status == "ERROR" and "CAN_RUN" in ans.error
    fake = FakeGenie(["COMPLETED"], delete_fails=True, use_wait=True)
    client, _ = _client(fake)
    assert client.ask("q").ok and fake.deleted == ["c1"]


def test_draft_sql_is_a_draft_and_never_fetches_results():
    fake = FakeGenie(["COMPLETED"])
    client, _ = _client(fake)
    ans = client.draft_sql("Active referrers", "Distinct HCPs with at least one referral", 183.75,
                           "June 2026", "brand total")
    assert ans.note == DRAFT_NOTE and ans.sql == "SELECT 1 AS value" and ans.rows is None
    assert ans.read_only is True and fake.result_calls == 0
    (space, question), = fake.started
    assert space == "draft01" and "Distinct HCPs with at least one referral" in question
    assert "183.75" not in question and ans.context["report_value"] == 183.75


def test_draft_sql_flags_writes():
    fake = FakeGenie(["COMPLETED"], attachments=[
        NS(attachment_id="a1", text=None, query=NS(query="DELETE FROM t WHERE 1=1", description=None))])
    client, _ = _client(fake)
    assert client.draft_sql("x", "y").read_only is False


@pytest.mark.parametrize("sql, expected", [
    ("SELECT SUM(trx_units) AS value FROM gv WHERE note = 'drop table'", True),
    ("WITH a AS (SELECT 1 AS v) SELECT v AS value FROM a", True),
    ("SELECT 1; DROP TABLE x", False),
    ("CREATE OR REPLACE TABLE t AS SELECT 1", False),
    ("", None),
])
def test_sql_is_read_only(sql, expected):
    assert sql_is_read_only(sql) is expected
