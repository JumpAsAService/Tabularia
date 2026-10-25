"""Engine settings from the panel: the gateway keeps them, has the engine check and apply
them, and sends them again at intervals.

The rules these tests pin: only what the engine accepted is saved (all or nothing;
engine down = nothing changed); the tasks' hard limit does not exceed the threshold
past which the gateway declares a run lost; only the administrator writes, never with
an API token, and the audit says from what to what; with ENGINE__SETTINGS_PANEL=false
they are seen and do not hold; the periodic round sends them again, and a saved one
that no longer holds is discarded instead of leaving the old one.
"""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.db.session import get_session
from app.models import AuditLog, EngineSetting
from app.routes import engine_settings as routes
from app.services import engine_panel_settings as rm
from tests.conftest import make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"


class FakeEngine:
    """The engine as far as needed here: two items, their checks, and what it applies."""

    def __init__(self):
        self.values: dict = {}
        self.received: list[dict] = []
        self.giu = False

    def _catalog(self, maximum):
        return {
            "items": [
                {"key": "export.max_rows", "category": "export", "unit": "rows", "min": 0, "max": 100_000_000,
                 "env_var": "EXPORT__MAX_ROWS", "env_value": 5_000_000, "value": self.values.get("export.max_rows")},
                {"key": "celery.task_time_limit", "category": "execution", "unit": "seconds", "min": 60,
                 "max": maximum, "env_var": "CELERY__TASK_TIME_LIMIT", "env_value": 3600,
                 "value": self.values.get("celery.task_time_limit")},
            ],
            "unavailable": ["bigquery"], "refresh_seconds": 10.0, "discarded": [],
        }

    def handle(self, req: httpx.Request) -> httpx.Response:
        if self.giu:
            raise httpx.ConnectError("il motore non risponde")
        assert req.url.path == "/settings"
        if req.method == "GET":
            return httpx.Response(200, json=self._catalog(int(req.url.params["max_task_time_limit"])))
        body = json.loads(req.content)
        self.received.append(body)
        errors = []
        for k, v in body["values"].items():
            if k in ("export.max_rows", "celery.task_time_limit") and (isinstance(v, bool) or not isinstance(v, int)):
                errors.append({"key": k, "code": "integer"})  # the engine checks the type (core/panel_settings.py)
                continue
            if k == "export.max_rows" and v < 0:
                errors.append({"key": k, "code": "min", "limit": 0})
            if k == "celery.task_time_limit" and v > body["max_task_time_limit"]:
                errors.append({"key": k, "code": "max", "limit": body["max_task_time_limit"]})
        if errors:
            return httpx.Response(422, json={"detail": {"errors": errors}})
        self.values = dict(body["values"])
        return httpx.Response(200, json=self._catalog(body["max_task_time_limit"]))


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


def test_the_administrator_changes_and_goes_back_to_the_environments_value(app, session, engine_):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    state = boss.get("/admin/engine-settings").json()
    assert state["editable"] and state["panel_enabled"] and state["unavailable"] == ["bigquery"]
    assert _item(state, "export.max_rows") | {"value": None, "applied": None, "env_value": 5_000_000} == _item(state, "export.max_rows")

    state = boss.put("/admin/engine-settings", json={"values": {"export.max_rows": 1_000_000}}, headers=CSRF).json()
    v = _item(state, "export.max_rows")
    assert (v["value"], v["applied"], v["updated_by"]) == (1_000_000, 1_000_000, "capo@x.local")
    assert session.get(EngineSetting, "export.max_rows").value == 1_000_000
    assert engine_.received[-1]["values"] == {"export.max_rows": 1_000_000}

    state = boss.put("/admin/engine-settings", json={"values": {"export.max_rows": None}}, headers=CSRF).json()
    assert _item(state, "export.max_rows")["value"] is None and engine_.values == {}
    session.expire_all()
    assert session.exec(select(EngineSetting)).all() == []


