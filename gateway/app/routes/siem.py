"""SIEM (Amministrazione → SIEM): dove va il registro di audit, e come sta andando.

Per ora Splunk, con l'HTTP Event Collector (vedi services/siem.py). Lettura per
amministratori e osservatori (all'osservatore niente indirizzo completo, niente
errore di Splunk); scrittura e prova solo amministratori, mai con un token API.
Il token di Splunk si scrive e non si rilegge: si vede solo che c'è.
"""
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.crypto import encrypt_secret
from app.core.routing import RottaCheRilascia
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import SiemDestination, User
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit, siem, webhooks
from app.services.permissions import is_admin
from sqlmodel import Session

router = APIRouter(route_class=RottaCheRilascia, prefix="/admin/siem", tags=["siem"])


class SiemOut(BaseModel):
    kind: str
    enabled: bool
    url: str  # all'osservatore solo schema e host
    token_set: bool
    index: Optional[str]
    source: Optional[str]
    sourcetype: Optional[str]
    host: Optional[str]
    verify_tls: bool
    editable: bool
    # come sta andando
    sent_total: int
    backlog: int  # voci dell'audit che aspettano di partire
    oldest_pending_at: Optional[UtcDateTime]
    last_ok_at: Optional[UtcDateTime]
    last_error: Optional[str]  # non all'osservatore
    last_error_at: Optional[UtcDateTime]
    failures: int
    next_attempt_at: Optional[UtcDateTime]


class SiemUpdate(BaseModel):
    # solo quelli che cambiano; il token: un testo lo sostituisce, "" lo toglie
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


def _niente_token(request: Request, _admin: User = Depends(require_superuser)) -> None:
    """Dopo l'autenticazione (la dipendenza qui sopra), prima di leggere il corpo."""
    from app.routes.api_tokens import nega_se_da_token

    nega_se_da_token(request, api_tokens.NIENTE_SIEM_DA_TOKEN)


def _destinazione(session: Session) -> SiemDestination:
    return session.get(SiemDestination, 1) or SiemDestination(id=1)


def _out(session: Session, d: SiemDestination, chi: User, request: Request) -> SiemOut:
    pieno = is_admin(session, chi)
    n, piu_vecchia = siem.arretrato(session, d) if d.enabled else (0, None)
    url = d.url
    if url and not pieno:
        parti = urlsplit(url)
        url = f"{parti.scheme}://{parti.hostname}/…"
    return SiemOut(
        kind=d.kind, enabled=d.enabled, url=url, token_set=bool(d.token_enc), index=d.index, source=d.source,
        sourcetype=d.sourcetype, host=d.host, verify_tls=d.verify_tls,
        editable=pieno and not api_tokens.nome_corrente(request),
        sent_total=d.sent_total or 0, backlog=n, oldest_pending_at=piu_vecchia, last_ok_at=d.last_ok_at,
        last_error=d.last_error if pieno else None, last_error_at=d.last_error_at,
        failures=d.failures or 0, next_attempt_at=d.next_attempt_at,
    )


@router.get("", response_model=SiemOut)
def leggi(request: Request, session: Session = Depends(get_session), chi: User = Depends(require_observer)):
    return _out(session, _destinazione(session), chi, request)


def _testo(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    return v or None


@router.put("", response_model=SiemOut, dependencies=[Depends(_niente_token)])
def scrivi(
    body: SiemUpdate, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Cambia quello che è indicato. Accendere vuole indirizzo e token; la prima
    volta l'inoltro parte da adesso (lo storico resta nel pannello Audit). Spento e
    riacceso riprende da dove era rimasto: niente buchi."""
    d = _destinazione(session)
    prima = {"enabled": d.enabled, "url": d.url, "index": d.index, "source": d.source,
             "sourcetype": d.sourcetype, "host": d.host, "verify_tls": d.verify_tls}
    cambi: dict = {}
    if body.url is not None:
        url = body.url.strip()
        if url:
            try:
                webhooks.valida_url(url)  # http(s), un host, leggibile da httpx
                webhooks.valida_url(siem.endpoint(url))
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
        d.url = url
    for campo in ("index", "source", "sourcetype", "host"):
        v = getattr(body, campo)
        if v is not None:
            setattr(d, campo, _testo(v))
    if body.verify_tls is not None:
        d.verify_tls = body.verify_tls
    if body.token is not None:
        d.token_enc = encrypt_secret(body.token.strip()) if body.token.strip() else None
        cambi["token"] = "changed" if body.token.strip() else "removed"
    if body.enabled is not None:
        d.enabled = body.enabled
    if d.enabled and not (d.url and d.token_enc):
        raise HTTPException(status_code=422, detail="Per accendere l'inoltro servono l'indirizzo e il token di Splunk")
    if d.enabled and not prima["enabled"]:
        if not d.cursor_id:
            d.cursor_id = siem.ultimo_id(session)  # da adesso: lo storico resta nel pannello Audit
        d.failures, d.next_attempt_at = 0, None
    if any(k in body.model_fields_set for k in ("url", "token", "verify_tls", "index")):
        d.failures, d.next_attempt_at = 0, None  # configurazione nuova: si riprova subito
    for k, v in prima.items():
        if getattr(d, k) != v:
            cambi[k] = {"from": v, "to": getattr(d, k)}
    if not cambi:
        return _out(session, d, admin, request)
    from datetime import datetime, timezone

    d.updated_at, d.updated_by = datetime.now(timezone.utc).replace(tzinfo=None), admin.email
    session.add(d)
    session.commit()
    session.refresh(d)
    audit.record_audit(
        session, actor=admin, action=audit.SIEM_UPDATE, request=request,
        target_type="siem", target_label=urlsplit(d.url).hostname if d.url else None, detail={"changes": cambi},
    )
    return _out(session, d, admin, request)


@router.post("/test", response_model=SiemTestOut, dependencies=[Depends(_niente_token)])
async def prova(request: Request, session: Session = Depends(get_session), admin: User = Depends(require_superuser)):
    """Manda adesso a Splunk un evento di prova (non passa dalla coda) e dice cosa
    ha risposto: token, indice, certificato e rete si verificano qui."""
    d = _destinazione(session)
    if not d.url or not d.token_enc:
        raise HTTPException(status_code=422, detail="Servono l'indirizzo e il token di Splunk")
    ora = siem.adesso()
    ok, codice, messaggio = await siem.spedisci(d, [{
        "time": round(siem._utc(ora).timestamp(), 3), "host": d.host or siem.HOST, "source": d.source or siem.SOURCE,
        "sourcetype": d.sourcetype or siem.SOURCETYPE, **({"index": d.index} if d.index else {}),
        "event": {"action": "siem.test", "outcome": "success", "user": admin.email, "app": "tabularia", "test": True,
                  "message": "Tabularia: prova di consegna a Splunk"},
    }])
    audit.record_audit(
        session, actor=admin, action=audit.SIEM_TEST, request=request, outcome="success" if ok else "failure",
        target_type="siem", target_label=urlsplit(d.url).hostname, detail={"status_code": codice},
    )
    return SiemTestOut(ok=ok, status_code=codice, message=messaggio)
