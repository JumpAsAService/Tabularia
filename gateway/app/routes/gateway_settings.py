"""The gateway's settings (Administration → Settings): the ones an administrator changes
without touching the deployment. Which ones, their ranges, how they reach every process:
app/core/overrides.py; the whole design: docs/specs/panel-settings.md.

Read by administrators and observers; written only by administrators, never with an API
token, every change in the audit log with the value before and after. With
ENGINE__SETTINGS_PANEL=false they are shown and not applied, as the engine's.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core import overrides as ov
from app.core.config import get_settings, settings_from_env
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import GatewaySetting, User
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit
from app.services import engine_panel_settings as rm
from app.services import settings_rollout as sr
from app.services.masking import mask_email
from app.services.permissions import is_admin

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ReleasingRoute, prefix="/admin/gateway-settings", tags=["settings"])

# not the engine's (engine_panel_settings.LUCCHETTO): the two panels save independently
LOCK = 7_402_120


class SettingOut(BaseModel):
    key: str
    group: str
    kind: str  # int | float | bool | str | timezone | cpu | memory
    unit: str
    min: Optional[float] = None
    max: Optional[float] = None
    max_length: Optional[int] = None
    rollout: list[str]  # the components to roll out for it to apply; empty = live
    env_var: str
    env_value: Any  # the environment's: the starting value
    value: Any = None  # the panel's (None = the environment's applies)
    applied: Any = None  # what this process applies now
    started_with: Any = None  # rollout ones: what this process started with
    updated_at: Optional[UtcDateTime] = None
    updated_by: Optional[str] = None


class SettingsOut(BaseModel):
    items: list[SettingOut]
    editable: bool
    panel_enabled: bool
    refresh_seconds: float


class SettingsUpdate(BaseModel):
    # only the ones that change; null goes back to the environment's
    values: dict[str, Any] = Field(max_length=80)


def _no_token(request: Request, _admin: User = Depends(require_superuser)) -> None:
    """After authentication (the dependency above), before the body is read."""
    from app.routes.api_tokens import deny_if_from_token

    deny_if_from_token(request, api_tokens.NO_SETTINGS_FROM_TOKEN)


def available(item: ov.Item) -> bool:
    """The groups of a feature this installation does not have are not shown."""
    s = get_settings()
    if item.when == "sso":
        return s.oidc.enabled
    if item.when == "kubernetes":
        return Path(s.kubernetes.token_file).exists()
    if item.when == "scripts":
        return s.scripts.enabled
    if item.when == "containers":
        return s.containers.enabled
    return True


def _json(v: Any) -> Any:
    return v if v is None or isinstance(v, (bool, int, float, str)) else str(v)


def _rows(session: Session) -> dict[str, GatewaySetting]:
    return {r.key: r for r in session.exec(select(GatewaySetting)).all()}


def _decode(row: GatewaySetting) -> Any:
    try:
        return json.loads(row.value)
    except ValueError:
        return None


def _state(session: Session, chi: User, request: Request) -> SettingsOut:
    rows = _rows(session)
    # The rows just read can be newer than this process's copy, re-read every
    # REREAD_EVERY seconds: another gateway replica saved them a moment ago. In force here
    # at once, or the page showed them «not applied» until this replica read them too.
    ov.apply_rows({k: v for k, v in ((k, _decode(r)) for k, r in rows.items()) if v is not None})
    env, now = settings_from_env(), get_settings()
    full = is_admin(session, chi)
    items = []
    for v in ov.ITEMS:
        if not available(v):
            continue
        r = rows.get(v.key)
        by = r.updated_by if r is not None else None
        items.append(SettingOut(
            key=v.key, group=v.group, kind=v.kind, unit=v.unit, min=v.minimum, max=v.maximum,
            max_length=v.max_length if v.kind in ("str", "timezone", "cpu", "memory") else None,
            rollout=list(v.rollout), env_var=v.env_var, env_value=_json(ov.effective(env, v.key)),
            value=_decode(r) if r is not None and rm.from_panel() else None,
            applied=_json(ov.effective(now, v.key)),
            started_with=_json(ov.started_with(v.key)) if v.rollout else None,
            updated_at=r.updated_at if r is not None else None,
            updated_by=(by if full else mask_email(by)) if by else None,
        ))
    return SettingsOut(items=items, editable=full and rm.from_panel() and not api_tokens.current_name(request),
                       panel_enabled=rm.from_panel(), refresh_seconds=ov.REREAD_EVERY)


@router.get("", response_model=SettingsOut)
def read(request: Request, session: Session = Depends(get_session), chi: User = Depends(require_observer)):
    return _state(session, chi, request)


async def _stale_below_hard_limit(stale: int) -> Optional[dict]:
    """The gateway declares a run lost after ENGINE__RUN_STALE_TIMEOUT_SECONDS: it must
    stay above the engine's hard time limit plus a margin, or a run still alive would be
    closed as failed. The engine says which hard limit it applies."""
    key = "engine.run_stale_timeout_seconds"
    try:
        cat = await rm.catalog()
    except (httpx.HTTPError, ValueError):
        return {"key": key, "code": "engine_unreachable"}
    hard = next((i.get("value") if i.get("value") is not None else i.get("env_value")
                 for i in cat.get("items", []) if i.get("key") == "celery.task_time_limit"), None)
    if hard is not None and stale < hard + rm.MARGIN:
        return {"key": key, "code": "stale_below_hard_limit", "limit": hard + rm.MARGIN}
    return None


@router.put("", response_model=SettingsOut, dependencies=[Depends(_no_token)])
async def write(
    body: SettingsUpdate, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Changes the ones given, all or none: a 422 says which and why, with a code the
    panel translates. Saved, they apply at once in this process, within
    `REREAD_EVERY` seconds in the others (the rollout ones: when they start again)."""
    _no_token(request)
    if not rm.from_panel():
        raise HTTPException(status_code=403, detail="On this installation the settings can only be changed in the environment (ENGINE__SETTINGS_PANEL=false)")
    if not await rm.lock(session, key=LOCK):
        raise HTTPException(status_code=409, detail="Another change to the settings is in progress: try again in a moment")
    errors = [{"key": k, "code": "unknown" if k not in ov.PER_KEY else "unavailable"}
              for k in body.values if k not in ov.PER_KEY or not available(ov.PER_KEY[k])]
    if errors:
        return JSONResponse(status_code=422, content={"detail": {"errors": errors}})
    env = settings_from_env()
    rows = _rows(session)
    # the saved ones that no longer hold (the environment changed under them) do not
    # block the others: they are dropped as at every reading
    before, _ = ov.check({k: _decode(r) for k, r in rows.items()}, env)
    after = dict(before)
    for k, v in body.values.items():
        if v is None:
            after.pop(k, None)
        else:
            after[k] = v
    changed = {k for k, v in body.values.items() if v is not None}
    good, errs = ov.check(after, env, changed)
    # the errors of the ones sent, and of a saved one that the change would drop (going
    # back to the environment's pool can leave a saved number of executions beyond it):
    # refused, not dropped in silence
    errs = [e for e in errs if e["key"] in body.values or e["key"] in before]
    stale_key = "engine.run_stale_timeout_seconds"
    if not errs and stale_key in body.values:
        # set, or back to the environment's: either way it must stay above the engine's limit
        stale = good.get(stale_key, ov.effective(env, stale_key))
        e = await _stale_below_hard_limit(stale)
        if e:
            errs.append(e)
    if errs:
        return JSONResponse(status_code=422, content={"detail": {"errors": errs}})
    raw_before = {k: _decode(r) for k, r in rows.items()}
    changes = {k: {"from": raw_before.get(k), "to": good.get(k)} for k in body.values
               if raw_before.get(k) != good.get(k) or (k in rows) != (k in good)}
    if not changes:
        return _state(session, admin, request)
    at = datetime.now(timezone.utc).replace(tzinfo=None)
    sr.mark_changed(session, (c for k in changes for c in ov.PER_KEY[k].rollout))
    for k in changes:
        if k in good:
            row = rows.get(k) or GatewaySetting(key=k, value="null", updated_at=at)
            row.value, row.updated_at, row.updated_by = json.dumps(good[k]), at, admin.email
            session.add(row)
        elif k in rows:
            session.delete(rows[k])
    session.commit()
    audit.record_audit(
        session, actor=admin, action=audit.SETTINGS_UPDATE, request=request,
        target_type="settings", target_label="gateway", detail={"changes": changes},
    )
    ov.reload(session.get_bind())  # this process at once; the others within REREAD_EVERY
    if "app.timezone" in changes:
        from app.services.scheduler import recompute_all

        n = recompute_all(session)
        logger.info("settings: the schedules' time zone changed, %d next slots computed again", n)
    if set(changes) & set(rm.FROM_GATEWAY):
        try:  # the engine's barrier too, now rather than at the next round (rm.SPINGI_OGNI)
            await rm.one_round(session.get_bind())
        except Exception as e:  # noqa: BLE001 — engine down: the next round sends it
            logger.info("settings: the upload limit not sent to the engine yet (%s)", e)
    return _state(session, admin, request)


