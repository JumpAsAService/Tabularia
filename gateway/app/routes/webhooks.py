"""Webhooks (administration): where, on which events, for which folder; the deliveries
and their history. The logic is in services/webhooks.py.

Only the administrator creates and changes: a webhook sends out, to a chosen address,
news about the flows and datasources of any folder. The observer sees that they exist
and whether they deliver, not where nor with what secret."""
import json
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import Project, User, Webhook, WebhookDelivery
from app.schemas.models import UtcDateTime
from app.services import audit, webhooks as svc
from app.services.permissions import is_admin

router = APIRouter(route_class=ReleasingRoute, prefix="/webhooks", tags=["webhooks"])


class WebhookIn(BaseModel):
    name: str
    url: str
    events: list[str]
    project_id: Optional[int] = None
    active: bool = True


class WebhookPatch(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    events: Optional[list[str]] = None
    project_id: Optional[int] = None
    clear_project: bool = False  # to remove the folder filter
    active: Optional[bool] = None


class WebhookOut(BaseModel):
    id: int
    name: str
    url: str  # for the observer only scheme and host
    events: list[str]
    project_id: Optional[int]
    project_name: Optional[str]
    active: bool
    created_at: UtcDateTime
    last_delivery_status: Optional[str]
    last_delivery_at: Optional[UtcDateTime]


class WebhookWithSecret(WebhookOut):
    """At creation and when it is regenerated: the only time it is seen."""
    secret: str


class DeliveryOut(BaseModel):
    id: int
    event: str
    status: str
    attempts: int
    created_at: UtcDateTime
    next_attempt_at: Optional[UtcDateTime]
    delivered_at: Optional[UtcDateTime]
    last_status_code: Optional[int]
    last_error: Optional[str]
    payload: Optional[str]  # the body sent; not to the observer


def _one(session: Session, wid: int) -> Webhook:
    w = session.get(Webhook, wid)
    if w is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return w


def _events(items_list: list[str]) -> str:
    valid_ones = [e for e in dict.fromkeys(items_list) if e in svc.EVENTS]
    if not valid_ones or len(valid_ones) != len(set(items_list)):
        raise HTTPException(status_code=422, detail=f"Allowed events: {', '.join(svc.EVENTS)} (at least one)")
    return json.dumps(valid_ones)


def _url(url: str) -> str:
    try:
        return svc.validate_url(url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


def _folder(session: Session, project_id: Optional[int]) -> Optional[int]:
    if project_id is not None and session.get(Project, project_id) is None:
        raise HTTPException(status_code=422, detail="The folder does not exist")
    return project_id


def _out(session: Session, w: Webhook, full: bool) -> WebhookOut:
    last_one = session.exec(
        select(WebhookDelivery).where(WebhookDelivery.webhook_id == w.id).order_by(WebhookDelivery.id.desc()).limit(1)
    ).first()
    p = session.get(Project, w.project_id) if w.project_id else None
    parts = urlsplit(w.url)
    return WebhookOut(
        id=w.id, name=w.name, url=w.url if full else f"{parts.scheme}://{parts.hostname}/…",
        events=svc.events_of(w), project_id=w.project_id, project_name=p.name if p else None,
        active=w.active, created_at=w.created_at,
        last_delivery_status=last_one.status if last_one else None,
        last_delivery_at=(last_one.delivered_at or last_one.created_at) if last_one else None,
    )


@router.get("/events", response_model=list[str], dependencies=[Depends(require_observer)])
def list_events():
    return list(svc.EVENTS)


@router.get("", response_model=list[WebhookOut])
def list_webhooks(session: Session = Depends(get_session), chi: User = Depends(require_observer)):
    full = is_admin(session, chi)
    return [_out(session, w, full) for w in session.exec(select(Webhook).order_by(Webhook.name)).all()]


@router.post("", response_model=WebhookWithSecret, status_code=status.HTTP_201_CREATED)
def create_webhook(
    body: WebhookIn, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    name = body.name.strip()
    if not name or len(name) > 120:
        raise HTTPException(status_code=422, detail="A name is required (at most 120 characters)")
    secret = svc.new_secret()
    w = Webhook(
        name=name, url=_url(body.url), secret_enc=svc.encrypt(secret), events=_events(body.events),
        project_id=_folder(session, body.project_id), active=body.active, created_by=admin.id,
        created_at=svc.now(),
    )
    session.add(w)
    session.commit()
    session.refresh(w)
    audit.record_audit(
        session, actor=admin, action=audit.WEBHOOK_CREATE, request=request,
        target_type="webhook", target_id=w.id, target_label=w.name,
        detail={"host": urlsplit(w.url).hostname, "events": svc.events_of(w), "project_id": w.project_id},
    )
    return WebhookWithSecret(**_out(session, w, True).model_dump(), secret=secret)


@router.patch("/{wid}", response_model=WebhookOut)
def update_webhook(
    wid: int, body: WebhookPatch, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    w = _one(session, wid)
    changes: dict = {}
    if body.name is not None:
        name = body.name.strip()
        if not name or len(name) > 120:
            raise HTTPException(status_code=422, detail="A name is required (at most 120 characters)")
        w.name, changes["name"] = name, name
    if body.url is not None:
        w.url = _url(body.url)
        changes["host"] = urlsplit(w.url).hostname
    if body.events is not None:
        w.events = _events(body.events)
        changes["events"] = svc.events_of(w)
    if body.clear_project:
        w.project_id, changes["project_id"] = None, None
    elif body.project_id is not None:
        w.project_id = _folder(session, body.project_id)
        changes["project_id"] = w.project_id
    if body.active is not None:
        w.active, changes["active"] = body.active, body.active
    session.add(w)
    session.commit()
    session.refresh(w)
    if changes:
        audit.record_audit(
            session, actor=admin, action=audit.WEBHOOK_UPDATE, request=request,
            target_type="webhook", target_id=w.id, target_label=w.name, detail=changes,
        )
    return _out(session, w, True)


@router.post("/{wid}/rotate-secret", response_model=WebhookWithSecret)
def rotate_secret(
    wid: int, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """A new secret: from here the deliveries are signed with it (those queued already
    included). The old one stops holding at once."""
    w = _one(session, wid)
    secret = svc.new_secret()
    w.secret_enc = svc.encrypt(secret)
    session.add(w)
    session.commit()
    audit.record_audit(
        session, actor=admin, action=audit.WEBHOOK_UPDATE, request=request,
        target_type="webhook", target_id=w.id, target_label=w.name, detail={"secret": "rotated"},
    )
    return WebhookWithSecret(**_out(session, w, True).model_dump(), secret=secret)


@router.delete("/{wid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook(
    wid: int, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    w = _one(session, wid)
    name = w.name
    session.exec(delete(WebhookDelivery).where(WebhookDelivery.webhook_id == wid))
    session.delete(w)
    session.commit()
    audit.record_audit(
        session, actor=admin, action=audit.WEBHOOK_DELETE, request=request,
        target_type="webhook", target_id=wid, target_label=name,
    )
    return None


@router.post("/{wid}/test", response_model=DeliveryOut, status_code=status.HTTP_202_ACCEPTED)
def test_webhook(wid: int, session: Session = Depends(get_session), _: User = Depends(require_superuser)):
    """Queues a `ping`: it goes within a couple of seconds, the outcome is read in the
    deliveries."""
    return _delivery_out(svc.enqueue_ping(session, _one(session, wid)), True)


def _delivery_out(d: WebhookDelivery, full: bool) -> DeliveryOut:
    fields = {c: getattr(d, c) for c in DeliveryOut.model_fields if c != "payload"}  # see api_tokens.token_out
    if not full:
        fields["last_error"] = None  # holds the receiver's answer and its address
    return DeliveryOut(**fields, payload=d.payload if full else None)


@router.get("/{wid}/deliveries", response_model=list[DeliveryOut])
def list_deliveries(
    wid: int, limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session), chi: User = Depends(require_observer),
):
    _one(session, wid)
    full = is_admin(session, chi)
    rows = session.exec(
        select(WebhookDelivery).where(WebhookDelivery.webhook_id == wid).order_by(WebhookDelivery.id.desc()).limit(limit)
    ).all()
    return [_delivery_out(d, full) for d in rows]


@router.post("/deliveries/{did}/redeliver", response_model=DeliveryOut, status_code=status.HTTP_202_ACCEPTED)
def redeliver(did: int, session: Session = Depends(get_session), _: User = Depends(require_superuser)):
    d = session.get(WebhookDelivery, did)
    if d is None:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return _delivery_out(svc.resend(session, d), True)
