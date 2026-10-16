"""Admin → Workers: processes per worker (engine settings) and, on Kubernetes, the
autoscaling bounds of the worker pods.

The rules these tests hold: everyone with the admin panel sees the workers, the
queues and the bounds, an observer with the emails masked; only an administrator
changes them, never with an API token, and the audit says from what to what; the
bounds go to the cluster first and are saved only if it took them; they are kept
applied when the cluster drifts (a helm upgrade), within the ceiling the chart set;
an HPA keeps at least one pod, KEDA may go to zero; without Kubernetes the panel
says so instead of scaling.
"""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.db.session import get_session
from app.models import AuditLog, WorkerScaling
from app.routes import engine_settings as settings_routes
from app.routes import workers as routes
from app.services import kube_scaling as kube
from tests.conftest import make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"


class Engine:
    """The engine for what is needed here: the two worker settings and two workers."""

    def __init__(self):
        self.values: dict = {}
        self.old = False

    def _catalog(self):
        return {"items": [
            {"key": f"workers.{k}_concurrency", "category": "workers", "unit": "processes", "min": 1, "max": 32,
             "env_var": f"WORKERS__{k.upper()}_CONCURRENCY", "env_value": 0, "value": self.values.get(f"workers.{k}_concurrency")}
            for k in ("run", "preview")], "unavailable": [], "refresh_seconds": 10.0, "discarded": []}

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/workers":
            if self.old:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json={"workers": [
                {"name": "celery@r1", "kind": "run", "queues": ["celery"], "processes": 2, "target": 2, "busy": 1,
                 "memory_used": 300 * 2**20, "memory_limit": 4 * 2**30, "pid": 1, "seen_at": 1.0},
                {"name": "celery@p1", "kind": "preview", "queues": ["preview"], "processes": 4, "target": 4, "busy": 0},
            ], "queued": {"celery": 3, "preview": 0}, "tick_seconds": 10.0, "ttl_seconds": 45})
        if req.method == "GET":
            return httpx.Response(200, json=self._catalog())
        self.values = dict(json.loads(req.content)["values"])
        return httpx.Response(200, json=self._catalog())


class Cluster:
    """The Kubernetes API for the two worker Deployments and their autoscalers."""

    def __init__(self):
        self.objects = {
            "deployments/tab-worker": {"spec": {"replicas": 2}, "status": {"replicas": 2, "readyReplicas": 2}},
            "deployments/tab-preview-worker": {"spec": {"replicas": 1}, "status": {"replicas": 1, "readyReplicas": 1}},
            "horizontalpodautoscalers/tab-worker": {"spec": {"minReplicas": 2, "maxReplicas": 4}, "status": {"desiredReplicas": 2}},
            "scaledobjects/tab-preview-worker": {"spec": {"minReplicaCount": 1, "maxReplicaCount": 3}},
        }
        self.patches: list[tuple[str, dict, str]] = []
        self.refuse_patch = 0

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer sa-token"
        parts = req.url.path.split("/")
        assert parts[parts.index("namespaces") + 1] == "tab"
        key = "/".join(parts[-2:])
        if key not in self.objects:
            return httpx.Response(404, json={"message": f"{key} not found"})
        if req.method == "PATCH":
            if self.refuse_patch:
                return httpx.Response(self.refuse_patch, json={"message": "forbidden: cannot patch"})
            body = json.loads(req.content)
            self.patches.append((key, body, req.headers["content-type"]))
            self.objects[key]["spec"].update(body["spec"])
        return httpx.Response(200, json=self.objects[key])


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(e.handle), base_url="http://engine"))
    return e


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    c = Cluster()
    (tmp_path / "token").write_text("sa-token\n")
    for k, v in {"SCALING": "true", "NAMESPACE": "tab", "TOKEN_FILE": str(tmp_path / "token"), "CA_FILE": "",
                 "RUN_WORKER_DEPLOYMENT": "tab-worker", "RUN_WORKER_AUTOSCALER": "hpa/tab-worker",
                 "RUN_WORKER_MAX_REPLICAS": "8", "PREVIEW_WORKER_DEPLOYMENT": "tab-preview-worker",
                 "PREVIEW_WORKER_AUTOSCALER": "scaledobject/tab-preview-worker", "PREVIEW_WORKER_MAX_REPLICAS": "6"}.items():
        monkeypatch.setenv(f"KUBERNETES__{k}", v)
    get_settings.cache_clear()
    monkeypatch.setattr(kube, "_transport", httpx.MockTransport(c.handle))
    return c


@pytest.fixture
def app(session, engine):
    from app.routes import api_tokens as tokens_routes
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, tokens_routes.router, settings_routes.router, routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _login(app, session, email, **kw) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw)
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def _group(state, kind):
    return next(g for g in state["groups"] if g["kind"] == kind)