# ── the rollout of what reads a changed setting only at start ────────────────

rollout_router = APIRouter(route_class=ReleasingRoute, prefix="/admin/settings-rollout", tags=["settings"])


class RolloutComponent(BaseModel):
    component: str
    changed_at: Optional[UtcDateTime] = None
    requested_at: Optional[UtcDateTime] = None
    requested_by: Optional[str] = None
    pending: bool
    compose_service: str
    pods: Optional[int] = None
    ready: Optional[int] = None
    old: Optional[int] = None
    waiting_for: list[str] = []
    error: Optional[str] = None


class RolloutOut(BaseModel):
    mode: str  # kubernetes | manual | compose
    components: list[RolloutComponent]
    # for the command shown when the panel cannot restart the pods itself
    namespace: str = ""
    release: str = ""
    editable: bool


class RolloutRequest(BaseModel):
    components: list[str] = Field(min_length=1, max_length=len(sr.ORDER))


async def _rollout(session: Session, chi: User, request: Request) -> RolloutOut:
    st = await sr.status(session)
    full = is_admin(session, chi)
    for c in st["components"]:
        if c["requested_by"] and not full:
            c["requested_by"] = mask_email(c["requested_by"])
    return RolloutOut(mode=st["mode"], components=st["components"], namespace=st["namespace"], release=st["release"],
                      editable=full and rm.from_panel() and not api_tokens.current_name(request))


