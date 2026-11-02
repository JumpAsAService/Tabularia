"""Engine settings (Administration → Settings): the parameters the administrator changes
without touching the environment and without restarts.

Read for administrators and observers; write for administrators only, never with an
API token, and every change in the audit with the value before and after (a password:
only whether it was set). The external ClickHouse's password arrives here in clear, is
encrypted at once with the key gateway and engine share, and is never sent back. With
ENGINE__SETTINGS_PANEL=false they are seen and not changed. Which parameters, with
what limits and how they reach the engine's processes: services/engine_panel_settings.py.
"""
from datetime import datetime, timezone
from typing import Any, Optional, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt, StrictStr
from sqlmodel import Session

from app.core.crypto import encrypt_secret
from app.core.engine_client import get_engine_client
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import EngineSetting, User
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit
from app.services import engine_panel_settings as rm
from app.services import settings_rollout
from app.services.permissions import is_admin
from app.services.masking import mask_email

router = APIRouter(route_class=ReleasingRoute, prefix="/admin/engine-settings", tags=["settings"])


class SettingOut(BaseModel):
    key: str
    category: str
    unit: str  # rows | seconds | bytes | threads | …
    kind: str = "int"  # int | float | bool | str | choice | secret
    choices: list[str] = []  # choice: the values allowed
    optional: bool = False  # str: it may be empty
    format: str = ""  # str: the shape it must have (host, name, url, user), checked by the panel too
    # the components to roll out for it to apply (worker, preview-worker, engine, beat); empty = live
    rollout: list[str] = []
    min: float
    max: float
    env_var: str
    env_value: Any  # the environment's: the starting value
    value: Any  # the one set from the panel (None = the environment holds; a secret: true when set)
    applied: Any  # the one the engine is applying now (None = the environment)
    updated_at: Optional[UtcDateTime] = None
    updated_by: Optional[str] = None


class SettingsOut(BaseModel):
    items: list[SettingOut]
    editable: bool  # whoever looks may change them (administrator, panel on, not from a token)
    panel_enabled: bool  # ENGINE__SETTINGS_PANEL
    unavailable: list[str]  # categories of the engines this installation does not have
    refresh_seconds: float  # within how long every engine process sees a change


class SettingsUpdate(BaseModel):
    # only those that change, null goes back to the environment. Strict: a text does not
    # become a number, nor `true` a 1 without the engine seeing it (the engine checks the
    # type). A password in clear, at most 1024 characters
    values: dict[str, Optional[Union[StrictBool, StrictInt, StrictFloat, StrictStr]]] = Field(max_length=80)


def _no_token(request: Request, _admin: User = Depends(require_superuser)) -> None:
    """Before reading the body: a token gets 403, even when the body is wrong. AFTER the
    authentication (the dependency above): it is the one that writes into the request
    whether it came with a token — before, it would not be known, and the token would get
    through (it happened while trying, 2026-10-12)."""
    from app.routes.api_tokens import deny_if_from_token

    deny_if_from_token(request, api_tokens.NO_SETTINGS_FROM_TOKEN)


SECRET_MAX = 1024
# the connection's passwords (the engine's catalog says `kind: secret`): the test does not
# read the catalog first
CLICKHOUSE_SECRETS = ("clickhouse_external.password", "clickhouse_external.ai_password")
# the engine gives each step at most 30 s: signing in twice, the database, the storage
CHECK_TIMEOUT = httpx.Timeout(150.0, connect=5.0)


def _masked(kind: Optional[str], v: Any) -> Any:
    """A value as it leaves the gateway, to the panel or to the audit: a secret only as set
    (true) or not, never its token nor its clear text."""
    return bool(v) if kind == "secret" and v is not None else v


