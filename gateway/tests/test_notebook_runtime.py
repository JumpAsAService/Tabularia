"""Notebooks, pieces 2–3: the pods, the proxy to marimo, the data they read
(services/notebook_runtime.py, routes/notebook_sessions.py, routes/notebook_proxy.py,
routes/notebook_data.py; docs/specs/notebooks.md).

Kubernetes is tests/fake_kube.py's, with Pods that say what each test wants; marimo is a
small HTTP and WebSocket server on this machine, the «pod» the proxy reaches.

Fixed here:
- the app's pod reads with the rights of the author of the version it runs, the editor's
  with the editor's; one editor at a time (409 with who); the Job has the scripts'
  security context, a lifetime, the notebook's file and the module, the token in a
  Secret only (the row keeps its fingerprint); starting is idempotent; the cap of live
  pods; an image without marimo, an image not downloaded, marimo gone, said in words;
- stopping the editor keeps its file as the draft; «Save version» makes a version whose
  author is whoever edits, none when nothing changed;
- the notebook token reads datasources by id, path or name, VIEW checked at every read
  (taken away → the next read fails), audited; it holds nowhere else, only from its pod's
  address, and not after its session ends;
- the proxy (here on the interface's own address, NOTEBOOKS__ALLOW_SAME_ORIGIN; the
  notebooks' own address is tests/test_notebook_frames.py's): the root starts the pod and
  answers a page that reloads, then marimo's answers; Origin required on what is not a
  GET and on WebSockets; the editor only for the person editing; framing only by
  Tabularia's own pages;
- the environment's variables reach the pod as they reach a script (the secret ones
  only in its Secret), never replacing the notebook's own;
- while the editor is open the source changes only there: PATCH and restore 409 with who
  holds it, «Save version» goes on, and once closed they work again;
- the reaper: idle pods stopped (the editor's file kept), pods gone forgotten, Jobs
  without a session deleted, starts that time out failed;
- the visitors of an app: counted in the database, the same on every replica; the cap
  is the notebook's own or the installation's default, never above the ceiling; past it
  the root answers «full» and reloads itself, a WebSocket that loses the race is closed
  with 1013 and the reason; the editor has no cap; stale visitors no longer count and the
  reaper clears them; they go with their notebook and their account.
"""
import hashlib
import json
import threading
import time
from datetime import timedelta

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.db.session import get_session
from app.core.crypto import encrypt_secret
from app.models import (
    AuditLog, Notebook, NotebookSession, NotebookVersion, NotebookVisitor, Permission, Project, ScriptEnvironment,
    ScriptEnvironmentVariable, ScriptEnvironmentVersion,
)
from app.services import kube_jobs
from app.services import notebook_runtime as runtime
from tests.conftest import make_datasource, make_permission, make_project, make_user
from tests.fake_kube import Cluster
from tests.test_notebooks import CSRF, SOURCE, _create, _login, _world

ORIGIN = {"Origin": "http://testserver"}


class NotebookCluster(Cluster):
    """Jobs that keep running; their Pod as `pod` says: "pending", "ready", "pull" (the
    image not downloaded), "ended" (marimo stopped, its log `log`)."""

    def __init__(self):
        super().__init__()
        self.behave = lambda job: (None, "", None)
        self.pod = "pending"
        self.log = ""

    def handle(self, req: httpx.Request) -> httpx.Response:
        parts = req.url.path.strip("/").split("/")
        if req.method == "GET" and parts[:2] == ["apis", "batch"] and len(parts) == 6:  # a list of Jobs
            label, value = req.url.params["labelSelector"].split("=")
            for j in self.jobs():
                self._set_status(j)
            return httpx.Response(200, json={"items": [j for j in self.jobs()
                                                       if j["metadata"]["labels"].get(label) == value]})
        return super().handle(req)

    def _pods(self, ns, name, parts, query):
        if name is not None:  # a container's log
            return httpx.Response(200, text=self.log)
        job_name = query["labelSelector"][0].split("=", 1)[1]
        if ("jobs", ns, job_name) not in self.objects:
            return httpx.Response(200, json={"items": []})
        status: dict = {"containerStatuses": [{"name": "marimo", "state": {"waiting": {"reason": "ContainerCreating"}}}]}
        if self.pod == "ready":
            status = {"podIP": "127.0.0.1", "conditions": [{"type": "Ready", "status": "True"}],
                      "containerStatuses": [{"name": "marimo", "state": {"running": {}}}]}
        elif self.pod == "pull":
            status = {"containerStatuses": [{"name": "marimo", "state": {"waiting": {
                "reason": "ErrImagePull", "message": "manifest unknown: manifest unknown"}}}]}
        elif self.pod == "ended":
            status = {"podIP": "127.0.0.1", "containerStatuses": [
                {"name": "marimo", "state": {"terminated": {"exitCode": 1, "reason": "Error"}}}]}
        return httpx.Response(200, json={"items": [{
            "metadata": {"name": f"{job_name}-pod", "creationTimestamp": "2026-10-27T00:00:00Z"}, "status": status}]})


