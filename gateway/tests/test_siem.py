"""Forwarding the audit to Splunk (HEC): the items all go out, once (or, after a fault
halfway, again with the same id), in order, only when really written; a fault skips
none; only an administrator changes the configuration, the token is not read back, the
observer sees neither address nor errors.
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.crypto import encrypt_secret
from app.db.session import get_session
from app.models import AuditLog, SiemDestination
from app.routes import siem as siem_routes
from app.services import siem, webhooks
from tests.conftest import make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"
TOKEN = "11111111-2222-3333-4444-555555555555"


class FakeSplunk:
    """The HEC as far as needed here: checks the token, counts the events, answers as Splunk
    does ({"text": "Success", "code": 0} or an error)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.events: list[dict] = []
        self.answers: list[tuple[int, dict]] = []  # in line; empty = success

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.headers.get("Authorization") != f"Splunk {TOKEN}":
            return httpx.Response(403, json={"text": "Invalid token", "code": 4})
        if self.answers:
            code, body = self.answers.pop(0)
            return httpx.Response(code, json=body)
        self.events += [json.loads(r) for r in req.content.decode().split("\n") if r]
        return httpx.Response(200, json={"text": "Success", "code": 0})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


@pytest.fixture
def splunk(monkeypatch):
    s = FakeSplunk()

    async def resolve(url):  # no DNS in the tests
        u = httpx.URL(url)
        return u, u.raw_host.decode(), "10.20.30.40"
    monkeypatch.setattr(webhooks, "_resolve", resolve)
    real = siem.send

    async def send(d, events, client=None):
        return await real(d, events, client or s.client())
    monkeypatch.setattr(siem, "send", send)
    return s


def _destination(session, **kw):
    d = SiemDestination(id=1, enabled=True, url="https://splunk.interno:8088", token_enc=encrypt_secret(TOKEN), **kw)
    session.add(d)
    session.commit()
    return d


def _item(session, action="flow.create", seconds_ago=60, **kw) -> int:
    v = AuditLog(ts=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago), action=action,
                 actor_label=kw.pop("actor_label", "anna@x.local"), **kw)
    session.add(v)
    session.commit()
    return v.id


def _dest(session) -> SiemDestination:
    session.expire_all()
    return session.get(SiemDestination, 1)


# ── what goes out ────────────────────────────────────────────────────────────

def test_an_item_becomes_a_hec_event_with_the_names_splunk_expects(session):
    d = SiemDestination(index="sicurezza", host="tabularia-prod")
    v = AuditLog(id=7, ts=datetime(2026, 10, 12, 8, 30, 0), action="auth.login", outcome="failure",
                 actor_label="anna@x.local", actor_id=3, ip="10.1.2.3", user_agent="Firefox",
                 target_type="user", target_id=3, target_label="anna@x.local", detail=json.dumps({"reason": "password"}))
    e = siem.event(v, d)
    assert e["time"] == datetime(2026, 10, 12, 8, 30, tzinfo=timezone.utc).timestamp()
    assert (e["index"], e["host"], e["source"], e["sourcetype"]) == ("sicurezza", "tabularia-prod", "tabularia", "tabularia:audit")
    assert e["event"] == {
        "id": 7, "time": "2026-10-12T08:30:00Z", "action": "auth.login", "outcome": "failure",
        "user": "anna@x.local", "user_id": 3, "src": "10.1.2.3", "user_agent": "Firefox",
        "target_type": "user", "target_id": 3, "target_label": "anna@x.local",
        "detail": {"reason": "password"}, "app": "tabularia",
    }
    # no index: the token's default one; no empty fields
    e = siem.event(AuditLog(id=8, ts=datetime(2026, 10, 12), action="x", actor_label="y"), SiemDestination())
    assert "index" not in e and "src" not in e["event"] and "detail" not in e["event"]


@pytest.mark.parametrize("written, expected", [
    ("https://splunk:8088", "https://splunk:8088/services/collector/event"),
    ("https://splunk:8088/", "https://splunk:8088/services/collector/event"),
    ("https://splunk:8088/services/collector", "https://splunk:8088/services/collector/event"),
    ("https://splunk:8088/services/collector/event", "https://splunk:8088/services/collector/event"),
    ("https://proxy.interno/hec/raccogli", "https://proxy.interno/hec/raccogli"),
])
def test_the_address_can_be_pasted_in_any_form(written, expected):
    assert siem.endpoint(written) == expected


