"""Container registries (Administration → Container registries): docs/specs/container-node.md.

Fixed here:
- the panel: Kubernetes only, written by administrators only (never with an API token),
  read by observers; the password and the secret variables never leave, nor reach the audit;
- what is saved is normalised: a host pasted as a URL, Docker Hub's official images
  under library/, plain HTTP with no certificate to check, a company authority that
  must be a readable PEM;
- the allowlist decides which repositories may be read;
- the Test signs in the way the registry asks (a token from its realm, or Basic),
  reads the first repository of the allowlist, and is kept on the registry;
- tags are read page by page, a tag resolves to the digest of its manifest.
"""
import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.db.session import get_session
from app.models import AuditLog, ContainerRegistry, ContainerRegistryVariable
from app.routes import registries as routes
from app.services import container_registry as cr
from tests.conftest import make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"
TOKEN = "registry-t0ken-s3cret"
DIGEST = "sha256:" + "cd" * 32


class FakeRegistry:
    """A registry at reg.test: Bearer (a token from auth.test, as Docker Hub, GHCR, Harbor)
    or Basic (as `registry` with htpasswd). The user is ada / TOKEN."""

    def __init__(self, scheme: str = "bearer"):
        self.scheme = scheme
        self.tags = {"team/forecast": [f"v{i}" for i in range(1, 8)], "library/python": ["3.12-slim", "3.13"]}
        self.calls: list[str] = []

    def _user_ok(self, req: httpx.Request) -> bool:
        h = req.headers.get("authorization", "")
        return h == "Basic " + base64.b64encode(f"ada:{TOKEN}".encode()).decode()

    def handle(self, req: httpx.Request) -> httpx.Response:
        url = urlsplit(str(req.url))
        self.calls.append(f"{req.method} {url.netloc}{url.path}")
        if url.netloc == "auth.test":
            if req.headers.get("authorization") and not self._user_ok(req):
                return httpx.Response(401, json={"errors": [{"code": "UNAUTHORIZED"}]})
            scope = parse_qs(url.query).get("scope", [""])[0]
            return httpx.Response(200, json={"token": f"tok:{scope}:{'user' if self._user_ok(req) else 'anon'}"})
        if self.scheme == "bearer":
            if not req.headers.get("authorization", "").startswith("Bearer tok:"):
                return httpx.Response(401, headers={"WWW-Authenticate":
                                                    'Bearer realm="https://auth.test/token",service="reg.test"'})
            if req.headers["authorization"].endswith(":anon") and url.path != "/v2/":
                return httpx.Response(401, json={"errors": [{"code": "UNAUTHORIZED"}]})
        else:
            if not self._user_ok(req):
                return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="acme"'})
        if url.path == "/v2/":
            return httpx.Response(200, json={})
        parts = url.path.removeprefix("/v2/").split("/")
        if parts[-2] == "tags":  # /v2/<repo>/tags/list?n=…&last=…
            repo = "/".join(parts[:-2])
            if repo not in self.tags:
                return httpx.Response(404, json={"errors": [{"code": "NAME_UNKNOWN"}]})
            q = parse_qs(url.query)
            n, last = int(q.get("n", ["100"])[0]), q.get("last", [None])[0]
            all_ = self.tags[repo]
            start = all_.index(last) + 1 if last else 0
            page = all_[start:start + 3]  # small pages: the client follows the Link header
            headers = {}
            if start + 3 < len(all_):
                headers["Link"] = f'</v2/{repo}/tags/list?n={n}&last={page[-1]}>; rel="next"'
            return httpx.Response(200, json={"name": repo, "tags": page}, headers=headers)
        if parts[-2] == "manifests":
            repo, ref = "/".join(parts[:-2]), parts[-1]
            if ref not in self.tags.get(repo, []):
                return httpx.Response(404)
            assert "application/vnd.oci.image.index.v1+json" in req.headers["accept"]
            return httpx.Response(200, headers={"Docker-Content-Digest": DIGEST})
        return httpx.Response(404)


