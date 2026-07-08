"""
Cache incrementale degli step del flow.

Idea: l'output di un nodo è una funzione deterministica della sorgente + delle
operazioni fino a lì. Ne calcoliamo un hash progressivo (rolling): cambiando
un'operazione cambia il suo hash e quelli a valle, ma quelli a monte restano
stabili. Così possiamo materializzare l'output di uno step in
`cache/<hash>.parquet` e, la volta dopo, ripartire dall'antenato già in cache
invece che dalla sorgente.

Strutture su Valkey (tenute SEMPRE sincronizzate tra loro e con lo storage):
- SET  `dataprep:stepcache`        → hash materializzati (indice di presenza).
- ZSET `dataprep:stepcache:atime`  → hash → timestamp di ultimo accesso (per il TTL).

I blob parquet stanno nello stesso storage S3, sotto `cache/`.

La cache è "best-effort": se Valkey non risponde, si comporta come cache vuota e
il flow viene ricalcolato normalmente (nessun errore all'utente).

Content-addressed ⇒ nessuna invalidazione: se cambi un parametro cambia l'hash.
Le voci non più accedute vengono rimosse dall'eviction TTL (`evict_expired`),
che cancella insieme blob + SET + ZSET, così indice e storage restano allineati.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CACHE_PREFIX = "cache"  # prefisso delle chiavi parquet nello storage
INDEX_SET = "dataprep:stepcache"  # SET Valkey con gli hash materializzati
ATIME_ZSET = "dataprep:stepcache:atime"  # ZSET hash -> ultimo accesso (unix ts)
HITS_KEY = "dataprep:metrics:cache_hits"  # contatore riusi (per le metriche)
MISSES_KEY = "dataprep:metrics:cache_misses"  # contatore miss (per le metriche)


def _canonical(op: dict) -> str:
    """Serializzazione stabile di un'operazione (chiavi ordinate) per l'hash."""
    return json.dumps(op, sort_keys=True, separators=(",", ":"))


def plan_hashes(source_id: str, operations: list[dict]) -> list[str]:
    """
    Hash progressivo della catena.

    `hashes[i]` identifica l'output DOPO aver applicato `operations[0..i]`.
    La lista ha la stessa lunghezza di `operations`.
    """
    hashes: list[str] = []
    running = source_id
    for op in operations:
        running = hashlib.sha256(f"{running}\n{_canonical(op)}".encode()).hexdigest()
        hashes.append(running)
    return hashes


class StepCache:
    def __init__(self, storage, redis_client=None):
        settings = get_settings()
        self.storage = storage
        self.bucket = settings.storage.bucket
        self.redis = redis_client or redis.Redis.from_url(settings.redis.url, decode_responses=True)

    def object_key(self, h: str) -> str:
        """Chiave storage del parquet materializzato per l'hash `h`."""
        return f"{CACHE_PREFIX}/{h}.parquet"

    def has(self, h: str) -> bool:
        """Solo presenza nell'indice (nessun effetto collaterale)."""
        try:
            return bool(self.redis.sismember(INDEX_SET, h))
        except redis.RedisError:
            return False  # cache non raggiungibile → trattala come vuota

    def mark(self, h: str) -> None:
        """Registra un hash appena materializzato e ne segna l'accesso."""
        try:
            self.redis.sadd(INDEX_SET, h)
            self.redis.zadd(ATIME_ZSET, {h: time.time()})
        except redis.RedisError:
            pass

    def touch(self, h: str) -> None:
        """Aggiorna il timestamp di ultimo accesso (chiamato quando si USA la cache)."""
        try:
            self.redis.zadd(ATIME_ZSET, {h: time.time()})
        except redis.RedisError:
            pass

    def record_hit(self) -> None:
        """Segna un riuso della cache (per le metriche)."""
        try:
            self.redis.incr(HITS_KEY)
        except redis.RedisError:
            pass

    def record_miss(self) -> None:
        """Segna un miss (ricalcolo dalla sorgente) per le metriche."""
        try:
            self.redis.incr(MISSES_KEY)
        except redis.RedisError:
            pass

    def nearest(self, hashes: list[str]) -> int:
        """
        Quanti step iniziali sono già coperti dalla cache.

        Ritorna l'indice `k` (0..len) tale che `hashes[k-1]` è l'antenato
        materializzato più vicino. 0 = nessun antenato in cache (si parte dalla
        sorgente).
        """
        for k in range(len(hashes), 0, -1):
            if self.has(hashes[k - 1]):
                return k
        return 0

    def _reconcile(self) -> None:
        """
        Ripristina l'invariante SET == ZSET.

        Serve solo a coprire drift/migrazioni: una voce nel SET senza `atime`
        (es. scritta da una versione precedente) sarebbe invisibile al TTL →
        le diamo `atime=now` (adottata nel ciclo TTL). Voci nello ZSET senza SET
        vengono rimosse.
        """
        now = time.time()
        in_set = set(self.redis.smembers(INDEX_SET))
        in_zset = set(self.redis.zrange(ATIME_ZSET, 0, -1))
        for h in in_set - in_zset:
            self.redis.zadd(ATIME_ZSET, {h: now})
        for h in in_zset - in_set:
            self.redis.zrem(ATIME_ZSET, h)

    def evict_expired(self, ttl_seconds: int) -> int:
        """
        Rimuove le voci non accedute da più di `ttl_seconds`.

        Per ogni voce scaduta cancella IN BLOCCO: blob parquet + SET + ZSET, così
        indice e storage restano sincronizzati. Ritorna il numero di voci rimosse.
        """
        try:
            self._reconcile()
            cutoff = time.time() - ttl_seconds
            expired = self.redis.zrangebyscore(ATIME_ZSET, "-inf", cutoff)
        except redis.RedisError:
            return 0

        for h in expired:
            # delete_object è idempotente (S3): nessun errore se il blob non c'è
            self.storage.delete_object(self.bucket, self.object_key(h))
            self.redis.srem(INDEX_SET, h)
            self.redis.zrem(ATIME_ZSET, h)

        if expired:
            logger.info("Cache eviction: rimosse %d voci (ttl=%ds)", len(expired), ttl_seconds)
        return len(expired)

    def clear(self) -> int:
        """Svuota tutta la cache (blob + indici). Ritorna quante voci rimosse."""
        try:
            hashes = self.redis.smembers(INDEX_SET)
        except redis.RedisError:
            return 0
        for h in hashes:
            self.storage.delete_object(self.bucket, self.object_key(h))
        try:
            self.redis.delete(INDEX_SET, ATIME_ZSET)
        except redis.RedisError:
            pass
        return len(hashes)
