"""Orchestrazione di un run di flusso: interpreta i NODI DI CONTROLLO del canvas
(non sono operazioni dell'engine, come i nodi Output) nell'ordine definito dagli
ARCHI DI SEQUENZA (topological sort, vedi `flow_resolver.sequence_order`); i nodi
non collegati usano il default refresh → output → runflow.

Tipi di action node:
  - refresh   — aggiorna la datasource e ASPETTA che finisca (se fallisce, tutto
                il run si ferma: meglio niente che dati stantii/parziali);
  - output    — lancia un nodo Output di questo flusso (via flow_resolver) e ne
                ATTENDE il completamento;
  - runflow   — esegue un altro flusso salvato (guardia anti-ciclo/profondità),
                attendendone gli output.

Ogni passo attende la fine del precedente: così l'ordine è REALE (un output 'a
valle' vede i dati che un refresh o un flusso 'a monte' hanno appena scritto),
non solo un ordine di lancio.

Traccia l'intera esecuzione in un 'run di orchestrazione' (kind=orchestration)
osservabile in cronologia. Con l'autorità di `user`, RBAC ri-verificata a ogni
passo (RUN/CONNECT per il refresh, RUN + EDIT/CONNECT per gli output).

CHI la esegue. Lo scheduler e il run manuale (`POST /flows/{id}/run-now`) non la
eseguono: la mettono IN CODA, cioè scrivono la riga del run in stato PENDING. La
prende un processo che fa da orchestratore (`APP__ROLE` = all | orchestrator;
possono essere più d'uno) e la porta a STARTED firmandola — un UPDATE
condizionato, quindi la prende uno solo. Da lì:

  - mentre la esegue ne aggiorna il BATTITO (`heartbeat_at`) — solo delle
    esecuzioni che hanno davvero un task vivo in questo processo, non di tutte
    quelle che ha firmato: una riga firmata il cui task è morto deve smettere
    di battere, o resterebbe «in corso» per sempre;
  - un indice unico sul database garantisce che di un flusso ne giri una sola;
  - un'orchestrazione STARTED il cui battito è fermo è di un processo morto: il
    primo orchestratore che se ne accorge la chiude dicendolo. NON la si
    riprende: i passi già eseguiti hanno scritto, e rifarli duplicherebbe gli
    Output in append;
  - prima di ogni passo chi esegue controlla che la riga sia ancora sua: se nel
    frattempo è stata chiusa (non riusciva a battere), si ferma.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import get_settings
from app.db.session import engine, engine_battito
from app.models import Connection, Datasource, Flow, Run, User
from app.models.permission import Capability
from app.models.run import ORCHESTRATION_INTERRUPTED, STEP_INTERRUPTED, TERMINAL_STATES  # noqa: F401 — riesportata
from app.routes.runs import (
    _launch_flow_run, _reconcile, _revoke_task, close_if_queued_too_long, launch_ingest_run, live_refresh,
    start_refresh_if_free,
)
from app.schemas.models import RunCreate
from app.services import permissions as perm_service
from app.services.flow_resolver import FlowResolveError, resolve_output_request, sequence_order

logger = logging.getLogger(__name__)

MAX_FLOW_DEPTH = 5  # guardia sui runflow annidati (oltre al set anti-ciclo)

# Tetti d'attesa e ritmi della coda (ORCHESTRATOR__* nell'env, o Amministrazione →
# Settings): l'orchestratore aspetta che refresh e output COMPLETINO prima dei passi a
# valle, così l'ordine è reale; oltre il tetto il flusso aborta invece di leggere dati
# stantii. Letti a ogni uso, non all'avvio: una modifica dal pannello vale entro pochi
# secondi, anche per le esecuzioni già in corso.
def _orc():
    return get_settings().orchestrator


def concorrenza() -> int:
    """ORCHESTRATOR__MAX_CONCURRENT, never beyond the connections this process's pool can
    give (each execution holds one while it asks the engine). The pool is sized when the
    process starts: raised together from the panel, the executions follow the pool only
    once the processes have restarted."""
    voluta = _orc().max_concurrent
    try:
        overflow = int(getattr(engine.pool, "_max_overflow", 0))
        if overflow < 0:  # no ceiling on the pool
            return voluta
        return max(1, min(voluta, engine.pool.size() + overflow))
    except Exception:  # noqa: BLE001 — a pool without a size (the tests' SQLite)
        return voluta


ORPHAN_SWEEP_S = 15.0  # ogni quanto si cercano le orchestrazioni di processi morti

# La firma di QUESTO processo sulle orchestrazioni che esegue. Cambia a ogni
# avvio: un processo che riparte non eredita quelle del precedente.
ISTANZA = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

ALREADY_RUNNING = "flow already running"


def _adesso() -> datetime:
    """Le colonne TIMESTAMP sono naive-UTC: battito e confronto usano la stessa forma."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OrchestrationError(RuntimeError):
    pass


class Spodestata(OrchestrationError):
    """Il run non è più di questo processo: è stato chiuso da un altro (che non
    ne vedeva più il battito) o non esiste più. I passi rimanenti non si
    eseguono, e non c'è niente da registrare: l'esito è già scritto."""


