"""Webhook (amministrazione): dove, a quali eventi, per quale cartella; le
consegne e la loro storia. La logica è in services/webhooks.py.

Solo l'amministratore crea e cambia: un webhook manda fuori, a un indirizzo
scelto, notizie su flussi e datasource di qualunque cartella. L'osservatore vede
che esistono e se consegnano, non dove né con che segreto."""
import json
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.routing import RottaCheRilascia
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import Project, User, Webhook, WebhookDelivery
from app.schemas.models import UtcDateTime
from app.services import audit, webhooks as svc
from app.services.permissions import is_admin

router = APIRouter(route_class=RottaCheRilascia, prefix="/webhooks", tags=["webhooks"])


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
    clear_project: bool = False  # per togliere il filtro della cartella
    active: Optional[bool] = None


class WebhookOut(BaseModel):
    id: int
    name: str
    url: str  # per l'osservatore solo schema e host
    events: list[str]
    project_id: Optional[int]
    project_name: Optional[str]
    active: bool
    created_at: UtcDateTime
    last_delivery_status: Optional[str]
    last_delivery_at: Optional[UtcDateTime]


class WebhookWithSecret(WebhookOut):
    """Alla creazione e quando lo si rigenera: l'unica volta che si vede."""
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
    payload: Optional[str]  # il corpo spedito; non all'osservatore


def _uno(session: Session, wid: int) -> Webhook:
    w = session.get(Webhook, wid)
    if w is None:
        raise HTTPException(status_code=404, detail="Webhook non trovato")
    return w


def _eventi(lista: list[str]) -> str:
    validi = [e for e in dict.fromkeys(lista) if e in svc.EVENTI]
    if not validi or len(validi) != len(set(lista)):
        raise HTTPException(status_code=422, detail=f"Eventi ammessi: {', '.join(svc.EVENTI)} (almeno uno)")
    return json.dumps(validi)


