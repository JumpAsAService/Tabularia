"""The viewer's materialized copies, after the review of 2026-09-16 (R10, R11).

R10: a copy dropped by the TTL sweep while a preview had just picked it made that
preview fail, against the module's promise of never an error to the user. The
sweep now re-reads the last access right before dropping, and a preview whose
copy vanished anyway reads the parquet instead.

R11: the negative caches ("below the threshold", "failed recently") grew forever,
one entry per refreshed source, and the failure check scanned a range per call.
"""
import io
import time

import polars as pl
import pytest

from app.core.config import ClickHouseExternalSettings
from app.engine.exceptions import EngineError
from app.engine.matview import ATIME_ZSET, FAILED_ZSET, FAILURE_BACKOFF_SECONDS, SMALL_SET, SMALL_ZSET, MatViewStore
from tests.fakes import FakeRedis
from tests.test_matview import SRC, FakeCtx, _cfg


# ── R11: the negative caches are pruned ───────────────────────────────────────
def test_below_the_threshold_is_remembered_then_counted_again_after_the_ttl():
    redis = FakeRedis()
    store = MatViewStore(redis, _cfg(materialize_ttl_seconds=100))
    small = FakeCtx(rows=10)
    assert store.resolve(small, SRC) is None
    assert store.resolve(small, SRC) is None
    assert small.counts == 1  # remembered: not counted again
    sid = next(iter(redis.zsets[SMALL_ZSET]))
    redis.zsets[SMALL_ZSET][sid] = time.time() - 1_000  # older than the TTL
    store.evict(FakeCtx(rows=0), 100)
    assert sid not in redis.zsets[SMALL_ZSET]
    assert store.resolve(small, SRC) is None
    assert small.counts == 2  # counted once more, then remembered again


def test_the_old_unpruned_set_is_deleted_by_the_sweep():
    redis = FakeRedis()
    redis.sadd(SMALL_SET, "datasets/1/old.parquet")
    MatViewStore(redis, _cfg(materialize_ttl_seconds=100)).evict(FakeCtx(rows=0), 100)
    assert redis.smembers(SMALL_SET) == set()


def test_a_failure_holds_back_retries_for_the_back_off_and_is_then_pruned():
    redis = FakeRedis()
    store = MatViewStore(redis, _cfg(materialize_ttl_seconds=100))
    sid = store._sid(SRC)
    store._mark_failed(sid)
    assert store._failed_recently(sid)
    redis.zsets[FAILED_ZSET][sid] = time.time() - FAILURE_BACKOFF_SECONDS - 1
    assert not store._failed_recently(sid)
    store.evict(FakeCtx(rows=0), 100)
    assert sid not in redis.zsets[FAILED_ZSET]


# ── R10: the sweep and the preview do not trip over each other ──────────────────
def test_a_copy_used_after_the_expiry_list_was_taken_is_not_dropped(monkeypatch):
    redis = FakeRedis()
    store = MatViewStore(redis, _cfg(materialize_ttl_seconds=100))
    ctx = FakeCtx(rows=5_000_000)
    store.resolve(ctx, SRC)  # builds the copy
    sid = store._sid(SRC)
    table = store.table_name(sid)
    # the expiry list says "expired", but a preview touched the copy in between
    monkeypatch.setattr(store, "_expired", lambda cutoff: [sid])
    redis.zsets[ATIME_ZSET][sid] = time.time()
    assert store.evict(ctx, 100) == 0
    assert table in ctx.tables and ctx.dropped == []


def test_an_expired_copy_is_still_dropped():
    redis = FakeRedis()
    store = MatViewStore(redis, _cfg(materialize_ttl_seconds=100))
    ctx = FakeCtx(rows=5_000_000)
    store.resolve(ctx, SRC)
    sid = store._sid(SRC)
    redis.zsets[ATIME_ZSET][sid] = time.time() - 1_000
    assert store.evict(ctx, 100) == 1
    assert ctx.dropped == [store.table_name(sid)]