class Pod:
    """marimo, as far as the gateway sees it: anything under the notebook's address, a
    WebSocket that greets and echoes, and the editor's file on /notebook.py."""

    def __init__(self):
        self.file = SOURCE
        self.requests: list[dict] = []

        async def notebook_file(request):
            return PlainTextResponse(self.file)

        async def anything(request):
            body = (await request.body()).decode()
            self.requests.append({"method": request.method, "path": request.url.path, "query": request.url.query,
                                  "body": body, "headers": dict(request.headers)})
            return JSONResponse({"marimo": request.url.path, "body": body},
                                headers={"X-Frame-Options": "DENY",
                                         "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
                                         "Set-Cookie": "marimo=1"})

        async def broken(request):
            async def body():  # the pod going away in the middle of an answer
                yield b"half of it"
                raise RuntimeError("the pod went away")
            return StreamingResponse(body())

        async def socket(ws):
            await ws.accept()
            await ws.send_text(f"hello {ws.url.path}?{ws.url.query}")
            try:
                while True:
                    message = await ws.receive_text()
                    if message == "bye":
                        await ws.close(code=1000)
                        return
                    if message.startswith("big "):  # a message of that many bytes
                        await ws.send_text("x" * int(message.split()[1]))
                        continue
                    await ws.send_text(f"echo {message}")
            except WebSocketDisconnect:
                pass

        self.app = Starlette(routes=[
            Route("/notebook.py", notebook_file),
            Route("/{base:path}/broken", broken),
            Route("/{path:path}", anything, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
            WebSocketRoute("/{path:path}", socket),
        ])


@pytest.fixture
def pod(monkeypatch):
    p = Pod()
    server = uvicorn.Server(uvicorn.Config(p.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off",
                                           ws="websockets-sansio"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    monkeypatch.setattr(runtime, "APP_PORT", port)
    monkeypatch.setattr(runtime, "FILES_PORT", port)
    yield p
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    c = NotebookCluster()
    (tmp_path / "token").write_text("sa-token\n")
    # the proxy's tests here run on the interface's own address (NOTEBOOKS__ALLOW_SAME_ORIGIN);
    # the notebooks' own address, its tickets and its cookie are tests/test_notebook_frames.py's
    for k, v in {"KUBERNETES__TOKEN_FILE": str(tmp_path / "token"), "KUBERNETES__CA_FILE": "",
                 "SCRIPTS__ENABLED": "true", "SCRIPTS__NAMESPACE": "tab-scripts",
                 "SCRIPTS__REGISTRY": "registry.example.com/tab", "SCRIPTS__REGISTRY_PULL_SECRET": "registry-pull",
                 "NOTEBOOKS__GATEWAY_URL": "http://tab-gateway.tab.svc:8000/",
                 "NOTEBOOKS__ALLOW_SAME_ORIGIN": "true"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    monkeypatch.setattr(kube_jobs, "_transport", httpx.MockTransport(c.handle))
    runtime._checked.clear()
    yield c
    runtime._checked.clear()
    get_settings.cache_clear()


class Engine:
    """The engine's /files/presigned."""

    def __init__(self):
        self.asked: list[dict] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/files/presigned":
            self.asked.append(dict(req.url.params))
            return httpx.Response(200, json={"url": f"https://s3.example.com/{req.url.params['key']}?sig=x"})
        return httpx.Response(404, json={"detail": "not mocked"})


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(e.handle), base_url="http://engine"))
    return e


@pytest.fixture
def app(session):
    from app.routes import auth as auth_routes
    from app.routes import notebook_data, notebook_proxy, notebook_sessions
    from app.routes import notebooks as notebooks_routes

    app = FastAPI()
    for r in (auth_routes.router, notebooks_routes.router, notebook_sessions.router, notebook_proxy.router,
              notebook_data.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    notebook_proxy.install(app)  # the notebooks' own host, as main.py
    return app


def _ready_env(session, env_id, lock="marimo==0.25.2\npolars==1.30.0\n"):
    v = ScriptEnvironmentVersion(environment_id=env_id, number=1, language_version="3.12", base_image="python:3.12-slim",
                                 status="ready", image_ref="registry.example.com/tab/tabularia-env-1@sha256:abc",
                                 arch="amd64", lock=lock)
    session.add(v)
    session.commit()
    env = session.get(ScriptEnvironment, env_id)
    env.current_version_id = v.id
    session.add(env)
    session.commit()


def _setup(app, session):
    boss, ana, ben, cleo, env_id, folder = _world(app, session)
    _ready_env(session, env_id)
    nid = _create(ana, folder, env_id).json()["id"]
    return boss, ana, ben, cleo, env_id, folder, nid


def _row(session, nid, mode) -> NotebookSession:
    session.expire_all()
    return session.exec(select(NotebookSession).where(NotebookSession.notebook_id == nid,
                                                      NotebookSession.mode == mode)).one()


def _token(cluster) -> str:
    secrets_ = [o for (k, _, _), o in cluster.objects.items() if k == "secrets"]
    return secrets_[-1]["stringData"][runtime.TOKEN_ENV]


def _job(cluster, mode="app") -> dict:
    return next(j for j in cluster.jobs() if j["metadata"]["labels"][runtime.LABEL_MODE] == mode)


def _actions(session, action):
    return session.exec(select(AuditLog).where(AuditLog.action == action)).all()


def _pod_client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 40000))  # the fake Pods' address


def _ready(client, nid):
    runtime._checked.clear()
    return client.get(f"/notebooks/{nid}/sessions").json()


# ── starting ─────────────────────────────────────────────────────────────────
def test_the_app_starts_a_job_reading_with_the_authors_rights(app, session, cluster):
    _, ana, _, cleo, _, _, nid = _setup(app, session)
    r = cleo.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["mode"] == "app" and out["status"] == "starting" and out["user"]["email"] == "ana@x.local"
    job = _job(cluster)
    assert job["metadata"]["labels"] == {"app.kubernetes.io/component": "notebook", "tabularia.io/notebook": str(nid),
                                         "tabularia.io/notebook-mode": "app"}
    assert job["spec"]["activeDeadlineSeconds"] == 12 * 3600 and job["spec"]["backoffLimit"] == 0
    spec = job["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsUser"] == 10001 and spec["securityContext"]["runAsNonRoot"] is True
    assert spec["automountServiceAccountToken"] is False and spec["imagePullSecrets"] == [{"name": "registry-pull"}]
    assert spec["nodeSelector"] == {"kubernetes.io/arch": "amd64"}
    (marimo,) = spec["containers"]
    assert marimo["image"].endswith("@sha256:abc")
    assert marimo["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                         "capabilities": {"drop": ["ALL"]}}
    cmd = marimo["command"]
    assert cmd[:2] == ["python", "-c"] and cmd[3:5] == ["run", "/work/notebook.py"]
    assert cmd[cmd.index("--base-url") + 1] == f"/notebooks/{nid}/app" and "--no-token" not in cmd
    env = {e["name"]: e["value"] for e in marimo["env"]}
    assert env == {"HOME": "/tmp", "PYTHONPATH": "/tabularia/lib", "TABULARIA_URL": "http://tab-gateway.tab.svc:8000"}
    assert marimo["readinessProbe"]["httpGet"] == {"path": f"/notebooks/{nid}/app/health", "port": 2718}
    assert marimo["resources"]["limits"] == {"cpu": "2", "memory": "4Gi"}
    # the file and the module in a ConfigMap; the token only in the Secret
    (config,) = [o for (k, _, _), o in cluster.objects.items() if k == "configmaps"]
    assert config["data"]["notebook.py"] == SOURCE
    assert config["data"]["tabularia.py"] == runtime.POD_MODULE.read_text()
    token = _token(cluster)
    assert token.startswith("nbk_") and token not in json.dumps(job)
    row = _row(session, nid, "app")
    assert row.token_fp == hashlib.sha256(token.encode()).hexdigest()
    assert row.user_id == session.get(Notebook, nid).owner_id  # ana's rights, though cleo opened it
    (started,) = _actions(session, "notebook.start")
    assert json.loads(started.detail)["rights_of"] == "ana@x.local" and started.actor_label == "cleo@x.local"
    # idempotent: the same session, no second Job
    assert cleo.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF).status_code == 202
    assert len(cluster.jobs()) == 1
    # the pod answers: ready, at its address
    cluster.pod = "ready"
    (live,) = _ready(cleo, nid)
    assert live["status"] == "ready" and _row(session, nid, "app").pod_ip == "127.0.0.1"


def test_one_editor_at_a_time_with_their_own_rights(app, session, cluster):
    _, ana, ben, cleo, _, folder, nid = _setup(app, session)
    r = ana.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF)
    assert r.status_code == 202 and r.json()["user"]["email"] == "ana@x.local"
    marimo, files = _job(cluster, "edit")["spec"]["template"]["spec"]["containers"]
    assert marimo["command"][3] == "edit" and "--no-token" in marimo["command"]
    assert files["command"][-3:] == ["0.0.0.0", "--directory", "/work"] and files["volumeMounts"][0]["readOnly"]
    busy = cleo.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF)
    assert busy.status_code == 409
    assert busy.json()["editor"]["email"] == "ana@x.local" and "being edited by ana@x.local" in busy.json()["detail"]
    # ben does not see the notebook (no use of its environment)
    assert ben.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF).status_code == 404
    # the editor's session is shown only to whoever may edit
    dan, dan_id = _login(app, session, "dan@x.local")
    from app.models import Group, UserGroupLink

    session.add(UserGroupLink(user_id=dan_id, group_id=session.exec(select(Group.id)).one()))
    session.commit()
    make_permission(session, user_id=dan_id, project_id=folder, capability="view")
    assert dan.get(f"/notebooks/{nid}/sessions").json() == []
    assert dan.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF).status_code == 403
    assert dan.delete(f"/notebooks/{nid}/sessions/edit", headers=CSRF).status_code == 403


