import hmac
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.security import hash_password, verify_password
from app.db.session import get_session
from app.deps.auth import get_current_user, require_observer
from app.models import User, UserSession
from app.core.passwords import password_problem
from app.services import audit, first_run, login_throttle, sessions
from app.services.permissions import is_admin, is_observer, is_observer_only, user_group_ids
from app.schemas.models import LoginRequest, Token, MeOut, UtcDateTime
from app.core.routing import RottaCheRilascia

router = APIRouter(route_class=RottaCheRilascia, prefix="/auth", tags=["auth"])

# The hash of a password nobody knows, checked when the account has none of its
# own, so that every refused sign-in costs the same bcrypt time.
_NESSUNO = hash_password(secrets.token_urlsafe(24))


@router.post("/login", response_model=Token)
def login(body: LoginRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    """Apre una sessione. Il token torna nel corpo, per gli script e le API che lo
    mettono nell'intestazione `Authorization`, e in un cookie `HttpOnly` per il
    browser, che così non lo tiene mai dove il JavaScript lo legge."""
    # Freno sui tentativi ripetuti, prima di tutto il resto: a chi è frenato si
    # risponde con una lettura per chiave, senza arrivare a bcrypt (vedi
    # services/login_throttle.py).
    ip = audit.client_ip(request)  # as the server resolved it, never a header taken at its word
    attesa = login_throttle.attesa_richiesta(session, ip, body.email)
    if attesa > 0:
        audit.record_audit(
            session, actor=None, actor_label=body.email, action=audit.LOGIN_FAILED,
            outcome="failure", detail={"reason": "troppi tentativi"}, request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many sign-in attempts. Try again in {int(attesa) + 1} seconds.",
            headers={"Retry-After": str(int(attesa) + 1)},
        )
    user = session.exec(select(User).where(User.email == body.email)).first()
    # l'hash contro cui si controlla la password: la sessione nascerà solo se è
    # ancora quello (vedi sessions.apri)
    verificata = user.hashed_password if user is not None else None
    # utente solo-SSO (nessuna password locale): entra solo dall'IdP; un account di
    # servizio entra solo con un token. bcrypt runs in every case, against a hash
    # nobody knows when there is no password to check: answering an unknown email
    # after one SELECT (~35 ms) and a real one after bcrypt (~300 ms) told which
    # emails have a password account.
    utilizzabile = user is not None and not user.is_service and bool(verificata)
    giusta = verify_password(body.password, verificata if utilizzabile else _NESSUNO)
    if not (utilizzabile and giusta):
        # login fallito: si registra chi ci ha provato e da dove (email tentata)
        audit.record_audit(
            session, actor=user, actor_label=body.email, action=audit.LOGIN_FAILED,
            outcome="failure", detail={"reason": "credenziali errate"}, request=request,
        )
        login_throttle.registra_errore(session, ip, body.email)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
    if not user.is_active:
        audit.record_audit(
            session, actor=user, action=audit.LOGIN_FAILED, outcome="failure",
            detail={"reason": "utente disattivato"}, request=request,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This user has been deactivated")
    login_throttle.registra_successo(session, ip, body.email)
    sessions.chiudi_quella_del_cookie(session, request)
    try:
        riga, token = sessions.apri(session, user, request, "password", password_verificata=verificata)
    except sessions.AccessoSuperato:
        # la password è cambiata (o l'account è stato disattivato) mentre si controllava
        audit.record_audit(
            session, actor=user, actor_label=body.email, action=audit.LOGIN_FAILED,
            outcome="failure", detail={"reason": "password cambiata durante l'accesso"}, request=request,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
    audit.record_audit(session, actor=user, action=audit.LOGIN, request=request)
    sessions.scrivi_cookie(response, request, token, riga)
    # All'interfaccia (che manda l'intestazione anti-CSRF) il token nel corpo non
    # serve, e la pagina lo potrebbe leggere: le basta il cookie HttpOnly.
    if request.headers.get(sessions.CSRF_HEADER):
        return Token(access_token="")
    return Token(access_token=token)


# ── the first start of the try-it stack (services/first_run.py) ──────────────
class SetupStatusOut(BaseModel):
    needed: bool


class SetupRequest(BaseModel):
    email: str
    password: str
    full_name: str = ""


class BootstrapRequest(BaseModel):
    token: str


@router.get("/setup", response_model=SetupStatusOut)
def setup_status(session: Session = Depends(get_session)):
    """Whether the sign-in page must ask for the first administrator: only on a stack
    that allows it (AUTH__FIRST_RUN_SETUP, never in production) and while no account exists."""
    return SetupStatusOut(needed=first_run.needed(session))


@router.post("/setup", response_model=Token, status_code=status.HTTP_201_CREATED)
def setup(body: SetupRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    """Creates the first administrator and signs it in. Possible once: from the moment any
    account exists this answers 409, and on a stack without first-run setup 404."""
    if not first_run.enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    email = body.email.strip().lower()
    if "@" not in email.strip("@") or len(email) > 254 or any(c.isspace() for c in email):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(status_code=422, detail=f"Password: {problem}.")
    first_run.lock(session)
    if first_run.accounts_exist(session):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The administrator already exists: sign in.")
    user = User(email=email, full_name=body.full_name.strip()[:200] or "Administrator",
                hashed_password=hash_password(body.password), is_active=True, is_superuser=True)
    session.add(user)
    session.commit()
    session.refresh(user)
    audit.record_audit(session, actor=user, action=audit.FIRST_RUN_SETUP, target_type="user",
                       target_id=user.id, target_label=user.email, request=request)
    riga, token = sessions.apri(session, user, request, "password", password_verificata=user.hashed_password)
    audit.record_audit(session, actor=user, action=audit.LOGIN, request=request)
    sessions.scrivi_cookie(response, request, token, riga)
    if request.headers.get(sessions.CSRF_HEADER):
        return Token(access_token="")
    return Token(access_token=token)


@router.post("/bootstrap", response_model=Token)
def bootstrap(body: BootstrapRequest, request: Request, session: Session = Depends(get_session)):
    """The sample-data container's sign-in: the start-up token from SECURITY__KEYS_DIR, for
    a session as the first administrator. 409 until that administrator exists (the
    container waits for it); 404 on a stack without first-run setup or token."""
    expected = first_run.bootstrap_token()
    if not expected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if not hmac.compare_digest(body.token.encode(), expected.encode()):
        audit.record_audit(session, actor=None, actor_label="bootstrap token", action=audit.LOGIN_FAILED,
                           outcome="failure", detail={"reason": "wrong start-up token"}, request=request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong start-up token")
    admin = first_run.first_administrator(session)
    if admin is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="No administrator yet: create it at the sign-in page first.")
    _riga, token = sessions.apri(session, admin, request, "bootstrap")
    audit.record_audit(session, actor=admin, action=audit.BOOTSTRAP_LOGIN, request=request)
    return Token(access_token=token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Chiude la sessione con cui arriva la richiesta: da qui il suo token non vale
    più, ovunque sia stato copiato."""
    sid = sessions.sid_corrente(request)
    if sid:
        sessions.chiudi(session, sid, user.id)
    audit.record_audit(session, actor=user, action=audit.LOGOUT, request=request)
    risposta = Response(status_code=status.HTTP_204_NO_CONTENT)
    sessions.cancella_cookie(risposta, request)
    return risposta


class SessionOut(BaseModel):
    """Una sessione aperta, come la vede chi la possiede."""
    id: str
    created_at: UtcDateTime
    last_seen_at: UtcDateTime
    expires_at: UtcDateTime
    ip: Optional[str]
    user_agent: Optional[str]
    method: str
    current: bool  # quella da cui arriva la domanda


def _session_out(s: UserSession, corrente: Optional[str]) -> SessionOut:
    return SessionOut(
        id=s.id, created_at=s.created_at, last_seen_at=s.last_seen_at, expires_at=s.expires_at,
        ip=s.ip, user_agent=s.user_agent, method=s.method, current=s.id == corrente,
    )


# quante sessioni si elencano al massimo (uno script che entra a ogni giro ne
# apre una per volta: valgono 12 ore)
ELENCO_MASSIMO = 200


def _condiviso(session: Session, user: User) -> bool:
    """Un OSSERVATORE e basta. È il ruolo dell'ospite di una demo pubblica, un
    account che entra in tanti: le sessioni degli altri non sono sue da vedere né
    da chiudere, e chiuderle butterebbe fuori tutti i visitatori."""
    from app.services.permissions import is_observer_only

    return is_observer_only(session, user)


@router.get("/sessions", response_model=list[SessionOut])
def my_sessions(request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Dove si è collegati adesso: un browser, un altro computer, uno script.
    All'osservatore solo la sua (vedi `_condiviso`)."""
    corrente = sessions.sid_corrente(request)
    if _condiviso(session, user):
        propria = session.get(UserSession, corrente) if corrente else None
        return [_session_out(propria, corrente)] if propria is not None else []
    return [_session_out(s, corrente) for s in sessions.aperte(session, user.id, limite=ELENCO_MASSIMO)]


@router.delete("/sessions/{sid}", status_code=status.HTTP_204_NO_CONTENT)
def close_my_session(
    sid: str, request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    """Chiude una delle proprie sessioni. Quelle degli altri non si vedono: 404.
    L'osservatore chiude solo quella da cui chiede, cioè esce."""
    if sid != sessions.sid_corrente(request) and _condiviso(session, user):
        raise HTTPException(status_code=403, detail="Observers can only close their own session")
    if not sessions.chiudi(session, sid, user.id):
        raise HTTPException(status_code=404, detail="Session not found")
    audit.record_audit(
        session, actor=user, action=audit.SESSION_REVOKE, request=request,
        target_type="user", target_id=user.id, target_label=user.email,
        detail={"sessions": 1, "current": sid == sessions.sid_corrente(request)},
    )
    risposta = Response(status_code=status.HTTP_204_NO_CONTENT)
    if sid == sessions.sid_corrente(request):
        sessions.cancella_cookie(risposta, request)
    return risposta


@router.post("/sessions/close-others", status_code=status.HTTP_204_NO_CONTENT)
def close_other_sessions(request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """«Esci dagli altri dispositivi»: resta aperta solo questa. Non per
    l'osservatore (vedi `_condiviso`)."""
    if _condiviso(session, user):
        raise HTTPException(status_code=403, detail="Observers can only close their own session")
    n = sessions.chiudi_tutte(session, user.id, tranne=sessions.sid_corrente(request))
    if n:
        audit.record_audit(
            session, actor=user, action=audit.SESSION_REVOKE, request=request,
            target_type="user", target_id=user.id, target_label=user.email, detail={"sessions": n},
        )
    return None


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    from app.models import Group

    gids = user_group_ids(session, user)
    names = session.exec(select(Group.name).where(Group.id.in_(gids))).all() if gids else []
    return MeOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        # EFFETTIVO: flag personale o gruppo di amministratori. È ciò con cui il
        # frontend decide se mostrare l'amministrazione.
        is_superuser=is_admin(session, user),
        is_observer=is_observer(session, user),
        groups=list(names),
        # no local password: the settings menu says the identity provider manages it
        sso_only=user.hashed_password is None,
        # the language the emails to this user speak (PUT /users/me/locale)
        locale=user.locale,
        # la pagina esce da sé dopo questi minuti senza che nessuno la tocchi
        session_idle_minutes=get_settings().auth.session_idle_minutes,
        # niente token personali per l'osservatore (vedi routes/api_tokens.py)
        personal_tokens=get_settings().auth.personal_tokens and not is_observer_only(session, user),
        api_token_max_days=get_settings().auth.api_token_max_days,
    )


@router.get("/observer", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_observer)], include_in_schema=False)
def puo_osservare() -> None:
    """«Può vedere il monitoraggio?» — sì (204) per amministratori e osservatori,
    401/403 per gli altri. La chiama il reverse proxy (`forward_auth`) prima di
    lasciar passare una richiesta verso Grafana, che di suo è anonimo perché
    l'iframe non può condividere la sessione dell'app. Nessun corpo, nessuna
    query oltre a quelle della guardia: viene chiamata a ogni richiesta."""
    return None