def _ancora_mia(session: Session, run_id: int) -> None:
    riga = session.exec(select(Run.status, Run.claimed_by).where(Run.id == run_id)).first()
    if riga is None or riga[0] != "STARTED" or riga[1] != ISTANZA:
        raise Spodestata(f"orchestration {run_id} no longer belongs to this process")


class StopSequence(OrchestrationError):
    """Interrompe la SEQUENZA, non solo il nodo: la solleva l'output email con
    `stopOnFailure`. Serve un tipo suo perché il blocco che esegue gli output ha
    un `except Exception` che raccoglie gli errori e prosegue — comportamento
    voluto per gli altri output. Senza un tipo distinto, l'interruzione veniva
    raccolta da quel medesimo except e la sequenza continuava lo stesso."""


async def _refresh_and_wait(
    session: Session, user: User, ds_id: int, trigger_type: str = "manual", parent_run_id: int | None = None
) -> None:
    """Aggiorna una datasource database e attende (bounded) il SUCCESS."""
    ds = session.get(Datasource, ds_id)
    if ds is None or ds.kind != "database":
        return  # non è una datasource DB (nulla da refreshare)
    if not perm_service.has_capability(session, user, ds.project_id, Capability.RUN):
        raise OrchestrationError(f"RUN missing for the refresh of datasource {ds_id}")
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        raise OrchestrationError(f"datasource {ds_id}: the connection does not exist")
    if not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        raise OrchestrationError(f"CONNECT missing for the refresh of datasource {ds_id}")

    # se c'è già un refresh in corso lo si attende, altrimenti se ne lancia uno — in
    # line with every other starter (see runs.start_refresh_if_free)
    run = None
    for _ in range(3):  # the running one may end between the two looks: then start ours
        run = await start_refresh_if_free(session, user, ds, conn, trigger_type=trigger_type,
                                          parent_run_id=parent_run_id, launch=launch_ingest_run)
        if run is None:
            live = live_refresh(session, ds_id)
            run = session.get(Run, live) if live is not None else None
        if run is not None:
            break
    if run is None:
        raise OrchestrationError(f"refresh of datasource {ds_id} could not be started")

    from app.services.waits import waits

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(_orc().refresh_wait_seconds, _orc().poll_interval_seconds)
    for pause in waits(_orc().poll_interval_seconds):
        run = await _reconcile(session, run)
        if run.status in TERMINAL_STATES or loop.time() >= deadline:
            break
        # rilascia la connessione del pool durante l'attesa: mentre lo stato non
        # cambia _reconcile non committa e la transazione della SELECT terrebbe
        # occupata una connessione per tutto lo sleep → con più orchestrazioni
        # lente in parallelo il pool si esaurisce e il gateway va giù.
        session.commit()
        await asyncio.sleep(pause)
    if run.status != "SUCCESS":
        raise OrchestrationError(f"refresh of datasource {ds_id} failed ({run.status})")
    logger.info("orchestrate: datasource %s aggiornata (refresh completato)", ds_id)


async def _wait_run(session: Session, run: Run) -> Run:
    """Attende che un run di output raggiunga uno stato terminale, così che i
    passi successivi dell'ordine vedano davvero il risultato già scritto (senza
    attesa, l'ordinamento sarebbe solo cosmetico: i passi corrono in parallelo e
    un output 'a valle' leggerebbe dati non ancora prodotti).

    No cap of its own: the run's life is already bounded, by the worker's hard time
    limit and by _reconcile, which revokes and fails a run older than
    ENGINE__RUN_STALE_TIMEOUT_SECONDS (counted from its real start, or from launch if
    it never started). A ten-minute cap here counted the time in the worker queue
    too, and on expiry let the flow move on and close while the output kept
    writing: steps downstream read old data, and the next slot could start the
    same flow again on top of it."""
    from app.services.waits import waits

    for pause in waits(_orc().poll_interval_seconds):
        run = await _reconcile(session, run)
        if run.status in TERMINAL_STATES:
            return run
        # vedi _refresh_and_wait: libera la connessione del pool durante lo sleep
        session.commit()
        await asyncio.sleep(pause)


