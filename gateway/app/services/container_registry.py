"""Container registries (Administration → Container registries): what the gateway asks
a registry, over the Docker Registry HTTP API v2 (the OCI distribution API) that Docker
Hub, GHCR, GitLab, Harbor, Scaleway and `registry` all speak.

- `ping`: are the address, the certificate and the credentials right?
- `tags`: the tags of a repository, for the node's picker;
- `digest`: the manifest a tag points to now, recorded in every run.

Authentication follows what the registry answers on /v2/: nothing, Basic, or a Bearer
token from the realm it names (asked with the credentials, for a pull scope). The
certificate is checked against the public authorities, the company's authority the
administrator gave, or not at all (a test registry; recorded in the audit when chosen).
Plain HTTP is for internal registries only.

The connections are administrators' and usually internal, so private addresses are
fine; the cloud metadata endpoints never are.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlsplit

import httpx

TIMEOUT_SECONDS = 15
MAX_TAGS = 1000
# what a registry may answer to a manifest request: an image or an index of images
MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
HOST = re.compile(r"^(?=.{1,253}(:|$))[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*(:\d{1,5})?$")
REPOSITORY = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$")
TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
TLS_MODES = ("public", "ca", "none")
# link-local (AWS, GCP, Azure IMDS), and the metadata addresses outside it: Alibaba,
# AWS in IPv6, Azure's wireserver. The address is checked when it is resolved; a name
# that changes its answer in between is not: the connections are administrators' own.
_METADATA = tuple(ipaddress.ip_network(n) for n in (
    "169.254.0.0/16", "fe80::/10", "100.100.100.200/32", "fd00:ec2::254/128", "168.63.129.16/32"))


# for the tests: a transport standing in for the registries (and no name resolution)
_transport: Optional[httpx.AsyncBaseTransport] = None


class RegistryError(Exception):
    """What went wrong, in the words of whoever must fix it."""


@dataclass(frozen=True)
class Registry:
    host: str  # docker.io, ghcr.io, registry.acme.internal:5000
    protocol: str = "https"  # https | http
    tls: str = "public"  # public | ca | none
    ca_cert: str = ""  # PEM, with tls "ca"
    username: str = ""
    password: str = ""


def api_host(host: str) -> str:
    """Docker Hub's images are named docker.io/…; its API answers elsewhere."""
    return "registry-1.docker.io" if host in ("docker.io", "index.docker.io") else host


def repository_name(host: str, repository: str) -> str:
    """Docker Hub's official images live under library/: python → library/python."""
    repository = repository.strip().strip("/")
    if host in ("docker.io", "index.docker.io") and "/" not in repository:
        return f"library/{repository}"
    return repository


def allowed(patterns: list[str], repository: str) -> bool:
    """The administrator's allowlist: an exact repository, a prefix ending in /* (anything
    under it, at any depth), or * (the whole registry)."""
    for p in patterns:
        p = p.strip().strip("/")
        if p == "*" or p == repository or (p.endswith("/*") and repository.startswith(p[:-1])):
            return True
    return False


def check_host(host: str) -> None:
    if not HOST.match(host):
        raise RegistryError("The registry's address is a host name, with a port if needed: ghcr.io, "
                            "registry.acme.internal:5000")


async def _guard(host: str) -> None:
    """Refuse an address that resolves to the cloud metadata endpoints."""
    if _transport is not None:
        return
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise RegistryError(f"The address {host} does not resolve: check its name") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in net for net in _METADATA):
            raise RegistryError(f"{host} resolves to a cloud metadata address, which is never a registry")


def _verify(r: Registry):
    if r.protocol == "http" or r.tls == "none":
        return False
    if r.tls == "ca":
        if "BEGIN CERTIFICATE" not in r.ca_cert:
            raise RegistryError("The authority's certificate is missing: paste it in PEM (-----BEGIN CERTIFICATE-----)")
        try:
            return ssl.create_default_context(cadata=r.ca_cert)
        except (ssl.SSLError, ValueError):
            raise RegistryError("The authority's certificate cannot be read: paste it in PEM") from None
    return True


