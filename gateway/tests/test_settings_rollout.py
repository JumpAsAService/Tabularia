"""The rollout of the components that read a changed setting only at start
(services/settings_rollout.py, the routes in routes/gateway_settings.py).

The rules these tests hold: saving such a setting marks its components; their pods
started before are «old» and the panel says so; «Roll out» restarts them one at a time,
by eviction, the oldest first, the orchestrator itself last; it waits while a pod is on
its way out or a new one is not ready (a value that stops a pod from starting stops the
rollout, with the old pods still serving); a PodDisruptionBudget that says «not now» is
waited for; a request with no old pod left is closed. On Docker Compose the panel names
the services and the administrator says when it is done. Only an administrator, never a
token, every request in the audit log.
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import select

from app.core import overrides as ov
from app.core.config import get_settings
from app.db.session import get_session
from app.models import AuditLog, SettingsRollout
from app.routes import engine_settings as engine_routes
from app.routes import gateway_settings as routes
from app.services import kube_jobs
from app.services import settings_rollout as sr
from tests.test_engine_settings import CSRF, MotoreFinto, _entra

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 10, 23, 12, 0, 0)


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


class Kube:
    """The pods of the release, and what an eviction does to them."""

    def __init__(self):
        self.pods: dict[str, dict] = {}
        self.evicted: list[str] = []
        self.pdb_says_no = False
        self.selectors: list[str] = []

    def add(self, name, component, created, ready=True, terminating=False):
        meta = {"name": name, "creationTimestamp": _iso(created),
                "labels": {"app.kubernetes.io/instance": "tab", "app.kubernetes.io/component": component}}
        if terminating:
            meta["deletionTimestamp"] = _iso(created + timedelta(hours=1))
        self.pods[name] = {"metadata": meta, "status": {"phase": "Running", "conditions": [
            {"type": "Ready", "status": "True" if ready else "False"}]}}

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/api/v1/namespaces/tab/pods":
            sel = req.url.params["labelSelector"]
            self.selectors.append(sel)
            want = dict(kv.split("=", 1) for kv in sel.split(","))
            items = [p for p in self.pods.values()
                     if all(p["metadata"]["labels"].get(k) == v for k, v in want.items())]
            return httpx.Response(200, json={"items": items})
        if req.method == "POST" and path.endswith("/eviction"):
            name = path.split("/")[-2]
            body = json.loads(req.content)
            assert body["kind"] == "Eviction" and body["metadata"] == {"name": name, "namespace": "tab"}
            if self.pdb_says_no:
                return httpx.Response(429, json={"message": "Cannot evict pod as it would violate the pod's disruption budget."})
            if name not in self.pods:
                return httpx.Response(404, json={"message": "not found"})
            self.evicted.append(name)
            # on its way out from now (one gone for over 15 minutes no longer holds the rollout)
            self.pods[name]["metadata"]["deletionTimestamp"] = _iso(datetime.now(timezone.utc).replace(tzinfo=None))
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": f"not here: {req.method} {path}"})

    def replace(self, name, new_name, created):
        """The ReplicaSet's work: the evicted pod goes, a new one comes, ready."""
        component = self.pods.pop(name)["metadata"]["labels"]["app.kubernetes.io/component"]
        self.add(new_name, component, created)


@pytest.fixture
def kube(monkeypatch, tmp_path):
    (tmp_path / "token").write_text("t")
    for k, v in {"KUBERNETES__TOKEN_FILE": str(tmp_path / "token"), "KUBERNETES__NAMESPACE": "tab",
                 "KUBERNETES__CA_FILE": "", "KUBERNETES__ROLLOUT": "true", "KUBERNETES__RELEASE": "tab"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    k = Kube()
    monkeypatch.setattr(kube_jobs, "_transport", httpx.MockTransport(k.handle))
    monkeypatch.setenv("HOSTNAME", "tab-orchestrator-a")
    yield k
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    ov.forget()
    yield
    ov.forget()


class MotoreConRiavvio(MotoreFinto):
    def _catalogo(self, maximum):
        c = super()._catalogo(maximum)
        c["items"].append({"key": "celery.max_tasks_per_child", "category": "execution", "unit": "tasks", "min": 1,
                           "max": 100_000, "kind": "int", "rollout": ["worker", "preview-worker"],
                           "env_var": "CELERY__MAX_TASKS_PER_CHILD", "env_value": 50,
                           "value": self.values.get("celery.max_tasks_per_child")})
        return c


@pytest.fixture
def motore(monkeypatch):
    m = MotoreConRiavvio()
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(m.gestisci), base_url="http://engine"))
    return m


