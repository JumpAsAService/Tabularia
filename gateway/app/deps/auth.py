"""Dependency di autenticazione: prende il token (intestazione o cookie), valida
il JWT, carica l'utente e la sua sessione con una lettura sola."""
import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import exists
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.security import decode_access_token
from app.db.session import engine_di_lettura, get_session
from app.models import ApiToken, Group, User, UserGroupLink, UserSession
from app.services import api_tokens, sessions

logger = logging.getLogger(__name__)

# throttling del last_seen: si aggiorna al massimo una volta ogni N secondi per
# sessione, così non c'è una scrittura DB a ogni richiesta autenticata.
_LAST_SEEN_THROTTLE = sessions.ATTIVITA_OGNI

# metodi che non cambiano niente: col solo cookie non chiedono l'intestazione anti-CSRF
_METODI_SICURI = {"GET", "HEAD", "OPTIONS"}

_credentials_error = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


class _TokenDellaRichiesta(OAuth2PasswordBearer):
    """Il token arriva in due modi: nell'intestazione `Authorization` (script, API,
    il reverse proxy che chiede se lasciar passare verso Grafana) o nel cookie
    `HttpOnly` che il gateway scrive al browser all'accesso. L'intestazione, se
    c'è, vince.

    Col solo cookie, una richiesta che modifica qualcosa deve portare anche
    `X-Tabularia-Csrf`: il cookie il browser lo allega da sé anche a una richiesta
    partita da un altro sito, l'intestazione no (vedi services/sessions.py)."""

    async def __call__(self, request: Request) -> str:  # type: ignore[override]
        dall_intestazione = await super().__call__(request)
        if dall_intestazione:
            return dall_intestazione
        dal_cookie = request.cookies.get(sessions.COOKIE)
        if not dal_cookie:
            raise _credentials_error
        if request.method not in _METODI_SICURI and not request.headers.get(sessions.CSRF_HEADER):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The request is missing the X-Tabularia-Csrf header",
            )
        request.state.token_dal_cookie = True  # se non vale, il 401 cancella il cookie
        return dal_cookie


# tokenUrl solo informativo (Swagger): il login vero è POST /auth/login (JSON).
oauth2_scheme = _TokenDellaRichiesta(tokenUrl="auth/login", auto_error=False)


def _respinto(request: Request) -> HTTPException:
    """Il 401. Se il token respinto era quello del cookie, la risposta lo cancella:
    un cookie che non vale più farebbe credere al server dell'interfaccia che ci
    sia ancora una sessione, a ogni pagina caricata da capo. Se veniva
    dall'intestazione no: il cookie del browser può essere buono (un token di
    script scaduto provato da /docs non deve far uscire dall'interfaccia)."""
    stato = getattr(request, "state", None)
    if not getattr(stato, "token_dal_cookie", False):
        return _credentials_error
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_credentials_error.detail,
        headers={"WWW-Authenticate": "Bearer", "Set-Cookie": sessions.intestazione_che_cancella(request)},
    )


def _vecchio(t: datetime | None, now: datetime) -> bool:
    if t is None:
        return True
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return now - t >= _LAST_SEEN_THROTTLE


def _touch_last_seen(session: Session, user: User, aperta: UserSession, request: Request) -> None:
    """Aggiorna l'ultima attività (throttled) della sessione e dell'utente: la
    prima decide il timeout per inattività, la seconda la colonna «visto» della
    pagina Admin.

    L'indirizzo sta solo nella SESSIONE, e se ne va con lei. `users.last_seen_ip`
    non si scrive più: nessuno lo leggeva, e nessuno l'avrebbe mai cancellato.
    Di un OSSERVATORE l'indirizzo non si tiene nemmeno lì: si aggiorna solo il
    momento, che è quel che serve a dire «c'è qualcuno collegato adesso». Vedi
    `permissions.is_observer_only` per il perché."""
    from app.services.permissions import is_observer_only

    now = datetime.now(timezone.utc)
    if not (_vecchio(user.last_seen_at, now) or _vecchio(aperta.last_seen_at, now)):
        return
    try:
        if is_observer_only(session, user):
            # anche cancellare quello che c'era: il ruolo può essere stato dato
            # dopo, e un indirizzo raccolto prima resterebbe lì fino alla scadenza
            aperta.ip = None
            aperta.user_agent = None
        else:
            fwd = request.headers.get("x-forwarded-for")
            aperta.ip = (fwd.split(",")[0].strip() if fwd
                         else (request.client.host if request.client else None))
        user.last_seen_at = now
        aperta.last_seen_at = now.replace(tzinfo=None)
        session.add(user)
        session.add(aperta)
        session.commit()
    except Exception:  # non deve mai rompere la richiesta
        logger.debug("last_seen non aggiornato per utente %s", user.id, exc_info=True)
        session.rollback()


