"""Sessions: opening, recognizing and closing them.

Every successful sign-in (password or SSO) opens a row in `user_sessions` and gets a
token carrying its id. The browser keeps it in an `HttpOnly` cookie, which the page's
JavaScript cannot read: an injected script does not take it away. Scripts and APIs
use it as before, in the `Authorization` header.

Closing a session is deleting its row: the token, still signed and not expired, stops
holding at the next request. Signing out does it, and the revocation (own or the
administrator's), the password change, disabling the account, and the periodic
cleanup of those expired or still for too long.

The cookie goes with `SameSite=Lax` and, when the page is on HTTPS, `Secure`. Against
the requests made by other sites there is the `X-Tabularia-Csrf` header as well: a
request that changes something and comes with the cookie alone must have it (see
deps/auth.py). An HTML form of another site cannot add it, and a fetch from an origin
CORS does not allow does not pass the preflight.
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

# a session's last activity is written at most once a minute: it is also the precision
# of the idle timeout
ACTIVITY_EVERY = timedelta(seconds=60)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SignInSuperseded(Exception):
    """The verified password is no longer the account's (or the account was disabled) while
    the sign-in was going on."""


def open_session(
    session: Session, user: User, request: Optional[Request], method: str,
    verified_password: Optional[str] = None,
) -> tuple[UserSession, str]:
    """Opens a session for `user` and returns the row and its token.

    `verified_password` is the hash the login checked the password against. Between
    that check (bcrypt, a quarter of a second) and this row an administrator may change
    the password and close the sessions: the user's row is read here LOCKED (FOR SHARE)
    and the session is born only if the password is still that one. The lock lines the
    two up: either the change waits for this session to exist (and closes it), or this
    one sees the new password and is not born. Otherwise whoever knew the old one,
    trying over and over, could stay in until expiry."""
    from app.services import audit
    from app.services.permissions import is_observer_only

    now_in_db = session.exec(
        select(User.hashed_password, User.is_active).where(User.id == user.id).with_for_update(read=True)
    ).first()
    if now_in_db is None or not now_in_db[1] or (
        verified_password is not None and now_in_db[0] != verified_password
    ):
        session.rollback()
        raise SignInSuperseded()
    at = now()
    # of an OBSERVER where they come in from and with what is not kept, as in the audit
    minimize = is_observer_only(session, user)
    agent = (request.headers.get("user-agent") or "")[:512] if request is not None else ""
    row = UserSession(
        id=secrets.token_urlsafe(24),
        user_id=user.id,
        created_at=at,
        last_seen_at=at,
        expires_at=at + timedelta(minutes=get_settings().jwt.access_ttl_minutes),
        ip=None if minimize else audit.client_ip(request),
        user_agent=None if minimize else (agent or None),
        method=method,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row, create_access_token(user.id, row.id, row.expires_at)


def validate(at: Optional[datetime] = None):
    """THE rule, in SQL, for whoever checks a token, lists or cleans: within the maximum
    length and, with the idle timeout, used recently. The minute the activity is written
    with is added to the timeout: a session used thirty seconds ago must not look still
    for longer than it is."""
    at = at or now()
    condition = UserSession.expires_at > at
    minutes = get_settings().auth.session_idle_minutes
    if minutes > 0:
        condition = and_(condition, UserSession.last_seen_at >= at - timedelta(minutes=minutes) - ACTIVITY_EVERY)
    return condition


def close(session: Session, sid: str, user_id: Optional[int] = None) -> bool:
    """Closes a session (of `user_id`, when given). Returns whether it was there."""
    q = delete(UserSession).where(UserSession.id == sid)
    if user_id is not None:
        q = q.where(UserSession.user_id == user_id)
    n = session.exec(q).rowcount or 0
    session.commit()
    return n > 0


def close_all(session: Session, user_id: int, except_: Optional[str] = None, commit: bool = True) -> int:
    """Closes all of a user's sessions, except possibly one. Returns how many.
    `commit=False` leaves it in the caller's transaction: a new password and closing the
    sessions go together, or neither."""
    q = delete(UserSession).where(UserSession.user_id == user_id)
    if except_:
        q = q.where(UserSession.id != except_)
    n = session.exec(q).rowcount or 0
    if commit:
        session.commit()
    return n


def open_ones(session: Session, user_id: Optional[int] = None, limit: Optional[int] = None) -> list[UserSession]:
    """The sessions still valid (of a user, or of everyone), newest first.
    `limit`: on a public demo every guest sign-in is a row."""
    q = select(UserSession).where(validate()).order_by(UserSession.last_seen_at.desc())
    if user_id is not None:
        q = q.where(UserSession.user_id == user_id)
    if limit is not None:
        q = q.limit(limit)
    return list(session.exec(q).all())


def how_many_open(session: Session) -> int:
    return session.exec(select(func.count()).select_from(UserSession).where(validate())).one()


def clean(session: Session) -> int:
    """Removes the sessions expired or still for too long (the scheduler calls it): their
    addresses go with them."""
    n = session.exec(delete(UserSession).where(not_(validate()))).rowcount or 0
    session.commit()
    return n


def close_the_cookies_session(session: Session, request: Request) -> None:
    """A new sign-in from the same browser takes the place of the cookie of before: the
    session it belonged to closes, instead of staying until expiry like an «other device»
    that is no longer there."""
    old = (getattr(request, "cookies", None) or {}).get(COOKIE)
    if not old:
        return
    from app.core.security import decode_access_token

    try:
        sid = str(decode_access_token(old)["sid"])
    except Exception:  # expired, tampered with, from before the sessions: nothing to close
        return
    close(session, sid)


def handle(sid: str) -> str:
    """A stable name for a session, to show to whoever cannot close it (the observer): it
    tells the rows apart without handing over the real id."""
    return "h-" + hashlib.sha256(sid.encode()).hexdigest()[:16]


def current_sid(request: Request) -> Optional[str]:
    """The id of the session the request came with (`get_current_user` puts it there)."""
    state = getattr(request, "state", None)
    return getattr(state, "sid", None) if state is not None else None


# ── the cookie ──────────────────────────────────────────────────────────────

def _in_https(request: Request) -> bool:
    """Behind a proxy that terminates TLS the gateway receives http: what the proxy declares
    counts. Whoever forged the header would remove `Secure` only from their own cookie."""
    if getattr(getattr(request, "url", None), "scheme", "") == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"


def write_cookie(response: Response, request: Request, token: str, row: UserSession) -> None:
    """Path `/`: the interface's server must see it too, which decides whether to send to
    the sign-in page (for the same reason, with the interface on another host,
    AUTH__COOKIE_DOMAIN). It lasts as long as the session."""
    seconds = max(0, int((row.expires_at - now()).total_seconds()))
    response.set_cookie(
        COOKIE, token, max_age=seconds, path="/", domain=get_settings().auth.cookie_domain or None,
        httponly=True, samesite="lax", secure=_in_https(request),
    )


def delete_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        COOKIE, path="/", domain=get_settings().auth.cookie_domain or None,
        httponly=True, samesite="lax", secure=_in_https(request),
    )


def deleting_header(request: Request) -> str:
    """The same `Set-Cookie` as `delete_cookie`, to put in an exception."""
    r = Response()
    delete_cookie(r, request)
    return r.headers["set-cookie"]
