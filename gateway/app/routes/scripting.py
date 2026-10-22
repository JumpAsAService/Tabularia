"""Scripting environments (Admin → Scripting environments): the bucket the scripts
work in, and the environments their code runs in. See docs/specs/script-node.md.

Read by administrators and observers (no secret ever leaves: the bucket's secret key
and the secret variables show only that they are set); changed by administrators
only, never with an API token. On Kubernetes only: elsewhere the section says why
and nothing can be saved.

Saving an environment's language version or packages adds a version that the
orchestrators build (services/script_builds.py); everything else (description,
resources, groups, variables) applies to the next run without a build.
"""
from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.core.engine_client import get_engine_client
from app.core.routing import RottaCheRilascia
from app.db.session import get_session
from app.deps.auth import get_current_user, require_observer, require_superuser
from app.models import (
    Flow, Group, ScriptEnvironment, ScriptEnvironmentGroup, ScriptEnvironmentVariable, ScriptEnvironmentVersion,
    Run, ScriptStorage, User,
)
from app.models.scripting import IN_PROGRESS, LANGUAGES, now_utc
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit, flow_access, script_builds, script_packages
from app.services.kube_scaling import KubeError
from app.services.permissions import is_admin

router = APIRouter(route_class=RottaCheRilascia, prefix="/admin/scripting", tags=["scripting"])
# what the flow editor needs to write a Script node, for whoever may write one
editor_router = APIRouter(route_class=RottaCheRilascia, prefix="/scripting", tags=["scripting"])

VARIABLE_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# set by Tabularia in every Job: an administrator cannot define them
RESERVED_PREFIXES = ("TABULARIA_", "AWS_")
_CPU = re.compile(r"^\d+(\.\d+)?m?$")
_MEMORY = re.compile(r"^\d+(\.\d+)?(Ki|Mi|Gi|Ti|K|M|G|T)?$")


# ── shapes ────────────────────────────────────────────────────────────────────
class StorageOut(BaseModel):
    endpoint: str
    region: str
    bucket: str
    access_key: str
    secret_key_set: bool
    updated_at: Optional[UtcDateTime]
    updated_by: Optional[str]


class StorageUpdate(BaseModel):
    # only what changes; the secret key: a text replaces it, "" removes it
    endpoint: Optional[str] = Field(default=None, max_length=2000)
    region: Optional[str] = Field(default=None, max_length=64)
    bucket: Optional[str] = Field(default=None, max_length=255)
    access_key: Optional[str] = Field(default=None, max_length=255)
    secret_key: Optional[str] = Field(default=None, max_length=500)


class StorageTestOut(BaseModel):
    ok: bool
    message: str
    can_list: Optional[bool] = None  # True = the key can list the bucket: scripts could find other runs' files


class VersionOut(BaseModel):
    id: int
    number: int
    language_version: str
    base_image: str
    packages: str
    status: str
    image_ref: Optional[str]
    lock: Optional[str]
    build_log: Optional[str]
    error: Optional[str]
    created_at: UtcDateTime
    created_by: Optional[str]
    started_at: Optional[UtcDateTime]
    finished_at: Optional[UtcDateTime]
    current: bool


class VariableOut(BaseModel):
    name: str
    secret: bool
    value: Optional[str]  # None for a secret one
    from_tabularia: bool
    updated_at: UtcDateTime
    updated_by: Optional[str]


class EnvironmentSummary(BaseModel):
    id: int
    name: str
    description: str
    language: str
    enabled: bool
    language_version: Optional[str]  # of the latest version
    current_number: Optional[int]
    latest_number: Optional[int]
    latest_status: Optional[str]
    variables: int
    groups: list[str]
    updated_at: UtcDateTime


class EnvironmentOut(EnvironmentSummary):
    packages: str  # of the latest version
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    max_timeout_seconds: int
    group_ids: list[int]
    variable_list: list[VariableOut]
    versions: list[VersionOut]
    flows_using: list[str]


class EnvironmentCreate(BaseModel):
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=4000)
    language: str
    language_version: str
    packages: str = Field(default="", max_length=20000)
    enabled: bool = True
    cpu_request: str = "500m"
    cpu_limit: str = "2"
    memory_request: str = "1Gi"
    memory_limit: str = "4Gi"
    max_timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    group_ids: list[int] = []


class EnvironmentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=4000)
    language_version: Optional[str] = None
    packages: Optional[str] = Field(default=None, max_length=20000)
    enabled: Optional[bool] = None
    cpu_request: Optional[str] = None
    cpu_limit: Optional[str] = None
    memory_request: Optional[str] = None
    memory_limit: Optional[str] = None
    max_timeout_seconds: Optional[int] = Field(default=None, ge=60, le=86400)
    group_ids: Optional[list[int]] = None


class VariableSet(BaseModel):
    # a secret one keeps its value when `value` is omitted (to change the flag only, give the value)
    value: Optional[str] = Field(default=None, max_length=32768)
    secret: bool = False
    from_tabularia: bool = False


