"""Orchestrazione di un run di flusso: interpreta i NODI DI CONTROLLO del canvas
(non sono operazioni dell'engine, come i nodi Output) in un ordine fisso:

  1. refresh   — aggiorna le datasource dei nodi `refresh` e ASPETTA che finiscano
                 (così gli output girano su dati freschi; se un refresh fallisce
                 il run si ferma: meglio niente che dati stantii/parziali);
  2. output    — lancia i nodi Output di questo flusso (via flow_resolver);
  3. runflow   — esegue i flussi referenziati dai nodi `runflow` (guardia
                 anti-ciclo/profondità).

Usato sia dallo scheduler sia dal run manuale (`POST /flows/{id}/run-now`), come
task di background per non bloccare (un refresh può durare). Con l'autorità di
`user`, RBAC ri-verificata a ogni passo (RUN/CONNECT per il refresh, RUN + EDIT/
CONNECT per gli output).
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlmodel import Session, select

from app.core.config import get_settings
from app.db.session import engine
from app.models import Connection, Datasource, Flow, Run, User
from app.models.permission import Capability
from app.models.run import TERMINAL_STATES
from app.routes.runs import _launch_flow_run, _reconcile, launch_ingest_run
from app.schemas.models import RunCreate
from app.services import permissions as perm_service
from app.services.flow_resolver import FlowResolveError, build_output_run_requests

logger = logging.getLogger(__name__)

MAX_FLOW_DEPTH = 5  # guardia sui runflow annidati (oltre al set anti-ciclo)
REFRESH_WAIT_TICKS = 200  # * intervallo = tetto d'attesa di un refresh
REFRESH_WAIT_INTERVAL_S = 3

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
    logger.info("orchestrate: datasource %s aggiornata prima del run", ds_id)


async def orchestrate(session: Session, user: User, flow: Flow, depth: int = 0, seen: set[int] | None = None) -> None:
    """Esegue un flusso: refresh → output → runflow (ricorsivo)."""
    seen = seen or set()
    if flow.id in seen or depth > MAX_FLOW_DEPTH:
        logger.warning("orchestrate: ciclo o profondità eccessiva su flusso %s (depth %d)", flow.id, depth)
        return
    seen = seen | {flow.id}

    definition = json.loads(flow.definition or "{}")
    nodes = definition.get("nodes") or []

    # 1. refresh delle sorgenti richieste (attende il completamento)
    for n in nodes:
        if n.get("type") == "refresh":
            ds_id = (n.get("data") or {}).get("datasourceId")
            if ds_id:
                await _refresh_and_wait(session, user, ds_id)

    # 2. output di questo flusso (se ne ha)
    if any(n.get("type") == "output" for n in nodes):
        def resolve_ds(i: int):
            d = session.get(Datasource, i)
            return (d.bucket, d.key) if d and d.key else None

        requests = build_output_run_requests(definition, resolve_ds, get_settings().engine.bucket)
        for req in requests:
            await _launch_flow_run(session, user, flow, RunCreate(**req))

    # 3. flussi da eseguire a valle
    for n in nodes:
        if n.get("type") == "runflow":
            sub_id = (n.get("data") or {}).get("flowId")
            if sub_id:
                sub = session.get(Flow, sub_id)
                if sub is None:
                    logger.warning("orchestrate: flusso %s riferisce un runflow inesistente %s", flow.id, sub_id)
                    continue
                await orchestrate(session, user, sub, depth + 1, seen)


async def orchestrate_bg(flow_id: int, user_id: int) -> None:
    """Entry point come task di background (scheduler o run-now): sessione propria,
    guardia anti-sovrapposizione, errori catturati (il run resta visibile a parte)."""
    if flow_id in _running:
        logger.info("orchestrate: flusso %s già in esecuzione, salto", flow_id)
        return
    _running.add(flow_id)
    try:
        with Session(engine) as session:
            flow = session.get(Flow, flow_id)
            user = session.get(User, user_id)
            if flow is None or user is None or not user.is_active:
                logger.warning("orchestrate: flusso %s o utente %s non validi", flow_id, user_id)
                return
            await orchestrate(session, user, flow)
            logger.info("orchestrate: flusso %s completato", flow_id)
    except FlowResolveError as e:
        logger.warning("orchestrate: flusso %s non eseguibile: %s", flow_id, e)
    except Exception:
        logger.exception("orchestrate: flusso %s fallito", flow_id)
    finally:
        _running.discard(flow_id)
