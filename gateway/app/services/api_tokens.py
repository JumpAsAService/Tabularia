"""Token API: crearli, riconoscerli, revocarli.

Un token è `tab_` seguito da 40 caratteri casuali. Si mostra una volta, alla
creazione; nel database resta solo la sua impronta SHA-256 (il segreto ha già
240 bit di caso: un hash lento come bcrypt non aggiungerebbe niente, e costerebbe
un quarto di secondo a OGNI richiesta). Il gateway lo riconosce dal prefisso e lo
cerca per impronta nella stessa lettura che carica l'utente (deps/auth.py).

Due livelli: `read` respinge ogni richiesta che modifica qualcosa, `write` vale
quanto il suo utente. In entrambi i casi i permessi sono quelli dell'utente: un
token non ne aggiunge mai.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import ApiToken, User

PREFISSO = "tab_"

# messaggi per chi integra: nella lingua della richiesta (Accept-Language)
SOLA_LETTURA = {
    "en": "Read-only token: this request changes data",
    "it": "Token di sola lettura: questa richiesta modifica dati",
    "fr": "Jeton en lecture seule : cette requête modifie des données",
    "de": "Nur-Lese-Token: Diese Anfrage ändert Daten",
    "es": "Token de solo lectura: esta petición modifica datos",
}
NIENTE_TOKEN_DA_TOKEN = {
    "en": "An API token cannot create credentials: do it from the interface",
    "it": "Un token API non può creare credenziali: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas créer d'identifiants : faites-le depuis l'interface",
    "de": "Ein API-Token kann keine Zugangsdaten erstellen: Das geht über die Oberfläche",
    "es": "Un token de API no puede crear credenciales: hazlo desde la interfaz",
}
NIENTE_RUOLI_DA_TOKEN = {
    "en": "An API token cannot make an account administrator or observer, nor reactivate it: do it from the interface",
    "it": "Un token API non rende un account amministratore od osservatore, né lo riattiva: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas rendre un compte administrateur ou observateur, ni le réactiver : faites-le depuis l'interface",
    "de": "Ein API-Token kann ein Konto weder zum Administrator oder Beobachter machen noch reaktivieren: Das geht über die Oberfläche",
    "es": "Un token de API no puede hacer una cuenta administradora u observadora, ni reactivarla: hazlo desde la interfaz",
}
NIENTE_IMPOSTAZIONI_DA_TOKEN = {
    "en": "An API token cannot change the engine settings: do it from the interface",
    "it": "Un token API non cambia le impostazioni del motore: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas modifier les réglages du moteur : faites-le depuis l'interface",
    "de": "Ein API-Token kann die Engine-Einstellungen nicht ändern: Das geht über die Oberfläche",
    "es": "Un token de API no puede cambiar los ajustes del motor: hazlo desde la interfaz",
}
NIENTE_SIEM_DA_TOKEN = {
    "en": "An API token cannot change where the audit log is sent: do it from the interface",
    "it": "Un token API non cambia dove va il registro di audit: si fa dall'interfaccia",
    "fr": "Un jeton d'API ne peut pas modifier la destination du journal d'audit : faites-le depuis l'interface",
    "de": "Ein API-Token kann nicht ändern, wohin das Audit-Protokoll geht: Das geht über die Oberfläche",
    "es": "Un token de API no puede cambiar adónde va el registro de auditoría: hazlo desde la interfaz",
}
# le POST che leggono soltanto: un token di sola lettura le può usare (un export o
# un'anteprima calcolano e restituiscono dati, non cambiano niente)
POST_DI_LETTURA = {"/tasks/export", "/tasks/preview"}


def messaggio(tabella: dict[str, str], request: Optional[Request]) -> str:
    from app.core.lingua import lingua_della_richiesta

    return tabella.get(lingua_della_richiesta(request), tabella["en"])
SCOPES = ("read", "write")
# l'ultimo uso si scrive al più una volta al minuto, come l'attività delle sessioni
USO_OGNI = timedelta(seconds=60)


def adesso() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def impronta(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def e_un_token(credenziale: str) -> bool:
    return credenziale.startswith(PREFISSO)


def crea(
    session: Session, user: User, name: str, scope: str, giorni: int, creato_da: Optional[User],
) -> tuple[ApiToken, str]:
    """Crea il token e torna la riga e il segreto (che da qui in poi non si rivede).
    Solleva 422 se nome, livello o scadenza non vanno."""
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=422, detail="Il nome del token è obbligatorio (al più 80 caratteri)")
    if scope not in SCOPES:
        raise HTTPException(status_code=422, detail="Livello del token: 'read' o 'write'")
    massimo = get_settings().auth.api_token_max_days
    if not 1 <= giorni <= massimo:
        raise HTTPException(status_code=422, detail=f"La scadenza va da 1 a {massimo} giorni")
    segreto = PREFISSO + secrets.token_urlsafe(30)
    ora = adesso()
    riga = ApiToken(
        user_id=user.id, name=name, prefix=segreto[:12], token_hash=impronta(segreto), scope=scope,
        created_at=ora, created_by=creato_da.id if creato_da else None, expires_at=ora + timedelta(days=giorni),
    )
    session.add(riga)
    session.commit()
    session.refresh(riga)
    return riga, segreto


def di(session: Session, user_id: int) -> list[ApiToken]:
    """I token ancora validi di un utente, dal più recente."""
    return list(session.exec(
        select(ApiToken).where(ApiToken.user_id == user_id, ApiToken.expires_at > adesso())
        .order_by(ApiToken.created_at.desc())
    ).all())


def revoca(session: Session, token_id: int, user_id: Optional[int] = None) -> Optional[ApiToken]:
    """Cancella il token (di `user_id`, se dato). Torna la riga com'era, o None."""
    riga = session.get(ApiToken, token_id)
    if riga is None or (user_id is not None and riga.user_id != user_id):
        return None
    copia = ApiToken(**{c: getattr(riga, c) for c in ApiToken.model_fields})
    session.exec(delete(ApiToken).where(ApiToken.id == token_id))
    session.commit()
    return copia