class ScriptingOut(BaseModel):
    disabled_reason: Optional[str]  # kubernetes | configuration; None = on
    languages: dict[str, list[str]]
    registry: str
    storage: StorageOut
    environments: list[EnvironmentSummary]
    editable: bool


# ── helpers ───────────────────────────────────────────────────────────────────
def _niente_token(request: Request, _admin: User = Depends(require_superuser)) -> None:
    """After authentication (the dependency above), before the body is read."""
    from app.routes.api_tokens import nega_se_da_token

    nega_se_da_token(request, api_tokens.NIENTE_SCRIPTING_DA_TOKEN)


def _acceso() -> None:
    reason = script_builds.disabled_reason()
    if reason == "kubernetes":
        raise HTTPException(status_code=409, detail="Scripting environments need Kubernetes (SCRIPTS__ENABLED)")
    if reason:
        raise HTTPException(status_code=409, detail="Scripting environments are not configured "
                                                    "(SCRIPTS__NAMESPACE and SCRIPTS__REGISTRY)")


def _storage(session: Session) -> ScriptStorage:
    return session.get(ScriptStorage, 1) or ScriptStorage(id=1)


def _storage_out(s: ScriptStorage) -> StorageOut:
    return StorageOut(endpoint=s.endpoint, region=s.region, bucket=s.bucket, access_key=s.access_key,
                      secret_key_set=bool(s.secret_key_enc), updated_at=s.updated_at, updated_by=s.updated_by)


def _environment(session: Session, environment_id: int) -> ScriptEnvironment:
    env = session.get(ScriptEnvironment, environment_id)
    if env is None:
        raise HTTPException(status_code=404, detail="Scripting environment not found")
    return env


def _versions(session: Session, environment_id: int) -> list[ScriptEnvironmentVersion]:
    return list(session.exec(select(ScriptEnvironmentVersion)
                             .where(ScriptEnvironmentVersion.environment_id == environment_id)
                             .order_by(ScriptEnvironmentVersion.number.desc())).all())  # type: ignore[attr-defined]


def _group_names(session: Session, environment_id: int) -> tuple[list[int], list[str]]:
    rows = session.exec(select(Group.id, Group.name).join(
        ScriptEnvironmentGroup, ScriptEnvironmentGroup.group_id == Group.id)
        .where(ScriptEnvironmentGroup.environment_id == environment_id).order_by(Group.name)).all()
    return [r[0] for r in rows], [r[1] for r in rows]


def _variables(session: Session, environment_id: int) -> list[ScriptEnvironmentVariable]:
    return list(session.exec(select(ScriptEnvironmentVariable)
                             .where(ScriptEnvironmentVariable.environment_id == environment_id)
                             .order_by(ScriptEnvironmentVariable.name)).all())


def flows_using(session: Session, environment_id: int) -> list[str]:
    """The flows with a Script node on this environment."""
    names = []
    for name, definition in session.exec(select(Flow.name, Flow.definition)
                                         .where(Flow.definition.contains('"script"'))).all():  # type: ignore[attr-defined]
        try:
            nodes = json.loads(definition or "{}").get("nodes") or []
        except ValueError:
            continue
        if any(n.get("type") == "script" and (n.get("data") or {}).get("environmentId") == environment_id
               for n in nodes if isinstance(n, dict)):
            names.append(name)
    return sorted(dict.fromkeys(names))  # two flows of the same name in two folders: said once


def _summary(session: Session, env: ScriptEnvironment, versions: list[ScriptEnvironmentVersion]) -> dict:
    latest = versions[0] if versions else None
    current = next((v for v in versions if v.id == env.current_version_id), None)
    _, groups = _group_names(session, env.id)
    n_vars = session.exec(select(func.count()).select_from(ScriptEnvironmentVariable)
                          .where(ScriptEnvironmentVariable.environment_id == env.id)).one()
    return dict(
        id=env.id, name=env.name, description=env.description, language=env.language, enabled=env.enabled,
        language_version=latest.language_version if latest else None,
        current_number=current.number if current else None,
        latest_number=latest.number if latest else None, latest_status=latest.status if latest else None,
        variables=n_vars, groups=groups, updated_at=env.updated_at,
    )


def _out(session: Session, env: ScriptEnvironment) -> EnvironmentOut:
    versions = _versions(session, env.id)
    group_ids, _ = _group_names(session, env.id)
    return EnvironmentOut(
        **_summary(session, env, versions),
        packages=versions[0].packages if versions else "",
        cpu_request=env.cpu_request, cpu_limit=env.cpu_limit, memory_request=env.memory_request,
        memory_limit=env.memory_limit, max_timeout_seconds=env.max_timeout_seconds, group_ids=group_ids,
        variable_list=[VariableOut(name=x.name, secret=x.secret, value=None if x.secret else x.value,
                                   from_tabularia=x.from_tabularia, updated_at=x.updated_at, updated_by=x.updated_by)
                       for x in _variables(session, env.id)],
        versions=[VersionOut(**v.model_dump(exclude={"environment_id", "job_name"}), current=v.id == env.current_version_id)
                  for v in versions],
        flows_using=flows_using(session, env.id),
    )