def test_the_environments_variables_reach_the_pod_as_a_scripts_do(app, session, cluster, monkeypatch):
    _, ana, _, _, env_id, folder, nid = _setup(app, session)
    monkeypatch.setenv("SHARED_DSN", "postgres://handed-over")
    session.add_all([
        ScriptEnvironmentVariable(environment_id=env_id, name="THRESHOLD", value="100"),
        ScriptEnvironmentVariable(environment_id=env_id, name="API_KEY", secret=True, value_enc=encrypt_secret("s3cret")),
        ScriptEnvironmentVariable(environment_id=env_id, name="DSN", from_tabularia=True, value="SHARED_DSN"),
        # the notebook's own are never replaced
        ScriptEnvironmentVariable(environment_id=env_id, name="HOME", value="/elsewhere"),
        ScriptEnvironmentVariable(environment_id=env_id, name="PYTHONPATH", secret=True, value_enc=encrypt_secret("/x")),
    ])
    session.commit()
    assert ana.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF).status_code == 202
    job = _job(cluster, "edit")
    marimo = job["spec"]["template"]["spec"]["containers"][0]
    assert {e["name"]: e["value"] for e in marimo["env"]} == {
        "THRESHOLD": "100", "HOME": "/tmp", "PYTHONPATH": "/tabularia/lib",
        "TABULARIA_URL": "http://tab-gateway.tab.svc:8000"}
    assert marimo["envFrom"] == [{"secretRef": {"name": f"{job['metadata']['name']}-token"}}]
    (secret,) = [o for (k, _, _), o in cluster.objects.items() if k == "secrets"]
    data = secret["stringData"]
    assert data == {"API_KEY": "s3cret", "DSN": "postgres://handed-over", runtime.TOKEN_ENV: data[runtime.TOKEN_ENV]}
    assert "s3cret" not in json.dumps(job) and "handed-over" not in json.dumps(job)
    # a value Tabularia does not have: said in words, and nothing starts
    monkeypatch.delenv("SHARED_DSN")
    other = _create(ana, folder, env_id, name="Other").json()["id"]
    r = ana.post(f"/notebooks/{other}/sessions", json={"mode": "app"}, headers=CSRF)
    assert r.status_code == 422 and "DSN" in r.json()["detail"] and "SHARED_DSN" in r.json()["detail"]
    assert len(cluster.jobs()) == 1
    assert all(row.notebook_id == nid for row in session.exec(select(NotebookSession)).all())


def test_what_stops_an_app_from_starting(app, session, cluster, monkeypatch):
    boss, ana, _, cleo, env_id, folder, nid = _setup(app, session)
    # the author of the version gone
    row = session.exec(select(NotebookVersion).where(NotebookVersion.notebook_id == nid)).one()
    row.created_by = None
    session.add(row)
    session.commit()
    r = cleo.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)
    assert r.status_code == 422 and "no longer exists" in r.json()["detail"]
    # an image built before marimo was among the base packages
    nid2 = _create(ana, folder, env_id, name="Other").json()["id"]
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    v.lock = "polars==1.30.0\n"
    session.add(v)
    session.commit()
    r = ana.post(f"/notebooks/{nid2}/sessions", json={"mode": "app"}, headers=CSRF)
    assert r.status_code == 422 and "marimo is not in the image" in r.json()["detail"] and "Build again" in r.json()["detail"]
    v.lock = "marimo==0.25.2\n"
    session.add(v)
    session.commit()
    # the cap of live pods
    monkeypatch.setenv("NOTEBOOKS__MAX_LIVE", "1")
    get_settings.cache_clear()
    assert ana.post(f"/notebooks/{nid2}/sessions", json={"mode": "app"}, headers=CSRF).status_code == 202
    r = ana.post(f"/notebooks/{nid2}/sessions", json={"mode": "edit"}, headers=CSRF)
    assert r.status_code == 503 and "NOTEBOOKS__MAX_LIVE" in r.json()["detail"]
    # without scripting: 400
    monkeypatch.setenv("SCRIPTS__ENABLED", "false")
    get_settings.cache_clear()
    r = ana.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)
    assert r.status_code == 400 and "Kubernetes" in r.json()["detail"]


def test_a_pod_that_cannot_start_says_why(app, session, cluster):
    _, ana, _, _, _, _, nid = _setup(app, session)
    ana.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)
    cluster.pod = "ended"
    cluster.log = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'marimo'\n"
    (failed,) = _ready(ana, nid)
    assert failed["status"] == "failed" and "marimo is not in the image" in failed["error"]
    assert cluster.jobs() == []  # its Job goes at once
    # opening it again tries again
    cluster.pod = "pull"
    assert ana.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF).json()["status"] == "starting"
    (failed,) = _ready(ana, nid)
    assert failed["status"] == "failed" and "could not download the image" in failed["error"]
    assert "not in the registry" in failed["error"]


# ── the editor's file ────────────────────────────────────────────────────────
def test_closing_the_editor_keeps_its_file_as_the_draft(app, session, cluster, pod):
    _, ana, _, cleo, _, _, nid = _setup(app, session)
    cleo.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF)
    cluster.pod = "ready"
    _ready(cleo, nid)
    pod.file = SOURCE + "\n# work in progress\n"
    assert cleo.delete(f"/notebooks/{nid}/sessions/edit", headers=CSRF).status_code == 204
    n = ana.get(f"/notebooks/{nid}").json()
    assert n["draft"] == pod.file and n["draft_by"] == "cleo@x.local" and n["current_version"] == 1
    assert cluster.jobs() == [] and session.exec(select(NotebookSession)).all() == []
    (stopped,) = _actions(session, "notebook.stop")
    assert json.loads(stopped.detail) == {"mode": "edit", "reason": "stopped"}
    # the next editor starts from the draft
    ana.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF)
    (config,) = [o for (k, _, _), o in cluster.objects.items() if k == "configmaps"]
    assert config["data"]["notebook.py"] == pod.file


def test_save_version_makes_the_editor_its_author(app, session, cluster, pod):
    _, ana, _, cleo, _, _, nid = _setup(app, session)
    assert cleo.post(f"/notebooks/{nid}/edit/save", json={}, headers=CSRF).status_code == 409  # no editor open
    cleo.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF)
    cluster.pod = "ready"
    _ready(cleo, nid)
    # nothing changed: no version
    r = cleo.post(f"/notebooks/{nid}/edit/save", json={"note": None}, headers=CSRF)
    assert r.status_code == 200 and r.json()["current_version"] == 1
    pod.file = SOURCE + "\n# chart by region\n"
    r = cleo.post(f"/notebooks/{nid}/edit/save", json={"note": "by region"}, headers=CSRF)
    assert r.status_code == 200, r.text
    n = r.json()
    assert n["current_version"] == 2 and n["author"]["email"] == "cleo@x.local" and n["draft"] is None
    assert n["content"]["source"] == pod.file
    versions = ana.get(f"/notebooks/{nid}/versions").json()
    assert versions[0]["note"] == "by region" and versions[0]["created_by"] == "cleo@x.local"
    assert len(_actions(session, "notebook.save")) == 1
    # only the person editing saves
    assert ana.post(f"/notebooks/{nid}/edit/save", json={}, headers=CSRF).status_code == 409
    # the app now starts with cleo's rights
    assert ana.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF).json()["user"]["email"] == "cleo@x.local"


