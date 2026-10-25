"""Container registries (Administration → Container registries): where the images of the
Container node come from (services/container_registry.py speaks to them).

Read by administrators and observers (no secret ever leaves: the password or token
and the secret variables show only that they are set); changed by administrators
only, never with an API token. On Kubernetes only (CONTAINERS__ENABLED): elsewhere the
section says why and nothing can be saved.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import ContainerRegistry, ContainerRegistryAccess, ContainerRegistryVariable, Group, User
from app.models.registry import PROTOCOLS
from app.models.scripting import now_utc
from app.schemas.models import UtcDateTime
from app.deps.auth import get_current_user
from app.services import api_tokens, audit, container_cluster, container_registry as cr, registry_access
from app.services.kube_scaling import KubeError
from app.services.permissions import is_admin

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ReleasingRoute, prefix="/admin/registries", tags=["registries"])
# for the flow editor: only what the person asking may use
editor_router = APIRouter(route_class=ReleasingRoute, prefix="/registries", tags=["registries"])

VARIABLE_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# set by Tabularia in every container: an administrator cannot define them
RESERVED_PREFIXES = ("TABULARIA_", "AWS_")
MAX_REPOSITORIES = 200


# ── shapes ────────────────────────────────────────────────────────────────────
class VariableOut(BaseModel):
    name: str
    secret: bool
    value: Optional[str]  # None for a secret one
    updated_at: Optional[UtcDateTime]
    updated_by: Optional[str]


class AccessIn(BaseModel):
    group_id: Optional[int] = None
    user_id: Optional[int] = None
    repositories: list[str] = Field(default_factory=list)  # empty: all the registry allows


class AccessOut(AccessIn):
    name: str  # the group's name, or the user's email


class RegistryOut(BaseModel):
    id: int
    name: str
    description: str
    host: str
    protocol: str
    tls: str
    ca_cert: str  # an authority's certificate is public
    username: str
    password_set: bool
    repositories: list[str]
    enabled: bool
    access: list[AccessOut]
    variables: list[VariableOut]
    last_test_at: Optional[UtcDateTime]
    last_test_ok: Optional[bool]
    last_test_message: str
    created_at: Optional[UtcDateTime]
    created_by: Optional[str]
    updated_at: Optional[UtcDateTime]
    updated_by: Optional[str]


class RegistriesOut(BaseModel):
    disabled_reason: Optional[str]  # "kubernetes": the section is off here
    editable: bool
    registries: list[RegistryOut]


class RegistryCreate(BaseModel):
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=2000)
    host: str = Field(max_length=260)
    protocol: str = "https"
    tls: str = "public"
    ca_cert: str = Field(default="", max_length=20000)
    username: str = Field(default="", max_length=255)
    password: str = Field(default="", max_length=4000)
    repositories: list[str] = Field(default_factory=list)
    enabled: bool = True
    access: list[AccessIn] = Field(default_factory=list)


class RegistryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    host: Optional[str] = Field(default=None, max_length=260)
    protocol: Optional[str] = None
    tls: Optional[str] = None
    ca_cert: Optional[str] = Field(default=None, max_length=20000)
    username: Optional[str] = Field(default=None, max_length=255)
    # None keeps it, "" removes it, a text replaces it
    password: Optional[str] = Field(default=None, max_length=4000)
    repositories: Optional[list[str]] = None
    enabled: Optional[bool] = None
    access: Optional[list[AccessIn]] = None


class VariableSet(BaseModel):
    value: Optional[str] = Field(default=None, max_length=20000)  # None: a secret one keeps its value
    secret: bool = False


class TestOut(BaseModel):
    ok: bool
    message: str
    auth: Optional[str] = None  # anonymous | basic | bearer
    repository: Optional[str] = None  # the one read, from the allowlist
    tags: Optional[list[str]] = None


class ClusterTestIn(BaseModel):
    repository: str = Field(max_length=255)
    reference: str = Field(default="latest", max_length=128)  # a tag, or a digest


class ClusterTestOut(BaseModel):
    ok: bool
    message: str
    image: Optional[str] = None  # host/repository@digest, as the node was asked


class TagsOut(BaseModel):
    repository: str
    tags: list[str]


# ── helpers ───────────────────────────────────────────────────────────────────
def disabled_reason() -> Optional[str]:
    return None if get_settings().containers.enabled else "kubernetes"


def _no_token(request: Request, _admin: User = Depends(require_superuser)) -> None:
    """After authentication (the dependency above), before the body is read."""
    from app.routes.api_tokens import deny_if_from_token

    deny_if_from_token(request, api_tokens.NO_REGISTRY_FROM_TOKEN)


def _is_on() -> None:
    if disabled_reason():
        raise HTTPException(status_code=409, detail="Container registries need Kubernetes (CONTAINERS__ENABLED)")


def _registry(session: Session, registry_id: int) -> ContainerRegistry:
    r = session.get(ContainerRegistry, registry_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Container registry not found")
    return r


def _access(session: Session, registry_id: int) -> list[AccessOut]:
    rows = session.exec(select(ContainerRegistryAccess).where(ContainerRegistryAccess.registry_id == registry_id)
                        .order_by(ContainerRegistryAccess.id)).all()
    groups = {g.id: g.name for g in session.exec(select(Group)).all()}
    users = {u.id: u.email for u in session.exec(select(User).where(
        User.id.in_({r.user_id for r in rows if r.user_id}))).all()} if any(r.user_id for r in rows) else {}  # type: ignore[attr-defined]
    return [AccessOut(group_id=r.group_id, user_id=r.user_id, repositories=registry_access.patterns(r.repositories),
                      name=groups.get(r.group_id, f"#{r.group_id}") if r.group_id else users.get(r.user_id, f"#{r.user_id}"))
            for r in rows]


def _variables(session: Session, registry_id: int) -> list[ContainerRegistryVariable]:
    return list(session.exec(select(ContainerRegistryVariable)
                             .where(ContainerRegistryVariable.registry_id == registry_id)
                             .order_by(ContainerRegistryVariable.name)).all())


def repositories_of(r: ContainerRegistry) -> list[str]:
    return [x for x in (r.repositories or "").splitlines() if x.strip()]


def _out(session: Session, r: ContainerRegistry) -> RegistryOut:
    return RegistryOut(
        id=r.id, name=r.name, description=r.description, host=r.host, protocol=r.protocol, tls=r.tls,
        ca_cert=r.ca_cert, username=r.username, password_set=bool(r.password_enc),
        repositories=repositories_of(r), enabled=r.enabled, access=_access(session, r.id),
        variables=[VariableOut(name=v.name, secret=v.secret, value=None if v.secret else v.value,
                               updated_at=v.updated_at, updated_by=v.updated_by) for v in _variables(session, r.id)],
        last_test_at=r.last_test_at, last_test_ok=r.last_test_ok, last_test_message=r.last_test_message,
        created_at=r.created_at, created_by=r.created_by, updated_at=r.updated_at, updated_by=r.updated_by,
    )


def _name(session: Session, name: str, registry_id: Optional[int] = None) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="The registry needs a name")
    clash = session.exec(select(ContainerRegistry.id).where(func.lower(ContainerRegistry.name) == name.lower())).first()
    if clash is not None and clash != registry_id:
        raise HTTPException(status_code=409, detail=f"A registry named «{name}» already exists")
    return name


def _host(host: str) -> str:
    host = (host or "").strip().lower()
    for prefix in ("https://", "http://"):  # pasted as a URL
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.rstrip("/")
    try:
        cr.check_host(host)
    except cr.RegistryError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return host


def _trust(protocol: str, tls: str, ca_cert: str) -> tuple[str, str, str]:
    if protocol not in PROTOCOLS:
        raise HTTPException(status_code=422, detail="The protocol is https or http")
    if tls not in cr.TLS_MODES:
        raise HTTPException(status_code=422, detail="The certificate is checked against the public authorities "
                                                    "(public), your company's authority (ca), or not at all (none)")
    if protocol == "http":
        return protocol, "none", ""  # no certificate to check
    ca_cert = (ca_cert or "").strip()
    if tls == "ca":
        try:
            cr.check_ca(ca_cert)
        except cr.RegistryError as e:
            raise HTTPException(status_code=422, detail=str(e))
    return protocol, tls, ca_cert if tls == "ca" else ""


def _repositories(host: str, lines: list[str]) -> str:
    """One allowed repository per line: team/forecast, team/* (anything under it), or *."""
    out: list[str] = []
    for line in lines:
        line = line.strip().strip("/")
        if not line:
            continue
        if line != "*":
            base = line[:-2] if line.endswith("/*") else line
            base = cr.repository_name(host, base) if not line.endswith("/*") else base
            if not cr.REPOSITORY.match(base):
                raise HTTPException(status_code=422, detail=f"«{line}» is not a repository: team/forecast, team/* or *")
            line = f"{base}/*" if line.endswith("/*") else base
        if line not in out:
            out.append(line)
    if len(out) > MAX_REPOSITORIES:
        raise HTTPException(status_code=422, detail=f"At most {MAX_REPOSITORIES} repositories")
    return "\n".join(out)


def _set_access(session: Session, r: ContainerRegistry, wanted: list[AccessIn], who: str) -> None:
    """Replace who may use the registry: a group or a user per line, each once."""
    seen: set[tuple] = set()
    rows: list[ContainerRegistryAccess] = []
    group_ids = {a.group_id for a in wanted if a.group_id is not None}
    user_ids = {a.user_id for a in wanted if a.user_id is not None}
    known_groups = set(session.exec(select(Group.id).where(Group.id.in_(group_ids))).all()) if group_ids else set()  # type: ignore[attr-defined]
    known_users = set(session.exec(select(User.id).where(User.id.in_(user_ids))).all()) if user_ids else set()  # type: ignore[attr-defined]
    for a in wanted:
        if (a.group_id is None) == (a.user_id is None):
            raise HTTPException(status_code=422, detail="Each access names a group or a user")
        if a.group_id is not None and a.group_id not in known_groups:
            raise HTTPException(status_code=422, detail="Unknown group")
        if a.user_id is not None and a.user_id not in known_users:
            raise HTTPException(status_code=422, detail="Unknown user")
        key = (a.group_id, a.user_id)
        if key in seen:
            raise HTTPException(status_code=422, detail="The same group or user is listed twice")
        seen.add(key)
        rows.append(ContainerRegistryAccess(registry_id=r.id, group_id=a.group_id, user_id=a.user_id,
                                            repositories=_repositories(r.host, a.repositories), created_by=who))
    for old in session.exec(select(ContainerRegistryAccess).where(ContainerRegistryAccess.registry_id == r.id)).all():
        session.delete(old)
    session.flush()
    session.add_all(rows)


def _access_audit(items: list[AccessOut]) -> list[dict]:
    return [{"group" if a.group_id else "user": a.name, "repositories": a.repositories or "all"} for a in items]


def client_of(r: ContainerRegistry) -> cr.Registry:
    """What services/container_registry.py needs to speak to it, the password decrypted."""
    return cr.Registry(host=r.host, protocol=r.protocol, tls=r.tls, ca_cert=r.ca_cert, username=r.username,
                       password=decrypt_secret(r.password_enc) if r.password_enc else "")


def _first_exact(r: ContainerRegistry) -> Optional[str]:
    """A repository the test can read: the first of the allowlist that is not a pattern."""
    return next((x for x in repositories_of(r) if "*" not in x), None)


# ── the registries ────────────────────────────────────────────────────────────
@router.get("", response_model=RegistriesOut)
def listing(request: Request, session: Session = Depends(get_session), chi: User = Depends(require_observer)):
    rows = session.exec(select(ContainerRegistry).order_by(func.lower(ContainerRegistry.name))).all()
    return RegistriesOut(disabled_reason=disabled_reason(),
                         editable=is_admin(session, chi) and not api_tokens.current_name(request),
                         registries=[_out(session, r) for r in rows])


@router.get("/{registry_id}", response_model=RegistryOut)
def read(registry_id: int, session: Session = Depends(get_session), _viewer: User = Depends(require_observer)):
    return _out(session, _registry(session, registry_id))


@router.post("", response_model=RegistryOut, status_code=201, dependencies=[Depends(_no_token)])
def create(body: RegistryCreate, request: Request, session: Session = Depends(get_session),
         admin: User = Depends(require_superuser)):
    _is_on()
    host = _host(body.host)
    protocol, tls, ca_cert = _trust(body.protocol, body.tls, body.ca_cert)
    r = ContainerRegistry(
        name=_name(session, body.name), description=body.description.strip(), host=host, protocol=protocol, tls=tls,
        ca_cert=ca_cert, username=body.username.strip(),
        password_enc=encrypt_secret(body.password) if body.password else None,
        repositories=_repositories(host, body.repositories), enabled=body.enabled,
        created_by=admin.email, updated_by=admin.email)
    session.add(r)
    session.flush()
    _set_access(session, r, body.access, admin.email)
    session.commit()
    session.refresh(r)
    audit.record_audit(session, actor=admin, action=audit.REGISTRY_CREATE, request=request,
                       target_type="container_registry", target_id=r.id, target_label=r.name,
                       detail={"host": r.host, "protocol": r.protocol, "tls": r.tls, "username": r.username,
                               "password": bool(r.password_enc), "repositories": repositories_of(r),
                               "access": _access_audit(_access(session, r.id))})
    return _out(session, r)


@router.put("/{registry_id}", response_model=RegistryOut, dependencies=[Depends(_no_token)])
def update(registry_id: int, body: RegistryUpdate, request: Request, session: Session = Depends(get_session),
             admin: User = Depends(require_superuser)):
    _is_on()
    r = _registry(session, registry_id)
    changes: dict = {}

    def change(field: str, value) -> None:
        if getattr(r, field) != value:
            changes[field] = {"from": getattr(r, field), "to": value}
            setattr(r, field, value)

    if body.name is not None:
        change("name", _name(session, body.name, r.id))
    if body.description is not None:
        change("description", body.description.strip())
    if body.host is not None:
        change("host", _host(body.host))
    if body.protocol is not None or body.tls is not None or body.ca_cert is not None:
        protocol, tls, ca_cert = _trust(body.protocol or r.protocol, body.tls or r.tls,
                                        r.ca_cert if body.ca_cert is None else body.ca_cert)
        change("protocol", protocol)
        change("tls", tls)
        if ca_cert != r.ca_cert:  # a certificate is long: the audit says it changed
            r.ca_cert = ca_cert
            changes["ca_cert"] = "changed" if ca_cert else "removed"
    if body.username is not None:
        change("username", body.username.strip())
    if body.password is not None:
        r.password_enc = encrypt_secret(body.password) if body.password else None
        changes["password"] = "changed" if body.password else "removed"
    if body.repositories is not None:
        allowed = _repositories(r.host, body.repositories)
        if allowed != r.repositories:
            changes["repositories"] = {"from": repositories_of(r), "to": [x for x in allowed.splitlines() if x]}
            r.repositories = allowed
    if body.enabled is not None:
        change("enabled", body.enabled)
    if body.access is not None:
        before = _access_audit(_access(session, r.id))
        _set_access(session, r, body.access, admin.email)
        after = _access_audit(_access(session, r.id))
        if before != after:
            changes["access"] = {"from": before, "to": after}
    if not changes:
        return _out(session, r)
    if {"host", "protocol", "tls", "ca_cert", "username", "password"} & changes.keys():
        r.last_test_at, r.last_test_ok, r.last_test_message = None, None, ""  # the old test no longer says anything
    r.updated_at, r.updated_by = now_utc(), admin.email
    session.add(r)
    session.commit()
    session.refresh(r)
    audit.record_audit(session, actor=admin, action=audit.REGISTRY_UPDATE, request=request,
                       target_type="container_registry", target_id=r.id, target_label=r.name, detail=changes)
    return _out(session, r)


@router.delete("/{registry_id}", status_code=204, dependencies=[Depends(_no_token)])
async def delete(registry_id: int, request: Request, session: Session = Depends(get_session),
                   admin: User = Depends(require_superuser)):
    r = _registry(session, registry_id)
    if not disabled_reason():
        try:  # its credentials leave the cluster with it
            await container_cluster.remove_pull_secrets(r.id)
        except KubeError as e:
            logger.warning("registry %s: pull Secrets not removed (%s)", r.id, e)
    name = r.name
    for row in session.exec(select(ContainerRegistryAccess).where(ContainerRegistryAccess.registry_id == r.id)).all():
        session.delete(row)
    for row in _variables(session, r.id):
        session.delete(row)
    session.flush()
    session.delete(r)
    session.commit()
    audit.record_audit(session, actor=admin, action=audit.REGISTRY_DELETE, request=request,
                       target_type="container_registry", target_id=registry_id, target_label=name)


@router.post("/{registry_id}/test", response_model=TestOut, dependencies=[Depends(_no_token)])
async def trial(registry_id: int, request: Request, session: Session = Depends(get_session),
                admin: User = Depends(require_superuser)):
    """The address, the certificate, the credentials and, with an allowlist that names a
    repository, the right to read it. Kept on the registry, so the list says whether it works."""
    _is_on()
    r = _registry(session, registry_id)
    repository = _first_exact(r)
    try:
        found = await cr.ping(client_of(r), repository)
        who = f"as {r.username}" if r.username else "anonymously"
        out = TestOut(ok=True, auth=found["auth"], repository=found["repository"], tags=found["tags"],
                      message=(f"Read {found['repository']} {who}: {len(found['tags'] or [])} tags shown" if repository
                               else f"The registry answers and lets Tabularia in {who}; name a repository in the "
                                    "allowlist to test reading it"))
    except cr.RegistryError as e:
        out = TestOut(ok=False, message=str(e))
    r.last_test_at, r.last_test_ok, r.last_test_message = now_utc(), out.ok, out.message
    session.add(r)
    session.commit()
    audit.record_audit(session, actor=admin, action=audit.REGISTRY_TEST, request=request,
                       target_type="container_registry", target_id=r.id, target_label=r.name,
                       detail={"ok": out.ok, "message": out.message[:500], "repository": out.repository},
                       outcome="success" if out.ok else "failure")
    return out


@router.post("/{registry_id}/cluster-test", response_model=ClusterTestOut, dependencies=[Depends(_no_token)])
async def trial_in_cluster(registry_id: int, body: ClusterTestIn, request: Request,
                            session: Session = Depends(get_session), admin: User = Depends(require_superuser)):
    """Can a node of the cluster download this image? A Pod with the image and a command
    that does not exist: the node downloads it, nothing of it runs (container_cluster.py)."""
    _is_on()
    if not container_cluster.namespaces():
        raise HTTPException(status_code=409, detail="The containers' namespace is not set (containers.enabled in the "
                                                    "Helm chart sets CONTAINERS__NAMESPACE)")
    r = _registry(session, registry_id)
    name = cr.repository_name(r.host, body.repository)
    if not cr.allowed(repositories_of(r), name):
        raise HTTPException(status_code=403, detail=f"{name} is not among the repositories allowed for {r.name}")
    found = await container_cluster.try_pull(r, client_of(r).password, name, body.reference.strip() or "latest")
    out = ClusterTestOut(**found)
    audit.record_audit(session, actor=admin, action=audit.REGISTRY_CLUSTER_TEST, request=request,
                       target_type="container_registry", target_id=r.id, target_label=r.name,
                       detail={"ok": out.ok, "image": out.image, "message": out.message[:500]},
                       outcome="success" if out.ok else "failure")
    return out


@router.get("/{registry_id}/tags", response_model=TagsOut)
async def tag(registry_id: int, repository: str = Query(max_length=255), session: Session = Depends(get_session),
              _viewer: User = Depends(require_observer)):
    """The tags of an allowed repository."""
    _is_on()
    r = _registry(session, registry_id)
    name = cr.repository_name(r.host, repository)
    if not cr.allowed(repositories_of(r), name):
        raise HTTPException(status_code=403, detail=f"{name} is not among the repositories allowed for {r.name}")
    try:
        return TagsOut(repository=name, tags=await cr.tags(client_of(r), name))
    except cr.RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── variables ────────────────────────────────────────────────────────────────
def _variable_name(name: str) -> str:
    if not VARIABLE_NAME.match(name) or len(name) > 128:
        raise HTTPException(status_code=422, detail="A variable name is in capitals, digits and _, "
                                                    "and does not start with a digit")
    if name.startswith(RESERVED_PREFIXES):
        raise HTTPException(status_code=422, detail=f"Names starting with {' or '.join(RESERVED_PREFIXES)} "
                                                    "are set by Tabularia")
    return name


@router.put("/{registry_id}/variables/{name}", response_model=RegistryOut, dependencies=[Depends(_no_token)])
def write_variable(registry_id: int, name: str, body: VariableSet, request: Request,
                     session: Session = Depends(get_session), admin: User = Depends(require_superuser)):
    _is_on()
    r = _registry(session, registry_id)
    name = _variable_name(name)
    var = session.exec(select(ContainerRegistryVariable).where(
        ContainerRegistryVariable.registry_id == r.id, ContainerRegistryVariable.name == name)).first()
    new_one = var is None
    if body.value is None and (new_one or not var.secret or not body.secret):
        raise HTTPException(status_code=422, detail="The variable needs a value")
    var = var or ContainerRegistryVariable(registry_id=r.id, name=name)
    var.secret = body.secret
    if body.value is not None:
        var.value, var.value_enc = ("", encrypt_secret(body.value)) if var.secret else (body.value, None)
    var.updated_at, var.updated_by = now_utc(), admin.email
    session.add(var)
    r.updated_at, r.updated_by = now_utc(), admin.email
    session.add(r)
    session.commit()
    audit.record_audit(session, actor=admin, action=audit.REGISTRY_VARIABLE_SET, request=request,
                       target_type="container_registry", target_id=r.id, target_label=r.name,
                       detail={"name": name, "secret": var.secret, "new": new_one})
    return _out(session, r)


@router.delete("/{registry_id}/variables/{name}", response_model=RegistryOut, dependencies=[Depends(_no_token)])
def delete_variable(registry_id: int, name: str, request: Request, session: Session = Depends(get_session),
                       admin: User = Depends(require_superuser)):
    r = _registry(session, registry_id)
    var = session.exec(select(ContainerRegistryVariable).where(
        ContainerRegistryVariable.registry_id == r.id, ContainerRegistryVariable.name == name)).first()
    if var is None:
        raise HTTPException(status_code=404, detail="Variable not found")
    session.delete(var)
    r.updated_at, r.updated_by = now_utc(), admin.email
    session.add(r)
    session.commit()
    audit.record_audit(session, actor=admin, action=audit.REGISTRY_VARIABLE_DELETE, request=request,
                       target_type="container_registry", target_id=r.id, target_label=r.name,
                       detail={"name": name})
    return _out(session, r)


# ── for the flow editor: only what the person asking may use ────────────────
class EditorVariable(BaseModel):
    name: str
    secret: bool
    value: Optional[str]  # None for a secret one


class EditorRegistry(BaseModel):
    id: int
    name: str
    description: str
    host: str
    repositories: list[str]  # the registry's patterns cut to this person's grants
    variables: list[EditorVariable]  # what every container of it finds, set by the administrators


class EditorLimits(BaseModel):
    max_cpu: str
    max_memory: str
    max_timeout_seconds: int


class EditorRegistries(BaseModel):
    disabled_reason: Optional[str]
    registries: list[EditorRegistry]
    limits: EditorLimits
    storage_ready: bool  # the scripts bucket, through which a node's input and output go
    tabularia_variables: list[str]


@editor_router.get("", response_model=EditorRegistries)
def editor_listing(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """The registries this person may choose in a Container node, and which repositories."""
    from app.models import ScriptStorage
    from app.services.container_runs import storage_ready
    from app.services.script_runs import TABULARIA_VARIABLES

    s = get_settings().containers
    return EditorRegistries(
        disabled_reason=disabled_reason(),
        registries=[EditorRegistry(id=r.id, name=r.name, description=r.description, host=r.host,
                                   repositories=registry_access.visible_repositories(session, user, r),
                                   variables=[EditorVariable(name=v.name, secret=v.secret,
                                                             value=None if v.secret else v.value)
                                              for v in _variables(session, r.id)])
                    for r in registry_access.usable_registries(session, user)],
        limits=EditorLimits(max_cpu=s.max_cpu, max_memory=s.max_memory, max_timeout_seconds=s.max_timeout_seconds),
        storage_ready=storage_ready(session.get(ScriptStorage, 1)),
        tabularia_variables=list(TABULARIA_VARIABLES))


@editor_router.get("/{registry_id}/tags", response_model=TagsOut)
async def editor_tag(registry_id: int, repository: str = Query(max_length=255),
                     session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """The tags of a repository this person may use; nothing about the others, not even
    whether they exist."""
    _is_on()
    r = session.get(ContainerRegistry, registry_id)
    if r is None or not r.enabled or not registry_access.can_use(session, user, r):
        raise HTTPException(status_code=404, detail="Container registry not found")
    name = cr.repository_name(r.host, repository)
    if not registry_access.repository_allowed(session, user, r, name):
        raise HTTPException(status_code=403, detail=f"{name} is not among the images of {r.name} you may use")
    try:
        return TagsOut(repository=name, tags=await cr.tags(client_of(r), name))
    except cr.RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e))


class ImageEnv(BaseModel):
    name: str
    value: str


class ImageConfigOut(BaseModel):
    """What an image does when nothing overrides it (container_registry.image_config)."""
    repository: str
    reference: str
    digest: Optional[str]
    platforms: list[str]
    created: Optional[str]
    entrypoint: list[str]
    cmd: list[str]
    env: list[ImageEnv]
    labels: dict[str, str]
    working_dir: Optional[str]
    user: Optional[str]
    size: Optional[int]


@editor_router.get("/{registry_id}/image", response_model=ImageConfigOut)
async def editor_image(registry_id: int, repository: str = Query(max_length=255), reference: str = Query("latest", max_length=200),
                       session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """The configuration of an image this person may use: what the Container node shows
    and offers to fill in. Nothing about the others, not even whether they exist."""
    _is_on()
    r = session.get(ContainerRegistry, registry_id)
    if r is None or not r.enabled or not registry_access.can_use(session, user, r):
        raise HTTPException(status_code=404, detail="Container registry not found")
    name = cr.repository_name(r.host, repository)
    if not registry_access.repository_allowed(session, user, r, name):
        raise HTTPException(status_code=403, detail=f"{name} is not among the images of {r.name} you may use")
    try:
        return ImageConfigOut(**await cr.image_config(client_of(r), name, reference or "latest"))
    except cr.RegistryError as e:
        raise HTTPException(status_code=502, detail=str(e))
