"""Engine settings (Administration → Settings): the parameters the administrator changes
without touching the environment and without restarts.

Read for administrators and observers; write for administrators only, never with an
API token, and every change in the audit with the value before and after. With
ENGINE__SETTINGS_PANEL=false they are seen and not changed. Which parameters, with
what limits and how they reach the engine's processes: services/engine_panel_settings.py.
"""
from datetime import datetime, timezone
from typing import Any, Optional, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt
from sqlmodel import Session

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
    kind: str = "int"  # int | float | bool
    # the components to roll out for it to apply (worker, preview-worker, engine, beat); empty = live
    rollout: list[str] = []
    min: float
    max: float
    env_var: str
    env_value: Any  # the environment's: the starting value
    value: Any  # the one set from the panel (None = the environment holds)
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
    # type)
    values: dict[str, Optional[Union[StrictBool, StrictInt, StrictFloat]]] = Field(max_length=80)


def _no_token(request: Request, _admin: User = Depends(require_superuser)) -> None:
    """Before reading the body: a token gets 403, even when the body is wrong. AFTER the
    authentication (the dependency above): it is the one that writes into the request
    whether it came with a token — before, it would not be known, and the token would get
    through (it happened while trying, 2026-10-12)."""
    from app.routes.api_tokens import deny_if_from_token

    deny_if_from_token(request, api_tokens.NO_SETTINGS_FROM_TOKEN)


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
            env_var=v["env_var"], env_value=v["env_value"],
            value=rm.value(r) if r is not None and rm.from_panel() else None,
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
        note = {v["key"] for v in (await rm.catalog())["items"]} - set(rm.FROM_GATEWAY)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=503, detail=OLD_ENGINE + ": nothing was changed")
        raise HTTPException(status_code=503, detail="The engine is not responding: nothing was changed")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="The engine is not responding: nothing was changed")
    unknown = [k for k in body.values if k not in note]
    if unknown:
        return JSONResponse(status_code=422, content={"detail": {"errors": [{"key": k, "code": "unknown"} for k in unknown]}})
    rows = rm.saved(session)
    before = {k: rm.value(r) for k, r in rows.items() if k not in rm.FROM_GATEWAY}
    after = dict(before)
    for k, v in body.values.items():
        if v is None:
            after.pop(k, None)
        else:
            after[k] = v
    changes = {k: {"from": before.get(k), "to": after.get(k)} for k in body.values if before.get(k) != after.get(k)}
    if not changes:
        return await _state(session, admin, request)
    try:
        # start from the saved ones the engine still accepts: one no longer valid (the
        # environment changed underneath) must not block the changes to the others
        valid = await rm.push_discarding(before)
        sending = {**{k: v for k, v in valid.items() if k not in body.values},
                 **{k: v for k, v in body.values.items() if v is not None}}
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
        target_type="engine_settings", target_label="engine", detail={"changes": changes},
    )
    return await _state(session, admin, request)
