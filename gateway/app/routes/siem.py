"""SIEM (Administration → SIEM): where the audit log goes, and how it is going.

Splunk for now, with the HTTP Event Collector (see services/siem.py). Read for
administrators and observers (to the observer no full address, no Splunk error);
write and test for administrators only, never with an API token. The Splunk token is
written and not read back: one sees only that it is there.
"""
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.crypto import encrypt_secret
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import SiemDestination, User
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit, siem, webhooks
from app.services.permissions import is_admin
from sqlmodel import Session

router = APIRouter(route_class=ReleasingRoute, prefix="/admin/siem", tags=["siem"])


class SiemOut(BaseModel):
    kind: str
    enabled: bool
    url: str  # to the observer only scheme and host
    token_set: bool
    index: Optional[str]
    source: Optional[str]
    sourcetype: Optional[str]
    host: Optional[str]
    verify_tls: bool
    editable: bool
    # how it is going
    sent_total: int
    backlog: int  # audit items waiting to go out
    oldest_pending_at: Optional[UtcDateTime]
    last_ok_at: Optional[UtcDateTime]
    last_error: Optional[str]  # not to the observer
    last_error_at: Optional[UtcDateTime]
    failures: int
    next_attempt_at: Optional[UtcDateTime]


class SiemUpdate(BaseModel):
    # only those that change; the token: a text replaces it, "" removes it
    enabled: Optional[bool] = None
    url: Optional[str] = Field(default=None, max_length=2000)
    token: Optional[str] = Field(default=None, max_length=200)
    index: Optional[str] = Field(default=None, max_length=120)
    source: Optional[str] = Field(default=None, max_length=120)
    sourcetype: Optional[str] = Field(default=None, max_length=120)
    host: Optional[str] = Field(default=None, max_length=255)
    verify_tls: Optional[bool] = None


class SiemTestOut(BaseModel):
    ok: bool
    status_code: Optional[int]
    message: str


def _no_token(request: Request, _admin: User = Depends(require_superuser)) -> None:
    """After the authentication (the dependency above), before reading the body."""
    from app.routes.api_tokens import deny_if_from_token

    deny_if_from_token(request, api_tokens.NO_SIEM_FROM_TOKEN)


def _destination(session: Session) -> SiemDestination:
    return session.get(SiemDestination, 1) or SiemDestination(id=1)


def _out(session: Session, d: SiemDestination, chi: User, request: Request) -> SiemOut:
    full = is_admin(session, chi)
    n, oldest = siem.backlog(session, d) if d.enabled else (0, None)
    url = d.url
    if url and not full:
        parts = urlsplit(url)
        url = f"{parts.scheme}://{parts.hostname}/…"
    return SiemOut(
        kind=d.kind, enabled=d.enabled, url=url, token_set=bool(d.token_enc), index=d.index, source=d.source,
        sourcetype=d.sourcetype, host=d.host, verify_tls=d.verify_tls,
        editable=full and not api_tokens.current_name(request),
        sent_total=d.sent_total or 0, backlog=n, oldest_pending_at=oldest, last_ok_at=d.last_ok_at,
        last_error=d.last_error if full else None, last_error_at=d.last_error_at,
        failures=d.failures or 0, next_attempt_at=d.next_attempt_at,
    )


@router.get("", response_model=SiemOut)
def read(request: Request, session: Session = Depends(get_session), chi: User = Depends(require_observer)):
    return _out(session, _destination(session), chi, request)


def _text(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    return v or None


@router.put("", response_model=SiemOut, dependencies=[Depends(_no_token)])
def write(
    body: SiemUpdate, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Changes what is given. Turning on wants address and token; the first time the
    forwarding starts from now (the history stays in the Audit panel). Off and on again it
    picks up where it left off: no gaps."""
    d = _destination(session)
    before = {"enabled": d.enabled, "url": d.url, "index": d.index, "source": d.source,
             "sourcetype": d.sourcetype, "host": d.host, "verify_tls": d.verify_tls}
    changes: dict = {}
    if body.url is not None:
        url = body.url.strip()
        if url:
            try:
                webhooks.validate_url(url)  # http(s), a host, readable by httpx
                webhooks.validate_url(siem.endpoint(url))
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
        d.url = url
    for field in ("index", "source", "sourcetype", "host"):
        v = getattr(body, field)
        if v is not None:
            setattr(d, field, _text(v))
    if body.verify_tls is not None:
        d.verify_tls = body.verify_tls
    if body.token is not None:
        d.token_enc = encrypt_secret(body.token.strip()) if body.token.strip() else None
        changes["token"] = "changed" if body.token.strip() else "removed"
    if body.enabled is not None:
        d.enabled = body.enabled
    if d.enabled and not (d.url and d.token_enc):
        raise HTTPException(status_code=422, detail="Turning forwarding on needs the Splunk address and token")
    if d.enabled and not before["enabled"]:
        if not d.cursor_id:
            d.cursor_id = siem.last_id(session)  # from now: the history stays in the Audit panel
        d.failures, d.next_attempt_at = 0, None
    if any(k in body.model_fields_set for k in ("url", "token", "verify_tls", "index")):
        d.failures, d.next_attempt_at = 0, None  # new configuration: retry at once
    for k, v in before.items():
        if getattr(d, k) != v:
            changes[k] = {"from": v, "to": getattr(d, k)}
    if not changes:
        return _out(session, d, admin, request)
    from datetime import datetime, timezone

    d.updated_at, d.updated_by = datetime.now(timezone.utc).replace(tzinfo=None), admin.email
    session.add(d)
    session.commit()
    session.refresh(d)
    audit.record_audit(
        session, actor=admin, action=audit.SIEM_UPDATE, request=request,
        target_type="siem", target_label=urlsplit(d.url).hostname if d.url else None, detail={"changes": changes},
    )
    return _out(session, d, admin, request)


@router.post("/test", response_model=SiemTestOut, dependencies=[Depends(_no_token)])
async def trial(request: Request, session: Session = Depends(get_session), admin: User = Depends(require_superuser)):
    """Sends a test event to Splunk now (it does not go through the queue) and says what it
    answered: token, index, certificate and network are checked here."""
    d = _destination(session)
    if not d.url or not d.token_enc:
        raise HTTPException(status_code=422, detail="The Splunk address and token are needed")
    at = siem.now()
    ok, code, message = await siem.send(d, [{
        "time": round(siem._utc(at).timestamp(), 3), "host": d.host or siem.HOST, "source": d.source or siem.SOURCE,
        "sourcetype": d.sourcetype or siem.SOURCETYPE, **({"index": d.index} if d.index else {}),
        "event": {"action": "siem.test", "outcome": "success", "user": admin.email, "app": "tabularia", "test": True,
                  "message": "Tabularia: Splunk delivery test"},
    }])
    audit.record_audit(
        session, actor=admin, action=audit.SIEM_TEST, request=request, outcome="success" if ok else "failure",
        target_type="siem", target_label=urlsplit(d.url).hostname, detail={"status_code": code},
    )
    return SiemTestOut(ok=ok, status_code=code, message=message)
