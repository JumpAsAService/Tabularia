"""Avvisa quando un'esecuzione programmata fallisce.

Il prodotto dice «metti in schedule e dimentica», ma finora dimenticare era
l'unica opzione: un flusso che falliva alle tre di notte restava fallito in
silenzio, e lo si scopriva aprendo la cronologia o accorgendosi che i dati erano
vecchi. Questo modulo è la seconda metà della promessa.

Tre regole che valgono la pena di essere dette:

- **Solo le esecuzioni programmate.** Chi lancia a mano sta già guardando lo
  schermo; avvisarlo sarebbe rumore, e il rumore fa ignorare anche gli avvisi
  veri.
- **Solo al PASSAGGIO a fallimento.** Un flusso rotto che scatta ogni cinque
  minuti manderebbe trecento email al giorno, e alla terza nessuno le leggerebbe
  più. Si avvisa quando l'esecuzione precedente era andata bene.
- **Non solleva mai.** Un avviso che fallisce non deve cambiare l'esito di
  niente: il run è già fallito, e un errore qui lo renderebbe soltanto più
  difficile da capire.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.models import Connection, Flow, Run
from app.models.run import TERMINAL_STATES
from app.services import audit
from app.routes.connections import engine_connection_payload

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 1200


def destinatari(flow: Flow) -> list[str]:
    grezzi = (flow.notify_emails or "").replace(";", ",").split(",")
    return [a.strip() for a in grezzi if a.strip()]


def _gia_falliva(session: Session, flow_id: int, run_id: int) -> bool:
    """L'esecuzione PRECEDENTE dello stesso flusso era già fallita?

    Se sì si tace: il primo avviso ha già detto quello che c'era da dire, e
    ripeterlo a ogni scatto trasforma la notifica in rumore da filtrare."""
    precedente = session.exec(
        select(Run)
        .where(Run.flow_id == flow_id, Run.kind == "flow", Run.parent_run_id.is_(None), Run.id != run_id)
        .order_by(Run.started_at.desc())
    ).first()
    return precedente is not None and precedente.status == "FAILURE"


def _corpo(flow: Flow, run: Run) -> str:
    quando = (run.finished_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    righe = [
        f"Il flusso «{flow.name}» non è andato a buon fine.",
        "",
        f"Quando:    {quando}",
        f"Esecuzione: #{run.id}",
    ]
    if run.engine:
        righe.append(f"Motore:    {run.engine}")
    if run.error:
        righe += ["", "Errore:", run.error[:MAX_ERROR_CHARS]]
    righe += ["", "—", "Messaggio automatico di Tabularia. Per non riceverlo più, togli gli indirizzi dalle notifiche del flusso."]
    return "\n".join(righe)


async def notify_failure(session: Session, run: Run) -> None:
    """Manda l'avviso, se il flusso lo ha chiesto e le condizioni ci sono."""
    try:
        if run.kind != "flow" or run.status != "FAILURE" or run.trigger_type != "schedule":
            return
        flow = session.get(Flow, run.flow_id) if run.flow_id else None
        if flow is None:
            return
        a = destinatari(flow)
        if not a or not flow.notify_connection_id:
            return
        conn = session.get(Connection, flow.notify_connection_id)
        if conn is None or conn.db_type != "smtp":
            logger.info("avviso per il flusso %s saltato: connessione SMTP assente", flow.id)
            return
        if _gia_falliva(session, flow.id, run.id):
            logger.info("avviso per il flusso %s saltato: falliva già", flow.id)
            return

        payload = {
            "connection": engine_connection_payload(conn),
            "to": a,
            "subject": f"[Tabularia] Flusso «{flow.name}» fallito",
            "body": _corpo(flow, run),
        }
        resp = await get_engine_client().post("/db/notify", json=payload, timeout=60)
        ok = resp.status_code < 400
        if not ok:
            logger.warning("avviso per il flusso %s rifiutato dall'engine: %s", flow.id, resp.text[:300])
        audit.record_audit(
            session, actor=None, actor_label="scheduler", action=audit.EMAIL_SEND,
            outcome="success" if ok else "failure",
            target_type="flow", target_id=flow.id, target_label=flow.name,
            detail={"reason": "run_failed", "run_id": run.id, "recipients": len(a)},
        )
    except Exception:  # noqa: BLE001 — il run è già fallito: un avviso rotto non deve peggiorarlo
        logger.exception("avviso di fallimento non inviato (run %s)", getattr(run, "id", "?"))
