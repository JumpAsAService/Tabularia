"""The deferred materialization and the cap of the step cache, the same for every engine:
the preview does not wait for the copy; the deferred task writes it once (a lock); a step
over CACHE__MAX_STEP_ROWS is not cached and is remembered as too large; a run's output is
cached only when it fits under the cap; the Viewer (use_cache=False) leaves nothing
pending.
"""
import importlib.util

import polars as pl
import pytest

from app.engine.base import DataSource
from app.engine.cache import StepCache, plan_hashes
from app.engine.polars_engine import PolarsEngine
from tests.conftest import BUCKET, drain_cache, upload_df
from tests.fakes import FakeRedis, FakeStorage

ENGINES = [
    "polars",
    pytest.param("duckdb", marks=pytest.mark.skipif(importlib.util.find_spec("duckdb") is None, reason="duckdb assente")),
    pytest.param("chdb", marks=pytest.mark.skipif(importlib.util.find_spec("chdb") is None, reason="chdb assente")),
]
FILTER = [{"type": "filter", "params": {"column": "v", "operator": "gt", "value": 2}}]
SORT = [{"type": "sort", "params": {"by": "v"}}]


def _engine(name, storage, cache):
    if name == "polars":
        return PolarsEngine(storage=storage, cache=cache)
    if name == "duckdb":
        from app.engine.duckdb_engine import DuckDBEngine

        return DuckDBEngine(storage=storage, cache=cache)
    from app.engine.chdb_engine import ChdbEngine

    return ChdbEngine(storage=storage, cache=cache)


