"""Avviso via SMTP quando un'esecuzione PROGRAMMATA fallisce.

«Metti in schedule e dimentica» funziona solo se qualcosa ti sveglia. Qui si
verifica soprattutto quando l'avviso NON deve partire: un avviso che arriva
troppo spesso viene filtrato, e allora non serve più a niente.
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
    progetto = make_project(session, name="P")
    autore = make_user(session, email="alice@x.it")
    conn = Connection(
        name="posta", db_type="smtp", project_id=progetto.id, owner_id=autore.id,
        host="smtp.x.it", port=587, username="u", extra='{"from_address": "tabularia@x.it"}',
    )
    session.add(conn)
    session.commit()
    session.refresh(conn)
    flow = make_flow(session, name="Fatturato", project_id=progetto.id)
    flow.notify_emails = "alice@x.it, bruno@x.it"
    flow.notify_connection_id = conn.id
    session.add(flow)
    session.commit()

    inviati = []

    class _Resp:
        status_code = 200
        text = ""

    class _Client:
        async def post(self, url, json=None, timeout=None):
            inviati.append({"url": url, **(json or {})})
            return _Resp()

    monkeypatch.setattr(notifier, "get_engine_client", lambda: _Client())
    return flow, conn, inviati


def _run(session, flow, *, status="FAILURE", trigger="schedule", quando=None, error="boom"):
    t = quando or datetime.now(timezone.utc).replace(tzinfo=None)
    return make_run(
        session, kind="flow", flow_id=flow.id, status=status, trigger_type=trigger,
        error=error, started_at=t, finished_at=t,
    )


def _avvisa(session, run):
    asyncio.run(notifier.notify_failure(session, run))


def test_a_scheduled_failure_sends_the_notice(session, scenario):
    flow, _, inviati = scenario
    _avvisa(session, _run(session, flow))
    assert len(inviati) == 1
    msg = inviati[0]
    assert msg["to"] == ["alice@x.it", "bruno@x.it"]
    assert flow.name in msg["subject"]
    assert "boom" in msg["body"]  # l'errore vero, non un «qualcosa è andato storto»


def test_a_manual_failure_says_nothing(session, scenario):
    """Chi lancia a mano sta guardando lo schermo: avvisarlo è rumore."""
    flow, _, inviati = scenario
    _avvisa(session, _run(session, flow, trigger="manual"))
    assert inviati == []


def test_a_success_says_nothing(session, scenario):
    flow, _, inviati = scenario
    _avvisa(session, _run(session, flow, status="SUCCESS", error=None))
    assert inviati == []


def test_it_does_not_repeat_while_it_keeps_failing(session, scenario):
    """Un flusso rotto che scatta ogni cinque minuti manderebbe 300 email al
    giorno, e alla terza nessuno le leggerebbe più."""
    flow, _, inviati = scenario
    ieri = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    _run(session, flow, quando=ieri)  # era già fallita
    _avvisa(session, _run(session, flow))
    assert inviati == []


def test_it_speaks_again_after_a_recovery(session, scenario):
    """Fallito, risolto, fallito di nuovo: il secondo guasto è una notizia."""
    flow, _, inviati = scenario
    base = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    _run(session, flow, quando=base)
    _run(session, flow, status="SUCCESS", error=None, quando=base + timedelta(hours=1))
    _avvisa(session, _run(session, flow, quando=base + timedelta(hours=2)))
    assert len(inviati) == 1


def test_without_recipients_or_connection_nothing_is_sent(session, scenario):
    flow, _, inviati = scenario
    flow.notify_emails = None
    session.add(flow)
    session.commit()
    _avvisa(session, _run(session, flow))
    assert inviati == []


def test_a_broken_notice_never_changes_the_outcome(session, scenario, monkeypatch):
    """Il run è già fallito: un avviso rotto non deve renderlo più confuso."""
    flow, _, _ = scenario

    class _Boom:
        async def post(self, *a, **k):
            raise RuntimeError("smtp giù")

    monkeypatch.setattr(notifier, "get_engine_client", lambda: _Boom())
    run = _run(session, flow)
    _avvisa(session, run)  # non solleva
    assert run.status == "FAILURE"


# ── configurazione ───────────────────────────────────────────────────────────

def test_the_allowed_domains_of_the_connection_still_apply(session, scenario):
    """L'avviso non è una scorciatoia per aggirare la barriera del nodo email."""
    from app.routes import flows as flow_routes
    from app.schemas.models import FlowScheduleUpdate

    flow, conn, _ = scenario
    conn.extra = '{"from_address": "tabularia@x.it", "allowed_domains": "x.it"}'
    session.add(conn)
    session.commit()
    capo = make_user(session, email="capo@x.it", is_superuser=True)

    with pytest.raises(HTTPException) as e:
        flow_routes._set_failure_notice(
            session, capo, flow,
            FlowScheduleUpdate(notify_emails="fuori@altrove.com", notify_connection_id=conn.id),
        )
    assert e.value.status_code == 422 and "altrove.com" in e.value.detail


def test_a_malformed_address_is_refused(session, scenario):
    from app.routes import flows as flow_routes
    from app.schemas.models import FlowScheduleUpdate

    flow, conn, _ = scenario
    capo = make_user(session, email="capo@x.it", is_superuser=True)
    with pytest.raises(HTTPException) as e:
        flow_routes._set_failure_notice(session, capo, flow, FlowScheduleUpdate(notify_emails="non-un-indirizzo"))
    assert e.value.status_code == 422