def test_the_source_changes_only_in_the_editor_while_it_is_open(app, session, cluster, pod):
    _, ana, _, cleo, _, _, nid = _setup(app, session)
    assert ana.patch(f"/notebooks/{nid}", json={"content": {"source": SOURCE + "# v2\n"}},
                     headers=CSRF).json()["current_version"] == 2
    cleo.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF)  # starting is open already
    busy = "«Sales app» is open in marimo's editor by cleo@x.local: save or close it there first"
    for c in (ana, cleo):  # whoever asks, the person editing too
        r = c.patch(f"/notebooks/{nid}", json={"content": {"source": SOURCE + "# elsewhere\n"}}, headers=CSRF)
        assert r.status_code == 409 and r.json()["detail"] == busy
        r = c.post(f"/notebooks/{nid}/versions/1/restore", headers=CSRF)
        assert r.status_code == 409 and r.json()["detail"] == busy
    # the rest changes as ever, and the same source is no change
    r = ana.patch(f"/notebooks/{nid}", json={"description": "margins", "content": {"source": SOURCE + "# v2\n"}},
                  headers=CSRF)
    assert r.status_code == 200 and r.json()["description"] == "margins" and r.json()["current_version"] == 2
    # «Save version» from the editor goes on
    cluster.pod = "ready"
    _ready(cleo, nid)
    pod.file = SOURCE + "# from marimo\n"
    assert cleo.post(f"/notebooks/{nid}/edit/save", json={}, headers=CSRF).json()["current_version"] == 3
    # closed: the page saves and restores again
    assert cleo.delete(f"/notebooks/{nid}/sessions/edit", headers=CSRF).status_code == 204
    assert ana.post(f"/notebooks/{nid}/versions/1/restore", headers=CSRF).json()["current_version"] == 4
    assert ana.patch(f"/notebooks/{nid}", json={"content": {"source": SOURCE + "# v5\n"}},
                     headers=CSRF).json()["current_version"] == 5
    # a failed editor holds nothing
    cluster.pod = "pull"
    cleo.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF)
    assert _ready(cleo, nid)[0]["status"] == "failed"
    assert ana.post(f"/notebooks/{nid}/versions/1/restore", headers=CSRF).status_code == 200


# ── the data ─────────────────────────────────────────────────────────────────
def test_the_notebook_reads_with_its_authors_rights_checked_at_every_read(app, session, cluster, engine):
    boss, ana, _, cleo, _, folder, nid = _setup(app, session)
    private = make_project(session, name="Private").id
    ana_id = session.exec(select(Notebook.owner_id).where(Notebook.id == nid)).one()
    make_permission(session, user_id=ana_id, project_id=private, capability="view")
    sales = make_datasource(session, name="sales", project_id=folder, key="datasets/sales.parquet", rows=3,
                            columns='[{"name": "region", "dtype": "str"}]').id
    secret = make_datasource(session, name="salaries", project_id=private, key="datasets/salaries.parquet").id
    make_datasource(session, name="sales", project_id=private, key="datasets/sales2.parquet")
    cleo.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)  # cleo opens ana's app
    cluster.pod = "ready"
    _ready(cleo, nid)
    bearer = {"Authorization": f"Bearer {_token(cluster)}"}
    pod_client = _pod_client(app)  # the pod: no cookie, only its token, from its address
    listed = pod_client.get("/notebook-data/datasources", headers=bearer).json()
    assert {d["path"] for d in listed} == {"Analyses/sales", "Private/salaries", "Private/sales"}
    assert next(d for d in listed if d["id"] == sales)["columns"] == ["region"]
    r = pod_client.post("/notebook-data/read", json={"datasource": "Private/salaries"}, headers=bearer)
    assert r.status_code == 200, r.text
    assert r.json() == {"url": "https://s3.example.com/datasets/salaries.parquet?sig=x", "id": secret,
                        "name": "salaries", "rows": None}
    assert engine.asked[-1] == {"bucket": "data-prep", "key": "datasets/salaries.parquet", "expires": "600"}
    assert pod_client.post("/notebook-data/read", json={"datasource": sales}, headers=bearer).json()["id"] == sales
    assert pod_client.post("/notebook-data/read", json={"datasource": "salaries"}, headers=bearer).json()["id"] == secret
    two = pod_client.post("/notebook-data/read", json={"datasource": "sales"}, headers=bearer)
    assert two.status_code == 409 and "Analyses/sales" in two.json()["detail"]
    reads = _actions(session, "notebook.read")
    assert len(reads) == 3 and reads[0].actor_label == "ana@x.local" and reads[0].target_label == "salaries"
    assert json.loads(reads[0].detail)["notebook"] == nid
    # the right taken away: the very next read fails
    for p in session.exec(select(Permission).where(Permission.project_id == private)).all():
        session.delete(p)
    session.commit()
    gone = pod_client.post("/notebook-data/read", json={"datasource": "Private/salaries"}, headers=bearer)
    assert gone.status_code == 404
    assert pod_client.post("/notebook-data/read", json={"datasource": secret}, headers=bearer).status_code == 404


def test_the_notebook_token_holds_only_on_the_data_and_only_while_its_session_lives(app, session, cluster, engine):
    _, ana, _, _, _, folder, nid = _setup(app, session)
    make_datasource(session, name="sales", project_id=folder, key="datasets/sales.parquet")
    ana.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)
    bearer = {"Authorization": f"Bearer {_token(cluster)}"}
    pod_client = _pod_client(app)
    # not before its pod answers, nor from anywhere but the pod
    assert pod_client.get("/notebook-data/datasources", headers=bearer).status_code == 401
    cluster.pod = "ready"
    _ready(ana, nid)
    assert pod_client.get("/notebook-data/datasources", headers=bearer).status_code == 200
    elsewhere = TestClient(app, client=("10.1.2.3", 50000)).get("/notebook-data/datasources", headers=bearer)
    assert elsewhere.status_code == 401 and "its own pod" in elsewhere.json()["detail"]
    # a forwarding header is refused even from the pod's address: behind uvicorn's
    # --proxy-headers any pod of the cluster could claim the notebook's address with it
    for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP"):
        spoofed = pod_client.get("/notebook-data/datasources", headers={**bearer, header: "127.0.0.1"})
        assert spoofed.status_code == 401, header
    # anywhere else it is nobody
    assert pod_client.get("/auth/me", headers=bearer).status_code == 401
    assert pod_client.get("/notebooks", headers=bearer).status_code == 401
    assert pod_client.get(f"/notebooks/{nid}", headers=bearer).status_code == 401
    assert pod_client.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=bearer).status_code == 401
    # and a person's session is nothing on the data endpoints
    assert ana.get("/notebook-data/datasources").status_code == 401
    # the session ends: the token with it
    assert ana.delete(f"/notebooks/{nid}/sessions/app", headers=CSRF).status_code == 204
    r = pod_client.post("/notebook-data/read", json={"datasource": "sales"}, headers=bearer)
    assert r.status_code == 401 and "ended" in r.json()["detail"]