def test_the_admin_sees_workers_queues_and_processes(app, session):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    state = boss.get("/admin/workers").json()
    run, preview = _group(state, "run"), _group(state, "preview")
    assert [w["name"] for w in run["workers"]] == ["celery@r1"] and [w["name"] for w in preview["workers"]] == ["celery@p1"]
    assert (run["queue"], run["queued"], preview["queued"]) == ("celery", 3, 0)
    assert run["workers"][0]["busy"] == 1 and run["workers"][0]["memory_limit"] == 4 * 2**30
    assert run["concurrency"] | {"value": None, "applied": None, "min": 1, "max": 32} == run["concurrency"]
    assert run["scaling"]["mode"] == "none"
    assert state["concurrency_editable"] and not state["scaling_editable"] and state["workers_known"]


def test_processes_are_engine_settings_and_show_here(app, session, engine):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    r = boss.put("/admin/engine-settings", json={"values": {"workers.run_concurrency": 4}}, headers=CSRF)
    assert r.status_code == 200 and engine.values == {"workers.run_concurrency": 4}
    c = _group(boss.get("/admin/workers").json(), "run")["concurrency"]
    assert (c["value"], c["applied"], c["updated_by"]) == (4, 4, "boss@x.local")
    assert session.exec(select(AuditLog).where(AuditLog.action == "engine_settings.update")).first() is not None