@pytest.fixture
def registry(monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(cr, "_transport", httpx.MockTransport(fake.handle))
    return fake


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("CONTAINERS__ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app(session):
    from app.routes import api_tokens as tokens_routes
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, tokens_routes.router, routes.router):
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


def _detail(entry) -> dict:
    return json.loads(entry.detail) if isinstance(entry.detail, str) else entry.detail


def _create(c, **kw):
    body = {"name": "Acme", "host": "reg.test", "username": "ada", "password": TOKEN,
            "repositories": ["team/forecast", "ml/*"], **kw}
    return c.post("/admin/registries", json=body, headers=CSRF)


def _ca_pem() -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Acme Internal CA")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


# ── the panel ────────────────────────────────────────────────────────────────
def test_without_kubernetes_the_section_says_why_and_saves_nothing(app, session):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    state = boss.get("/admin/registries").json()
    assert state["disabled_reason"] == "kubernetes" and state["registries"] == []
    assert _create(boss).status_code == 409
    assert session.exec(select(ContainerRegistry)).all() == []


def test_the_password_is_written_and_never_read_back(app, session, enabled):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    r = _create(boss)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["password_set"] is True and TOKEN not in json.dumps(out)
    row = session.get(ContainerRegistry, out["id"])
    assert decrypt_secret(row.password_enc) == TOKEN
    assert TOKEN not in json.dumps(boss.get("/admin/registries").json())
    entry = _audit(session, "registry.create")[0]
    assert TOKEN not in json.dumps(entry.detail) and _detail(entry)["password"] is True
    # None keeps it, a text replaces it, "" removes it
    boss.put(f"/admin/registries/{out['id']}", json={"description": "the data team's"}, headers=CSRF)
    session.expire_all()
    assert decrypt_secret(session.get(ContainerRegistry, out["id"]).password_enc) == TOKEN
    boss.put(f"/admin/registries/{out['id']}", json={"password": "n3w"}, headers=CSRF)
    session.expire_all()
    assert decrypt_secret(session.get(ContainerRegistry, out["id"]).password_enc) == "n3w"
    gone = boss.put(f"/admin/registries/{out['id']}", json={"password": ""}, headers=CSRF).json()
    assert gone["password_set"] is False
    assert [_detail(e)["password"] for e in _audit(session, "registry.update")
            if "password" in _detail(e)] == ["changed", "removed"]


def test_an_observer_reads_and_a_token_changes_nothing(app, session, enabled):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    rid = _create(boss).json()["id"]
    watcher = _login(app, session, "eye@x.local", is_observer=True)
    state = watcher.get("/admin/registries").json()
    assert state["editable"] is False and [x["name"] for x in state["registries"]] == ["Acme"]
    assert watcher.put(f"/admin/registries/{rid}", json={"name": "Mine"}, headers=CSRF).status_code == 403
    assert watcher.delete(f"/admin/registries/{rid}", headers=CSRF).status_code == 403
    token = boss.post("/auth/tokens", json={"name": "ci", "scope": "write", "expires_in_days": 5},
                      headers=CSRF).json()["token"]
    bearer = TestClient(app)
    r = bearer.put(f"/admin/registries/{rid}", json={"name": "Mine"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and "container registries" in r.json()["detail"]


@pytest.mark.parametrize("field, value, message", [
    ("host", "not a host", "host name"),
    ("repositories", ["Team/Forecast"], "not a repository"),
    ("tls", "maybe", "public"),
    ("protocol", "ftp", "https or http"),
])
def test_what_cannot_be_saved_says_why(app, session, enabled, field, value, message):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    r = _create(boss, **{field: value})
    assert r.status_code == 422 and message in json.dumps(r.json())


def test_what_is_saved_is_normalised(app, session, enabled):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    hub = _create(boss, name="Hub", host="https://Docker.io/", repositories=["python", "team/*", "python", "/ml/x/"]).json()
    assert hub["host"] == "docker.io" and hub["repositories"] == ["library/python", "team/*", "ml/x"]
    assert _create(boss, name="hub").status_code == 409  # names are unique whatever their case
    internal = _create(boss, name="Internal", host="registry.acme.internal:5000", protocol="http", tls="ca",
                       ca_cert="garbage").json()
    assert internal["protocol"] == "http" and internal["tls"] == "none" and internal["ca_cert"] == ""
    assert _create(boss, name="Bad CA", tls="ca", ca_cert="garbage").status_code == 422
    pem = _ca_pem()
    company = _create(boss, name="Company", tls="ca", ca_cert=pem).json()
    assert company["tls"] == "ca" and "BEGIN CERTIFICATE" in company["ca_cert"]
    # a certificate the administrator chose not to check is in the audit
    boss.put(f"/admin/registries/{company['id']}", json={"tls": "none"}, headers=CSRF)
    change = _detail(_audit(session, "registry.update")[-1])
    assert change["tls"] == {"from": "ca", "to": "none"} and change["ca_cert"] == "removed"


# ── speaking to the registry ─────────────────────────────────────────────────
@pytest.mark.parametrize("scheme", ["bearer", "basic"])
def test_the_test_signs_in_and_reads_the_first_allowed_repository(app, session, enabled, registry, scheme):
    registry.scheme = scheme
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    rid = _create(boss).json()["id"]
    out = boss.post(f"/admin/registries/{rid}/test", headers=CSRF).json()
    assert out["ok"] is True and out["auth"] == scheme and out["repository"] == "team/forecast"
    assert out["tags"] == ["v1", "v2", "v3", "v4", "v5"]  # a few, through two pages
    listed = boss.get("/admin/registries").json()["registries"][0]
    assert listed["last_test_ok"] is True and "team/forecast" in listed["last_test_message"]
    assert _audit(session, "registry.test")[0].outcome == "success"


def test_a_failed_test_says_why_and_is_kept(app, session, enabled, registry):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    rid = _create(boss, password="wrong").json()["id"]
    out = boss.post(f"/admin/registries/{rid}/test", headers=CSRF).json()
    assert out["ok"] is False and "refused the user and the password" in out["message"]
    assert boss.get(f"/admin/registries/{rid}").json()["last_test_ok"] is False
    assert _audit(session, "registry.test")[0].outcome == "failure"
    # a new password: the old result no longer says anything
    fresh = boss.put(f"/admin/registries/{rid}", json={"password": TOKEN}, headers=CSRF).json()
    assert fresh["last_test_ok"] is None and fresh["last_test_message"] == ""


def test_only_allowed_repositories_are_read(app, session, enabled, registry):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    rid = _create(boss).json()["id"]
    r = boss.get(f"/admin/registries/{rid}/tags", params={"repository": "team/forecast"})
    assert r.status_code == 200 and r.json()["tags"] == [f"v{i}" for i in range(1, 8)]
    assert boss.get(f"/admin/registries/{rid}/tags", params={"repository": "library/python"}).status_code == 403
    assert boss.get(f"/admin/registries/{rid}/tags", params={"repository": "ml/missing"}).status_code == 502


def test_variables_reserved_names_and_secrets(app, session, enabled):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    rid = _create(boss).json()["id"]
    for bad in ("lower", "TABULARIA_INPUT", "AWS_REGION", "1X"):
        assert boss.put(f"/admin/registries/{rid}/variables/{bad}", json={"value": "x"}, headers=CSRF).status_code == 422
    out = boss.put(f"/admin/registries/{rid}/variables/API_KEY", json={"value": "k-s3cret", "secret": True},
                   headers=CSRF).json()
    boss.put(f"/admin/registries/{rid}/variables/REGION", json={"value": "eu"}, headers=CSRF)
    out = boss.get(f"/admin/registries/{rid}").json()
    assert {v["name"]: v["value"] for v in out["variables"]} == {"API_KEY": None, "REGION": "eu"}
    assert "k-s3cret" not in json.dumps(out) and "k-s3cret" not in json.dumps([e.detail for e in session.exec(select(AuditLog)).all()])
    # a secret one keeps its value when only re-saved as secret
    assert boss.put(f"/admin/registries/{rid}/variables/API_KEY", json={"secret": True}, headers=CSRF).status_code == 200
    row = session.exec(select(ContainerRegistryVariable).where(ContainerRegistryVariable.name == "API_KEY")).one()
    assert decrypt_secret(row.value_enc) == "k-s3cret"
    assert boss.delete(f"/admin/registries/{rid}/variables/REGION", headers=CSRF).status_code == 200


def test_deleting_a_registry_takes_its_variables_and_access(app, session, enabled):
    from app.models import ContainerRegistryAccess, Group

    boss = _login(app, session, "boss@x.local", is_superuser=True)
    g = Group(name="data science")
    session.add(g)
    session.commit()
    rid = _create(boss, access=[{"group_id": g.id}]).json()["id"]
    assert boss.get(f"/admin/registries/{rid}").json()["access"] == [
        {"group_id": g.id, "user_id": None, "repositories": [], "name": "data science"}]
    boss.put(f"/admin/registries/{rid}/variables/REGION", json={"value": "eu"}, headers=CSRF)
    assert boss.delete(f"/admin/registries/{rid}", headers=CSRF).status_code == 204
    assert session.exec(select(ContainerRegistryAccess)).all() == []
    assert session.exec(select(ContainerRegistryVariable)).all() == []
    assert _audit(session, "registry.delete")[0].target_label == "Acme"


# ── the client ───────────────────────────────────────────────────────────────
def test_a_tag_resolves_to_its_digest(registry):
    r = cr.Registry(host="reg.test", username="ada", password=TOKEN)
    assert asyncio.run(cr.digest(r, "team/forecast", "v3")) == DIGEST
    assert asyncio.run(cr.digest(r, "team/forecast", DIGEST)) == DIGEST  # a digest is already one
    with pytest.raises(cr.RegistryError, match="no tag «v99»"):
        asyncio.run(cr.digest(r, "team/forecast", "v99"))


def test_an_anonymous_client_cannot_read_a_private_repository(registry):
    with pytest.raises(cr.RegistryError, match="team/forecast is private: the registry asks for a user"):
        asyncio.run(cr.tags(cr.Registry(host="reg.test"), "team/forecast"))


@pytest.mark.parametrize("patterns, repository, ok", [
    (["team/forecast"], "team/forecast", True),
    (["team/forecast"], "team/forecast-v2", False),
    (["team/*"], "team/forecast", True),
    (["team/*"], "team/sub/model", True),
    (["team/*"], "teams/forecast", False),
    (["*"], "anything/at/all", True),
    ([], "team/forecast", False),
])
def test_the_allowlist(patterns, repository, ok):
    assert cr.allowed(patterns, repository) is ok


def test_docker_hub_names(registry):
    assert cr.api_host("docker.io") == "registry-1.docker.io" and cr.api_host("ghcr.io") == "ghcr.io"
    assert cr.repository_name("docker.io", "python") == "library/python"
    assert cr.repository_name("docker.io", "bitnami/redis") == "bitnami/redis"
    assert cr.repository_name("ghcr.io", "python") == "python"


@pytest.mark.parametrize("address", ["169.254.169.254", "100.100.100.200", "168.63.129.16"])
def test_the_cloud_metadata_addresses_are_never_a_registry(address):
    with pytest.raises(cr.RegistryError, match="metadata"):
        asyncio.run(cr.ping(cr.Registry(host=address)))


def test_plain_http_on_an_https_registry_says_so(monkeypatch):
    def handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Client sent an HTTP request to an HTTPS server.\n")
    monkeypatch.setattr(cr, "_transport", httpx.MockTransport(handle))
    with pytest.raises(cr.RegistryError, match="choose HTTPS"):
        asyncio.run(cr.ping(cr.Registry(host="reg.test", protocol="http")))
