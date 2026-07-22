"""Lettura dell'audit log (solo admin): eventi filtrabili + sessioni attive.

Chi entra, chi fa il login (e i tentativi falliti), quali flussi/datasource/
connessioni vengono creati/modificati/eseguiti, quali dati vengono scaricati,
chi cambia i permessi. La scrittura degli eventi sta in services/audit.py.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlmodel import Session, func, select

from app.core.config import get_settings
from app.db.session import get_session
from app.deps.auth import require_superuser
from app.deps.demo import demo_active, mask_ip
from app.models import AuditLog, User
from app.schemas.models import Page
from app.services import audit as audit_svc

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"], dependencies=[Depends(require_superuser)])

# finestra entro cui un utente è considerato "attivo ora" (last_seen recente)
ACTIVE_WINDOW = timedelta(minutes=15)


class AuditEntryOut(BaseModel):
    id: int
    ts: datetime
    actor_id: Optional[int]
    actor_label: str
    action: str
    outcome: str
    target_type: Optional[str]
    target_id: Optional[int]
    target_label: Optional[str]
    detail: Optional[dict]
    ip: Optional[str]
    user_agent: Optional[str]


class ActiveSession(BaseModel):
    user_id: int
    email: str
    full_name: str
    is_superuser: bool
    last_seen_at: Optional[datetime]
    last_seen_ip: Optional[str]
    online: bool  # last_seen entro ACTIVE_WINDOW


def _to_out(e: AuditLog) -> AuditEntryOut:
    try:
        detail = json.loads(e.detail) if e.detail else None
    except json.JSONDecodeError:
        detail = {"_raw": e.detail}
    out = AuditEntryOut(**e.model_dump(exclude={"detail"}), detail=detail)
    if demo_active():  # sandbox: non esporre l'IP di un visitatore a un altro
        out.ip = mask_ip(out.ip)
    return out


@router.get("/audit", response_model=Page[AuditEntryOut])
def list_audit(
    q: Optional[str] = Query(None, description="cerca su attore/bersaglio/azione/IP"),
    action: Optional[str] = Query(None, description="filtro esatto sull'azione (es. flow.create)"),
    target_type: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None, description="success | failure"),
    actor_id: Optional[int] = Query(None),
    since: Optional[datetime] = Query(None, description="dal timestamp (incluso)"),
    until: Optional[datetime] = Query(None, description="fino al timestamp (incluso)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    """Eventi di audit, dal più recente, paginati e filtrabili."""
    base = select(AuditLog)
    if demo_active():  # sandbox: ogni visitatore vede SOLO i propri eventi
        base = base.where(AuditLog.actor_id == user.id)
    if q:
        like = f"%{q}%"
        base = base.where(or_(
            AuditLog.actor_label.ilike(like), AuditLog.target_label.ilike(like),
            AuditLog.action.ilike(like), AuditLog.ip.ilike(like),
        ))
    if action:
        base = base.where(AuditLog.action == action)
    if target_type:
        base = base.where(AuditLog.target_type == target_type)
    if outcome:
        base = base.where(AuditLog.outcome == outcome)
    if actor_id is not None:
        base = base.where(AuditLog.actor_id == actor_id)
    if since is not None:
        base = base.where(AuditLog.ts >= since)
    if until is not None:
        base = base.where(AuditLog.ts <= until)

    total = session.exec(select(func.count()).select_from(base.subquery())).one()
    rows = session.exec(
        base.order_by(AuditLog.ts.desc()).offset(offset).limit(limit)
    ).all()
    return Page(items=[_to_out(e) for e in rows], total=total)


@router.get("/audit/actions", response_model=list[str])
def list_actions(session: Session = Depends(get_session)):
    """Azioni presenti nell'audit (per popolare il filtro nell'UI)."""
    rows = session.exec(select(AuditLog.action).distinct().order_by(AuditLog.action)).all()
    return list(rows)


class AccessBucket(BaseModel):
    ts: datetime      # inizio dell'ora (nel fuso del deployment)
    label: str        # "HH:00"
    success: int      # login riusciti
    failure: int      # login falliti (tentativi)


class AccessActivityOut(BaseModel):
    hours: int
    timezone: str
    buckets: list[AccessBucket]
    total_success: int
    total_failure: int


@router.get("/audit/access-activity", response_model=AccessActivityOut)
def access_activity(
    hours: int = Query(24, ge=1, le=168, description="finestra in ore"),
    session: Session = Depends(get_session),
):
    """Accessi (login) per ora nelle ultime `hours` ore, riusciti vs falliti.
    Bucketizzati nel fuso del deployment così l'asse orario è in ora locale."""
    tz = get_settings().app.tzinfo()
    now = datetime.now(tz)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    # bucket [start-h+1 … start], uno per ora
    starts = [hour_start - timedelta(hours=hours - 1 - i) for i in range(hours)]
    index = {s.replace(tzinfo=None): i for i, s in enumerate(starts)}  # chiave: ora locale naive
    success = [0] * hours
    failure = [0] * hours

    cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = session.exec(
        select(AuditLog.ts, AuditLog.action).where(
            AuditLog.ts >= cutoff_utc,
            AuditLog.action.in_([audit_svc.LOGIN, audit_svc.LOGIN_FAILED]),
        )
    ).all()
    for ts, action in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local_hour = ts.astimezone(tz).replace(minute=0, second=0, microsecond=0, tzinfo=None)
        i = index.get(local_hour)
        if i is None:
            continue
        if action == audit_svc.LOGIN:
            success[i] += 1
        else:
            failure[i] += 1

    buckets = [
        AccessBucket(ts=starts[i], label=starts[i].strftime("%H:00"), success=success[i], failure=failure[i])
        for i in range(hours)
    ]
    return AccessActivityOut(
        hours=hours, timezone=get_settings().app.timezone, buckets=buckets,
        total_success=sum(success), total_failure=sum(failure),
    )


@router.get("/audit/sessions", response_model=list[ActiveSession])
def active_sessions(
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    """Utenti con attività recente (il JWT è stateless: 'attivo ora' = last_seen
    entro la finestra). Ordinati dal più recente. Chi non è mai stato visto è escluso."""
    now = datetime.now(timezone.utc)
    threshold = now - ACTIVE_WINDOW
    base = select(User).where(User.last_seen_at.is_not(None))
    if demo_active():  # sandbox: mostra solo la propria sessione, non gli altri visitatori
        base = base.where(User.id == user.id)
    users = session.exec(base.order_by(User.last_seen_at.desc())).all()
    out: list[ActiveSession] = []
    for u in users:
        seen = u.last_seen_at
        if seen is not None and seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        out.append(ActiveSession(
            user_id=u.id, email=u.email, full_name=u.full_name, is_superuser=u.is_superuser,
            last_seen_at=u.last_seen_at, last_seen_ip=mask_ip(u.last_seen_ip) if demo_active() else u.last_seen_ip,
            online=(seen is not None and seen >= threshold),
        ))
    return out