def test_the_observer_sees_masked_and_changes_nothing(app, session, engine, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    boss.put("/admin/engine-settings", json={"values": {"workers.preview_concurrency": 6}}, headers=CSRF)
    boss.put("/admin/workers/run/scaling", json={"min_replicas": 2, "max_replicas": 6}, headers=CSRF)
    eye = _login(app, session, "eye@x.local", is_observer=True)
    state = eye.get("/admin/workers").json()
    assert not state["concurrency_editable"] and not state["scaling_editable"]
    assert _group(state, "preview")["concurrency"]["updated_by"] == "b***@x.local"
    assert _group(state, "run")["scaling"]["updated_by"] == "b***@x.local"
    assert eye.put("/admin/workers/run/scaling", json={"min_replicas": 1, "max_replicas": 2}, headers=CSRF).status_code == 403
    assert eye.delete("/admin/workers/run/scaling", headers=CSRF).status_code == 403


def test_a_token_cannot_scale(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    token = boss.post("/auth/tokens", json={"name": "x", "scope": "write", "expires_in_days": 5}, headers=CSRF).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    script = TestClient(app)
    state = script.get("/admin/workers", headers=h).json()
    assert not state["concurrency_editable"] and not state["scaling_editable"]
    r = script.put("/admin/workers/run/scaling", json={"min_replicas": 1, "max_replicas": 2}, headers={**h, "Accept-Language": "it"})
    assert r.status_code == 403 and "worker" in r.json()["detail"]
    assert script.delete("/admin/workers/run/scaling", headers=h).status_code == 403
    assert cluster.patches == []


def test_an_engine_down_or_too_old_says_so(app, session, engine, monkeypatch):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    engine.old = True
    r = boss.get("/admin/workers")
    assert r.status_code == 503 and "older version" in r.json()["detail"]

    def down(req):
        raise httpx.ConnectError("no engine")

    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://engine"))
    assert boss.get("/admin/workers").status_code == 503


def test_without_kubernetes_the_panel_does_not_scale(app, session):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    r = boss.put("/admin/workers/run/scaling", json={"min_replicas": 1, "max_replicas": 2}, headers=CSRF)
    assert r.status_code == 409 and "Kubernetes" in r.json()["detail"]


def test_bounds_go_to_the_cluster_then_are_saved_and_audited(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    s = _group(boss.get("/admin/workers").json(), "run")["scaling"]
    assert (s["mode"], s["autoscaler"], s["min"], s["max"], s["desired"], s["ready"], s["ceiling"], s["floor"], s["saved"]) == \
        ("kubernetes", "hpa", 2, 4, 2, 2, 8, 1, False)

    state = boss.put("/admin/workers/run/scaling", json={"min_replicas": 3, "max_replicas": 7}, headers=CSRF).json()
    s = _group(state, "run")["scaling"]
    assert (s["min"], s["max"], s["saved"], s["saved_min"], s["saved_max"], s["updated_by"]) == (3, 7, True, 3, 7, "boss@x.local")
    key, body, ctype = cluster.patches[-1]
    assert key == "horizontalpodautoscalers/tab-worker" and body == {"spec": {"minReplicas": 3, "maxReplicas": 7}}
    assert ctype == "application/merge-patch+json"
    row = session.get(WorkerScaling, "run")
    assert (row.min_replicas, row.max_replicas) == (3, 7)
    a = session.exec(select(AuditLog).where(AuditLog.action == "workers.scaling_update")).one()
    assert a.target_label == "run" and json.loads(a.detail) == {"from": {"min": 2, "max": 4}, "to": {"min": 3, "max": 7}}


def test_keda_patches_its_own_fields_and_may_go_to_zero(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    s = _group(boss.get("/admin/workers").json(), "preview")["scaling"]
    assert (s["autoscaler"], s["min"], s["max"], s["floor"], s["ceiling"]) == ("scaledobject", 1, 3, 0, 6)
    r = boss.put("/admin/workers/preview/scaling", json={"min_replicas": 0, "max_replicas": 5}, headers=CSRF)
    assert r.status_code == 200
    assert cluster.patches[-1][:2] == ("scaledobjects/tab-preview-worker", {"spec": {"minReplicaCount": 0, "maxReplicaCount": 5}})


@pytest.mark.parametrize("bounds,code,limit", [
    ((0, 4), "min", 1),  # an HPA keeps one pod
    ((3, 2), "order", 3),
    ((1, 9), "ceiling", 8),  # above what the chart allows
])
def test_bounds_out_of_range_are_refused_with_a_code(app, session, cluster, bounds, code, limit):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    r = boss.put("/admin/workers/run/scaling", json={"min_replicas": bounds[0], "max_replicas": bounds[1]}, headers=CSRF)
    assert r.status_code == 422 and r.json()["detail"]["errors"] == [{"code": code, "limit": limit}]
    assert cluster.patches == [] and session.get(WorkerScaling, "run") is None


def test_a_cluster_that_refuses_changes_nothing(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    cluster.refuse_patch = 403
    r = boss.put("/admin/workers/run/scaling", json={"min_replicas": 2, "max_replicas": 6}, headers=CSRF)
    assert r.status_code == 502 and "Nothing was changed" in r.json()["detail"] and "403" in r.json()["detail"]
    assert session.get(WorkerScaling, "run") is None


def test_no_autoscaler_no_bounds(app, session, cluster, monkeypatch):
    monkeypatch.setenv("KUBERNETES__RUN_WORKER_AUTOSCALER", "")
    get_settings.cache_clear()
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    s = _group(boss.get("/admin/workers").json(), "run")["scaling"]
    assert s["autoscaler"] is None and s["min"] is None and s["replicas"] == 2
    r = boss.put("/admin/workers/run/scaling", json={"min_replicas": 1, "max_replicas": 2}, headers=CSRF)
    assert r.status_code == 422 and r.json()["detail"]["errors"][0]["code"] == "no_autoscaler"


def test_an_unreadable_cluster_is_shown_not_a_crash(app, session, cluster, monkeypatch, tmp_path):
    monkeypatch.setenv("KUBERNETES__TOKEN_FILE", str(tmp_path / "missing"))
    get_settings.cache_clear()
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    s = _group(boss.get("/admin/workers").json(), "run")["scaling"]
    assert s["mode"] == "kubernetes" and "service account token" in s["error"] and s["ceiling"] == 8


def test_giving_the_bounds_back_to_the_chart(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    boss.put("/admin/workers/run/scaling", json={"min_replicas": 3, "max_replicas": 7}, headers=CSRF)
    patches = len(cluster.patches)
    s = _group(boss.delete("/admin/workers/run/scaling", headers=CSRF).json(), "run")["scaling"]
    assert not s["saved"] and (s["min"], s["max"]) == (3, 7)  # the cluster keeps them until the next deployment
    assert len(cluster.patches) == patches and session.get(WorkerScaling, "run") is None
    a = session.exec(select(AuditLog).where(AuditLog.action == "workers.scaling_reset")).one()
    assert json.loads(a.detail) == {"from": {"min": 3, "max": 7}}


@pytest.mark.anyio
async def test_drifted_bounds_are_put_back_within_the_ceiling(db_engine, session, cluster, monkeypatch):
    from datetime import datetime

    session.add(WorkerScaling(kind="run", min_replicas=3, max_replicas=7, updated_at=datetime(2026, 10, 15)))
    session.commit()
    cluster.objects["horizontalpodautoscalers/tab-worker"]["spec"] = {"minReplicas": 2, "maxReplicas": 4}  # a helm upgrade
    assert await kube.un_giro(db_engine) == 1
    assert cluster.objects["horizontalpodautoscalers/tab-worker"]["spec"] == {"minReplicas": 3, "maxReplicas": 7}
    assert await kube.un_giro(db_engine) == 0  # nothing to put back

    monkeypatch.setenv("KUBERNETES__RUN_WORKER_MAX_REPLICAS", "5")  # the chart lowered the ceiling
    get_settings.cache_clear()
    assert await kube.un_giro(db_engine) == 1
    assert cluster.objects["horizontalpodautoscalers/tab-worker"]["spec"] == {"minReplicas": 3, "maxReplicas": 5}


@pytest.mark.anyio
async def test_the_loop_does_nothing_off_kubernetes():
    import asyncio

    await asyncio.wait_for(kube.riapplica_loop(asyncio.Event()), timeout=1)  # returns at once