def _fine_passo(session: Session) -> None:
    """Chiude la transazione di lettura prima di uscire da una dipendenza.

    Ogni dipendenza sincrona gira in un thread suo, e la rotta in un altro. Se
    questa uscisse con la transazione aperta, la richiesta si terrebbe una
    connessione del pool mentre aspetta un thread per il passo successivo — e
    sotto una raffica quei thread sono tutti occupati da richieste che
    aspettano una connessione. Misurato: 60 richieste insieme → gateway fermo
    30 secondi e più di metà in errore.

    `rollback` e non `commit`: qui si è solo letto, e un commit potrebbe rendere
    permanente qualcosa che un chiamante avesse appoggiato sull'oggetto. Gli
    oggetti restano agganciati alla sessione: chi li legge nel passo dopo li
    ricarica, con una connessione presa e resa lì.
    """
    session.rollback()


def _una_lettura(session: Session, q):
    """Esegue `q` (utente + una riga sua) con una connessione presa e resa qui,
    fuori da ogni transazione.

    È la lettura che OGNI richiesta fa. Dentro la sessione della richiesta
    costava quattro giri verso Postgres (ping, BEGIN, la SELECT, il ROLLBACK di
    fine passo) e il rollback faceva scadere l'utente, che la rotta rileggeva un
    attimo dopo. Così è un giro solo: una SELECT per chiave in autocommit vede
    esattamente quello che vedrebbe in una transazione appena aperta (vedi
    `engine_di_lettura`). Gli oggetti passano poi alla sessione della richiesta già
    caricati (`merge(load=False)` non tocca il database), e la rotta trova
    l'utente lì come l'ha sempre trovato."""
    bind = session.get_bind()
    if not isinstance(bind, Engine):  # sessione legata a una connessione già aperta
        return session.exec(q).first()
    with Session(engine_di_lettura(bind)) as lettura:
        riga = lettura.exec(q).first()
        if riga is None:
            return None
        user, altro, *valori = riga  # valori: colonne semplici calcolate nella stessa SELECT
        lettura.expunge(user)
        lettura.expunge(altro)
    return (session.merge(user, load=False), session.merge(altro, load=False), *valori)


def _carica_utente(session: Session, user_id: int, sid: str) -> tuple[User, UserSession] | None:
    """L'utente E la sua sessione, in una SELECT sola con la sessione in JOIN."""
    return _una_lettura(session, (
        select(User, UserSession)
        .join(UserSession, UserSession.user_id == User.id)
        # scaduta o ferma da troppo = come se non ci fosse (vedi sessions.valida)
        .where(User.id == user_id, UserSession.id == sid, sessions.valida())
    ))


