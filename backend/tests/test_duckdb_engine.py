"""Engine DuckDB (out-of-core): stessa IR di Polars, operazioni single-input v1.

Verifica preview/run e le 11 operazioni supportate; le altre devono sollevare
un errore chiaro (usa Polars).
"""
import polars as pl
import pytest

from app.engine.base import DataSource
from app.engine.duckdb_engine import DuckDBEngine
from app.engine.exceptions import EngineError
from tests.conftest import BUCKET, upload_df


def _engine(storage):
    return DuckDBEngine(storage=storage)


@pytest.fixture
def src(storage):
    df = pl.DataFrame(
        {
            "paese": ["IT", "FR", "DE", "IT"],
            "vendite": [100, 50, 250, 300],
            "citta": ["Roma", "Parigi", None, "Milano"],
        }
    )
    return upload_df(storage, df, "datasets/vendite_duck.parquet")


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
    assert tot["IT"] == 400  # 100 + 300


def test_select_drop_rename(storage, src):
    e = _engine(storage)
    sel = e.preview(src, [{"type": "select", "params": {"columns": ["paese"]}}], limit=5)
    assert [c.name for c in sel.columns] == ["paese"]
    drp = e.preview(src, [{"type": "drop", "params": {"columns": ["citta"]}}], limit=5)
    assert {c.name for c in drp.columns} == {"paese", "vendite"}
    ren = e.preview(src, [{"type": "rename", "params": {"mapping": {"vendite": "ricavi"}}}], limit=5)
    names = {c.name for c in ren.columns}
    assert "ricavi" in names and "vendite" not in names


def test_cast(storage, src):
    # lo schema torna dai tipi Polars (preview via .pl()) → Float
    res = _engine(storage).preview(src, [{"type": "cast", "params": {"columns": {"vendite": "float"}}}], limit=5)
    dt = {c.name: c.dtype for c in res.columns}
    assert "Float" in dt["vendite"]


def test_drop_nulls_and_fill_null(storage, src):
    e = _engine(storage)
    dn = e.preview(src, [{"type": "drop_nulls", "params": {"subset": ["citta"]}}], limit=10)
    assert dn.row_count == 3
    fn = e.preview(src, [{"type": "fill_null", "params": {"columns": {"citta": "?"}}}], limit=10)
    assert all(r["citta"] is not None for r in fn.rows)


def test_sort_limit_unique(storage, src):
    e = _engine(storage)
    s = e.preview(src, [{"type": "sort", "params": {"by": "vendite", "descending": True}}], limit=10)
    assert s.rows[0]["vendite"] == 300
    lim = e.preview(src, [{"type": "limit", "params": {"n": 2}}], limit=10)
    assert lim.row_count == 2
    uniq = e.preview(src, [{"type": "unique", "params": {"subset": ["paese"]}}], limit=10)
    assert uniq.row_count == 3  # IT, FR, DE


def test_multi_op_chain(storage, src):
    # catena di più operazioni: verifica che gli alias per-step non ricorsino
    ops = [
        {"type": "filter", "params": {"column": "vendite", "operator": "ge", "value": 80}},
        {"type": "group_by", "params": {
            "by": ["paese"],
            "aggregations": [{"column": "vendite", "func": "sum", "alias": "tot"}],
        }},
        {"type": "sort", "params": {"by": "tot", "descending": True}},
    ]
    # filtro ≥80 esclude l'unica riga FR (50) → gruppi IT(400), DE(250)
    res = _engine(storage).preview(src, ops, limit=10)
    assert [r["paese"] for r in res.rows] == ["IT", "DE"]
    assert res.rows[0]["tot"] == 400


def test_run_writes_output(storage, src):
    dest = DataSource(bucket=BUCKET, key="out/duck.parquet")
    ops = [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}}]
    res = _engine(storage).run(src, ops, dest)
    assert res.rows_written == 2
    assert storage.exists(BUCKET, "out/duck.parquet")


def test_unsupported_ops_raise(storage, src):
    e = _engine(storage)
    for bad in ("sql", "join", "compute", "pivot", "foreach"):
        with pytest.raises(EngineError, match="non ancora supportata"):
            e.preview(src, [{"type": bad, "params": {}}], limit=5)
