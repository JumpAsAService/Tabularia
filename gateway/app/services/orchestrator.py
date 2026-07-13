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

Usato sia dallo scheduler sia dal run manuale (`POST /flows/{id}/run-now`), come
task di background per non bloccare. Traccia l'intera esecuzione in un 'run di
orchestrazione' (kind=orchestration) osservabile in cronologia. Con l'autorità di
`user`, RBAC ri-verificata a ogni passo (RUN/CONNECT per il refresh, RUN + EDIT/
CONNECT per gli output).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.core.config import get_settings
from app.db.session import engine
from app.models import Connection, Datasource, Flow, Run, User
from app.models.permission import Capability
from app.models.run import TERMINAL_STATES
from app.routes.runs import _launch_flow_run, _reconcile, launch_ingest_run
from app.schemas.models import RunCreate
from app.services import permissions as perm_service
from app.services.flow_resolver import FlowResolveError, resolve_output_request, sequence_order

logger = logging.getLogger(__name__)

MAX_FLOW_DEPTH = 5  # guardia sui runflow annidati (oltre al set anti-ciclo)
REFRESH_WAIT_TICKS = 200  # * intervallo = tetto d'attesa di un refresh
REFRESH_WAIT_INTERVAL_S = 3
OUTPUT_WAIT_TICKS = 200  # idem per il completamento di un output (l'ordine è reale
OUTPUT_WAIT_INTERVAL_S = 3  # solo se ogni passo ATTENDE la fine del precedente)

# flussi con un'orchestrazione in corso: evita sovrapposizioni (stesso flow_id)
_running: set[int] = set()


class OrchestrationError(RuntimeError):
    pass


async def _refresh_and_wait(session: Session, user: User, ds_id: int) -> None:
    """Aggiorna una datasource database e attende (bounded) il SUCCESS."""
    ds = session.get(Datasource, ds_id)
    if ds is None or ds.kind != "database":
        return  # non è una datasource DB (nulla da refreshare)
    if not perm_service.has_capability(session, user, ds.project_id, Capability.RUN):
        raise OrchestrationError(f"RUN mancante per il refresh della datasource {ds_id}")
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        raise OrchestrationError(f"datasource {ds_id}: connessione inesistente")
    if not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        raise OrchestrationError(f"CONNECT mancante per il refresh della datasource {ds_id}")

    # se c'è già un refresh in corso lo si attende, altrimenti se ne lancia uno
    last = session.exec(
        select(Run)
        .where(Run.datasource_id == ds.id, Run.kind == "ingest")
        .order_by(Run.started_at.desc())
    ).first()
    if last is not None:
        last = await _reconcile(session, last)
    run = last if (last is not None and last.status not in TERMINAL_STATES) else await launch_ingest_run(session, user, ds, conn)

    for _ in range(REFRESH_WAIT_TICKS):
        run = await _reconcile(session, run)
        if run.status in TERMINAL_STATES:
            break
        await asyncio.sleep(REFRESH_WAIT_INTERVAL_S)
    if run.status != "SUCCESS":
        raise OrchestrationError(f"refresh datasource {ds_id} non riuscito ({run.status})")
    logger.info("orchestrate: datasource %s aggiornata (refresh completato)", ds_id)


async def _wait_run(session: Session, run: Run) -> Run:
    """Attende (bounded) che un run di output raggiunga uno stato terminale, così
    che i passi successivi dell'ordine vedano davvero il risultato già scritto
    (senza attesa, l'ordinamento sarebbe solo cosmetico: i passi corrono in
    parallelo e un output 'a valle' leggerebbe dati non ancora prodotti)."""
    for _ in range(OUTPUT_WAIT_TICKS):
        run = await _reconcile(session, run)
        if run.status in TERMINAL_STATES:
            break
        await asyncio.sleep(OUTPUT_WAIT_INTERVAL_S)
    return run


