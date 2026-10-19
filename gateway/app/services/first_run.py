"""The first start of the try-it Compose stack, with nothing configured.

With AUTH__FIRST_RUN_SETUP and no AUTH__ADMIN_PASSWORD, the gateway starts without an
administrator; while no account exists, the sign-in page asks for one (POST
/auth/setup). It is off with APP__ENV_NAME=production, and it closes for good the
moment any account exists.

The sample-data container cannot know that administrator's password. With a
SECURITY__KEYS_DIR it reads a start-up token the gateway leaves there
(bootstrap.token) and exchanges it, once an administrator exists, for a session as
that administrator (POST /auth/bootstrap). Whoever can read that directory also has
the Fernet key, so the token opens nothing they could not reach already.
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Optional

from sqlalchemy import func, text
from sqlmodel import Session, select

from app.core.config import Settings, get_settings
from app.models import User

logger = logging.getLogger(__name__)

TOKEN_FILE = "bootstrap.token"
_SETUP_LOCK = 742_011  # pg_advisory_xact_lock: two first administrators cannot be created at once


def enabled(s: Optional[Settings] = None) -> bool:
    s = s or get_settings()
    return s.auth.first_run_setup and not s.is_production()


def needed(session: Session) -> bool:
    """The sign-in page asks for the first administrator: enabled, and no account yet."""
    return enabled() and not _any_account(session)


def _any_account(session: Session) -> bool:
    return bool(session.exec(select(func.count()).select_from(User)).one())


def lock(session: Session) -> None:
    """Holds the setup until the end of the transaction (Postgres; SQLite has one writer)."""
    if session.get_bind().dialect.name == "postgresql":
        session.exec(text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_SETUP_LOCK))


def accounts_exist(session: Session) -> bool:
    return _any_account(session)


def ensure_bootstrap_token() -> None:
    """At the gateway's start: the token the sample-data container exchanges for a session."""
    s = get_settings()
    if not (enabled(s) and s.security.keys_dir):
        return
    from app.core.keys import read_or_create

    read_or_create(s.security.keys_dir, TOKEN_FILE, lambda: secrets.token_urlsafe(32))


def bootstrap_token() -> Optional[str]:
    """The start-up token, if this installation has one (never created here)."""
    s = get_settings()
    if not (enabled(s) and s.security.keys_dir):
        return None
    try:
        return (Path(s.security.keys_dir) / TOKEN_FILE).read_text().strip() or None
    except OSError:
        return None


def first_administrator(session: Session) -> Optional[User]:
    """The oldest active administrator with a sign-in of its own (never a service account)."""
    return session.exec(
        select(User).where(User.is_superuser == True, User.is_active == True, User.is_service == False)  # noqa: E712
        .order_by(User.id)
    ).first()