def test_the_audit_says_from_what_to_what(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    boss.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    boss.put("/admin/engine-settings", json={"values": {"export.max_rows": 20}}, headers=CSRF)
    items = session.exec(select(AuditLog).where(AuditLog.action == "engine_settings.update").order_by(AuditLog.id)).all()
    assert [json.loads(v.detail)["changes"]["export.max_rows"] for v in items] == [
        {"from": None, "to": 10}, {"from": 10, "to": 20}]
    # nothing changed, no audit
    boss.put("/admin/engine-settings", json={"values": {"export.max_rows": 20}}, headers=CSRF)
    assert len(session.exec(select(AuditLog).where(AuditLog.action == "engine_settings.update")).all()) == 2


def test_only_what_the_engine_accepts_is_saved(app, session, engine_):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    boss.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    r = boss.put("/admin/engine-settings", json={"values": {"export.max_rows": -1}}, headers=CSRF)
    assert r.status_code == 422 and r.json()["detail"]["errors"][0] == {"key": "export.max_rows", "code": "min", "limit": 0}
    session.expire_all()
    assert session.get(EngineSetting, "export.max_rows").value == 10 and engine_.values == {"export.max_rows": 10}


def test_engine_down_nothing_changed(app, session, engine_):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    engine_.giu = True
    assert boss.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF).status_code == 503
    assert boss.get("/admin/engine-settings").status_code == 503
    assert session.exec(select(EngineSetting)).all() == []


def test_the_hard_limit_stays_under_the_gateways_threshold(app, session, engine_, monkeypatch):
    """The gateway declares a run lost after ENGINE__RUN_STALE_TIMEOUT_SECONDS: with the hard
    limit above it, it would close as failed a run still alive."""
    monkeypatch.setenv("ENGINE__RUN_STALE_TIMEOUT_SECONDS", "3900")
    get_settings.cache_clear()
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    assert _item(boss.get("/admin/engine-settings").json(), "celery.task_time_limit")["max"] == 3600
    r = boss.put("/admin/engine-settings", json={"values": {"celery.task_time_limit": 3700}}, headers=CSRF)
    assert r.status_code == 422 and engine_.received[-1]["max_task_time_limit"] == 3600
    assert boss.put("/admin/engine-settings", json={"values": {"celery.task_time_limit": 1800}}, headers=CSRF).status_code == 200


def test_the_observer_sees_and_does_not_change(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    boss.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    eye = _sign_in(app, session, "occhio@x.local", is_observer=True)
    state = eye.get("/admin/engine-settings").json()
    v = _item(state, "export.max_rows")
    assert not state["editable"] and v["value"] == 10 and v["updated_by"] != "capo@x.local"
    assert eye.put("/admin/engine-settings", json={"values": {"export.max_rows": 1}}, headers=CSRF).status_code == 403
    anyone = _sign_in(app, session, "anna@x.local")
    assert anyone.get("/admin/engine-settings").status_code == 403


def test_a_token_does_not_change_the_settings(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    token = boss.post("/auth/tokens", json={"name": "x", "scope": "write", "expires_in_days": 5}, headers=CSRF).json()["token"]
    script = TestClient(app)
    h = {"Authorization": f"Bearer {token}"}
    assert script.get("/admin/engine-settings", headers=h).json()["editable"] is False
    r = script.put("/admin/engine-settings", json={"values": {"export.max_rows": 1}}, headers={**h, "Accept-Language": "it"})
    assert r.status_code == 403 and "non cambia le impostazioni" in r.json()["detail"]


def test_with_the_panel_off_from_the_environment_they_are_seen_and_do_not_hold(app, session, engine_, monkeypatch):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    boss.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    monkeypatch.setenv("ENGINE__SETTINGS_PANEL", "false")
    get_settings.cache_clear()
    state = boss.get("/admin/engine-settings").json()
    assert not state["editable"] and not state["panel_enabled"] and _item(state, "export.max_rows")["value"] is None
    assert boss.put("/admin/engine-settings", json={"values": {"export.max_rows": 20}}, headers=CSRF).status_code == 403
    assert rm.in_force(session) == {}  # the periodic round sends «none»


@pytest.mark.anyio
async def test_the_round_sends_again_and_discards_the_one_no_longer_valid(session, engine_, monkeypatch):
    """Saved when it held (hard limit 3700 with a higher threshold), then the threshold went
    down: the others are sent, the old one is not left in force."""
    from datetime import datetime

    for k, v in (("export.max_rows", 10), ("celery.task_time_limit", 3700)):
        session.add(EngineSetting(key=k, value=v, updated_at=datetime(2026, 10, 12)))
    session.commit()
    monkeypatch.setenv("ENGINE__RUN_STALE_TIMEOUT_SECONDS", "3900")
    get_settings.cache_clear()
    await rm.push_discarding(rm.in_force(session))
    assert engine_.values == {"export.max_rows": 10}


@pytest.mark.anyio
async def test_the_periodic_round_sends_the_saved_ones_again(db_engine, session, engine_):
    """Valkey emptied or engine restarted: the round puts back the database's."""
    from datetime import datetime

    session.add(EngineSetting(key="export.max_rows", value=77, updated_at=datetime(2026, 10, 12)))
    session.commit()
    engine_.values = {}
    assert await rm.one_round(db_engine) is True
    assert engine_.values == {"export.max_rows": 77}


def test_an_engine_of_before_says_so_instead_of_not_answering(app, session, engine_, monkeypatch):
    """A half-done update: the engine does not know /settings (404)."""
    monkeypatch.setattr(engine_, "handle", lambda req: httpx.Response(404, json={"detail": "Not Found"}))
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(lambda req: engine_.handle(req)), base_url="http://engine"))
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.get("/admin/engine-settings")
    assert r.status_code == 503 and "older version" in r.json()["detail"]
    r = boss.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    assert r.status_code == 503 and "nothing was changed" in r.json()["detail"]
    assert session.exec(select(EngineSetting)).all() == []


