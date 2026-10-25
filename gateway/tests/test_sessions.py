"""Sessions: the token holds as long as its row exists, and it can be closed.

Before, the JWT was stateless: signing out deleted the cookie only in the browser, and
whoever had copied the token used it until expiry (12 hours). Nobody could throw
anybody out, and changing the password closed nothing. The token sat in a cookie
JavaScript read. Here the new rules, tested through the real routes with an HTTP
client: HttpOnly cookie, anti-CSRF header, a real sign-out, revocation by oneself and
by the administrator, password and deactivation that close, maximum length,
inactivity, cleanup.
"""
import re
from datetime import timedelta

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.core.security import create_access_token, hash_password
from app.db.session import get_session
from app.models import AuditLog, User, UserSession
from app.routes import audit as audit_routes
from app.routes import auth as auth_routes
from app.routes import users as users_routes
from app.services import sessions
from tests.conftest import make_user

PASSWORD = "una-password-lunga"
CSRF = {"X-Tabularia-Csrf": "1"}


@pytest.fixture
def app(session):
    app = FastAPI()
    for r in (auth_routes.router, audit_routes.router, users_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _user(session, email, **kw) -> int:
    """Creates the user and returns its id: the objects read before a request leave the
    session when the request releases it."""
    return make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw).id


def _row(session, sid: str) -> UserSession | None:
    session.expire_all()
    return session.get(UserSession, sid)


def _move(session, sid: str, **fields) -> None:
    row = _row(session, sid)
    for k, v in fields.items():
        setattr(row, k, v)
    session.add(row)
    session.commit()


def _sign_in(app, email, **headers) -> TestClient:
    """A browser signing in: from here on it has the cookie."""
    client = TestClient(app)
    r = client.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=headers)
    assert r.status_code == 200, r.text
    return client


def _sid(token: str) -> str:
    return pyjwt.decode(token, options={"verify_signature": False})["sid"]


def _cookie(client: TestClient) -> str:
    return client.cookies.get(sessions.COOKIE)


def _idle(monkeypatch, minutes: int):
    monkeypatch.setenv("AUTH__SESSION_IDLE_MINUTES", str(minutes))
    get_settings.cache_clear()


# ── sign-in and cookie ───────────────────────────────────────────────────────

