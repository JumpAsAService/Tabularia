"""Scheduler in-process del refresh delle datasource database.

Un timer nel gateway (deroga controllata al "niente poller": quello valeva per
lo STATO dei run, riconciliato pigramente; una schedulazione ha bisogno di un
timer). A ogni tick lancia i refresh delle datasource il cui `next_refresh_at`
è scaduto, con l'autorità di `refresh_scheduled_by` (RUN+CONNECT catturati
quando lo schedule è stato impostato), poi calcola il prossimo slot.

Single-instance: con più repliche del gateway servirebbe un lock (advisory lock
Postgres) per non lanciare due volte — documentato, non necessario ora.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.db.session import engine
from app.models import Connection, Datasource, Run, User
from app.models.run import TERMINAL_STATES
from app.routes.runs import _reconcile, launch_ingest_run
from app.services.schedule import next_fire

logger = logging.getLogger(__name__)

TICK_SECONDS = 60


def _advance(session: Session, ds: Datasource, now: datetime) -> None:
    """Programma il prossimo slot (o disabilita se il cron è diventato invalido)."""
    try:
        ds.next_refresh_at = next_fire(ds.refresh_schedule, now).replace(tzinfo=None)
    except Exception:
        ds.refresh_schedule = None
        ds.next_refresh_at = None
    session.add(ds)
    session.commit()


def _disable(session: Session, ds: Datasource, reason: str) -> None:
    logger.warning("scheduler: datasource %s disabilitata (%s)", ds.id, reason)
    ds.refresh_schedule = None
    ds.refresh_scheduled_by = None
    ds.next_refresh_at = None
    session.add(ds)
    session.commit()


async def _fire_one(session: Session, ds: Datasource, now: datetime) -> None:
    # autorità del refresh schedulato: l'utente che ha impostato lo schedule
    user = session.get(User, ds.refresh_scheduled_by) if ds.refresh_scheduled_by else None
    if user is None or not user.is_active:
        _disable(session, ds, "autore dello schedule assente o disattivato")
        return
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        _disable(session, ds, "connessione inesistente")
        return

    # un refresh alla volta: se ce n'è uno in corso, salta questo slot
    last = session.exec(
        select(Run)
        .where(Run.datasource_id == ds.id, Run.kind == "ingest")
        .order_by(Run.started_at.desc())
    ).first()
    if last is not None:
        last = await _reconcile(session, last)
        if last.status not in TERMINAL_STATES:
            logger.info("scheduler: datasource %s ha già un refresh in corso, salto lo slot", ds.id)
            _advance(session, ds, now)
            return

    await launch_ingest_run(session, user, ds, conn)
    logger.info("scheduler: refresh schedulato lanciato per datasource %s (%s)", ds.id, ds.name)
    _advance(session, ds, now)


async def _tick() -> None:
    now = datetime.now(timezone.utc)
    now_naive = now.replace(tzinfo=None)  # le colonne TIMESTAMP sono naive-UTC
    with Session(engine) as session:
        due = session.exec(
            select(Datasource).where(
                Datasource.refresh_schedule.is_not(None),  # type: ignore[union-attr]
                Datasource.next_refresh_at.is_not(None),  # type: ignore[union-attr]
                Datasource.next_refresh_at <= now_naive,
            )
        ).all()
        for ds in due:
            try:
                await _fire_one(session, ds, now)
            except Exception:
                logger.exception("scheduler: refresh datasource %s fallito", ds.id)
                _advance(session, ds, now)  # avanza per non ripetere a ogni tick


async def scheduler_loop(stop: asyncio.Event) -> None:
    logger.info("scheduler del refresh avviato (tick %ss)", TICK_SECONDS)
    while not stop.is_set():
        try:
            await _tick()
        except Exception:
            logger.exception("scheduler: errore nel tick")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("scheduler del refresh fermato")
