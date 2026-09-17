"""Slot delle preview: in ogni slot conta solo l'ULTIMA richiesta.

L'utente che disegna un flusso clicca un nodo dopo l'altro; ogni click lancia
una o piu' preview e quelle dei nodi gia' abbandonati restavano in coda davanti
all'unica che interessa. Il client dichiara a quale slot appartiene la preview
("questo editor, pannello anteprima"): una nuova preview sullo stesso slot
butta giu' la precedente, in modo ESPLICITO. Non ci si affida alla propagazione
della disconnessione HTTP: fra browser e worker ci sono ingress, gateway e una
route sincrona, e nessuno dei tre la garantisce.

Tutto fail-open: se Valkey non risponde, la preview gira come se lo slot non ci
fosse. Un registro di cortesia non deve poter rompere l'anteprima.
"""
import logging

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)
_PREFIX = "dataprep:preview:slot:"
# compare-and-delete: si libera lo slot solo se e' ancora NOSTRO
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_client: redis.Redis | None = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(get_settings().redis.url, decode_responses=True)
    return _client


def claim(slot: str, task_id: str, ttl_seconds: int) -> str | None:
    """Prende lo slot per `task_id` e restituisce chi lo occupava (o None).
    Atomico (SET … GET): due richieste simultanee non si perdono a vicenda."""
    try:
        return _r().set(_PREFIX + slot, task_id, ex=ttl_seconds, get=True)
    except Exception as e:
        logger.warning("slot preview non disponibile (%s): procedo senza", e)
        return None


def owner(slot: str) -> str | None:
    try:
        return _r().get(_PREFIX + slot)
    except Exception:
        return None


def release(slot: str, task_id: str) -> None:
    try:
        _r().eval(_RELEASE, 1, _PREFIX + slot, task_id)
    except Exception:
        pass