def _sealed(kinds: dict[str, str], values: dict[str, Any]) -> tuple[dict[str, Any], list[dict]]:
    """The passwords typed in clear, encrypted with the shared key: so they are sent to the
    engine, kept in Valkey and saved. (values, errors)"""
    out, errors = {}, []
    for k, v in values.items():
        if kinds.get(k) == "secret" and v is not None:
            if not isinstance(v, str):
                errors.append({"key": k, "code": "text"})
                continue
            if len(v) > SECRET_MAX:
                errors.append({"key": k, "code": "too_long"})
                continue
            v = encrypt_secret(v)
        out[k] = v
    return out, errors


# an engine from before these settings (a half-done update, or done before the gateway):
# it answers 404, and «does not answer» would be the wrong diagnosis
OLD_ENGINE = "The engine is an older version that does not support the settings panel: update it"


async def _state(session: Session, asker: User, request: Request) -> SettingsOut:
    try:
        cat = await rm.catalog()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=503, detail=OLD_ENGINE)
        raise HTTPException(status_code=503, detail="The engine is not responding: the settings cannot be read right now")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="The engine is not responding: the settings cannot be read right now")
    rows = rm.saved(session)
    full = is_admin(session, asker)
    items = []
    for v in cat["items"]:
        if v["key"] in rm.FROM_GATEWAY:
            continue  # in the gateway's box (routes/gateway_settings.py)
        r = rows.get(v["key"])
        changed_by = r.updated_by if r is not None else None
        items.append(SettingOut(
            key=v["key"], category=v["category"], unit=v["unit"], min=v["min"], max=v["max"],
            kind=v.get("kind", "int"), rollout=v.get("rollout", []),
            choices=v.get("choices", []), optional=v.get("optional", False), format=v.get("format", ""),
            env_var=v["env_var"], env_value=v["env_value"],
            value=_masked(v.get("kind"), rm.value(r)) if r is not None and rm.from_panel() else None,
            applied=v["value"],
            updated_at=r.updated_at if r is not None else None,
            updated_by=(changed_by if full else mask_email(changed_by)) if changed_by else None,
        ))
    return SettingsOut(
        items=items, editable=full and rm.from_panel() and not api_tokens.current_name(request),
        panel_enabled=rm.from_panel(), unavailable=cat.get("unavailable", []),
        refresh_seconds=cat.get("refresh_seconds", 10),
    )


@router.get("", response_model=SettingsOut)
async def read(request: Request, session: Session = Depends(get_session), asker: User = Depends(require_observer)):
    return await _state(session, asker, request)


