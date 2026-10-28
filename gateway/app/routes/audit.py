"""Reading the audit log (admin only): filterable events + active sessions.

Who signs in, who logs in (and the failed attempts), which flows/datasources/
connections are created/changed/run, which data is downloaded, who changes the
permissions. Writing the events is in services/audit.py.
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
from app.core.routing import ReleasingRoute

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ReleasingRoute, tags=["audit"], dependencies=[Depends(require_observer)])

# the window within which a user counts as "active now" (recent last_seen)
ACTIVE_WINDOW = timedelta(minutes=15)


class AuditEntryOut(BaseModel):
    id: int
    ts: UtcDateTime  # naive UTC from the DB: in JSON with +00:00
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
    """An open session (a browser, a script) and whose it is."""
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
    online: bool  # active within ACTIVE_WINDOW
    current: bool  # the one the asker is looking from


def _to_out(e: AuditLog, full: bool = True) -> AuditEntryOut:
    """An audit item. `full=False` for an OBSERVER: WHAT was done stays — which is the point
    of the log — and who did it, from where and with what disappear. The person acted on
    too, when the target is an email (sessions closed, users promoted or added to a
    group): it would be the same person as the actor, in clear next to their mask. The
    masks are stable, so «these three actions are the same person» still shows."""
    try:
        detail = json.loads(e.detail) if e.detail else None
    except json.JSONDecodeError:
        detail = {"_raw": e.detail}
    out = AuditEntryOut(**e.model_dump(exclude={"detail"}), detail=detail)
    if not full:
        out.actor_label = mask_email(out.actor_label) if "@" in (out.actor_label or "") else out.actor_label
        if "@" in (out.target_label or ""):
            out.target_label = mask_email(out.target_label)
        out.ip = None
        out.user_agent = None
        out.detail = _detail_for_observer(out.detail)
    return out


# keys of `detail` that say WHERE (hosts, endpoints, paths, SQL) or WHO (IdP groups
# and identities, a person's name — the users' list blanks it for them too): an observer
# sees "…" in their place, at any depth
_HIDDEN = frozenset({
    "host", "url", "endpoint", "server", "fingerprint", "path", "source_keys", "sql", "secret",
    "groups", "groups_added", "groups_removed", "group_names", "sub", "subject", "issuer",
    "to", "cc", "bcc", "full_name",
})


def _detail_for_observer(value, key: str | None = None):
    """`detail` as an observer reads it: what was done stays (counts, formats,
    engines, ids), where and who goes; emails are masked like the actor's."""
    if key in _HIDDEN and not isinstance(value, (int, float, bool)) and value is not None:
        return "…"
    if key == "prefix" and value:
        return "tab_…"  # come in /admin/api-tokens
    if isinstance(value, dict):
        return {k: _detail_for_observer(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_detail_for_observer(v) for v in value]
    if isinstance(value, str) and "@" in value:
        return mask_email(value)
    return value


@router.get("/audit", response_model=Page[AuditEntryOut])
def list_audit(
    q: Optional[str] = Query(None, description="searches actor/target/action/IP"),
    action: Optional[str] = Query(None, description="exact filter on the action (e.g. flow.create)"),
    target_type: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None, description="success | failure"),
    actor_id: Optional[int] = Query(None),
    since: Optional[datetime] = Query(None, description="from the timestamp (included)"),
    until: Optional[datetime] = Query(None, description="up to the timestamp (included)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    asker: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Audit events, newest first, paginated and filterable."""
    full = is_admin(session, asker)  # whoever is in charge sees; whoever watches does not
    base = select(AuditLog)
    if q:
        like = f"%{q}%"
        if full:
            base = base.where(or_(
                AuditLog.actor_label.ilike(like), AuditLog.target_label.ilike(like),
                AuditLog.action.ilike(like), AuditLog.ip.ilike(like),
            ))
        else:
            # the observer searches only over what they see: over the IPs and the emails hidden
            # from them, counting the rows found would rebuild them by trial
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
    return Page(items=[_to_out(e, full) for e in rows], total=total)


@router.get("/audit/actions", response_model=list[str])
def list_actions(session: Session = Depends(get_session)):
    """The actions present in the audit (to fill the filter in the UI)."""
    rows = session.exec(select(AuditLog.action).distinct().order_by(AuditLog.action)).all()
    return list(rows)


class AccessBucket(BaseModel):
    ts: datetime      # the start of the hour (in the deployment's zone)
    label: str        # "HH:00"
    success: int      # successful logins
    failure: int      # failed logins (attempts)


class AccessActivityOut(BaseModel):
    hours: int
    timezone: str
    buckets: list[AccessBucket]
    total_success: int
    total_failure: int


@router.get("/audit/access-activity", response_model=AccessActivityOut)
def access_activity(
    hours: int = Query(24, ge=1, le=168, description="window in hours"),
    session: Session = Depends(get_session),
):
    """Sign-ins (logins) per hour in the last `hours` hours, successful vs failed. Bucketed
    in the deployment's zone so the hour axis is in local time."""
    tz = get_settings().app.tzinfo()
    now = datetime.now(tz)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    # buckets [start-h+1 … start], one per hour
    starts = [hour_start - timedelta(hours=hours - 1 - i) for i in range(hours)]
    index = {s.replace(tzinfo=None): i for i, s in enumerate(starts)}  # key: naive local hour
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
    asker: User = Depends(get_current_user),
):
    """The open sessions, one per browser or script, newest first: the first `limit`, and
    how many there are in all (on a public demo every guest sign-in is a row). They are
    the rows that keep the tokens valid: closing one (DELETE) throws that browser out at
    the next request."""
    full = is_admin(session, asker)  # whoever is in charge sees; whoever watches does not
    threshold = sessions_svc.now() - ACTIVE_WINDOW
    open_ones = sessions_svc.open_ones(session, limit=limit)
    users = {u.id: u for u in session.exec(
        select(User).where(User.id.in_({s.user_id for s in open_ones}))
    ).all()} if open_ones else {}
    # EFFECTIVE admins: whoever is one by membership of an administrators' group too
    from app.models import Group, UserGroupLink

    via_group = set(session.exec(
        select(UserGroupLink.user_id)
        .where(UserGroupLink.group_id == Group.id)
        .where(Group.is_admin == True)  # noqa: E712
    ).all())
    current = sessions_svc.current_sid(request)
    out: list[ActiveSession] = []
    for s in open_ones:
        u = users.get(s.user_id)
        if u is None:
            continue
        out.append(ActiveSession(
            # the observer closes nothing: telling the rows apart is enough for them
            id=s.id if full else sessions_svc.handle(s.id),
            user_id=u.id,
            # an OBSERVER sees how many sessions there are and whether they are alive, not who is
            # behind them nor where they come in from (see services/masking.py)
            email=u.email if full else mask_email(u.email),
            full_name=u.full_name if full else "",
            is_superuser=u.is_superuser or u.id in via_group,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            expires_at=s.expires_at,
            ip=s.ip if full else None,
            user_agent=s.user_agent if full else None,
            method=s.method,
            online=s.last_seen_at >= threshold,
            current=s.id == current,
        ))
    return Page(items=out, total=sessions_svc.how_many_open(session))


@router.delete("/audit/sessions/{sid}", status_code=204)
def revoke_session(
    sid: str,
    request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(require_superuser),
):
    """Closes someone else's session (or one's own): its token stops holding at the next
    request. Administrators only: the observer watches."""
    row = session.get(UserSession, sid)
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    owner = session.get(User, row.user_id)
    sessions_svc.close(session, sid)
    audit_svc.record_audit(
        session, actor=admin, action=audit_svc.SESSION_REVOKE, request=request,
        target_type="user", target_id=row.user_id,
        target_label=owner.email if owner else None,
        detail={"sessions": 1},
    )
    return None
