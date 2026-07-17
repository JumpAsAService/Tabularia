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


def test_unknown_op_raises(storage, src):
    with pytest.raises(EngineError, match="non ancora supportata"):
        _engine(storage).preview(src, [{"type": "does_not_exist", "params": {}}], limit=5)


# ── Execute SQL (sandbox) ────────────────────────────────────────────────────
def test_sql_aggregation(storage, src):
    ops = [{"type": "sql", "params": {"query": "SELECT paese, sum(vendite) AS tot FROM self GROUP BY paese"}}]
    res = _engine(storage).preview(src, ops, limit=10)
    assert {r["paese"]: r["tot"] for r in res.rows}["IT"] == 400


def test_sql_input_alias(storage, src):
    res = _engine(storage).preview(src, [{"type": "sql", "params": {"query": "SELECT count(*) AS n FROM input"}}], limit=10)
    assert res.rows[0]["n"] == 4


def test_sql_sandbox_blocks_file_read(storage, src):
    # la query utente NON può leggere il filesystem del worker
    with pytest.raises(EngineError):
        _engine(storage).preview(
            src, [{"type": "sql", "params": {"query": "SELECT * FROM read_csv('/app/app/core/config.py')"}}], limit=5
        )


def test_sql_run_writes_output(storage, src):
    dest = DataSource(bucket=BUCKET, key="out/duck_sql.parquet")
    res = _engine(storage).run(src, [{"type": "sql", "params": {"query": "SELECT paese FROM self WHERE vendite > 100"}}], dest)
    assert res.rows_written == 2 and storage.exists(BUCKET, "out/duck_sql.parquet")


# ── foreach ──────────────────────────────────────────────────────────────────
def test_foreach_items(storage, src):
    ops = [{"type": "foreach", "params": {
        "items": [{"soglia": 100}, {"soglia": 250}],
        "add_keys_as_columns": True,
        "body": [{"type": "filter", "params": {"column": "vendite", "operator": "ge", "value": "{{soglia}}"}}],
    }}]
    res = _engine(storage).preview(src, ops, limit=50)
    # soglia 100 → 3 righe (100,250,300); soglia 250 → 2 (250,300); totale 5
    assert res.row_count == 5
    assert "soglia" in {c.name for c in res.columns}


def test_foreach_driver(storage, src):
    driver = upload_df(storage, pl.DataFrame({"soglia": [100, 300]}), "datasets/drv_duck.parquet")
    ops = [{"type": "foreach", "params": {
        "driver": {"bucket": driver.bucket, "key": driver.key},
        "body": [{"type": "filter", "params": {"column": "vendite", "operator": "ge", "value": "{{soglia}}"}}],
    }}]
    res = _engine(storage).preview(src, ops, limit=50)
    assert res.row_count == 4  # soglia100 → 3, soglia300 → 1


# ── join / union / pivot / unpivot / compute ─────────────────────────────────
@pytest.fixture
def anag(storage):
    df = pl.DataFrame({"paese": ["IT", "FR"], "nome": ["Italia", "Francia"]})
    return upload_df(storage, df, "datasets/anag_duck.parquet")


def test_join_inner(storage, src, anag):
    ops = [{"type": "join", "params": {"right": {"bucket": anag.bucket, "key": anag.key}, "on": ["paese"], "how": "inner"}}]
    res = _engine(storage).preview(src, ops, limit=10)
    assert "nome" in {c.name for c in res.columns}
    assert {r["nome"] for r in res.rows} == {"Italia", "Francia"}  # DE senza match escluso


def test_join_left_keeps_unmatched(storage, src, anag):
    ops = [{"type": "join", "params": {"right": {"bucket": anag.bucket, "key": anag.key}, "on": ["paese"], "how": "left"}}]
    res = _engine(storage).preview(src, ops, limit=10)
    de = [r for r in res.rows if r["paese"] == "DE"]
    assert de and de[0]["nome"] is None


def test_join_overlap_suffix(storage, src):
    r = upload_df(storage, pl.DataFrame({"paese": ["IT"], "vendite": [999]}), "datasets/ov_duck.parquet")
    ops = [{"type": "join", "params": {"right": {"bucket": r.bucket, "key": r.key}, "on": ["paese"], "how": "inner"}}]
    cols = {c.name for c in _engine(storage).preview(src, ops, limit=10).columns}
    assert "vendite" in cols and "vendite_right" in cols  # omonima non-chiave → suffisso


def test_union_relaxed(storage, src):
    r = upload_df(storage, pl.DataFrame({"paese": ["ES"], "vendite": [7], "citta": ["Madrid"]}), "datasets/es_duck.parquet")
    ops = [{"type": "union", "params": {"right": {"bucket": r.bucket, "key": r.key}, "strategy": "relaxed"}}]
    res = _engine(storage).preview(src, ops, limit=20)
    assert res.row_count == 5 and "ES" in {row["paese"] for row in res.rows}


def test_compute(storage, src):
    ops = [{"type": "compute", "params": {"columns": [{"name": "doppio", "expr": "vendite * 2"}]}}]
    res = _engine(storage).preview(src, ops, limit=10)
    row = [r for r in res.rows if r["vendite"] == 100][0]
    assert row["doppio"] == 200


def test_compute_blocks_subquery_and_file(storage, src):
    e = _engine(storage)
    for bad_expr in ("(SELECT 1)", "(SELECT * FROM read_csv('/etc/passwd'))"):
        with pytest.raises(EngineError, match="non consentita"):
            e.preview(src, [{"type": "compute", "params": {"columns": [{"name": "x", "expr": bad_expr}]}}], limit=5)


def test_pivot(storage):
    s = upload_df(storage, pl.DataFrame({"paese": ["IT", "IT", "FR"], "anno": [2023, 2024, 2023], "v": [10, 20, 5]}), "datasets/piv_duck.parquet")
    ops = [{"type": "pivot", "params": {"index": ["paese"], "on": "anno", "values": "v", "func": "sum"}}]
    cols = {c.name for c in _engine(storage).preview(s, ops, limit=10).columns}
    assert "paese" in cols and "2023" in cols and "2024" in cols


def test_unpivot(storage):
    s = upload_df(storage, pl.DataFrame({"paese": ["IT", "FR"], "a": [1, 2], "b": [3, 4]}), "datasets/unp_duck.parquet")
    ops = [{"type": "unpivot", "params": {"index": ["paese"], "on": ["a", "b"], "variable_name": "metric", "value_name": "val"}}]
    res = _engine(storage).preview(s, ops, limit=10)
    assert {c.name for c in res.columns} == {"paese", "metric", "val"}
    assert res.row_count == 4
