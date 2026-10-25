"""Data contract notices: who is notified, when, and when NOT.

As for the flows, the part that counts is the silence: a notice at every evaluation
would be filtered, and then it would no longer serve. It notifies at a change of
state, for what happens by itself (new data, time passing).
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import AuditLog, Connection, DataContractResult
from app.models.permission import Capability
from app.routes import contracts as routes
from app.services import contract_notifier, contracts
from tests.conftest import make_datasource, make_permission, make_project, make_user

RULES = [{"kind": "row_count", "max": 10, "severity": "error"}, {"kind": "not_null", "column": "id", "severity": "warning"}]
REQUEST = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"), url="", method="PUT")

WELL = {"outcome": "passed", "rows": 3, "errors": 0, "warnings": 0, "rules": []}
WARNING = {"outcome": "warning", "rows": 3, "errors": 0, "warnings": 1, "rules": [
    {"id": "r2", "kind": "not_null", "column": "id", "severity": "warning", "passed": False, "violations": 2}]}
BROKEN = {"outcome": "failed", "rows": 50, "errors": 1, "warnings": 0, "rules": [
    {"id": "r1", "kind": "row_count", "severity": "error", "passed": False, "observed": 50, "expected": {"min": None, "max": 10}}]}


@pytest.fixture
def scene(session, monkeypatch):
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    folder = make_project(session, name="dati", owner_id=boss.id)
    elsewhere = make_project(session, name="posta", owner_id=boss.id)
    author = make_user(session, email="autore@x.it")
    make_permission(session, user_id=author.id, project_id=folder.id, capability=Capability.EDIT)
    reader = make_user(session, email="lettore@x.it")
    make_permission(session, user_id=reader.id, project_id=folder.id, capability=Capability.VIEW)
    smtp = Connection(name="posta", db_type="smtp", project_id=elsewhere.id, owner_id=boss.id, host="smtp.x.it", port=587,
                      username="u", extra='{"from_address": "tabularia@x.it", "allowed_domains": ["x.it"]}')
    session.add(smtp)
    session.commit()
    ds = make_datasource(session, name="ordini", project_id=folder.id, key="datasets/1/v1.parquet", rows=3)
    c = contracts.save(session, ds, contracts.validate_document({"rules": RULES}), True, boss.id, notify=("alice@x.it, bruno@x.it", smtp.id))
    contracts.record_result(session, c, WELL, trigger="save")
    session.commit()

    sent = []

    class _Client:
        answer = 200

        async def post(self, url, json=None, timeout=None):
            sent.append({"url": url, **(json or {})})
            return SimpleNamespace(status_code=self.answer, text="no")

    monkeypatch.setattr(contract_notifier, "get_engine_client", lambda: _Client())
    # the routes that wait for the engine close the session before waiting: the scene's
    # objects get detached loaded already (see test_contracts.scena)
    for o in (boss, author, reader, smtp, ds, folder):
        session.refresh(o)
    session.expunge_all()
    return SimpleNamespace(ds=ds, boss=boss, author=author, reader=reader, smtp=smtp, sent=sent, client=_Client)


def _report(session, scene, report, trigger="refresh", **k):
    r = contracts.record_result(session, contracts.get(session, scene.ds.id), report, trigger=trigger, **k)
    session.commit()
    return r


def _delivery(session):
    return asyncio.run(contract_notifier.deliver_pending(session))


def _states(session):
    return [r.notify for r in session.exec(select(DataContractResult).order_by(DataContractResult.id)).all()]


# ── when ─────────────────────────────────────────────────────────────────────
def test_a_refused_update_notifies_and_says_which_rule(session, scene):
    _report(session, scene, BROKEN, blocked=True, run_id=41)
    assert _delivery(session) == 1
    (m,) = scene.sent
    assert m["url"] == "/db/notify" and m["to"] == ["alice@x.it", "bruno@x.it"]
    # alice and bruno are not users: English (see test_email_language.py for the others)
    assert m["subject"] == "[Tabularia] Data contract of «ordini»: update refused"
    assert "was NOT published" in m["body"] and "run #41" in m["body"]
    assert "[blocking] row_count: observed 50" in m["body"]
    assert _states(session) == [None, "sent"]
    (item,) = session.exec(select(AuditLog).where(AuditLog.action == "email.send")).all()
    assert item.target_type == "datasource" and '"contract_state"' in item.detail and '"refused"' in item.detail


def test_does_not_repeat_while_the_state_stays_that(session, scene):
    _report(session, scene, BROKEN, blocked=True)
    _report(session, scene, BROKEN, blocked=True)      # the next night: refused again
    _report(session, scene, BROKEN, blocked=True)
    assert _delivery(session) == 1 and len(scene.sent) == 1
    assert _states(session) == [None, "sent", None, None]


def test_notifies_when_it_is_fixed_and_when_it_breaks_again(session, scene):
    _report(session, scene, BROKEN, blocked=True)
    _report(session, scene, WELL)                      # successful refresh: the refusal is overtaken
    _report(session, scene, WARNING)
    _report(session, scene, WARNING)
    assert _delivery(session) == 3
    assert [m["subject"].rsplit(": ", 1)[1] for m in scene.sent] == ["update refused", "met again", "rules not met (warning)"]
    assert "[warning] not_null on id: 2 rows" in scene.sent[2]["body"]


def test_whoever_saves_or_checks_by_hand_receives_nothing(session, scene):
    _report(session, scene, BROKEN, trigger="save")
    _report(session, scene, WARNING, trigger="manual")
    assert _delivery(session) == 0 and scene.sent == [] and _states(session) == [None, None, None]


def test_the_first_check_gone_well_is_not_news(session, scene):
    c = contracts.save(session, scene.ds, contracts.validate_document({"rules": RULES[:1]}), True, scene.boss.id)   # new document: «pending»
    assert c.status == "pending"
    _report(session, scene, WELL)
    assert _delivery(session) == 0
    _report(session, scene, WARNING)                    # but from «pending» to a fault yes
    assert _delivery(session) == 1


def test_freshness_expiring_notifies_without_anything_arriving(session, scene):
    _report(session, scene, {**BROKEN, "rules": [{"id": "r9", "kind": "freshness", "severity": "error", "passed": False, "observed": 30.5, "expected": 24}]}, trigger="freshness")
    assert _delivery(session) == 1
    assert "contract broken" in scene.sent[0]["subject"] and "freshness (the time" in scene.sent[0]["body"]
    assert "[blocking] freshness: observed 30.5" in scene.sent[0]["body"]


def test_without_recipients_or_connection_nothing_is_marked(session, scene):
    c = contracts.get(session, scene.ds.id)
    c.notify_emails = None
    session.add(c)
    session.commit()
    _report(session, scene, BROKEN, blocked=True)
    assert _states(session) == [None, None] and _delivery(session) == 0


# ── the delivery ─────────────────────────────────────────────────────────────
def test_a_notice_taken_by_another_process_already_does_not_go_twice(session, scene):
    r = _report(session, scene, BROKEN, blocked=True)
    assert contract_notifier._mark(session, r.id, "pending", "sending")      # another process took it
    assert _delivery(session) == 0 and scene.sent == []


def test_when_the_mail_refuses_it_stays_written_and_is_not_retried_forever(session, scene):
    scene.client.answer = 502
    _report(session, scene, BROKEN, blocked=True)
    assert _delivery(session) == 0 and _states(session) == [None, "failed"]
    assert _delivery(session) == 0 and len(scene.sent) == 1


def test_a_broken_notice_does_not_stop_the_others(session, scene, monkeypatch):
    _report(session, scene, BROKEN, blocked=True)
    _report(session, scene, WELL)
    real, calls = contract_notifier._send, []

    async def _sometimes(session, rid):
        calls.append(rid)
        if len(calls) == 1:
            raise RuntimeError("posta giù")
        return await real(session, rid)

    monkeypatch.setattr(contract_notifier, "_send", _sometimes)
    assert _delivery(session) == 1 and _states(session) == [None, "failed", "sent"]


def test_slow_mail_does_not_hold_the_scheduler(session, scene):
    _report(session, scene, BROKEN, blocked=True)
    _report(session, scene, WELL)
    # the round's time used up after the first: the second waits for the next round, it is not lost
    assert asyncio.run(contract_notifier.deliver_pending(session, budget_seconds=-1)) == 0
    assert _states(session) == [None, "pending", "pending"]
    assert _delivery(session) == 2


def test_connection_or_contract_gone_the_notice_is_skipped(session, scene):
    _report(session, scene, BROKEN, blocked=True)
    session.delete(session.get(Connection, scene.smtp.id))
    session.commit()
    assert _delivery(session) == 0 and _states(session) == [None, "skipped"] and scene.sent == []


# ── who may ask to be notified ───────────────────────────────────────────────

async def _save(session, who, ds, **notify):
    return await routes.put_contract(ds.id, routes.ContractIn(document={"rules": RULES}, enabled=True, **notify), REQUEST, who, session)


@pytest.mark.anyio
async def test_the_addresses_go_through_the_connections_barriers(session, scene, fake_engine):
    out = await _save(session, scene.boss, scene.ds, notify_emails="carla@x.it; dario@x.it", notify_connection_id=scene.smtp.id)
    assert (out.notify_emails, out.notify_connection_id) == ("carla@x.it, dario@x.it", scene.smtp.id)
    with pytest.raises(HTTPException) as e:
        await _save(session, scene.boss, scene.ds, notify_emails="fuori@altrove.com")
    assert e.value.status_code == 422 and "altrove.com" not in "x.it" and "fuori@altrove.com" in e.value.detail
    with pytest.raises(HTTPException) as e:
        await _save(session, scene.boss, scene.ds, notify_emails="non-un-indirizzo")
    assert e.value.status_code == 422
    assert contracts.get(session, scene.ds.id).notify_emails == "carla@x.it, dario@x.it"     # the refusals changed nothing


@pytest.mark.anyio
async def test_choosing_a_connection_asks_connect_keeping_it_does_not(session, scene, fake_engine):
    c = contracts.get(session, scene.ds.id)
    c.notify_connection_id = None
    session.add(c)
    session.commit()
    with pytest.raises(HTTPException) as e:      # the author changes the contract, but that mail is not theirs
        await _save(session, scene.author, scene.ds, notify_connection_id=scene.smtp.id)
    assert e.value.status_code == 403
    await _save(session, scene.boss, scene.ds, notify_connection_id=scene.smtp.id)
    out = await _save(session, scene.author, scene.ds, notify_emails="alice@x.it, bruno@x.it", notify_connection_id=scene.smtp.id)   # sends everything back as it is
    assert out.notify_connection_id == scene.smtp.id
    with pytest.raises(HTTPException) as e:      # but does not decide whom someone else's mail is sent to
        await _save(session, scene.author, scene.ds, notify_emails="alice@x.it, nuovo@x.it", notify_connection_id=scene.smtp.id)
    assert e.value.status_code == 403
    assert contracts.get(session, scene.ds.id).notify_emails == "alice@x.it, bruno@x.it"
    out = await _save(session, scene.author, scene.ds, notify_connection_id=0)                                           # turning it off yes
    assert out.notify_connection_id is None


@pytest.mark.anyio
async def test_whoever_does_not_send_the_fields_does_not_change_them_and_readers_do_not_see_them(session, scene, fake_engine):
    out = await _save(session, scene.boss, scene.ds)
    assert (out.notify_emails, out.notify_connection_id) == ("alice@x.it, bruno@x.it", scene.smtp.id)
    seen = routes.get_contract(scene.ds.id, scene.reader, session)
    assert (seen.notify_emails, seen.notify_connection_id, seen.editable) == ("", None, False)
    assert routes.get_contract(scene.ds.id, scene.author, session).notify_emails == "alice@x.it, bruno@x.it"