def _name(session: Session, name: str, environment_id: Optional[int] = None) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="The environment needs a name")
    clash = session.exec(select(ScriptEnvironment.id).where(func.lower(ScriptEnvironment.name) == name.lower())).first()
    if clash is not None and clash != environment_id:
        raise HTTPException(status_code=409, detail=f"An environment named «{name}» already exists")
    return name


def _language_version(language: str, version: str) -> str:
    images = get_settings().scripts.images().get(language) or {}
    if version not in images:
        raise HTTPException(status_code=422, detail=f"{language} {version} is not offered here "
                                                    f"(available: {', '.join(images) or 'none'})")
    return images[version]


def _packages(language: str, text: str) -> str:
    try:
        script_builds.parse_packages(language, text)
    except script_builds.PackageError as e:
        raise HTTPException(status_code=422, detail={"code": "package", "line": e.line, "text": e.text})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return (text or "").strip()


def _resources(env: ScriptEnvironment) -> None:
    for field, pattern in (("cpu_request", _CPU), ("cpu_limit", _CPU), ("memory_request", _MEMORY),
                           ("memory_limit", _MEMORY)):
        value = (getattr(env, field) or "").strip()
        if not pattern.match(value):
            raise HTTPException(status_code=422, detail=f"{field}: «{value}» is not a Kubernetes quantity")
        setattr(env, field, value)


def _groups(session: Session, env: ScriptEnvironment, group_ids: list[int]) -> None:
    wanted = set(group_ids)
    found = set(session.exec(select(Group.id).where(Group.id.in_(wanted))).all()) if wanted else set()  # type: ignore[attr-defined]
    if found != wanted:
        raise HTTPException(status_code=422, detail="Unknown group")
    for row in session.exec(select(ScriptEnvironmentGroup).where(ScriptEnvironmentGroup.environment_id == env.id)).all():
        if row.group_id not in wanted:
            session.delete(row)
        wanted.discard(row.group_id)
    for gid in wanted:
        session.add(ScriptEnvironmentGroup(environment_id=env.id, group_id=gid))


def _new_version(session: Session, env: ScriptEnvironment, language_version: str, packages: str,
                 who: User) -> ScriptEnvironmentVersion:
    number = (session.exec(select(func.max(ScriptEnvironmentVersion.number))
                           .where(ScriptEnvironmentVersion.environment_id == env.id)).one() or 0) + 1
    v = ScriptEnvironmentVersion(environment_id=env.id, number=number, language_version=language_version,
                                 base_image=_language_version(env.language, language_version), packages=packages,
                                 created_by=who.email)
    session.add(v)
    return v


# ── the section ──────────────────────────────────────────────────────────────
@router.get("", response_model=ScriptingOut)
def leggi(request: Request, session: Session = Depends(get_session), chi: User = Depends(require_observer)):
    settings = get_settings().scripts
    try:
        languages = {lang: sorted(v, key=lambda x: [int(p) if p.isdigit() else p for p in x.split(".")])
                     for lang, v in settings.images().items()}
    except ValueError:
        languages = {lang: [] for lang in LANGUAGES}
    envs = session.exec(select(ScriptEnvironment).order_by(func.lower(ScriptEnvironment.name))).all()
    return ScriptingOut(
        disabled_reason=script_builds.disabled_reason(), languages=languages, registry=settings.registry,
        storage=_storage_out(_storage(session)),
        environments=[EnvironmentSummary(**_summary(session, e, _versions(session, e.id))) for e in envs],
        editable=is_admin(session, chi) and not api_tokens.nome_corrente(request),
    )


# ── the bucket ───────────────────────────────────────────────────────────────
@router.put("/storage", response_model=StorageOut, dependencies=[Depends(_niente_token)])
def scrivi_storage(body: StorageUpdate, request: Request, session: Session = Depends(get_session),
                   admin: User = Depends(require_superuser)):
    _acceso()
    s = _storage(session)
    cambi: dict = {}
    for campo in ("endpoint", "region", "bucket", "access_key"):
        v = getattr(body, campo)
        if v is not None and v.strip() != getattr(s, campo):
            cambi[campo] = {"from": getattr(s, campo), "to": v.strip()}
            setattr(s, campo, v.strip())
    if s.endpoint:
        parti = urlsplit(s.endpoint)
        if parti.scheme not in ("http", "https") or not parti.hostname:
            raise HTTPException(status_code=422, detail="The endpoint is an http(s) address (empty = AWS)")
    if body.secret_key is not None:
        s.secret_key_enc = encrypt_secret(body.secret_key.strip()) if body.secret_key.strip() else None
        cambi["secret_key"] = "changed" if body.secret_key.strip() else "removed"
    if not cambi:
        return _storage_out(s)
    s.updated_at, s.updated_by = now_utc(), admin.email
    session.add(s)
    session.commit()
    session.refresh(s)
    audit.record_audit(session, actor=admin, action=audit.SCRIPTING_STORAGE_UPDATE, request=request,
                       target_type="scripting", target_label=s.bucket or None, detail={"changes": cambi})
    return _storage_out(s)


