"""Materializzazione differita e tetto della step-cache, uguali per tutti i
motori: la preview non aspetta la copia; il task differito la scrive una volta
sola (lucchetto); un passo oltre CACHE__MAX_STEP_ROWS non va in cache e viene
ricordato come troppo grande; l'output di un run va in cache solo se sta nel
tetto; il Viewer (use_cache=False) non lascia nulla in sospeso.
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
FILTRO = [{"type": "filter", "params": {"column": "v", "operator": "gt", "value": 2}}]
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
    """Tetto della cache a 5 righe per questi test."""
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
    ops = FILTRO + SORT
    res = eng.preview(src, ops, limit=10)
    assert res.row_count == 8
    parent = plan_hashes(eng._source_id(src), FILTRO)[-1]
    assert not cache.has(parent)
    pending = eng.take_pending()
    assert len(pending) == 1 and pending[0][1] == FILTRO
    assert eng.take_pending() == []
    assert eng.materialize(*pending[0]) is True
    assert cache.has(parent) and storage.exists(cache.bucket, cache.object_key(parent))
    # gia' in cache: niente in sospeso alla prossima preview, e materialize non riscrive
    eng.preview(src, ops, limit=10)
    assert eng.take_pending() == []
    assert eng.materialize(src, FILTRO) is False


@pytest.mark.parametrize("name", ENGINES)
def test_steps_over_the_cap_are_not_cached_and_remembered(name, storage, src, cap):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = _engine(name, storage, cache)
    # [filter v > 2] ha 8 righe > 5: non va in cache
    eng.preview(src, FILTRO + SORT, limit=10)
    assert drain_cache(eng) == 0
    parent = plan_hashes(eng._source_id(src), FILTRO)[-1]
    assert not cache.has(parent) and cache.is_skipped(parent)
    assert not storage.exists(cache.bucket, cache.object_key(parent))
    # ricordato: la prossima preview non lo rimette nemmeno in sospeso
    eng.preview(src, FILTRO + SORT, limit=10)
    assert eng.take_pending() == []
    # un passo piccolo (3 righe) invece si'
    small = [{"type": "filter", "params": {"column": "v", "operator": "gt", "value": 7}}]
    eng.preview(src, small + SORT, limit=10)
    assert drain_cache(eng) == 1
    assert cache.has(plan_hashes(eng._source_id(src), small)[-1])


@pytest.mark.parametrize("name", ENGINES)
def test_run_output_goes_to_cache_only_within_the_cap(name, storage, src, cap):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = _engine(name, storage, cache)
    big = eng.run(src, FILTRO, DataSource(bucket=BUCKET, key="out/big.parquet"))
    assert big.rows_written == 8 and storage.exists(BUCKET, "out/big.parquet")
    assert not cache.has(plan_hashes(eng._source_id(src), FILTRO)[-1])
    small = [{"type": "filter", "params": {"column": "v", "operator": "gt", "value": 7}}]
    out = eng.run(src, small, DataSource(bucket=BUCKET, key="out/small.parquet"))
    assert out.rows_written == 3
    assert cache.has(plan_hashes(eng._source_id(src), small)[-1])


def test_viewer_leaves_nothing_pending(storage, src):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    eng.preview(src, FILTRO + SORT, limit=10, use_cache=False)
    assert eng.take_pending() == []


def test_lock_prevents_a_second_writer(storage, src):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    parent = plan_hashes(eng._source_id(src), FILTRO)[-1]
    assert cache.try_lock(parent)  # qualcun altro sta gia' scrivendo
    assert eng.materialize(src, FILTRO) is False
    cache.unlock(parent)
    assert eng.materialize(src, FILTRO) is True


def test_same_step_pending_once(storage, src):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    eng.preview(src, FILTRO + SORT, limit=10)
    eng.preview(src, FILTRO + [{"type": "limit", "params": {"n": 2}}], limit=10)
    assert len(eng.take_pending()) == 1  # stesso parent, una sola materializzazione


def test_no_cap_means_everything_is_cached(storage, src, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings().cache, "max_step_rows", 0)
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = PolarsEngine(storage=storage, cache=cache)
    eng.preview(src, FILTRO + SORT, limit=10)
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
    # nessun passo a monte: niente da dire
    assert eng.preview(src, FILTRO, limit=10).cache_state is None
    # passo a monte grande: prima «pending», dopo il task «skipped» con il tetto
    r = eng.preview(src, FILTRO + SORT, limit=10)
    assert (r.cache_state, r.cache_cap_rows) == ("pending", 5)
    drain_cache(eng)
    assert eng.preview(src, FILTRO + SORT, limit=10).cache_state == "skipped"
    # passo piccolo: dopo il task e' «hit»
    small = [{"type": "filter", "params": {"column": "v", "operator": "gt", "value": 7}}]
    eng.preview(src, small + SORT, limit=10)
    drain_cache(eng)
    assert eng.preview(src, small + SORT, limit=10).cache_state == "hit"
    # il Viewer: cache non usata
    assert eng.preview(src, FILTRO + SORT, limit=10, use_cache=False).cache_state == "off"