def revoca_tutti(session: Session, user_id: int, commit: bool = True) -> int:
    n = session.exec(delete(ApiToken).where(ApiToken.user_id == user_id)).rowcount or 0
    if commit:
        session.commit()
    return n


def pulisci(session: Session) -> int:
    """Toglie i token scaduti (lo chiama lo scheduler)."""
    n = session.exec(delete(ApiToken).where(ApiToken.expires_at <= adesso())).rowcount or 0
    session.commit()
    return n


def controlla_livello(riga: ApiToken, request: Request) -> None:
    """Un token di sola lettura non modifica niente: è la stessa separazione per
    METODO dell'osservatore, quindi nessuna rotta nuova può sfuggirle. Fanno
    eccezione solo le POST elencate in POST_DI_LETTURA, che leggono soltanto."""
    if riga.scope != "read" or request.method in ("GET", "HEAD", "OPTIONS"):
        return
    rotta = getattr((getattr(request, "scope", None) or {}).get("route"), "path", None)
    if request.method == "POST" and rotta in POST_DI_LETTURA:
        return
    raise HTTPException(status_code=403, detail=messaggio(SOLA_LETTURA, request))


def ritira_se_osservatore(session: Session, user_ids) -> int:
    """I token personali di chi è diventato SOLO osservatore (flag, gruppo): un
    osservatore non ne ha (è l'ospite condiviso di una demo), e quelli creati
    prima resterebbero validi. Quelli degli account di servizio no: sono
    dell'integrazione. Nella transazione di chi chiama (senza commit)."""
    from app.models import User
    from app.services.permissions import is_observer_only

    n = 0
    for uid in user_ids:
        u = session.get(User, uid)
        if u is not None and not u.is_service and is_observer_only(session, u):
            n += revoca_tutti(session, uid, commit=False)
    return n


def nome_corrente(request: Optional[Request]) -> Optional[str]:
    """Il nome del token con cui è arrivata la richiesta, se è arrivata con un token
    (lo mette lì `get_current_user`); finisce nel dettaglio dell'audit."""
    stato = getattr(request, "state", None) if request is not None else None
    return getattr(stato, "api_token", None) if stato is not None else None