@router.post("/storage/test", response_model=StorageTestOut, dependencies=[Depends(_niente_token)])
async def prova_storage(request: Request, session: Session = Depends(get_session),
                        admin: User = Depends(require_superuser)):
    """Writes, reads and deletes a probe object with the key, from the engine (the
    workers write there the same way), and says whether the key can list the bucket."""
    s = _storage(session)
    if not (s.bucket and s.access_key and s.secret_key_enc):
        raise HTTPException(status_code=422, detail="The bucket, the access key and the secret key are needed")
    resp = await get_engine_client().post("/scripts/storage/test", json={"connection": {
        "endpoint_url": s.endpoint, "access_key": s.access_key, "secret_key_encrypted": s.secret_key_enc,
        "region": s.region, "bucket": s.bucket}})
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        out = StorageTestOut(ok=False, message=str(detail)[:500])
    else:
        out = StorageTestOut(**resp.json())
    audit.record_audit(session, actor=admin, action=audit.SCRIPTING_STORAGE_TEST, request=request,
                       outcome="success" if out.ok else "failure", target_type="scripting", target_label=s.bucket,
                       detail={"can_list": out.can_list})
    return out


# ── environments ─────────────────────────────────────────────────────────────
@router.get("/environments/{environment_id}", response_model=EnvironmentOut)
def leggi_ambiente(environment_id: int, session: Session = Depends(get_session), _chi: User = Depends(require_observer)):
    return _out(session, _environment(session, environment_id))


@router.post("/environments", response_model=EnvironmentOut, status_code=201, dependencies=[Depends(_niente_token)])
def crea_ambiente(body: EnvironmentCreate, request: Request, session: Session = Depends(get_session),
                  admin: User = Depends(require_superuser)):
    _acceso()
    if body.language not in LANGUAGES:
        raise HTTPException(status_code=422, detail="The language is python or r")
    _language_version(body.language, body.language_version)
    env = ScriptEnvironment(
        name=_name(session, body.name), description=body.description.strip(), language=body.language,
        enabled=body.enabled, cpu_request=body.cpu_request, cpu_limit=body.cpu_limit,
        memory_request=body.memory_request, memory_limit=body.memory_limit,
        max_timeout_seconds=body.max_timeout_seconds, created_by=admin.email, updated_by=admin.email)
    _resources(env)
    packages = _packages(body.language, body.packages)
    session.add(env)
    session.flush()
    _groups(session, env, body.group_ids)
    v = _new_version(session, env, body.language_version, packages, admin)
    session.commit()
    audit.record_audit(session, actor=admin, action=audit.SCRIPTING_ENV_CREATE, request=request,
                       target_type="script_environment", target_id=env.id, target_label=env.name,
                       detail={"language": env.language, "language_version": v.language_version,
                               "packages": packages.splitlines(), "groups": body.group_ids})
    return _out(session, env)


@router.put("/environments/{environment_id}", response_model=EnvironmentOut, dependencies=[Depends(_niente_token)])
def cambia_ambiente(environment_id: int, body: EnvironmentUpdate, request: Request,
                    session: Session = Depends(get_session), admin: User = Depends(require_superuser)):
    _acceso()
    env = _environment(session, environment_id)
    cambi: dict = {}
    if body.name is not None and body.name.strip() != env.name:
        cambi["name"] = {"from": env.name, "to": _name(session, body.name, env.id)}
        env.name = cambi["name"]["to"]
    for campo in ("description", "enabled", "cpu_request", "cpu_limit", "memory_request", "memory_limit",
                  "max_timeout_seconds"):
        v = getattr(body, campo)
        if isinstance(v, str):
            v = v.strip()
        if v is not None and v != getattr(env, campo):
            cambi[campo] = {"from": getattr(env, campo), "to": v}
            setattr(env, campo, v)
    _resources(env)
    if body.group_ids is not None:
        prima, _ = _group_names(session, env.id)
        if sorted(set(body.group_ids)) != sorted(prima):
            cambi["groups"] = {"from": prima, "to": sorted(set(body.group_ids))}
        _groups(session, env, body.group_ids)
    latest = next(iter(_versions(session, env.id)), None)
    lv = body.language_version if body.language_version is not None else (latest.language_version if latest else None)
    pk = _packages(env.language, body.packages) if body.packages is not None else (latest.packages if latest else "")
    new_version = None
    if lv is None:
        raise HTTPException(status_code=422, detail="The environment needs a language version")
    if latest is None or lv != latest.language_version or pk != latest.packages:
        new_version = _new_version(session, env, lv, pk, admin)
        cambi["build"] = {"language_version": lv, "packages": pk.splitlines()}
    if not cambi:
        return _out(session, env)
    env.updated_at, env.updated_by = now_utc(), admin.email
    session.add(env)
    session.commit()
    audit.record_audit(session, actor=admin, action=audit.SCRIPTING_ENV_UPDATE, request=request,
                       target_type="script_environment", target_id=env.id, target_label=env.name,
                       detail={"changes": cambi, **({"version": new_version.number} if new_version else {})})
    return _out(session, env)