# ── the proxy ────────────────────────────────────────────────────────────────
def test_the_proxy_starts_the_app_then_serves_marimo(app, session, cluster, pod):
    _, ana, ben, cleo, _, _, nid = _setup(app, session)
    base = f"/notebooks/{nid}/app"
    page = cleo.get(base + "/")
    assert page.status_code == 200 and 'http-equiv="refresh"' in page.text and "Starting" in page.text
    assert _row(session, nid, "app").status == "starting"
    assert cleo.get(base + "/assets/index.js").status_code == 503  # nothing but the root while it starts
    cluster.pod = "ready"
    runtime._checked.clear()
    r = cleo.get(base + "/")
    assert r.status_code == 200 and r.json()["marimo"] == f"/notebooks/{nid}/app/"
    assert r.headers["x-frame-options"] == "SAMEORIGIN" and "set-cookie" not in r.headers
    assert r.headers["content-security-policy"] == "default-src 'self'; frame-ancestors 'self'"
    r = cleo.get(base + "/assets/index.js?v=2")
    assert r.json()["marimo"] == f"/notebooks/{nid}/app/assets/index.js" and pod.requests[-1]["query"] == "v=2"
    seen = pod.requests[-1]["headers"]
    assert "cookie" not in seen and "x-tabularia-csrf" not in seen
    # what is not a GET comes from Tabularia's own pages only
    assert cleo.post(base + "/api/kernel/run", content=b"{}").status_code == 403
    assert cleo.post(base + "/api/kernel/run", content=b"{}", headers={"Origin": "https://evil.example"}).status_code == 403
    r = cleo.post(base + "/api/kernel/run", content=b'{"x": 1}', headers=ORIGIN)
    assert r.status_code == 200 and r.json()["body"] == '{"x": 1}'
    # nobody signed in, or somebody who does not see the notebook
    assert TestClient(app).get(base + "/").status_code == 401
    assert ben.get(base + "/").status_code == 404
    assert cleo.get(base, follow_redirects=False).headers["location"] == "app/"


def test_the_proxy_carries_the_websocket(app, session, cluster, pod):
    _, ana, _, cleo, _, _, nid = _setup(app, session)
    cleo.get(f"/notebooks/{nid}/app/")
    cluster.pod = "ready"
    _ready(cleo, nid)
    with cleo.websocket_connect(f"/notebooks/{nid}/app/ws?session_id=s1", headers=ORIGIN) as ws:
        assert ws.receive_text() == f"hello /notebooks/{nid}/app/ws?session_id=s1"
        ws.send_text("run cell")
        assert ws.receive_text() == "echo run cell"
        ws.send_text("bye")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    # without Tabularia's origin, or without a session: refused
    for headers, client in (({}, cleo), ({"Origin": "https://evil.example"}, cleo), (ORIGIN, TestClient(app))):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/notebooks/{nid}/app/ws", headers=headers) as ws:
                ws.receive_text()


def test_the_editor_is_only_for_the_person_editing(app, session, cluster, pod):
    _, ana, _, cleo, _, _, nid = _setup(app, session)
    assert ana.get(f"/notebooks/{nid}/edit/").status_code == 200  # starts ana's editor
    assert _row(session, nid, "edit").user_id is not None
    cluster.pod = "ready"
    _ready(ana, nid)
    assert ana.get(f"/notebooks/{nid}/edit/").json()["marimo"] == f"/notebooks/{nid}/edit/"
    busy = cleo.get(f"/notebooks/{nid}/edit/")
    assert busy.status_code == 409 and "being edited by ana@x.local" in busy.text
    assert cleo.get(f"/notebooks/{nid}/edit/assets/x.js").status_code == 409
    with pytest.raises(WebSocketDisconnect):
        with cleo.websocket_connect(f"/notebooks/{nid}/edit/ws", headers=ORIGIN) as ws:
            ws.receive_text()


def test_a_failed_start_shows_why_in_the_frame(app, session, cluster):
    _, ana, _, _, _, _, nid = _setup(app, session)
    ana.get(f"/notebooks/{nid}/app/")
    cluster.pod = "pull"
    _ready(ana, nid)
    page = ana.get(f"/notebooks/{nid}/app/")
    assert page.status_code == 502 and "could not download the image" in page.text and "refresh" not in page.text


# ── the reaper ───────────────────────────────────────────────────────────────
def _reap(db_engine):
    import asyncio

    runtime._checked.clear()
    asyncio.run(runtime.one_round(db_engine))


def test_the_reaper_stops_idle_pods_and_keeps_the_editors_file(app, session, cluster, pod, db_engine, monkeypatch):
    _, ana, _, cleo, env_id, folder, nid = _setup(app, session)
    other = _create(ana, folder, env_id, name="Other").json()["id"]
    ana.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF)
    cleo.post(f"/notebooks/{other}/sessions", json={"mode": "app"}, headers=CSRF)
    cluster.pod = "ready"
    _reap(db_engine)  # both become ready
    assert {r.status for r in session.exec(select(NotebookSession)).all()} == {"ready"}
    # every round keeps the editor's file as the draft
    pod.file = SOURCE + "\n# unsaved\n"
    _reap(db_engine)
    session.expire_all()
    assert session.get(Notebook, nid).draft == pod.file
    # the app idle past the limit: stopped; the editor used a moment ago stays
    row = _row(session, other, "app")
    row.last_seen_at = row.last_seen_at - timedelta(minutes=16)
    session.add(row)
    session.commit()
    _reap(db_engine)
    session.expire_all()
    assert [r.mode for r in session.exec(select(NotebookSession)).all()] == ["edit"]
    assert [j["metadata"]["labels"]["tabularia.io/notebook-mode"] for j in cluster.jobs()] == ["edit"]
    (stopped,) = _actions(session, "notebook.stop")
    assert stopped.actor_label == "orchestrator" and json.loads(stopped.detail)["reason"] == "idle"
    # the editor's Job gone from the cluster: the session is forgotten
    cluster.objects.pop(("jobs", "tab-scripts", _row(session, nid, "edit").job_name))
    _reap(db_engine)
    assert session.exec(select(NotebookSession)).all() == []


def test_the_reaper_deletes_jobs_nobody_holds_and_fails_slow_starts(app, session, cluster, db_engine, monkeypatch):
    _, ana, _, _, env_id, folder, nid = _setup(app, session)
    ana.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)
    # a Job of a notebook whose session went (a gateway stopped between the two)
    cluster.objects[("jobs", "tab-scripts", "nb-99-app-abcdef")] = {
        "metadata": {"name": "nb-99-app-abcdef", "labels": runtime.labels(99, "app"), "uid": "u-99"}}
    row = _row(session, nid, "app")
    row.started_at = row.started_at - timedelta(seconds=301)
    session.add(row)
    session.commit()
    _reap(db_engine)
    assert cluster.jobs() == []
    failed = _row(session, nid, "app")
    assert failed.status == "failed" and "did not start within 300 s" in failed.error
    # a failed session is cleared after a while
    failed.started_at = failed.started_at - timedelta(minutes=20)
    session.add(failed)
    session.commit()
    _reap(db_engine)
    assert session.exec(select(NotebookSession)).all() == []


def test_deleting_the_notebook_or_its_reader_forgets_the_sessions(app, session, cluster):
    boss, ana, _, cleo, env_id, folder, nid = _setup(app, session)
    other = _create(ana, folder, env_id, name="Other").json()["id"]
    ana.post(f"/notebooks/{nid}/sessions", json={"mode": "app"}, headers=CSRF)
    cleo.post(f"/notebooks/{other}/sessions", json={"mode": "edit"}, headers=CSRF)
    assert ana.delete(f"/notebooks/{nid}", headers=CSRF).status_code == 204
    assert [r.notebook_id for r in session.exec(select(NotebookSession)).all()] == [other]
    from app.routes.users import remove_user

    remove_user(session, _row(session, other, "edit").user_id)
    assert session.exec(select(NotebookSession)).all() == []