# ── how it goes out ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_the_items_go_out_in_order_with_the_token_and_the_cursor_moves(db_engine, session, splunk):
    _destination(session)
    ids = [_item(session, f"azione.{i}") for i in range(3)]
    assert await siem.one_round(db_engine) == 3
    assert [e["event"]["id"] for e in splunk.events] == ids
    req = splunk.requests[0]
    assert req.url.host == "10.20.30.40" and req.headers["Host"] == "splunk.interno:8088"  # to the checked address
    assert req.url.path == "/services/collector/event" and req.headers["X-Splunk-Request-Channel"]
    d = _dest(session)
    assert (d.cursor_id, d.sent_total, d.failures) == (ids[-1], 3, 0) and d.last_ok_at
    assert await siem.one_round(db_engine) == 0  # nothing twice


@pytest.mark.anyio
async def test_the_items_just_written_wait_a_few_seconds(db_engine, session, splunk):
    """The ids are assigned before the commit: an item younger than the delay could have
    before it a lower id not written yet."""
    _destination(session)
    old_one = _item(session, seconds_ago=60)
    _item(session, seconds_ago=1)
    await siem.one_round(db_engine)
    assert [e["event"]["id"] for e in splunk.events] == [old_one]


@pytest.mark.anyio
async def test_splunk_down_nothing_is_skipped_and_the_wait_grows(db_engine, session, splunk):
    _destination(session)
    ids = [_item(session) for _ in range(2)]
    splunk.answers = [(503, {"text": "Server is busy", "code": 9})]
    assert await siem.one_round(db_engine) == 0
    d = _dest(session)
    assert d.cursor_id == 0 and d.failures == 1 and "Server is busy" in d.last_error
    assert d.next_attempt_at > siem.now()
    assert await siem.one_round(db_engine) == 0  # no retrying before the next attempt
    d.next_attempt_at = siem.now() - timedelta(seconds=1)
    session.add(d)
    session.commit()
    assert await siem.one_round(db_engine) == 2
    assert [e["event"]["id"] for e in splunk.events] == ids  # the same ones, none lost
    d = _dest(session)
    assert d.failures == 0 and d.next_attempt_at is None  # the earlier error stays as history, with its time


def test_the_waits_grow_up_to_the_cap():
    assert [siem.wait(n) for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert siem.wait(30) == siem.MAX_WAIT


@pytest.mark.anyio
async def test_a_wrong_token_says_so_and_retries(db_engine, session, splunk):
    _destination(session)
    d = _dest(session)
    d.token_enc = encrypt_secret("sbagliato")
    session.add(d)
    session.commit()
    _item(session)
    await siem.one_round(db_engine)
    d = _dest(session)
    assert d.cursor_id == 0 and "Invalid token" in d.last_error and "403" in d.last_error


@pytest.mark.anyio
async def test_a_batch_does_not_exceed_the_byte_cap(db_engine, session, splunk, monkeypatch):
    monkeypatch.setattr(siem, "MAX_BYTE", 3000)
    _destination(session)
    ids = [_item(session, detail=json.dumps({"x": "a" * 1000})) for _ in range(6)]
    assert await siem.one_round(db_engine) == 6
    assert len(splunk.requests) > 1 and all(len(r.content) <= 3000 for r in splunk.requests)
    assert [e["event"]["id"] for e in splunk.events] == ids


@pytest.mark.anyio
async def test_off_nothing_goes_out(db_engine, session, splunk):
    _destination(session)
    d = _dest(session)
    d.enabled = False
    session.add(d)
    session.commit()
    _item(session)
    assert await siem.one_round(db_engine) == 0 and splunk.requests == []


# ── the panel ────────────────────────────────────────────────────────────────

@pytest.fixture
def app(session, splunk):
    from app.routes import api_tokens as tokens_routes
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, tokens_routes.router, siem_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _sign_in(app, session, email, **kw) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw)
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def test_turns_on_with_address_and_token_and_starts_from_now(app, session, splunk):
    old_one = _item(session)
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.put("/admin/siem", json={"enabled": True}, headers=CSRF)
    assert r.status_code == 422  # not without address and token
    r = boss.put("/admin/siem", json={"url": "https://splunk.interno:8088", "token": TOKEN, "index": "sicurezza",
                                      "enabled": True}, headers=CSRF)
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["enabled"] and st["token_set"] and "token" not in st and TOKEN not in r.text
    d = _dest(session)
    assert d.cursor_id >= old_one  # the earlier history stays in the Audit panel
    assert d.token_enc and TOKEN not in d.token_enc  # cifrato
    item = session.exec(select(AuditLog).where(AuditLog.action == "siem.update")).one()
    assert json.loads(item.detail)["changes"]["token"] == "changed" and TOKEN not in item.detail


