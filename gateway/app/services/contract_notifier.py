"""Avvisa quando lo stato di un data contract cambia.

Chi registra un referto (un refresh, una pubblicazione, il controllo della
freschezza) segna soltanto che un avviso è dovuto (`DataContractResult.notify =
'pending'`, vedi `contracts.record_result`); qui lo si manda. Così nessuna email
parte dentro la transazione che pubblica i dati, e l'avviso sopravvive a un
riavvio fra il referto e la spedizione.

Valgono le regole dell'avviso di fallimento dei flussi (`services.notifier`):
solo al cambio di stato — lo decide chi registra il referto — e non solleva mai.
Con più processi ogni avviso è di chi lo prende per primo; se quel processo muore
a metà l'avviso resta «sending» e non riparte: meglio un'email in meno che due.
"""
from __future__ import annotations

import json
import logging
import time

from sqlalchemy import update
from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.models import Connection, DataContractResult, Datasource
from app.routes.connections import engine_connection_payload
from app.services import audit, contracts

logger = logging.getLogger(__name__)

MAX_RULES_LISTED = 10
# Il giro dello scheduler lancia anche i flussi programmati: un server di posta
# che non risponde (30 s a tentativo) non deve tenerlo fermo per minuti. Oltre
# questo tempo gli avvisi rimasti aspettano il giro dopo.
BUDGET_SECONDS = 30.0

_TITOLO = {
    "refused": "aggiornamento rifiutato",
    "failed": "contratto violato",
    "warning": "regole non rispettate (avviso)",
    "passed": "di nuovo rispettato",
}
_FRASE = {
    "refused": "Un aggiornamento violava una regola bloccante e NON è stato pubblicato: la datasource serve ancora i dati precedenti.",
    "failed": "I dati che la datasource serve non rispettano una regola bloccante.",
    "warning": "I dati che la datasource serve non rispettano alcune regole non bloccanti.",
    "passed": "Tutte le regole sono di nuovo rispettate.",
}
_ORIGINE = {
    "refresh": "refresh della datasource",
    "publish": "esecuzione di un flusso",
    "freshness": "freschezza (il tempo passato dall'ultimo aggiornamento)",
}


def _riga(x: dict) -> str:
    dove = x.get("column") or ", ".join(x.get("columns") or []) or x.get("name") or ""
    testa = f"[{'avviso' if x.get('severity') == 'warning' else 'bloccante'}] {x.get('kind')}" + (f" su {dove}" if dove else "")
    if x.get("error"):
        return f"{testa}: non valutabile ({str(x['error'])[:200]})"
    if x.get("violations"):
        return f"{testa}: {x['violations']} righe"
    if x.get("observed") is not None:
        return f"{testa}: osservato {x['observed']}"
    return testa


def messaggio(ds: Datasource, r: DataContractResult) -> tuple[str, str]:
    """(oggetto, corpo) dell'avviso per questa valutazione."""
    stato = "refused" if r.blocked else r.outcome
    try:
        report = json.loads(r.report)
    except json.JSONDecodeError:
        report = {}
    rotte = [x for x in report.get("rules", []) if not x.get("passed")]
    righe = [
        f"Data contract della datasource «{ds.name}»: {_TITOLO.get(stato, stato)}.",
        "",
        _FRASE.get(stato, ""),
        "",
        f"Quando:    {r.evaluated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"Origine:   {_ORIGINE.get(r.trigger, r.trigger)}" + (f" (esecuzione #{r.run_id})" if r.run_id else ""),
        f"Contratto: versione {r.contract_version}",
    ]
    if r.rows is not None:
        righe.append(f"Righe:     {r.rows}")
    if rotte:
        righe += ["", "Regole non rispettate:"] + [f"  - {_riga(x)}" for x in rotte[:MAX_RULES_LISTED]]
        if len(rotte) > MAX_RULES_LISTED:
            righe.append(f"  … e altre {len(rotte) - MAX_RULES_LISTED}")
    elif report.get("error"):
        righe += ["", f"Il contratto non si è potuto valutare: {str(report['error'])[:300]}"]
    righe += ["", "—", "Messaggio automatico di Tabularia. Per non riceverlo più, togli gli indirizzi dalle notifiche del data contract."]
    return f"[Tabularia] Data contract di «{ds.name}»: {_TITOLO.get(stato, stato)}", "\n".join(righe)


async def _manda(session: Session, result_id: int) -> str:
    """Spedisce l'avviso di una valutazione: 'sent' | 'failed' | 'skipped'."""
    r = session.get(DataContractResult, result_id)
    ds = session.get(Datasource, r.datasource_id) if r else None
    c = contracts.get(session, r.datasource_id) if r else None
    if ds is None or c is None:
        return "skipped"  # datasource o contratto tolti nel frattempo
    a = [x.strip() for x in (c.notify_emails or "").split(",") if x.strip()]
    conn = session.get(Connection, c.notify_connection_id) if c.notify_connection_id else None
    if not a or conn is None or conn.db_type != "smtp":
        return "skipped"
    oggetto, corpo = messaggio(ds, r)
    resp = await get_engine_client().post(
        "/db/notify", json={"connection": engine_connection_payload(conn), "to": a, "subject": oggetto, "body": corpo}, timeout=60,
    )
    ok = resp.status_code < 400
    if not ok:
        logger.warning("avviso del data contract della datasource %s rifiutato dall'engine: %s", ds.id, resp.text[:300])
    audit.record_audit(
        session, actor=None, actor_label="scheduler", action=audit.EMAIL_SEND, outcome="success" if ok else "failure",
        target_type="datasource", target_id=ds.id, target_label=ds.name,
        detail={"reason": "contract_state", "state": "refused" if r.blocked else r.outcome, "result_id": r.id, "recipients": len(a)},
    )
    return "sent" if ok else "failed"


def _segna(session: Session, result_id: int, da: str | None, a: str) -> bool:
    cond = [DataContractResult.id == result_id] + ([DataContractResult.notify == da] if da else [])
    fatto = session.exec(update(DataContractResult).where(*cond).values(notify=a))  # type: ignore[call-overload]
    session.commit()
    return fatto.rowcount == 1


async def deliver_pending(session: Session, limit: int = 20, budget_seconds: float = BUDGET_SECONDS) -> int:
    """Manda gli avvisi dovuti. Lo chiama lo scheduler a ogni giro. Torna quanti
    ne sono partiti. Non solleva."""
    mandati, inizio = 0, time.monotonic()
    try:
        ids = session.exec(
            select(DataContractResult.id).where(DataContractResult.notify == "pending").order_by(DataContractResult.id).limit(limit)
        ).all()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("avvisi dei data contract: coda non leggibile")
        return 0
    for rid in ids:
        if time.monotonic() - inizio > budget_seconds:
            logger.warning("avvisi dei data contract: tempo del giro esaurito, gli altri al prossimo")
            break
        try:
            if not _segna(session, rid, "pending", "sending"):
                continue  # un altro processo l'ha preso
            esito = await _manda(session, rid)
        except Exception:  # noqa: BLE001 — un avviso rotto non ferma gli altri né lo scheduler
            session.rollback()
            logger.exception("avviso del data contract non inviato (valutazione %s)", rid)
            esito = "failed"
        try:
            _segna(session, rid, None, esito)
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("avviso del data contract: esito non registrato (valutazione %s)", rid)
        mandati += esito == "sent"
    return mandati
