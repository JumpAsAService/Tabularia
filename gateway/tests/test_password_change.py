"""Changing a password: one's own from the settings menu, another user's as an
administrator.

Before 1.2.0 there was no way to change one's own password from the interface
(a getting-started run on a clean machine found it): only an administrator could,
through PATCH /users/{id}, and the panel had no field for it. The self-service
route checks the current password, applies the same rules as user creation,
closes the user's OTHER sessions and API tokens (as the administrator's change
does), writes an audit line and is throttled like sign-in.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.security import hash_password, verify_password
from app.db.session import get_session
from app.models import AuditLog, User
from app.routes import api_tokens as tokens_routes
from app.routes import auth as auth_routes
from app.routes import users as users_routes
from app.core import passwords
from app.services import login_throttle
from tests.conftest import make_user

PASSWORD = "the-current-password"
NEW = "a-brand-new-password"
CSRF = {"X-Tabularia-Csrf": "1"}
URL = "/users/me/password"


@pytest.fixture
def app(session, monkeypatch):
    monkeypatch.setenv("AUTH__PERSONAL_TOKENS", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    for r in (auth_routes.router, users_routes.router, tokens_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _user(session, email="a@x.local", **kw) -> int:
    kw.setdefault("hashed_password", hash_password(PASSWORD))
    return make_user(session, email=email, **kw).id


def _sign_in(app, email="a@x.local", password=PASSWORD) -> TestClient:
    client = TestClient(app)
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return client


def _change(client, current=PASSWORD, new=NEW, **headers):
    return client.post(URL, json={"current_password": current, "new_password": new}, headers={**CSRF, **headers})


def _hash(session, uid) -> str | None:
    session.expire_all()
    return session.get(User, uid).hashed_password


# ── one's own ───────────────────────────────────────────────────────────────

def test_the_new_password_replaces_the_old_one(app, session):
    uid = _user(session)
    client = _sign_in(app)
    r = _change(client)
    assert r.status_code == 204, r.text
    assert verify_password(NEW, _hash(session, uid)) and not verify_password(PASSWORD, _hash(session, uid))
    assert TestClient(app).post("/auth/login", json={"email": "a@x.local", "password": PASSWORD}).status_code == 401
    assert TestClient(app).post("/auth/login", json={"email": "a@x.local", "password": NEW}).status_code == 200


def test_the_other_sessions_close_this_one_stays(app, session):
    _user(session)
    here, elsewhere = _sign_in(app), _sign_in(app)
    assert _change(here).status_code == 204
    assert here.get("/auth/me").status_code == 200
    assert elsewhere.get("/auth/me").status_code == 401


def test_the_api_tokens_of_the_user_are_withdrawn(app, session):
    """Whoever had the old password could have made a token with it."""
    _user(session)
    client = _sign_in(app)
    r = client.post("/auth/tokens", json={"name": "script", "scope": "read", "expires_in_days": 30}, headers=CSRF)
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    assert TestClient(app).get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert _change(client).status_code == 204
    assert TestClient(app).get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_a_wrong_current_password_changes_nothing(app, session):
    """400, not 401: a 401 would sign the browser out."""
    uid = _user(session)
    client = _sign_in(app)
    before = _hash(session, uid)
    r = _change(client, current="not-the-password")
    assert r.status_code == 400 and "current password" in r.json()["detail"]
    assert _hash(session, uid) == before
    assert client.get("/auth/me").status_code == 200


def test_the_rules_of_user_creation_apply(app, session):
    _user(session)
    client = _sign_in(app)
    assert _change(client, new="short").status_code == 422
    assert _change(client, new="x" * (passwords.PASSWORD_MIN_LENGTH - 1)).status_code == 422
    # a published password, whatever its length and case
    r = _change(client, new="Change-Me-In-Production")
    assert r.status_code == 422 and "published" in r.text
    # bcrypt reads at most 72 bytes: longer would be cut silently (or refused with a 500)
    assert _change(client, new="é" * 37).status_code == 422
    assert _change(client, new="x" * 72).status_code == 204


# ── ONE rule, wherever a password is set ────────────────────────────────────

def test_the_seed_and_the_users_share_one_rule():
    """The bootstrap administrator's rule and the users' rule are the same list
    and the same function, not two copies that drift apart."""
    from app.core import config

    assert config.KNOWN_ADMIN_PASSWORDS is passwords.KNOWN_PASSWORDS
    assert config.ADMIN_PASSWORD_MIN == passwords.PASSWORD_MIN_LENGTH == 12
    assert passwords.PASSWORD_MAX_BYTES == 72
    for bad in ("x" * 11, "admin", "TABULARIA", "change-me-2026", "y" * 73):
        assert passwords.password_problem(bad) and config.admin_password_problem(bad)
    assert passwords.password_problem("a-long-password-of-mine") is None


@pytest.mark.parametrize("bad", ["x" * 11, "Change-me-2026", "123456789012", "y" * 73])
def test_user_creation_and_the_administrator_change_refuse_the_same(app, session, bad):
    _user(session, "boss@x.local", is_superuser=True)
    boss = _sign_in(app, "boss@x.local")
    assert boss.post("/users", json={"email": "n@x.local", "password": bad}, headers=CSRF).status_code == 422
    other = _user(session, "b@x.local")
    assert boss.patch(f"/users/{other}", json={"password": bad}, headers=CSRF).status_code == 422


def test_an_existing_short_password_keeps_working(app, session):
    """The rule applies when a password is set: an account made under the old
    6-character rule still signs in, and can move to a good password."""
    _user(session, hashed_password=hash_password("old-6c"))
    client = _sign_in(app, password="old-6c")
    assert _change(client, current="old-6c").status_code == 204


def test_the_interface_reads_the_rule_from_the_gateway(app, session):
    _user(session)
    rules = _sign_in(app).get("/users/password-rules").json()
    assert rules["min_length"] == 12 and rules["max_bytes"] == 72
    assert "admin" in rules["known"] and sorted(rules["known"]) == sorted(passwords.KNOWN_PASSWORDS)
    assert TestClient(app).get("/users/password-rules").status_code == 401


def test_too_long_passwords_are_refused_at_creation_too(app, session):
    """The same rule on POST /users: bcrypt raised on more than 72 bytes and the
    administrator got a 500."""
    _user(session, "boss@x.local", is_superuser=True)
    boss = _sign_in(app, "boss@x.local")
    r = boss.post("/users", json={"email": "n@x.local", "password": "x" * 73}, headers=CSRF)  # noqa
    assert r.status_code == 422
    r = boss.patch(f"/users/{_user(session, 'b@x.local')}", json={"password": "x" * 73}, headers=CSRF)
    assert r.status_code == 422


def test_the_change_is_in_the_audit(app, session):
    uid = _user(session)
    _change(_sign_in(app))
    line = session.exec(select(AuditLog).where(AuditLog.action == "auth.password_change")).one()
    assert line.actor_id == uid and line.target_id == uid and line.outcome == "success"
    assert NEW not in (line.detail or "") and PASSWORD not in (line.detail or "")


def test_wrong_attempts_are_throttled_like_sign_in(app, session):
    uid = _user(session)
    client = _sign_in(app)
    for _ in range(login_throttle.SOGLIA):
        assert _change(client, current="wrong-guess").status_code == 400
    # from now on even the right one waits: a stolen session cannot guess on
    r = _change(client)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert verify_password(PASSWORD, _hash(session, uid))
    failed = session.exec(select(AuditLog).where(AuditLog.action == "auth.password_change",
                                                 AuditLog.outcome == "failure")).all()
    assert len(failed) == login_throttle.SOGLIA + 1


def test_a_successful_change_clears_the_count(app, session):
    _user(session)
    client = _sign_in(app)
    for _ in range(login_throttle.SOGLIA - 1):
        _change(client, current="wrong-guess")
    assert _change(client).status_code == 204
    # the counter starts again from zero
    for _ in range(login_throttle.SOGLIA - 1):
        assert _change(client, current="wrong-guess", new="another-password").status_code == 400
    assert _change(client, current=NEW, new="another-password").status_code == 204


def test_the_throttle_does_not_lock_the_sign_in(app, session):
    """The password change counts on its own key: a stolen session that guesses
    does not lock the owner out of signing in."""
    _user(session)
    client = _sign_in(app)
    for _ in range(login_throttle.SOGLIA + 1):
        _change(client, current="wrong-guess")
    assert TestClient(app).post("/auth/login", json={"email": "a@x.local", "password": PASSWORD}).status_code == 200


def test_an_api_token_cannot_change_the_password(app, session):
    """Not even a write token: a leaked token must not become a password."""
    uid = _user(session)
    client = _sign_in(app)
    token = client.post("/auth/tokens", json={"name": "s", "scope": "write", "expires_in_days": 30},
                        headers=CSRF).json()["token"]
    r = TestClient(app).post(URL, json={"current_password": PASSWORD, "new_password": NEW},
                             headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert verify_password(PASSWORD, _hash(session, uid))


def test_an_sso_only_account_has_no_password_to_change(app, session):
    from app.services import sessions

    uid = _user(session, hashed_password=None)
    token = sessions.apri(session, session.get(User, uid), None, "sso")[1]
    bearer = {"Authorization": f"Bearer {token}"}
    me = TestClient(app).get("/auth/me", headers=bearer).json()
    assert me["sso_only"] is True
    r = TestClient(app).post(URL, json={"current_password": "", "new_password": NEW}, headers=bearer)
    assert r.status_code == 409 and "identity provider" in r.json()["detail"]
    assert _hash(session, uid) is None


def test_me_says_a_local_account_is_not_sso_only(app, session):
    _user(session)
    assert _sign_in(app).get("/auth/me").json()["sso_only"] is False


def test_an_observer_only_account_cannot_change_it(app, session):
    """The guest of a public demo is ONE account many people use: one of them
    changing the password would lock all the others out."""
    uid = _user(session, is_observer=True)
    r = _change(_sign_in(app))
    assert r.status_code == 403
    assert verify_password(PASSWORD, _hash(session, uid))


def test_without_a_session_401(app):
    assert TestClient(app).post(URL, json={"current_password": PASSWORD, "new_password": NEW}).status_code == 401


# ── an administrator, for another user ──────────────────────────────────────

def test_the_administrator_change_is_in_the_audit_too(app, session):
    _user(session, "boss@x.local", is_superuser=True)
    uid = _user(session, "b@x.local")
    boss = _sign_in(app, "boss@x.local")
    assert boss.patch(f"/users/{uid}", json={"password": NEW}, headers=CSRF).status_code == 200
    line = session.exec(select(AuditLog).where(AuditLog.action == "auth.password_change")).one()
    assert line.target_id == uid and line.actor_label == "boss@x.local"
    assert '"by_admin": true' in (line.detail or "")