def _url(url: str) -> str:
    try:
        return svc.valida_url(url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


def _cartella(session: Session, project_id: Optional[int]) -> Optional[int]:
    if project_id is not None and session.get(Project, project_id) is None:
        raise HTTPException(status_code=422, detail="Cartella inesistente")
    return project_id


def _out(session: Session, w: Webhook, pieno: bool) -> WebhookOut:
    ultima = session.exec(
        select(WebhookDelivery).where(WebhookDelivery.webhook_id == w.id).order_by(WebhookDelivery.id.desc()).limit(1)
    ).first()
    p = session.get(Project, w.project_id) if w.project_id else None
    parti = urlsplit(w.url)
    return WebhookOut(
        id=w.id, name=w.name, url=w.url if pieno else f"{parti.scheme}://{parti.hostname}/…",
        events=svc.eventi_di(w), project_id=w.project_id, project_name=p.name if p else None,
        active=w.active, created_at=w.created_at,
        last_delivery_status=ultima.status if ultima else None,
        last_delivery_at=(ultima.delivered_at or ultima.created_at) if ultima else None,
    )


@router.get("/events", response_model=list[str], dependencies=[Depends(require_observer)])
def list_events():
    return list(svc.EVENTI)


@router.get("", response_model=list[WebhookOut])
def list_webhooks(session: Session = Depends(get_session), chi: User = Depends(require_observer)):
    pieno = is_admin(session, chi)
    return [_out(session, w, pieno) for w in session.exec(select(Webhook).order_by(Webhook.name)).all()]


@router.post("", response_model=WebhookWithSecret, status_code=status.HTTP_201_CREATED)
def create_webhook(
    body: WebhookIn, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    nome = body.name.strip()
    if not nome or len(nome) > 120:
        raise HTTPException(status_code=422, detail="Il nome è obbligatorio (al più 120 caratteri)")
    segreto = svc.nuovo_segreto()
    w = Webhook(
        name=nome, url=_url(body.url), secret_enc=svc.cifra(segreto), events=_eventi(body.events),
        project_id=_cartella(session, body.project_id), active=body.active, created_by=admin.id,
        created_at=svc.adesso(),
    )
    session.add(w)
    session.commit()
    session.refresh(w)
    audit.record_audit(
        session, actor=admin, action=audit.WEBHOOK_CREATE, request=request,
        target_type="webhook", target_id=w.id, target_label=w.name,
        detail={"host": urlsplit(w.url).hostname, "events": svc.eventi_di(w), "project_id": w.project_id},
    )
    return WebhookWithSecret(**_out(session, w, True).model_dump(), secret=segreto)


@router.patch("/{wid}", response_model=WebhookOut)
def update_webhook(
    wid: int, body: WebhookPatch, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    w = _uno(session, wid)
    cambi: dict = {}
    if body.name is not None:
        nome = body.name.strip()
        if not nome or len(nome) > 120:
            raise HTTPException(status_code=422, detail="Il nome è obbligatorio (al più 120 caratteri)")
        w.name, cambi["name"] = nome, nome
    if body.url is not None:
        w.url = _url(body.url)
        cambi["host"] = urlsplit(w.url).hostname
    if body.events is not None:
        w.events = _eventi(body.events)
        cambi["events"] = svc.eventi_di(w)
    if body.clear_project:
        w.project_id, cambi["project_id"] = None, None
    elif body.project_id is not None:
        w.project_id = _cartella(session, body.project_id)
        cambi["project_id"] = w.project_id
    if body.active is not None:
        w.active, cambi["active"] = body.active, body.active
    session.add(w)
    session.commit()
    session.refresh(w)
    if cambi:
        audit.record_audit(
            session, actor=admin, action=audit.WEBHOOK_UPDATE, request=request,
            target_type="webhook", target_id=w.id, target_label=w.name, detail=cambi,
        )
    return _out(session, w, True)


@router.post("/{wid}/rotate-secret", response_model=WebhookWithSecret)
def rotate_secret(
    wid: int, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Un segreto nuovo: da qui le consegne sono firmate con lui (quelle già in
    coda comprese). Il vecchio smette di valere subito."""
    w = _uno(session, wid)
    segreto = svc.nuovo_segreto()
    w.secret_enc = svc.cifra(segreto)
    session.add(w)
    session.commit()
    audit.record_audit(
        session, actor=admin, action=audit.WEBHOOK_UPDATE, request=request,
        target_type="webhook", target_id=w.id, target_label=w.name, detail={"secret": "rotated"},
    )
    return WebhookWithSecret(**_out(session, w, True).model_dump(), secret=segreto)


@router.delete("/{wid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook(
    wid: int, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    w = _uno(session, wid)
    nome = w.name
    session.exec(delete(WebhookDelivery).where(WebhookDelivery.webhook_id == wid))
    session.delete(w)
    session.commit()
    audit.record_audit(
        session, actor=admin, action=audit.WEBHOOK_DELETE, request=request,
        target_type="webhook", target_id=wid, target_label=nome,
    )
    return None


@router.post("/{wid}/test", response_model=DeliveryOut, status_code=status.HTTP_202_ACCEPTED)
def test_webhook(wid: int, session: Session = Depends(get_session), _: User = Depends(require_superuser)):
    """Accoda un `ping`: parte entro un paio di secondi, l'esito si legge nelle consegne."""
    return _delivery_out(svc.accoda_ping(session, _uno(session, wid)), True)


def _delivery_out(d: WebhookDelivery, pieno: bool) -> DeliveryOut:
    campi = {c: getattr(d, c) for c in DeliveryOut.model_fields if c != "payload"}  # vedi api_tokens.token_out
    if not pieno:
        campi["last_error"] = None  # contiene la risposta del ricevente e il suo indirizzo
    return DeliveryOut(**campi, payload=d.payload if pieno else None)


@router.get("/{wid}/deliveries", response_model=list[DeliveryOut])
def list_deliveries(
    wid: int, limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session), chi: User = Depends(require_observer),
):
    _uno(session, wid)
    pieno = is_admin(session, chi)
    righe = session.exec(
        select(WebhookDelivery).where(WebhookDelivery.webhook_id == wid).order_by(WebhookDelivery.id.desc()).limit(limit)
    ).all()
    return [_delivery_out(d, pieno) for d in righe]


@router.post("/deliveries/{did}/redeliver", response_model=DeliveryOut, status_code=status.HTTP_202_ACCEPTED)
def redeliver(did: int, session: Session = Depends(get_session), _: User = Depends(require_superuser)):
    d = session.get(WebhookDelivery, did)
    if d is None:
        raise HTTPException(status_code=404, detail="Consegna non trovata")
    return _delivery_out(svc.rispedisci(session, d), True)
