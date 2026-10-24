"""Backups (Administration → Backups): the automatic backup of this database into the
object storage, and the ones there (services/db_backup.py).

Read by administrators and observers (the observer sees no error text and downloads
nothing: a backup holds every account's e-mail address); changed, started and downloaded
by administrators only, never with an API token. Every download is in the audit log.
"""
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlmodel import Session

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.core.routing import RottaCheRilascia
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import DatabaseBackup, User
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit, db_backup
from app.services.permissions import is_admin
from app.services.schedule import ScheduleError, validate_schedule

router = APIRouter(route_class=RottaCheRilascia, prefix="/admin/backups", tags=["backups"])


class BackupItem(BaseModel):
    name: str
    size: int
    modified: Optional[str] = None


class BackupOut(BaseModel):
    available: bool
    disabled_reason: Optional[str]  # kubernetes | postgres | pg_dump
    enabled: bool
    schedule: str
    keep: int
    timezone: str
    defaults: dict
    changed: dict
    next_at: Optional[UtcDateTime]
    running_since: Optional[UtcDateTime]
    requested_at: Optional[UtcDateTime]
    last_ok_at: Optional[UtcDateTime]
    last_name: Optional[str]
    last_size: Optional[int]
    last_seconds: Optional[float]
    last_error: Optional[str]  # not to the observer
    last_error_at: Optional[UtcDateTime]
    # where they go (BACKUP__* on the engine) and the ones there
    bucket: Optional[str]
    prefix: Optional[str]
    same_as_data: Optional[bool]
    items: list[BackupItem]
    storage_error: Optional[str]
    editable: bool


class BackupUpdate(BaseModel):
    # only what changes; reset=[…] puts those back to the installation's value
    enabled: Optional[bool] = None
    schedule: Optional[str] = Field(default=None, max_length=120)
    keep: Optional[int] = Field(default=None, ge=1, le=1000)
    reset: list[str] = []


def _niente_token(request: Request, _admin: User = Depends(require_superuser)) -> None:
    from app.routes.api_tokens import nega_se_da_token

    nega_se_da_token(request, api_tokens.NIENTE_BACKUP_DA_TOKEN)


async def _out(session: Session, who: User) -> BackupOut:
    reason = db_backup.disabled_reason(session)
    r = db_backup.row(session)
    eff = db_backup.effective(r)
    full = is_admin(session, who)
    found = await db_backup.listing() if not reason else {"items": [], "error": None}
    return BackupOut(
        available=reason is None, disabled_reason=reason, enabled=eff["enabled"], schedule=eff["schedule"],
        keep=eff["keep"], timezone=get_settings().app.timezone, defaults=eff["defaults"], changed=eff["changed"],
        next_at=r.next_at if eff["enabled"] else None,
        running_since=r.running_since if db_backup.in_corso(r) else None, requested_at=r.requested_at,
        last_ok_at=r.last_ok_at, last_name=r.last_name, last_size=r.last_size, last_seconds=r.last_seconds,
        last_error=r.last_error if full else None, last_error_at=r.last_error_at,
        bucket=found.get("bucket"), prefix=found.get("prefix"), same_as_data=found.get("same_as_data"),
        items=[BackupItem(**i) for i in found.get("items") or []],
        storage_error=(found.get("error") if full else ("The backups could not be listed" if found.get("error") else None)),
        editable=full,
    )


@router.get("", response_model=BackupOut)
async def stato(session: Session = Depends(get_session), who: User = Depends(require_observer)):
    return await _out(session, who)


@router.put("", response_model=BackupOut, dependencies=[Depends(_niente_token)])
async def cambia(body: BackupUpdate, request: Request, session: Session = Depends(get_session),
                 admin: User = Depends(require_superuser)):
    r = db_backup.row(session)
    changes: dict = {}
    for name in body.reset:
        if name not in ("enabled", "schedule", "keep"):
            raise HTTPException(status_code=422, detail=f"Not a backup setting: {name}")
        changes[name] = None
    if body.enabled is not None:
        changes["enabled"] = body.enabled
    if body.schedule is not None:
        try:
            changes["schedule"] = validate_schedule(body.schedule)
        except ScheduleError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if body.keep is not None:
        changes["keep"] = body.keep
    if changes:
        for k, v in changes.items():
            setattr(r, k, v)
        r.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        r.updated_by = admin.email
        session.add(r)
        session.commit()
        db_backup.replan(session)
        audit.record_audit(session, actor=admin, action=audit.BACKUP_UPDATE, target_type="database_backup",
                           detail={k: v for k, v in changes.items()}, request=request)
    return await _out(session, admin)


@router.post("/run", response_model=BackupOut, dependencies=[Depends(_niente_token)])
async def adesso(request: Request, session: Session = Depends(get_session), admin: User = Depends(require_superuser)):
    """«Back up now»: the first orchestrator that looks (every few seconds) makes it."""
    reason = db_backup.disabled_reason(session)
    if reason:
        raise HTTPException(status_code=409, detail="Backups are not available on this installation")
    db_backup.row(session)
    taken = session.exec(update(DatabaseBackup).where(
        DatabaseBackup.id == 1, db_backup.ferma(), DatabaseBackup.requested_at.is_(None))  # type: ignore[union-attr]
        .values(requested_at=datetime.now(timezone.utc).replace(tzinfo=None), requested_by=admin.email)).rowcount
    session.commit()
    if taken != 1:
        raise HTTPException(status_code=409, detail="A backup is already running or about to start")
    audit.record_audit(session, actor=admin, action=audit.BACKUP_REQUEST, target_type="database_backup", request=request)
    return await _out(session, admin)


@router.get("/{name}/download", dependencies=[Depends(_niente_token)])
async def scarica(name: str, request: Request, session: Session = Depends(get_session),
                  admin: User = Depends(require_superuser)):
    if not db_backup.NAME.match(name):
        raise HTTPException(status_code=404, detail="No such backup")
    client = get_engine_client()
    resp = await client.send(client.build_request("GET", f"/backups/objects/{name}",
                                                  timeout=httpx.Timeout(None, connect=10.0)), stream=True)
    if resp.status_code >= 400:
        await resp.aread()
        await resp.aclose()
        raise HTTPException(status_code=404 if resp.status_code == 404 else 502,
                            detail="No such backup" if resp.status_code == 404 else "The backup could not be read")
    audit.record_audit(session, actor=admin, action=audit.BACKUP_DOWNLOAD, target_type="database_backup",
                       target_label=name, request=request)

    async def body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    if resp.headers.get("content-length"):
        headers["Content-Length"] = resp.headers["content-length"]
    return StreamingResponse(body(), media_type="application/octet-stream", headers=headers)
