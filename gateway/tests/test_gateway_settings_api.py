"""Administration → Settings, the gateway's part (routes/gateway_settings.py).

The rules these tests hold: an administrator changes and goes back to the environment's
value, and this process applies it at once; all or none, with the reason of each refusal;
a feature this installation does not have shows no settings and takes none; the run's
lost-after threshold stays above the engine's hard limit; observers read with the e-mails
masked, an API token never writes; with the panel off they are shown and not applied.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core import overrides as ov
from app.core.config import get_settings
from app.db.session import get_session
from app.models import AuditLog, GatewaySetting
from app.routes import gateway_settings as routes
from tests.test_engine_settings import CSRF, _entra, _voce, motore  # noqa: F401 — the fake engine

URL = "/admin/gateway-settings"


@pytest.fixture(autouse=True)
def _clean():
    ov.forget()
    yield
    ov.forget()


@pytest.fixture
def app(session, motore):  # noqa: F811
    from app.routes import api_tokens as tokens_routes
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, tokens_routes.router, routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _errors(r):
    assert r.status_code == 422, r.text
    return {e["key"]: (e["code"], e.get("limit")) for e in r.json()["detail"]["errors"]}


def test_the_administrator_changes_and_goes_back_to_the_environment(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    state = capo.get(URL).json()
    assert state["editable"] and state["panel_enabled"] and state["refresh_seconds"] == ov.REREAD_EVERY
    v = _voce(state, "orchestrator.max_concurrent")
    assert (v["value"], v["applied"], v["env_value"], v["env_var"], v["rollout"]) == (None, 20, 20, "ORCHESTRATOR__MAX_CONCURRENT", [])

    state = capo.put(URL, json={"values": {"orchestrator.max_concurrent": 8, "app.timezone": "Europe/Rome"}}, headers=CSRF).json()
    v = _voce(state, "orchestrator.max_concurrent")
    assert (v["value"], v["applied"], v["updated_by"]) == (8, 8, "capo@x.local")
    assert get_settings().orchestrator.max_concurrent == 8 and get_settings().app.timezone == "Europe/Rome"
    assert json.loads(session.get(GatewaySetting, "orchestrator.max_concurrent").value) == 8
    entry = session.exec(select(AuditLog).where(AuditLog.action == "settings.update")).one()
    assert json.loads(entry.detail)["changes"]["orchestrator.max_concurrent"] == {"from": None, "to": 8}

    state = capo.put(URL, json={"values": {"orchestrator.max_concurrent": None}}, headers=CSRF).json()
    assert _voce(state, "orchestrator.max_concurrent")["value"] is None
    assert get_settings().orchestrator.max_concurrent == 20 and get_settings().app.timezone == "Europe/Rome"
    session.expire_all()
    assert [r.key for r in session.exec(select(GatewaySetting)).all()] == ["app.timezone"]


def test_what_another_replica_saved_is_shown_applied_at_once(app, session):
    # two gateway replicas: one saves, the next page comes from the other, which reads the
    # table only every REREAD_EVERY seconds and showed the value «not applied» meanwhile
    from datetime import datetime

    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    assert _voce(capo.get(URL).json(), "ai.max_requests")["applied"] == 12
    session.add(GatewaySetting(key="ai.max_requests", value="15", updated_at=datetime(2026, 10, 24)))
    session.commit()  # written by the other replica: this process has not reloaded
    v = _voce(capo.get(URL).json(), "ai.max_requests")
    assert (v["value"], v["applied"]) == (15, 15) and get_settings().ai.max_requests == 15


def test_the_same_value_again_changes_nothing(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put(URL, json={"values": {"ai.max_requests": 10}}, headers=CSRF)
    capo.put(URL, json={"values": {"ai.max_requests": 10, "ai.max_result_rows": None}}, headers=CSRF)
    assert len(session.exec(select(AuditLog).where(AuditLog.action == "settings.update")).all()) == 1


def test_all_or_none_with_the_reason_of_each(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    r = capo.put(URL, json={"values": {"ai.max_requests": 10, "orchestrator.max_concurrent": 51,
                                       "auth.personal_tokens": "yes", "app.timezone": "Nowhere/Land"}}, headers=CSRF)
    assert _errors(r) == {"orchestrator.max_concurrent": ("concurrent_pool", 50),
                          "auth.personal_tokens": ("boolean", None), "app.timezone": ("invalid", None)}
    assert session.exec(select(GatewaySetting)).all() == [] and ov.current() == {}


def test_what_the_installation_does_not_have_is_neither_shown_nor_taken(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    groups = {v["group"] for v in capo.get(URL).json()["items"]}
    assert "orchestration" in groups and not groups & {"sso", "kubernetes", "scripts", "builds", "containers"}
    r = capo.put(URL, json={"values": {"oidc.button_label": "Company", "db.url": "x", "audit.retention_minutes": 1}}, headers=CSRF)
    assert _errors(r) == {"oidc.button_label": ("unavailable", None), "db.url": ("unknown", None),
                          "audit.retention_minutes": ("unknown", None)}


def test_sso_settings_appear_when_sso_is_on(app, session, monkeypatch):
    monkeypatch.setenv("OIDC__ISSUER", "https://idp.example/realms/x")
    monkeypatch.setenv("OIDC__CLIENT_ID", "tabularia")
    get_settings.cache_clear()
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    state = capo.put(URL, json={"values": {"oidc.button_label": "Company account"}}, headers=CSRF).json()
    assert _voce(state, "oidc.button_label")["applied"] == "Company account"


def test_the_lost_run_threshold_stays_above_the_engine_hard_limit(app, session, motore):  # noqa: F811
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    # the fake engine's hard limit: 3600 from the environment → at least 3900
    r = capo.put(URL, json={"values": {"engine.run_stale_timeout_seconds": 3800}}, headers=CSRF)
    assert _errors(r) == {"engine.run_stale_timeout_seconds": ("stale_below_hard_limit", 3900)}
    assert capo.put(URL, json={"values": {"engine.run_stale_timeout_seconds": 3900}}, headers=CSRF).status_code == 200
    motore.giu = True
    r = capo.put(URL, json={"values": {"engine.run_stale_timeout_seconds": 5000}}, headers=CSRF)
    assert _errors(r) == {"engine.run_stale_timeout_seconds": ("engine_unreachable", None)}
    # the others do not need the engine
    assert capo.put(URL, json={"values": {"ai.max_requests": 12}}, headers=CSRF).status_code == 200


def test_a_saved_value_that_no_longer_holds_does_not_block_the_others(app, session, monkeypatch):
    from datetime import datetime

    session.add(GatewaySetting(key="orchestrator.heartbeat_seconds", value="30", updated_at=datetime(2026, 10, 1)))
    session.commit()
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    # heartbeat 30 × 3 > dead after 60: dropped at every reading, the change goes through
    assert capo.put(URL, json={"values": {"ai.max_requests": 12}}, headers=CSRF).status_code == 200
    assert get_settings().orchestrator.heartbeat_seconds == 10 and get_settings().ai.max_requests == 12
    # and raising the dead-after makes it hold again
    capo.put(URL, json={"values": {"orchestrator.dead_after_seconds": 120}}, headers=CSRF)
    assert get_settings().orchestrator.heartbeat_seconds == 30


def test_a_rollout_setting_says_what_the_process_started_with(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    ov.remember_start()
    state = capo.put(URL, json={"values": {"db.max_overflow": 30}}, headers=CSRF).json()
    v = _voce(state, "db.max_overflow")
    assert v["rollout"] == ["gateway", "orchestrator"] and v["value"] == 30 and v["started_with"] == 10


def test_the_observer_reads_masked_and_does_not_write(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put(URL, json={"values": {"ai.max_requests": 10}}, headers=CSRF)
    occhio = _entra(app, session, "occhio@x.local", is_observer=True)
    state = occhio.get(URL).json()
    v = _voce(state, "ai.max_requests")
    assert not state["editable"] and v["value"] == 10 and v["updated_by"] != "capo@x.local"
    assert occhio.put(URL, json={"values": {"ai.max_requests": 1}}, headers=CSRF).status_code == 403
    assert _entra(app, session, "anna@x.local").get(URL).status_code == 403


def test_an_api_token_never_writes(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    token = capo.post("/auth/tokens", json={"name": "x", "scope": "write", "expires_in_days": 5}, headers=CSRF).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    script = TestClient(app)
    assert script.get(URL, headers=h).json()["editable"] is False
    r = script.put(URL, json={"values": "not even a dict"}, headers={**h, "Accept-Language": "it"})
    assert r.status_code == 403 and "non cambia le impostazioni" in r.json()["detail"]


def test_with_the_panel_off_they_are_shown_and_not_applied(app, session, db_engine, monkeypatch):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put(URL, json={"values": {"ai.max_requests": 10}}, headers=CSRF)
    monkeypatch.setenv("ENGINE__SETTINGS_PANEL", "false")
    get_settings.cache_clear()
    ov.reload(db_engine)
    state = capo.get(URL).json()
    assert not state["editable"] and not state["panel_enabled"]
    assert _voce(state, "ai.max_requests")["value"] is None and get_settings().ai.max_requests != 10
    assert capo.put(URL, json={"values": {"ai.max_requests": 11}}, headers=CSRF).status_code == 403


def test_the_upload_limit_reaches_the_engine_too(app, session, motore):  # noqa: F811
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    # the engine's panel does not show it: it is in the gateway's box
    capo.put(URL, json={"values": {"upload.max_bytes": 5 * 1024 ** 3}}, headers=CSRF)
    assert motore.ricevute[-1]["values"]["upload.max_bytes"] == 5 * 1024 ** 3
    capo.put(URL, json={"values": {"upload.max_bytes": None}}, headers=CSRF)
    assert "upload.max_bytes" not in motore.ricevute[-1]["values"]


# ── from the bug hunt of 2026-10-23 ───────────────────────────────────────────
def test_the_lost_run_threshold_back_to_the_environment_is_checked_too(app, session, motore):  # noqa: F811
    """Back to the installation's value, it must still stay above the engine's limit."""
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    assert capo.put(URL, json={"values": {"engine.run_stale_timeout_seconds": 6000}}, headers=CSRF).status_code == 200
    motore.values["celery.task_time_limit"] = 5000  # the engine's limit raised meanwhile
    r = capo.put(URL, json={"values": {"engine.run_stale_timeout_seconds": None}}, headers=CSRF)
    assert _errors(r) == {"engine.run_stale_timeout_seconds": ("stale_below_hard_limit", 5300)}


def test_going_back_that_drops_another_saved_value_is_refused(app, session):
    """The pool back to the environment's left 90 executions saved and silently not applied."""
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    assert capo.put(URL, json={"values": {"db.pool_size": 100, "orchestrator.max_concurrent": 90}}, headers=CSRF).status_code == 200
    r = capo.put(URL, json={"values": {"db.pool_size": None}}, headers=CSRF)
    assert _errors(r) == {"orchestrator.max_concurrent": ("concurrent_pool", 50)}


def test_a_new_time_zone_computes_the_next_slots_again(app, session):
    from datetime import datetime

    from tests.conftest import make_flow, make_project

    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, project_id=p.id)
    flow.run_schedule, flow.next_run_at = "0 3 * * *", datetime(2030, 1, 1, 3, 0)
    session.add(flow)
    session.commit()
    capo.put(URL, json={"values": {"app.timezone": "Asia/Tokyo"}}, headers=CSRF)
    session.expire_all()
    nxt = session.get(type(flow), flow.id).next_run_at
    assert nxt.hour == 18  # 03:00 in Tokyo is 18:00 UTC the day before
