"""Chi ha un flusso aperto nell'editor, adesso.

Due persone possono aprire lo stesso flusso e l'ULTIMO che salva sovrascrive
l'altro, in silenzio (lo storico delle versioni conserva il lavoro, ma bisogna
sapere di doverlo cercare). Per scelta non si blocca e non si fondono le
modifiche: si AVVISA. L'editor dice "sono qui" ogni pochi secondi e riceve in
risposta chi altro c'e'; chi smette di dirlo — scheda chiusa, rete caduta —
scade da solo.

In una TABELLA (`flow_presence`) e non in memoria: con più repliche del gateway
due persone sullo stesso flusso parlerebbero con processi diversi, e ognuno
vedrebbe solo chi è passato da lui — l'avviso mancherebbe proprio quando serve.
Un battito è un UPDATE per chiave e una lettura delle altre righe del flusso;
quelle scadute non si leggono (si filtra sull'ultimo battito) e le toglie lo
scheduler.
"""
import time

from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.shared_state import FlowPresence

# un'istanza che non si fa sentire da tanto e' considerata chiusa; l'editor
# batte ogni HEARTBEAT_SECONDS, quindi si tollerano due battiti persi
HEARTBEAT_SECONDS = 15
TTL_SECONDS = 40
_MAX_INSTANCES_PER_FLOW = 50  # tetto di sicurezza: nessuno riempie la tabella


def beat(session: Session, flow_id: int, instance: str, user_id: int, email: str, full_name: str, now: float | None = None) -> list[dict]:
    """Registra il battito di `instance` e restituisce le ALTRE istanze aperte
    sullo stesso flusso (anche dello stesso utente: una seconda scheda sovrascrive
    quanto una seconda persona)."""
    now = time.time() if now is None else now
    vive = (FlowPresence.flow_id == flow_id) & (FlowPresence.seen >= now - TTL_SECONDS)
    battuto = session.exec(
        update(FlowPresence).where(vive, FlowPresence.instance == instance).values(seen=now)
    )
    if battuto.rowcount == 0:
        # prima volta (o tornata dopo essere scaduta: `since` riparte)
        session.exec(delete(FlowPresence).where(FlowPresence.flow_id == flow_id, FlowPresence.instance == instance))
        quante = session.exec(select(func.count()).select_from(FlowPresence).where(vive)).one()
        if quante < _MAX_INSTANCES_PER_FLOW:
            session.add(FlowPresence(flow_id=flow_id, instance=instance, user_id=user_id, email=email, full_name=full_name, since=now, seen=now))
    try:
        session.commit()
    except IntegrityError:  # la stessa scheda ha battuto due volte insieme: c'è già
        session.rollback()
    altre = session.exec(
        select(FlowPresence).where(vive, FlowPresence.instance != instance).order_by(FlowPresence.since)
    ).all()
    out = [{"user_id": e.user_id, "email": e.email, "full_name": e.full_name, "since": e.since} for e in altre]
    session.rollback()  # lettura finita: la connessione torna al pool
    return out


def leave(session: Session, flow_id: int, instance: str) -> None:
    session.exec(delete(FlowPresence).where(FlowPresence.flow_id == flow_id, FlowPresence.instance == instance))
    session.commit()


def pulisci(session: Session, now: float | None = None) -> int:
    """Toglie le schede che non battono più (lo chiama lo scheduler)."""
    now = time.time() if now is None else now
    tolte = session.exec(delete(FlowPresence).where(FlowPresence.seen < now - TTL_SECONDS))
    session.commit()
    return tolte.rowcount or 0