# ── visitors ─────────────────────────────────────────────────────────────────
def _caps(monkeypatch, per_app=None, ceiling=None):
    if per_app is not None:
        monkeypatch.setenv("NOTEBOOKS__MAX_VISITORS_PER_APP", str(per_app))
    if ceiling is not None:
        monkeypatch.setenv("NOTEBOOKS__MAX_VISITORS_CEILING", str(ceiling))
    get_settings.cache_clear()


def test_visitors_are_counted_across_replicas(app, session, cluster, db_engine, monkeypatch):
    _, ana, _, cleo, _, _, nid = _setup(app, session)
    _caps(monkeypatch, per_app=2)
    users = [session.exec(select(Notebook.owner_id).where(Notebook.id == nid)).one(), 99]
    # two gateway replicas: two processes, each with its own connection to the database
    with Session(db_engine) as one, Session(db_engine) as two:
        first, n, cap = runtime.take_visitor(one, one.get(Notebook, nid), users[0])
        assert first is not None and (n, cap) == (1, 2)
        second, n, _ = runtime.take_visitor(two, two.get(Notebook, nid), users[1])
        assert second is not None and n == 2
        # the third, on either replica: full
        assert runtime.take_visitor(one, one.get(Notebook, nid), users[1])[:2] == (None, 2)
        assert runtime.take_visitor(two, two.get(Notebook, nid), users[0])[:2] == (None, 2)
        assert runtime.visitors(one, nid) == runtime.visitors(two, nid) == 2
        # one leaves on the first replica: the second takes the place
        runtime.drop_visitor(one, first)
        third, n, _ = runtime.take_visitor(two, two.get(Notebook, nid), users[0])
        assert third is not None and n == 2
        # a replica that died: its visitor, no longer refreshed, stops counting after three
        # missed beats, and the reaper clears it; the one kept fresh stays
        dead = two.get(NotebookVisitor, second)
        dead.last_seen_at = dead.last_seen_at - timedelta(minutes=4)
        two.add(dead)
        two.commit()
        live = two.get(NotebookVisitor, third)
        live.last_seen_at = live.last_seen_at - timedelta(minutes=2)
        two.add(live)
        two.commit()
        runtime.keep_visitor(one, third)
        assert runtime.visitors(one, nid) == 1
        assert runtime.take_visitor(one, one.get(Notebook, nid), users[1])[0] is not None
    _reap(db_engine)
    session.expire_all()
    assert second not in {v.id for v in session.exec(select(NotebookVisitor)).all()}
    assert len(session.exec(select(NotebookVisitor)).all()) == 2


def test_the_notebooks_own_cap_within_the_ceiling(app, session, cluster, monkeypatch):
    _, ana, _, cleo, env_id, folder, nid = _setup(app, session)
    n = ana.get(f"/notebooks/{nid}").json()
    assert (n["max_visitors"], n["visitors_default"], n["visitors_ceiling"]) == (None, 10, 50)
    notebook = session.get(Notebook, nid)
    assert runtime.visitors_cap(notebook) == 10
    r = cleo.patch(f"/notebooks/{nid}", json={"max_visitors": 3}, headers=CSRF)
    assert r.status_code == 200 and r.json()["max_visitors"] == 3
    session.expire_all()
    assert runtime.visitors_cap(session.get(Notebook, nid)) == 3
    (changed,) = _actions(session, "notebook.update")
    assert json.loads(changed.detail) == {"max_visitors": {"from": None, "to": 3}}
    # left out: unchanged; past the ceiling or below one: refused
    assert ana.patch(f"/notebooks/{nid}", json={"description": "x"}, headers=CSRF).json()["max_visitors"] == 3
    over = ana.patch(f"/notebooks/{nid}", json={"max_visitors": 51}, headers=CSRF)
    assert over.status_code == 422 and "NOTEBOOKS__MAX_VISITORS_CEILING" in over.json()["detail"]
    assert ana.patch(f"/notebooks/{nid}", json={"max_visitors": 0}, headers=CSRF).status_code == 422
    assert ana.get(f"/notebooks/{nid}").json()["max_visitors"] == 3
    # only with EDIT
    dan, dan_id = _login(app, session, "dan@x.local")
    from app.models import Group, UserGroupLink

    session.add(UserGroupLink(user_id=dan_id, group_id=session.exec(select(Group.id)).one()))
    session.commit()
    make_permission(session, user_id=dan_id, project_id=folder, capability="view")
    assert dan.patch(f"/notebooks/{nid}", json={"max_visitors": 40}, headers=CSRF).status_code == 403
    # a duplicate keeps it; null goes back to the installation's default
    copy = ana.post(f"/notebooks/{nid}/duplicate", headers=CSRF).json()
    assert copy["max_visitors"] == 3
    assert ana.patch(f"/notebooks/{nid}", json={"max_visitors": None}, headers=CSRF).json()["max_visitors"] is None
    # the installation's numbers: the default, and a ceiling lowered under a notebook's own
    _caps(monkeypatch, per_app=4, ceiling=2)
    session.expire_all()
    assert runtime.visitors_cap(session.get(Notebook, nid)) == 2  # the default above the ceiling: the ceiling
    n = ana.get(f"/notebooks/{nid}").json()
    assert (n["visitors_default"], n["visitors_ceiling"]) == (2, 2)
    assert runtime.visitors_cap(session.get(Notebook, copy["id"])) == 2
    assert ana.patch(f"/notebooks/{nid}", json={"max_visitors": 3}, headers=CSRF).status_code == 422
    # set when created, within the ceiling too
    r = ana.post(f"/projects/{folder}/notebooks", json={"name": "Small", "environment_id": env_id, "max_visitors": 1},
                 headers=CSRF)
    assert r.status_code == 201 and r.json()["max_visitors"] == 1
    assert ana.post(f"/projects/{folder}/notebooks", json={"name": "Big", "environment_id": env_id, "max_visitors": 5},
                    headers=CSRF).status_code == 422


