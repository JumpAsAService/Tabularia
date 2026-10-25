"""Periodic deletion of the action log, when configured.

The log is append-only by choice: no route deletes it, and deleting an account does
not delete what it did. But «forever» is not a retention policy, it is the absence of
one — and on an installation open to strangers it means keeping other people's data
with no reason that expires.

Here is the only deletion allowed: a declared window, the same for everyone, applied
by the clock and not by someone. Off by default (0 = forever): turning on the deletion
of a security log must be the installer's decision, never a side effect of an update.

It does NOT touch `ai_spend`: there is nobody's text there, and the daily spending cap
rests on those rows — deleting them would reset it.

The same loop empties the client addresses past AUDIT__IP_RETENTION_DAYS (on by
default, 90 days: services/ip_retention.py). That one does not delete a row.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlmodel import Session

from app.core.config import Settings, get_settings
from app.db.session import engine
from app.services import ip_retention

logger = logging.getLogger(__name__)

# It looks once a minute: the window is in minutes, so a row lives at most a minute
# longer than it should. More often would serve nothing.
TICK_SECONDS = 60
# how many rows per round: a first pass on a months-old log would otherwise delete
# everything in a single transaction, holding the lock while the application works
BATCH = 5_000
MAX_BATCHES_PER_ROUND = 20


def purge(session: Session, minutes: int, at: datetime | None = None) -> int:
    """Deletes the items older than the window. Returns how many it removed."""
    if minutes <= 0:
        return 0
    now = at or datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(minutes=minutes)
    removed_ones = 0
    for _ in range(MAX_BATCHES_PER_ROUND):
        # `IN (SELECT ... LIMIT)` because DELETE does not take a LIMIT of its own
        res = session.exec(
            text("DELETE FROM audit_logs WHERE id IN "
                 "(SELECT id FROM audit_logs WHERE ts < :cutoff LIMIT :batch)")
            .bindparams(cutoff=cutoff, batch=BATCH)
        )
        session.commit()
        n = res.rowcount or 0
        removed_ones += n
        if n < BATCH:
            break
    return removed_ones


# client addresses are kept in days: looking once an hour is plenty
IP_EVERY_SECONDS = 3600


def to_do(settings: Settings) -> bool:
    """Whether the loop has any work: the whole-row window or the address window."""
    return settings.audit.retention_minutes > 0 or settings.audit.ip_retention_days > 0


def one_round(session: Session, settings: Settings, at: datetime | None = None, addresses: bool = True) -> None:
    """One round: the rows past AUDIT__RETENTION_MINUTES, then (when `addresses`)
    the client addresses past AUDIT__IP_RETENTION_DAYS (see services/ip_retention.py)."""
    minutes = settings.audit.retention_minutes
    removed_ones = purge(session, minutes, at=at)
    if removed_ones:
        logger.info("audit: deleted %s items past the %s-minute window", removed_ones, minutes)
    if addresses and settings.audit.ip_retention_days > 0:
        emptied = ip_retention.delete_addresses(session, settings.audit.ip_retention_days, at=at)
        if emptied:
            logger.info("audit: client address and browser emptied on %s rows older than %s days",
                        emptied, settings.audit.ip_retention_days)


def _round_with_session(settings: Settings, addresses: bool) -> None:
    with Session(engine) as session:
        one_round(session, settings, None, addresses)


async def retention_loop(stop: asyncio.Event) -> None:
    settings = get_settings()
    if not to_do(settings):
        logger.info("audit: no expiry configured, the log is kept whole")
        return
    if settings.audit.retention_minutes > 0:
        logger.info("audit: the items older than %s minutes will be deleted (checked every %ss)",
                    settings.audit.retention_minutes, TICK_SECONDS)
    if settings.audit.ip_retention_days > 0:
        logger.info("audit: client addresses older than %s days will be emptied (checked every %ss)",
                    settings.audit.ip_retention_days, IP_EVERY_SECONDS)
    last_addresses = 0.0
    while not stop.is_set():
        now = time.monotonic()
        addresses = not last_addresses or now - last_addresses >= IP_EVERY_SECONDS
        try:
            await asyncio.to_thread(_round_with_session, settings, addresses)
            if addresses:
                last_addresses = now
        except Exception:  # noqa: BLE001 — the cleanup must never stop the gateway
            logger.exception("audit: cleanup failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("audit: cleanup stopped")
