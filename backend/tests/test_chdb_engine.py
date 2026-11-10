"""The chDB engine (embedded ClickHouse, out of core): the same IR as Polars/DuckDB.

Checks preview/run and the 16 structural operations; sql/foreach must raise a clear
error. The cache with a fake Redis (no Valkey).
"""
import polars as pl
import pytest

from app.engine.base import DataSource
from app.engine.cache import StepCache, plan_hashes
from app.engine.chdb_engine import ChdbEngine
from app.engine.exceptions import EngineError
from tests.conftest import BUCKET, upload_df
from tests.fakes import FakeRedis


def _engine(storage, cache=None):
    return ChdbEngine(storage=storage, cache=cache or StepCache(storage, redis_client=FakeRedis()))


@pytest.fixture
def src(storage):
    df = pl.DataFrame({
        "paese": ["IT", "FR", "DE", "IT"],
        "vendite": [100, 50, 250, 300],
        "citta": ["Roma", "Parigi", None, "Milano"],
    })
    return upload_df(storage, df, "datasets/vendite_chdb.parquet")


@pytest.fixture
def anagrafica(storage):
    df = pl.DataFrame({"paese": ["IT", "DE"], "nome": ["Italia", "Germania"]})
    return upload_df(storage, df, "datasets/anagrafica_chdb.parquet")


def test_preview_passthrough(storage, src):
    res = _engine(storage).preview(src, [], limit=10)
    assert res.row_count == 4
    assert {c.name for c in res.columns} == {"paese", "vendite", "citta"}


def test_filter(storage, src):
    ops = [{"type": "filter", "params": {"column": "vendite", "operator": "gt", "value": 100}}]
    res = _engine(storage).preview(src, ops, limit=10)
    assert res.row_count == 2
    assert {r["paese"] for r in res.rows} == {"DE", "IT"}



def test_filter_on_dates_takes_the_editors_values(storage):
    """As the other engines: the editor's ISO values, and a value that is not a date said
    in the same words, not with ClickHouse's error and the query."""
    from datetime import date, datetime

    df = pl.DataFrame({"id": [1, 2, 3], "ts": [datetime(2024, 1, 14, 18), datetime(2024, 1, 15, 6), datetime(2024, 1, 16)],
                       "day": [date(2024, 1, 14), date(2024, 1, 15), date(2024, 1, 16)], "note": ["a", "b", "c"]})
    src = upload_df(storage, df, "datasets/dates_chdb.parquet")

    def ids(ops):
        return sorted(r["id"] for r in _engine(storage).preview(src, ops, limit=10).rows)

    # what the calendar sends, without seconds: ClickHouse could not read it
    assert ids([{"type": "filter", "params": {"column": "ts", "operator": "gt", "value": "2024-01-15T05:30"}}]) == [2, 3]
    assert ids([{"type": "filter", "params": {"column": "ts", "operator": "between",
                                              "value": ["2024-01-15T00:00", "2024-01-15T23:59"]}}]) == [2]
    assert ids([{"type": "filter", "params": {"column": "ts", "operator": "in", "value": ["2024-01-15T06:00"]}}]) == [2]
    assert ids([{"type": "filter", "params": {"column": "day", "operator": "eq", "value": "2024-01-15"}}]) == [2]
    # a text column keeps the text as it is
    assert ids([{"type": "filter", "params": {"column": "note", "operator": "lt", "value": "2024-01-15T05:30"}}]) == []
    for col in ("ts", "day"):
        with pytest.raises(EngineError, match="is not valid for a date or time column: use the ISO format"):
            _engine(storage).preview(src, [{"type": "filter", "params": {"column": col, "operator": "gt",
                                                                          "value": "15/01/2024"}}], limit=10)

def test_group_by(storage, src):
    ops = [{"type": "group_by", "params": {
        "by": ["paese"],
        "aggregations": [{"column": "vendite", "func": "sum", "alias": "tot"}],
    }}]
    res = _engine(storage).preview(src, ops, limit=10)
    tot = {r["paese"]: r["tot"] for r in res.rows}
    assert tot["IT"] == 400


