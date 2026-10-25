"""API tokens and service accounts: the credential of scripts and integrations.

The rules these tests pin: the secret is seen once and not stored; a token holds with
its user's permissions and, when read-only, changes nothing; it expires, is revoked,
dies with its account; it travels only in the header; it creates no other tokens; the
audit says with which token what was done. Service accounts do not sign in with a
password or SSO.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_session
from app.models import ApiToken, AuditLog, Project, User
from app.routes import api_tokens as tokens_routes
from app.routes import auth as auth_routes
from app.routes import projects as projects_routes
from app.routes import service_accounts as sa_routes
from app.routes import users as users_routes
from app.services import api_tokens
from tests.conftest import make_user

PASSWORD = "una-password-lunga"
CSRF = {"X-Tabularia-Csrf": "1"}


@pytest.fixture
def app(session):
    app = FastAPI()
    for r in (auth_routes.router, tokens_routes.router, sa_routes.router, users_routes.router, projects_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _user(session, email, **kw) -> int:
    return make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw).id


def _sign_in(app, email) -> TestClient:
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _create(client, **kw):
    body = {"name": "script", "scope": "read", "expires_in_days": 30, **kw}
    r = client.post("/auth/tokens", json=body, headers=CSRF)
    assert r.status_code == 201, r.text
    return r.json()


# ── the token ───────────────────────────────────────────────────────────────

def test_the_secret_is_seen_once_and_the_database_keeps_the_fingerprint(app, session):
    _user(session, "a@x.local")
    browser = _sign_in(app, "a@x.local")
    created = _create(browser, name="report notturno")
    assert created["token"].startswith("tab_") and created["prefix"] == created["token"][:12]
    row = session.exec(select(ApiToken)).one()
    assert row.token_hash == api_tokens.fingerprint(created["token"]) and created["token"] not in row.token_hash
    listing = browser.get("/auth/tokens").json()
    assert [t["name"] for t in listing] == ["report notturno"] and "token" not in listing[0]


def test_a_token_is_worth_as_much_as_its_user(app, session):
    _user(session, "a@x.local", full_name="Anna")
    token = _create(_sign_in(app, "a@x.local"))["token"]
    r = TestClient(app).get("/auth/me", headers=_bearer(token))
    assert r.status_code == 200 and r.json()["email"] == "a@x.local"


def test_read_only_changes_nothing(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    browser = _sign_in(app, "capo@x.local")
    reading = _create(browser, scope="read")["token"]
    writing = _create(browser, name="scrive", scope="write")["token"]
    script = TestClient(app)
    assert script.get("/projects", headers=_bearer(reading)).status_code == 200
    r = script.post("/projects", json={"name": "zz"}, headers={**_bearer(reading), "Accept-Language": "it"})
    assert r.status_code == 403 and "sola lettura" in r.json()["detail"]
    # in the asker's language
    r = script.post("/projects", json={"name": "zz"}, headers={**_bearer(reading), "Accept-Language": "en"})
    assert "Read-only token" in r.json()["detail"]
    assert script.post("/projects", json={"name": "zz"}, headers=_bearer(writing)).status_code in (200, 201)


def test_the_audit_says_with_which_token_it_was_done(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    browser = _sign_in(app, "capo@x.local")
    token = _create(browser, name="airflow notte", scope="write")["token"]
    old = _create(browser, name="da ritirare")
    # a script withdrawing a credential: an action the audit records
    assert TestClient(app).delete(f"/auth/tokens/{old['id']}", headers=_bearer(token)).status_code == 204
    item = session.exec(select(AuditLog).where(AuditLog.action == "token.revoke")).one()
    assert '"token": "airflow notte"' in item.detail and '"name": "da ritirare"' in item.detail
    # the secret never ends up in the log
    assert not any(token in (v.detail or "") for v in session.exec(select(AuditLog)).all())


def test_expired_revoked_or_invented_does_not_hold(app, session):
    uid = _user(session, "a@x.local")
    browser = _sign_in(app, "a@x.local")
    expired, revoked_one = _create(browser, name="uno")["token"], _create(browser, name="due")
    row = session.exec(select(ApiToken).where(ApiToken.name == "uno")).one()
    row.expires_at = api_tokens.now()
    session.add(row)
    session.commit()
    assert browser.delete(f"/auth/tokens/{revoked_one['id']}", headers=CSRF).status_code == 204
    for t in (expired, revoked_one["token"], "tab_inventato"):
        assert TestClient(app).get("/auth/me", headers=_bearer(t)).status_code == 401
    assert session.get(User, uid) is not None


def test_a_disabled_account_stops_its_tokens(app, session):
    uid = _user(session, "a@x.local")
    token = _create(_sign_in(app, "a@x.local"))["token"]
    u = session.get(User, uid)
    u.is_active = False
    session.add(u)
    session.commit()
    assert TestClient(app).get("/auth/me", headers=_bearer(token)).status_code == 401


def test_travels_only_in_the_header(app, session):
    """The cookie is the browser session's: an API token does not hold there."""
    _user(session, "a@x.local")
    token = _create(_sign_in(app, "a@x.local"))["token"]
    c = TestClient(app)
    c.cookies.set("tab_token", token)
    assert c.get("/auth/me").status_code == 401


