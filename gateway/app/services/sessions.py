"""Sessioni: aprirle, riconoscerle, chiuderle.

Ogni accesso riuscito (password o SSO) apre una riga in `user_sessions` e riceve
un token che ne porta l'id. Il browser lo tiene in un cookie `HttpOnly`, che il
JavaScript della pagina non può leggere: uno script iniettato non se lo porta
via. Gli script e le API lo usano come prima, nell'intestazione `Authorization`.

Chiudere una sessione è cancellarne la riga: il token, ancora firmato e non
scaduto, smette di valere alla richiesta dopo. Lo fanno l'uscita, la revoca
(propria o dell'amministratore), il cambio di password, la disattivazione
dell'account, e la pulizia periodica di quelle scadute o ferme da troppo.

Il cookie va con `SameSite=Lax` e, quando la pagina è in HTTPS, `Secure`. Contro
le richieste fatte da altri siti c'è in più l'intestazione `X-Tabularia-Csrf`:
una richiesta che modifica qualcosa e si presenta col solo cookie deve averla
(vedi deps/auth.py). Un modulo HTML di un altro sito non la può aggiungere, e
una fetch da un'origine che CORS non ammette non passa la verifica preliminare.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request, Response
from sqlalchemy import and_, delete, func, not_
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models import User, UserSession

COOKIE = "tab_token"
CSRF_HEADER = "x-tabularia-csrf"

# l'ultima attività di una sessione si scrive al più una volta al minuto: è anche
# la precisione del timeout per inattività
ATTIVITA_OGNI = timedelta(seconds=60)


def adesso() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AccessoSuperato(Exception):
    """La password verificata non è più quella dell'account (o l'account è stato
    disattivato) mentre l'accesso era in corso."""


def apri(
    session: Session, user: User, request: Optional[Request], metodo: str,
    password_verificata: Optional[str] = None,
) -> tuple[UserSession, str]:
    """Apre una sessione per `user` e torna la riga e il suo token.

    `password_verificata` è l'hash contro cui il login ha controllato la password.
    Fra quel controllo (bcrypt, un quarto di secondo) e questa riga un
    amministratore può cambiare la password e chiudere le sessioni: la riga
    dell'utente si legge qui BLOCCATA (FOR SHARE) e la sessione nasce solo se la
    password è ancora quella. Il blocco mette in fila i due: o il cambio aspetta
    che questa sessione esista (e la chiude), o questa vede la password nuova e
    non nasce. Altrimenti chi conosceva la vecchia, provando a ripetizione,
    potrebbe restare dentro fino alla scadenza."""
    from app.services import audit
    from app.services.permissions import is_observer_only

    adesso_in_db = session.exec(
        select(User.hashed_password, User.is_active).where(User.id == user.id).with_for_update(read=True)
    ).first()
    if adesso_in_db is None or not adesso_in_db[1] or (
        password_verificata is not None and adesso_in_db[0] != password_verificata
    ):
        session.rollback()
        raise AccessoSuperato()
    ora = adesso()
    # di un OSSERVATORE non si tiene da dove e con che cosa entra, come nell'audit
    minimizza = is_observer_only(session, user)
    agente = (request.headers.get("user-agent") or "")[:512] if request is not None else ""
    riga = UserSession(
        id=secrets.token_urlsafe(24),
        user_id=user.id,
        created_at=ora,
        last_seen_at=ora,
        expires_at=ora + timedelta(minutes=get_settings().jwt.access_ttl_minutes),
        ip=None if minimizza else audit.client_ip(request),
        user_agent=None if minimizza else (agente or None),
        method=metodo,
    )
    session.add(riga)
    session.commit()
    session.refresh(riga)
    return riga, create_access_token(user.id, riga.id, riga.expires_at)


def valida(ora: Optional[datetime] = None):
    """LA regola, in SQL, per chi controlla un token, chi elenca e chi pulisce:
    entro la durata massima e, col timeout per inattività, usata di recente. Al
    timeout si aggiunge il minuto con cui si scrive l'attività: una sessione usata
    trenta secondi fa non deve sembrare ferma da più del dovuto."""
    ora = ora or adesso()
    condizione = UserSession.expires_at > ora
    minuti = get_settings().auth.session_idle_minutes
    if minuti > 0:
        condizione = and_(condizione, UserSession.last_seen_at >= ora - timedelta(minutes=minuti) - ATTIVITA_OGNI)
    return condizione