@rollout_router.get("", response_model=RolloutOut)
async def rollout_state(request: Request, session: Session = Depends(get_session), chi: User = Depends(require_observer)):
    return await _rollout(session, chi, request)


@rollout_router.post("", response_model=RolloutOut, dependencies=[Depends(_no_token)])
async def rollout_start(
    body: RolloutRequest, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Kubernetes: the orchestrator restarts their pods one at a time. Docker Compose, or
    Kubernetes without the chart's right: the administrator says they restarted them."""
    _no_token(request)
    if not rm.from_panel():
        raise HTTPException(status_code=403, detail="On this installation the settings can only be changed in the environment (ENGINE__SETTINGS_PANEL=false)")
    unknown = [c for c in body.components if c not in sr.ORDER]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown components: {', '.join(unknown)}")
    done = sr.request(session, body.components, admin.email)
    session.commit()
    audit.record_audit(session, actor=admin, action=audit.SETTINGS_ROLLOUT, request=request,
                       target_type="settings", target_label=", ".join(done),
                       detail={"components": done, "mode": sr.mode()})
    return await _rollout(session, admin, request)


@rollout_router.delete("/{component}", response_model=RolloutOut, dependencies=[Depends(_no_token)])
async def rollout_cancel(
    component: str, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Stops a rollout: the pods already restarted stay, the others keep the old value."""
    _no_token(request)
    if sr.cancel(session, component):
        session.commit()
        audit.record_audit(session, actor=admin, action=audit.SETTINGS_ROLLOUT_CANCEL, request=request,
                           target_type="settings", target_label=component, detail={"component": component})
    return await _rollout(session, admin, request)