@router.put("", response_model=SettingsOut, dependencies=[Depends(_no_token)])
async def write(
    body: SettingsUpdate, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Changes the ones given. First the engine checks and applies them (all or nothing: a
    422 says which and why, with a code the panel translates), then they are saved: what
    is read here is always what the engine applies."""
    _no_token(request)  # the dependency did it already: it stays here in case it is removed one day
    if not rm.from_panel():
        raise HTTPException(status_code=403, detail="On this installation the engine settings can only be changed in the environment (ENGINE__SETTINGS_PANEL=false)")
    # in line with the other saves and with the periodic round: read, send and save holding
    # the lock, so the engine always receives the whole set
    if not await rm.lock(session):
        raise HTTPException(status_code=409, detail="Another change to the settings is in progress: try again in a moment")
    try:
        cat_items = (await rm.catalog())["items"]
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=503, detail=OLD_ENGINE + ": nothing was changed")
        raise HTTPException(status_code=503, detail="The engine is not responding: nothing was changed")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="The engine is not responding: nothing was changed")
    kinds = {v["key"]: v.get("kind", "int") for v in cat_items if v["key"] not in rm.FROM_GATEWAY}
    unknown = [{"key": k, "code": "unknown"} for k in body.values if k not in kinds]
    asked, wrong = _sealed(kinds, body.values)
    if unknown or wrong:
        return JSONResponse(status_code=422, content={"detail": {"errors": unknown + wrong}})
    rows = rm.saved(session)
    before = {k: rm.value(r) for k, r in rows.items() if k not in rm.FROM_GATEWAY}
    after = dict(before)
    for k, v in asked.items():
        if v is None:
            after.pop(k, None)
        else:
            after[k] = v
    # a password typed again is a change (its token is new each time)
    changes = {k: {"from": before.get(k), "to": after.get(k)} for k in asked if before.get(k) != after.get(k)}
    if not changes:
        return await _state(session, admin, request)
    try:
        # start from the saved ones the engine still accepts: one no longer valid (the
        # environment changed underneath) must not block the changes to the others
        valid = await rm.push_discarding(before)
        sending = {**{k: v for k, v in valid.items() if k not in asked},
                 **{k: v for k, v in asked.items() if v is not None}}
        r = await rm.push(sending, changed=list(changes))
        errors = ((r.json().get("detail") or {}).get("errors") or []) if r.status_code == 422 else []
        if errors and {e.get("key") for e in errors} <= set(rm.FROM_GATEWAY):
            # an engine older than the upload limit from the panel: without it
            r = await rm.push(sending, changed=list(changes), with_gateway=False)
    except (httpx.HTTPError, RuntimeError):
        raise HTTPException(status_code=503, detail="The engine is not responding: nothing was changed")
    if r.status_code == 422:
        return JSONResponse(status_code=422, content=r.json())
    if r.status_code == 404:
        raise HTTPException(status_code=503, detail=OLD_ENGINE + ": nothing was changed")
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"The engine answered {r.status_code}: nothing was changed")
    at = datetime.now(timezone.utc).replace(tzinfo=None)
    # the ones read only at start: their components run the old value until they start again
    to_restart = {v["key"]: v.get("rollout") or [] for v in (r.json() or {}).get("items", [])}
    settings_rollout.mark_changed(session, (c for k in changes for c in to_restart.get(k, [])))
    for k in changes:
        if k in after:
            row = rows.get(k) or EngineSetting(key=k, value=0, updated_at=at)
            (row.value, row.value_json), row.updated_at, row.updated_by = rm.columns(after[k]), at, admin.email
            session.add(row)
        elif k in rows:
            session.delete(rows[k])
    try:
        session.commit()
    except Exception:
        session.rollback()
        # the engine has them already: the ones of before are put back, which are the saved ones
        try:
            await rm.push_discarding(before)
        except (httpx.HTTPError, RuntimeError):
            pass  # the periodic round (push_loop) puts them right
        raise
    audit.record_audit(
        session, actor=admin, action=audit.ENGINE_SETTINGS_UPDATE, request=request,
        target_type="engine_settings", target_label="engine",
        detail={"changes": {k: {side: _masked(kinds.get(k), x) for side, x in c.items()} for k, c in changes.items()}},
    )
    return await _state(session, admin, request)


class CheckRequest(BaseModel):
    # the connection as the panel's fields hold it now; a password in clear (missing = the saved one)
    values: dict[str, Optional[Union[StrictBool, StrictInt, StrictStr]]] = Field(default_factory=dict, max_length=20)


@router.post("/clickhouse-check", dependencies=[Depends(_no_token)])
async def clickhouse_check(body: CheckRequest, request: Request, admin: User = Depends(require_superuser)):
    """«Test connection»: the engine tries the external ClickHouse with these values, step by
    step, and saves nothing. Administrators only: it makes the engine open a connection
    to the address given."""
    _no_token(request)
    values = {k: v for k, v in body.values.items() if v is not None}
    sealed, wrong = _sealed({k: "secret" for k in CLICKHOUSE_SECRETS}, values)
    if wrong:
        return JSONResponse(status_code=422, content={"detail": {"errors": wrong}})
    try:
        r = await get_engine_client().post("/settings/clickhouse-check", json={"values": sealed}, timeout=CHECK_TIMEOUT)
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="The engine is not responding: the connection was not tested")
    if r.status_code == 404:
        raise HTTPException(status_code=503, detail=OLD_ENGINE)
    if r.status_code == 422:
        return JSONResponse(status_code=422, content=r.json())
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"The engine answered {r.status_code}: the connection was not tested")
    return r.json()