async def orchestrate(session: Session, user: User, flow: Flow, depth: int = 0, seen: set[int] | None = None, trigger_type: str = "manual", parent_run_id: int | None = None, engine_mode: str = "development", custodita: bool = False) -> list[str]:
    """Esegue un flusso: gli 'action node' (refresh/output/runflow) nell'ordine
    definito dagli archi di sequenza (topological sort), coi non collegati
    nell'ordine di default refresh → output → runflow.

    Un refresh fallito SOLLEVA (interrompe tutto: meglio niente che dati stantii);
    un output o un sotto-flusso in errore vengono raccolti e si prosegue. Torna la
    lista degli errori non fatali (vuota = tutto ok) per marcare il run di
    orchestrazione.

    `engine_mode` ("development" | "production") decide il motore di ogni output
    (vedi `_launch_flow_run`); i sotto-flussi (runflow) lo ereditano e ciascuno
    risolve il PROPRIO motore di produzione.

    `custodita`: prima di ogni passo si controlla che il run `parent_run_id` sia
    ancora di questo processo (vedi `Spodestata`). Lo passa chi esegue dalla coda."""
    seen = seen or set()
    if flow.id in seen or depth > MAX_FLOW_DEPTH:
        logger.warning("orchestrate: ciclo o profondità eccessiva su flusso %s (depth %d)", flow.id, depth)
        return [f"flow {flow.id}: cycle or excessive depth"]
    seen = seen | {flow.id}

    definition = json.loads(flow.definition or "{}")
    default_bucket = get_settings().engine.bucket
    errors: list[str] = []

    def resolve_ds(i: int):
        d = session.get(Datasource, i)
        return (d.bucket, d.key) if d and d.key else None

    # where the Script and Container nodes already run wrote their output: the nodes after them read it
    script_outputs: dict[str, tuple[str, str]] = {}

    for node in sequence_order(definition):
        t = node.get("type")
        d = node.get("data") or {}
        if custodita and parent_run_id is not None:
            _ancora_mia(session, parent_run_id)
        if t == "refresh":
            ds_id = d.get("datasourceId")
            if ds_id:
                await _refresh_and_wait(session, user, ds_id, trigger_type, parent_run_id)  # errore → propaga (aborta tutto)
        elif t == "output":
            try:
                # engine_mode decide anche il CAMPIONE di sviluppo: solo "development"
                # lo inietta; in produzione la catena legge tutti i record
                req = resolve_output_request(definition, node, resolve_ds, default_bucket, engine_mode, script_outputs)
                run = await _launch_flow_run(session, user, flow, RunCreate(**req), trigger_type=trigger_type, parent_run_id=parent_run_id, engine_mode=engine_mode)
                run = await _wait_run(session, run)  # attende: l'ordine dev'essere reale
                if run.status != "SUCCESS":
                    # Un output email con `stopOnFailure` INTERROMPE la sequenza,
                    # come fa il refresh. Gli altri output restano raccolti e si
                    # prosegue: la differenza è voluta e vale solo per l'email,
                    # perché i passi a valle di una notifica spesso la danno per
                    # avvenuta (marcare "notificato" senza aver notificato è
                    # peggio che fermarsi). L'opzione è sul nodo, non implicita.
                    if d.get("destType") == "email" and d.get("stopOnFailure") is not False:
                        raise StopSequence(
                            f"email sending failed (run {run.id}): {run.error or run.status}. "
                            "The next steps of the flow were not run."
                        )
                    errors.append(f"output: run {run.id} {run.status} — {run.error or ''}".strip())
            except (StopSequence, Spodestata):
                # devono uscire: l'except qui sotto le raccoglierebbe come un
                # errore qualunque e i passi a valle girerebbero comunque —
                # dando per avvenuta una notifica che non è partita, o
                # continuando un run che non è più nostro
                raise
            except Exception as e:
                logger.warning("orchestrate: flusso %s, output non eseguito: %s", flow.id, e)
                if d.get("destType") == "email" and d.get("stopOnFailure") is not False:
                    # refused before its run existed (a template the engine rejects, a
                    # recipient outside the allowed domains): the email did not leave
                    # either, so the steps after it must not run
                    reason = getattr(e, "detail", None) or str(e)
                    raise StopSequence(
                        f"email not sent, refused at launch: {reason}. The next steps of the flow were not run."
                    ) from e
                errors.append(f"output: {e}")
        elif t == "script":
            # a failed script stops the flow, as a failed refresh does: the steps after
            # it would read nothing, or stale data
            from app.services import script_runs

            try:
                out = await script_runs.run_script(
                    session, user, flow, definition, node, parent_run_id=parent_run_id, trigger_type=trigger_type,
                    engine_mode=engine_mode, script_outputs=script_outputs, resolve_ds=resolve_ds,
                    default_bucket=default_bucket, launch=_launch_flow_run, wait=_wait_run)
            except (script_runs.ScriptFailed, FlowResolveError) as e:
                raise OrchestrationError(str(e)) from e
            if out is not None:
                script_outputs[node["id"]] = out
        elif t == "container":
            # as a Script node: a failure stops the flow
            from app.services import container_runs

            try:
                out = await container_runs.run_container(
                    session, user, flow, definition, node, parent_run_id=parent_run_id, trigger_type=trigger_type,
                    engine_mode=engine_mode, script_outputs=script_outputs, resolve_ds=resolve_ds,
                    default_bucket=default_bucket, launch=_launch_flow_run, wait=_wait_run)
            except (container_runs.ContainerFailed, FlowResolveError) as e:
                raise OrchestrationError(str(e)) from e
            if out is not None:
                script_outputs[node["id"]] = out
        elif t == "runflow":
            sub_id = d.get("flowId")
            if not sub_id:
                continue
            sub = session.get(Flow, sub_id)
            if sub is None:
                logger.warning("orchestrate: flusso %s riferisce un runflow inesistente %s", flow.id, sub_id)
                errors.append(f"runflow: flow {sub_id} does not exist")
                continue
            # i figli dei sotto-flussi riferiscono lo stesso run di orchestrazione
            # di testa (non c'è un tracciante separato per i sotto-flussi)
            errors.extend(await orchestrate(session, user, sub, depth + 1, seen, trigger_type, parent_run_id, engine_mode, custodita))
    return errors