def test_a_token_creates_no_other_tokens(app, session):
    _user(session, "a@x.local")
    token = _create(_sign_in(app, "a@x.local"), scope="write")["token"]
    r = TestClient(app).post("/auth/tokens", json={"name": "figlio", "scope": "write", "expires_in_days": 5}, headers=_bearer(token))
    assert r.status_code == 403


def test_expiry_required_and_capped(app, session, monkeypatch):
    monkeypatch.setenv("AUTH__API_TOKEN_MAX_DAYS", "90")
    get_settings.cache_clear()
    _user(session, "a@x.local")
    browser = _sign_in(app, "a@x.local")
    for days in (0, 91):
        assert browser.post("/auth/tokens", json={"name": "x", "expires_in_days": days}, headers=CSRF).status_code == 422
    assert browser.post("/auth/tokens", json={"name": "x", "expires_in_days": 90}, headers=CSRF).status_code == 201


def test_personal_tokens_can_be_turned_off(app, session, monkeypatch):
    monkeypatch.setenv("AUTH__PERSONAL_TOKENS", "false")
    get_settings.cache_clear()
    _user(session, "a@x.local")
    r = _sign_in(app, "a@x.local").post("/auth/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF)
    assert r.status_code == 403


def test_the_observer_has_no_personal_tokens(app, session):
    """The observer is a demo's shared guest: a token would survive its password change, and
    all the visitors would see (and revoke) it."""
    _user(session, "occhio@x.local", is_observer=True)
    eye = _sign_in(app, "occhio@x.local")
    assert eye.post("/auth/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).status_code == 403
    assert eye.get("/auth/me").json()["personal_tokens"] is False


def test_the_token_authentication_is_one_read(db_engine):
    """As for the sessions: one SELECT, the token joined with the user."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from sqlalchemy import event
    from sqlmodel import Session

    from app.deps import auth

    with Session(db_engine) as s:
        u = make_user(s, email="c@x.local", last_seen_at=datetime.now(timezone.utc))
        _, token = api_tokens.create(s, u, "t", "read", 5, u)
        row = s.exec(select(ApiToken)).one()
        row.last_used_at = api_tokens.now()
        s.add(row)
        s.commit()
    commands = []
    event.listen(db_engine, "before_cursor_execute", lambda c, cur, st, *a: commands.append(st))
    request_ = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"), method="GET", state=SimpleNamespace())
    with Session(db_engine) as s:
        user_ = auth.get_current_user(request_, token=token, session=s)
    assert user_.email == "c@x.local"
    assert len(commands) == 1 and "api_tokens" in commands[0] and "users" in commands[0]


# ── the service accounts ────────────────────────────────────────────────────

def test_the_administrator_creates_a_service_account_and_its_tokens(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    boss = _sign_in(app, "capo@x.local")
    acc = boss.post("/service-accounts", json={"name": "Airflow produzione"}, headers=CSRF).json()
    assert acc["email"] == "airflow-produzione@service.invalid" and acc["tokens"] == 0
    t = boss.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "dag", "scope": "write", "expires_in_days": 30}, headers=CSRF)
    assert t.status_code == 201
    me = TestClient(app).get("/auth/me", headers=_bearer(t.json()["token"])).json()
    assert me["email"] == acc["email"]
    assert boss.get("/service-accounts").json()[0]["tokens"] == 1
    # signs in only with the token: password and SSO do not (no local password)
    assert TestClient(app).post("/auth/login", json={"email": acc["email"], "password": "x"}).status_code == 401
    assert session.get(User, acc["id"]).is_service and session.get(User, acc["id"]).hashed_password is None


def test_the_observer_sees_the_accounts_and_does_not_touch_them(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    _user(session, "occhio@x.local", is_observer=True)
    acc = _sign_in(app, "capo@x.local").post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    eye = _sign_in(app, "occhio@x.local")
    assert [a["name"] for a in eye.get("/service-accounts").json()] == ["CI"]
    assert eye.post("/service-accounts", json={"name": "altro"}, headers=CSRF).status_code == 403
    assert eye.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).status_code == 403


def test_the_administrators_tokens_only_for_service_accounts(app, session):
    """An administrator does not create credentials in a person's name."""
    _user(session, "capo@x.local", is_superuser=True)
    anna = _user(session, "anna@x.local")
    r = _sign_in(app, "capo@x.local").post(f"/service-accounts/{anna}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF)
    assert r.status_code == 404


def test_removing_the_account_removes_the_tokens(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    boss = _sign_in(app, "capo@x.local")
    acc = boss.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    token = boss.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).json()["token"]
    assert boss.delete(f"/service-accounts/{acc['id']}", headers=CSRF).status_code == 204
    assert TestClient(app).get("/auth/me", headers=_bearer(token)).status_code == 401
    assert session.exec(select(ApiToken)).all() == []


def test_the_list_of_every_token_is_masked_to_the_observer(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    _user(session, "occhio@x.local", is_observer=True)
    _user(session, "anna@x.local")
    _create(_sign_in(app, "anna@x.local"), name="di anna")
    full = _sign_in(app, "capo@x.local").get("/admin/api-tokens").json()
    assert full[0]["user_email"] == "anna@x.local" and full[0]["prefix"].startswith("tab_")
    eye = _sign_in(app, "occhio@x.local")
    seen = eye.get("/admin/api-tokens").json()
    assert seen[0]["user_email"] != "anna@x.local" and seen[0]["prefix"] == "tab_…" and seen[0]["last_used_ip"] is None
    assert eye.delete(f"/admin/api-tokens/{seen[0]['id']}", headers=CSRF).status_code == 403


# ── from the security review ─────────────────────────────────────────────────

def test_a_new_password_withdraws_the_tokens_too(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    anna = _user(session, "anna@x.local")
    token = _create(_sign_in(app, "anna@x.local"))["token"]
    boss = _sign_in(app, "capo@x.local")
    assert boss.patch(f"/users/{anna}", json={"password": "un-altra-password"}, headers=CSRF).status_code == 200
    assert TestClient(app).get("/auth/me", headers=_bearer(token)).status_code == 401


def test_turning_off_personal_tokens_turns_off_those_given_too(app, session, monkeypatch):
    _user(session, "capo@x.local", is_superuser=True)
    _user(session, "anna@x.local")
    personal = _create(_sign_in(app, "anna@x.local"))["token"]
    boss = _sign_in(app, "capo@x.local")
    acc = boss.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    service_ = boss.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).json()["token"]
    monkeypatch.setenv("AUTH__PERSONAL_TOKENS", "false")
    get_settings.cache_clear()
    assert TestClient(app).get("/auth/me", headers=_bearer(personal)).status_code == 401
    assert TestClient(app).get("/auth/me", headers=_bearer(service_)).status_code == 200


def test_a_service_account_has_no_password(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    boss = _sign_in(app, "capo@x.local")
    acc = boss.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    assert boss.patch(f"/users/{acc['id']}", json={"password": "una-password-lunga"}, headers=CSRF).status_code == 422
    # and even if it had one, it would not sign in with the form
    u = session.get(User, acc["id"])
    u.hashed_password = hash_password(PASSWORD)
    session.add(u)
    session.commit()
    assert TestClient(app).post("/auth/login", json={"email": acc["email"], "password": PASSWORD}).status_code == 401


def test_a_token_neither_creates_users_nor_sets_passwords(app, session):
    """A leaked administrator token must not be able to leave behind a credential lasting
    past its revocation."""
    boss_id = _user(session, "capo@x.local", is_superuser=True)
    token = _create(_sign_in(app, "capo@x.local"), scope="write")["token"]
    script = TestClient(app)
    assert script.post("/users", json={"email": "nuovo@x.local", "password": "una-password-lunga"}, headers=_bearer(token)).status_code == 403
    assert script.patch(f"/users/{boss_id}", json={"password": "una-password-lunga"}, headers=_bearer(token)).status_code == 403


def test_the_observer_sees_an_accounts_tokens_masked(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    _user(session, "occhio@x.local", is_observer=True)
    boss = _sign_in(app, "capo@x.local")
    acc = boss.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    boss.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF)
    seen = _sign_in(app, "occhio@x.local").get(f"/service-accounts/{acc['id']}/tokens").json()[0]
    assert seen["prefix"] == "tab_…" and seen["last_used_ip"] is None


def test_a_read_only_token_may_export_and_preview():
    """Export and preview are POSTs that only read."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    row = ApiToken(user_id=1, name="t", prefix="tab_x", token_hash="h", scope="read",
                    created_at=api_tokens.now(), expires_at=api_tokens.now())
    def request_(method, route):
        return SimpleNamespace(method=method, scope={"route": SimpleNamespace(path=route)}, headers={})
    for route in ("/tasks/export", "/tasks/preview"):
        api_tokens.check_level(row, request_("POST", route))
    for method, route in (("POST", "/tasks/transform-data"), ("POST", "/projects"), ("DELETE", "/tasks/export")):
        with pytest.raises(HTTPException):
            api_tokens.check_level(row, request_(method, route))


def test_the_page_knows_the_maximum_expiry(app, session, monkeypatch):
    monkeypatch.setenv("AUTH__API_TOKEN_MAX_DAYS", "30")
    get_settings.cache_clear()
    _user(session, "a@x.local")
    assert _sign_in(app, "a@x.local").get("/auth/me").json()["api_token_max_days"] == 30


# ── from the black-box trial (2026-10-12) ───────────────────────────────────

@pytest.fixture
def full_app(app):
    from app.routes import audit as audit_routes
    from app.routes import groups as groups_routes

    app.include_router(groups_routes.router)
    app.include_router(audit_routes.router)
    return app


def _boss_with_token(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    return _create(_sign_in(app, "capo@x.local"), scope="write", name="trapelato")["token"]


def test_a_leaked_token_does_not_leave_one_more_administrator(full_app, session):
    """With the token revoked, no other way in must stay: an account made administrator
    (flag, admin group, group membership) or observer."""
    from app.models import Group

    token = _boss_with_token(full_app, session)
    accomplice = _user(session, "complice@x.local")
    admin = Group(name="Admin", is_admin=True)
    eyes = Group(name="Occhi", is_observer=True)
    normal = Group(name="Vendite")
    for g in (admin, eyes, normal):
        session.add(g)
    session.commit()
    admin, eyes, normal = (type("G", (), {"id": g.id}) for g in (admin, eyes, normal))  # only the ids: the rows get detached
    script = TestClient(full_app)
    h = {**_bearer(token), "Accept-Language": "it"}
    for method, path, body in (
        ("PATCH", f"/users/{accomplice}", {"is_superuser": True}),
        ("PATCH", f"/users/{accomplice}", {"is_observer": True}),
        ("PATCH", f"/groups/{normal.id}", {"is_admin": True}),
        ("PATCH", f"/groups/{normal.id}", {"is_observer": True}),
        ("PUT", f"/users/{accomplice}/groups/{admin.id}", None),
        ("PUT", f"/users/{accomplice}/groups/{eyes.id}", None),
    ):
        r = script.request(method, path, json=body, headers=h)
        assert r.status_code == 403 and "amministratore" in r.json()["detail"], (path, body, r.text)
    session.expire_all()
    assert session.get(User, accomplice).is_superuser is False
    # what grants no roles stays possible: normal groups, removing a role
    assert script.put(f"/users/{accomplice}/groups/{normal.id}", headers=h).status_code == 204
    assert script.patch(f"/users/{accomplice}", json={"is_superuser": False, "full_name": "C"}, headers=h).status_code == 200


def test_disabling_withdraws_the_tokens_and_reactivating_does_not_bring_them_back(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    boss = _sign_in(app, "capo@x.local")
    acc = boss.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    token = boss.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).json()["token"]
    assert boss.patch(f"/users/{acc['id']}", json={"is_active": False}, headers=CSRF).status_code == 200
    assert boss.patch(f"/users/{acc['id']}", json={"is_active": True}, headers=CSRF).status_code == 200
    assert TestClient(app).get("/auth/me", headers=_bearer(token)).status_code == 401
    assert session.exec(select(ApiToken)).all() == []


def test_a_token_does_not_reactivate_an_account(app, session):
    token = _boss_with_token(app, session)
    ex = _user(session, "ex@x.local", is_active=False)
    r = TestClient(app).patch(f"/users/{ex}", json={"is_active": True}, headers=_bearer(token))
    assert r.status_code == 403
    session.expire_all()
    assert session.get(User, ex).is_active is False


def test_whoever_becomes_an_observer_loses_the_personal_tokens(full_app, session):
    from app.models import Group, UserGroupLink

    _user(session, "capo@x.local", is_superuser=True)
    boss = _sign_in(full_app, "capo@x.local")
    ids = {e: _user(session, e) for e in ("a@x.local", "b@x.local", "c@x.local")}
    token = {e: _create(_sign_in(full_app, e))["token"] for e in ids}
    acc = boss.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    t_service = boss.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).json()["token"]
    # three ways: the flag, joining an observers' group, the group's flag
    boss.patch(f"/users/{ids['a@x.local']}", json={"is_observer": True}, headers=CSRF)
    eyes = boss.post("/groups", json={"name": "Occhi"}, headers=CSRF).json()["id"]
    boss.patch(f"/groups/{eyes}", json={"is_observer": True}, headers=CSRF)
    boss.put(f"/users/{ids['b@x.local']}/groups/{eyes}", headers=CSRF)
    sales = boss.post("/groups", json={"name": "Vendite"}, headers=CSRF).json()["id"]
    boss.put(f"/users/{ids['c@x.local']}/groups/{sales}", headers=CSRF)
    boss.put(f"/users/{acc['id']}/groups/{sales}", headers=CSRF)
    assert TestClient(full_app).get("/auth/me", headers=_bearer(token["c@x.local"])).status_code == 200
    boss.patch(f"/groups/{sales}", json={"is_observer": True}, headers=CSRF)
    for e in ids:
        assert TestClient(full_app).get("/auth/me", headers=_bearer(token[e])).status_code == 401, e
    # a service account's stay: they belong to the integration, not to a person
    assert TestClient(full_app).get("/auth/me", headers=_bearer(t_service)).status_code == 200


def test_nan_and_infinity_are_a_422_not_a_500(app, session):
    from fastapi.exceptions import RequestValidationError

    from app.main import _invalid_request

    app.add_exception_handler(RequestValidationError, _invalid_request)  # the real app's
    _user(session, "a@x.local")
    browser = _sign_in(app, "a@x.local")
    for value in ("NaN", "Infinity", "-Infinity"):
        r = browser.post("/auth/tokens", content='{"name": "x", "expires_in_days": %s}' % value,
                         headers={**CSRF, "content-type": "application/json"})
        assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "expires_in_days"], r.text
    r = TestClient(app).post("/auth/login", content='{"email": NaN, "password": "x"}', headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_to_the_observer_the_audit_does_not_say_the_tokens_start_nor_searches_by_ip(full_app, session):
    from app.models import AuditLog as Item

    _user(session, "capo@x.local", is_superuser=True)
    _user(session, "occhio@x.local", is_observer=True)
    boss = _sign_in(full_app, "capo@x.local")
    _create(boss, name="report")
    session.add(Item(ts=api_tokens.now(), actor_label="anna@x.local", action="flow.create", ip="203.0.113.7",
                     target_type="flow", target_label="Margini", outcome="success"))
    session.commit()
    eye = _sign_in(full_app, "occhio@x.local")
    item = eye.get("/audit", params={"action": "token.create"}).json()["items"][0]
    assert item["detail"]["prefix"] == "tab_…"
    assert boss.get("/audit", params={"action": "token.create"}).json()["items"][0]["detail"]["prefix"].startswith("tab_")
    for q in ("203.0.113", "anna@"):
        assert eye.get("/audit", params={"q": q}).json()["total"] == 0, q
        assert boss.get("/audit", params={"q": q}).json()["total"] >= 1, q
    assert eye.get("/audit", params={"q": "Margini"}).json()["total"] == 1  # what it sees, it searches


def test_whoever_stays_only_observer_losing_admin_does_not_use_personal_tokens(full_app, session):
    """The ways to become one are many (flag removed, leaving the admin group, group
    downgraded or deleted, SSO synchronization): the point where the token is accepted
    enforces the rule, in the same SELECT."""
    from app.models import Group, UserGroupLink

    _user(session, "capo@x.local", is_superuser=True)
    boss = _sign_in(full_app, "capo@x.local")
    a = _user(session, "a@x.local", is_superuser=True, is_observer=True)
    b = _user(session, "b@x.local", is_observer=True)
    g = Group(name="Admin", is_admin=True)
    session.add(g)
    session.commit()
    gid = g.id
    session.add(UserGroupLink(user_id=b, group_id=gid))
    session.commit()
    token = {e: _create(_sign_in(full_app, e))["token"] for e in ("a@x.local", "b@x.local")}
    for t in token.values():
        assert TestClient(full_app).get("/auth/me", headers=_bearer(t)).status_code == 200
    boss.patch(f"/users/{a}", json={"is_superuser": False}, headers=CSRF)
    boss.delete(f"/users/{b}/groups/{gid}", headers=CSRF)
    for e, t in token.items():
        assert TestClient(full_app).get("/auth/me", headers=_bearer(t)).status_code == 401, e


@pytest.mark.anyio
async def test_nan_in_the_body_forwarded_to_the_engine_is_a_422():
    """The gateway forwards the body to the engine: NaN passed Python's parser and the
    engine answered 500. It is not JSON, it stops at the border."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.routes.proxy import _read_json

    for body in (b'{"limit": NaN}', b'{"x": [Infinity]}', b'{"x": -Infinity}'):
        async def receive(body=body):
            return {"type": "http.request", "body": body, "more_body": False}
        with pytest.raises(HTTPException) as e:
            await _read_json(Request({"type": "http", "method": "POST", "headers": [], "path": "/"}, receive))
        assert e.value.status_code == 422
