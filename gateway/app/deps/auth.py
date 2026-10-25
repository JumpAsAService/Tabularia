"""The authentication dependency: takes the token (header or cookie), validates the
JWT, loads the user and their session with one read."""
import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import exists
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.security import decode_access_token
from app.db.session import read_engine, get_session
from app.models import ApiToken, Group, User, UserGroupLink, UserSession
from app.services import api_tokens, audit, sessions

logger = logging.getLogger(__name__)

# last_seen throttling: it updates at most once every N seconds per session, so there
# is no DB write at every authenticated request.
_LAST_SEEN_THROTTLE = sessions.ACTIVITY_EVERY

# methods that change nothing: with the cookie alone they do not ask for the anti-CSRF header
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_credentials_error = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


class _RequestToken(OAuth2PasswordBearer):
    """The token arrives in two ways: in the `Authorization` header (scripts, API, the
    reverse proxy asking whether to let through to Grafana) or in the `HttpOnly` cookie
    the gateway writes to the browser at sign-in. The header, when there, wins.

    With the cookie alone, a request that changes something must carry
    `X-Tabularia-Csrf` too: the browser attaches the cookie by itself even to a request
    started from another site, the header it does not (see services/sessions.py)."""

    async def __call__(self, request: Request) -> str:  # type: ignore[override]
        from_header = await super().__call__(request)
        if from_header:
            return from_header
        from_cookie = request.cookies.get(sessions.COOKIE)
        if not from_cookie:
            raise _credentials_error
        if request.method not in _SAFE_METHODS and not request.headers.get(sessions.CSRF_HEADER):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The request is missing the X-Tabularia-Csrf header",
            )
        request.state.token_from_cookie = True  # if it does not hold, the 401 deletes the cookie
        return from_cookie


# tokenUrl informative only (Swagger): the real login is POST /auth/login (JSON).
oauth2_scheme = _RequestToken(tokenUrl="auth/login", auto_error=False)


def _refused(request: Request) -> HTTPException:
    """The 401. When the refused token was the cookie's, the answer deletes it: a cookie no
    longer valid would make the interface's server believe there is still a session, at
    every page loaded from scratch. When it came from the header, no: the browser's
    cookie may be good (a script's expired token tried from /docs must not sign out of
    the interface)."""
    state = getattr(request, "state", None)
    if not getattr(state, "token_from_cookie", False):
        return _credentials_error
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_credentials_error.detail,
        headers={"WWW-Authenticate": "Bearer", "Set-Cookie": sessions.deleting_header(request)},
    )


def _old(t: datetime | None, now: datetime) -> bool:
    if t is None:
        return True
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return now - t >= _LAST_SEEN_THROTTLE


def _touch_last_seen(session: Session, user: User, open_: UserSession, request: Request) -> None:
    """Updates the last activity (throttled) of the session and the user: the first decides
    the idle timeout, the second the «seen» column of the Admin page.

    The address sits only in the SESSION, and goes with it. `users.last_seen_ip` is no
    longer written: nobody read it, and nobody would ever have deleted it. Of an
    OBSERVER the address is not kept even there: only the moment is updated, which is
    what is needed to say «someone is connected now». See
    `permissions.is_observer_only` for why."""
    from app.services.permissions import is_observer_only

    now = datetime.now(timezone.utc)
    if not (_old(user.last_seen_at, now) or _old(open_.last_seen_at, now)):
        return
    try:
        if is_observer_only(session, user):
            # also delete what was there: the role may have been given later, and an address
            # collected earlier would stay there until expiry
            open_.ip = None
            open_.user_agent = None
        else:
            open_.ip = audit.client_ip(request)
        user.last_seen_at = now
        open_.last_seen_at = now.replace(tzinfo=None)
        session.add(user)
        session.add(open_)
        session.commit()
    except Exception:  # it must never break the request
        logger.debug("last_seen not updated for user %s", user.id, exc_info=True)
        session.rollback()


def _end_of_step(session: Session) -> None:
    """Closes the read transaction before leaving a dependency.

    Every synchronous dependency runs in a thread of its own, and the route in another.
    If this one left with the transaction open, the request would keep a pool
    connection while waiting for a thread for the next step — and under a burst those
    threads are all busy with requests waiting for a connection. Measured: 60 requests
    together → gateway stuck 30 seconds and more than half in error.

    `rollback` and not `commit`: here there was only reading, and a commit could make
    permanent something a caller had put on the object. The objects stay attached to
    the session: whoever reads them in the next step reloads them, with a connection
    taken and handed back there.
    """
    session.rollback()


def _one_read(session: Session, q):
    """Runs `q` (user + one row of theirs) with a connection taken and handed back here,
    outside any transaction.

    It is the read EVERY request makes. Inside the request's session it cost four round
    trips to Postgres (ping, BEGIN, the SELECT, the ROLLBACK at the end of the step) and
    the rollback expired the user, whom the route read again a moment later. This way it
    is one round trip: a SELECT by key in autocommit sees exactly what it would see in a
    transaction just opened (see `read_engine`). The objects then pass to the
    request's session loaded already (`merge(load=False)` does not touch the database),
    and the route finds the user there as it always did."""
    bind = session.get_bind()
    if not isinstance(bind, Engine):  # a session bound to a connection open already
        return session.exec(q).first()
    with Session(read_engine(bind)) as reading:
        row = reading.exec(q).first()
        if row is None:
            return None
        user, other, *values = row  # values: plain columns computed in the same SELECT
        reading.expunge(user)
        reading.expunge(other)
    return (session.merge(user, load=False), session.merge(other, load=False), *values)


