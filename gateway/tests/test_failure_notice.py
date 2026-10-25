"""A notice over SMTP when a SCHEDULED run fails.

«Schedule it and forget it» works only if something wakes you up. Here above all it is
checked when the notice must NOT go out: a notice arriving too often is filtered, and
then it serves nothing.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.models import Connection
from app.services import notifier
from tests.conftest import make_flow, make_project, make_run, make_user


@pytest.fixture
def scenario(session, monkeypatch):
    project = make_project(session, name="P")
    author = make_user(session, email="alice@x.it")
    conn = Connection(
        name="posta", db_type="smtp", project_id=project.id, owner_id=author.id,
        host="smtp.x.it", port=587, username="u", extra='{"from_address": "tabularia@x.it"}',
    )
    session.add(conn)
    session.commit()
    session.refresh(conn)
    flow = make_flow(session, name="Fatturato", project_id=project.id)
    flow.notify_emails = "alice@x.it, bruno@x.it"
    flow.notify_connection_id = conn.id
    session.add(flow)
    session.commit()

    sent = []

    class _Resp:
        status_code = 200
        text = ""

    class _Client:
        async def post(self, url, json=None, timeout=None):
            sent.append({"url": url, **(json or {})})
            return _Resp()

    monkeypatch.setattr(notifier, "get_engine_client", lambda: _Client())
    return flow, conn, sent


def _run(session, flow, *, status="FAILURE", trigger="schedule", when=None, error="boom", kind="orchestration"):
    """A flow's scheduled run is the ORCHESTRATION run: it is what the orchestrator passes to
    the notifier."""
    t = when or datetime.now(timezone.utc).replace(tzinfo=None)
    return make_run(
        session, kind=kind, flow_id=flow.id, status=status, trigger_type=trigger,
        error=error, started_at=t, finished_at=t, task_id="" if kind == "orchestration" else "t-1",
    )


def _notify(session, run):
    asyncio.run(notifier.notify_failure(session, run))


def test_a_scheduled_failure_sends_the_notice(session, scenario):
    flow, _, sent = scenario
    _notify(session, _run(session, flow))
    assert len(sent) == 1
    msg = sent[0]
    assert msg["to"] == ["alice@x.it", "bruno@x.it"]
    assert flow.name in msg["subject"]
    assert "boom" in msg["body"]  # the real error, not a «something went wrong»


def test_a_manual_failure_says_nothing(session, scenario):
    """Whoever launches by hand is looking at the screen: notifying them is noise."""
    flow, _, sent = scenario
    _notify(session, _run(session, flow, trigger="manual"))
    assert sent == []


def test_a_success_says_nothing(session, scenario):
    flow, _, sent = scenario
    _notify(session, _run(session, flow, status="SUCCESS", error=None))
    assert sent == []


def test_it_does_not_repeat_while_it_keeps_failing(session, scenario):
    """A broken flow firing every five minutes would send 300 emails a day, and by the third
    nobody would read them any more."""
    flow, _, sent = scenario
    yesterday = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    _run(session, flow, when=yesterday)  # it had failed already
    _notify(session, _run(session, flow))
    assert sent == []


def test_it_speaks_again_after_a_recovery(session, scenario):
    """Failed, fixed, failed again: the second fault is news."""
    flow, _, sent = scenario
    base = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    _run(session, flow, when=base)
    _run(session, flow, status="SUCCESS", error=None, when=base + timedelta(hours=1))
    _notify(session, _run(session, flow, when=base + timedelta(hours=2)))
    assert len(sent) == 1


def test_without_recipients_or_connection_nothing_is_sent(session, scenario):
    flow, _, sent = scenario
    flow.notify_emails = None
    session.add(flow)
    session.commit()
    _notify(session, _run(session, flow))
    assert sent == []


def test_a_broken_notice_never_changes_the_outcome(session, scenario, monkeypatch):
    """The run has failed already: a broken notice must not make it more confusing."""
    flow, _, _ = scenario

    class _Boom:
        async def post(self, *a, **k):
            raise RuntimeError("smtp giù")

    monkeypatch.setattr(notifier, "get_engine_client", lambda: _Boom())
    run = _run(session, flow)
    _notify(session, run)  # does not raise
    assert run.status == "FAILURE"


# ── configuration ────────────────────────────────────────────────────────────

def test_the_allowed_domains_of_the_connection_still_apply(session, scenario):
    """The notice is not a shortcut around the email node's barrier."""
    from app.routes import flows as flow_routes
    from app.schemas.models import FlowScheduleUpdate

    flow, conn, _ = scenario
    conn.extra = '{"from_address": "tabularia@x.it", "allowed_domains": "x.it"}'
    session.add(conn)
    session.commit()
    boss = make_user(session, email="capo@x.it", is_superuser=True)

    with pytest.raises(HTTPException) as e:
        flow_routes._set_failure_notice(
            session, boss, flow,
            FlowScheduleUpdate(notify_emails="fuori@altrove.com", notify_connection_id=conn.id),
        )
    assert e.value.status_code == 422 and "altrove.com" in e.value.detail


def test_a_malformed_address_is_refused(session, scenario):
    from app.routes import flows as flow_routes
    from app.schemas.models import FlowScheduleUpdate

    flow, conn, _ = scenario
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    with pytest.raises(HTTPException) as e:
        flow_routes._set_failure_notice(session, boss, flow, FlowScheduleUpdate(notify_emails="non-un-indirizzo"))
    assert e.value.status_code == 422


def test_the_notice_is_about_the_orchestration_not_its_output_runs(session, scenario):
    """For months kind == "flow" was written here: the orchestrator passes the orchestration
    run, and the notice never went out. The "flow" runs are the children (one per Output
    node) and do not notify alone."""
    flow, _, sent = scenario
    _notify(session, _run(session, flow, kind="flow"))
    assert sent == []
    _notify(session, _run(session, flow))
    assert len(sent) == 1


def test_a_rejected_launch_does_not_count_as_a_previous_failure(session, scenario):
    """A manual launch refused because the flow was running already is not a failed run: it
    must not silence the notice of the next one that really fails."""
    from datetime import timedelta

    flow, _, sent = scenario
    at = datetime.now(timezone.utc).replace(tzinfo=None)
    _run(session, flow, status="SUCCESS", when=at - timedelta(hours=2), error=None)
    _run(session, flow, trigger="manual", when=at - timedelta(hours=1), error="flow already running")
    _notify(session, _run(session, flow, when=at))
    assert len(sent) == 1
