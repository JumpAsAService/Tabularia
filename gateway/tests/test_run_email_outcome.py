"""The email outcome of a run that did not succeed (review 2026-09-16, R9).

The worker reports the outcome only when its task returns. When the run fails,
the email itself with stop on failure or anything before it, nothing comes back,
and the row kept the launch summary, `ok: null`, which reads "pending" forever.
"""
import json

import pytest

from app.routes.runs import _reconcile
from tests.conftest import make_run

pytestmark = pytest.mark.anyio

PENDING = json.dumps({"connection_id": 1, "host": "smtp.example.com", "to": ["x@example.com"],
                      "subject": "Report", "attachment": "report", "format": "csv", "ok": None})


async def test_a_failed_run_marks_its_email_not_sent(session, fake_engine):
    run = make_run(session, kind="flow", task_id="t-fail", email=PENDING)
    fake_engine.set_task("t-fail", "FAILURE", error="SMTPAuthenticationError: 535")
    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "FAILURE"
    email = json.loads(run.email)
    assert email["ok"] is False and "not sent" in email["error"]
    assert email["subject"] == "Report"  # the rest of the summary stays


async def test_a_stopped_run_marks_its_email_not_sent(session, fake_engine):
    run = make_run(session, kind="flow", task_id="t-stop", email=PENDING)
    fake_engine.set_task("t-stop", "REVOKED")
    await _reconcile(session, run)
    session.refresh(run)
    assert json.loads(run.email)["ok"] is False


async def test_a_successful_run_records_what_the_worker_says(session, fake_engine):
    run = make_run(session, kind="flow", task_id="t-ok", email=PENDING)
    fake_engine.set_task("t-ok", "SUCCESS", result={"rows_written": 3, "columns": [],
                                                    "email": {"ok": True, "recipients": 1}})
    await _reconcile(session, run)
    session.refresh(run)
    # over the launch's summary, as on failure: the server it went through names the
    # email in the lineage (the worker's answer used to replace it)
    assert json.loads(run.email) == {**json.loads(PENDING), "ok": True, "recipients": 1}


async def test_a_run_without_email_stays_without(session, fake_engine):
    run = make_run(session, kind="flow", task_id="t-plain")
    fake_engine.set_task("t-plain", "FAILURE", error="boom")
    await _reconcile(session, run)
    session.refresh(run)
    assert run.email is None


async def test_an_email_to_many_people_stays_readable_when_not_sent(session, fake_engine):
    """The summary keeps every recipient at launch: closing the run cut its JSON at 2000
    characters, and with fifty addresses the run page and the lineage could no longer
    read it (bug hunt 2026-10-28)."""
    many = [f"person.number.{i}@example-company.com" for i in range(60)]
    summary = json.dumps({**json.loads(PENDING), "to": many})
    assert len(summary) > 2000
    run = make_run(session, kind="flow", task_id="t-many", email=summary)
    fake_engine.set_task("t-many", "FAILURE", error="boom")
    await _reconcile(session, run)
    session.refresh(run)
    email = json.loads(run.email)
    assert email["ok"] is False and email["to"] == many