def check_ca(pem: str) -> None:
    """A company authority's certificate that can be used: PEM, readable."""
    _verify(Registry(host="ca.check", tls="ca", ca_cert=pem))


def _explain(e: Exception, r: Registry) -> RegistryError:
    text = str(e)
    if isinstance(e, httpx.ConnectError) and ("CERTIFICATE_VERIFY_FAILED" in text or "certificate" in text.lower()):
        if r.tls == "ca":
            return RegistryError("The registry's certificate is not signed by the authority given: check that it is "
                                 "the one that signed the registry's certificate")
        return RegistryError("The registry's certificate is not signed by a public authority: choose «Company "
                             "authority» and paste your authority's certificate")
    if "WRONG_VERSION_NUMBER" in text or "wrong version number" in text:
        return RegistryError("The registry answers in plain HTTP: choose HTTP as the protocol (internal registries only)")
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        return RegistryError(f"The gateway cannot reach {r.host}: {text[:200] or type(e).__name__}")
    if isinstance(e, httpx.TimeoutException):
        return RegistryError(f"{r.host} did not answer within {TIMEOUT_SECONDS} s")
    if isinstance(e, httpx.RemoteProtocolError) and r.protocol == "http":
        return RegistryError("The registry does not answer in plain HTTP: choose HTTPS as the protocol")
    return RegistryError(f"{r.host}: {text[:300] or type(e).__name__}")


