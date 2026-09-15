"""Materializzazione dei dataset grandi sul server ClickHouse, per il viewer.

Il viewer interroga sempre SENZA cache (filtri, grafici e pivot cambiano in
continuazione): con l'engine ClickHouse esterno ogni query rilegge il parquet
dallo storage con la table function `s3()`. Su una scansione piena (un grafico,
un pivot, un ordinamento) questo costa: rileggere e ri-parsare il parquet ogni
volta. Copiare UNA VOLTA il dataset in una tabella `MergeTree` sul server e far
puntare lì le query successive è 2–6× più veloce, e il guadagno cresce con la
dimensione.

Questo modulo è la POLITICA: quando conviene farlo, come non rifarlo due volte,
come non lasciare copie orfane. Le cinque operazioni SQL concrete (contare,
verificare, costruire, eliminare, elencare) stanno sul `ClickHouseContext`, che
è l'unico a saper parlare col server — così qui non c'è SQL e la politica si
prova con un contesto finto.

Confine di sicurezza. Queste tabelle copiano un intero dataset in un database
CONDIVISO da tutte le esecuzioni. Il nodo `sql` vieta le table function di
accesso esterno ma non un `FROM <tabella>`: se il nome fosse derivabile dalla
chiave S3, chi la conoscesse potrebbe leggere in join la copia di un dataset che
il gateway non gli lascerebbe vedere. Perciò il nome è un HMAC con la chiave
Fernet del deployment (segreto, stabile fra worker e riavvii): deterministico
per poterla riusare, ma non calcolabile da fuori. La tabella contiene comunque
solo gli stessi dati del parquet che l'utente sta già guardando: non è una
nuova via d'accesso, è una copia più veloce della stessa.

Best-effort come la step cache: qualunque errore (permessi, database assente,
Valkey giù) fa cadere sul percorso `s3()` di sempre, mai un errore all'utente.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

import redis

from app.core.config import get_settings
from app.engine.chdb_ops import _qi

logger = logging.getLogger(__name__)

# prefisso del nome tabella: riconoscibile per lo sweep delle orfane
TABLE_PREFIX = "_mv_"
INDEX_SET = "dataprep:matview"  # SET Valkey: source id materializzati
ATIME_ZSET = "dataprep:matview:atime"  # ZSET: source id -> ultimo accesso (unix ts)
SMALL_SET = "dataprep:matview:small"  # SET: source id sotto soglia (cache negativa)


class MatViewStore:
    """Registro delle materializzazioni su Valkey + decisione se/quando farle.

    Tre insiemi, tenuti allineati fra loro e col server:
    - INDEX_SET   gli id sorgente con una tabella viva;
    - ATIME_ZSET  il loro ultimo accesso, per il drop a TTL;
    - SMALL_SET   gli id già visti sotto soglia, per non ricontarli a ogni preview
                  (le chiavi dei dataset sono immutabili: sotto soglia oggi, sotto
                  soglia domani → nessuna invalidazione).
    """

    def __init__(self, redis_client, cfg):
        self.redis = redis_client
        self.cfg = cfg

    # ── identità ────────────────────────────────────────────────────────────
    @staticmethod
    def _sid(source, sort_keys=None) -> str:
        # le chiavi fanno parte dell'IDENTITÀ: cambiarle deve dare una tabella
        # nuova (con ORDER BY diverso), non riusare quella vecchia
        base = f"{source.bucket}/{source.key}"
        keys = [k for k in (sort_keys or []) if k]
        return f"{base}|sk={','.join(keys)}" if keys else base

    def _secret(self) -> bytes:
        # la chiave Fernet è obbligatoria (check_required_secrets): il fallback
        # serve solo a non sollevare in un test che non la imposta
        return (get_settings().security.fernet_key or "tabularia-matview").encode()

    def table_name(self, sid: str) -> str:
        digest = hmac.new(self._secret(), sid.encode(), hashlib.sha256).hexdigest()
        return f"{TABLE_PREFIX}{digest[:32]}"

    def _qualified(self, table: str) -> str:
        return f"{_qi(self.cfg.matview_database)}.{_qi(table)}"

    # ── decisione (chiamata da ClickHouseContext.scan sulla sorgente radice) ──
    def resolve(self, ctx, source, sort_keys=None) -> str | None:
        """Nome qualificato della tabella da leggere al posto di `s3()`, o None
        per restare su `s3()`. Materializza pigramente alla prima richiesta di un
        dataset sopra soglia; per gli altri è un no-op dopo la prima volta. La
        copia eredita `sort_keys` come ORDER BY (vedi ctx.matview_build)."""
        cfg = self.cfg
        if not cfg.materialize_enabled:
            return None
        sid = self._sid(source, sort_keys)
        db = cfg.matview_database
        table = self.table_name(sid)
        try:
            if self._is_small(sid):
                return None
            if self._is_known(sid):
                if ctx.matview_exists(db, table):
                    self._touch(sid)
                    return self._qualified(table)
                self._forget(sid)  # sparita sotto i piedi (drop, TTL): la rifaccio
            rows = ctx.matview_count(source)
            if rows < cfg.materialize_min_rows:
                self._mark_small(sid)
                return None
            ctx.matview_build(db, table, source, sort_keys)
            self._mark(sid)
            logger.info("materializzata la sorgente %s in %s (%d righe)", sid, table, rows)
            return self._qualified(table)
        except Exception as e:  # best-effort: sempre giù su s3(), mai un errore
            logger.warning("materializzazione non riuscita per %s, resto su s3(): %s", sid, e)
            return None

    # ── drop periodico (chiamato dal task beat) ───────────────────────────────
    def evict(self, ctx, ttl_seconds: int) -> int:
        """Elimina le tabelle non accedute da più di `ttl_seconds` e le orfane
        (nel database ma non nel registro, e più vecchie del TTL: residui di un
        worker morto fra CREATE e RENAME, o di un Valkey svuotato)."""
        db = self.cfg.matview_database
        cutoff = time.time() - ttl_seconds
        removed = 0

        for sid in self._expired(cutoff):
            try:
                ctx.matview_drop(db, self.table_name(sid))
            except Exception:
                logger.warning("drop della matview scaduta %s non riuscito", sid)
                continue
            self._forget(sid)
            removed += 1

        known = {self.table_name(s) for s in self._all_known()}
        try:
            server = ctx.matview_list(db, TABLE_PREFIX)
        except Exception:
            server = []
        for name, mtime in server:
            if name in known or mtime > cutoff:
                continue  # registrata, oppure troppo recente (forse in creazione)
            try:
                ctx.matview_drop(db, name)
                removed += 1
            except Exception:
                pass

        if removed:
            logger.info("matview eviction: rimosse %d tabelle (ttl=%ds)", removed, ttl_seconds)
        return removed

    # ── registro Valkey (best-effort: se giù, la feature semplicemente non agisce) ──
    def _is_known(self, sid: str) -> bool:
        try:
            return bool(self.redis.sismember(INDEX_SET, sid))
        except redis.RedisError:
            return False

    def _all_known(self) -> set:
        try:
            return set(self.redis.smembers(INDEX_SET))
        except redis.RedisError:
            return set()

    def _mark(self, sid: str) -> None:
        try:
            self.redis.sadd(INDEX_SET, sid)
            self.redis.zadd(ATIME_ZSET, {sid: time.time()})
            self.redis.srem(SMALL_SET, sid)
        except redis.RedisError:
            pass

    def _touch(self, sid: str) -> None:
        try:
            self.redis.zadd(ATIME_ZSET, {sid: time.time()})
        except redis.RedisError:
            pass

    def _forget(self, sid: str) -> None:
        try:
            self.redis.srem(INDEX_SET, sid)
            self.redis.zrem(ATIME_ZSET, sid)
        except redis.RedisError:
            pass

    def _is_small(self, sid: str) -> bool:
        try:
            return bool(self.redis.sismember(SMALL_SET, sid))
        except redis.RedisError:
            return False

    def _mark_small(self, sid: str) -> None:
        try:
            self.redis.sadd(SMALL_SET, sid)
        except redis.RedisError:
            pass

    def _expired(self, cutoff: float) -> list:
        try:
            return list(self.redis.zrangebyscore(ATIME_ZSET, "-inf", cutoff))
        except redis.RedisError:
            return []
