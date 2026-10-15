"""Impostazioni del motore (Amministrazione → Impostazioni): i parametri che
l'amministratore cambia senza toccare l'ambiente e senza riavvii.

Lettura per amministratori e osservatori; scrittura solo amministratori, mai con
un token API, e ogni modifica nell'audit col valore di prima e quello dopo. Con
ENGINE__SETTINGS_PANEL=false si vedono e non si cambiano. Quali parametri, con che
limiti e come arrivano ai processi del motore: services/regolazioni_motore.py.
"""
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt
from sqlmodel import Session

from app.core.routing import RottaCheRilascia
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import EngineSetting, User
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit
from app.services import regolazioni_motore as rm
from app.services.permissions import is_admin
from app.services.masking import mask_email

router = APIRouter(route_class=RottaCheRilascia, prefix="/admin/engine-settings", tags=["settings"])


class SettingOut(BaseModel):
    key: str
    category: str
    unit: str  # rows | seconds | bytes | threads
    min: int
    max: int
    env_var: str
    env_value: int  # quello dell'ambiente: il valore di partenza
    value: Optional[int]  # quello impostato dal pannello (None = vale l'ambiente)
    applied: Optional[int]  # quello che il motore sta applicando adesso (None = l'ambiente)
    updated_at: Optional[UtcDateTime] = None
    updated_by: Optional[str] = None


class SettingsOut(BaseModel):
    items: list[SettingOut]
    editable: bool  # chi guarda può cambiarle (amministratore, pannello acceso, non da token)
    panel_enabled: bool  # ENGINE__SETTINGS_PANEL
    unavailable: list[str]  # categorie dei motori che questa installazione non ha
    refresh_seconds: float  # entro quanto ogni processo del motore vede una modifica


class SettingsUpdate(BaseModel):
    # solo quelle che cambiano: un numero INTERO imposta (non un testo, non un true:
    # `true` diventerebbe 1 senza che il motore lo veda), null torna all'ambiente
    values: dict[str, Optional[StrictInt]] = Field(max_length=50)


def _niente_token(request: Request, _admin: User = Depends(require_superuser)) -> None:
    """Prima di leggere il corpo: a un token si risponde 403, anche se il corpo è
    sbagliato. DOPO l'autenticazione (la dipendenza qui sopra): è lei che scrive
    nella richiesta se è arrivata con un token — prima non lo si saprebbe, e il
    token passerebbe (successo provando, 2026-10-12)."""
    from app.routes.api_tokens import nega_se_da_token

    nega_se_da_token(request, api_tokens.NIENTE_IMPOSTAZIONI_DA_TOKEN)


# un motore di prima di queste impostazioni (aggiornamento a metà, o fatto prima del
# gateway): risponde 404, e «non risponde» sarebbe la diagnosi sbagliata
MOTORE_VECCHIO = "The engine is an older version that does not support the settings panel: update it"


async def _stato(session: Session, chi: User, request: Request) -> SettingsOut:
    try:
        cat = await rm.catalogo()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=503, detail=MOTORE_VECCHIO)
        raise HTTPException(status_code=503, detail="The engine is not responding: the settings cannot be read right now")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="The engine is not responding: the settings cannot be read right now")
    righe = rm.salvate(session)
    pieno = is_admin(session, chi)
    voci = []
    for v in cat["items"]:
        r = righe.get(v["key"])
        chi_lha = r.updated_by if r is not None else None
        voci.append(SettingOut(
            key=v["key"], category=v["category"], unit=v["unit"], min=v["min"], max=v["max"],
            env_var=v["env_var"], env_value=v["env_value"],
            value=r.value if r is not None and rm.dal_pannello() else None,
            applied=v["value"],
            updated_at=r.updated_at if r is not None else None,
            updated_by=(chi_lha if pieno else mask_email(chi_lha)) if chi_lha else None,
        ))
    return SettingsOut(
        items=voci, editable=pieno and rm.dal_pannello() and not api_tokens.nome_corrente(request),
        panel_enabled=rm.dal_pannello(), unavailable=cat.get("unavailable", []),
        refresh_seconds=cat.get("refresh_seconds", 10),
    )