def test_discard_makes_the_next_preview_build_the_copy_again():
    store = MatViewStore(FakeRedis(), _cfg())
    ctx = FakeCtx(rows=5_000_000)
    store.resolve(ctx, SRC, ["id"])
    ctx.tables.clear()  # gone from the server
    store.discard(SRC, ["id"])
    assert store.resolve(ctx, SRC, ["id"]) is not None
    assert len(ctx.built) == 2


def _unknown_table() -> EngineError:
    try:
        raise RuntimeError("Code: 60. DB::Exception: Table mv._mv_x does not exist. (UNKNOWN_TABLE)")
    except RuntimeError as cause:
        try:
            raise EngineError("the table is missing") from cause
        except EngineError as e:
            return e


def test_the_unknown_table_error_is_recognised_through_the_wrapper():
    from app.engine.clickhouse_engine import _is_unknown_table

    assert _is_unknown_table(_unknown_table())
    assert not _is_unknown_table(EngineError("Code: 62. Syntax error"))


class _PreviewCtx:
    """Stands in for ClickHouseContext: a copy whose table is gone when queried."""

    def __init__(self):
        self.settings: dict = {}
        self.allow_matview = True
        self.root_source = "?"
        self.queries: list[str] = []

    def cleanup(self) -> None:
        pass

    def parquet_bytes(self, sql: str) -> bytes:
        self.queries.append(sql)
        if "mv_copy" in sql:
            raise _unknown_table()
        buf = io.BytesIO()
        pl.DataFrame({"id": [1, 2, 3]}).write_parquet(buf)
        return buf.getvalue()


def _preview_engine(monkeypatch, ctx, root_from_copy=True):
    from app.engine import clickhouse_engine as ce

    eng = ce.ClickHouseEngine.__new__(ce.ClickHouseEngine)  # no host, no Valkey
    eng.cfg = ClickHouseExternalSettings(host="h", transport="s3", materialize_min_rows=1)
    eng._server_threads = None
    eng.storage = None
    eng.matviews = MatViewStore(FakeRedis(), eng.cfg)
    discarded = []
    monkeypatch.setattr(eng.matviews, "discard", lambda source, keys=None: discarded.append((source.key, keys)))
    monkeypatch.setattr(ce, "ClickHouseContext", lambda *a, **k: ctx)
    monkeypatch.setattr(eng, "_client", lambda: object())
    monkeypatch.setattr(eng, "_source_id", lambda source: "sid")
    monkeypatch.setattr(eng, "_cache_state", lambda *a: (None, None))

    def sql(c, source, ops, hashes, record=False, use_cache=True):
        if c.allow_matview and root_from_copy:
            c.root_source = "matview"
            return "SELECT * FROM mv_copy"
        c.root_source = "s3"
        return "SELECT * FROM s3_parquet" if root_from_copy else "SELECT * FROM mv_copy"

    monkeypatch.setattr(eng, "_sql_from_cache", sql)
    return eng, discarded


def test_a_copy_that_vanished_under_the_preview_falls_back_to_the_parquet(monkeypatch):
    ctx = _PreviewCtx()
    eng, discarded = _preview_engine(monkeypatch, ctx)
    res = eng.preview(SRC, [], limit=10, use_cache=False, sort_keys=["id"])
    assert res.row_count == 3
    assert discarded == [(SRC.key, ["id"])]
    assert "s3_parquet" in ctx.queries[-1] and ctx.root_source == "s3"


def test_a_missing_table_that_is_not_the_copy_is_still_an_error(monkeypatch):
    # the same error from the user's own SQL, with the parquet as the root: no retry
    ctx = _PreviewCtx()
    eng, discarded = _preview_engine(monkeypatch, ctx, root_from_copy=False)
    with pytest.raises(EngineError):
        eng.preview(SRC, [], limit=10, use_cache=False)
    assert discarded == [] and len(ctx.queries) == 1