def test_past_the_cap_the_app_is_full_and_says_so(app, session, cluster, pod, monkeypatch):
    from app.routes import notebook_proxy

    monkeypatch.setattr(notebook_proxy, "TAKE_TRIES", 2)
    monkeypatch.setattr(notebook_proxy, "TAKE_PAUSE", 0.01)
    _, ana, _, cleo, _, _, nid = _setup(app, session)
    assert ana.patch(f"/notebooks/{nid}", json={"max_visitors": 1}, headers=CSRF).status_code == 200
    cleo.get(f"/notebooks/{nid}/app/")
    cluster.pod = "ready"
    (live,) = _ready(cleo, nid)
    assert (live["visitors"], live["max_visitors"]) == (0, 1)
    base = f"/notebooks/{nid}/app"
    with ana.websocket_connect(base + "/ws?session_id=a", headers=ORIGIN) as ws:
        assert ws.receive_text().startswith("hello")
        (live,) = cleo.get(f"/notebooks/{nid}/sessions").json()
        assert live["visitors"] == 1
        # anyone else: a page that says so and reloads itself
        page = cleo.get(base + "/")
        assert page.status_code == 503 and page.headers["retry-after"] == "10"
        assert '<meta http-equiv="refresh" content="10">' in page.text and 'role="status"' in page.text
        assert "This app already has 1 person connected, the most it allows." in page.text
        assert "It will open by itself as soon as someone leaves" in page.text
        assert page.headers["x-frame-options"] == "SAMEORIGIN"
        # the person who has it open reloads: through
        assert ana.get(base + "/").json()["marimo"] == f"/notebooks/{nid}/app/"
        # the rest of marimo's requests are not the root: never turned away
        assert cleo.get(base + "/assets/index.js").status_code == 200
        # a WebSocket that loses the race for the last place: closed, saying why
        with cleo.websocket_connect(base + "/ws?session_id=c", headers=ORIGIN) as refused:
            with pytest.raises(WebSocketDisconnect) as closed:
                refused.receive_text()
        assert closed.value.code == 1013
        assert closed.value.reason == "This app already has 1 person connected, the most it allows"
        # the editor has no cap, and holds no place
        assert ana.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF).status_code == 202
        _ready(ana, nid)
        with ana.websocket_connect(f"/notebooks/{nid}/edit/ws", headers=ORIGIN) as editor:
            assert editor.receive_text() == f"hello /notebooks/{nid}/edit/ws?"
            assert runtime.visitors(session, nid) == 1
    # ana left: her place is free
    session.expire_all()
    assert session.exec(select(NotebookVisitor)).all() == []
    assert cleo.get(base + "/").json()["marimo"] == f"/notebooks/{nid}/app/"
    with cleo.websocket_connect(base + "/ws?session_id=c", headers=ORIGIN) as ws:
        assert ws.receive_text().startswith("hello")
        assert runtime.visitors(session, nid) == 1


def test_an_open_app_keeps_its_place_and_frees_it_when_taken_away(app, session, cluster, pod, db_engine,
                                                                     monkeypatch):
    """The minute's check refreshes the visitor; a person no longer allowed loses the
    WebSocket and the place."""
    from app.routes import notebook_proxy

    monkeypatch.setattr(notebook_proxy, "WS_CHECK_SECONDS", 0.05)
    _, ana, _, cleo, _, folder, nid = _setup(app, session)
    cleo.get(f"/notebooks/{nid}/app/")
    cluster.pod = "ready"
    _ready(cleo, nid)
    with cleo.websocket_connect(f"/notebooks/{nid}/app/ws", headers=ORIGIN) as ws:
        ws.receive_text()
        # the proxy's own session is the request's: the test looks with another one
        with Session(db_engine) as look:
            (visitor,) = look.exec(select(NotebookVisitor)).all()
            visitor.last_seen_at = visitor.last_seen_at - timedelta(minutes=2)
            look.add(visitor)
            look.commit()
            vid = visitor.id
            for _ in range(100):
                look.expire_all()
                if runtime._age(look.get(NotebookVisitor, vid).last_seen_at) < 60:
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("the visitor was not refreshed")
            # cleo may no longer open the folder: the next check closes the socket
            for p in look.exec(select(Permission).where(Permission.project_id == folder)).all():
                look.delete(p)
            look.commit()
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(200):
                ws.receive_text()
        assert closed.value.code == 1008
    with Session(db_engine) as look:
        assert look.exec(select(NotebookVisitor)).all() == []


def test_visitors_go_with_their_notebook_and_their_account(app, session, cluster):
    _, ana, _, cleo, env_id, folder, nid = _setup(app, session)
    other = _create(ana, folder, env_id, name="Other").json()["id"]
    ana_id = session.exec(select(Notebook.owner_id).where(Notebook.id == nid)).one()
    from app.models import User

    cleo_id = session.exec(select(User.id).where(User.email == "cleo@x.local")).one()
    session.add_all([NotebookVisitor(notebook_id=nid, user_id=cleo_id), NotebookVisitor(notebook_id=other, user_id=ana_id),
                     NotebookVisitor(notebook_id=other, user_id=cleo_id)])
    session.commit()
    assert ana.delete(f"/notebooks/{nid}", headers=CSRF).status_code == 204
    assert {(v.notebook_id, v.user_id) for v in session.exec(select(NotebookVisitor)).all()} == {
        (other, ana_id), (other, cleo_id)}
    from app.routes.users import remove_user

    remove_user(session, cleo_id)
    assert [(v.notebook_id, v.user_id) for v in session.exec(select(NotebookVisitor)).all()] == [(other, ana_id)]


def test_visitors_on_a_database_that_enforces_foreign_keys(fk_session):
    from app.routes.users import remove_user

    owner, guest = make_user(fk_session, email="owner@x.local"), make_user(fk_session, email="guest@x.local")
    folder = make_project(fk_session, name="F")
    env = ScriptEnvironment(name="py", language="python")
    fk_session.add(env)
    fk_session.commit()
    notebook = Notebook(name="N", project_id=folder.id, owner_id=owner.id, environment_id=env.id, max_visitors=2)
    fk_session.add(notebook)
    fk_session.commit()
    fk_session.add_all([NotebookVisitor(notebook_id=notebook.id, user_id=guest.id),
                        NotebookVisitor(notebook_id=notebook.id, user_id=owner.id)])
    fk_session.commit()
    remove_user(fk_session, guest.id)
    assert [v.user_id for v in fk_session.exec(select(NotebookVisitor)).all()] == [owner.id]
    runtime.forget(fk_session, notebook.id)
    fk_session.delete(notebook)
    fk_session.commit()
    assert fk_session.exec(select(NotebookVisitor)).all() == []
    assert fk_session.get(Project, folder.id) is not None


