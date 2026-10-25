"""How long client addresses are kept, and forgetting them with the account.

An IP address and a browser string identify a person, and the audit log kept them for
ever: every row of every user, and deleting an account left them there. The log
itself stays append-only (who did what, and when, is evidence an organisation needs);
what goes is the part that points at the person's network and device:

- `delete_addresses`: past AUDIT__IP_RETENTION_DAYS (90 by default; 0 keeps them),
  the address and the browser of audit rows, the last address of API tokens and of
  sessions, and the old `users.last_seen_ip` that no release writes any more. Run by
  `audit_retention.retention_loop`.
- `forget_user`: when an account is deleted, its rows lose them at once, failed
  sign-ins with its email included (they have no actor, only the email as typed).

The whole-row retention (AUDIT__RETENTION_MINUTES) is a separate setting: when it is
shorter, rows go before their addresses would.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlmodel import Session

from app.models import ApiToken, AuditLog, LoginAttempt, User, UserSession

logger = logging.getLogger(__name__)

# rows per transaction: a first pass over a log of several years must not hold
# one long transaction while the application works
BATCH = 5_000
MAX_BATCHES = 200


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC columns


def delete_addresses(session: Session, days: int, at: datetime | None = None) -> int:
    """Empties the addresses older than `days` days. Returns how many audit rows
    lost theirs (tokens, sessions and users are few and not counted)."""
    if days <= 0:
        return 0
    cutoff = (at or _now()) - timedelta(days=days)
    with_address = or_(AuditLog.ip.is_not(None), AuditLog.user_agent.is_not(None))
    removed_ones = 0
    for _ in range(MAX_BATCHES):
        ids = select(AuditLog.id).where(AuditLog.ts < cutoff, with_address).limit(BATCH).scalar_subquery()
        n = session.exec(
            update(AuditLog).where(AuditLog.id.in_(ids)).values(ip=None, user_agent=None)
            .execution_options(synchronize_session=False)
        ).rowcount or 0
        session.commit()
        removed_ones += n
        if n < BATCH:
            break
    session.exec(
        update(ApiToken).where(ApiToken.last_used_at < cutoff, ApiToken.last_used_ip.is_not(None))
        .values(last_used_ip=None).execution_options(synchronize_session=False)
    )
    session.exec(
        update(UserSession)
        .where(UserSession.last_seen_at < cutoff,
               or_(UserSession.ip.is_not(None), UserSession.user_agent.is_not(None)))
        .values(ip=None, user_agent=None).execution_options(synchronize_session=False)
    )
    # no release writes it any more (the address lives in the session): whatever
    # is there is old, and has no date of its own to measure it by
    session.exec(
        update(User).where(User.last_seen_ip.is_not(None)).values(last_seen_ip=None)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return removed_ones


def forget_user(session: Session, user: User) -> None:
    """Empties the addresses of an account being deleted, in the caller's transaction:
    the rows it acted in, and the failed sign-ins with its email (no actor there).
    The email of the time stays in `actor_label`: it is what makes the log readable."""
    email = (user.email or "").strip().lower()
    session.exec(
        update(AuditLog)
        .where(or_(AuditLog.actor_id == user.id, func.lower(AuditLog.actor_label) == email))
        .values(ip=None, user_agent=None).execution_options(synchronize_session=False)
    )
    session.exec(delete(LoginAttempt).where(LoginAttempt.email == email))