@router.post("/environments/{environment_id}/rebuild", response_model=EnvironmentOut,
             dependencies=[Depends(_niente_token)])
def ricostruisci(environment_id: int, request: Request, session: Session = Depends(get_session),
                 admin: User = Depends(require_superuser)):
    """A new version from the same language version and packages as the latest: after
    a failed build, or to start again from an updated base image."""
    _acceso()
    env = _environment(session, environment_id)
    latest = next(iter(_versions(session, env.id)), None)
    if latest is None:
        raise HTTPException(status_code=422, detail="The environment has no version to build again")
    if latest.status in IN_PROGRESS:
        raise HTTPException(status_code=409, detail=f"Version {latest.number} is still being built")
    v = _new_version(session, env, latest.language_version, latest.packages, admin)
    env.updated_at, env.updated_by = now_utc(), admin.email
    session.add(env)
    session.commit()
    audit.record_audit(session, actor=admin, action=audit.SCRIPTING_ENV_UPDATE, request=request,
                       target_type="script_environment", target_id=env.id, target_label=env.name,
                       detail={"changes": {"build": "rebuild"}, "version": v.number})
    return _out(session, env)


@router.post("/environments/{environment_id}/versions/{version_id}/current", response_model=EnvironmentOut,
             dependencies=[Depends(_niente_token)])
def rendi_corrente(environment_id: int, version_id: int, request: Request, session: Session = Depends(get_session),
                   admin: User = Depends(require_superuser)):
    """Runs go back (or forward) to a version already built: its image is still in
    the registry."""
    _acceso()
    env = _environment(session, environment_id)
    v = session.get(ScriptEnvironmentVersion, version_id)
    if v is None or v.environment_id != env.id:
        raise HTTPException(status_code=404, detail="Version not found")
    if v.status != "ready":
        raise HTTPException(status_code=409, detail="Only a version built and checked can run scripts")
    if env.current_version_id == v.id:
        return _out(session, env)
    prima = env.current_version_id
    env.current_version_id, env.updated_at, env.updated_by = v.id, now_utc(), admin.email
    session.add(env)
    session.commit()
    previous = session.get(ScriptEnvironmentVersion, prima) if prima else None
    audit.record_audit(session, actor=admin, action=audit.SCRIPTING_VERSION_CURRENT, request=request,
                       target_type="script_environment", target_id=env.id, target_label=env.name,
                       detail={"from": previous.number if previous else None, "to": v.number})
    return _out(session, env)


@router.delete("/environments/{environment_id}", status_code=204, dependencies=[Depends(_niente_token)])
async def cancella_ambiente(environment_id: int, request: Request, session: Session = Depends(get_session),
                            admin: User = Depends(require_superuser)):
    env = _environment(session, environment_id)
    usati = flows_using(session, env.id)
    if usati:
        raise HTTPException(status_code=409, detail={"code": "in_use", "flows": usati})
    from app.services.saved_scripts import scripts_using_environment

    scripts = scripts_using_environment(session, env.id)
    if scripts:
        raise HTTPException(status_code=409, detail={"code": "in_use", "flows": [], "scripts": scripts})
    if not script_builds.disabled_reason():
        try:
            await script_builds.cancel_environment(env.id)
        except KubeError as e:
            if e.status != 404:
                raise HTTPException(status_code=502, detail=f"The builds in progress could not be stopped: {e}")
    nome = env.name
    for model in (ScriptEnvironmentVariable, ScriptEnvironmentGroup, ScriptEnvironmentVersion):
        for row in session.exec(select(model).where(model.environment_id == env.id)).all():  # type: ignore[attr-defined]
            session.delete(row)
    session.flush()
    session.delete(env)
    session.commit()
    audit.record_audit(session, actor=admin, action=audit.SCRIPTING_ENV_DELETE, request=request,
                       target_type="script_environment", target_id=environment_id, target_label=nome)


# ── variables ────────────────────────────────────────────────────────────────
def _variable_name(name: str) -> str:
    if not VARIABLE_NAME.match(name) or len(name) > 128:
        raise HTTPException(status_code=422, detail="A variable name is in capitals, digits and _, "
                                                    "and does not start with a digit")
    if name.startswith(RESERVED_PREFIXES):
        raise HTTPException(status_code=422, detail=f"Names starting with {' or '.join(RESERVED_PREFIXES)} "
                                                    "are set by Tabularia")
    return name


@router.put("/environments/{environment_id}/variables/{name}", response_model=EnvironmentOut,
            dependencies=[Depends(_niente_token)])
