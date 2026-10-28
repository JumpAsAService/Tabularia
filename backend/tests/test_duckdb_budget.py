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
    real = duckdb_budget._budget_for  # tests may replace budget(); teardown order is not ours to pick
    real.cache_clear()
    yield
    real.cache_clear()


def _share(monkeypatch, cpus=8):
    """per_process_kb as the worker computes it: a 4 GiB container × 0.75, shared."""
    from app.tasks import worker_pool

    monkeypatch.setattr(worker_pool, "per_process_kb", lambda processes: 4 * 1024 * 1024 * 3 // 4 // processes)
    monkeypatch.setattr(duckdb_budget, "_container_cpus", lambda: cpus)


def test_the_budget_is_the_childs_share(monkeypatch):
    _share(monkeypatch)
    monkeypatch.setattr(config.get_settings().celery, "worker_concurrency", 4)
    assert duckdb_budget.budget() == (768 - duckdb_budget.PROCESS_OVERHEAD_MB, 2)


def test_a_tiny_share_still_leaves_a_floor(monkeypatch):
    _share(monkeypatch, cpus=1)
    monkeypatch.setattr(config.get_settings().celery, "worker_concurrency", 16)
    assert duckdb_budget.budget() == (duckdb_budget.MIN_MEMORY_MB, 1)


@pytest.mark.parametrize("argv, key", [
    (["celery", "-A", "app.tasks.celery_app", "worker", "-E", "-Q", "celery"], "workers.run_concurrency"),
    (["celery", "-A", "app.tasks.celery_app", "worker", "-E", "--queues=preview"], "workers.preview_concurrency"),
])
def test_the_panels_pool_size_is_the_share(monkeypatch, isolated_panel_settings, argv, key):
    """Admin → Workers grows a pool started with 2 processes to 6: every connection gets a
    sixth, not a half (six halves of the container: the kernel killed it whole)."""
    from app.core import panel_settings

    _share(monkeypatch)
    monkeypatch.setattr(duckdb_budget.sys, "argv", argv)
    monkeypatch.setattr(config.get_settings().celery, "worker_concurrency", 2)
    assert duckdb_budget.budget() == (1536 - duckdb_budget.PROCESS_OVERHEAD_MB, 4)
    panel_settings.write({key: 6})
    panel_settings.forget()
    assert duckdb_budget.budget() == (512 - duckdb_budget.PROCESS_OVERHEAD_MB, 1)


@pytest.mark.parametrize("text, expected", [
    ("50Gi", 50 * 2**30), ("500M", 500 * 10**6), ("1.5Gi", int(1.5 * 2**30)), ("1024", 1024), ("", None),
    ("lots", None),
])
def test_a_kubernetes_quantity(text, expected):
    assert duckdb_budget.quantity_bytes(text) == expected


def test_duckdb_spills_within_the_pods_tmp(monkeypatch):
    """Past the emptyDir's sizeLimit the kubelet evicts the pod at once: DuckDB spilled up to
    90% of the node's free disk, not of /tmp."""
    import duckdb

    monkeypatch.setattr(config.get_settings().celery, "worker_concurrency", 2)
    monkeypatch.setattr(duckdb_budget.sys, "argv", ["celery", "worker", "-Q", "celery"])
    monkeypatch.setenv("TMP_SIZE_LIMIT", "8Gi")
    assert duckdb_budget.spill_mib() == 2048  # half of 8 GiB, for two processes
    with_contract = duckdb.connect(":memory:")
    duckdb_budget.apply(with_contract)
    assert with_contract.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0] in ("2.0 GiB", "2.1 GB")
    monkeypatch.delenv("TMP_SIZE_LIMIT")
    assert duckdb_budget.spill_mib() is None


def test_the_cpus_of_a_cgroup_v1_container(monkeypatch):
    files = {"/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "300000\n", "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000\n"}

    def fake_open(path, *a, **kw):
        if path in files:
            import io
            return io.StringIO(files[path])
        raise FileNotFoundError(path)
    monkeypatch.setattr(duckdb_budget.os, "cpu_count", lambda: 16)
    monkeypatch.setattr("builtins.open", fake_open)
    assert duckdb_budget._container_cpus() == 3


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
    with_contract = DuckDBEngine(storage=object())._connection(tmp)
    try:
        n, total = with_contract.execute(
            "SELECT count(*), sum(s) FROM (SELECT i % 400000 AS g, sum(i) AS s FROM range(4000000) t(i) GROUP BY g)"
        ).fetchone()
        return int(n), int(total)
    finally:
        with_contract.close()
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
