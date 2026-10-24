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


@pytest.fixture
def repeats(storage):
    df = pl.DataFrame({"regione": ["ER", "ER", "ER", "ER", "LO"], "prodotto": ["a", "a", "a", "b", "a"],
                       "cliente": [1, 1, 2, 2, 7], "id": [1, 2, 3, 4, 5], "nome": ["x", "y", "z", "w", "v"]})
    return upload_df(storage, df, "datasets/reshape_repeats.parquet")


@pytest.mark.parametrize("other", OTHER_ENGINES)
def test_pivot_distinct_count_counts_distinct_values(storage, repeats, other):
    # DuckDB's pivot counted every row (3 for ER/a), Polars and ClickHouse the distinct ones (2)
    ops = [{"type": "pivot", "params": {"index": ["regione"], "on": "prodotto", "values": "cliente", "func": "n_unique"}}]
    ref = _snapshot(_engine("polars", storage).preview(repeats, ops, limit=100))
    assert dict((r[0], r[1]) for r in ref[1])["ER"] == 2
    assert _snapshot(_engine(other, storage).preview(repeats, ops, limit=100))[1] == ref[1]


@pytest.mark.parametrize("engine", [pytest.param("polars")] + OTHER_ENGINES)
def test_strict_union_puts_columns_together_by_name(storage, repeats, engine):
    """Two inputs with the same columns in another order: by name, never by position (DuckDB
    and ClickHouse put the names of the products in the ids, without a word)."""
    right = upload_df(storage, pl.DataFrame({"nome": ["q"], "id": [9]}), "datasets/union_right_order.parquet")
    left_ops = [{"type": "select", "params": {"columns": ["id", "nome"]}}]
    ops = left_ops + [{"type": "union", "params": {"right": {"bucket": right.bucket, "key": right.key}, "strategy": "strict"}}]
    res = _engine(engine, storage).preview(repeats, ops, limit=100)
    assert [c.name for c in res.columns] == ["id", "nome"] and _family(res.columns[0].dtype) == "int"
    assert sorted((r["id"], r["nome"]) for r in res.rows)[-1] == (9, "q")


@pytest.mark.parametrize("engine", [pytest.param("polars")] + OTHER_ENGINES)
def test_strict_union_of_other_columns_says_which(storage, repeats, engine):
    from app.engine.exceptions import EngineError

    right = upload_df(storage, pl.DataFrame({"id": [9], "descrizione": ["q"]}), "datasets/union_right_other.parquet")
    ops = [{"type": "select", "params": {"columns": ["id", "nome"]}},
           {"type": "union", "params": {"right": {"bucket": right.bucket, "key": right.key}, "strategy": "strict"}}]
    with pytest.raises(EngineError, match="missing on the right: nome; only on the right: descrizione"):
        _engine(engine, storage).preview(repeats, ops, limit=100)


@pytest.mark.parametrize("engine", [pytest.param("polars")] + OTHER_ENGINES)
def test_text_to_bool_reads_the_same_words_everywhere(storage, engine):
    # Polars refused the cast, ClickHouse read «true» as NULL and «2» as 2
    df = pl.DataFrame({"t": ["true", " Yes ", "1", "T", "n", "FALSE", "0", "no", "maybe", "2", None], "i": list(range(11))})
    src = upload_df(storage, df, "datasets/cast_bool.parquet")
    ops = [{"type": "cast", "params": {"columns": {"t": "bool"}}}]
    res = _engine(engine, storage).preview(src, ops, limit=100)
    got = [r["t"] for r in sorted(res.rows, key=lambda r: r["i"])]
    assert got == [True, True, True, True, False, False, False, False, None, None, None]
    assert _family([c for c in res.columns if c.name == "t"][0].dtype) == "bool"


@pytest.mark.parametrize("engine", [pytest.param("polars")] + OTHER_ENGINES)
def test_not_in_drops_empty_values_everywhere(storage, engine):
    """«is not one of» on a row without a value: SQL's NULL, the row goes, as on Polars and
    DuckDB (ClickHouse's NOT IN kept it)."""
    df = pl.DataFrame({"r": ["ER", "LO", None, "VE"], "i": [1, 2, 3, 4]})
    src = upload_df(storage, df, "datasets/not_in_null.parquet")
    ops = [{"type": "filter", "params": {"column": "r", "operator": "not_in", "value": ["LO"]}}]
    assert sorted(r["i"] for r in _engine(engine, storage).preview(src, ops, limit=100).rows) == [1, 4]


