"""Scheduler in-process del gateway: refresh delle datasource database E
esecuzione dei flussi.

A ogni tick lancia il lavoro scaduto con l'autorità di chi ha impostato lo
schedule (capability catturate alla creazione). Per i FLUSSI la definizione
viene RI-RISOLTA al fire-time (Opzione A): usa sempre gli snapshot correnti
delle datasource e segue le modifiche al flusso.

Il tick riconcilia anche lo STATO dei run non terminali (oltre alla
riconciliazione pigra on-poll del frontend): così un ingest/flusso completato
lato engine viene applicato entro un tick anche se nessuno sta guardando —
altrimenti un refresh finito resterebbe invisibile e la datasource punterebbe a
dati vecchi.

Single-instance: con più repliche del gateway servirebbe un lock (advisory lock
Postgres) per non lanciare due volte — documentato, non necessario ora.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.db.session import engine
from app.models import Connection, Datasource, Flow, Run, User
from app.models.run import TERMINAL_STATES
from app.routes.runs import _reconcile, launch_ingest_run
from app.services.blobgc import sweep_blob_deletions
from app.services.orchestrator import orchestrate_bg
from app.models.permission import Capability
from app.services import permissions as perm_service
from app.services.schedule import next_fire

logger = logging.getLogger(__name__)

TICK_SECONDS = 60


# ── datasource: refresh schedulato ──────────────────────────────────────────
def _advance_ds(session: Session, ds: Datasource, now: datetime) -> None:
    try:
        ds.next_refresh_at = next_fire(ds.refresh_schedule, now).replace(tzinfo=None)
    except Exception:
        ds.refresh_schedule = None
        ds.next_refresh_at = None
    session.add(ds)
    session.commit()


def _disable_ds(session: Session, ds: Datasource, reason: str) -> None:
    logger.warning("scheduler: refresh datasource %s disabilitato (%s)", ds.id, reason)
    ds.refresh_schedule = None
    ds.refresh_scheduled_by = None
    ds.next_refresh_at = None
    session.add(ds)
    session.commit()


async def _fire_ds(session: Session, ds: Datasource, now: datetime) -> None:
    user = session.get(User, ds.refresh_scheduled_by) if ds.refresh_scheduled_by else None
    if user is None or not user.is_active:
        _disable_ds(session, ds, "autore dello schedule assente o disattivato")
        return
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        _disable_ds(session, ds, "connessione inesistente")
        return
    # I permessi si riverificano AL FUOCO, non solo quando lo schedule viene
    # creato: altrimenti togliere CONNECT a qualcuno non ferma il suo cron, e
    # chi ha EDIT può spostarsi in casa una datasource schedulata da altri e
    # farsi rifornire di dati con l'autorità della vittima (audit 2026-09-19,
    # A7). È ciò che l'orchestratore fa già in `_refresh_and_wait`.
    if not perm_service.has_capability(session, user, ds.project_id, Capability.RUN):
        _disable_ds(session, ds, f"RUN mancante per {user.email} sulla cartella della datasource")
        return
    if not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        _disable_ds(session, ds, f"CONNECT mancante per {user.email} sulla cartella della connessione")
        return
    last = session.exec(
        select(Run)
        .where(Run.datasource_id == ds.id, Run.kind == "ingest")
        .order_by(Run.started_at.desc())
    ).first()
    if last is not None:
        last = await _reconcile(session, last)
        if last.status not in TERMINAL_STATES:
            logger.info("scheduler: datasource %s ha già un refresh in corso, salto lo slot", ds.id)
            _advance_ds(session, ds, now)
            return
    await launch_ingest_run(session, user, ds, conn, trigger_type="schedule")
    logger.info("scheduler: refresh schedulato lanciato per datasource %s (%s)", ds.id, ds.name)
    _advance_ds(session, ds, now)


# ── flussi: esecuzione schedulata ───────────────────────────────────────────
def _advance_flow(session: Session, flow: Flow, now: datetime) -> None:
    try:
        flow.next_run_at = next_fire(flow.run_schedule, now).replace(tzinfo=None)
    except Exception:
        flow.run_schedule = None
        flow.next_run_at = None
    session.add(flow)
    session.commit()


def _disable_flow(session: Session, flow: Flow, reason: str) -> None:
    logger.warning("scheduler: esecuzione flusso %s disabilitata (%s)", flow.id, reason)
    flow.run_schedule = None
    flow.run_scheduled_by = None
    flow.next_run_at = None
    session.add(flow)
    session.commit()


async def _fire_flow(session: Session, flow: Flow, now: datetime) -> None:
    user = session.get(User, flow.run_scheduled_by) if flow.run_scheduled_by else None
    if user is None or not user.is_active:
        _disable_flow(session, flow, "autore dello schedule assente o disattivato")
        return
    # orchestrazione (refresh → output → runflow) come task detached: non blocca
    # il tick, e la guardia _running evita di sovrapporre lo stesso flusso
    # i run schedulati sono "produzione": usano flow.production_engine (se impostato)
    asyncio.create_task(orchestrate_bg(flow.id, user.id, trigger_type="schedule", engine_mode="production"))
    logger.info("scheduler: orchestrazione flusso %s (%s) avviata", flow.id, flow.name)
    _advance_flow(session, flow, now)


# ── loop ────────────────────────────────────────────────────────────────────
async def _tick() -> None:
    now = datetime.now(timezone.utc)
    now_naive = now.replace(tzinfo=None)  # le colonne TIMESTAMP sono naive-UTC
    with Session(engine) as session:
        # Reconcile SERVER-SIDE dei run non terminali: un ingest/flusso completato
        # lato engine va APPLICATO (swap parquet + schema della datasource) entro
        # un tick anche se nessuno sta pollando dal frontend. Senza questo, un
        # refresh finito mentre l'utente ha lasciato la vista resta invisibile e
        # la datasource continua a puntare ai dati VECCHI (l'utente non sa cosa sta
        # guardando). _reconcile salta i run già terminali e quelli senza task
        # engine, e il claim a SUCCESS è atomico → sicuro anche col poll della UI.
        try:
            pending = session.exec(
                select(Run).where(
                    Run.status.not_in(TERMINAL_STATES),  # type: ignore[union-attr]
                    Run.task_id != "",
                )
            ).all()
            for r in pending:
                try:
                    await _reconcile(session, r)
                except Exception:
                    logger.exception("scheduler: reconcile del run %s fallito", r.id)
        except Exception:
            logger.exception("scheduler: passata di reconcile dei run fallita")

        due_ds = session.exec(
            select(Datasource).where(
                Datasource.refresh_schedule.is_not(None),  # type: ignore[union-attr]
                Datasource.next_refresh_at.is_not(None),  # type: ignore[union-attr]
                Datasource.next_refresh_at <= now_naive,
            )
        ).all()
        for ds in due_ds:
            try:
                await _fire_ds(session, ds, now)
            except Exception:
                logger.exception("scheduler: refresh datasource %s fallito", ds.id)
                _advance_ds(session, ds, now)

        due_flows = session.exec(
            select(Flow).where(
                Flow.run_schedule.is_not(None),  # type: ignore[union-attr]
                Flow.next_run_at.is_not(None),  # type: ignore[union-attr]
                Flow.next_run_at <= now_naive,
            )
        ).all()
        for flow in due_flows:
            try:
                await _fire_flow(session, flow, now)
            except Exception:
                logger.exception("scheduler: esecuzione flusso %s fallita", flow.id)
                _advance_flow(session, flow, now)

        # cancellazione differita dei blob la cui grace è scaduta (snapshot
        # superati, parquet rimpiazzati, datasource eliminate)
        try:
            removed = await sweep_blob_deletions(session, now)
            if removed:
                logger.info("scheduler: %d blob obsoleti eliminati", removed)
        except Exception:
            logger.exception("scheduler: sweep dei blob differiti fallito")


async def scheduler_loop(stop: asyncio.Event) -> None:
    logger.info("scheduler avviato (tick %ss): refresh datasource + esecuzione flussi", TICK_SECONDS)
    while not stop.is_set():
        try:
            await _tick()
        except Exception:
            logger.exception("scheduler: errore nel tick")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("scheduler fermato")
