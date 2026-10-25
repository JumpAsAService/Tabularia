"""A brake on repeated sign-in attempts.

The only cost of a wrong attempt was bcrypt: 25 attempts in 6.7 seconds, measured
during the audit of 2026-09-19 (M5). On a public address — a demo, a link in a post —
the credentials end up in a comment and a script does the rest.

The count sits in a TABLE (`login_attempts`), not in memory: with several gateway
replicas a dictionary per process would count five errors per replica, and the brake
would loosen exactly when the installation grows. It costs one read by key at every
sign-in, which is rare. For what it must do — make a dictionary useless, not resist a
botnet — it is enough.

It counts by **(IP address, email)** together: by IP alone, an office behind a NAT
would block itself; by email alone, changing account is enough to start again. A
successful sign-in resets, so whoever gets it wrong twice and then signs in carries
nothing along.

A second, looser count is kept per address alone (any email): one address trying
one password on many accounts (spraying), or probing which emails exist, never
reached five errors on any single (address, email) key. Its threshold is high
enough for an office behind one NAT, and a successful sign-in does not clear it —
otherwise one valid account would be enough to keep spraying the others.

The address is the one the server resolved (see `audit.client_ip`): a client
writing its own X-Forwarded-For does not get a fresh key per request.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.models.shared_state import LoginAttempt

logger = logging.getLogger(__name__)

# after how many errors slowing down starts, and for how long
THRESHOLD = 5
WINDOW_SECONDS = 300.0
BLOCK_SECONDS = 60.0
MAX_BLOCK = 900.0
# failures from one address, whatever the email, before that address slows down
ADDRESS_THRESHOLD = 30
# the email of the per-address row: emails are stripped, so no login can produce it
ALL = " *"


def _key(ip: str | None, email: str) -> tuple[str, str]:
    return (ip or "?", (email or "").strip().lower())


def _wait(session: Session, key: tuple[str, str], threshold: int) -> float:
    row = session.get(LoginAttempt, key)
    if row is None:
        return 0.0
    failed, elapsed = row.failures, (_now() - row.last_at).total_seconds()
    if failed < threshold or elapsed > WINDOW_SECONDS:
        return 0.0
    # doubles at every error past the threshold, with a cap
    block = min(BLOCK_SECONDS * (2 ** (failed - threshold)), MAX_BLOCK)
    return max(0.0, block - elapsed)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive columns = UTC


def request_wait(session: Session, ip: str | None, email: str) -> float:
    """Seconds to wait before being able to try again. `0` = may go ahead."""
    key = _key(ip, email)
    try:
        return max(_wait(session, key, THRESHOLD), _wait(session, (key[0], ALL), ADDRESS_THRESHOLD))
    finally:
        session.rollback()  # only a read: the transaction is not kept open


def record_failure(session: Session, ip: str | None, email: str) -> None:
    """One more error, for (address, email) and for the whole address."""
    ip_key, email_key = _key(ip, email)
    _count(session, ip_key, email_key)
    _count(session, ip_key, ALL)


def _count(session: Session, ip_key: str, email_key: str) -> None:
    """The database does the increment (`failures + 1`), so two replicas recording together
    do not lose one."""
    now = _now()
    expired = now - timedelta(seconds=WINDOW_SECONDS)

    def increment() -> int:
        outcome = session.exec(
            update(LoginAttempt)
            .where(LoginAttempt.ip == ip_key, LoginAttempt.email == email_key)
            .values(
                # past the window it starts from scratch
                failures=case((LoginAttempt.last_at < expired, 1), else_=LoginAttempt.failures + 1),
                last_at=now,
            )
        )
        session.commit()
        return outcome.rowcount

    if increment():
        return
    try:
        session.add(LoginAttempt(ip=ip_key, email=email_key, failures=1, last_at=now))
        session.commit()
    except IntegrityError:  # another replica wrote the first error a moment before
        session.rollback()
        increment()


def record_success(session: Session, ip: str | None, email: str) -> None:
    ip_key, email_key = _key(ip, email)
    session.exec(delete(LoginAttempt).where(LoginAttempt.ip == ip_key, LoginAttempt.email == email_key))
    session.commit()


def clean(session: Session) -> int:
    """Removes the counts that no longer brake anyone (the scheduler calls it)."""
    removed_ones = session.exec(
        delete(LoginAttempt).where(LoginAttempt.last_at < _now() - timedelta(seconds=WINDOW_SECONDS))
    )
    session.commit()
    return removed_ones.rowcount or 0