def create_orchestration_run(
    session: Session, user: User, flow: Flow, trigger_type: str = "manual",
    engine_mode: str = "development", commit: bool = True,
) -> Run:
    """METTE IN CODA un'esecuzione del flusso: crea la riga 'run di orchestrazione'
    (kind=orchestration) in stato PENDING. È il TRACCIANTE dell'intera esecuzione
    (anche di flussi senza nodo Output, che altrimenti non lascerebbero traccia)
    ed è insieme la voce di coda: la prende un orchestratore (`rivendica`). Non ha
    un task Celery, `_reconcile` la salta.

    `user` è l'autorità con cui girerà; `engine_mode`: "production" dallo
    scheduler e da run-now ?mode=production. `commit=False` per chi la scrive
    nella stessa transazione di qualcos'altro (lo scheduler: lo slot preso e la
    riga in coda o ci sono entrambi o nessuno)."""
    run = Run(
        kind="orchestration",
        flow_id=flow.id,
        task_id="",
        status="PENDING",
        launched_by=user.id,
        trigger_type=trigger_type,
        engine_mode=engine_mode,
        input_key="",
        output_bucket="",
        output_key="",
    )
    session.add(run)
    if commit:
        session.commit()
        session.refresh(run)
    return run


# spazio di nomi dei lucchetti consultivi «coda del flusso» (il secondo numero è l'id)
_LUCCHETTO_CODA = 74610001
# the same for flow groups, in a namespace of their own (a group and a flow can share an id)
_GROUP_QUEUE_LOCK = 74610004


def create_group_run(session: Session, user: User, group, trigger_type: str = "manual", commit: bool = True) -> Run:
    """Queues a flow group's run: the same PENDING orchestration row a flow gets,
    with `flow_group_id` instead of `flow_id`. An orchestrator claims it and hands it
    to services/flow_group_runner.py. Its flows always run in production mode."""
    run = Run(
        kind="orchestration", flow_group_id=group.id, task_id="", status="PENDING",
        launched_by=user.id, trigger_type=trigger_type, engine_mode="production",
        input_key="", output_bucket="", output_key="",
    )
    session.add(run)
    if commit:
        session.commit()
        session.refresh(run)
    return run


def in_line_for_the_group(session: Session, group_id: int) -> None:
    """As `in_fila_per_il_flusso`, for a flow group: two replicas, or a replica and
    the scheduler, cannot both see no run and both queue one."""
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(:space, :group)"), {"space": _GROUP_QUEUE_LOCK, "group": group_id}
        )


def group_running(session: Session, group_id: int) -> bool:
    """The group has a run queued or in progress."""
    return session.exec(
        select(Run.id)
        .where(Run.kind == "orchestration", Run.flow_group_id == group_id, Run.status.in_(("PENDING", "STARTED")))  # type: ignore[union-attr]
        .limit(1)
    ).first() is not None


def in_fila_per_il_flusso(session: Session, flow_id: int) -> None:
    """Mette in fila, fino alla fine della transazione, chi sta per guardare se il
    flusso ha già un'esecuzione e per accodarne una: «Esegui ora» da più repliche
    del gateway, e lo scheduler. Senza, due richieste insieme vedono entrambe la
    coda vuota e accodano entrambe (dal vivo: 2 lanci su 20 passavano). Lucchetto
    consultivo di Postgres legato alla transazione: lo rilascia il commit. Su
    SQLite (i test) non serve: c'è un processo solo."""
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(:spazio, :flusso)"), {"spazio": _LUCCHETTO_CODA, "flusso": flow_id}
        )


def flusso_in_corso(session: Session, flow_id: int) -> bool:
    """C'è già un'esecuzione del flusso, in corso o in coda."""
    return session.exec(
        select(Run.id)
        .where(Run.kind == "orchestration", Run.flow_id == flow_id, Run.status.in_(("PENDING", "STARTED")))  # type: ignore[union-attr]
        .limit(1)
    ).first() is not None


async def _finalize_orch_run(run_id: int, status: str, error: str | None = None, solo_in_coda: bool = False) -> None:
    """Chiude il run di orchestrazione (sessione propria: gira nel task detached).

    UPDATE condizionato: chiude solo chi lo trova ancora aperto. Più processi
    possono arrivarci insieme (chi esegue e chi lo dà per perso), e l'avviso di
    fallimento deve partire una volta sola.

    `solo_in_coda`: lo chiude solo se è ancora PENDING. Per il lancio respinto
    perché il flusso girava già: fra il rifiuto e questa chiusura l'altra
    esecuzione può essere finita e un altro processo può aver preso questa — che
    a quel punto sta girando, e non va chiusa come «mai partita»."""
    aperto = Run.status == "PENDING" if solo_in_coda else Run.status.not_in(TERMINAL_STATES)  # type: ignore[union-attr]
    with Session(engine) as session:
        chiuso = session.exec(
            update(Run)
            .where(Run.id == run_id, aperto)
            .values(status=status, error=error[:2000] if error else None, finished_at=datetime.now(timezone.utc))
        )
        if chiuso.rowcount == 1:
            # i webhook nella stessa transazione della chiusura (vedi services/webhooks.py)
            from app.services import webhooks

            webhooks.accoda_per_run(session, session.get(Run, run_id))
        session.commit()
        if chiuso.rowcount != 1:
            return
        run = session.get(Run, run_id)
        # l'avviso parte DOPO il commit: si annuncia un fatto già scritto, e non
        # tiene aperta la transazione per il tempo di una spedizione SMTP
        from app.services import openlineage
        from app.services.notifier import notify_failure

        openlineage.run_closed(session, run)
        await notify_failure(session, run)


