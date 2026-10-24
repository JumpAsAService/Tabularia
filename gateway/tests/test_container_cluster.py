"""The cluster's side of the container registries (services/container_cluster.py).

Fixed here:
- a registry's pull Secret holds its credentials as they are when a Job needs it, is
  replaced when they change, removed when the registry has none, and goes with the
  registry;
- «Try in the cluster» resolves the tag to a digest, runs a Pod with the image and a
  command that does not exist, in the trials' namespace, and says whether a node
  downloaded it; a download that keeps failing is explained, not waited for; the Job
  never stays behind, but for a download still going when the request must answer
  (under the ingress' 300 s), which is said and left to finish;
- the check is an administrator's, recorded in the audit, and only for allowed
  repositories.
"""
import base64
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.db.session import get_session
from app.models import AuditLog, ContainerRegistry
from app.routes import registries as routes
from app.services import container_cluster, container_registry as cr, kube_jobs
from tests.fake_kube import Cluster
from tests.test_registries import CSRF, DIGEST, PASSWORD, TOKEN, FakeRegistry
from tests.conftest import make_user


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    c = Cluster()
    (tmp_path / "token").write_text("sa-token\n")
    for k, v in {"KUBERNETES__TOKEN_FILE": str(tmp_path / "token"), "KUBERNETES__CA_FILE": "",
                 "CONTAINERS__ENABLED": "true", "CONTAINERS__NAMESPACE": "tab-containers",
                 "CONTAINERS__TRIAL_NAMESPACE": "tab-containers-dev", "CONTAINERS__POLL_SECONDS": "0.01"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    monkeypatch.setattr(kube_jobs, "_transport", httpx.MockTransport(c.handle))
    monkeypatch.setattr(container_cluster, "PULL_FAILURE_SECONDS", 0)
    yield c
    get_settings.cache_clear()


@pytest.fixture
def registry(monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(cr, "_transport", httpx.MockTransport(fake.handle))
    return fake


@pytest.fixture
def app(session):
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _boss(app, session) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email="boss@x.local", is_superuser=True, hashed_password=hash_password(PASSWORD))
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": "boss@x.local", "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def _create(c, **kw) -> int:
    r = c.post("/admin/registries", json={"name": "Acme", "host": "reg.test", "username": "ada", "password": TOKEN,
                                          "repositories": ["team/*"], **kw}, headers=CSRF)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _secret(cluster, ns, rid) -> dict:
    obj = cluster.objects[("secrets", ns, f"tabularia-registry-{rid}")]
    return json.loads(base64.b64decode(obj["data"][".dockerconfigjson"]))


def test_a_node_downloads_the_image_and_runs_nothing(app, session, cluster, registry):
    boss = _boss(app, session)
    rid = _create(boss)

    def pulled(job):
        cluster.events[job["metadata"]["name"] + "-pod"] = [{"reason": "Pulled", "message": "Successfully pulled image"}]
        return None, "", None  # the container never starts: its command does not exist
    cluster.behave = pulled
    out = boss.post(f"/admin/registries/{rid}/cluster-test", json={"repository": "team/forecast", "reference": "v3"},
                    headers=CSRF).json()
    assert out["ok"] is True and out["image"] == f"reg.test/team/forecast@{DIGEST}"
    job = next(body for kind, ns, body in cluster.created if kind == "jobs")
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    # by digest, with a command that exists nowhere, in the trials' namespace
    assert c["image"] == out["image"] and c["command"] == [container_cluster.MISSING_COMMAND]
    assert c["imagePullPolicy"] == "Always" and pod["automountServiceAccountToken"] is False
    assert pod["imagePullSecrets"] == [{"name": f"tabularia-registry-{rid}"}]
    assert [ns for kind, ns, _ in cluster.created if kind == "jobs"] == ["tab-containers-dev"]
    # the pull Secret: the registry's credentials, under its host
    auth = _secret(cluster, "tab-containers-dev", rid)["auths"]["reg.test"]
    assert auth["username"] == "ada" and auth["password"] == TOKEN
    assert cluster.jobs() == []  # the Job never stays
    entry = session.exec(select(AuditLog).where(AuditLog.action == "registry.cluster_test")).one()
    assert entry.outcome == "success" and TOKEN not in (entry.detail or "")


@pytest.mark.parametrize("message, expected", [
    ("failed to pull: pull access denied, insufficient_scope: authorization failed", "refused the cluster's credentials"),
    ("tls: failed to verify certificate: x509: certificate signed by unknown authority", "nodes do not trust"),
    ("http: server gave HTTP response to HTTPS client", "plain HTTP"),
])
def test_a_download_that_fails_is_explained(app, session, cluster, registry, message, expected):
    boss = _boss(app, session)
    rid = _create(boss)
    cluster.behave = lambda job: (None, "", None)
    cluster.pull = ("ErrImagePull", message, [])
    out = boss.post(f"/admin/registries/{rid}/cluster-test", json={"repository": "team/forecast", "reference": "v1"},
                    headers=CSRF).json()
    assert out["ok"] is False and expected in out["message"]
    assert cluster.jobs() == []


def test_a_download_longer_than_the_request_is_said_and_left_to_finish(app, session, cluster, registry, monkeypatch):
    # CONTAINERS__PULL_CHECK_SECONDS is 300 by default and the ingress cuts a request at
    # 300 s: the check answers before, saying the node is still downloading
    monkeypatch.setattr(container_cluster, "REQUEST_WAIT_SECONDS", 0.05)
    boss = _boss(app, session)
    rid = _create(boss)
    cluster.behave = lambda job: (None, "", None)  # pulling: no event, no failure
    out = boss.post(f"/admin/registries/{rid}/cluster-test", json={"repository": "team/forecast", "reference": "v3"},
                    headers=CSRF).json()
    assert out["ok"] is False and "still downloading" in out["message"] and "up to 300 s" in out["message"]
    assert len(cluster.jobs()) == 1  # the node goes on downloading: its deadline and TTL end it
    job = next(body for kind, ns, body in cluster.created if kind == "jobs")
    assert job["spec"]["activeDeadlineSeconds"] == 300 and job["spec"]["ttlSecondsAfterFinished"] > 0


def test_a_check_the_cluster_refuses_says_why(app, session, cluster, registry, monkeypatch):
    # the trials' quota full: no Pod, nothing downloading; it said «still downloading»
    monkeypatch.setattr(container_cluster, "NO_POD_SECONDS", 0)
    boss = _boss(app, session)
    rid = _create(boss)
    cluster.behave = lambda job: (None, "", None)
    real = cluster.handle

    def quota_full(req):
        if req.url.path.endswith("/pods"):
            return httpx.Response(200, json={"items": []})
        if req.url.path.endswith("/events"):
            return httpx.Response(200, json={"items": [{"reason": "FailedCreate", "message":
                'Error creating: pods "x" is forbidden: exceeded quota: tabularia-container-trials, requested: '
                "pods=1, used: pods=10, limited: pods=10"}]})
        return real(req)

    monkeypatch.setattr(kube_jobs, "_transport", httpx.MockTransport(quota_full))
    out = boss.post(f"/admin/registries/{rid}/cluster-test", json={"repository": "team/forecast", "reference": "v3"},
                    headers=CSRF).json()
    assert out["ok"] is False and "ResourceQuota is full" in out["message"] and cluster.jobs() == []


def test_what_the_gateway_cannot_resolve_is_said_before_the_cluster(app, session, cluster, registry):
    boss = _boss(app, session)
    rid = _create(boss)
    out = boss.post(f"/admin/registries/{rid}/cluster-test", json={"repository": "team/forecast", "reference": "v99"},
                    headers=CSRF).json()
    assert out["ok"] is False and "no tag «v99»" in out["message"] and cluster.created == []
    assert boss.post(f"/admin/registries/{rid}/cluster-test", json={"repository": "other/x"},
                     headers=CSRF).status_code == 403


@pytest.mark.anyio
async def test_the_pull_secret_follows_the_credentials(session, cluster):
    r = ContainerRegistry(id=7, name="Hub", host="docker.io", username="ada")
    assert await container_cluster.sync_pull_secret(r, "first", "tab-containers") == "tabularia-registry-7"
    hub = _secret(cluster, "tab-containers", 7)["auths"]
    assert list(hub) == ["https://index.docker.io/v1/"] and hub["https://index.docker.io/v1/"]["password"] == "first"
    await container_cluster.sync_pull_secret(r, "second", "tab-containers")  # replaced, not a second one
    assert _secret(cluster, "tab-containers", 7)["auths"]["https://index.docker.io/v1/"]["password"] == "second"
    r.username = ""  # anonymous now: no Secret left behind
    assert await container_cluster.sync_pull_secret(r, "", "tab-containers") is None
    assert ("secrets", "tab-containers", "tabularia-registry-7") not in cluster.objects


def test_a_deleted_registry_takes_its_pull_secrets(app, session, cluster, registry):
    import asyncio

    boss = _boss(app, session)
    rid = _create(boss)
    reg = session.get(ContainerRegistry, rid)
    for ns in ("tab-containers", "tab-containers-dev"):
        asyncio.run(container_cluster.sync_pull_secret(reg, TOKEN, ns))
    assert boss.delete(f"/admin/registries/{rid}", headers=CSRF).status_code == 204
    assert not [k for k in cluster.objects if k[0] == "secrets"]


class _RacingCluster(httpx.AsyncBaseTransport):
    """Secrets only, as the API server keeps them: a PUT with a stale resourceVersion is
    refused with 409, and other requests come in between."""

    def __init__(self):
        self.secrets: dict[str, dict] = {}
        self.rv, self.puts = 0, 0

    async def handle_async_request(self, req):
        import asyncio

        await asyncio.sleep(0.01)
        name = req.url.path.rstrip("/").split("/")[-1]
        if req.method == "POST":
            body = json.loads(req.content)
            if body["metadata"]["name"] in self.secrets:
                return httpx.Response(409, json={"message": "already exists"})
            self.rv += 1
            self.secrets[body["metadata"]["name"]] = {**body, "metadata": {**body["metadata"],
                                                                           "resourceVersion": str(self.rv)}}
            return httpx.Response(201, json=body)
        if req.method == "GET":
            return httpx.Response(200, json=self.secrets[name])
        body = json.loads(req.content)  # PUT
        self.puts += 1
        if body["metadata"].get("resourceVersion") != self.secrets[name]["metadata"]["resourceVersion"]:
            return httpx.Response(409, json={"message": "the object has been modified"})
        self.rv += 1
        self.secrets[name] = {**body, "metadata": {**body["metadata"], "resourceVersion": str(self.rv)}}
        return httpx.Response(200, json=self.secrets[name])


@pytest.mark.anyio
@pytest.mark.parametrize("password", ["tok", "new-token"])
async def test_two_runs_of_one_registry_write_its_pull_secret_at_once(cluster, monkeypatch, password):
    # parallel Container cells of a flow group: one of the two failed with 409
    import asyncio

    racing = _RacingCluster()
    monkeypatch.setattr(kube_jobs, "_transport", racing)
    reg = ContainerRegistry(id=7, name="Acme", host="reg.test", username="ada")
    await container_cluster.sync_pull_secret(reg, "tok", "tab-containers")  # an earlier run wrote it
    results = await asyncio.gather(*[container_cluster.sync_pull_secret(reg, password, "tab-containers")
                                     for _ in range(2)], return_exceptions=True)
    assert results == ["tabularia-registry-7", "tabularia-registry-7"]
    auth = json.loads(base64.b64decode(racing.secrets["tabularia-registry-7"]["data"][".dockerconfigjson"]))
    assert auth["auths"]["reg.test"]["password"] == password
    if password == "tok":
        assert racing.puts == 0  # unchanged credentials: nothing rewritten
