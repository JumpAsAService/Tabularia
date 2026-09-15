"""`ignore_missing` sul sort: solo per il sort INIETTATO dal publish.

Una chiave di ordinamento dichiarata sull'Output e poi sparita dalla catena
(colonna rinominata o tolta a monte) faceva fallire OGNI run di quel flusso — e
non era nemmeno rimuovibile dalla checklist, che mostra solo le colonne che
esistono ancora. Il flag lo mette il gateway; un `sort` chiesto dall'utente resta
severo, altrimenti un refuso passerebbe in silenzio.

Vale su tutti e tre i dialetti toccati: Polars, DuckDB e chDB/ClickHouse.
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


# ── senza il flag: una colonna assente DEVE farsi sentire ───────────────────────
@pytest.mark.parametrize("name", ENGINES)
def test_a_plain_sort_on_a_missing_column_still_fails(name, storage):
    with pytest.raises(Exception):
        _preview(name, storage, [{"type": "sort", "params": {"by": ["sparita"]}}])


# ── col flag: la chiave sparita si ignora, le altre ordinano ────────────────────
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
