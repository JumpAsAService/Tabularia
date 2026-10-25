"""`ignore_missing` on the sort: only for the sort the publish INJECTS.

A sort key declared on the Output and then gone from the chain (a column renamed or
removed upstream) failed EVERY run of that flow — and could not even be removed from the
checklist, which shows only the columns that still exist. The gateway sets the flag; a
`sort` the user asks for stays strict, otherwise a typo would pass silently.

On all three dialects touched: Polars, DuckDB and chDB/ClickHouse.
"""
from __future__ import annotations

import importlib.util

import polars as pl
import pytest

from app.engine.cache import StepCache
from app.engine.polars_engine import PolarsEngine
from tests.conftest import upload_df
from tests.fakes import FakeRedis

DF = pl.DataFrame({"id": [3, 1, 2], "nome": ["c", "a", "b"]})

ENGINES = ["polars"]
if importlib.util.find_spec("duckdb"):
    ENGINES.append("duckdb")
if importlib.util.find_spec("chdb"):
    ENGINES.append("chdb")


def _engine(name: str, storage):
    cache = StepCache(storage, redis_client=FakeRedis())
    if name == "polars":
        return PolarsEngine(storage=storage, cache=cache)
    if name == "duckdb":
        from app.engine.duckdb_engine import DuckDBEngine

        return DuckDBEngine(storage=storage, cache=cache)
    from app.engine.chdb_engine import ChdbEngine

    return ChdbEngine(storage=storage, cache=cache)


def _preview(name, storage, ops):
    src = upload_df(storage, DF, "datasets/sortkeys.parquet")
    return _engine(name, storage).preview(src, ops, limit=10, use_cache=False)


# ── without the flag: a missing column MUST be heard ───────────────────────
@pytest.mark.parametrize("name", ENGINES)
def test_a_plain_sort_on_a_missing_column_still_fails(name, storage):
    with pytest.raises(Exception):
        _preview(name, storage, [{"type": "sort", "params": {"by": ["sparita"]}}])


# ── with the flag: the key gone is ignored, the others sort ────────────────────
@pytest.mark.parametrize("name", ENGINES)
def test_ignore_missing_drops_the_vanished_key(name, storage):
    res = _preview(name, storage, [
        {"type": "sort", "params": {"by": ["sparita", "id"], "ignore_missing": True}}
    ])
    assert [r["id"] for r in res.rows] == [1, 2, 3], f"{name}: ordina per id, ignora 'sparita'"


@pytest.mark.parametrize("name", ENGINES)
def test_ignore_missing_with_no_surviving_key_is_a_no_op(name, storage):
    res = _preview(name, storage, [
        {"type": "sort", "params": {"by": ["sparita"], "ignore_missing": True}}
    ])
    assert len(res.rows) == 3, f"{name}: nessun ordinamento, ma nemmeno un errore"


@pytest.mark.parametrize("name", ENGINES)
def test_a_normal_sort_still_sorts_with_the_flag(name, storage):
    res = _preview(name, storage, [
        {"type": "sort", "params": {"by": ["nome"], "ignore_missing": True}}
    ])
    assert [r["nome"] for r in res.rows] == ["a", "b", "c"], name
