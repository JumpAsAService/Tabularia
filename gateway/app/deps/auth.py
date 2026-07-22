"""Dependency di autenticazione: estrae il Bearer token, valida il JWT, carica l'utente."""
import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session

from app.core.security import decode_access_token
from app.db.session import get_session
from app.models import User

logger = logging.getLogger(__name__)

# tokenUrl solo informativo (Swagger): il login vero è POST /auth/login (JSON).
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=True)

# throttling del last_seen: si aggiorna al massimo una volta ogni N secondi per
# utente, così non c'è una scrittura DB a ogni richiesta autenticata.
_LAST_SEEN_THROTTLE = timedelta(seconds=60)

_credentials_error = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Credenziali non valide",
    headers={"WWW-Authenticate": "Bearer"},
)


def _touch_last_seen(session: Session, user: User, request: Request) -> None:
    """Aggiorna last_seen (throttled) per la vista 'sessioni attive' dell'audit."""
    now = datetime.now(timezone.utc)
    seen = user.last_seen_at
    if seen is not None and seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    if seen is not None and now - seen < _LAST_SEEN_THROTTLE:
        return
    try:
        fwd = request.headers.get("x-forwarded-for")
        user.last_seen_ip = (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None))
        user.last_seen_at = now
        session.add(user)
        session.commit()
    except Exception:  # non deve mai rompere la richiesta
        logger.debug("last_seen non aggiornato per utente %s", user.id, exc_info=True)
        session.rollback()


def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> User:
    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise _credentials_error
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise _credentials_error
    _touch_last_seen(session, user, request)
    return user


def require_superuser(user: User = Depends(get_current_user)) -> User:
    if not user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Richiesti privilegi admin")
    return user