async def ferma_passi(run_id: int) -> int:
    """The steps still open of an orchestration closed as interrupted are stopped: an
    Output or a refresh already with a worker went on writing while the flow, closed,
    could start again on top of it (the same rows twice in an append Output, two
    writers on a replaced table). Their engine tasks are revoked, the Jobs of Script
    and Container nodes deleted, a flow group's flows closed the same way. Called
    after the orchestration is closed (a Script or Container run is an orphan only
    then); best effort, one step at a time: what fails is logged, the rest still go."""
    from app.services.script_runs import close_if_orphaned

    fermati = 0
    with Session(engine) as session:
        aperti = session.exec(select(Run).where(
            Run.parent_run_id == run_id, Run.status.not_in(TERMINAL_STATES))).all()  # type: ignore[union-attr]
        for run in aperti:
            try:
                if run.kind in ("script", "container"):
                    await close_if_orphaned(session, run)
                    continue
                if run.kind == "orchestration":  # a flow of a flow group
                    chiuso = session.exec(update(Run).where(Run.id == run.id, Run.status.not_in(TERMINAL_STATES))
                                          .values(status="FAILURE", error=ORCHESTRATION_INTERRUPTED,
                                                  finished_at=datetime.now(timezone.utc)))
                    session.commit()
                    if chiuso.rowcount == 1:
                        fermati += 1 + await ferma_passi(run.id)
                    continue
                # first what the engine says: a step that ended meanwhile keeps its outcome
                # and its effects (a refresh's new parquet swapped in, an Output published),
                # instead of being failed as stopped
                run = await _reconcile(session, run)
                session.commit()
                if run.status in TERMINAL_STATES:
                    continue
                await _revoke_task(run)
                chiuso = session.exec(update(Run).where(Run.id == run.id, Run.status.not_in(TERMINAL_STATES))
                                      .values(status="FAILURE", error=STEP_INTERRUPTED,
                                              finished_at=datetime.now(timezone.utc)))
                session.commit()
                fermati += chiuso.rowcount
            except Exception:  # noqa: BLE001 — one step must not keep the others running
                session.rollback()
                logger.exception("orchestrate: passo %s dell'orchestrazione interrotta %s non fermato", run.id, run_id)
    if fermati:
        logger.warning("orchestrate: %d passi dell'orchestrazione interrotta %s fermati", fermati, run_id)
    return fermati


async def interrompi(run_id: int) -> None:
    """Closes an orchestration whose process is stopping, then stops its steps."""
    await _finalize_orch_run(run_id, "FAILURE", ORCHESTRATION_INTERRUPTED)
    await ferma_passi(run_id)


# ── Prendere dalla coda ──────────────────────────────────────────────────────
def rivendica(limite: int) -> tuple[list[int], list[int]]:
    """Prende dalla coda fino a `limite` orchestrazioni e le firma come di questo
    processo. Torna `(prese, respinte)`: le seconde sono lanci MANUALI di un
    flusso che sta già girando, da chiudere come fallite.

    Ogni presa è un UPDATE condizionato sullo stato: se due processi provano la
    stessa riga la ottiene uno solo, l'altro trova zero righe e passa oltre. Se
    del flusso gira già un'esecuzione è l'indice unico a rifiutare l'UPDATE."""
    prese: list[int] = []
    respinte: list[int] = []
    with Session(engine) as session:
        candidati = session.exec(
            select(Run.id, Run.trigger_type)
            .where(Run.kind == "orchestration", Run.status == "PENDING")
            .order_by(Run.id)
            .limit(limite + 20)  # qualcuna la prenderà un altro, o sarà respinta
        ).all()
        session.rollback()
        for run_id, trigger in candidati:
            if len(prese) >= limite:
                break
            try:
                presa = session.exec(
                    update(Run)
                    .where(Run.id == run_id, Run.status == "PENDING")
                    .values(status="STARTED", claimed_by=ISTANZA, heartbeat_at=_adesso())
                )
                session.commit()
            except IntegrityError:
                session.rollback()
                if trigger == "schedule":
                    # uno slot scattato mentre il giro precedente non era finito:
                    # si salta senza lasciare traccia, come ha sempre fatto
                    session.exec(delete(Run).where(Run.id == run_id, Run.status == "PENDING"))
                    session.commit()
                    logger.info("orchestrate: run %s, flusso già in esecuzione: slot saltato", run_id)
                else:
                    respinte.append(run_id)
                continue
            except Exception:
                # database caduto a metà: ci si ferma qui e si torna quello che
                # si è già preso, così chi chiama lo esegue. Sollevare lo
                # perderebbe: righe firmate senza nessuno che le porti avanti.
                logger.exception("orchestrate: presa dalla coda interrotta al run %s", run_id)
                session.rollback()
                break
            if presa.rowcount == 1:
                prese.append(run_id)
    return prese, respinte


