"""The external ClickHouse's connection from the panel, as the gateway handles it.

The rules these tests pin: a password typed in clear is encrypted at once with the key
gateway and engine share — the engine receives a token, the database keeps the token —
and it never comes back, neither in the panel nor in the audit (only «set» or not); a
password typed again is a change; going back to the environment removes it; the
connection's items roll out the engine's processes; «Test connection» is for the
administrator only, never with a token, sends the password encrypted, and passes the
engine's answer on.
"""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.crypto import decrypt_secret
from app.db.session import get_session
from app.models import AuditLog, EngineSetting, SettingsRollout
from app.routes import engine_settings as routes
from tests.conftest import make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"
CLICKHOUSE = ["engine", "worker", "preview-worker", "beat"]


class FakeEngine:
    """The engine as far as the connection goes: three items (a text, a secret, a
    choice), shown as the real catalog shows them, and the test route."""

    def __init__(self):
        self.values: dict = {}
        self.received: list[dict] = []
        self.checks: list[dict] = []

    def _item(self, key, kind, **kw):
        v = self.values.get(key)
        return {"key": key, "category": "clickhouse_connection", "unit": "", "min": 0, "max": 4096, "kind": kind,
                "rollout": CLICKHOUSE, "env_var": key.upper().replace(".", "__"), "env_value": kw.pop("env", ""),
                "value": (v is not None) if kind == "secret" and v is not None else v, **kw}

    def _catalog(self):
        return {"items": [self._item("clickhouse_external.host", "str"),
                          self._item("clickhouse_external.password", "secret", env=False),
                          self._item("clickhouse_external.transport", "choice", env="s3", choices=["s3", "push"])],
                "unavailable": ["clickhouse"], "refresh_seconds": 10.0, "discarded": []}

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/settings/clickhouse-check":
            body = json.loads(req.content)
            self.checks.append(body)
            return httpx.Response(200, json={"ok": True, "steps": [{"step": "server", "ok": True, "detail": "24.8.4"}]})
        if req.method == "GET":
            return httpx.Response(200, json=self._catalog())
        body = json.loads(req.content)
        self.received.append(body)
        for k, v in body["values"].items():
            if k == "clickhouse_external.password":
                decrypt_secret(v)  # the engine opens it with the shared key: a clear one would fail
        self.values = dict(body["values"])
        return httpx.Response(200, json=self._catalog())


@pytest.fixture
def engine_(monkeypatch):
    m = FakeEngine()
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(m.handle), base_url="http://engine"))
    return m


@pytest.fixture
def app(session, engine_):
    from app.routes import api_tokens as tokens_routes
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, tokens_routes.router, routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _sign_in(app, session, email, **kw) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw)
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def _item(state, key):
    return next(v for v in state["items"] if v["key"] == key)


def test_the_password_is_encrypted_at_once_and_never_comes_back(app, session, engine_):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.put("/admin/engine-settings", json={"values": {
        "clickhouse_external.host": "ch.acme.local", "clickhouse_external.password": "Pa55-segreta",
        "clickhouse_external.transport": "push"}}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert "Pa55-segreta" not in r.text
    sent = engine_.received[-1]["values"]
    assert sent["clickhouse_external.host"] == "ch.acme.local" and sent["clickhouse_external.transport"] == "push"
    token = sent["clickhouse_external.password"]
    assert token != "Pa55-segreta" and decrypt_secret(token) == "Pa55-segreta"
    row = session.get(EngineSetting, "clickhouse_external.password")
    assert json.loads(row.value_json) == token  # the database keeps the token, never the clear
    state = boss.get("/admin/engine-settings").json()
    assert _item(state, "clickhouse_external.password")["value"] is True
    assert _item(state, "clickhouse_external.host")["value"] == "ch.acme.local"
    assert _item(state, "clickhouse_external.transport")["choices"] == ["s3", "push"]
    assert token not in json.dumps(state)
    # the server changes when the engine's processes start again
    session.expire_all()
    pending = {r.component for r in session.exec(select(SettingsRollout)).all() if r.changed_at}
    assert pending == set(CLICKHOUSE)


def test_the_audit_says_only_that_the_password_was_set(app, session, engine_):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    boss.put("/admin/engine-settings", json={"values": {"clickhouse_external.password": "prima-123"}}, headers=CSRF)
    boss.put("/admin/engine-settings", json={"values": {"clickhouse_external.password": "prima-123"}}, headers=CSRF)
    boss.put("/admin/engine-settings", json={"values": {"clickhouse_external.password": None}}, headers=CSRF)
    rows = session.exec(select(AuditLog).where(AuditLog.action == "engine_settings.update").order_by(AuditLog.id)).all()
    # typed again is a change (who typed it may have meant another one): three entries
    assert [json.loads(v.detail)["changes"]["clickhouse_external.password"] for v in rows] == [
        {"from": None, "to": True}, {"from": True, "to": True}, {"from": True, "to": None}]
    assert all("prima-123" not in (v.detail or "") for v in rows)
    session.expire_all()
    assert session.get(EngineSetting, "clickhouse_external.password") is None
    assert "clickhouse_external.password" not in engine_.values


def test_a_password_must_be_a_text_of_reasonable_length(app, session, engine_):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.put("/admin/engine-settings", json={"values": {"clickhouse_external.password": 12345}}, headers=CSRF)
    assert r.status_code == 422 and r.json()["detail"]["errors"] == [{"key": "clickhouse_external.password", "code": "text"}]
    r = boss.put("/admin/engine-settings", json={"values": {"clickhouse_external.password": "x" * 1025}}, headers=CSRF)
    assert r.status_code == 422 and r.json()["detail"]["errors"] == [{"key": "clickhouse_external.password", "code": "too_long"}]
    assert engine_.received == []


def test_the_check_sends_the_password_encrypted_and_passes_the_answer_on(app, session, engine_):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.post("/admin/engine-settings/clickhouse-check", json={"values": {
        "clickhouse_external.host": "ch.acme.local", "clickhouse_external.port": 8443,
        "clickhouse_external.secure": True, "clickhouse_external.password": "da-provare"}}, headers=CSRF)
    assert r.status_code == 200 and r.json()["steps"][0] == {"step": "server", "ok": True, "detail": "24.8.4"}
    asked = engine_.checks[-1]["values"]
    assert asked["clickhouse_external.port"] == 8443 and asked["clickhouse_external.secure"] is True
    assert decrypt_secret(asked["clickhouse_external.password"]) == "da-provare"
    assert engine_.received == [] and session.exec(select(EngineSetting)).all() == []  # nothing saved


def test_only_the_administrator_tests_and_never_with_a_token(app, session, engine_):
    reader = _sign_in(app, session, "osserva@x.local", is_observer=True)
    assert reader.post("/admin/engine-settings/clickhouse-check", json={"values": {}}, headers=CSRF).status_code == 403
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    token = boss.post("/auth/tokens", json={"name": "x", "scope": "write", "expires_in_days": 5}, headers=CSRF).json()["token"]
    r = TestClient(app).post("/admin/engine-settings/clickhouse-check", json={"values": {}},
                             headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and engine_.checks == []


def test_engine_down_the_check_says_so(app, session, monkeypatch):
    def down(req):
        raise httpx.ConnectError("il motore non risponde")
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://engine"))
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.post("/admin/engine-settings/clickhouse-check", json={"values": {}}, headers=CSRF)
    assert r.status_code == 503 and "not tested" in r.json()["detail"]
