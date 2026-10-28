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
from app.core.routing import ReleasingRoute

router = APIRouter(route_class=ReleasingRoute, prefix="/auth", tags=["auth"])

# The hash of a password nobody knows, checked when the account has none of its
# own, so that every refused sign-in costs the same bcrypt time.
_NOBODY = hash_password(secrets.token_urlsafe(24))


@router.post("/login", response_model=Token)
def login(body: LoginRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    """Opens a session. The token comes back in the body, for the scripts and APIs that put
    it in the `Authorization` header, and in an `HttpOnly` cookie for the browser, which
    so never keeps it where JavaScript reads it."""
    # A brake on repeated attempts, before everything else: whoever is braked gets an
    # answer after one read by key, without reaching bcrypt (see
    # services/login_throttle.py).
    ip = audit.client_ip(request)  # as the server resolved it, never a header taken at its word
    wait = login_throttle.request_wait(session, ip, body.email)
    if wait > 0:
        audit.record_audit(
            session, actor=None, actor_label=body.email, action=audit.LOGIN_FAILED,
            outcome="failure", detail={"reason": "throttled"}, request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many sign-in attempts. Try again in {int(wait) + 1} seconds.",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    user = session.exec(select(User).where(User.email == body.email)).first()
    # the hash the password is checked against: the session will be born only if it is
    # still that one (see sessions.open_session)
    verified = user.hashed_password if user is not None else None
    # an SSO-only user (no local password): signs in from the IdP only; a service
    # account signs in with a token only. bcrypt runs in every case, against a hash
    # nobody knows when there is no password to check: answering an unknown email
    # after one SELECT (~35 ms) and a real one after bcrypt (~300 ms) told which
    # emails have a password account.
    usable = user is not None and not user.is_service and bool(verified)
    right_one = verify_password(body.password, verified if usable else _NOBODY)
    if not (usable and right_one):
        # failed login: who tried and from where is recorded (the email tried)
        audit.record_audit(
            session, actor=user, actor_label=body.email, action=audit.LOGIN_FAILED,
            outcome="failure", detail={"reason": "wrong credentials"}, request=request,
        )
        login_throttle.record_failure(session, ip, body.email)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
    if not user.is_active:
        audit.record_audit(
            session, actor=user, action=audit.LOGIN_FAILED, outcome="failure",
            detail={"reason": "deactivated"}, request=request,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This user has been deactivated")
    login_throttle.record_success(session, ip, body.email)
    sessions.close_the_cookies_session(session, request)
    try:
        row, token = sessions.open_session(session, user, request, "password", verified_password=verified)
    except sessions.SignInSuperseded:
        # the password changed (or the account was disabled) while checking
        audit.record_audit(
            session, actor=user, actor_label=body.email, action=audit.LOGIN_FAILED,
            outcome="failure", detail={"reason": "password changed while signing in"}, request=request,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
    audit.record_audit(session, actor=user, action=audit.LOGIN, request=request)
    sessions.write_cookie(response, request, token, row)
    # The interface (which sends the anti-CSRF header) does not need the token in the body,
    # and the page could read it: the HttpOnly cookie is enough for it.
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
    row, token = sessions.open_session(session, user, request, "password", verified_password=user.hashed_password)
    audit.record_audit(session, actor=user, action=audit.LOGIN, request=request)
    sessions.write_cookie(response, request, token, row)
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
    _row, token = sessions.open_session(session, admin, request, "bootstrap")
    audit.record_audit(session, actor=admin, action=audit.BOOTSTRAP_LOGIN, request=request)
    return Token(access_token=token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Closes the session the request comes with: from here its token no longer holds,
    wherever it was copied."""
    sid = sessions.current_sid(request)
    if sid:
        sessions.close(session, sid, user.id)
    audit.record_audit(session, actor=user, action=audit.LOGOUT, request=request)
    answer = Response(status_code=status.HTTP_204_NO_CONTENT)
    sessions.delete_cookie(answer, request)
    return answer


class SessionOut(BaseModel):
    """An open session, as its owner sees it."""
    id: str
    created_at: UtcDateTime
    last_seen_at: UtcDateTime
    expires_at: UtcDateTime
    ip: Optional[str]
    user_agent: Optional[str]
    method: str
    current: bool  # the one the question comes from


def _session_out(s: UserSession, current: Optional[str]) -> SessionOut:
    return SessionOut(
        id=s.id, created_at=s.created_at, last_seen_at=s.last_seen_at, expires_at=s.expires_at,
        ip=s.ip, user_agent=s.user_agent, method=s.method, current=s.id == current,
    )


# how many sessions are listed at most (a script signing in at every round opens one at
# a time: they hold 12 hours)
MAX_LISTING = 200


def _shared(session: Session, user: User) -> bool:
    """An OBSERVER and nothing else. It is the role of a public demo's guest, an account many
    sign in with: the others' sessions are not theirs to see nor to close, and closing
    them would throw out all the visitors."""
    from app.services.permissions import is_observer_only

    return is_observer_only(session, user)


@router.get("/sessions", response_model=list[SessionOut])
def my_sessions(request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Where one is connected now: a browser, another computer, a script. The observer only
    their own (see `_shared`)."""
    current = sessions.current_sid(request)
    if _shared(session, user):
        own = session.get(UserSession, current) if current else None
        return [_session_out(own, current)] if own is not None else []
    return [_session_out(s, current) for s in sessions.open_ones(session, user.id, limit=MAX_LISTING)]


@router.delete("/sessions/{sid}", status_code=status.HTTP_204_NO_CONTENT)
def close_my_session(
    sid: str, request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    """Closes one of one's own sessions. The others' are not seen: 404. The observer closes
    only the one they ask from, that is they sign out."""
    if sid != sessions.current_sid(request) and _shared(session, user):
        raise HTTPException(status_code=403, detail="Observers can only close their own session")
    if not sessions.close(session, sid, user.id):
        raise HTTPException(status_code=404, detail="Session not found")
    audit.record_audit(
        session, actor=user, action=audit.SESSION_REVOKE, request=request,
        target_type="user", target_id=user.id, target_label=user.email,
        detail={"sessions": 1, "current": sid == sessions.current_sid(request)},
    )
    answer = Response(status_code=status.HTTP_204_NO_CONTENT)
    if sid == sessions.current_sid(request):
        sessions.delete_cookie(answer, request)
    return answer


@router.post("/sessions/close-others", status_code=status.HTTP_204_NO_CONTENT)
def close_other_sessions(request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """«Sign out of the other devices»: only this one stays open. Not for the observer (see
    `_shared`)."""
    if _shared(session, user):
        raise HTTPException(status_code=403, detail="Observers can only close their own session")
    n = sessions.close_all(session, user.id, except_=sessions.current_sid(request))
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
        # EFFECTIVE: personal flag or administrators' group. It is what the frontend decides
        # whether to show the administration with.
        is_superuser=is_admin(session, user),
        is_observer=is_observer(session, user),
        # an integration signed in with its token (said `false` before: the field was
        # inherited from UserOut and never filled)
        is_service=user.is_service,
        groups=list(names),
        # no local password: the settings menu says the identity provider manages it
        sso_only=user.hashed_password is None,
        # the language the emails to this user speak (PUT /users/me/locale)
        locale=user.locale,
        # the page signs out by itself after these minutes without anyone touching it
        session_idle_minutes=get_settings().auth.session_idle_minutes,
        # no personal tokens for the observer (see routes/api_tokens.py)
        personal_tokens=get_settings().auth.personal_tokens and not is_observer_only(session, user),
        api_token_max_days=get_settings().auth.api_token_max_days,
    )


@router.get("/observer", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_observer)], include_in_schema=False)
def can_observe() -> None:
    """«May they see the monitoring?» — yes (204) for administrators and observers, 401/403
    for the others. The reverse proxy calls it (`forward_auth`) before letting a request
    through to Grafana, which is anonymous by itself because the iframe cannot share the
    app's session. No body, no query beyond the guard's: it is called at every request."""
    return None