async def esegui(run_id: int) -> None:
    """Porta in fondo un'orchestrazione che questo processo ha rivendicato, e ne
    registra l'esito sul run."""
    try:
        with Session(engine) as session:
            group_id, script_id = session.exec(
                select(Run.flow_group_id, Run.saved_script_id).where(Run.id == run_id)).first() or (None, None)
        if script_id is not None:
            # a saved script's run: same queue, claim and heartbeat, its own runner
            from app.services.saved_script_runs import run_queued

            await run_queued(run_id)
            return
        if group_id is not None:
            # a flow group's run: same queue, claim and heartbeat, its own runner
            from app.services.flow_group_runner import run_group  # imports this module

            await run_group(run_id)
            return
        with Session(engine) as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            flow = session.get(Flow, run.flow_id) if run.flow_id else None
            user = session.get(User, run.launched_by) if run.launched_by else None
            if flow is None or user is None or not user.is_active:
                logger.warning("orchestrate: run %s, flusso %s o utente %s non validi", run_id, run.flow_id, run.launched_by)
                await _finalize_orch_run(run_id, "FAILURE", "invalid flow or user")
                return
            flow_id = flow.id
            if run.trigger_type == "schedule":
                # the schedule's author must still hold everything the run will need
                from app.services import schedule_authority

                gaps = schedule_authority.gaps_of_flow(session, user, flow)
                if gaps:
                    logger.warning("orchestrate: scheduled run %s of flow %s not started: %s", run_id, flow_id, gaps)
                    await _finalize_orch_run(run_id, "FAILURE", schedule_authority.not_started(user, gaps))
                    return
            try:
                # i run figli (output/refresh) riferiscono il run di orchestrazione:
                # così il calendario conta solo quest'ultimo, non i doppioni figli
                errors = await orchestrate(
                    session, user, flow, trigger_type=run.trigger_type, parent_run_id=run_id,
                    engine_mode=run.engine_mode or "development", custodita=True,
                )
            except Spodestata as e:
                logger.warning("orchestrate: flusso %s, mi fermo: %s", flow_id, e)
                return
            except (FlowResolveError, OrchestrationError) as e:
                logger.warning("orchestrate: flusso %s interrotto: %s", flow_id, e)
                await _finalize_orch_run(run_id, "FAILURE", str(e))
                return
            status = "FAILURE" if errors else "SUCCESS"
            await _finalize_orch_run(run_id, status, "; ".join(errors) if errors else None)
            logger.info("orchestrate: flusso %s completato (%s)", flow_id, status)
    except asyncio.CancelledError:
        # il processo si sta fermando: i passi rimanenti non verranno eseguiti.
        # Lo si scrive subito, invece di lasciarlo scoprire a chi ne cercherà il battito,
        # e si fermano quelli già partiti
        await interrompi(run_id)
        raise
    except Exception:
        logger.exception("orchestrate: run %s fallito", run_id)
        await _finalize_orch_run(run_id, "FAILURE", "internal error during the orchestration")


# ── Battito, e chi non batte più ─────────────────────────────────────────────
def batti() -> int:
    """Dice «ci sono» sulle orchestrazioni che questo processo sta eseguendo
    DAVVERO: quelle con un task vivo (`_vive`), non tutte quelle che ha firmato.

    La differenza conta quando un task muore senza chiudere il suo run — il
    database cade mentre scrive l'esito, o la presa dalla coda si interrompe a
    metà. Se si battesse per firma, quella riga continuerebbe a risultare viva
    finché il processo campa: nessuno la chiuderebbe, e l'indice unico terrebbe
    il flusso bloccato. Così invece smette di battere e viene chiusa come orfana.

    Su una connessione sua (`engine_battito`), che il pool delle richieste non
    può togliergli."""
    with _vive_lock:
        ids = list(_vive)
    with Session(engine_battito) as session:
        if not ids:
            session.exec(text("SELECT 1"))  # the database answers: see battito_sano
            return 0
        vive = session.exec(
            update(Run)
            .where(Run.id.in_(ids), Run.kind == "orchestration", Run.status == "STARTED", Run.claimed_by == ISTANZA)  # type: ignore[union-attr]
            .values(heartbeat_at=_adesso())
        )
        session.commit()
        return vive.rowcount


# since when (time.monotonic) this process's beats have gone through, without a miss
_battito_ok_dal: float | None = None


