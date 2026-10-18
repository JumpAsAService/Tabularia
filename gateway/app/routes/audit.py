"""Lettura dell'audit log (solo admin): eventi filtrabili + sessioni attive.

Chi entra, chi fa il login (e i tentativi falliti), quali flussi/datasource/
connessioni vengono creati/modificati/eseguiti, quali dati vengono scaricati,
chi cambia i permessi. La scrittura degli eventi sta in services/audit.py.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, or_
from sqlmodel import Session, func, select

from app.core.config import get_settings
from app.db.session import get_session
from app.deps.auth import get_current_user, require_observer, require_superuser
from app.services.masking import mask_email
from app.services.permissions import is_admin
from app.models import AuditLog, User, UserSession
from app.schemas.models import UtcDateTime, Page
from app.services import audit as audit_svc
from app.services import sessions as sessions_svc
from app.core.routing import RottaCheRilascia

logger = logging.getLogger(__name__)

router = APIRouter(route_class=RottaCheRilascia, tags=["audit"], dependencies=[Depends(require_observer)])

# finestra entro cui un utente è considerato "attivo ora" (last_seen recente)
ACTIVE_WINDOW = timedelta(minutes=15)


class AuditEntryOut(BaseModel):
    id: int
    ts: UtcDateTime  # naive UTC dal DB: in JSON con +00:00
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
    """Una sessione aperta (un browser, uno script) e di chi è."""
    id: str
    user_id: int
    email: str
    full_name: str
    is_superuser: bool
    created_at: UtcDateTime
    last_seen_at: UtcDateTime
    expires_at: UtcDateTime
    ip: Optional[str]
    user_agent: Optional[str]
    method: str
    online: bool  # attiva entro ACTIVE_WINDOW
    current: bool  # quella da cui guarda chi chiede


def _to_out(e: AuditLog, pieno: bool = True) -> AuditEntryOut:
    """Una voce dell'audit. `pieno=False` per un OSSERVATORE: resta CHE COSA è
    stato fatto — che è il senso del registro — e spariscono chi l'ha fatto, da
    dove e con che cosa. Anche la persona su cui si è agito, quando il bersaglio è
    un'email (sessioni chiuse, utenti promossi o aggiunti a un gruppo): sarebbe la
    stessa persona dell'attore, in chiaro accanto alla sua maschera. Le maschere
    sono stabili, quindi «queste tre azioni sono la stessa persona» si vede ancora."""
    try:
        detail = json.loads(e.detail) if e.detail else None
    except json.JSONDecodeError:
        detail = {"_raw": e.detail}
    out = AuditEntryOut(**e.model_dump(exclude={"detail"}), detail=detail)
    if not pieno:
        out.actor_label = mask_email(out.actor_label) if "@" in (out.actor_label or "") else out.actor_label
        if "@" in (out.target_label or ""):
            out.target_label = mask_email(out.target_label)
        out.ip = None
        out.user_agent = None
        out.detail = _detail_per_osservatore(out.detail)
    return out


# keys of `detail` that say WHERE (hosts, endpoints, paths, SQL) or WHO (IdP groups
# and identities): an observer sees "…" in their place, at any depth
_NASCOSTI = frozenset({
    "host", "url", "endpoint", "server", "fingerprint", "path", "source_keys", "sql", "secret",
    "groups", "groups_added", "groups_removed", "group_names", "sub", "subject", "issuer",
    "to", "cc", "bcc",
})


def _detail_per_osservatore(valore, chiave: str | None = None):
    """`detail` as an observer reads it: what was done stays (counts, formats,
    engines, ids), where and who goes; emails are masked like the actor's."""
    if chiave in _NASCOSTI and not isinstance(valore, (int, float, bool)) and valore is not None:
        return "…"
    if chiave == "prefix" and valore:
        return "tab_…"  # come in /admin/api-tokens
    if isinstance(valore, dict):
        return {k: _detail_per_osservatore(v, k) for k, v in valore.items()}
    if isinstance(valore, list):
        return [_detail_per_osservatore(v) for v in valore]
    if isinstance(valore, str) and "@" in valore:
        return mask_email(valore)
    return valore


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
    chi_chiede: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Eventi di audit, dal più recente, paginati e filtrabili."""
    pieno = is_admin(session, chi_chiede)  # chi comanda vede; chi guarda no
    base = select(AuditLog)
    if q:
        like = f"%{q}%"
        if pieno:
            base = base.where(or_(
                AuditLog.actor_label.ilike(like), AuditLog.target_label.ilike(like),
                AuditLog.action.ilike(like), AuditLog.ip.ilike(like),
            ))
        else:
            # l'osservatore cerca solo su quel che vede: sugli IP e sulle email che
            # gli sono nascosti, contare le righe trovate li ricostruirebbe a tentativi
            base = base.where(or_(
                AuditLog.action.ilike(like),
                and_(AuditLog.target_label.ilike(like), ~AuditLog.target_label.contains("@")),
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
    return Page(items=[_to_out(e, pieno) for e in rows], total=total)


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


@router.get("/audit/sessions", response_model=Page[ActiveSession])
def active_sessions(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
    chi_chiede: User = Depends(get_current_user),
):
    """Le sessioni aperte, una per browser o script, dalla più recente: le prime
    `limit`, e quante sono in tutto (su una demo pubblica ogni accesso dell'ospite
    è una riga). Sono le righe che tengono validi i token: chiuderne una (DELETE)
    butta fuori quel browser alla richiesta dopo."""
    pieno = is_admin(session, chi_chiede)  # chi comanda vede; chi guarda no
    threshold = sessions_svc.adesso() - ACTIVE_WINDOW
    aperte = sessions_svc.aperte(session, limite=limit)
    utenti = {u.id: u for u in session.exec(
        select(User).where(User.id.in_({s.user_id for s in aperte}))
    ).all()} if aperte else {}
    # admin EFFETTIVI: anche chi lo è per appartenenza a un gruppo di amministratori
    from app.models import Group, UserGroupLink

    via_gruppo = set(session.exec(
        select(UserGroupLink.user_id)
        .where(UserGroupLink.group_id == Group.id)
        .where(Group.is_admin == True)  # noqa: E712
    ).all())
    corrente = sessions_svc.sid_corrente(request)
    out: list[ActiveSession] = []
    for s in aperte:
        u = utenti.get(s.user_id)
        if u is None:
            continue
        out.append(ActiveSession(
            # l'osservatore non chiude niente: gli basta distinguere le righe
            id=s.id if pieno else sessions_svc.maniglia(s.id),
            user_id=u.id,
            # un OSSERVATORE vede quante sessioni ci sono e se sono vive, non
            # chi c'è dietro né da dove entra (vedi services/masking.py)
            email=u.email if pieno else mask_email(u.email),
            full_name=u.full_name if pieno else "",
            is_superuser=u.is_superuser or u.id in via_gruppo,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            expires_at=s.expires_at,
            ip=s.ip if pieno else None,
            user_agent=s.user_agent if pieno else None,
            method=s.method,
            online=s.last_seen_at >= threshold,
            current=s.id == corrente,
        ))
    return Page(items=out, total=sessions_svc.quante_aperte(session))


@router.delete("/audit/sessions/{sid}", status_code=204)
def revoke_session(
    sid: str,
    request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(require_superuser),
):
    """Chiude la sessione di qualcun altro (o la propria): il suo token smette di
    valere alla richiesta dopo. Solo amministratori: l'osservatore guarda."""
    riga = session.get(UserSession, sid)
    if riga is None:
        raise HTTPException(status_code=404, detail="Session not found")
    proprietario = session.get(User, riga.user_id)
    sessions_svc.chiudi(session, sid)
    audit_svc.record_audit(
        session, actor=admin, action=audit_svc.SESSION_REVOKE, request=request,
        target_type="user", target_id=riga.user_id,
        target_label=proprietario.email if proprietario else None,
        detail={"sessions": 1},
    )
    return None
