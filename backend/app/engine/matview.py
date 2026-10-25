"""Materializing large datasets on the ClickHouse server, for the viewer.

The viewer always queries WITHOUT a cache (filters, charts and pivots change all the
time): with the external ClickHouse engine every query reads the parquet again from the
storage with the `s3()` table function. On a full scan (a chart, a pivot, a sort) that
costs: reading and parsing the parquet every time. Copying the dataset ONCE into a
`MergeTree` table on the server and pointing the next queries there is 2–6× faster, and
the gain grows with the size.

This module is the POLICY: when it pays, how not to do it twice, how not to leave
orphaned copies. The five concrete SQL operations (count, check, build, drop, list) are
on `ClickHouseContext`, the only one that can talk to the server — so there is no SQL
here and the policy is tested with a fake context.

Security boundary. These tables copy a whole dataset into a database SHARED by every
run. The `sql` node forbids the table functions of external access but not a
`FROM <table>`: if the name could be derived from the S3 key, whoever knew it could join
the copy of a dataset the gateway would not let them see. So the name is an HMAC with
the deployment's Fernet key (secret, stable across workers and restarts): deterministic
so it can be reused, but not computable from outside. The table holds only the same data
as the parquet the user is looking at already: not a new way in, a faster copy of the
same.

Best effort as the step cache: any error (permissions, a missing database, Valkey down)
falls back on the usual `s3()` path, never an error for the user.
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

# the table name's prefix: recognizable by the sweep of the orphans
TABLE_PREFIX = "_mv_"
INDEX_SET = "dataprep:matview"  # SET Valkey: source id materializzati
ATIME_ZSET = "dataprep:matview:atime"  # ZSET: source id -> last access (unix ts)
# ZSET: source id below the threshold -> when it was counted (negative cache). A
# sorted set, so the TTL sweep can prune it: every refresh is a new source id.
SMALL_ZSET = "dataprep:matview:small:z"
SMALL_SET = "dataprep:matview:small"  # the unpruned SET of earlier releases: deleted by the sweep
FAILED_ZSET = "dataprep:matview:failed"  # ZSET: source id -> when it last failed
# After a failure, a wait before trying again: without it EVERY preview starts the
# copy of the whole dataset again (a missing grant, a full disk, a worker killed by
# the timeout) and the viewer stays SLOWER for ever than before the feature existed.
FAILURE_BACKOFF_SECONDS = 600
# A cap on the live copies: `sort_keys` is part of the identity, so whoever varies
# the keys makes distinct copies of the same data. The TTL alone is not enough to
# protect the shared server's disk.
MAX_TABLES = 32


class MatViewStore:
    """The registry of the materializations on Valkey + the decision whether and when.

    Three sets, kept aligned with each other and with the server:
    - INDEX_SET   the source ids with a live table;
    - ATIME_ZSET  their last access, for the TTL drop;
    - SMALL_ZSET  the ids seen below the threshold already, not to count them again at
                  every preview (the datasets' keys are immutable: below the threshold
                  today, below it tomorrow → no invalidation). Pruned after the TTL with
                  FAILED_ZSET: a source counted again after that costs one count.
    """

    def __init__(self, redis_client, cfg):
        self.redis = redis_client
        self.cfg = cfg

    # the copies' threshold and lifetime are set from the panel (core/panel_settings.py)
    @property
    def cfg(self):
        from app.core import panel_settings

        return panel_settings.overlapping(self._cfg, "clickhouse_external")

    @cfg.setter
    def cfg(self, value) -> None:
        self._cfg = value

    # ── identity ────────────────────────────────────────────────────────────
    @staticmethod
    def _sid(source, sort_keys=None) -> str:
        # the keys are part of the IDENTITY: changing them must give a new table (with
        # another ORDER BY), not reuse the old one
        base = f"{source.bucket}/{source.key}"
        keys = [k for k in (sort_keys or []) if k]
        return f"{base}|sk={','.join(keys)}" if keys else base

    def _secret(self) -> bytes:
        # the Fernet key is required (check_required_secrets): the fallback only keeps
        # a test that does not set it from raising
        return (get_settings().security.fernet_key or "tabularia-matview").encode()

    def table_name(self, sid: str) -> str:
        digest = hmac.new(self._secret(), sid.encode(), hashlib.sha256).hexdigest()
        return f"{TABLE_PREFIX}{digest[:32]}"

    def _qualified(self, table: str) -> str:
        return f"{_qi(self.cfg.matview_database)}.{_qi(table)}"

    # ── decision (called by ClickHouseContext.scan on the root source) ──
    def resolve(self, ctx, source, sort_keys=None) -> str | None:
        """The qualified name of the table to read instead of `s3()`, or None to stay on
        `s3()`. Materializes lazily at the first request for a dataset above the
        threshold; for the others a no-op after the first time. The copy inherits
        `sort_keys` as its ORDER BY (see ctx.matview_build)."""
        cfg = self.cfg
        if not cfg.materialize_enabled:
            return None
        sid = self._sid(source, sort_keys)
        db = cfg.matview_database
        table = self.table_name(sid)
        try:
            if self._is_small(sid) or self._failed_recently(sid):
                return None
            if self._is_known(sid):
                if ctx.matview_exists(db, table):
                    self._touch(sid)
                    return self._qualified(table)
                self._forget(sid)  # gone underneath (drop, TTL): made again
            rows = ctx.matview_count(source)
            if rows < cfg.materialize_min_rows:
                self._mark_small(sid)
                return None
            # cap on the live copies: beyond it, s3() stays (no error)
            try:
                live = len(ctx.matview_list(db, TABLE_PREFIX))
            except Exception:
                live = 0
            if live >= MAX_TABLES:
                logger.warning(
                    "matview: %d copies there already (cap %d), staying on s3() for %s",
                    live, MAX_TABLES, sid,
                )
                return None
            ctx.matview_build(db, table, source, sort_keys)
            self._mark(sid)
            logger.info("materialized the source %s in %s (%d rows)", sid, table, rows)
            return self._qualified(table)
        except Exception as e:  # best effort: always down to s3(), never an error
            from app.engine.query_tag import was_interrupted

            if was_interrupted(e):  # a superseded preview: not a failure of the copy
                raise
            # records the failure: without it, the whole copy is tried again at EVERY preview
            self._mark_failed(sid)
            logger.warning("materialization failed for %s, staying on s3(): %s", sid, e)
            return None

    def discard(self, source, sort_keys=None) -> None:
        """Forgets the copy of a source whose table went away under a query, so the
        next preview reads the parquet or builds it again instead of failing."""
        self._forget(self._sid(source, sort_keys))

    # ── periodic drop (called by the beat task) ───────────────────────────────
    def evict(self, ctx, ttl_seconds: int) -> int:
        """Drops the tables not accessed for more than `ttl_seconds` and the orphans (in the
        database but not in the registry, and older than the TTL: left by a worker that
        died between CREATE and RENAME, or by an emptied Valkey)."""
        db = self.cfg.matview_database
        cutoff = time.time() - ttl_seconds
        removed = 0
        self._reconcile()
        self._prune(cutoff)

        for sid in self._expired(cutoff):
            # read again right before the drop: a preview may have used the copy
            # since the list above was taken, and is reading it now
            if self._accessed_since(sid, cutoff):
                continue
            try:
                ctx.matview_drop(db, self.table_name(sid))
            except Exception:
                logger.warning("drop of the expired matview %s failed", sid)
                continue
            self._forget(sid)
            removed += 1

        # The registry MUST be readable to sweep the orphans: if Valkey does not answer,
        # `_all_known` would come back empty and every live table would look ORPHANED,
        # all dropped (even under running queries, since metadata_modification_time is
        # the DDL's instant and does not move with use). As the step cache, this fails
        # CLOSED: no sweep.
        known_raw = self._all_known(strict=True)
        if known_raw is None:
            logger.warning("matview: registry unreadable, skipping the sweep of the orphans")
            return removed
        known = {self.table_name(s) for s in known_raw}
        try:
            server = ctx.matview_list(db, TABLE_PREFIX)
        except Exception:
            server = []
        for name, mtime in server:
            if name in known or mtime > cutoff:
                continue  # registered, or too recent (perhaps being made)
            try:
                ctx.matview_drop(db, name)
                removed += 1
            except Exception:
                pass

        if removed:
            logger.info("matview eviction: removed %d tables (ttl=%ds)", removed, ttl_seconds)
        return removed

    # ── Valkey registry (best effort: when it is down, the feature simply does nothing) ──
    def _is_known(self, sid: str) -> bool:
        try:
            return bool(self.redis.sismember(INDEX_SET, sid))
        except redis.RedisError:
            return False

    def _all_known(self, strict: bool = False) -> set | None:
        """The registered ids. `strict=True` returns None when Valkey does not answer, to tell
        «no entries» from «I don't know» (see evict)."""
        try:
            return set(self.redis.smembers(INDEX_SET))
        except redis.RedisError:
            return None if strict else set()

    def _mark(self, sid: str) -> None:
        try:
            self.redis.sadd(INDEX_SET, sid)
            self.redis.zadd(ATIME_ZSET, {sid: time.time()})
            self.redis.zrem(SMALL_ZSET, sid)
            self.redis.zrem(FAILED_ZSET, sid)
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

    def _reconcile(self) -> None:
        """Restores the invariant SET == ZSET (as StepCache._reconcile). An entry in the SET
        without an `atime` — two non-atomic commands interrupted half way — would be
        INVISIBLE to the TTL and at the same time protected from the sweep of the
        orphans: a copy of several GB would stay on the server for ever."""
        try:
            in_set = set(self.redis.smembers(INDEX_SET))
            in_zset = set(self.redis.zrange(ATIME_ZSET, 0, -1))
            now = time.time()
            for sid in in_set - in_zset:
                self.redis.zadd(ATIME_ZSET, {sid: now})  # adopted into the TTL cycle
            for sid in in_zset - in_set:
                self.redis.zrem(ATIME_ZSET, sid)
        except redis.RedisError:
            pass

    def _failed_recently(self, sid: str) -> bool:
        try:
            failed_at = self.redis.zscore(FAILED_ZSET, sid)
        except redis.RedisError:
            return False
        return failed_at is not None and failed_at >= time.time() - FAILURE_BACKOFF_SECONDS

    def _accessed_since(self, sid: str, cutoff: float) -> bool:
        try:
            atime = self.redis.zscore(ATIME_ZSET, sid)
        except redis.RedisError:
            return True  # unknown: keep the table, the next sweep decides
        return atime is not None and atime > cutoff

    def _prune(self, cutoff: float) -> None:
        """Keeps the negative caches from growing forever: failures older than the
        back-off decide nothing any more, and "below the threshold" is counted again
        after the TTL."""
        try:
            self.redis.zremrangebyscore(FAILED_ZSET, "-inf", time.time() - FAILURE_BACKOFF_SECONDS)
            self.redis.zremrangebyscore(SMALL_ZSET, "-inf", cutoff)
            self.redis.delete(SMALL_SET)
        except redis.RedisError:
            pass

    def _mark_failed(self, sid: str) -> None:
        try:
            self.redis.zadd(FAILED_ZSET, {sid: time.time()})
        except redis.RedisError:
            pass

    def _is_small(self, sid: str) -> bool:
        try:
            return self.redis.zscore(SMALL_ZSET, sid) is not None
        except redis.RedisError:
            return False

    def _mark_small(self, sid: str) -> None:
        try:
            self.redis.zadd(SMALL_ZSET, {sid: time.time()})
        except redis.RedisError:
            pass

    def _expired(self, cutoff: float) -> list:
        try:
            return list(self.redis.zrangebyscore(ATIME_ZSET, "-inf", cutoff))
        except redis.RedisError:
            return []