def battito_sano() -> bool:
    """This process has been beating for a whole `dead_after_seconds`. Right after the
    database comes back from an outage every heartbeat is old, the living ones too:
    the orphans' sweep waits until every live orchestrator has had the time to beat
    again, instead of closing (and now stopping the steps of) runs that are running."""
    return _battito_ok_dal is not None and time.monotonic() - _battito_ok_dal >= _orc().dead_after_seconds


def avvia_battito(ferma: threading.Event) -> threading.Thread:
    """In un THREAD suo e non sul ciclo degli eventi: il battito dice che il
    processo è vivo, e deve continuare anche mentre il ciclo è occupato. Se
    battesse dal ciclo, un'attesa lunga lì dentro farebbe dare per morte
    orchestrazioni che stanno girando."""

    def gira() -> None:
        # a second at a time, reading the interval each time: a beat made shorter from the
        # panel applies at once, not after the long wait already started
        ultimo = time.monotonic()
        while not ferma.wait(1.0):
            if time.monotonic() - ultimo < _orc().heartbeat_seconds:
                continue
            ultimo = time.monotonic()
            global _battito_ok_dal
            try:
                batti()
                if _battito_ok_dal is None:
                    _battito_ok_dal = time.monotonic()
            except Exception:
                _battito_ok_dal = None
                logger.exception("orchestrate: battito non riuscito")

    t = threading.Thread(target=gira, name="battito-orchestrazioni", daemon=True)
    t.start()
    return t


# the dead-after threshold this process used before the last change, and since when
_soglia_prima: tuple[float, float] | None = None
_soglia_ultima: float | None = None


def soglia_morte() -> float:
    """`dead_after_seconds`, but for a while after it is lowered from the panel the old
    one: the other orchestrators read the change up to `RILEGGI_OGNI` seconds later, and
    meanwhile beat at the old pace. Their live runs are not orphans in that window."""
    global _soglia_prima, _soglia_ultima
    from app.core.overrides import RILEGGI_OGNI

    ora, adesso = _orc().dead_after_seconds, time.monotonic()
    if _soglia_ultima is not None and ora != _soglia_ultima:
        _soglia_prima = (_soglia_ultima, adesso)
    _soglia_ultima = ora
    if _soglia_prima is not None and adesso - _soglia_prima[1] < _soglia_prima[0] + RILEGGI_OGNI:
        return max(ora, _soglia_prima[0])
    return ora


async def chiudi_orfane() -> int:
    """Chiude le orchestrazioni STARTED il cui battito è fermo: il processo che le
    eseguiva è morto (crash, riavvio, macchina persa) e nessuno le finirà.
    `_reconcile` non le vede (non hanno un task sull'engine): senza questo
    resterebbero STARTED per sempre, e il flusso non potrebbe più ripartire.

    Vale anche per le righe senza battito, che sono di una versione precedente."""
    taglio = _adesso() - timedelta(seconds=soglia_morte())
    with _vive_lock:
        mie_vive = list(_vive)
    orfana = (
        (Run.kind == "orchestration")
        & (Run.status == "STARTED")
        & or_(Run.heartbeat_at.is_(None), Run.heartbeat_at < taglio)  # type: ignore[union-attr]
        # what this very process is running is alive, whatever its beat says
        & ~((Run.claimed_by == ISTANZA) & Run.id.in_(mie_vive or [-1]))  # type: ignore[union-attr]
    )
    chiuse = 0
    from app.services.notifier import notify_failure

    with Session(engine) as session:
        for run_id in session.exec(select(Run.id).where(orfana)).all():
            # di nuovo la condizione intera: fra la lettura e qui può averla
            # chiusa un altro, o chi la esegue può aver ripreso a battere
            chiusa = session.exec(
                update(Run)
                .where(Run.id == run_id, orfana)
                .values(status="FAILURE", error=ORCHESTRATION_INTERRUPTED, finished_at=datetime.now(timezone.utc))
            )
            if chiusa.rowcount == 1:
                from app.services import webhooks

                webhooks.accoda_per_run(session, session.get(Run, run_id))  # nella stessa transazione
            session.commit()
            if chiusa.rowcount != 1:
                continue
            chiuse += 1
            await ferma_passi(run_id)
            await notify_failure(session, session.get(Run, run_id))
        # e quelle rimaste in coda oltre il tempo massimo: non partono più
        vecchie = _adesso() - timedelta(seconds=_orc().queue_timeout_seconds)
        for run in session.exec(
            select(Run).where(Run.kind == "orchestration", Run.status == "PENDING", Run.started_at < vecchie)
        ).all():
            await close_if_queued_too_long(session, run)
    if chiuse:
        logger.warning("orchestrate: %d orchestrazioni di un processo che non c'è più, chiuse", chiuse)
    return chiuse