def _challenge(header: str) -> tuple[str, dict]:
    """WWW-Authenticate: Bearer realm="…",service="…" → ("bearer", {realm, service})."""
    scheme, _, rest = header.strip().partition(" ")
    return scheme.lower(), {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', rest)}


class _Session:
    """One conversation with a registry: the challenge is read once, tokens per scope."""

    def __init__(self, r: Registry):
        self.r = r
        self.base = f"{r.protocol}://{api_host(r.host)}"
        self.client = httpx.AsyncClient(verify=_verify(r), timeout=TIMEOUT_SECONDS, follow_redirects=True,
                                        transport=_transport)
        self.scheme: Optional[str] = None
        self.params: dict = {}
        self.tokens: dict[str, str] = {}

    async def __aenter__(self):
        await _guard(api_host(self.r.host))
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def ping(self) -> str:
        """How the registry wants to be spoken to: anonymous, basic or bearer."""
        try:
            resp = await self.client.get(f"{self.base}/v2/")
        except httpx.HTTPError as e:
            raise _explain(e, self.r) from None
        if resp.status_code == 200:
            self.scheme = "anonymous" if not self.r.username else "basic"
            return self.scheme
        if resp.status_code == 400 and "HTTPS server" in resp.text:
            raise RegistryError("The registry answers in HTTPS: choose HTTPS as the protocol")
        if resp.status_code != 401:
            raise RegistryError(f"{self.r.host} is not a container registry (it answered {resp.status_code} on /v2/)")
        self.scheme, self.params = _challenge(resp.headers.get("www-authenticate", ""))
        if self.scheme not in ("basic", "bearer"):
            raise RegistryError(f"{self.r.host} asks for an authentication Tabularia does not speak ({self.scheme or 'none'})")
        if self.scheme == "basic":
            if not self.r.username:
                raise RegistryError("The registry asks for a user and a password")
            check = await self.client.get(f"{self.base}/v2/", auth=(self.r.username, self.r.password))
            if check.status_code in (401, 403):
                raise RegistryError("The registry refused the user and the password")
        return self.scheme

    async def _token(self, scope: str) -> str:
        if scope in self.tokens:
            return self.tokens[scope]
        realm = self.params.get("realm", "")
        if not realm.startswith(("https://", "http://")):
            raise RegistryError(f"{self.r.host} names no place to ask for a token")
        await _guard(urlsplit(realm).netloc)
        query = {"service": self.params.get("service", "")} if self.params.get("service") else {}
        if scope:
            query["scope"] = scope
        auth = (self.r.username, self.r.password) if self.r.username else None
        try:
            resp = await self.client.get(realm, params=query, auth=auth)
        except httpx.HTTPError as e:
            raise _explain(e, self.r) from None
        if resp.status_code in (401, 403):
            raise RegistryError("The registry refused the user and the password" if auth
                                else "The registry asks for a user and a password")
        if resp.status_code != 200:
            raise RegistryError(f"{self.r.host} refused a token ({resp.status_code})")
        body = resp.json()
        token = body.get("token") or body.get("access_token") or ""
        if not token:
            raise RegistryError(f"{self.r.host} gave no token")
        self.tokens[scope] = token
        return token

    async def get(self, path: str, scope: str, *, method: str = "GET", headers: Optional[dict] = None) -> httpx.Response:
        if self.scheme is None:
            await self.ping()
        auth = None
        hdrs = dict(headers or {})
        if self.scheme == "bearer":
            hdrs["Authorization"] = f"Bearer {await self._token(scope)}"
        elif self.scheme == "basic":
            auth = (self.r.username, self.r.password)
        try:
            return await self.client.request(method, path if path.startswith("http") else f"{self.base}{path}",
                                             headers=hdrs, auth=auth)
        except httpx.HTTPError as e:
            raise _explain(e, self.r) from None


def _not_visible(repository: str, status: int, anonymous: bool = False) -> RegistryError:
    if status in (401, 403):
        if anonymous:
            return RegistryError(f"{repository} is private: the registry asks for a user and a password to read it")
        return RegistryError(f"The credentials cannot read {repository}")
    return RegistryError(f"{repository} is not in the registry, or not visible with these credentials")


async def ping(r: Registry, repository: Optional[str] = None) -> dict:
    """The connection's test: the address, the certificate, the credentials and, given a
    repository of the allowlist, the right to read it."""
    check_host(r.host)
    async with _Session(r) as s:
        scheme = await s.ping()
        out = {"auth": scheme, "repository": None, "tags": None}
        if s.scheme == "bearer" and r.username:
            await s._token("")  # the credentials, whatever the repository
        if repository:
            found = await tags(r, repository, session=s, limit=5)
            out["repository"], out["tags"] = repository_name(r.host, repository), found
        return out


async def tags(r: Registry, repository: str, *, session: Optional[_Session] = None, limit: int = MAX_TAGS) -> list[str]:
    repo = repository_name(r.host, repository)
    if not REPOSITORY.match(repo):
        raise RegistryError(f"«{repository}» is not a repository name")

    async def run(s: _Session) -> list[str]:
        scope = f"repository:{repo}:pull"
        out: list[str] = []
        url: Optional[str] = f"/v2/{repo}/tags/list?n={min(limit, 1000)}"
        while url and len(out) < limit:
            resp = await s.get(url, scope)
            if resp.status_code != 200:
                raise _not_visible(repo, resp.status_code, not r.username)
            out += [t for t in (resp.json().get("tags") or []) if isinstance(t, str)]
            nxt = re.search(r'<([^>]+)>;\s*rel="next"', resp.headers.get("link", ""))
            url = nxt.group(1) if nxt else None
            if url and not url.startswith("http"):
                url = url if url.startswith("/") else f"/{url}"
        return out[:limit]

    if session is not None:
        return await run(session)
    check_host(r.host)
    async with _Session(r) as s:
        return await run(s)


async def digest(r: Registry, repository: str, reference: str) -> str:
    """The digest a tag points to now (a digest is returned as it is)."""
    repo = repository_name(r.host, repository)
    if DIGEST.match(reference):
        return reference
    if not REPOSITORY.match(repo) or not TAG.match(reference):
        raise RegistryError(f"«{repository}:{reference}» is not an image name")
    check_host(r.host)
    async with _Session(r) as s:
        scope = f"repository:{repo}:pull"
        resp = await s.get(f"/v2/{repo}/manifests/{quote(reference)}", scope, method="HEAD",
                           headers={"Accept": MANIFEST_TYPES})
        if resp.status_code == 404:
            raise RegistryError(f"{repo} has no tag «{reference}»")
        if resp.status_code != 200:
            raise _not_visible(repo, resp.status_code, not r.username)
        found = resp.headers.get("docker-content-digest", "")
        if not DIGEST.match(found):
            raise RegistryError(f"{r.host} did not say which manifest «{reference}» is")
        return found


INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
# an image's configuration is a few kilobytes; past this it is not one
MAX_CONFIG_BYTES = 2 * 2**20


def _platform(p: dict) -> str:
    return "/".join(x for x in (p.get("os"), p.get("architecture"), p.get("variant")) if x)


async def image_config(r: Registry, repository: str, reference: str) -> dict:
    """What an image does when nothing overrides it, read from its manifest and its
    configuration blob: the entrypoint and the command, the variables and the labels it
    declares, where and as whom it runs, the platforms it is built for, its size. The
    guided choice of the Container node and cell shows and offers it
    (docs/specs/saved-scripts.md, piece 6)."""
    repo = repository_name(r.host, repository)
    if not REPOSITORY.match(repo) or not (TAG.match(reference) or DIGEST.match(reference)):
        raise RegistryError(f"«{repository}:{reference}» is not an image name")
    check_host(r.host)
    async with _Session(r) as s:
        scope = f"repository:{repo}:pull"

        async def manifest(ref: str) -> tuple[dict, str]:
            resp = await s.get(f"/v2/{repo}/manifests/{quote(ref)}", scope, headers={"Accept": MANIFEST_TYPES})
            if resp.status_code == 404:
                raise RegistryError(f"{repo} has no tag «{ref}»")
            if resp.status_code != 200:
                raise _not_visible(repo, resp.status_code, not r.username)
            try:
                return resp.json(), resp.headers.get("docker-content-digest", "")
            except ValueError:
                raise RegistryError(f"{r.host} answered with a manifest that is not JSON") from None

        body, found = await manifest(reference)
        platforms: list[str] = []
        if body.get("mediaType") in INDEX_TYPES or "manifests" in body:
            entries = [m for m in body.get("manifests") or [] if isinstance(m, dict)
                       and (m.get("platform") or {}).get("os") not in (None, "unknown")]
            platforms = [_platform(m["platform"]) for m in entries]
            if not entries:
                raise RegistryError(f"{repo}:{reference} names no image for any platform")
            chosen = next((m for m in entries if m["platform"].get("os") == "linux"
                           and m["platform"].get("architecture") == "amd64"), entries[0])
            body, _ = await manifest(chosen["digest"])
        config = body.get("config") or {}
        if not DIGEST.match(str(config.get("digest") or "")):
            raise RegistryError(f"{repo}:{reference} has no configuration Tabularia can read")
        if int(config.get("size") or 0) > MAX_CONFIG_BYTES:
            raise RegistryError(f"the configuration of {repo}:{reference} is too large to read")
        resp = await s.get(f"/v2/{repo}/blobs/{config['digest']}", scope)
        if resp.status_code != 200:
            raise _not_visible(repo, resp.status_code, not r.username)
        try:
            blob = resp.json()
        except ValueError:
            raise RegistryError(f"the configuration of {repo}:{reference} is not JSON") from None
        c = blob.get("config") or {}
        env = []
        for item in c.get("Env") or []:
            name, _, value = str(item).partition("=")
            env.append({"name": name, "value": value})
        if not platforms:
            platforms = [_platform(blob)] if blob.get("architecture") else []
        return {
            "repository": repo, "reference": reference, "digest": found if DIGEST.match(found) else None,
            "platforms": platforms, "created": blob.get("created"),
            "entrypoint": [str(x) for x in c.get("Entrypoint") or []], "cmd": [str(x) for x in c.get("Cmd") or []],
            "env": env, "labels": {str(k): str(v) for k, v in (c.get("Labels") or {}).items()},
            "working_dir": c.get("WorkingDir") or None, "user": c.get("User") or None,
            "size": sum(int(x.get("size") or 0) for x in body.get("layers") or [] if isinstance(x, dict)) or None,
        }