def _load_user(session: Session, user_id: int, sid: str) -> tuple[User, UserSession] | None:
    """The user AND their session, in one SELECT with the session joined."""
    return _one_read(session, (
        select(User, UserSession)
        .join(UserSession, UserSession.user_id == User.id)
        # expired or still for too long = as if it were not there (see sessions.valida)
        .where(User.id == user_id, UserSession.id == sid, sessions.validate())
    ))


def _token_user(request: Request, token: str, session: Session) -> User:
    """An API token (`tab_…`): the user and the token in one SELECT, by fingerprint.
    Only from the `Authorization` header: an API token put in the session's cookie does
    not hold (the cookie is the browser's, the token the scripts')."""
    state = getattr(request, "state", None)
    if getattr(state, "token_from_cookie", False):
        raise _refused(request)
    def in_a_group(flag):
        return exists().where(UserGroupLink.user_id == User.id, UserGroupLink.group_id == Group.id, flag == True)  # noqa: E712

    found = _one_read(session, (
        # the roles from the groups in the same SELECT: needed to know whether they are ONLY an observer
        select(User, ApiToken, in_a_group(Group.is_admin).label("admin_g"), in_a_group(Group.is_observer).label("observer_g"))
        .join(ApiToken, ApiToken.user_id == User.id)
        .where(ApiToken.token_hash == api_tokens.fingerprint(token), ApiToken.expires_at > api_tokens.now())
    ))
    if found is None:  # never existed, revoked or expired
        raise _refused(request)
    user, row, admin_g, observer_g = found
    if not user.is_active:
        raise _refused(request)
    from app.core.config import get_settings

    if not user.is_service and not get_settings().auth.personal_tokens:
        raise _refused(request)  # personal tokens turned off: the ones given already too
    if not user.is_service and (user.is_observer or observer_g) and not (user.is_superuser or admin_g):
        # observer only: no personal tokens, whatever way they got there (flag, groups, an
        # administrator role lost, SSO). When they become one, their tokens are withdrawn;
        # this covers the ways that do not
        raise _refused(request)
    api_tokens.check_level(row, request)
    if state is not None:
        state.api_token = row.name  # per l'audit: «fatto con il token …»
    _touch_token(session, user, row, request)
    return user


def _touch_token(session: Session, user: User, row: ApiToken, request: Request) -> None:
    """The token's last use (throttled), and the user's activity as for the sessions. Of an
    observer the address is not kept."""
    from app.services.permissions import is_observer_only

    now = datetime.now(timezone.utc)
    if not (_old(row.last_used_at, now) or _old(user.last_seen_at, now)):
        return
    try:
        if is_observer_only(session, user):
            row.last_used_ip = None
        else:
            row.last_used_ip = (audit.client_ip(request) or "")[:64] or None
        row.last_used_at = now.replace(tzinfo=None)
        user.last_seen_at = now
        session.add(row)
        session.add(user)
        session.commit()
    except Exception:  # it must never break the request
        logger.debug("token's last use not updated (user %s)", user.id, exc_info=True)
        session.rollback()


def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> User:
    if api_tokens.is_a_token(token):
        return _token_user(request, token, session)
    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
        # a token without a session is from before the sessions: it cannot be revoked, so it
        # does not hold (after the update one signs in once)
        sid = str(payload["sid"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise _refused(request)
    found = _load_user(session, user_id, sid)
    if found is None:  # user gone, session closed, expired or still
        raise _refused(request)
    user, open_ = found
    if not user.is_active:
        raise _refused(request)
    state = getattr(request, "state", None)
    if state is not None:
        state.sid = sid  # for whoever closes the current session (see sessions.sid_corrente)
    # No `_end_of_step` here: the request's session took no connections (the read used one
    # of its own, handed back already) and `_touch_last_seen`, the once a minute it
    # writes, closes by itself with commit or rollback. One more rollback would only expire
    # the user just loaded.
    _touch_last_seen(session, user, open_, request)
    return user


def require_superuser(
    user: User = Depends(get_current_user), session: Session = Depends(get_session)
) -> User:
    # EFFECTIVE admin: personal flag or administrators' group
    from app.services.permissions import is_admin

    if not is_admin(session, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    _end_of_step(session)
    return user


def require_observer(
    user: User = Depends(get_current_user), session: Session = Depends(get_session)
) -> User:
    """To READ an administration panel: administrator or observer.

    The writing routes stay on `require_superuser`. The split is by METHOD and not by
    panel: a «read-only» panel holding a writing route would be a silent hole, while
    this way every POST, PUT and DELETE stays closed by construction."""
    from app.services.permissions import is_observer

    if not is_observer(session, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrative read access required")
    _end_of_step(session)
    return user