def chiudi_le_mie(solo_le_precedenti: bool) -> list[int]:
    """Chiude come interrotte le esecuzioni firmate da questo processo.

    All'AVVIO (`solo_le_precedenti`): quelle di una sua incarnazione precedente —
    stesso host e stesso pid, altra firma. È il container che riparte dopo un
    crash o un kill: quel processo non c'è più di sicuro, e non serve aspettare
    che il battito scada per saperlo (nel frattempo il flusso resterebbe
    bloccato e uno slot schedulato andrebbe perso). Un processo su un altro host,
    o con un altro pid, non si tocca: può essere vivo.

    Allo SPEGNIMENTO: tutte le sue. Quelle con un task le ha già chiuse il task;
    restano le eventuali firmate e mai partite."""
    mie = Run.claimed_by.startswith(ISTANZA.rsplit(":", 1)[0] + ":", autoescape=True) if solo_le_precedenti else Run.claimed_by == ISTANZA  # type: ignore[union-attr]
    altra_firma = Run.claimed_by != ISTANZA if solo_le_precedenti else True
    from app.services import webhooks

    with Session(engine) as session:
        chiuse = session.exec(
            update(Run)
            .where(Run.kind == "orchestration", Run.status == "STARTED", mie, altra_firma)
            .values(status="FAILURE", error=ORCHESTRATION_INTERRUPTED, finished_at=datetime.now(timezone.utc))
            .returning(Run.id)
        ).all()
        for (run_id,) in chiuse:
            webhooks.accoda_per_run(session, session.get(Run, run_id))  # nella stessa transazione
        session.commit()
        return [run_id for (run_id,) in chiuse]


# ── Il lavoratore ────────────────────────────────────────────────────────────
_in_corso: set[asyncio.Task] = set()
# gli id dei run che hanno un task vivo qui: è ciò di cui si batte (vedi `batti`).
# Lo legge il thread del battito, lo scrive il ciclo degli eventi.
_vive: set[int] = set()
_vive_lock = threading.Lock()
# the cells of flow groups running in this process: each holds a connection like an
# orchestration, so they count in the same budget (see services/flow_group_runner.py)
celle_in_corso = 0
_sveglia: asyncio.Event | None = None


def _avvia(run_id: int) -> asyncio.Task:
    with _vive_lock:
        _vive.add(run_id)
    task = asyncio.create_task(esegui(run_id))
    _in_corso.add(task)

    def finito(t: asyncio.Task) -> None:
        _in_corso.discard(t)
        with _vive_lock:
            _vive.discard(run_id)
        if not t.cancelled() and t.exception() is not None:
            # non è riuscito nemmeno a scrivere l'esito (database irraggiungibile):
            # da adesso non batte più, e verrà chiuso come orfano
            logger.error("orchestrate: run %s, il task è morto senza chiudere il run: %r", run_id, t.exception())

    task.add_done_callback(finito)
    return task


def sveglia() -> None:
    """Chi ha appena messo in coda un'esecuzione lo dice al lavoratore di questo
    processo, se c'è: la prende subito invece che al prossimo giro. In un
    processo che risponde soltanto (`APP__ROLE=api`) non fa niente, e la riga la
    trova un orchestratore entro `ORCHESTRATOR__QUEUE_POLL_SECONDS`."""
    if _sveglia is not None:
        _sveglia.set()


async def coda_loop(stop: asyncio.Event) -> None:
    """Prende dalla coda ed esegue, finché il processo vive."""
    global _sveglia
    _sveglia = asyncio.Event()
    ferma_battito = threading.Event()
    avvia_battito(ferma_battito)
    logger.info(
        "orchestratore %s avviato: coda ogni %ss, fino a %d esecuzioni insieme",
        ISTANZA, _orc().queue_poll_seconds, concorrenza(),
    )
    ultima_ricerca = 0.0
    try:
        precedenti = await asyncio.to_thread(chiudi_le_mie, True)
        if precedenti:
            logger.warning("orchestratore: %d esecuzioni di un avvio precedente di questo processo, chiuse",
                           len(precedenti))
        for run_id in precedenti:
            await ferma_passi(run_id)
    except Exception:
        logger.exception("orchestratore: chiusura delle esecuzioni precedenti non riuscita")
    try:
        while not stop.is_set():
            _sveglia.clear()
            try:
                liberi = concorrenza() - len(_in_corso) - celle_in_corso
                if liberi > 0:
                    prese, respinte = await asyncio.to_thread(rivendica, liberi)
                    for run_id in prese:
                        _avvia(run_id)
                    for run_id in respinte:
                        await _finalize_orch_run(run_id, "FAILURE", ALREADY_RUNNING, solo_in_coda=True)
                if time.monotonic() - ultima_ricerca >= ORPHAN_SWEEP_S and battito_sano():
                    ultima_ricerca = time.monotonic()
                    await chiudi_orfane()
            except Exception:
                logger.exception("orchestratore: errore nel giro della coda")
            try:
                await asyncio.wait_for(_sveglia.wait(), timeout=_orc().queue_poll_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        _sveglia = None
        ferma_battito.set()
        # il processo si ferma: le esecuzioni in corso non possono finire, e
        # ciascuna lo scrive sul suo run (vedi `esegui`)
        for task in list(_in_corso):
            task.cancel()
        await asyncio.gather(*list(_in_corso), return_exceptions=True)
        try:
            chiudi_le_mie(False)  # firmate e mai partite: nessuno le eseguirà
        except Exception:
            logger.exception("orchestratore: chiusura finale non riuscita")
        logger.info("orchestratore %s fermato", ISTANZA)
