"""Chi ha un flusso aperto nell'editor, adesso.

Due persone possono aprire lo stesso flusso e l'ULTIMO che salva sovrascrive
l'altro, in silenzio (lo storico delle versioni conserva il lavoro, ma bisogna
sapere di doverlo cercare). Per scelta non si blocca e non si fondono le
modifiche: si AVVISA. L'editor dice "sono qui" ogni pochi secondi e riceve in
risposta chi altro c'e'; chi smette di dirlo — scheda chiusa, rete caduta —
scade da solo.

In MEMORIA e non in un database: e' uno stato effimero per definizione, e il
gateway e' un singleton in un solo processo (una replica, strategia Recreate,
uvicorn senza --workers). Se un giorno i processi diventassero piu' d'uno,
questo registro va spostato in uno store condiviso: ognuno vedrebbe solo i suoi.
Un riavvio lo azzera, e va bene cosi': entro un battito si ripopola.
"""
import threading
import time
from dataclasses import dataclass

# un'istanza che non si fa sentire da tanto e' considerata chiusa; l'editor
# batte ogni HEARTBEAT_SECONDS, quindi si tollerano due battiti persi
HEARTBEAT_SECONDS = 15
TTL_SECONDS = 40
_MAX_INSTANCES_PER_FLOW = 50  # tetto di sicurezza: nessuno riempie la memoria


@dataclass
class _Entry:
    user_id: int
    email: str
    full_name: str
    since: float
    seen: float


_lock = threading.Lock()
_open: dict[int, dict[str, _Entry]] = {}


def _prune(flow_id: int, now: float) -> dict[str, _Entry]:
    entries = _open.get(flow_id, {})
    for key in [k for k, e in entries.items() if now - e.seen > TTL_SECONDS]:
        entries.pop(key, None)
    if not entries:
        _open.pop(flow_id, None)
    return entries


def beat(flow_id: int, instance: str, user_id: int, email: str, full_name: str, now: float | None = None) -> list[dict]:
    """Registra il battito di `instance` e restituisce le ALTRE istanze aperte
    sullo stesso flusso (anche dello stesso utente: una seconda scheda sovrascrive
    quanto una seconda persona)."""
    now = time.time() if now is None else now
    with _lock:
        entries = _prune(flow_id, now)
        mine = entries.get(instance)
        if mine is None:
            if len(entries) >= _MAX_INSTANCES_PER_FLOW:
                return _others(entries, instance)
            entries[instance] = _Entry(user_id, email, full_name, since=now, seen=now)
            _open[flow_id] = entries
        else:
            mine.seen = now
        return _others(entries, instance)


def _others(entries: dict[str, _Entry], instance: str) -> list[dict]:
    out = [
        {"user_id": e.user_id, "email": e.email, "full_name": e.full_name, "since": e.since}
        for k, e in entries.items() if k != instance
    ]
    return sorted(out, key=lambda o: o["since"])


def leave(flow_id: int, instance: str) -> None:
    with _lock:
        entries = _open.get(flow_id)
        if entries:
            entries.pop(instance, None)
            if not entries:
                _open.pop(flow_id, None)


def reset() -> None:
    """Solo per i test."""
    with _lock:
        _open.clear()
