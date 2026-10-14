"""DuckDB works within its process's share, not the whole container.

Every Celery child opens its own connections, and DuckDB assumed it owned 80% of
the container and every CPU: a few heavy previews at once were killed by the kernel.
Each connection now gets the child's share and its own spill directory: DuckDB names
its spill files the same in every instance, so connections spilling into a shared
directory read each other's blocks and returned wrong sums.
"""
import multiprocessing
import os
import tempfile

import polars as pl
import pytest

from app.core import config
from app.engine import duckdb_budget
from app.engine.cache import StepCache
from app.engine.duckdb_engine import DuckDBEngine
from tests.conftest import upload_df
from tests.fakes import FakeRedis


@pytest.fixture(autouse=True)
def fresh_budget():
    real = duckdb_budget.budget  # tests may replace it; teardown order is not ours to pick
    real.cache_clear()
    yield
    real.cache_clear()


def test_the_budget_is_the_childs_share(monkeypatch):
    monkeypatch.setattr(duckdb_budget, "resolve_max_memory_per_child_kb", lambda celery: 786_432)  # 4 GiB × 0.75 / 4
    monkeypatch.setattr(duckdb_budget, "_container_cpus", lambda: 8)
    monkeypatch.setattr(config.get_settings().celery, "worker_concurrency", 4)
    assert duckdb_budget.budget() == (768 - duckdb_budget.PROCESS_OVERHEAD_MB, 2)


def test_a_tiny_share_still_leaves_a_floor(monkeypatch):
    monkeypatch.setattr(duckdb_budget, "resolve_max_memory_per_child_kb", lambda celery: 200_000)
    monkeypatch.setattr(duckdb_budget, "_container_cpus", lambda: 1)
    assert duckdb_budget.budget() == (duckdb_budget.MIN_MEMORY_MB, 1)


def test_the_engine_connection_uses_it_and_its_own_spill_directory(storage, monkeypatch):
    monkeypatch.setattr(duckdb_budget, "budget", lambda: (300, 1))
    engine = DuckDBEngine(storage=storage)
    tmp_a: list[str] = []
    tmp_b: list[str] = []
    a, b = engine._connection(tmp_a), engine._connection(tmp_b)
    try:
        assert a.execute("SELECT current_setting('memory_limit')").fetchone()[0] == "300.0 MiB"
        assert a.execute("SELECT current_setting('threads')").fetchone()[0] == 1
        spill_a = a.execute("SELECT current_setting('temp_directory')").fetchone()[0]
        spill_b = b.execute("SELECT current_setting('temp_directory')").fetchone()[0]
        assert spill_a != spill_b and spill_a in tmp_a and os.path.isdir(spill_a)
    finally:
        a.close()
        b.close()
        DuckDBEngine._cleanup(tmp_a)
        DuckDBEngine._cleanup(tmp_b)
    assert not os.path.exists(spill_a)  # removed with the request's other files


def _spill_and_sum(_):
    duckdb_budget.budget = lambda: (64, 1)  # in a forked child: small enough to spill
    tmp: list[str] = []
    con = DuckDBEngine(storage=object())._connection(tmp)
    try:
        n, total = con.execute(
            "SELECT count(*), sum(s) FROM (SELECT i % 400000 AS g, sum(i) AS s FROM range(4000000) t(i) GROUP BY g)"
        ).fetchone()
        return int(n), int(total)
    finally:
        con.close()
        DuckDBEngine._cleanup(tmp)


def test_two_processes_spilling_at_once_get_the_right_answer():
    expected = (400_000, sum(range(4_000_000)))
    with multiprocessing.get_context("fork").Pool(2) as pool:
        assert pool.map(_spill_and_sum, [0, 1]) == [expected, expected]


def test_the_sql_sandbox_works_on_disk_and_leaves_nothing_behind(storage):
    src = upload_df(storage, pl.DataFrame({"x": [1, 2, 3, 4]}), "datasets/budget_sql.parquet")
    engine = DuckDBEngine(storage=storage, cache=StepCache(storage, redis_client=FakeRedis()))
    before = {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("duckdb-")}
    res = engine.preview(src, [{"type": "sql", "params": {"query": "SELECT count(*) AS n FROM self"}}], limit=5)
    assert res.rows[0]["n"] == 4
    assert {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("duckdb-")} == before