def chiudi(session: Session, sid: str, user_id: Optional[int] = None) -> bool:
    """Chiude una sessione (di `user_id`, se dato). Torna se c'era."""
    q = delete(UserSession).where(UserSession.id == sid)
    if user_id is not None:
        q = q.where(UserSession.user_id == user_id)
    n = session.exec(q).rowcount or 0
    session.commit()
    return n > 0


def chiudi_tutte(session: Session, user_id: int, tranne: Optional[str] = None, commit: bool = True) -> int:
    """Chiude tutte le sessioni di un utente, tranne eventualmente una. Torna quante.
    `commit=False` la lascia nella transazione di chi chiama: una password nuova e la
    chiusura delle sessioni vanno insieme, o nessuna delle due."""
    q = delete(UserSession).where(UserSession.user_id == user_id)
    if tranne:
        q = q.where(UserSession.id != tranne)
    n = session.exec(q).rowcount or 0
    if commit:
        session.commit()
    return n


def aperte(session: Session, user_id: Optional[int] = None, limite: Optional[int] = None) -> list[UserSession]:
    """Le sessioni ancora valide (di un utente, o di tutti), dalla più recente.
    `limite`: su una demo pubblica ogni accesso dell'ospite è una riga."""
    q = select(UserSession).where(valida()).order_by(UserSession.last_seen_at.desc())
    if user_id is not None:
        q = q.where(UserSession.user_id == user_id)
    if limite is not None:
        q = q.limit(limite)
    return list(session.exec(q).all())


def quante_aperte(session: Session) -> int:
    return session.exec(select(func.count()).select_from(UserSession).where(valida())).one()


def pulisci(session: Session) -> int:
    """Toglie le sessioni scadute o ferme da troppo (lo chiama lo scheduler):
    con loro se ne vanno gli indirizzi."""
    n = session.exec(delete(UserSession).where(not_(valida()))).rowcount or 0
    session.commit()
    return n


def chiudi_quella_del_cookie(session: Session, request: Request) -> None:
    """Un accesso nuovo dallo stesso browser prende il posto del cookie di prima:
    la sessione a cui apparteneva si chiude, invece di restare fino alla scadenza
    come un «altro dispositivo» che non c'è più."""
    vecchio = (getattr(request, "cookies", None) or {}).get(COOKIE)
    if not vecchio:
        return
    from app.core.security import decode_access_token

    try:
        sid = str(decode_access_token(vecchio)["sid"])
    except Exception:  # scaduto, manomesso, di prima delle sessioni: niente da chiudere
        return
    chiudi(session, sid)


def maniglia(sid: str) -> str:
    """Un nome stabile per una sessione, da mostrare a chi non la può chiudere
    (l'osservatore): distingue le righe senza consegnare l'id vero."""
    return "h-" + hashlib.sha256(sid.encode()).hexdigest()[:16]


def sid_corrente(request: Request) -> Optional[str]:
    """L'id della sessione con cui è arrivata la richiesta (lo mette lì
    `get_current_user`)."""
    stato = getattr(request, "state", None)
    return getattr(stato, "sid", None) if stato is not None else None


# ── il cookie ────────────────────────────────────────────────────────────────

def _in_https(request: Request) -> bool:
    """Dietro un proxy che termina il TLS il gateway riceve http: conta quel che il
    proxy dichiara. Chi falsificasse l'intestazione toglierebbe `Secure` solo al
    proprio cookie."""
    if getattr(getattr(request, "url", None), "scheme", "") == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"


def scrivi_cookie(response: Response, request: Request, token: str, riga: UserSession) -> None:
    """Percorso `/`: lo deve vedere anche il server dell'interfaccia, che decide
    se mandare alla pagina di accesso (per lo stesso motivo, con l'interfaccia su
    un altro host, AUTH__COOKIE_DOMAIN). Dura quanto la sessione."""
    secondi = max(0, int((riga.expires_at - adesso()).total_seconds()))
    response.set_cookie(
        COOKIE, token, max_age=secondi, path="/", domain=get_settings().auth.cookie_domain or None,
        httponly=True, samesite="lax", secure=_in_https(request),
    )


def cancella_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        COOKIE, path="/", domain=get_settings().auth.cookie_domain or None,
        httponly=True, samesite="lax", secure=_in_https(request),
    )


def intestazione_che_cancella(request: Request) -> str:
    """Lo stesso `Set-Cookie` di `cancella_cookie`, da mettere in un'eccezione."""
    r = Response()
    cancella_cookie(r, request)
    return r.headers["set-cookie"]