@pytest.fixture
def app(session, motore):
    from app.routes import api_tokens as tokens_routes
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, tokens_routes.router, routes.router, routes.rollout_router, engine_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _component(state, name):
    return next(c for c in state["components"] if c["component"] == name)


# ── what a save marks ────────────────────────────────────────────────────────
def test_saving_a_start_setting_marks_its_components_and_a_live_one_does_not(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put("/admin/gateway-settings", json={"values": {"ai.max_requests": 10}}, headers=CSRF)
    assert session.exec(select(SettingsRollout)).all() == []
    capo.put("/admin/gateway-settings", json={"values": {"db.max_overflow": 30}}, headers=CSRF)
    capo.put("/admin/engine-settings", json={"values": {"celery.max_tasks_per_child": 20}}, headers=CSRF)
    session.expire_all()
    marked = {r.component for r in session.exec(select(SettingsRollout)).all() if r.changed_at}
    assert marked == {"gateway", "orchestrator", "worker", "preview-worker"}
    # going back to the environment's value is a change too
    before = session.get(SettingsRollout, "gateway").changed_at
    capo.put("/admin/gateway-settings", json={"values": {"db.max_overflow": None}}, headers=CSRF)
    session.expire_all()
    assert session.get(SettingsRollout, "gateway").changed_at >= before


# ── on Docker Compose ────────────────────────────────────────────────────────
def test_on_compose_the_panel_names_the_services_and_the_administrator_says_done(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put("/admin/gateway-settings", json={"values": {"app.gateway_threads": 60}}, headers=CSRF)
    state = capo.get("/admin/settings-rollout").json()
    assert state["mode"] == "compose" and state["editable"]
    gw, eng = _component(state, "gateway"), _component(state, "engine")
    assert gw["pending"] and gw["compose_service"] == "gateway" and _component(state, "orchestrator")["compose_service"] == "gateway"
    assert not eng["pending"] and eng["compose_service"] == "backend"
    state = capo.post("/admin/settings-rollout", json={"components": ["gateway", "orchestrator"]}, headers=CSRF).json()
    assert not _component(state, "gateway")["pending"]
    entry = session.exec(select(AuditLog).where(AuditLog.action == "settings.rollout")).one()
    assert json.loads(entry.detail) == {"components": ["gateway", "orchestrator"], "mode": "compose"}


def test_only_an_administrator_and_never_a_token(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    occhio = _entra(app, session, "occhio@x.local", is_observer=True)
    assert occhio.get("/admin/settings-rollout").json()["editable"] is False
    assert occhio.post("/admin/settings-rollout", json={"components": ["engine"]}, headers=CSRF).status_code == 403
    token = capo.post("/auth/tokens", json={"name": "x", "scope": "write", "expires_in_days": 5}, headers=CSRF).json()["token"]
    from fastapi.testclient import TestClient

    r = TestClient(app).post("/admin/settings-rollout", json={"components": ["engine"]}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert capo.post("/admin/settings-rollout", json={"components": ["frontend"]}, headers=CSRF).status_code == 422


# ── on Kubernetes ────────────────────────────────────────────────────────────
def test_the_pods_started_before_the_change_are_old(app, session, kube):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    kube.add("tab-worker-a", "worker", T0 - timedelta(days=1))
    session.add(SettingsRollout(component="worker", changed_at=T0))
    session.commit()
    state = capo.get("/admin/settings-rollout").json()
    w = _component(state, "worker")
    assert state["mode"] == "kubernetes" and (w["pods"], w["ready"], w["old"], w["pending"]) == (1, 1, 1, True)
    assert "app.kubernetes.io/instance=tab,app.kubernetes.io/component=worker" in kube.selectors
    kube.replace("tab-worker-a", "tab-worker-b", T0 + timedelta(seconds=5))
    assert _component(capo.get("/admin/settings-rollout").json(), "worker")["pending"] is False


async def test_one_pod_at_a_time_the_oldest_first(session, db_engine, kube):
    kube.add("tab-worker-new", "worker", T0 - timedelta(hours=1))
    kube.add("tab-worker-old", "worker", T0 - timedelta(days=2))
    session.add(SettingsRollout(component="worker", changed_at=T0, requested_at=T0))
    session.commit()
    assert await sr.one_step(db_engine) == "evicted worker: tab-worker-old"
    # on its way out: wait, nothing else is evicted
    assert (await sr.one_step(db_engine)).startswith("waiting worker")
    kube.replace("tab-worker-old", "tab-worker-c", T0 + timedelta(seconds=10))
    kube.add("tab-worker-d", "worker", T0 + timedelta(seconds=20), ready=False)  # a new one, not ready yet
    assert await sr.one_step(db_engine) == "waiting worker: tab-worker-d"
    kube.pods["tab-worker-d"]["status"]["conditions"][0]["status"] = "True"
    assert await sr.one_step(db_engine) == "evicted worker: tab-worker-new"
    kube.replace("tab-worker-new", "tab-worker-e", T0 + timedelta(seconds=30))
    assert await sr.one_step(db_engine) == "done worker"
    session.expire_all()
    assert session.get(SettingsRollout, "worker").requested_at is None
    assert kube.evicted == ["tab-worker-old", "tab-worker-new"]


async def test_the_components_go_in_order_and_the_orchestrator_evicts_itself_last(session, db_engine, kube):
    kube.add("tab-orchestrator-a", "orchestrator", T0 - timedelta(days=1))   # itself (HOSTNAME)
    kube.add("tab-orchestrator-b", "orchestrator", T0 - timedelta(hours=1))
    kube.add("tab-engine-a", "engine", T0 - timedelta(days=1))
    for c in ("orchestrator", "engine"):
        session.add(SettingsRollout(component=c, changed_at=T0, requested_at=T0))
    session.commit()
    assert await sr.one_step(db_engine) == "evicted engine: tab-engine-a"   # the engine before the orchestrator
    kube.replace("tab-engine-a", "tab-engine-b", T0 + timedelta(seconds=5))
    assert await sr.one_step(db_engine) == "done engine"
    assert await sr.one_step(db_engine) == "evicted orchestrator: tab-orchestrator-b"  # not itself, though older
    kube.replace("tab-orchestrator-b", "tab-orchestrator-c", T0 + timedelta(seconds=10))
    assert await sr.one_step(db_engine) == "evicted orchestrator: tab-orchestrator-a"
    # the next orchestrator finds nothing left
    kube.replace("tab-orchestrator-a", "tab-orchestrator-d", T0 + timedelta(seconds=20))
    assert await sr.one_step(db_engine) == "done orchestrator"


async def test_a_disruption_budget_that_says_not_now_is_waited_for(session, db_engine, kube):
    kube.add("tab-engine-a", "engine", T0 - timedelta(days=1))
    session.add(SettingsRollout(component="engine", changed_at=T0, requested_at=T0))
    session.commit()
    kube.pdb_says_no = True
    assert await sr.one_step(db_engine) == "blocked engine: tab-engine-a"
    kube.pdb_says_no = False
    assert await sr.one_step(db_engine) == "evicted engine: tab-engine-a"


async def test_a_new_pod_that_never_gets_ready_stops_the_rollout_there(session, db_engine, kube):
    """A value that stops the pods from starting (a pool larger than Postgres accepts):
    the old pods keep serving, the panel says which pod it waits for."""
    kube.add("tab-gateway-a", "gateway", T0 - timedelta(days=1))
    kube.add("tab-gateway-b", "gateway", T0 - timedelta(days=1))
    session.add(SettingsRollout(component="gateway", changed_at=T0, requested_at=T0))
    session.commit()
    assert (await sr.one_step(db_engine)).startswith("evicted gateway")
    first = kube.evicted[0]
    kube.replace(first, "tab-gateway-crash", T0 + timedelta(seconds=5))
    kube.pods["tab-gateway-crash"]["status"]["conditions"][0]["status"] = "False"
    for _ in range(3):
        assert await sr.one_step(db_engine) == "waiting gateway: tab-gateway-crash"
    assert len(kube.evicted) == 1
    # the administrator puts the value back and asks again: the broken pod goes first
    session.expire_all()
    row = session.get(SettingsRollout, "gateway")
    row.requested_at = T0 + timedelta(minutes=5)
    session.add(row)
    session.commit()
    assert await sr.one_step(db_engine) == "evicted gateway: tab-gateway-crash"


async def test_a_cancelled_rollout_stops(app, session, db_engine, kube):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    kube.add("tab-beat-a", "beat", T0 - timedelta(days=1))
    session.add(SettingsRollout(component="beat", changed_at=T0))
    session.commit()
    state = capo.post("/admin/settings-rollout", json={"components": ["beat"]}, headers=CSRF).json()
    assert _component(state, "beat")["requested_at"] is not None
    capo.delete("/admin/settings-rollout/beat", headers=CSRF)
    assert await sr.one_step(db_engine) is None and kube.evicted == []
    assert session.exec(select(AuditLog).where(AuditLog.action == "settings.rollout_cancel")).one()


def test_without_the_chart_right_the_panel_says_manual(app, session, kube, monkeypatch):
    monkeypatch.setenv("KUBERNETES__ROLLOUT", "false")
    get_settings.cache_clear()
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    assert capo.get("/admin/settings-rollout").json()["mode"] == "manual"


# ── from the bug hunt of 2026-10-23 ───────────────────────────────────────────
async def test_the_last_pods_replacement_is_waited_for(session, db_engine, kube):
    """A component of one pod was closed as soon as it was evicted, and the next one went
    down while its replacement was not ready yet."""
    kube.add("tab-engine-a", "engine", T0 - timedelta(days=1))
    kube.add("tab-beat-a", "beat", T0 - timedelta(days=1))
    for c in ("engine", "beat"):
        session.add(SettingsRollout(component=c, changed_at=T0, requested_at=T0))
    session.commit()
    assert await sr.one_step(db_engine) == "evicted beat: tab-beat-a"
    kube.replace("tab-beat-a", "tab-beat-b", T0 + timedelta(seconds=5))
    kube.pods["tab-beat-b"]["status"]["conditions"][0]["status"] = "False"
    assert await sr.one_step(db_engine) == "waiting beat: tab-beat-b"
    kube.pods["tab-beat-b"]["status"]["conditions"][0]["status"] = "True"
    assert await sr.one_step(db_engine) == "done beat"
    assert await sr.one_step(db_engine) == "evicted engine: tab-engine-a"


async def test_a_pod_gone_with_its_node_does_not_hold_the_rollout(session, db_engine, kube):
    kube.add("tab-worker-lost", "worker", T0 - timedelta(days=1), terminating=True)  # since a day
    kube.add("tab-worker-a", "worker", T0 - timedelta(days=1))
    session.add(SettingsRollout(component="worker", changed_at=T0, requested_at=T0))
    session.commit()
    assert await sr.one_step(db_engine) == "evicted worker: tab-worker-a"


def test_on_kubernetes_without_the_token_it_is_not_compose(app, session, monkeypatch):
    """With clusterView and settingsRollout off the chart mounts no token: the backups and
    the panel took the installation for Docker Compose."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    get_settings.cache_clear()
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    assert capo.get("/admin/settings-rollout").json()["mode"] == "manual"
    from app.services import db_backup

    assert db_backup.disabled_reason(session) != "kubernetes"


def test_an_orchestrator_of_its_own_on_compose(app, session, monkeypatch):
    monkeypatch.setenv("APP__ROLE", "api")
    get_settings.cache_clear()
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    state = capo.get("/admin/settings-rollout").json()
    assert _component(state, "orchestrator")["compose_service"] == "orchestrator"


def test_with_the_panel_off_nothing_waits_for_a_restart(app, session, monkeypatch):
    session.add(SettingsRollout(component="gateway", changed_at=T0))
    session.commit()
    monkeypatch.setenv("ENGINE__SETTINGS_PANEL", "false")
    get_settings.cache_clear()
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    assert not _component(capo.get("/admin/settings-rollout").json(), "gateway")["pending"]


def test_an_api_error_offers_no_restart(app, session, kube, monkeypatch):
    """Not knowing the pods, every changed component was offered, and its pods all evicted."""
    session.add(SettingsRollout(component="worker", changed_at=T0))
    session.commit()
    monkeypatch.setattr(kube_jobs, "_transport", httpx.MockTransport(lambda req: httpx.Response(403, json={"message": "forbidden"})))
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    w = _component(capo.get("/admin/settings-rollout").json(), "worker")
    assert not w["pending"] and "403" in w["error"]


async def test_the_pods_of_other_charts_are_left_alone(session, db_engine, kube, monkeypatch):
    monkeypatch.setenv("KUBERNETES__APP_NAME", "tabularia")
    get_settings.cache_clear()
    await sr.pods("worker")
    assert kube.selectors[-1] == "app.kubernetes.io/instance=tab,app.kubernetes.io/component=worker,app.kubernetes.io/name=tabularia"