def test_select_drop_rename(storage, src):
    e = _engine(storage)
    sel = e.preview(src, [{"type": "select", "params": {"columns": ["paese"]}}], limit=5)
    assert [c.name for c in sel.columns] == ["paese"]
    drop = e.preview(src, [{"type": "drop", "params": {"columns": ["citta"]}}], limit=5)
    assert {c.name for c in drop.columns} == {"paese", "vendite"}
    ren = e.preview(src, [{"type": "rename", "params": {"mapping": {"paese": "country"}}}], limit=5)
    assert "country" in {c.name for c in ren.columns}
    assert [c.name for c in ren.columns][0] == "country"  # order kept


def test_cast(storage, src):
    ops = [{"type": "cast", "params": {"columns": {"vendite": "float"}}}]
    res = _engine(storage).preview(src, ops, limit=5)
    dt = {c.name: c.dtype for c in res.columns}
    assert "f" in dt["vendite"].lower() or "float" in dt["vendite"].lower()


def test_sort_limit(storage, src):
    ops = [{"type": "sort", "params": {"by": "vendite", "descending": True}},
           {"type": "limit", "params": {"n": 2}}]
    res = _engine(storage).preview(src, ops, limit=10)
    assert res.row_count == 2
    assert res.rows[0]["vendite"] == 300


def test_unique(storage, src):
    ops = [{"type": "unique", "params": {"subset": ["paese"]}}]
    res = _engine(storage).preview(src, ops, limit=10)
    assert res.row_count == 3  # IT, FR, DE


def test_fill_null(storage, src):
    ops = [{"type": "fill_null", "params": {"columns": {"citta": "N/D"}}}]
    res = _engine(storage).preview(src, ops, limit=10)
    assert all(r["citta"] is not None for r in res.rows)


def test_drop_nulls(storage, src):
    ops = [{"type": "drop_nulls", "params": {"subset": ["citta"]}}]
    res = _engine(storage).preview(src, ops, limit=10)
    assert res.row_count == 3


def test_compute(storage, src):
    ops = [{"type": "compute", "params": {"columns": [{"name": "doppio", "expr": "vendite * 2"}]}}]
    res = _engine(storage).preview(src, ops, limit=10)
    got = {r["paese"]: r["doppio"] for r in res.rows if r["paese"] == "DE"}
    assert got["DE"] == 500


def test_compute_blocks_table_functions(storage, src):
    ops = [{"type": "compute", "params": {"columns": [{"name": "x", "expr": "(SELECT 1 FROM file('/etc/passwd'))"}]}}]
    with pytest.raises(EngineError):
        _engine(storage).preview(src, ops, limit=5)


def test_join(storage, src, anagrafica):
    ops = [{"type": "join", "params": {
        "right": {"source": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "operations": []},
        "on": ["paese"], "how": "inner",
    }}]
    res = _engine(storage).preview(src, ops, limit=10)
    assert "nome" in {c.name for c in res.columns}
    assert res.row_count == 3  # IT,IT,DE (FR has no match)


def test_union(storage, src, anagrafica):
    ops = [{"type": "union", "params": {
        "right": {"source": {"bucket": anagrafica.bucket, "key": anagrafica.key}, "operations": []},
    }}]
    res = _engine(storage).preview(src, ops, limit=20)
    assert res.row_count == 6  # 4 + 2
    assert {"paese", "vendite", "citta", "nome"} <= {c.name for c in res.columns}


def test_pivot_and_unpivot(storage):
    df = pl.DataFrame({
        "anno": [2023, 2023, 2024, 2024],
        "paese": ["IT", "DE", "IT", "DE"],
        "val": [1, 2, 3, 4],
    })
    s = upload_df(storage, df, "datasets/pivot_chdb.parquet")
    piv = [{"type": "pivot", "params": {"index": ["anno"], "on": "paese", "values": "val", "func": "sum"}}]
    res = _engine(storage).preview(s, piv, limit=10)
    cols = {c.name for c in res.columns}
    assert "IT" in cols and "DE" in cols
    by_year = {r["anno"]: r for r in res.rows}
    assert by_year[2024]["IT"] == 3

    unp = [{"type": "unpivot", "params": {"index": ["anno", "paese"], "on": ["val"]}}]
    res2 = _engine(storage).preview(s, unp, limit=10)
    assert {"variable", "value"} <= {c.name for c in res2.columns}