def _utente_del_token(request: Request, token: str, session: Session) -> User:
    """Un token API (`tab_…`): l'utente e il token in una SELECT sola, per impronta.
    Solo dall'intestazione `Authorization`: un token API messo nel cookie della
    sessione non vale (il cookie è del browser, il token degli script)."""
    stato = getattr(request, "state", None)
    if getattr(stato, "token_dal_cookie", False):
        raise _respinto(request)
    def in_un_gruppo(flag):
        return exists().where(UserGroupLink.user_id == User.id, UserGroupLink.group_id == Group.id, flag == True)  # noqa: E712

    trovati = _una_lettura(session, (
        # i ruoli dai gruppi nella stessa SELECT: servono a sapere se è SOLO osservatore
        select(User, ApiToken, in_un_gruppo(Group.is_admin).label("admin_g"), in_un_gruppo(Group.is_observer).label("occhio_g"))
        .join(ApiToken, ApiToken.user_id == User.id)
        .where(ApiToken.token_hash == api_tokens.impronta(token), ApiToken.expires_at > api_tokens.adesso())
    ))
    if trovati is None:  # mai esistito, revocato o scaduto
        raise _respinto(request)
    user, riga, admin_g, occhio_g = trovati
    if not user.is_active:
        raise _respinto(request)
    from app.core.config import get_settings

    if not user.is_service and not get_settings().auth.personal_tokens:
        raise _respinto(request)  # token personali spenti: anche quelli già dati
    if not user.is_service and (user.is_observer or occhio_g) and not (user.is_superuser or admin_g):
        # solo osservatore: niente token personali, per qualunque strada ci sia
        # arrivato (flag, gruppi, un ruolo di amministratore perso, l'SSO). Quando
        # lo diventa gli si ritirano; questo copre le strade che non lo fanno
        raise _respinto(request)
    api_tokens.controlla_livello(riga, request)
    if stato is not None:
        stato.api_token = riga.name  # per l'audit: «fatto con il token …»
    _touch_token(session, user, riga, request)
    return user


def _touch_token(session: Session, user: User, riga: ApiToken, request: Request) -> None:
    """Ultimo uso del token (throttled), e l'attività dell'utente come per le
    sessioni. Di un osservatore l'indirizzo non si tiene."""
    from app.services.permissions import is_observer_only

    now = datetime.now(timezone.utc)
    if not (_vecchio(riga.last_used_at, now) or _vecchio(user.last_seen_at, now)):
        return
    try:
        if is_observer_only(session, user):
            riga.last_used_ip = None
        else:
            fwd = request.headers.get("x-forwarded-for")
            riga.last_used_ip = ((fwd.split(",")[0].strip() if fwd
                                  else (request.client.host if request.client else None)) or "")[:64] or None
        riga.last_used_at = now.replace(tzinfo=None)
        user.last_seen_at = now
        session.add(riga)
        session.add(user)
        session.commit()
    except Exception:  # non deve mai rompere la richiesta
        logger.debug("ultimo uso del token non aggiornato (utente %s)", user.id, exc_info=True)
        session.rollback()


def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> User:
    if api_tokens.e_un_token(token):
        return _utente_del_token(request, token, session)
    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
        # un token senza sessione è di prima delle sessioni: non si può revocare,
        # quindi non vale (all'aggiornamento si rientra una volta)
        sid = str(payload["sid"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise _respinto(request)
    trovati = _carica_utente(session, user_id, sid)
    if trovati is None:  # utente sparito, sessione chiusa, scaduta o ferma
        raise _respinto(request)
    user, aperta = trovati
    if not user.is_active:
        raise _respinto(request)
    stato = getattr(request, "state", None)
    if stato is not None:
        stato.sid = sid  # per chi chiude la sessione corrente (vedi sessions.sid_corrente)
    # Niente `_fine_passo` qui: la sessione della richiesta non ha preso
    # connessioni (la lettura ne ha usata una sua, già resa) e `_touch_last_seen`,
    # la volta al minuto che scrive, chiude da sé con commit o rollback. Un
    # rollback in più farebbe solo scadere l'utente appena caricato.
    _touch_last_seen(session, user, aperta, request)
    return user


def require_superuser(
    user: User = Depends(get_current_user), session: Session = Depends(get_session)
) -> User:
    # admin EFFETTIVO: flag personale o gruppo di amministratori
    from app.services.permissions import is_admin

    if not is_admin(session, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    _fine_passo(session)
    return user


def require_observer(
    user: User = Depends(get_current_user), session: Session = Depends(get_session)
) -> User:
    """Per LEGGERE un pannello di amministrazione: amministratore od osservatore.

    Le rotte di scrittura restano su `require_superuser`. La separazione è per
    METODO e non per pannello: un pannello «di sola lettura» che contenga una
    rotta di scrittura sarebbe un buco silenzioso, mentre così ogni POST, PUT e
    DELETE resta chiuso per costruzione."""
    from app.services.permissions import is_observer

    if not is_observer(session, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrative read access required")
    _fine_passo(session)
    return user