def test_off_and_on_again_it_resumes_where_it_was(app, session, splunk):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    boss.put("/admin/siem", json={"url": "https://s:8088", "token": TOKEN, "enabled": True}, headers=CSRF)
    cursor = _dest(session).cursor_id
    boss.put("/admin/siem", json={"enabled": False}, headers=CSRF)
    _item(session)
    boss.put("/admin/siem", json={"enabled": True}, headers=CSRF)
    assert _dest(session).cursor_id == cursor  # the items from while it was off will go out


def test_the_test_says_what_splunk_answered(app, session, splunk):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    boss.put("/admin/siem", json={"url": "https://s:8088", "token": TOKEN}, headers=CSRF)
    r = boss.post("/admin/siem/test", headers=CSRF).json()
    assert r == {"ok": True, "status_code": 200, "message": "Success"}
    assert splunk.events[-1]["event"]["action"] == "siem.test" and splunk.events[-1]["event"]["test"] is True
    boss.put("/admin/siem", json={"token": "sbagliato"}, headers=CSRF)
    r = boss.post("/admin/siem/test", headers=CSRF).json()
    assert not r["ok"] and r["status_code"] == 403 and "Invalid token" in r["message"]


def test_the_observer_sees_the_state_but_not_address_and_errors(app, session, splunk):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    boss.put("/admin/siem", json={"url": "https://splunk.interno:8088/services/collector", "token": TOKEN, "enabled": True}, headers=CSRF)
    d = _dest(session)
    d.last_error = "response 403: Invalid token"
    session.add(d)
    session.commit()
    eye = _sign_in(app, session, "occhio@x.local", is_observer=True)
    st = eye.get("/admin/siem").json()
    assert st["url"] == "https://splunk.interno/…" and st["last_error"] is None and not st["editable"]
    assert eye.put("/admin/siem", json={"enabled": False}, headers=CSRF).status_code == 403
    assert eye.post("/admin/siem/test", headers=CSRF).status_code == 403
    assert _sign_in(app, session, "anna@x.local").get("/admin/siem").status_code == 403


def test_an_api_token_does_not_change_the_destination(app, session, splunk):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    token = boss.post("/auth/tokens", json={"name": "x", "scope": "write", "expires_in_days": 5}, headers=CSRF).json()["token"]
    script = TestClient(app)
    h = {"Authorization": f"Bearer {token}"}
    assert script.get("/admin/siem", headers=h).json()["editable"] is False
    # with the wrong body too: 403, not 422
    assert script.put("/admin/siem", json={"url": "https://altrove:8088", "token": TOKEN}, headers=h).status_code == 403
    assert script.put("/admin/siem", json={"enabled": "forse"}, headers=h).status_code == 403
    assert script.post("/admin/siem/test", headers=h).status_code == 403


def test_the_address_is_checked(app, session, splunk):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    for url in ("ftp://splunk/x", "splunk:8088", "http://0177.0.0.1:8088"):
        assert boss.put("/admin/siem", json={"url": url}, headers=CSRF).status_code == 422, url


def test_the_state_says_how_many_items_wait(app, session, splunk):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    boss.put("/admin/siem", json={"url": "https://s:8088", "token": TOKEN, "enabled": True}, headers=CSRF)
    for _ in range(3):
        _item(session)
    st = boss.get("/admin/siem").json()
    assert st["backlog"] >= 3 and st["oldest_pending_at"]
