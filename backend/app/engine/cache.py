"""
The incremental cache of the flow's steps.

Idea: a node's output is a deterministic function of the source + the operations up to
it. A progressive (rolling) hash is computed: changing an operation changes its hash and
those downstream, but those upstream stay stable. So a step's output can be
materialized in `cache/<hash>.parquet` and, next time, the chain starts from the
cached ancestor instead of the source.

Structures on Valkey (ALWAYS kept in sync with each other and with the storage):
- SET  `dataprep:stepcache`        → materialized hashes (an index of presence).
- ZSET `dataprep:stepcache:atime`  → hash → timestamp of the last access (for the TTL).

The parquet blobs are on the same S3 storage, under `cache/`.

The cache is "best effort": when Valkey does not answer it behaves as an empty cache
and the flow is computed again normally (no error for the user).

Content-addressed ⇒ no invalidation: changing a parameter changes the hash. The entries
no longer accessed are removed by the TTL eviction (`evict_expired`), which deletes
blob + SET + ZSET together, so index and storage stay aligned.

The index is a PROMISE, not a proof: a blob may vanish underneath (deleted by hand on
the bucket, a lifecycle rule, a change of storage). A hit on a missing blob must never
break a preview: `nearest` checks it exists (HEAD) before reusing a step, forgets the
orphaned entry and goes back to the previous ancestor, up to the source; the periodic
sweep discards the entries without a blob.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CACHE_PREFIX = "cache"  # prefix of the parquet keys in the storage
INDEX_SET = "dataprep:stepcache"  # Valkey SET with the materialized hashes
ATIME_ZSET = "dataprep:stepcache:atime"  # ZSET hash -> last access (unix ts)
HITS_KEY = "dataprep:metrics:cache_hits"  # reuse counter (for the metrics)
MISSES_KEY = "dataprep:metrics:cache_misses"  # miss counter (for the metrics)


def _canonical(op: dict) -> str:
    """A stable serialization of an operation (sorted keys) for the hash."""
    return json.dumps(op, sort_keys=True, separators=(",", ":"))


def plan_hashes(source_id: str, operations: list[dict]) -> list[str]:
    """
    The chain's progressive hash.

    `hashes[i]` identifies the output AFTER applying `operations[0..i]`. The list is as
    long as `operations`.
    """
    hashes: list[str] = []
    running = source_id
    for op in operations:
        running = hashlib.sha256(f"{running}\n{_canonical(op)}".encode()).hexdigest()
        hashes.append(running)
    return hashes


class StepCache:
    def __init__(self, storage, redis_client=None, namespace: str = ""):
        settings = get_settings()
        self.storage = storage
        self.bucket = settings.storage.bucket
        self.redis = redis_client or redis.Redis.from_url(settings.redis.url, decode_responses=True)
        # `namespace`: a SEPARATE index for the caches that are not in the storage
        # (BigQuery: native tables). The common index's sweep must not see them, or it
        # would discard them as orphans (no parquet).
        suffix = f":{namespace}" if namespace else ""
        self.index_set = INDEX_SET + suffix
        self.atime_zset = ATIME_ZSET + suffix if not namespace else f"{INDEX_SET}{suffix}:atime"

    def object_key(self, h: str) -> str:
        """Chiave storage del parquet materializzato per l'hash `h`."""
        return f"{CACHE_PREFIX}/{h}.parquet"

    def blob_exists(self, h: str) -> bool:
        """Does the step's parquet really exist on the storage? (HEAD, never a download).
        When the storage cannot answer (a transient error) True is assumed: the next
        download fails explicitly anyway."""
        check = getattr(self.storage, "object_exists", None) or getattr(self.storage, "exists", None)
        if check is None:
            return True
        try:
            return bool(check(self.bucket, self.object_key(h)))
        except Exception as e:
            from app.engine.query_tag import was_interrupted

            if was_interrupted(e):
                raise
            return True

    def forget(self, h: str) -> None:
        """Removes an entry from the index (SET + ZSET) without touching the storage: for the
        ORPHANED entries, whose blob no longer exists."""
        try:
            self.redis.srem(self.index_set, h)
            self.redis.zrem(self.atime_zset, h)
        except redis.RedisError:
            pass

    def has(self, h: str) -> bool:
        """Presence in the index only (no side effects)."""
        try:
            return bool(self.redis.sismember(self.index_set, h))
        except redis.RedisError:
            return False  # cache unreachable → treat it as empty

    def mark(self, h: str) -> None:
        """Records a hash just materialized and marks its access."""
        try:
            self.redis.sadd(self.index_set, h)
            self.redis.zadd(self.atime_zset, {h: time.time()})
        except redis.RedisError:
            pass

    def touch(self, h: str) -> None:
        """Updates the last-access timestamp (called when the cache is USED)."""
        try:
            self.redis.zadd(self.atime_zset, {h: time.time()})
        except redis.RedisError:
            pass

    # ── lock and discarded steps (deferred materialization) ────────
    def _lock_key(self, h: str) -> str:
        return f"{self.index_set}:lock:{h}"

    def _skip_key(self, h: str) -> str:
        return f"{self.index_set}:skip:{h}"

    def try_lock(self, h: str, ttl_seconds: int = 900) -> bool:
        """Takes the lock of the materialization of `h` (SET NX with an expiry). Valkey down
        → True: better a copy twice than none."""
        try:
            return bool(self.redis.set(self._lock_key(h), "1", nx=True, ex=ttl_seconds))
        except redis.RedisError:
            return True

    def unlock(self, h: str) -> None:
        try:
            self.redis.delete(self._lock_key(h))
        except redis.RedisError:
            pass

    def mark_skipped(self, h: str, ttl_seconds: int | None = None) -> None:
        """Remembers that step `h` is too large for the cache (for the cache's TTL): the next
        clicks do not count it again."""
        ttl = ttl_seconds or int(get_settings().cache.ttl_seconds)
        try:
            self.redis.set(self._skip_key(h), "1", ex=ttl)
        except redis.RedisError:
            pass

    def is_skipped(self, h: str) -> bool:
        try:
            return bool(self.redis.exists(self._skip_key(h)))
        except redis.RedisError:
            return False

    def record_hit(self) -> None:
        """Marks a reuse of the cache (for the metrics)."""
        try:
            self.redis.incr(HITS_KEY)
        except redis.RedisError:
            pass

    def record_miss(self) -> None:
        """Marks a miss (computed again from the source) for the metrics."""
        try:
            self.redis.incr(MISSES_KEY)
        except redis.RedisError:
            pass

    def nearest(self, hashes: list[str]) -> int:
        """
        How many leading steps the cache covers already.

        Returns the index `k` (0..len) such that `hashes[k-1]` is the nearest
        materialized ancestor. 0 = no ancestor cached (start from the source).

        Self-repairing: a hash in the index without a blob on the storage is forgotten
        and the previous ancestor is tried. One HEAD per hit: negligible next to the
        download of the parquet that follows.
        """
        for k in range(len(hashes), 0, -1):
            h = hashes[k - 1]
            if not self.has(h):
                continue
            if self.blob_exists(h):
                return k
            logger.warning("cache: orphaned entry %s (blob missing on the storage), removed from the index", h[:12])
            self.forget(h)
        return 0

    def _reconcile(self) -> None:
        """
        Restores the invariant SET == ZSET.

        Only to cover drift and migrations: an entry in the SET without an `atime`
        (written by an earlier version) would be invisible to the TTL → it gets
        `atime=now` (adopted into the TTL cycle). Entries in the ZSET without the SET are
        removed.
        """
        now = time.time()
        in_set = set(self.redis.smembers(self.index_set))
        in_zset = set(self.redis.zrange(self.atime_zset, 0, -1))
        for h in in_set - in_zset:
            self.redis.zadd(self.atime_zset, {h: now})
        for h in in_zset - in_set:
            self.redis.zrem(self.atime_zset, h)

    def evict_expired(self, ttl_seconds: int) -> int:
        """
        Removes the entries not accessed for more than `ttl_seconds`.

        For every expired entry it deletes TOGETHER: parquet blob + SET + ZSET, so index
        and storage stay in sync. The entries NOT expired whose blob vanished from the
        storage (deleted by hand, lifecycle) are forgotten. Returns how many entries were
        removed (expired + orphaned).
        """
        try:
            self._reconcile()
            cutoff = time.time() - ttl_seconds
            expired = self.redis.zrangebyscore(self.atime_zset, "-inf", cutoff)
            alive = [h for h in self.redis.zrangebyscore(self.atime_zset, cutoff, "+inf")]
        except redis.RedisError:
            return 0

        for h in expired:
            # delete_object is idempotent (S3): no error when the blob is not there
            self.storage.delete_object(self.bucket, self.object_key(h))
            self.redis.srem(self.index_set, h)
            self.redis.zrem(self.atime_zset, h)

        orphans = [h for h in alive if not self.blob_exists(h)]
        for h in orphans:
            self.forget(h)

        if expired or orphans:
            logger.info(
                "Cache eviction: removed %d expired entries (ttl=%ds) and %d orphaned (blob missing)",
                len(expired), ttl_seconds, len(orphans),
            )
        return len(expired) + len(orphans)

    def clear(self) -> int:
        """Empties the whole cache (blobs + indexes). Returns how many entries were removed."""
        try:
            hashes = self.redis.smembers(self.index_set)
        except redis.RedisError:
            return 0
        for h in hashes:
            self.storage.delete_object(self.bucket, self.object_key(h))
        try:
            self.redis.delete(self.index_set, self.atime_zset)
        except redis.RedisError:
            pass
        return len(hashes)
