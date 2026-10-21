"""The cluster's side of the container registries (services/container_cluster.py).

Fixed here:
- a registry's pull Secret holds its credentials as they are when a Job needs it, is
  replaced when they change, removed when the registry has none, and goes with the
  registry;
- «Try in the cluster» resolves the tag to a digest, runs a Pod with the image and a
  command that does not exist, in the trials' namespace, and says whether a node
  downloaded it; a download that keeps failing is explained, not waited for; the Job
  never stays behind;
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