def test_two_replicas_racing_for_the_last_place_on_postgres(monkeypatch):
    """The lock of the notebook's visitors, on a real Postgres: many replicas asking at
    the same moment for the last place, one gets it."""
    from concurrent.futures import ThreadPoolExecutor

    from sqlmodel import SQLModel, create_engine

    from tests.test_migrations import _url_base

    base = _url_base()
    if base is None:
        pytest.skip("serve un Postgres (DB__*)")
    from sqlalchemy import text as sql

    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"zzvis_{time.time_ns() % 10 ** 10}"
    try:
        with admin.connect() as c:
            c.execute(sql(f'CREATE DATABASE "{name}"'))
    except Exception as e:  # noqa: BLE001 — no reachable Postgres: skipped
        pytest.skip(f"Postgres non raggiungibile ({e.__class__.__name__})")
    db = create_engine(base.set(database=name), pool_size=10)
    try:
        SQLModel.metadata.create_all(db)
        with Session(db) as s:
            owner = make_user(s, email="owner@x.local")
            folder = make_project(s, name="F")
            env = ScriptEnvironment(name="py", language="python")
            s.add(env)
            s.commit()
            notebook = Notebook(name="N", project_id=folder.id, owner_id=owner.id, environment_id=env.id, max_visitors=3)
            s.add(notebook)
            s.commit()
            nid, uid = notebook.id, owner.id
            runtime.take_visitor(s, s.get(Notebook, nid), uid)
            runtime.take_visitor(s, s.get(Notebook, nid), uid)

        def one(_):
            with Session(db) as replica:
                return runtime.take_visitor(replica, replica.get(Notebook, nid), uid)[0]

        with ThreadPoolExecutor(8) as pool:
            got = [v for v in pool.map(one, range(8)) if v is not None]
        assert len(got) == 1
        with Session(db) as s:
            assert runtime.visitors(s, nid) == 3
    finally:
        db.dispose()
        with admin.connect() as c:
            c.execute(sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


# ── the proxy and the pod, hardened (bug hunt 2026-10-28) ────────────────────
def _ready_app(app, session, cluster):
    _, ana, _, cleo, _, _, nid = _setup(app, session)
    cleo.get(f"/notebooks/{nid}/app/")
    cluster.pod = "ready"
    _ready(cleo, nid)
    return ana, cleo, nid


def test_the_app_takes_visitors_only_through_its_websocket(app, session, cluster, pod):
    """marimo opens a kernel for every `…/sse?session_id=` too (its server-sent events,
    an alternative to the WebSocket its page does not use here): through the proxy it
    was a kernel, and a copy of the data, outside the cap of visitors. And httpx removes
    the dot segments of a path, so `x/../sse` reached it as well."""
    ana, cleo, nid = _ready_app(app, session, cluster)
    base = f"/notebooks/{nid}/app"
    for path in ("sse?session_id=s1", "x/%2e%2e/sse?session_id=s2", "%2e/sse"):
        r = cleo.get(f"{base}/{path}")
        assert r.status_code == 404, path
    assert not [p for p in pod.requests if p["path"].endswith("/sse")]
    # a path with dot segments reaches nothing, in either mode; the rest goes on
    assert cleo.get(f"{base}/assets/%2e%2e/%2e%2e/edit/").status_code == 404
    assert cleo.get(f"{base}/assets/index.js").status_code == 200
    with pytest.raises(WebSocketDisconnect) as closed:
        with cleo.websocket_connect(f"{base}/x/%2e%2e/ws", headers=ORIGIN) as ws:
            ws.receive_text()
    assert closed.value.code == 1008
    assert runtime.visitors(session, nid) == 0


def test_the_pod_is_reached_directly_never_through_the_environments_proxy(app, session, cluster, pod, monkeypatch):
    """A gateway with HTTP_PROXY (to reach an AI provider, say) must still reach the pods
    at their address: through the proxy they are not there."""
    ana, cleo, nid = _ready_app(app, session, cluster)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing answers there
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    r = cleo.get(f"/notebooks/{nid}/app/")
    assert r.status_code == 200 and r.json()["marimo"] == f"/notebooks/{nid}/app/"
    with cleo.websocket_connect(f"/notebooks/{nid}/app/ws?session_id=a", headers=ORIGIN) as ws:
        assert ws.receive_text().startswith("hello")
    # the editor's file too («Save version», the draft)
    cleo.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF)
    _ready(cleo, nid)
    pod.file = SOURCE + "# by proxy\n"
    r = cleo.post(f"/notebooks/{nid}/edit/save", json={}, headers=CSRF)
    assert r.status_code == 200 and r.json()["current_version"] == 2, r.text


def test_a_proxied_request_loads_no_certificates(app, session, cluster, pod, monkeypatch):
    """httpx loads the system's certificates for every client it makes: ~45 ms of the event
    loop, at every request of marimo's page (dozens of files), for plain HTTP to the pod."""
    import ssl

    _, cleo, nid = _ready_app(app, session, cluster)
    loaded = []
    real = ssl.SSLContext.load_verify_locations
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations",
                        lambda self, *a, **k: loaded.append(a or k) or real(self, *a, **k))
    for _ in range(3):
        assert cleo.get(f"/notebooks/{nid}/app/assets/index.js").status_code == 200
    assert loaded == []


def test_a_message_too_large_from_the_pod_closes_its_websocket(app, session, cluster, pod, monkeypatch):
    """What the pod sends is held whole in the gateway's memory before it goes on: past
    the bound the socket closes (1009) instead of the gateway taking gigabytes."""
    from app.routes import notebook_proxy

    monkeypatch.setattr(notebook_proxy, "MAX_MESSAGE_BYTES", 1024)
    _, cleo, nid = _ready_app(app, session, cluster)
    with cleo.websocket_connect(f"/notebooks/{nid}/app/ws", headers=ORIGIN) as ws:
        ws.receive_text()
        ws.send_text("big 1000")
        assert len(ws.receive_text()) == 1000
        ws.send_text("big 5000")
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1009


def test_an_answer_broken_halfway_closes_the_connection_to_the_pod(app, session, cluster, pod, monkeypatch):
    """The pod going away in the middle of an answer: the gateway's client to it is closed
    (Starlette skips the response's background task when its stream fails)."""
    made: list[httpx.AsyncClient] = []

    class Recording(httpx.AsyncClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    _, cleo, nid = _ready_app(app, session, cluster)
    monkeypatch.setattr(httpx, "AsyncClient", Recording)
    with pytest.raises(httpx.HTTPError):
        cleo.get(f"/notebooks/{nid}/app/broken")
    assert made and all(c.is_closed for c in made)


# ── the module in the pod ────────────────────────────────────────────────────
@pytest.fixture
def pod_module(monkeypatch):
    """app/notebook_pod/tabularia.py as the pod imports it, talking to a gateway on this
    machine; polars is not in the gateway's image: a stand-in that reads the bytes."""
    import importlib.util
    import sys
    import types
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[dict] = []

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            seen.append({"path": self.path, "authorization": self.headers.get("authorization"),
                         "forwarded": self.headers.get("x-forwarded-for")})
            if self.path == "/notebook-data/datasources":
                return self._json([{"id": 1, "name": "sales"}])
            if self.path == "/slow.parquet":  # headers, half the file, then nothing
                self.send_response(200)
                self.send_header("content-length", "100")
                self.end_headers()
                self.wfile.write(b"x" * 10)
                self.wfile.flush()
                time.sleep(3)
                return None
            self.send_response(404)
            self.end_headers()
            return None

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            seen.append({"path": self.path, "authorization": self.headers.get("authorization")})
            port = self.server.server_address[1]
            return self._json({"url": f"http://127.0.0.1:{port}/slow.parquet", "id": 1, "name": "sales"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TABULARIA_URL", f"http://127.0.0.1:{server.server_address[1]}/")
    monkeypatch.setenv("TABULARIA_NOTEBOOK_TOKEN", "nbk_test")
    monkeypatch.setitem(sys.modules, "polars", types.SimpleNamespace(
        read_parquet=lambda path, columns=None: open(path, "rb").read()))
    spec = importlib.util.spec_from_file_location("tabularia_in_the_pod", runtime.POD_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seen = seen
    yield module
    server.shutdown()
    thread.join(5)


def test_the_pod_calls_the_gateway_directly_never_through_a_proxy(pod_module, monkeypatch):
    """The environment's variables reach the pod: an HTTP_PROXY there (for the internet)
    sent the calls to the gateway through the proxy, and the gateway refuses a call that
    does not come from the pod's own address ("holds only from its own pod")."""
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing answers there
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert pod_module.datasources() == [{"id": 1, "name": "sales"}]
    assert pod_module.seen[-1]["authorization"] == "Bearer nbk_test"


def test_a_download_that_stalls_fails_instead_of_hanging_the_cell(pod_module, monkeypatch):
    monkeypatch.setattr(pod_module, "_DOWNLOAD_TIMEOUT", 0.5)
    started = time.monotonic()
    with pytest.raises(pod_module.TabulariaError, match="could not be downloaded"):
        pod_module.read("sales")
    assert time.monotonic() - started < 2.5