@pytest.fixture
def many(storage):
    import numpy as np

    rng = np.random.default_rng(3)
    n = 400_000
    df = pl.DataFrame({"g": rng.integers(0, 10, n), "riga": np.arange(n), "v": rng.integers(0, 50, n)})
    return upload_df(storage, df, "datasets/order_many.parquet")


SORTED_CASES = {
    "first_last_after_sort": [
        {"type": "sort", "params": {"by": ["riga"], "descending": True}},
        {"type": "group_by", "params": {"by": ["g"], "aggregations": [
            {"column": "riga", "func": "first", "alias": "f"}, {"column": "riga", "func": "last", "alias": "l"}]}}],
    "unique_after_sort": [
        {"type": "sort", "params": {"by": ["riga"], "descending": True}},
        {"type": "unique", "params": {"subset": ["g"]}},
        {"type": "select", "params": {"columns": ["g", "riga"]}}],
    "first_after_sort_and_filter": [
        {"type": "sort", "params": {"by": ["v", "riga"], "descending": False}},
        {"type": "filter", "params": {"column": "v", "operator": "gt", "value": 10}},
        {"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": "riga", "func": "first", "alias": "f"}]}}],
}


@pytest.mark.parametrize("case", list(SORTED_CASES))
@pytest.mark.parametrize("engine", [pytest.param("polars")] + OTHER_ENGINES)
def test_first_last_unique_follow_a_sort_before_them(storage, many, case, engine):
    """«first», «last» and «Unique» after a sort take the rows in that order, on every engine."""
    df = pl.read_parquet(f"{many.bucket}/{many.key}") if False else None  # noqa: F841
    ops = SORTED_CASES[case]
    got = sorted(tuple(r.values()) for r in _engine(engine, storage).preview(many, ops, limit=100).rows)
    import numpy as np  # the expected rows, from the data itself

    rng = np.random.default_rng(3)
    n = 400_000
    ref = pl.DataFrame({"g": rng.integers(0, 10, n), "riga": np.arange(n), "v": rng.integers(0, 50, n)})
    if case == "first_last_after_sort":
        want = ref.group_by("g").agg(pl.col("riga").max().alias("f"), pl.col("riga").min().alias("l"))
    elif case == "unique_after_sort":
        want = ref.group_by("g").agg(pl.col("riga").max())
    else:
        r = ref.filter(pl.col("v") > 10).sort(["v", "riga"])
        want = r.group_by("g", maintain_order=True).agg(pl.col("riga").first().alias("f"))
    assert got == sorted(want.iter_rows()), case


@pytest.fixture
def mixed(storage):
    df = pl.DataFrame({
        "regione": ["Nord", "Sud", "Nord", "Sud", None, "Nord", "Sud"],
        "importo": [10, 5, 30, None, 7, 30, 5],
        "id": [1, 2, 3, 4, 5, 6, 7],
    })
    return upload_df(storage, df, "datasets/sort_mixed.parquet")


SORT_CASES = {
    # a direction each: regione up, importo down, id up; NULLs last in both directions
    "per_column": ({"by": ["regione", "importo", "id"], "descending": [False, True, False]}, [3, 6, 1, 2, 7, 4, 5]),
    # one yes/no for all, as the flows saved before
    "one_for_all": ({"by": ["regione", "id"], "descending": True}, [7, 4, 2, 6, 3, 1, 5]),
    # a list shorter than the columns: the rest ascending
    "short_list": ({"by": ["importo", "id"], "descending": [True]}, [3, 6, 1, 5, 2, 7, 4]),
    "one_column_string": ({"by": "id", "descending": [True]}, [7, 6, 5, 4, 3, 2, 1]),
}


@pytest.mark.parametrize("case", list(SORT_CASES))
@pytest.mark.parametrize("engine", [pytest.param("polars")] + OTHER_ENGINES)
def test_sort_on_several_columns_a_direction_each(storage, mixed, case, engine):
    params, want = SORT_CASES[case]
    rows = _engine(engine, storage).preview(mixed, [{"type": "sort", "params": params}], limit=100).rows
    assert [r["id"] for r in rows] == want, case


@pytest.mark.parametrize("engine", [pytest.param("polars")] + OTHER_ENGINES)
def test_sort_ignore_missing_keeps_each_direction_with_its_column(storage, mixed, engine):
    """The publish's sort drops a key no longer in the chain: the others keep their own direction."""
    params = {"by": ["gone", "importo", "id"], "descending": [False, True, True], "ignore_missing": True}
    rows = _engine(engine, storage).preview(mixed, [{"type": "sort", "params": params}], limit=100).rows
    assert [r["id"] for r in rows] == [6, 3, 1, 5, 7, 2, 4]