# ── from the black-box trial (2026-10-12) ───────────────────────────────────

@pytest.fixture
def refusing_engine(engine_, monkeypatch):
    """The engine refuses the items in `refuse` (saved when they held: the environment
    changed)."""
    engine_.refuse = set()
    real = engine_.handle

    def handle(req):
        if req.method == "PUT":
            body = json.loads(req.content)
            bad_ones = [k for k in body["values"] if k in engine_.refuse]
            if bad_ones:
                engine_.received.append(body)
                return httpx.Response(422, json={"detail": {"errors": [{"key": k, "code": "below"} for k in bad_ones]}})
        return real(req)
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://engine"))
    return engine_


@pytest.mark.parametrize("value", [True, False, "100000", 1.5, 100000.0, [1], {"a": 1}])
def test_whole_numbers_only(app, session, engine_, value):
    """`true` became 1 (and `false` 0, «no cap» for BigQuery) before the engine saw it."""
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.put("/admin/engine-settings", json={"values": {"export.max_rows": value}}, headers=CSRF)
    assert r.status_code == 422 and engine_.values == {}


def test_no_unknown_keys_nor_huge_bodies(app, session, engine_):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.put("/admin/engine-settings", json={"values": {"zz.sconosciuta": None}}, headers=CSRF)
    assert r.status_code == 422 and r.json()["detail"]["errors"] == [{"key": "zz.sconosciuta", "code": "unknown"}]
    r = boss.put("/admin/engine-settings", json={"values": {f"k{i}": None for i in range(200)}}, headers=CSRF)
    assert r.status_code == 422


def test_a_token_gets_403_even_with_the_wrong_body(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    token = boss.post("/auth/tokens", json={"name": "x", "scope": "write", "expires_in_days": 5}, headers=CSRF).json()["token"]
    r = TestClient(app).put("/admin/engine-settings", json={"values": {"export.max_rows": "molti"}},
                            headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.anyio
async def test_the_round_discards_until_the_engine_accepts(db_engine, session, refusing_engine):
    """Before, it discarded once only and, at the second refusal, sent nothing any more."""
    from datetime import datetime

    for k, v in (("export.max_rows", 4777), ("celery.task_time_limit", 1800)):
        session.add(EngineSetting(key=k, value=v, updated_at=datetime(2026, 10, 12)))
    session.commit()
    refusing_engine.refuse = {"celery.task_time_limit"}
    assert await rm.one_round(db_engine)
    assert refusing_engine.values == {"export.max_rows": 4777}


def test_a_saved_one_no_longer_valid_does_not_block_the_others(app, session, refusing_engine):
    """The environment changed under a saved value: the others are changed, and that one
    stays saved (not applied) until it is changed or reset."""
    from datetime import datetime

    session.add(EngineSetting(key="celery.task_time_limit", value=3250, updated_at=datetime(2026, 10, 12)))
    session.commit()
    refusing_engine.refuse = {"celery.task_time_limit"}
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert refusing_engine.values == {"export.max_rows": 10}
    assert refusing_engine.received[-1]["changed"] == ["export.max_rows"]
    v = _item(r.json(), "celery.task_time_limit")
    assert v["value"] == 3250 and v["applied"] is None  # saved, not applied: the panel says so
    r = boss.put("/admin/engine-settings", json={"values": {"celery.task_time_limit": None}}, headers=CSRF)
    assert r.status_code == 200 and session.get(EngineSetting, "celery.task_time_limit") is None