def scrivi_variabile(environment_id: int, name: str, body: VariableSet, request: Request,
                     session: Session = Depends(get_session), admin: User = Depends(require_superuser)):
    _acceso()
    env = _environment(session, environment_id)
    name = _variable_name(name)
    var = session.exec(select(ScriptEnvironmentVariable).where(
        ScriptEnvironmentVariable.environment_id == env.id, ScriptEnvironmentVariable.name == name)).first()
    nuova = var is None
    if body.from_tabularia:
        # the marked exception: the value is the name of one of Tabularia's own variables
        if body.value is None or not VARIABLE_NAME.match(body.value.strip()):
            raise HTTPException(status_code=422, detail="Give the name of the Tabularia variable to hand over")
    if body.value is None and (nuova or not var.secret or not body.secret or var.from_tabularia != body.from_tabularia):
        raise HTTPException(status_code=422, detail="The variable needs a value")
    var = var or ScriptEnvironmentVariable(environment_id=env.id, name=name)
    var.from_tabularia = body.from_tabularia
    var.secret = body.secret and not body.from_tabularia
    if body.value is not None:
        if var.secret:
            var.value, var.value_enc = "", encrypt_secret(body.value)
        else:
            var.value, var.value_enc = body.value.strip() if body.from_tabularia else body.value, None
    var.updated_at, var.updated_by = now_utc(), admin.email
    session.add(var)
    env.updated_at, env.updated_by = now_utc(), admin.email
    session.add(env)
    session.commit()
    detail = {"name": name, "secret": var.secret, "from_tabularia": var.from_tabularia, "new": nuova}
    if var.from_tabularia:
        detail["tabularia_variable"] = var.value  # a name, not a value
    audit.record_audit(session, actor=admin, action=audit.SCRIPTING_VARIABLE_SET, request=request,
                       target_type="script_environment", target_id=env.id, target_label=env.name, detail=detail)
    return _out(session, env)


@router.delete("/environments/{environment_id}/variables/{name}", response_model=EnvironmentOut,
               dependencies=[Depends(_niente_token)])
def cancella_variabile(environment_id: int, name: str, request: Request, session: Session = Depends(get_session),
                       admin: User = Depends(require_superuser)):
    env = _environment(session, environment_id)
    var = session.exec(select(ScriptEnvironmentVariable).where(
        ScriptEnvironmentVariable.environment_id == env.id, ScriptEnvironmentVariable.name == name)).first()
    if var is None:
        raise HTTPException(status_code=404, detail="Variable not found")
    session.delete(var)
    env.updated_at, env.updated_by = now_utc(), admin.email
    session.add(env)
    session.commit()
    audit.record_audit(session, actor=admin, action=audit.SCRIPTING_VARIABLE_DELETE, request=request,
                       target_type="script_environment", target_id=env.id, target_label=env.name,
                       detail={"name": name})
    return _out(session, env)


# ── package search ───────────────────────────────────────────────────────────
class PackageHit(BaseModel):
    name: str
    version: Optional[str]  # R: the current one; Python: the index does not say


class PackageVersion(BaseModel):
    version: str
    pre: bool
    # Python: a wheel exists for the environment's Python (False = it compiles from
    # source during the build, which often fails); None = not known (R, no version given)
    wheel: Optional[bool]


class PackageVersionsOut(BaseModel):
    name: str
    versions: list[PackageVersion]


_PACKAGE_NAME = {"python": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"),
                 "r": re.compile(r"^[A-Za-z][A-Za-z0-9.]{0,199}$")}


def _language(language: str) -> str:
    if language not in LANGUAGES:
        raise HTTPException(status_code=422, detail="The language is python or r")
    return language


@router.get("/packages", response_model=list[PackageHit])
async def cerca_pacchetti(language: str, q: str = "", _admin: User = Depends(require_superuser)):
    """The packages of PyPI or CRAN (or their mirrors) whose name holds `q`."""
    try:
        return await script_packages.search(_language(language), q[:200])
    except script_packages.PackageIndexError as e:
        raise HTTPException(status_code=502, detail=f"The package index could not be read: {e}")


@router.get("/packages/{language}/{name}", response_model=PackageVersionsOut)
async def versioni_pacchetto(language: str, name: str, python: Optional[str] = None,
                             _admin: User = Depends(require_superuser)):
    """The versions of a package, newest first; for Python, with `python=3.12`, whether
    each has a wheel for that Python."""
    language = _language(language)
    if not _PACKAGE_NAME[language].match(name):
        raise HTTPException(status_code=422, detail="Not a package name")
    try:
        versions = (await script_packages.python_versions(name, python or "") if language == "python"
                    else await script_packages.r_versions(name))
    except script_packages.PackageIndexError as e:
        raise HTTPException(status_code=502, detail=f"The package index could not be read: {e}")
    if not versions:
        raise HTTPException(status_code=404, detail=f"No package «{name}» in the index")
    return PackageVersionsOut(name=name, versions=versions)


# ── for the flow editor ──────────────────────────────────────────────────────
class EditorVariable(BaseModel):
    name: str
    secret: bool
    value: Optional[str]  # None for a secret one
    from_tabularia: bool