def test_signing_in_opens_a_session_and_hands_it_in_an_httponly_cookie(app, session):
    uid = _user(session, "a@x.local")
    client = TestClient(app)
    r = client.post("/auth/login", json={"email": "a@x.local", "password": PASSWORD},
                    headers={"User-Agent": "Firefox di prova"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    cookie = r.headers["set-cookie"]
    # the page's JavaScript does not read it; the interface's server sees it too
    assert cookie.startswith(f"tab_token={token};")
    assert "HttpOnly" in cookie and "Path=/" in cookie and "SameSite=lax" in cookie
    # lasts as long as the session
    duration = int(re.search(r"Max-Age=(\d+)", cookie).group(1))
    assert get_settings().jwt.access_ttl_minutes * 60 - 5 <= duration <= get_settings().jwt.access_ttl_minutes * 60
    # on http no Secure (development on localhost); behind a proxy on HTTPS yes
    assert "Secure" not in cookie
    row = _row(session, _sid(token))
    assert row.user_id == uid and row.method == "password"
    assert row.ip == "testclient" and row.user_agent == "Firefox di prova"


def test_behind_an_https_proxy_the_cookie_is_secure(app, session):
    _user(session, "a@x.local")
    r = TestClient(app).post("/auth/login", json={"email": "a@x.local", "password": PASSWORD},
                             headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers["set-cookie"]


def test_the_cookie_is_enough_to_read(app, session):
    _user(session, "a@x.local")
    client = _sign_in(app, "a@x.local")
    r = client.get("/auth/me")
    assert r.status_code == 200 and r.json()["email"] == "a@x.local"


def test_with_the_cookie_alone_a_changing_request_wants_the_anti_csrf_header(app, session):
    """The browser attaches the cookie even to a form started from another site; the header
    it does not."""
    _user(session, "a@x.local")
    client = _sign_in(app, "a@x.local")
    r = client.post("/auth/sessions/close-others")
    assert r.status_code == 403 and "X-Tabularia-Csrf" in r.json()["detail"]
    assert client.post("/auth/sessions/close-others", headers=CSRF).status_code == 204


def test_with_the_authorization_header_anti_csrf_is_not_needed(app, session):
    """A script, or a page of another site, cannot put the Authorization header without
    knowing the token: there is no CSRF there."""
    u = session.get(User, _user(session, "a@x.local"))
    token = sessions.open_session(session, u, None, "password")[1]
    r = TestClient(app).post("/auth/sessions/close-others", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 204


def test_without_token_nor_cookie_401(app):
    assert TestClient(app).get("/auth/me").status_code == 401


# ── signing out is real ──────────────────────────────────────────────────────

def test_signing_out_closes_the_session_for_whoever_copied_the_token_too(app, session):
    _user(session, "a@x.local")
    client = _sign_in(app, "a@x.local")
    copied = _cookie(client)
    r = client.post("/auth/logout", headers=CSRF)
    assert r.status_code == 204
    # the cookie is deleted in the browser…
    assert r.headers["set-cookie"].startswith('tab_token=""') and "Max-Age=0" in r.headers["set-cookie"]
    # …and the token no longer holds elsewhere either
    assert TestClient(app).get("/auth/me", headers={"Authorization": f"Bearer {copied}"}).status_code == 401
    assert _row(session, _sid(copied)) is None
    assert session.exec(select(AuditLog).where(AuditLog.action == "auth.logout")).first() is not None


def test_a_401_given_to_a_cookie_deletes_it(app, session):
    """Otherwise the interface's server would keep seeing a cookie and believing there is a
    session, at every page loaded from scratch."""
    uid = _user(session, "a@x.local")
    client = _sign_in(app, "a@x.local")
    sessions.close_all(session, uid)
    r = client.get("/auth/me")
    assert r.status_code == 401
    assert r.headers["set-cookie"].startswith('tab_token=""') and "Max-Age=0" in r.headers["set-cookie"]


def test_a_token_without_session_does_not_hold(app, session):
    """A token from before the sessions (without `sid`) could not be revoked: at the update
    one signs in once, and that is it."""
    uid = _user(session, "a@x.local")
    s = get_settings().jwt
    old = pyjwt.encode({"sub": str(uid), "exp": 9_999_999_999}, s.secret.get_secret_value(), algorithm=s.algorithm)
    for token in (old, create_access_token(uid, "inventata")):
        assert TestClient(app).get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ── length and inactivity ────────────────────────────────────────────────────

def test_past_the_maximum_length_it_does_not_hold(app, session):
    _user(session, "a@x.local")
    client = _sign_in(app, "a@x.local")
    _move(session, _sid(_cookie(client)), expires_at=sessions.now() - timedelta(seconds=1))
    assert client.get("/auth/me").status_code == 401


def test_still_past_the_idle_timeout_it_does_not_hold(app, session, monkeypatch):
    _idle(monkeypatch, 30)
    _user(session, "a@x.local")
    client = _sign_in(app, "a@x.local")
    sid = _sid(_cookie(client))
    # twenty minutes ago: it holds, and the activity is updated
    _move(session, sid, last_seen_at=sessions.now() - timedelta(minutes=20))
    assert client.get("/auth/me").status_code == 200
    assert sessions.now() - _row(session, sid).last_seen_at < timedelta(seconds=5)
    # forty minutes ago: it no longer holds
    _move(session, sid, last_seen_at=sessions.now() - timedelta(minutes=40))
    assert client.get("/auth/me").status_code == 401


def test_without_timeout_only_the_maximum_length_counts(app, session, monkeypatch):
    _idle(monkeypatch, 0)
    _user(session, "a@x.local")
    client = _sign_in(app, "a@x.local")
    _move(session, _sid(_cookie(client)), last_seen_at=sessions.now() - timedelta(hours=11))
    assert client.get("/auth/me").status_code == 200


def test_the_page_knows_after_how_long_to_sign_out_by_itself(app, session, monkeypatch):
    _idle(monkeypatch, 45)
    _user(session, "a@x.local")
    assert _sign_in(app, "a@x.local").get("/auth/me").json()["session_idle_minutes"] == 45


def test_the_cleanup_removes_the_expired_and_still_ones(session, monkeypatch):
    _idle(monkeypatch, 30)
    u = session.get(User, _user(session, "a@x.local"))
    viva, expired, stop = (sessions.open_session(session, u, None, "password")[0] for _ in range(3))
    ids = viva.id, expired.id, stop.id
    expired.expires_at = sessions.now() - timedelta(minutes=1)
    stop.last_seen_at = sessions.now() - timedelta(hours=1)
    session.add_all([expired, stop])
    session.commit()
    assert sessions.clean(session) == 2
    session.expire_all()
    assert [s.id for s in session.exec(select(UserSession)).all()] == [ids[0]]


# ── one's own sessions ───────────────────────────────────────────────────────

def test_everyone_sees_and_closes_only_their_own(app, session):
    _user(session, "a@x.local")
    _user(session, "b@x.local")
    here, elsewhere = _sign_in(app, "a@x.local"), _sign_in(app, "a@x.local")
    stranger = _sign_in(app, "b@x.local")

    listing = here.get("/auth/sessions").json()
    assert len(listing) == 2
    assert [s["current"] for s in listing if s["id"] == _sid(_cookie(here))] == [True]

    # someone else's is not seen: 404, as if it did not exist
    assert stranger.delete(f"/auth/sessions/{_sid(_cookie(here))}", headers=CSRF).status_code == 404
    assert here.get("/auth/me").status_code == 200

    assert here.delete(f"/auth/sessions/{_sid(_cookie(elsewhere))}", headers=CSRF).status_code == 204
    assert elsewhere.get("/auth/me").status_code == 401
    assert here.get("/auth/me").status_code == 200
    item = session.exec(select(AuditLog).where(AuditLog.action == "auth.session_revoke")).first()
    assert item is not None and item.target_label == "a@x.local"


def test_sign_out_of_the_other_devices_leaves_this_one_open(app, session):
    _user(session, "a@x.local")
    here, elsewhere, still = (_sign_in(app, "a@x.local") for _ in range(3))
    assert here.post("/auth/sessions/close-others", headers=CSRF).status_code == 204
    assert here.get("/auth/me").status_code == 200
    assert elsewhere.get("/auth/me").status_code == 401 and still.get("/auth/me").status_code == 401


# ── the administrator ────────────────────────────────────────────────────────

def test_the_administrator_sees_every_session_and_closes_one(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    _user(session, "a@x.local", full_name="Anna")
    boss = _sign_in(app, "capo@x.local")
    anna = _sign_in(app, "a@x.local", **{"User-Agent": "Firefox di Anna"})

    listing = boss.get("/audit/sessions").json()["items"]
    annas = next(s for s in listing if s["email"] == "a@x.local")
    assert annas["full_name"] == "Anna" and annas["user_agent"] == "Firefox di Anna"
    assert annas["ip"] == "testclient" and annas["online"] and not annas["current"]
    assert [s["current"] for s in listing if s["email"] == "capo@x.local"] == [True]

    assert boss.delete(f"/audit/sessions/{annas['id']}", headers=CSRF).status_code == 204
    assert anna.get("/auth/me").status_code == 401
    item = session.exec(select(AuditLog).where(AuditLog.action == "auth.session_revoke")).first()
    assert item.actor_label == "capo@x.local" and item.target_label == "a@x.local"
    assert boss.delete(f"/audit/sessions/{annas['id']}", headers=CSRF).status_code == 404


def test_the_observer_sees_the_sessions_without_who_nor_where_and_closes_none(app, session):
    _user(session, "occhio@x.local", is_observer=True)
    _user(session, "a@x.local", full_name="Anna")
    eye = _sign_in(app, "occhio@x.local")
    anna = _sign_in(app, "a@x.local", **{"User-Agent": "Firefox di Anna"})

    # the observer gets a handle, not the real id
    annas = next(s for s in eye.get("/audit/sessions").json()["items"] if s["id"] == sessions.handle(_sid(_cookie(anna))))
    assert annas["email"] != "a@x.local" and annas["full_name"] == ""
    assert annas["ip"] is None and annas["user_agent"] is None
    assert eye.delete(f"/audit/sessions/{annas['id']}", headers=CSRF).status_code == 403
    assert eye.delete(f"/audit/sessions/{_sid(_cookie(anna))}", headers=CSRF).status_code == 403
    assert anna.get("/auth/me").status_code == 200


def test_an_observers_session_keeps_neither_address_nor_browser(app, session):
    _user(session, "occhio@x.local", is_observer=True)
    client = _sign_in(app, "occhio@x.local", **{"User-Agent": "Firefox"})
    row = _row(session, _sid(_cookie(client)))
    assert row.ip is None and row.user_agent is None


def test_a_new_password_closes_the_users_sessions(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    a = _user(session, "a@x.local")
    boss = _sign_in(app, "capo@x.local")
    anna, anna_elsewhere = _sign_in(app, "a@x.local"), _sign_in(app, "a@x.local")
    r = boss.patch(f"/users/{a}", json={"password": "un-altra-password"}, headers=CSRF)
    assert r.status_code == 200
    assert anna.get("/auth/me").status_code == 401 and anna_elsewhere.get("/auth/me").status_code == 401
    item = session.exec(select(AuditLog).where(AuditLog.action == "auth.session_revoke")).first()
    assert item.detail and '"password"' in item.detail


def test_whoever_changes_their_password_stays_in_where_they_do_it(app, session):
    boss_id = _user(session, "capo@x.local", is_superuser=True)
    here, elsewhere = _sign_in(app, "capo@x.local"), _sign_in(app, "capo@x.local")
    assert here.patch(f"/users/{boss_id}", json={"password": "un-altra-password"}, headers=CSRF).status_code == 200
    assert here.get("/auth/me").status_code == 200
    assert elsewhere.get("/auth/me").status_code == 401


def test_disabling_an_account_closes_its_sessions(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    a = _user(session, "a@x.local")
    boss = _sign_in(app, "capo@x.local")
    _sign_in(app, "a@x.local")
    assert boss.patch(f"/users/{a}", json={"is_active": False}, headers=CSRF).status_code == 200
    session.expire_all()
    assert session.exec(select(UserSession).where(UserSession.user_id == a)).all() == []


def test_changing_only_the_name_closes_nothing(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    a = _user(session, "a@x.local")
    boss = _sign_in(app, "capo@x.local")
    anna = _sign_in(app, "a@x.local")
    assert boss.patch(f"/users/{a}", json={"full_name": "Anna B."}, headers=CSRF).status_code == 200
    assert anna.get("/auth/me").status_code == 200


def test_deleting_a_signed_in_user_removes_their_sessions_too(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    a = _user(session, "a@x.local")
    boss = _sign_in(app, "capo@x.local")
    _sign_in(app, "a@x.local")
    assert boss.delete(f"/users/{a}", headers=CSRF).status_code == 204
    session.expire_all()
    assert session.exec(select(UserSession).where(UserSession.user_id == a)).all() == []


# ── from the review ──────────────────────────────────────────────────────────

def test_the_shared_guest_sees_and_closes_only_its_own_session(app, session):
    """The observer is a public demo's account, which everyone signs in with: with «close the
    others» it would throw out every other visitor."""
    _user(session, "ospite@x.local", is_observer=True)
    io, other_visitor = _sign_in(app, "ospite@x.local"), _sign_in(app, "ospite@x.local")
    listing = io.get("/auth/sessions").json()
    assert [s["id"] for s in listing] == [_sid(_cookie(io))]
    assert io.post("/auth/sessions/close-others", headers=CSRF).status_code == 403
    assert io.delete(f"/auth/sessions/{_sid(_cookie(other_visitor))}", headers=CSRF).status_code == 403
    assert other_visitor.get("/auth/me").status_code == 200
    # it closes its own: that is signing out
    assert io.delete(f"/auth/sessions/{_sid(_cookie(io))}", headers=CSRF).status_code == 204
    assert other_visitor.get("/auth/me").status_code == 200


def test_to_the_observer_the_audit_does_not_hand_over_the_real_ids(app, session):
    _user(session, "occhio@x.local", is_observer=True)
    _user(session, "a@x.local")
    eye, anna = _sign_in(app, "occhio@x.local"), _sign_in(app, "a@x.local")
    ids = {s["id"] for s in eye.get("/audit/sessions").json()["items"]}
    assert _sid(_cookie(anna)) not in ids and all(i.startswith("h-") for i in ids)


def test_the_interface_gets_no_token_in_the_body(app, session):
    """The interface sends the anti-CSRF header at sign-in too: the cookie is enough for it,
    and a token in the body could be read by a script of the page."""
    _user(session, "a@x.local")
    client = TestClient(app)
    r = client.post("/auth/login", json={"email": "a@x.local", "password": PASSWORD}, headers=CSRF)
    assert r.status_code == 200 and r.json()["access_token"] == ""
    assert client.get("/auth/me").status_code == 200
    # a script (without the header) receives it as before
    assert TestClient(app).post("/auth/login", json={"email": "a@x.local", "password": PASSWORD}).json()["access_token"]


def test_a_new_sign_in_from_the_same_browser_closes_the_session_of_before(app, session):
    _user(session, "a@x.local")
    _user(session, "b@x.local")
    browser = _sign_in(app, "a@x.local")
    before = _cookie(browser)
    r = browser.post("/auth/login", json={"email": "b@x.local", "password": PASSWORD})
    assert r.status_code == 200
    assert _row(session, _sid(before)) is None
    assert browser.get("/auth/me").json()["email"] == "b@x.local"


def test_a_refused_bearer_does_not_delete_the_good_cookie(app, session):
    """A script's expired token tried from /docs, in the same browser: the 401 is its own,
    the interface's cookie stays."""
    _user(session, "a@x.local")
    client = _sign_in(app, "a@x.local")
    r = client.get("/auth/me", headers={"Authorization": "Bearer non-un-token"})
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert client.get("/auth/me").status_code == 200


def test_the_cors_origins_do_not_allow_the_asterisk(monkeypatch):
    """With credentials, Starlette would mirror any origin: every site visited would use
    the visitor's session."""
    from pydantic import ValidationError

    from app.core.config import Settings

    for bad_one in ('["*"]', '["null"]', '["https://app.example.com", "*"]'):
        monkeypatch.setenv("APP__CORS_ORIGINS", bad_one)
        with pytest.raises(ValidationError):
            Settings()
    monkeypatch.setenv("APP__CORS_ORIGINS", '["https://app.example.com"]')
    assert Settings().app.cors_origins == ["https://app.example.com"]


def test_with_the_domain_configured_the_cookie_carries_it(app, session, monkeypatch):
    """Interface and gateway on two hosts of the same domain: the interface's server must see
    the cookie."""
    monkeypatch.setenv("AUTH__COOKIE_DOMAIN", "example.com")
    get_settings.cache_clear()
    _user(session, "a@x.local")
    r = TestClient(app).post("/auth/login", json={"email": "a@x.local", "password": PASSWORD})
    assert "Domain=example.com" in r.headers["set-cookie"]


def test_the_proxy_forwards_neither_cookie_nor_session_headers_to_the_engine():
    from app.routes import proxy

    request_ = type("R", (), {"headers": {
        "Cookie": "tab_token=segreto", "Authorization": "Bearer segreto",
        "X-Tabularia-Csrf": "1", "Accept": "application/json",
    }})()
    forwarded = {k.lower() for k in proxy._request_headers(request_)}
    assert forwarded == {"accept"}


# ── from the second review ───────────────────────────────────────────────────

def test_a_password_changed_while_signing_in_opens_no_session(app, session, monkeypatch):
    """Between the password check (bcrypt, a quarter of a second) and the session opening an
    administrator changes it and closes the sessions: whoever was signing in with the old
    one must not find a session born after the closing."""
    from app.routes import auth as auth_routes

    uid = _user(session, "a@x.local")
    vera = auth_routes.verify_password

    def check_and_change_meanwhile(password, hashed):
        outcome = vera(password, hashed)
        # the administrator's change, arriving with the check done
        u = session.get(User, uid)
        u.hashed_password = hash_password("la-password-nuova")
        session.add(u)
        sessions.close_all(session, uid, commit=False)
        session.commit()
        return outcome

    monkeypatch.setattr(auth_routes, "verify_password", check_and_change_meanwhile)
    r = TestClient(app).post("/auth/login", json={"email": "a@x.local", "password": PASSWORD})
    assert r.status_code == 401
    session.expire_all()
    assert session.exec(select(UserSession).where(UserSession.user_id == uid)).all() == []
    item = session.exec(select(AuditLog).where(AuditLog.action == "auth.login_failed")).first()
    assert item is not None and "cambiata" in (item.detail or "")


def test_the_listings_carry_no_expired_or_still_sessions(app, session, monkeypatch):
    _idle(monkeypatch, 30)
    _user(session, "capo@x.local", is_superuser=True)
    boss = _sign_in(app, "capo@x.local")
    expired, stop = _sign_in(app, "capo@x.local"), _sign_in(app, "capo@x.local")
    _move(session, _sid(_cookie(expired)), expires_at=sessions.now() - timedelta(minutes=1))
    _move(session, _sid(_cookie(stop)), last_seen_at=sessions.now() - timedelta(hours=1))
    assert [s["current"] for s in boss.get("/auth/sessions").json()] == [True]
    page = boss.get("/audit/sessions").json()
    assert page["total"] == 1 and len(page["items"]) == 1


def test_the_audit_listing_carries_the_newest_and_the_total(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    _user(session, "a@x.local")
    boss = _sign_in(app, "capo@x.local")
    for _ in range(3):
        _sign_in(app, "a@x.local")
    page = boss.get("/audit/sessions?limit=2").json()
    assert len(page["items"]) == 2 and page["total"] == 4


def test_to_the_observer_the_log_shows_neither_the_person_nor_the_browser(app, session):
    """The session closings target the user: in clear next to the actor's mask they would
    say who it is."""
    _user(session, "capo@x.local", is_superuser=True)
    _user(session, "occhio@x.local", is_observer=True)
    _user(session, "anna@x.local")
    boss = _sign_in(app, "capo@x.local", **{"User-Agent": "Firefox del capo"})
    anna = _sign_in(app, "anna@x.local")
    boss.delete(f"/audit/sessions/{_sid(_cookie(anna))}", headers=CSRF)
    eye = _sign_in(app, "occhio@x.local")
    items = eye.get("/audit?action=auth.session_revoke").json()["items"]
    assert items and all("anna@x.local" not in (v["target_label"] or "") for v in items)
    assert all(v["user_agent"] is None and v["ip"] is None for v in items)
    # the administrator sees them
    full = boss.get("/audit?action=auth.session_revoke").json()["items"]
    assert any(v["target_label"] == "anna@x.local" for v in full) and all(v["user_agent"] for v in full)


def test_the_address_sits_only_in_the_session(app, session):
    """`users.last_seen_ip` is no longer written: nobody read it and nobody would have
    deleted it. The address sits in the session and goes with it."""
    uid = _user(session, "a@x.local")
    client = _sign_in(app, "a@x.local")
    assert client.get("/auth/me").status_code == 200
    session.expire_all()
    assert session.get(User, uid).last_seen_ip is None
    assert session.get(User, uid).last_seen_at is not None
    assert _row(session, _sid(_cookie(client))).ip == "testclient"