async def orchestrate(session: Session, user: User, flow: Flow, depth: int = 0, seen: set[int] | None = None) -> list[str]:
    """Esegue un flusso: gli 'action node' (refresh/output/runflow) nell'ordine
    definito dagli archi di sequenza (topological sort), coi non collegati
    nell'ordine di default refresh → output → runflow.

    Un refresh fallito SOLLEVA (interrompe tutto: meglio niente che dati stantii);
    un output o un sotto-flusso in errore vengono raccolti e si prosegue. Torna la
    lista degli errori non fatali (vuota = tutto ok) per marcare il run di
    orchestrazione."""
    seen = seen or set()
    if flow.id in seen or depth > MAX_FLOW_DEPTH:
        logger.warning("orchestrate: ciclo o profondità eccessiva su flusso %s (depth %d)", flow.id, depth)
        return [f"flusso {flow.id}: ciclo o profondità eccessiva"]
    seen = seen | {flow.id}

    definition = json.loads(flow.definition or "{}")
    default_bucket = get_settings().engine.bucket
    errors: list[str] = []

    def resolve_ds(i: int):
        d = session.get(Datasource, i)
        return (d.bucket, d.key) if d and d.key else None

    for node in sequence_order(definition):
        t = node.get("type")
        d = node.get("data") or {}
        if t == "refresh":
            ds_id = d.get("datasourceId")
            if ds_id:
                await _refresh_and_wait(session, user, ds_id)  # errore → propaga (aborta tutto)
        elif t == "output":
            try:
                req = resolve_output_request(definition, node, resolve_ds, default_bucket)
                run = await _launch_flow_run(session, user, flow, RunCreate(**req))
                run = await _wait_run(session, run)  # attende: l'ordine dev'essere reale
                if run.status != "SUCCESS":
                    errors.append(f"output: run {run.id} {run.status} — {run.error or ''}".strip())
            except Exception as e:
                logger.warning("orchestrate: flusso %s, output non eseguito: %s", flow.id, e)
                errors.append(f"output: {e}")
        elif t == "runflow":
            sub_id = d.get("flowId")
            if not sub_id:
                continue
            sub = session.get(Flow, sub_id)
            if sub is None:
                logger.warning("orchestrate: flusso %s riferisce un runflow inesistente %s", flow.id, sub_id)
                errors.append(f"runflow: flusso {sub_id} inesistente")
                continue
            errors.extend(await orchestrate(session, user, sub, depth + 1, seen))
    return errors


def create_orchestration_run(session: Session, user: User, flow: Flow) -> Run:
    """Crea la riga 'run di orchestrazione' (kind=orchestration) in stato STARTED:
    è il TRACCIANTE dell'intera esecuzione del flusso (anche di flussi senza nodo
    Output, che altrimenti non lascerebbero traccia). Non ha un task Celery: lo
    stato lo gestisce l'orchestratore, `_reconcile` la salta. La si crea nella
    sessione della richiesta così che il frontend possa pollarla subito per id."""
    run = Run(
        kind="orchestration",
        flow_id=flow.id,
        task_id="",
        status="STARTED",
        launched_by=user.id,
        input_key="",
        output_bucket="",
        output_key="",
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _finalize_orch_run(run_id: int, status: str, error: str | None = None) -> None:
    """Chiude il run di orchestrazione (sessione propria: gira nel task detached)."""
    with Session(engine) as session:
        run = session.get(Run, run_id)
        if run is None or run.status in TERMINAL_STATES:
            return
        run.status = status
        run.error = error[:2000] if error else None
        run.finished_at = datetime.now(timezone.utc)
        session.add(run)
        session.commit()


async def orchestrate_bg(flow_id: int, user_id: int, orch_run_id: int | None = None) -> None:
    """Entry point come task di background (scheduler o run-now): sessione propria,
    guardia anti-sovrapposizione, esito registrato sul run di orchestrazione.

    `orch_run_id` è passato da run-now (creato nella sessione della richiesta per
    tornarlo subito al frontend); lo scheduler non lo passa e lo si crea qui."""
    if flow_id in _running:
        logger.info("orchestrate: flusso %s già in esecuzione, salto", flow_id)
        if orch_run_id is not None:
            _finalize_orch_run(orch_run_id, "FAILURE", "flusso già in esecuzione")
        return
    _running.add(flow_id)
    try:
        with Session(engine) as session:
            flow = session.get(Flow, flow_id)
            user = session.get(User, user_id)
            if flow is None or user is None or not user.is_active:
                logger.warning("orchestrate: flusso %s o utente %s non validi", flow_id, user_id)
                if orch_run_id is not None:
                    _finalize_orch_run(orch_run_id, "FAILURE", "flusso o utente non validi")
                return
            if orch_run_id is None:  # percorso scheduler: nessun tracciante ancora
                orch_run_id = create_orchestration_run(session, user, flow).id
            try:
                errors = await orchestrate(session, user, flow)
            except (FlowResolveError, OrchestrationError) as e:
                logger.warning("orchestrate: flusso %s interrotto: %s", flow_id, e)
                _finalize_orch_run(orch_run_id, "FAILURE", str(e))
                return
            status = "FAILURE" if errors else "SUCCESS"
            _finalize_orch_run(orch_run_id, status, "; ".join(errors) if errors else None)
            logger.info("orchestrate: flusso %s completato (%s)", flow_id, status)
    except Exception:
        logger.exception("orchestrate: flusso %s fallito", flow_id)
        if orch_run_id is not None:
            _finalize_orch_run(orch_run_id, "FAILURE", "errore interno durante l'orchestrazione")
    finally:
        _running.discard(flow_id)
