"""The `sample` operation (a DETERMINISTIC random sample of a fraction of the rows) on
every engine, and the tolerance of the `_dev_sample` marker on the existing ops.

The engines use different hashes → not the same rows across engines (it is a development
sample), but on each: size ≈ fraction, the same rows at every run (a fixed seed), an
invalid fraction → a clear error.
"""
from __future__ import annotations

import importlib.util
import os

import polars as pl
import pytest

from app.engine.cache import StepCache
from app.engine.exceptions import EngineError
from app.engine.polars_engine import PolarsEngine
from tests.conftest import upload_df
from tests.fakes import FakeRedis


def _engine(name: str, storage):
    cache = StepCache(storage, redis_client=FakeRedis())
    if name == "polars":
        return PolarsEngine(storage=storage, cache=cache)
    if name == "duckdb":
        from app.engine.duckdb_engine import DuckDBEngine

        return DuckDBEngine(storage=storage, cache=cache)
    if name == "chdb":
        from app.engine.chdb_engine import ChdbEngine

        return ChdbEngine(storage=storage, cache=cache)
    if name == "clickhouse":
        from app.core.config import ClickHouseExternalSettings
        from app.engine.clickhouse_engine import ClickHouseEngine

        cfg = ClickHouseExternalSettings(
            host=os.getenv("CLICKHOUSE_TEST_HOST", ""), port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
            username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""),
            database=os.getenv("CLICKHOUSE_TEST_DB", "default"), transport="push",
        )
        return ClickHouseEngine(storage=storage, cache=cache, cfg=cfg)
    raise AssertionError(name)


ENGINES = [
    "polars",
    pytest.param("duckdb", marks=pytest.mark.skipif(importlib.util.find_spec("duckdb") is None, reason="duckdb assente")),
    pytest.param("chdb", marks=pytest.mark.skipif(importlib.util.find_spec("chdb") is None, reason="chdb assente")),
    pytest.param("clickhouse", marks=pytest.mark.skipif(not os.getenv("CLICKHOUSE_TEST_HOST"), reason="serve CLICKHOUSE_TEST_HOST")),
]

N = 4000


@pytest.fixture
def big(storage):
    df = pl.DataFrame({"id": range(N), "grp": [i % 7 for i in range(N)], "txt": [f"r{i}" for i in range(N)]})
    return upload_df(storage, df, "datasets/sample_src.parquet")


@pytest.mark.parametrize("name", ENGINES)
def test_sample_fraction_and_determinism(storage, big, name):
    eng = _engine(name, storage)
    ops = [{"type": "sample", "params": {"fraction": 0.25, "seed": 42, "_dev_sample": True}}]
    a = eng.preview(big, ops, limit=N, use_cache=False)
    b = eng.preview(big, ops, limit=N, use_cache=False)
    assert N * 0.25 * 0.75 <= a.row_count <= N * 0.25 * 1.25, f"{name}: {a.row_count} righe su {N} al 25%"
    assert sorted(r["id"] for r in a.rows) == sorted(r["id"] for r in b.rows)  # the same seed → the same rows
    assert {c.name for c in a.columns} == {"id", "grp", "txt"}  # no service column


@pytest.mark.parametrize("name", ENGINES)
def test_sample_then_ops_and_marked_limit(storage, big, name):
    eng = _engine(name, storage)
    # the `_dev_sample` marker is ignored by the operations (limit included)
    ops = [
        {"type": "limit", "params": {"n": 100, "_dev_sample": True}},
        {"type": "group_by", "params": {"by": ["grp"], "aggregations": [{"column": "id", "func": "count", "alias": "n"}]}},
    ]
    res = eng.preview(big, ops, limit=50, use_cache=False)
    assert sum(r["n"] for r in res.rows) == 100


def test_sample_rejects_bad_fraction(storage, big):
    eng = _engine("polars", storage)
    for bad in (0, 1.5, -0.1, "x"):
        with pytest.raises(EngineError):
            eng.preview(big, [{"type": "sample", "params": {"fraction": bad}}], limit=10, use_cache=False)
