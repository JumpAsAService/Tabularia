"""Tests of the incremental cache: progressive hashes, index, synchronized eviction."""
from __future__ import annotations

import time

import pytest

from app.engine.cache import ATIME_ZSET, INDEX_SET, StepCache, plan_hashes
from tests.fakes import BrokenRedis

OPS = [
    {"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}},
    {"type": "sort", "params": {"by": "vendite"}},
    {"type": "limit", "params": {"n": 10}},
]


# ── plan_hashes: the properties that make the cache right ──────────────────
def test_same_chain_same_hashes():
    assert plan_hashes("src", OPS) == plan_hashes("src", OPS)


def test_changing_the_last_operation_keeps_the_hashes_upstream():
    modified = OPS[:2] + [{"type": "limit", "params": {"n": 99}}]
    a, b = plan_hashes("src", OPS), plan_hashes("src", modified)
    assert a[:2] == b[:2]  # the ancestors stay reusable
    assert a[2] != b[2]


def test_changing_the_first_operation_invalidates_everything_downstream():
    modified = [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "FR"}}] + OPS[1:]
    a, b = plan_hashes("src", OPS), plan_hashes("src", modified)
    assert all(x != y for x, y in zip(a, b))


def test_different_sources_make_different_hashes():
    assert plan_hashes("src-a", OPS) != plan_hashes("src-b", OPS)


def test_the_order_of_the_keys_in_params_does_not_count():
    a = plan_hashes("src", [{"type": "sort", "params": {"by": "v", "descending": True}}])
    b = plan_hashes("src", [{"type": "sort", "params": {"descending": True, "by": "v"}}])
    assert a == b  # canonical serialization


# ── StepCache: index and accesses ──────────────────────────────────────────────
def _materialize(cache, storage, h: str) -> None:
    """As the engine would: a blob in the storage + an entry in the index (the index alone is
    no longer enough: a hit without a blob is treated as an orphan)."""
    storage.blobs[(cache.bucket, cache.object_key(h))] = b"parquet"
    cache.mark(h)


def test_mark_makes_the_hash_visible_to_has(cache):
    assert not cache.has("abc")
    cache.mark("abc")
    assert cache.has("abc")


def test_nearest_finds_the_deepest_ancestor(cache, storage):
    hashes = plan_hashes("src", OPS)
    _materialize(cache, storage, hashes[0])
    _materialize(cache, storage, hashes[1])
    assert cache.nearest(hashes) == 2  # starts again after the first 2 steps


def test_nearest_without_ancestors_returns_zero(cache):
    assert cache.nearest(plan_hashes("src", OPS)) == 0


def test_touch_posticipa_l_eviction(cache, storage, fredis):
    _materialize(cache, storage, "h1")
    fredis.zadd(ATIME_ZSET, {"h1": time.time() - 9999})  # fakes an old access
    cache.touch("h1")  # use makes it younger
    assert cache.evict_expired(ttl_seconds=3600) == 0
    assert cache.has("h1")


# ── Eviction: blob + SET + ZSET removed TOGETHER ──────────────────────────────
def test_evict_expired_removes_blobs_and_indexes_in_sync(cache, storage, fredis):
    cache.mark("vecchio")
    storage.blobs[(cache.bucket, cache.object_key("vecchio"))] = b"parquet-finto"
    fredis.zadd(ATIME_ZSET, {"vecchio": time.time() - 9999})

    removed = cache.evict_expired(ttl_seconds=3600)

    assert removed == 1
    assert not cache.has("vecchio")
    assert not storage.exists(cache.bucket, cache.object_key("vecchio"))
    assert "vecchio" not in fredis.zrange(ATIME_ZSET, 0, -1)


def test_evict_leaves_the_recent_entries(cache, storage):
    _materialize(cache, storage, "fresco")
    assert cache.evict_expired(ttl_seconds=3600) == 0
    assert cache.has("fresco")


def test_reconcile_adopts_entries_without_atime_and_cleans_orphans(cache, fredis):
    fredis.sadd(INDEX_SET, "solo-nel-set")  # drift: the atime is missing
    fredis.zadd(ATIME_ZSET, {"solo-nello-zset": time.time()})  # drift the other way
    cache._reconcile()
    assert "solo-nel-set" in fredis.zrange(ATIME_ZSET, 0, -1)
    assert "solo-nello-zset" not in fredis.zrange(ATIME_ZSET, 0, -1)


def test_clear_empties_index_and_blobs(cache, storage):
    cache.mark("h1")
    storage.blobs[(cache.bucket, cache.object_key("h1"))] = b"x"
    assert cache.clear() == 1
    assert not cache.has("h1")
    assert not storage.exists(cache.bucket, cache.object_key("h1"))


# ── Best effort: Valkey down must NEVER break a flow ─────────────────────
def test_with_redis_broken_the_cache_behaves_as_empty(storage):
    broken = StepCache(storage, redis_client=BrokenRedis())
    assert broken.has("h") is False
    broken.mark("h")  # does not raise
    broken.touch("h")  # does not raise
    broken.record_hit()  # does not raise
    assert broken.nearest(["a", "b"]) == 0
    assert broken.evict_expired(3600) == 0


# ── Orphaned entries: an index promising a blob that is gone ─────────────────
def test_nearest_skips_and_forgets_the_entry_without_blob(cache, storage):
    """A blob deleted by hand on the bucket: the hit must not break the preview but go back to
    the previous ancestor (or the source) and clean the index."""
    hashes = plan_hashes("src", OPS)
    for h in hashes[:2]:
        cache.mark(h)
        storage.blobs[(cache.bucket, cache.object_key(h))] = b"parquet"
    # step 2's blob vanishes (deleted by hand)
    del storage.blobs[(cache.bucket, cache.object_key(hashes[1]))]
    assert cache.nearest(hashes) == 1  # starts again from step 1, which exists
    assert not cache.has(hashes[1]) and cache.has(hashes[0])
    # step 1 vanishes too → the source, an empty index
    del storage.blobs[(cache.bucket, cache.object_key(hashes[0]))]
    assert cache.nearest(hashes) == 0
    assert not cache.has(hashes[0])


def test_evict_discards_the_orphaned_entries_not_expired(cache, storage):
    hashes = plan_hashes("src", OPS)
    ok, orphan = hashes[0], hashes[1]
    for h in (ok, orphan):
        cache.mark(h)
    storage.blobs[(cache.bucket, cache.object_key(ok))] = b"parquet"  # only `ok` has the blob
    removed = cache.evict_expired(ttl_seconds=3600)  # no expired entry
    assert removed == 1
    assert cache.has(ok) and not cache.has(orphan)
    assert (cache.bucket, cache.object_key(ok)) in storage.blobs  # the good blob stays
