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

L'indice è una PROMESSA, non una prova: un blob può sparire sotto i suoi piedi
(cancellazione manuale sul bucket, regola di lifecycle, cambio di storage).
Un hit su un blob mancante non deve mai rompere una preview: `nearest` verifica
l'esistenza (HEAD) prima di riusare uno step, dimentica la voce orfana e risale
all'antenato precedente, fino alla sorgente; lo sweep periodico scarta le voci
senza blob.
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

    def blob_exists(self, h: str) -> bool:
        """Il parquet dello step esiste davvero nello storage? (HEAD, mai download).
        Se lo storage non sa rispondere (errore transitorio) si assume True: il
        download successivo fallirà comunque in modo esplicito."""
        check = getattr(self.storage, "object_exists", None) or getattr(self.storage, "exists", None)
        if check is None:
            return True
        try:
            return bool(check(self.bucket, self.object_key(h)))
        except Exception:
            return True

    def forget(self, h: str) -> None:
        """Toglie una voce dall'indice (SET + ZSET) senza toccare lo storage:
        per le voci ORFANE, il cui blob non esiste più."""
        try:
            self.redis.srem(INDEX_SET, h)
            self.redis.zrem(ATIME_ZSET, h)
        except redis.RedisError:
            pass

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

        Auto-riparante: un hash presente nell'indice ma senza blob nello storage
        viene dimenticato e si prosegue con l'antenato precedente. Una HEAD per
        hit: trascurabile rispetto al download del parquet che segue.
        """
        for k in range(len(hashes), 0, -1):
            h = hashes[k - 1]
            if not self.has(h):
                continue
            if self.blob_exists(h):
                return k
            logger.warning("cache: voce orfana %s (blob mancante nello storage), rimossa dall'indice", h[:12])
            self.forget(h)
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
        indice e storage restano sincronizzati. Le voci NON scadute il cui blob
        è sparito dallo storage (cancellazione manuale, lifecycle) vengono
        dimenticate. Ritorna il numero di voci rimosse (scadute + orfane).
        """
        try:
            self._reconcile()
            cutoff = time.time() - ttl_seconds
            expired = self.redis.zrangebyscore(ATIME_ZSET, "-inf", cutoff)
            alive = [h for h in self.redis.zrangebyscore(ATIME_ZSET, cutoff, "+inf")]
        except redis.RedisError:
            return 0

        for h in expired:
            # delete_object è idempotente (S3): nessun errore se il blob non c'è
            self.storage.delete_object(self.bucket, self.object_key(h))
            self.redis.srem(INDEX_SET, h)
            self.redis.zrem(ATIME_ZSET, h)

        orphans = [h for h in alive if not self.blob_exists(h)]
        for h in orphans:
            self.forget(h)

        if expired or orphans:
            logger.info(
                "Cache eviction: rimosse %d voci scadute (ttl=%ds) e %d orfane (blob mancante)",
                len(expired), ttl_seconds, len(orphans),
            )
        return len(expired) + len(orphans)

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
