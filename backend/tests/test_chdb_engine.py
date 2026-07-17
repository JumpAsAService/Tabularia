"""Engine chDB (ClickHouse embedded, out-of-core): stessa IR di Polars/DuckDB.

Verifica preview/run e le 16 operazioni strutturali; sql/foreach devono sollevare
un errore chiaro (usa Polars/DuckDB). Cache con Redis finto (nessun Valkey).
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
    assert [c.name for c in ren.columns][0] == "country"  # ordine preservato


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


def test_compute_blocca_table_function(storage, src):
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
    assert res.row_count == 3  # IT,IT,DE (FR non ha match)


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
    by_anno = {r["anno"]: r for r in res.rows}
    assert by_anno[2024]["IT"] == 3

    unp = [{"type": "unpivot", "params": {"index": ["anno", "paese"], "on": ["val"]}}]
    res2 = _engine(storage).preview(s, unp, limit=10)
    assert {"variable", "value"} <= {c.name for c in res2.columns}


def test_run_scrive_output(storage, src):
    dest = DataSource(bucket=BUCKET, key="out/chdb_run.parquet")
    ops = [{"type": "filter", "params": {"column": "vendite", "operator": "ge", "value": 100}}]
    res = _engine(storage).run(src, ops, dest)
    assert res.rows_written == 3
    assert storage.exists(BUCKET, "out/chdb_run.parquet")


def test_sql_e_foreach_non_supportati(storage, src):
    e = _engine(storage)
    with pytest.raises(EngineError):
        e.preview(src, [{"type": "sql", "params": {"query": "SELECT 1"}}], limit=5)
    with pytest.raises(EngineError):
        e.preview(src, [{"type": "foreach", "params": {"body": [], "items": [{}]}}], limit=5)


def test_step_cache_materializza_il_parent(storage, src):
    cache = StepCache(storage, redis_client=FakeRedis())
    e = _engine(storage, cache)
    ops = [
        {"type": "filter", "params": {"column": "vendite", "operator": "gt", "value": 40}},
        {"type": "sort", "params": {"by": "vendite"}},
    ]
    e.preview(src, ops, limit=10)
    # il parent (ops[:-1]) dev'essere in cache
    hashes = plan_hashes(e._source_id(src), [{"type": ops[0]["type"], "params": ops[0]["params"]}])
    assert cache.has(hashes[-1])


def test_namespacing_engine(storage, src):
    e = _engine(storage)
    assert e._source_id(src).startswith("chdb:")
