"""Parità CROSS-ENGINE di pivot/unpivot (Polars, DuckDB, chDB, ClickHouse esterno).

Con engine di sviluppo ≠ engine di produzione lo stesso flusso deve produrre
le STESSE colonne e gli stessi valori ovunque: i nodi a valle referenziano le
colonne del pivot per nome. Polars è il riferimento; ogni altro engine
disponibile deve coincidere (nomi colonna, righe, famiglia di tipo).

DuckDB/chDB girano se i pacchetti sono installati (nel container sì);
l'engine ClickHouse esterno se `CLICKHOUSE_TEST_HOST` è impostata (vedi
test_clickhouse_engine.py).
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os

import polars as pl
import pytest

from app.engine.cache import StepCache
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
            host=os.getenv("CLICKHOUSE_TEST_HOST", ""),
            port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
            username=os.getenv("CLICKHOUSE_TEST_USER", "default"),
            password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""),
            database=os.getenv("CLICKHOUSE_TEST_DB", "default"),
            transport="push",
        )
        return ClickHouseEngine(storage=storage, cache=cache, cfg=cfg)
    raise AssertionError(name)


OTHER_ENGINES = [
    pytest.param("duckdb", marks=pytest.mark.skipif(importlib.util.find_spec("duckdb") is None, reason="duckdb assente")),
    pytest.param("chdb", marks=pytest.mark.skipif(importlib.util.find_spec("chdb") is None, reason="chdb assente")),
    pytest.param("clickhouse", marks=pytest.mark.skipif(not os.getenv("CLICKHOUSE_TEST_HOST"), reason="serve CLICKHOUSE_TEST_HOST")),
]


@pytest.fixture
def src(storage):
    df = pl.DataFrame({
        "paese": ["IT", "IT", "FR", "FR", "DE"],
        "anno": [2023, 2024, 2023, 2024, 2024],
        "canale": ["web", "shop", "web", "web", "shop"],
        "vendite": [10.5, 20.0, 5.0, None, 7.0],  # FR/2024 esiste ma è NULL
        "qta": [1, 2, 3, 4, 10],  # 10: l'ordine delle etichette è testuale ("10" < "2")
        "giorno": [dt.date(2024, 1, i + 1) for i in range(5)],
        "attivo": [True, False, True, None, False],
    })
    return upload_df(storage, df, "datasets/reshape_parity.parquet")


CASES = {
    "pivot_sum_null_group": [{"type": "pivot", "params": {"index": ["paese"], "on": "anno", "values": "vendite", "func": "sum"}}],
    "pivot_mean": [{"type": "pivot", "params": {"index": ["paese"], "on": "anno", "values": "vendite", "func": "mean"}}],
    "pivot_count": [{"type": "pivot", "params": {"index": ["paese"], "on": "anno", "values": "vendite", "func": "count"}}],
    "pivot_max_int": [{"type": "pivot", "params": {"index": ["paese"], "on": "anno", "values": "qta", "func": "max"}}],
    "pivot_first": [{"type": "pivot", "params": {"index": ["paese"], "on": "anno", "values": "qta", "func": "first"}}],
    "pivot_multi_on": [{"type": "pivot", "params": {"index": ["paese"], "on": ["anno", "canale"], "values": "qta", "func": "sum"}}],
    "pivot_on_date": [{"type": "pivot", "params": {"index": ["paese"], "on": "giorno", "values": "qta", "func": "sum"}}],
    "pivot_on_bool_null": [{"type": "pivot", "params": {"index": ["paese"], "on": "attivo", "values": "qta", "func": "sum"}}],
    "pivot_on_int_text_order": [{"type": "pivot", "params": {"index": ["paese"], "on": "qta", "values": "anno", "func": "max"}}],
    "pivot_multi_index": [{"type": "pivot", "params": {"index": ["paese", "canale"], "on": "anno", "values": "qta", "func": "sum"}}],
    "unpivot_numeric_super": [{"type": "unpivot", "params": {"index": ["paese"], "on": ["vendite", "qta"], "variable_name": "m", "value_name": "v"}}],
    "unpivot_ints": [{"type": "unpivot", "params": {"index": ["paese"], "on": ["anno", "qta"]}}],
    "unpivot_no_index": [{"type": "unpivot", "params": {"on": ["vendite", "qta"]}}],
    "unpivot_all_mixed": [{"type": "unpivot", "params": {"index": ["paese", "anno"]}}],
    "pivot_then_filter": [
        {"type": "pivot", "params": {"index": ["paese"], "on": "anno", "values": "qta", "func": "sum"}},
        {"type": "filter", "params": {"column": "2024", "operator": "gt", "value": 1}},
    ],
}


def _family(dtype: str) -> str:
    d = dtype.lower()
    if d.startswith(("int", "uint")):
        return "int"
    if d.startswith(("float", "decimal")):
        return "float"
    if d.startswith("datetime"):
        return "datetime"
    if d.startswith("date"):
        return "date"
    if d in ("string", "utf8", "str"):
        return "str"
    if d in ("boolean", "bool"):
        return "bool"
    return d


def _norm_value(v):
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 9)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return str(v)


def _snapshot(res):
    cols = [c.name for c in res.columns]
    rows = sorted((tuple(_norm_value(r.get(c)) for c in cols) for r in res.rows), key=lambda t: tuple(map(str, t)))
    return cols, rows, [_family(c.dtype) for c in res.columns]


@pytest.mark.parametrize("case", list(CASES))
@pytest.mark.parametrize("other", OTHER_ENGINES)
def test_reshape_matches_polars(storage, src, case, other):
    ops = CASES[case]
    ref = _snapshot(_engine("polars", storage).preview(src, ops, limit=100))
    got = _snapshot(_engine(other, storage).preview(src, ops, limit=100))
    assert got[0] == ref[0], f"{case} [{other}]: colonne diverse"
    assert got[1] == ref[1], f"{case} [{other}]: righe diverse"
    assert got[2] == ref[2], f"{case} [{other}]: tipi diversi"


def test_pivot_standard_on_polars(storage, src):
    """Lo standard stesso, sul riferimento: etichette, NULL, count → 0/Int64."""
    eng = _engine("polars", storage)
    res = eng.preview(src, CASES["pivot_sum_null_group"], limit=100)
    assert [c.name for c in res.columns] == ["paese", "2023", "2024"]
    by = {r["paese"]: r for r in res.rows}
    assert by["DE"]["2023"] is None  # gruppo assente
    assert by["FR"]["2024"] is None  # gruppo presente ma tutto NULL: NULL, non 0
    res = eng.preview(src, CASES["pivot_count"], limit=100)
    by = {r["paese"]: r for r in res.rows}
    assert by["DE"]["2023"] == 0 and by["FR"]["2024"] == 0
    assert all(c.dtype == "Int64" for c in res.columns if c.name != "paese")
    res = eng.preview(src, CASES["pivot_multi_on"], limit=100)
    assert [c.name for c in res.columns] == ["paese", "2023_web", "2024_shop", "2024_web"]
    res = eng.preview(src, CASES["pivot_on_bool_null"], limit=100)
    assert [c.name for c in res.columns] == ["paese", "false", "null", "true"]
    res = eng.preview(src, CASES["pivot_on_int_text_order"], limit=100)
    assert [c.name for c in res.columns] == ["paese", "1", "10", "2", "3", "4"]