@pytest.fixture
def cap(monkeypatch):
    """The cache's cap at 5 rows for these tests."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings().cache, "max_step_rows", 5)
    yield 5


@pytest.fixture
def src(storage):
    return upload_df(storage, pl.DataFrame({"v": list(range(1, 11))}), "datasets/defer.parquet")


@pytest.mark.parametrize("name", ENGINES)
def test_preview_answers_first_and_the_task_materializes_once(name, storage, src):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = _engine(name, storage, cache)
    ops = FILTER + SORT
    res = eng.preview(src, ops, limit=10)
    assert res.row_count == 8
    parent = plan_hashes(eng._source_id(src), FILTER)[-1]
    assert not cache.has(parent)
    pending = eng.take_pending()
    assert len(pending) == 1 and pending[0][1] == FILTER
    assert eng.take_pending() == []
    assert eng.materialize(*pending[0]) is True
    assert cache.has(parent) and storage.exists(cache.bucket, cache.object_key(parent))
    # cached already: nothing pending at the next preview, and materialize does not write again
    eng.preview(src, ops, limit=10)
    assert eng.take_pending() == []
    assert eng.materialize(src, FILTER) is False


@pytest.mark.parametrize("name", ENGINES)
def test_steps_over_the_cap_are_not_cached_and_remembered(name, storage, src, cap):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = _engine(name, storage, cache)
    # [filter v > 2] has 8 rows > 5: not cached
    eng.preview(src, FILTER + SORT, limit=10)
    assert drain_cache(eng) == 0
    parent = plan_hashes(eng._source_id(src), FILTER)[-1]
    assert not cache.has(parent) and cache.is_skipped(parent)
    assert not storage.exists(cache.bucket, cache.object_key(parent))
    # remembered: the next preview does not even leave it pending again
    eng.preview(src, FILTER + SORT, limit=10)
    assert eng.take_pending() == []
    # a small step (3 rows) is
    small = [{"type": "filter", "params": {"column": "v", "operator": "gt", "value": 7}}]
    eng.preview(src, small + SORT, limit=10)
    assert drain_cache(eng) == 1
    assert cache.has(plan_hashes(eng._source_id(src), small)[-1])


@pytest.mark.parametrize("name", ENGINES)
def test_run_output_goes_to_cache_only_within_the_cap(name, storage, src, cap):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = _engine(name, storage, cache)
    big = eng.run(src, FILTER, DataSource(bucket=BUCKET, key="out/big.parquet"))
    assert big.rows_written == 8 and storage.exists(BUCKET, "out/big.parquet")
    assert not cache.has(plan_hashes(eng._source_id(src), FILTER)[-1])
    small = [{"type": "filter", "params": {"column": "v", "operator": "gt", "value": 7}}]
    out = eng.run(src, small, DataSource(bucket=BUCKET, key="out/small.parquet"))
    assert out.rows_written == 3
    assert cache.has(plan_hashes(eng._source_id(src), small)[-1])


def test_viewer_leaves_nothing_pending(storage, src):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    eng.preview(src, FILTER + SORT, limit=10, use_cache=False)
    assert eng.take_pending() == []


def test_lock_prevents_a_second_writer(storage, src):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    parent = plan_hashes(eng._source_id(src), FILTER)[-1]
    assert cache.try_lock(parent)  # someone else is writing already
    assert eng.materialize(src, FILTER) is False
    cache.unlock(parent)
    assert eng.materialize(src, FILTER) is True


def test_same_step_pending_once(storage, src):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    eng.preview(src, FILTER + SORT, limit=10)
    eng.preview(src, FILTER + [{"type": "limit", "params": {"n": 2}}], limit=10)
    assert len(eng.take_pending()) == 1  # the same parent, one materialization


def test_no_cap_means_everything_is_cached(storage, src, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings().cache, "max_step_rows", 0)
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    eng.preview(src, FILTER + SORT, limit=10)
    assert drain_cache(eng) == 1


def test_broken_redis_is_fail_open_for_the_lock(storage):
    from tests.fakes import BrokenRedis

    cache = StepCache(storage, redis_client=BrokenRedis())
    assert cache.try_lock("x") is True
    cache.unlock("x")
    cache.mark_skipped("x")
    assert cache.is_skipped("x") is False


@pytest.mark.parametrize("name", ENGINES)
def test_preview_reports_the_cache_state_of_the_parent(name, storage, src, cap):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = _engine(name, storage, cache)
    # no upstream step: nothing to say
    assert eng.preview(src, FILTER, limit=10).cache_state is None
    # a large upstream step: «pending» first, «skipped» with the cap after the task
    r = eng.preview(src, FILTER + SORT, limit=10)
    assert (r.cache_state, r.cache_cap_rows) == ("pending", 5)
    drain_cache(eng)
    assert eng.preview(src, FILTER + SORT, limit=10).cache_state == "skipped"
    # a small step: «hit» after the task
    small = [{"type": "filter", "params": {"column": "v", "operator": "gt", "value": 7}}]
    eng.preview(src, small + SORT, limit=10)
    drain_cache(eng)
    assert eng.preview(src, small + SORT, limit=10).cache_state == "hit"
    # the Viewer: cache not used
    assert eng.preview(src, FILTER + SORT, limit=10, use_cache=False).cache_state == "off"


# ── a preview waits for a copy of its own chain that is being written ────────────
def test_wait_for_running_copy_nothing_running(storage, src):
    from app.engine.stepcache_defer import wait_for_running_copy

    eng = PolarsEngine(storage=storage, cache=StepCache(storage, redis_client=FakeRedis()))
    assert wait_for_running_copy(eng, src, FILTER + SORT) == 0.0
    assert wait_for_running_copy(eng, src, []) == 0.0


def test_wait_for_running_copy_waits_until_written(storage, src, monkeypatch):
    """A Pivot clicked while the copy of its upstream steps runs: it waits for the copy,
    then starts from the cache (it does not compute the same rows beside it)."""
    from app.engine import stepcache_defer

    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    parent = plan_hashes(eng._source_id(src), FILTER)[-1]
    assert cache.try_lock(parent)
    sleeps = []

    def fake_sleep(s):
        # the copy finishes during the third poll
        sleeps.append(s)
        if len(sleeps) == 3:
            cache.unlock(parent)
            with eng._materialize_session(src) as ctx:
                eng._materialize(ctx, src, [stepcache_defer._coerce(FILTER)[0]])

    monkeypatch.setattr(stepcache_defer.time, "sleep", fake_sleep)
    stepcache_defer.wait_for_running_copy(eng, src, FILTER + SORT)
    assert len(sleeps) == 3 and cache.has(parent)
    res = eng.preview(src, FILTER + SORT, limit=10)
    assert res.row_count == 8 and eng.take_pending() == []  # started from the copy


def test_wait_for_running_copy_stops_when_the_copy_gives_up(storage, src, monkeypatch):
    """A copy that ends without writing (too large, failed) releases its lock: the
    preview goes on from the source."""
    from app.engine import stepcache_defer

    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    parent = plan_hashes(eng._source_id(src), FILTER)[-1]
    cache.try_lock(parent)
    monkeypatch.setattr(stepcache_defer.time, "sleep", lambda s: cache.unlock(parent))
    stepcache_defer.wait_for_running_copy(eng, src, FILTER + SORT)
    assert not cache.has(parent) and not cache.is_locked(parent)


def test_wait_for_running_copy_is_bounded(storage, src, monkeypatch):
    from app.engine import stepcache_defer

    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    cache.try_lock(plan_hashes(eng._source_id(src), FILTER)[-1])
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(stepcache_defer.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(stepcache_defer.time, "sleep", lambda s: None)
    waited = stepcache_defer.wait_for_running_copy(eng, src, FILTER + SORT, max_seconds=30)
    assert 30 <= waited <= 50


def test_wait_ignores_a_copy_below_a_cached_step(storage, src):
    """A lock on a step shorter than one cached already: nothing to wait for."""
    from app.engine.stepcache_defer import wait_for_running_copy

    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    assert eng.materialize(src, FILTER + SORT) is True
    cache.try_lock(plan_hashes(eng._source_id(src), FILTER)[-1])
    assert wait_for_running_copy(eng, src, FILTER + SORT + [{"type": "limit", "params": {"n": 3}}]) == 0.0


def test_is_locked_fail_open(storage):
    import redis as _redis

    class Broken(FakeRedis):
        def exists(self, *a):
            raise _redis.RedisError("down")

    assert StepCache(storage, redis_client=Broken()).is_locked("x") is False
