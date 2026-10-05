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

PIÙ PROCESSI possono far girare questo scheduler insieme (`APP__ROLE`): ognuno
vede gli stessi slot scaduti, e ne lancia il lavoro UNO solo. Chi vuole uno slot
lo PRENDE prima di lanciare, spostando in avanti la prossima esecuzione con un
UPDATE condizionato al valore che ha letto: se un altro l'ha già spostata, trova
zero righe e lascia stare. Prima si prende e poi si lancia: se il processo muore
in mezzo lo slot è perso (una volta in meno), mai eseguito due volte.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import update
from sqlmodel import Session, select

from app.db.session import engine
from app.models import Connection, Datasource, Flow, Run, User
from app.models.run import TERMINAL_STATES
from app.routes.runs import _reconcile, launch_ingest_run
from app.services.blobgc import sweep_blob_deletions
from app.services import contracts as contract_service
from app.services import flow_presence, login_throttle, orchestrator
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


def _prendi_slot_ds(session: Session, ds: Datasource, now: datetime, visto: datetime | None = None) -> bool:
    """Sposta in avanti il prossimo refresh SOLO SE è ancora quello letto: vero
    per un processo solo, quello a cui tocca lo slot.

    `visto` è il valore letto dalla query degli scaduti. Va passato da chi ha
    fatto altro in mezzo: ogni commit della sessione fa scadere gli oggetti, e
    rileggere `ds.next_refresh_at` qui darebbe il valore GIÀ spostato da un altro
    processo — con cui il confronto riuscirebbe sempre. Per lo stesso motivo
    l'UPDATE chiede anche che lo slot sia ancora scaduto: uno già preso sta nel
    futuro."""
    visto = ds.next_refresh_at if visto is None else visto
    valori: dict = {}
    try:
        valori["next_refresh_at"] = next_fire(ds.refresh_schedule, now).replace(tzinfo=None)
    except Exception:
        valori.update(refresh_schedule=None, next_refresh_at=None)
    preso = session.exec(
        update(Datasource)
        .where(Datasource.id == ds.id, Datasource.next_refresh_at == visto, Datasource.next_refresh_at <= now.replace(tzinfo=None))
        .values(**valori)
    )
    session.commit()
    return preso.rowcount == 1


def _disable_ds(session: Session, ds: Datasource, reason: str) -> None:
    logger.warning("scheduler: refresh datasource %s disabilitato (%s)", ds.id, reason)
    ds.refresh_schedule = None
    ds.refresh_scheduled_by = None
    ds.next_refresh_at = None
    session.add(ds)
    session.commit()


async def _fire_ds(session: Session, ds: Datasource, now: datetime, visto: datetime | None = None) -> None:
    visto = ds.next_refresh_at if visto is None else visto  # prima di ogni commit: vedi _prendi_slot_ds
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
            if _prendi_slot_ds(session, ds, now, visto):
                logger.info("scheduler: datasource %s ha già un refresh in corso, salto lo slot", ds.id)
            return
    if not _prendi_slot_ds(session, ds, now, visto):
        return  # lo slot l'ha preso un altro processo
    await launch_ingest_run(session, user, ds, conn, trigger_type="schedule")
    logger.info("scheduler: refresh schedulato lanciato per datasource %s (%s)", ds.id, ds.name)


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


async def _fire_flow(session: Session, flow: Flow, now: datetime, visto: datetime | None = None) -> None:
    visto = flow.next_run_at if visto is None else visto  # prima di ogni commit: vedi _prendi_slot_ds
    user = session.get(User, flow.run_scheduled_by) if flow.run_scheduled_by else None
    if user is None or not user.is_active:
        _disable_flow(session, flow, "autore dello schedule assente o disattivato")
        return
    flow_id, nome = flow.id, flow.name
    valori: dict = {}
    try:
        valori["next_run_at"] = next_fire(flow.run_schedule, now).replace(tzinfo=None)
    except Exception:
        valori.update(run_schedule=None, next_run_at=None)
    preso = session.exec(
        update(Flow)
        .where(Flow.id == flow_id, Flow.next_run_at == visto, Flow.next_run_at <= now.replace(tzinfo=None))
        .values(**valori)
    )
    if preso.rowcount != 1:
        session.rollback()  # lo slot l'ha preso un altro processo
        return
    if orchestrator.flusso_in_corso(session, flow_id):
        session.commit()
        logger.info("scheduler: flusso %s (%s) ancora in esecuzione, salto lo slot", flow_id, nome)
        return
    # Lo slot preso e l'esecuzione in coda nella STESSA transazione: o entrambi o
    # nessuno. La esegue un orchestratore (refresh → output → runflow), questo o
    # un altro; i run schedulati sono "produzione": usano flow.production_engine.
    orchestrator.create_orchestration_run(session, user, flow, "schedule", "production", commit=False)
    session.commit()
    orchestrator.sveglia()
    logger.info("scheduler: esecuzione del flusso %s (%s) messa in coda", flow_id, nome)


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
        # il valore dello slot si fissa ADESSO, com'è uscito dalla query: dopo il
        # primo commit gli oggetti scadono e rileggerli darebbe quello già
        # spostato da un altro processo
        for ds, visto in [(ds, ds.next_refresh_at) for ds in due_ds]:
            try:
                await _fire_ds(session, ds, now, visto)
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
        for flow, visto in [(flow, flow.next_run_at) for flow in due_flows]:
            try:
                await _fire_flow(session, flow, now, visto)
            except Exception:
                logger.exception("scheduler: esecuzione flusso %s fallita", flow.id)
                _advance_flow(session, flow, now)

        # data contracts: la freschezza è l'unica regola che si rompe da sola,
        # col passare del tempo — qui la si ricontrolla
        try:
            scaduti = contract_service.sweep_freshness(session, now)
            if scaduti:
                logger.info("scheduler: %d data contract hanno cambiato stato per la freschezza", scaduti)
        except Exception:
            logger.exception("scheduler: controllo della freschezza dei data contract fallito")
        # …e chi ha chiesto di essere avvisato lo viene, quando uno stato cambia
        from app.services import contract_notifier  # tardivo: passa dalle rotte delle connessioni

        mandati = await contract_notifier.deliver_pending(session)
        if mandati:
            logger.info("scheduler: %d avvisi di data contract inviati", mandati)

        # stato di breve durata condiviso fra le repliche: via le righe scadute
        try:
            login_throttle.pulisci(session)
            flow_presence.pulisci(session)
        except Exception:
            logger.exception("scheduler: pulizia dello stato condiviso fallita")

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