@router.get("", response_model=SettingsOut)
async def leggi(request: Request, session: Session = Depends(get_session), chi: User = Depends(require_observer)):
    return await _stato(session, chi, request)


@router.put("", response_model=SettingsOut, dependencies=[Depends(_niente_token)])
async def scrivi(
    body: SettingsUpdate, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Cambia quelle indicate. Prima le controlla e le applica il motore (tutto o
    niente: un 422 dice quali e perché, con un codice che il pannello traduce), poi
    si salvano: quello che si legge qui è sempre quello che il motore applica."""
    _niente_token(request)  # la dipendenza l'ha già fatto: qui resta se un giorno la si toglie
    if not rm.dal_pannello():
        raise HTTPException(status_code=403, detail="On this installation the engine settings can only be changed in the environment (ENGINE__SETTINGS_PANEL=false)")
    # in fila con gli altri salvataggi e col giro periodico: si legge, si spedisce
    # e si salva tenendo il lucchetto, così il motore riceve sempre l'insieme intero
    if not await rm.blocca(session):
        raise HTTPException(status_code=409, detail="Another change to the settings is in progress: try again in a moment")
    try:
        note = {v["key"] for v in (await rm.catalogo())["items"]}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=503, detail=MOTORE_VECCHIO + ": nothing was changed")
        raise HTTPException(status_code=503, detail="The engine is not responding: nothing was changed")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="The engine is not responding: nothing was changed")
    sconosciute = [k for k in body.values if k not in note]
    if sconosciute:
        return JSONResponse(status_code=422, content={"detail": {"errors": [{"key": k, "code": "unknown"} for k in sconosciute]}})
    righe = rm.salvate(session)
    prima = {k: r.value for k, r in righe.items()}
    dopo = dict(prima)
    for k, v in body.values.items():
        if v is None:
            dopo.pop(k, None)
        else:
            dopo[k] = v
    cambi = {k: {"from": prima.get(k), "to": dopo.get(k)} for k in body.values if prima.get(k) != dopo.get(k)}
    if not cambi:
        return await _stato(session, admin, request)
    try:
        # si parte da quelle salvate che il motore accetta ancora: una che non vale
        # più (l'ambiente è cambiato sotto) non deve bloccare le modifiche alle altre
        valide = await rm.spingi_scartando(prima)
        invio = {**{k: v for k, v in valide.items() if k not in body.values},
                 **{k: v for k, v in body.values.items() if v is not None}}
        r = await rm.spingi(invio, cambiate=list(cambi))
    except (httpx.HTTPError, RuntimeError):
        raise HTTPException(status_code=503, detail="The engine is not responding: nothing was changed")
    if r.status_code == 422:
        return JSONResponse(status_code=422, content=r.json())
    if r.status_code == 404:
        raise HTTPException(status_code=503, detail=MOTORE_VECCHIO + ": nothing was changed")
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"The engine answered {r.status_code}: nothing was changed")
    ora = datetime.now(timezone.utc).replace(tzinfo=None)
    for k in cambi:
        if k in dopo:
            riga = righe.get(k) or EngineSetting(key=k, value=dopo[k], updated_at=ora)
            riga.value, riga.updated_at, riga.updated_by = dopo[k], ora, admin.email
            session.add(riga)
        elif k in righe:
            session.delete(righe[k])
    try:
        session.commit()
    except Exception:
        session.rollback()
        # il motore le ha già: si rimettono quelle di prima, che sono quelle salvate
        try:
            await rm.spingi_scartando(prima)
        except (httpx.HTTPError, RuntimeError):
            pass  # le rimette a posto il giro periodico (spingi_loop)
        raise
    audit.record_audit(
        session, actor=admin, action=audit.ENGINE_SETTINGS_UPDATE, request=request,
        target_type="engine_settings", target_label="engine", detail={"changes": cambi},
    )
    return await _stato(session, admin, request)