class EditorEnvironment(BaseModel):
    id: int
    name: str
    description: str
    language: str
    language_version: Optional[str]  # of the version in use
    version: Optional[int]
    ready: bool  # a version is built and the environment is enabled: its scripts can run
    max_timeout_seconds: int
    variables: list[EditorVariable]
    packages: list[str]  # what the version in use holds, as its lock says


class EditorScripting(BaseModel):
    disabled_reason: Optional[str]
    storage_ready: bool
    environments: list[EditorEnvironment]
    tabularia_variables: list[str]
    lint: bool  # the editor may ask POST /scripting/lint


@editor_router.get("", response_model=EditorScripting)
def editor_ambienti(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """The environments this person may write Script nodes with (an administrator:
    all of them), with what a script finds in its environment."""
    from app.services.script_nodes import can_use
    from app.services.script_runs import TABULARIA_VARIABLES

    storage = _storage(session)
    out = []
    for env in session.exec(select(ScriptEnvironment).order_by(func.lower(ScriptEnvironment.name))).all():
        if not can_use(session, user, env):
            continue
        current = session.get(ScriptEnvironmentVersion, env.current_version_id) if env.current_version_id else None
        out.append(EditorEnvironment(
            id=env.id, name=env.name, description=env.description, language=env.language,
            language_version=current.language_version if current else None,
            version=current.number if current else None,
            ready=bool(env.enabled and current is not None and current.status == "ready"),
            max_timeout_seconds=env.max_timeout_seconds,
            variables=[EditorVariable(name=v.name, secret=v.secret, value=None if v.secret else v.value,
                                      from_tabularia=v.from_tabularia) for v in _variables(session, env.id)],
            packages=(current.lock or "").splitlines() if current else [],
        ))
    return EditorScripting(disabled_reason=script_builds.disabled_reason(),
                           storage_ready=bool(storage.bucket and storage.access_key and storage.secret_key_enc),
                           environments=out, tabularia_variables=list(TABULARIA_VARIABLES),
                           lint=get_settings().scripts.lint)


@editor_router.get("/environments/{environment_id}/completions")
def editor_completamenti(environment_id: int, session: Session = Depends(get_session),
                         user: User = Depends(get_current_user)) -> dict:
    """{module: [public names]} of the version in use: what the code editor offers
    after `import polars as pl` and `pl.`, or `dplyr::`."""
    from app.services.script_nodes import can_use

    env = _environment(session, environment_id)
    if not can_use(session, user, env):
        raise HTTPException(status_code=403, detail="You cannot write scripts for this environment")
    current = session.get(ScriptEnvironmentVersion, env.current_version_id) if env.current_version_id else None
    if current is None or not current.completions:
        return {}
    try:
        return json.loads(current.completions)
    except ValueError:
        return {}


class LintIn(BaseModel):
    environment_id: int
    path: str = Field(min_length=1, max_length=900)
    content: str
    files: list[str] = Field(default_factory=list, max_length=50)  # the node's file paths: its own modules


class LintDiagnostic(BaseModel):
    line: int
    column: int
    end_line: int
    end_column: int
    code: str
    message: str
    severity: str  # error | warning


class LintOut(BaseModel):
    diagnostics: list[LintDiagnostic]
    skipped: bool = False  # too many checks from this person this second: the next pause checks again


@editor_router.post("/lint", response_model=LintOut)
def editor_controllo(body: LintIn, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """What Ruff finds in one Python file of a Script node, for the editor to underline,
    and the imports the environment does not have (services/script_lint.py). Nothing
    for R, and nothing when SCRIPTS__LINT is off."""
    from app.services import script_lint
    from app.services.script_nodes import MAX_FILE_BYTES, can_use

    env = _environment(session, body.environment_id)
    if not can_use(session, user, env):
        raise HTTPException(status_code=403, detail="You cannot write scripts for this environment")
    if len(body.content.encode()) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"A file holds at most {MAX_FILE_BYTES // 1024} KB")
    if env.language != "python" or not get_settings().scripts.lint or not body.path.endswith(".py"):
        return LintOut(diagnostics=[])
    if not script_lint.allowed(user.id):
        return LintOut(diagnostics=[], skipped=True)
    current = session.get(ScriptEnvironmentVersion, env.current_version_id) if env.current_version_id else None
    found = script_lint.lint_python(body.path, body.content, current.language_version if current else None)
    try:
        installed = set(json.loads(current.completions)) if current and current.completions else set()
    except ValueError:
        installed = set()
    if installed:  # a version checked before the list existed says nothing
        found = sorted(found + script_lint.missing_imports(
            body.content, installed, script_lint.node_modules([body.path, *body.files]), env.name),
            key=lambda d: (d["line"], d["column"]))
    return LintOut(diagnostics=[LintDiagnostic(**d) for d in found])


# ── the editor's «Run» of a Script node, and what the nodes after it preview ──
class TrialOut(BaseModel):
    run_id: int


# the trials running in this process: a task nobody holds may be collected mid-run
_trials: set = set()


@editor_router.post("/flows/{flow_id}/nodes/{node_id}/run", response_model=TrialOut, status_code=202)
async def prova_nodo(flow_id: int, node_id: str, request: Request, session: Session = Depends(get_session),
                     user: User = Depends(get_current_user)):
    """Runs a Script or Container node of the SAVED flow: the chain before it on the
    sources' development sample, the node as saved, the nodes after it not run. Its
    output is what the editor previews the nodes after it from. Answers at once with
    the run to follow (GET /runs/{id}); one trial per node at a time."""
    import asyncio

    from app.deps.permissions import ensure_can
    from app.models.permission import Capability
    from app.services import container_nodes, script_nodes, script_runs

    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    ensure_can(session, user, flow.project_id, Capability.RUN)
    flow_access.ensure_can_open(session, user, flow.definition)
    definition = json.loads(flow.definition or "{}")
    node = next((n for n in definition.get("nodes") or [] if n.get("id") == node_id), None)
    kind = (node or {}).get("type")
    if node is None or kind not in ("script", "container"):
        raise HTTPException(status_code=404, detail="The node is not in the saved flow: save the flow first")
    if kind == "script":
        # a node by reference: the saved script's environment and settings
        node, _saved = script_nodes.resolved(session, node)
        if script_nodes.saved_script_id(node) is not None and _saved is None:
            raise HTTPException(status_code=422, detail="The node runs a saved script that no longer exists")
        env_id = (node.get("data") or {}).get("environmentId")
        env = session.get(ScriptEnvironment, env_id) if isinstance(env_id, int) else None
        if env is None:
            raise HTTPException(status_code=422, detail="Choose the node's environment, and save the flow")
        if not script_nodes.can_use(session, user, env):
            raise HTTPException(status_code=403, detail=f"You cannot run scripts in the environment «{env.name}»")
        label, detail = script_nodes.label(node), {"environment": env.name}
    else:
        if not get_settings().containers.enabled:
            raise HTTPException(status_code=409, detail="Containers are off on this installation")
        d = node.get("data") or {}
        if not isinstance(d.get("registryId"), int) or not d.get("repository"):
            raise HTTPException(status_code=422, detail="Choose the node's registry and image, and save the flow")
        label, detail = container_nodes.label(node), {"registry": d["registryId"], "image": d["repository"],
                                                      "tag": d.get("tag") or "latest"}
    for live in session.exec(select(Run).where(Run.flow_id == flow.id, Run.kind == kind,
                                               Run.trigger_type == "trial",
                                               Run.status.in_(("PENDING", "STARTED")))).all():  # type: ignore[attr-defined]
        if json.loads(live.script or "{}").get("node_id") != node_id:
            continue
        # a trial left behind by a process that stopped is closed now, not offered again
        live = await script_runs.close_if_orphaned(session, live)
        if live.status in ("PENDING", "STARTED"):
            return TrialOut(run_id=live.id)
    # the budget from the start: a trial whose process dies before it begins is closed too
    from app.services import container_runs

    budget = (container_runs.budget_seconds if kind == "container" else script_runs.budget_seconds)(node.get("data") or {})
    run = Run(kind=kind, flow_id=flow.id, task_id="", status="PENDING", launched_by=user.id,
              trigger_type="trial", input_key="", output_bucket="", output_key="",
              script=json.dumps({"node_id": node_id, "label": label, "trial": True, "budget_seconds": budget}))
    session.add(run)
    session.commit()
    session.refresh(run)
    audit.record_audit(session, actor=user, action=audit.SCRIPTING_TRIAL if kind == "script" else audit.CONTAINER_TRIAL,
                       request=request, target_type="flow", target_id=flow.id, target_label=flow.name,
                       detail={"run_id": run.id, "node": node_id, **detail})
    task = asyncio.create_task(script_runs.run_trial(run.id, user.id, node_id))
    _trials.add(task)
    task.add_done_callback(_trials.discard)
    return TrialOut(run_id=run.id)


@editor_router.get("/flows/{flow_id}/outputs")
def output_dei_nodi(flow_id: int, session: Session = Depends(get_session),
                    user: User = Depends(get_current_user)) -> dict:
    """Per Script or Container node of the flow: its latest run, and the output of its
    latest successful one (a run of the flow or a trial), which the editor previews the
    nodes after it from; `stale` when the saved node is no longer the one that produced it."""
    from app.deps.permissions import ensure_can
    from app.models.permission import Capability
    from app.services import container_nodes, script_nodes, script_runs

    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    flow_access.ensure_can_open(session, user, flow.definition)
    saved = {n.get("id"): script_nodes.fingerprint(session, n) for n in script_nodes.script_nodes(flow.definition)}
    saved |= {n.get("id"): container_nodes.fingerprint(n) for n in container_nodes.container_nodes(flow.definition)}
    out = script_runs.latest_outputs(session, flow.id, list(saved))
    for node_id, entry in out.items():
        for part in ("last", "output"):
            if entry[part]:
                entry[part]["stale"] = entry[part].pop("fingerprint", None) != saved.get(node_id)
    return {k: v for k, v in out.items() if k in saved}