def test_run_writes_output(storage, src):
    dest = DataSource(bucket=BUCKET, key="out/chdb_run.parquet")
    ops = [{"type": "filter", "params": {"column": "vendite", "operator": "ge", "value": 100}}]
    res = _engine(storage).run(src, ops, dest)
    assert res.rows_written == 3
    assert storage.exists(BUCKET, "out/chdb_run.parquet")


def test_sql_and_foreach_not_supported(storage, src):
    e = _engine(storage)
    with pytest.raises(EngineError):
        e.preview(src, [{"type": "sql", "params": {"query": "SELECT 1"}}], limit=5)
    with pytest.raises(EngineError):
        e.preview(src, [{"type": "foreach", "params": {"body": [], "items": [{}]}}], limit=5)


def test_step_cache_materializes_the_parent(storage, src):
    cache = StepCache(storage, redis_client=FakeRedis())
    e = _engine(storage, cache)
    ops = [
        {"type": "filter", "params": {"column": "vendite", "operator": "gt", "value": 40}},
        {"type": "sort", "params": {"by": "vendite"}},
    ]
    e.preview(src, ops, limit=10)
    hashes = plan_hashes(e._source_id(src), [{"type": ops[0]["type"], "params": ops[0]["params"]}])
    assert not cache.has(hashes[-1])  # the preview does not wait for the copy
    from tests.conftest import drain_cache

    assert drain_cache(e) == 1
    assert cache.has(hashes[-1])  # the parent (ops[:-1]) is cached


def test_namespacing_engine(storage, src):
    e = _engine(storage)
    assert e._source_id(src).startswith("chdb:")


def test_a_cast_to_an_instant_keeps_the_dates_a_32_bit_datetime_cannot_hold(storage):
    """The cast went through ClickHouse's 32-bit DateTime: a birth date of 1955 became
    1970-01-01, 2150 became 2106, and an instant lost its microseconds. A DateTime64(6) now;
    a year it cannot hold either (before 1900, after 2299) is NULL, never moved to the edge."""
    from datetime import date, datetime

    df = pl.DataFrame({"id": [1, 2, 3, 4], "d": [date(1955, 3, 12), date(2150, 6, 1), date(2024, 1, 5), None],
                       "t": ["1955-03-12 10:30:00.123456", "1800-01-01", "abc", " 2024-01-05T10:30 "]})
    src = upload_df(storage, df, "datasets/instants_chdb.parquet")
    ops = [{"type": "cast", "params": {"columns": {"d": "datetime", "t": "datetime"}}}, {"type": "sort", "params": {"by": ["id"]}}]
    res = _engine(storage).preview(src, ops, limit=10)
    assert [r["d"] for r in res.rows] == [datetime(1955, 3, 12), datetime(2150, 6, 1), datetime(2024, 1, 5), None]
    assert [r["t"] for r in res.rows] == [datetime(1955, 3, 12, 10, 30, 0, 123456), None, None, datetime(2024, 1, 5, 10, 30)]


def test_a_big_integer_cast_to_a_float_is_rounded_as_on_the_other_engines(storage):
    """2^53 + 1 is not exactly a double: ClickHouse's accurateCastOrNull gave NULL, Polars and
    DuckDB the nearest double."""
    df = pl.DataFrame({"id": [1, 2, 3], "big": [9007199254740993, -9007199254740995, 5]})
    src = upload_df(storage, df, "datasets/big_float_chdb.parquet")
    ops = [{"type": "cast", "params": {"columns": {"big": "float"}}}, {"type": "sort", "params": {"by": ["id"]}}]
    res = _engine(storage).preview(src, ops, limit=10)
    assert [r["big"] for r in res.rows] == df["big"].cast(pl.Float64).to_list() == [9007199254740992.0, -9007199254740996.0, 5.0]
