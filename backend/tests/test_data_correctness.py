"""The core's DATA CORRECTNESS, with an ORACLE COMPUTED BY HAND, on every engine.

Unlike the parity tests (which compare the engines with each other) here every case
states the EXPECTED result, computed by hand on a small but "nasty" dataset: NULLs
everywhere, strings with apostrophes/quotes/accents/spaces, the empty string other than
NULL, column names with spaces and capitals, decimals (Postgres numeric), integers beyond
2^53, dates and booleans with NULLs.

The reference semantics (= SQL, which is what runs in production):
- comparisons with NULL are false (ne/not_in do NOT keep the NULLs);
- the aggregates ignore NULLs; count/n_unique do not count NULLs; sum/mean/min/max of a
  group all NULL → NULL; sample std/var (n-1), NULL with fewer than 2 values; median =
  linear interpolation (R7);
- sorting: NULL ALWAYS last (asc and desc): a "top N" does not take the NULLs;
- failed casts → NULL (never an error); numbers → integers by truncation; numeric
  strings with spaces around → trimmed;
- join: a NULL key never matches; the missing side → NULL (never a default); a non-key
  column with the same name on the right → suffix `_right`.

DuckDB/chDB run when the packages are installed (they are in the container); the
external ClickHouse when `CLICKHOUSE_TEST_HOST` is set (see test_clickhouse_engine.py).
"""
from __future__ import annotations

import datetime as dt
import decimal
import importlib.util
import math
import os
import statistics

import polars as pl
import pytest

from app.engine.cache import StepCache
from app.engine.exceptions import EngineError
from app.engine.polars_engine import PolarsEngine
from tests.conftest import BUCKET, upload_df
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
    if name == "bigquery":
        # the REAL storage (GCS): BigQuery reads the parquet from the bucket, not from a fake
        from app.engine.bigquery_engine import BigQueryEngine

        return BigQueryEngine(storage=storage)
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


ENGINES = [
    "polars",
    pytest.param("duckdb", marks=pytest.mark.skipif(importlib.util.find_spec("duckdb") is None, reason="duckdb assente")),
    pytest.param("chdb", marks=pytest.mark.skipif(importlib.util.find_spec("chdb") is None, reason="chdb assente")),
    pytest.param("clickhouse", marks=pytest.mark.skipif(not os.getenv("CLICKHOUSE_TEST_HOST"), reason="serve CLICKHOUSE_TEST_HOST")),
    # LIVE: BIGQUERY_LIVE=1 with BIGQUERY__* and STORAGE__* (GCS) in the environment and
    # TEST_BUCKET = the real bucket. Cost: a few MB billed per test.
    pytest.param("bigquery", marks=pytest.mark.skipif(not os.getenv("BIGQUERY_LIVE"), reason="serve BIGQUERY_LIVE=1 (credenziali GCP)")),
]

if os.getenv("BIGQUERY_LIVE"):
    # In live mode ALL the engines of this module read from the real storage: the test
    # parquet files are uploaded to the bucket under datasets/ and removed at the end of
    # the session. Run with `-k bigquery` not to pay for the others.
    @pytest.fixture
    def storage():
        from app.utils import StorageService

        svc = StorageService()
        uploaded: list[tuple[str, str]] = []
        real_upload = svc.upload_file

        def tracking_upload(path, bucket, key):
            uploaded.append((bucket, key))
            return real_upload(path, bucket, key)

        svc.upload_file = tracking_upload  # type: ignore[method-assign]
        yield svc
        for bucket, key in uploaded:  # nothing left on the real bucket
            try:
                svc.delete_object(bucket, key)
            except Exception:  # noqa: BLE001
                pass

D = dt.date
BIG = 9007199254740993  # 2^53 + 1: not representable as float64


# ── The "ordini" dataset: 8 rows, 15 columns ──────────────────────────────────────
ORDERS = pl.DataFrame({
    "id": [1, 2, 3, 4, 5, 6, 7, 8],
    "cliente": ["Rossi", "Bianchi", "O'Neil", 'Ac "me"', "Rossi", "Città Srl", "Bianchi", None],
    "Ragione Sociale": ["Rossi S.p.A.", "Bianchi & C.", "O'Neil Ltd", "Acme", "Rossi S.p.A.", "Città Srl", "Bianchi & C.", "Sconosciuto"],
    "canale": ["GDO", "HoReCa", "GDO", None, "Dettaglio", "GDO", "HoReCa", "GDO"],
    "importo": [100.5, 200.0, None, 50.25, 100.5, -30.0, 0.1, 0.2],
    "qta": [10, 5, 3, None, 10, 1, 2, 4],
    "data": [D(2024, 1, 5), D(2024, 1, 15), D(2024, 2, 1), D(2024, 2, 29), D(2024, 3, 10), None, D(2024, 12, 31), D(2025, 1, 1)],
    "attivo": [True, False, None, True, True, False, True, None],
    "big": [BIG, 1, 2, 3, 4, 5, 6, 7],
    "prezzo": pl.Series(
        [decimal.Decimal(x) if x else None for x in ["12.5000", "3.3333", None, "0.0001", "12.5000", "99999.9999", "1.0000", "2.0000"]],
        dtype=pl.Decimal(10, 4),
    ),
    "note": ["", None, "x", "  spazi  ", "", "y", "z", "w"],
    "num_txt": ["1", "2", "x", "2.7", " 4 ", "", "-5", None],
    "data_txt": ["2024-01-05", "2024-02-30", "x", None, "2025-12-31", "", "05/01/2024", "1999-09-09"],
    "ts": [dt.datetime(2024, 1, 5, 10, 30), dt.datetime(2024, 1, 15, 8, 0), dt.datetime(2024, 2, 1, 23, 59, 59), dt.datetime(2024, 2, 29, 12, 0),
           dt.datetime(2024, 3, 10, 0, 0), None, dt.datetime(2024, 12, 31, 18, 45), dt.datetime(2025, 1, 1, 0, 0, 1)],
    "ts_txt": ["2024-01-05T10:30:00", "2024-01-05 10:30:00", "x", None, "2025-12-31T23:59:59", "", "1999-09-09T00:00:00", "2024-06-30T12:00:00"],
}, schema_overrides={"id": pl.Int64, "qta": pl.Int64, "big": pl.Int64})

COLS = ORDERS.columns

CUSTOMERS = pl.DataFrame({
    "cliente": ["Rossi", "Bianchi", "O'Neil", "Verdi", None],
    "regione": ["Lazio", "Lombardia", "Irlanda", "Veneto", "Nessuna"],
    "canale": ["c1", "c2", "c3", "c4", "c5"],  # same name, NOT a key → `canale_right`
})


@pytest.fixture
def orders(storage):
    return upload_df(storage, ORDERS, "datasets/ordini_corr.parquet")


@pytest.fixture
def customers(storage):
    upload_df(storage, CUSTOMERS, "datasets/clienti_corr.parquet")
    upload_df(storage, CUSTOMERS.rename({"cliente": "nome"}), "datasets/clienti_nome_corr.parquet")
    return {"bucket": BUCKET, "key": "datasets/clienti_corr.parquet"}


# ── Normalizing the results ─────────────────────────────────────────────
def _family(dtype: str) -> str:
    d = dtype.lower()
    if d.startswith(("int", "uint")):
        return "int"
    if d.startswith(("float", "decimal", "double")):
        return "float"
    if d.startswith("datetime"):
        return "datetime"
    if d.startswith("date"):
        return "date"
    if d in ("string", "utf8", "str", "large_string"):
        return "str"
    if d in ("boolean", "bool"):
        return "bool"
    return d


def _norm(v):
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, decimal.Decimal):
        return round(float(v), 9)
    if isinstance(v, float):
        return round(v, 9)
    if isinstance(v, int):
        return v
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def _rows(res, cols=None):
    cols = cols or [c.name for c in res.columns]
    return [{c: _norm(r.get(c)) for c in cols} for r in res.rows]


def _key(row: dict):
    return tuple(("~" if row[c] is None else "", str(row[c])) for c in sorted(row))


def run(name, storage, src, ops, limit=1000):
    return _engine(name, storage).preview(src, ops, limit=limit, use_cache=False)


def check(name, storage, src, ops, expected: list[dict], *, ordered=False, columns=None, limit=1000):
    """Compares the result with the expected rows (a subset of the columns is fine)."""
    res = run(name, storage, src, ops, limit)
    if columns is not None:
        assert [c.name for c in res.columns] == columns, f"[{name}] colonne {[c.name for c in res.columns]}"
    want_cols = list(expected[0].keys()) if expected else [c.name for c in res.columns]
    got = _rows(res, want_cols)
    want = [{c: _norm(r[c]) for c in want_cols} for r in expected]
    if not ordered:
        got, want = sorted(got, key=_key), sorted(want, key=_key)
    assert got == want, f"[{name}]\n got: {got}\nwant: {want}"
    return res


def ids(res) -> list[int]:
    return sorted(r["id"] for r in res.rows)


def filt(column, operator, value=None):
    p = {"column": column, "operator": operator}
    if value is not None:
        p["value"] = value
    return {"type": "filter", "params": p}


# ══════════════════════════════════════════════════════════════════════════════
# Selecting / renaming / "difficult" names
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_select_reorder_drop_rename_with_hard_names(storage, orders, name):
    res = run(name, storage, orders, [{"type": "select", "params": {"columns": ["Ragione Sociale", "id"]}}])
    assert [c.name for c in res.columns] == ["Ragione Sociale", "id"] and res.row_count == 8
    res = run(name, storage, orders, [{"type": "reorder", "params": {"columns": ["note", "id", "cliente"]}}])
    assert [c.name for c in res.columns][:3] == ["note", "id", "cliente"] and len(res.columns) == 15
    res = run(name, storage, orders, [{"type": "drop", "params": {"columns": ["Ragione Sociale", "prezzo"]}}])
    assert [c.name for c in res.columns] == [c for c in COLS if c not in ("Ragione Sociale", "prezzo")]
    # renamed to a name with spaces, parentheses and a symbol, then a filter on it
    res = check(name, storage, orders, [
        {"type": "rename", "params": {"mapping": {"importo": "Importo (€)"}}},
        filt("Importo (€)", "gt", 100),
    ], [{"id": 1, "Importo (€)": 100.5}, {"id": 2, "Importo (€)": 200.0}, {"id": 5, "Importo (€)": 100.5}])
    assert "importo" not in [c.name for c in res.columns]


@pytest.mark.parametrize("name", ENGINES)
def test_values_survive_untouched(storage, orders, name):
    """No operations: the values come back EXACT (spaces, empty ≠ NULL, big ints, decimals)."""
    res = run(name, storage, orders, [])
    by = {r["id"]: r for r in res.rows}
    assert by[4]["note"] == "  spazi  "
    assert by[1]["note"] == "" and by[2]["note"] is None
    assert by[3]["cliente"] == "O'Neil" and by[4]["cliente"] == 'Ac "me"' and by[6]["cliente"] == "Città Srl"
    assert by[1]["big"] == BIG and isinstance(by[1]["big"], int)
    assert _norm(by[6]["prezzo"]) == 99999.9999 and _norm(by[4]["prezzo"]) == 0.0001
    assert _norm(by[1]["data"]) == "2024-01-05" and by[6]["data"] is None
    # NAIVE datetimes (UTC instants, without a zone): no "+00:00", no epoch
    assert _norm(by[1]["ts"]) == "2024-01-05T10:30:00" and _norm(by[8]["ts"]) == "2025-01-01T00:00:01" and by[6]["ts"] is None
    assert by[1]["attivo"] is True and by[2]["attivo"] is False and by[3]["attivo"] is None
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["id"] == "int" and fam["importo"] == "float" and fam["data"] == "date"
    assert fam["attivo"] == "bool" and fam["cliente"] == "str" and fam["prezzo"] == "float"
    assert fam["ts"] == "datetime"


# ══════════════════════════════════════════════════════════════════════════════
# Filters
# ══════════════════════════════════════════════════════════════════════════════
FILTER_CASES = {
    "gt_float": (filt("importo", "gt", 100), [1, 2, 5]),
    "ge_float": (filt("importo", "ge", 100.5), [1, 2, 5]),
    "lt_float_negative": (filt("importo", "lt", 0.15), [6, 7]),
    "le_int": (filt("qta", "le", 3), [3, 6, 7]),
    "eq_apostrophe": (filt("cliente", "eq", "O'Neil"), [3]),
    "eq_double_quote": (filt("cliente", "eq", 'Ac "me"'), [4]),
    "eq_accent": (filt("cliente", "eq", "Città Srl"), [6]),
    "eq_empty_string_is_not_null": (filt("note", "eq", ""), [1, 5]),
    "eq_preserves_spaces": (filt("note", "eq", "  spazi  "), [4]),
    "ne_excludes_null": (filt("cliente", "ne", "Rossi"), [2, 3, 4, 6, 7]),
    "in_strings": (filt("canale", "in", ["GDO", "HoReCa"]), [1, 2, 3, 6, 7, 8]),
    "in_ints": (filt("qta", "in", [10, 2]), [1, 5, 7]),
    "not_in_excludes_null": (filt("canale", "not_in", ["GDO"]), [2, 5, 7]),
    "between_inclusive": (filt("importo", "between", [0.2, 100.5]), [1, 4, 5, 8]),
    "contains": (filt("cliente", "contains", "os"), [1, 5]),
    "starts_with": (filt("cliente", "starts_with", "Bi"), [2, 7]),
    "ends_with": (filt("cliente", "ends_with", "Srl"), [6]),
    "is_null": (filt("canale", "is_null"), [4]),
    "is_null_not_empty_string": (filt("note", "is_null"), [2]),
    "is_not_null": (filt("importo", "is_not_null"), [1, 2, 4, 5, 6, 7, 8]),
    "date_gt_iso": (filt("data", "gt", "2024-02-29"), [5, 7, 8]),
    "date_between_iso": (filt("data", "between", ["2024-01-15", "2024-03-10"]), [2, 3, 4, 5]),
    "date_eq_iso": (filt("data", "eq", "2024-02-29"), [4]),
    "datetime_gt_iso": (filt("ts", "gt", "2024-02-29T12:00:00"), [5, 7, 8]),
    "datetime_ge_iso_equal_bound": (filt("ts", "ge", "2024-02-29T12:00:00"), [4, 5, 7, 8]),
    "datetime_between_iso": (filt("ts", "between", ["2024-01-15T08:00:00", "2024-03-10T00:00:00"]), [2, 3, 4, 5]),
    "bool_eq_true": (filt("attivo", "eq", True), [1, 4, 5, 7]),
    "bool_eq_false": (filt("attivo", "eq", False), [2, 6]),
    "decimal_gt": (filt("prezzo", "gt", 12), [1, 5, 6]),
    "decimal_eq": (filt("prezzo", "eq", 12.5), [1, 5]),
    "bigint_exact": (filt("big", "eq", BIG), [1]),
    "bigint_exact_neighbour_no_match": (filt("big", "eq", BIG - 1), []),
    "text_on_id_column_with_space": (filt("Ragione Sociale", "starts_with", "Rossi"), [1, 5]),
    # digits on a text column are a code, compared as text (the form sent a number until 2026-10-14)
    "eq_digits_on_text_column": (filt("num_txt", "eq", "2"), [2]),
    "in_digits_on_text_column": (filt("num_txt", "in", ["1", "-5"]), [1, 7]),
}


@pytest.mark.parametrize("case", list(FILTER_CASES))
@pytest.mark.parametrize("name", ENGINES)
def test_filter(storage, orders, name, case):
    op, want = FILTER_CASES[case]
    res = run(name, storage, orders, [op])
    assert ids(res) == want, f"[{name}] {case}"
    assert [c.name for c in res.columns] == COLS  # the filter does not touch the schema


@pytest.mark.parametrize("op", [
    {"type": "filter", "params": {"column": "cliente", "operator": "eq"}},
    {"type": "filter", "params": {"column": "cliente", "operator": "eq", "value": None}},
    {"type": "filter", "params": {"column": "canale", "operator": "in", "value": []}},
    {"type": "filter", "params": {"column": "importo", "operator": "between", "value": [1]}},
], ids=["eq_without_value", "eq_null", "in_empty", "between_one_bound"])
@pytest.mark.parametrize("name", ENGINES)
def test_a_filter_left_without_a_value_is_refused(storage, orders, name, op):
    """Compared with NULL it kept zero rows without a word, and a run published an
    empty datasource: now every engine says what is missing."""
    with pytest.raises(EngineError, match="filter: "):
        run(name, storage, orders, [op])


@pytest.mark.parametrize("name", ENGINES)
def test_a_preview_cuts_only_what_nobody_reads_after_it(storage, orders, name):
    """An sql node may stop at the preview's first rows only as the last operation:
    duckdb cut it anywhere, so what followed (a filter, a group_by) worked on a sample
    and the preview showed wrong totals (found on the Sample RFM flow, 2026-10-14)."""
    sql = {"type": "sql", "params": {"query": "SELECT * FROM self"}}
    assert ids(run(name, storage, orders, [sql, filt("id", "eq", 8)], limit=1)) == [8]
    counted = run(name, storage, orders, [sql, {"type": "group_by", "params": {
        "by": ["attivo"], "aggregations": [{"column": "id", "func": "count", "alias": "n"}]}}], limit=5)
    assert sum(r["n"] for r in counted.rows) == 8
    last = run(name, storage, orders, [filt("id", "gt", 0), sql], limit=2)
    assert last.row_count == 2 and last.truncated


@pytest.mark.parametrize("name", ENGINES)
def test_filter_chain_is_and(storage, orders, name):
    res = run(name, storage, orders, [filt("canale", "eq", "GDO"), filt("importo", "is_not_null"), filt("qta", "gt", 1)])
    assert ids(res) == [1, 8]


# ══════════════════════════════════════════════════════════════════════════════
# Sorting, limit, top-N: NULLs ALWAYS go last
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_sort_desc_nulls_last_top_n(storage, orders, name):
    res = run(name, storage, orders, [
        {"type": "sort", "params": {"by": ["importo", "id"], "descending": True}},
        {"type": "limit", "params": {"n": 3}},
    ])
    assert [r["id"] for r in res.rows] == [2, 5, 1]


@pytest.mark.parametrize("name", ENGINES)
def test_sort_asc_nulls_last_bottom_n(storage, orders, name):
    res = run(name, storage, orders, [
        {"type": "sort", "params": {"by": "importo", "descending": False}},
        {"type": "limit", "params": {"n": 2}},
    ])
    assert [r["id"] for r in res.rows] == [6, 7]


@pytest.mark.parametrize("name", ENGINES)
def test_sort_full_order_with_nulls_and_strings(storage, orders, name):
    res = run(name, storage, orders, [{"type": "sort", "params": {"by": "importo", "descending": False}}])
    assert [r["id"] for r in res.rows][-1] == 3  # the NULL closes
    assert [r["id"] for r in res.rows][:3] == [6, 7, 8]
    res = run(name, storage, orders, [{"type": "sort", "params": {"by": "importo", "descending": True}}])
    assert [r["id"] for r in res.rows][-1] == 3 and [r["id"] for r in res.rows][0] == 2
    res = run(name, storage, orders, [{"type": "sort", "params": {"by": "data", "descending": True}}])
    assert [r["id"] for r in res.rows][:2] == [8, 7] and [r["id"] for r in res.rows][-1] == 6


@pytest.mark.parametrize("name", ENGINES)
def test_limit(storage, orders, name):
    assert run(name, storage, orders, [{"type": "limit", "params": {"n": 3}}]).row_count == 3
    assert run(name, storage, orders, [{"type": "limit", "params": {"n": 100}}]).row_count == 8


# ══════════════════════════════════════════════════════════════════════════════
# Unique / null handling
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_unique_subset_keeps_null_group(storage, orders, name):
    res = run(name, storage, orders, [{"type": "unique", "params": {"subset": ["cliente"]}}])
    assert sorted((r["cliente"] or "") for r in res.rows) == sorted(["Rossi", "Bianchi", "O'Neil", 'Ac "me"', "Città Srl", ""])
    assert [c.name for c in res.columns] == COLS


@pytest.mark.parametrize("name", ENGINES)
def test_unique_full_rows(storage, orders, name):
    res = run(name, storage, orders, [
        {"type": "select", "params": {"columns": ["cliente", "canale"]}},
        {"type": "unique", "params": {}},
    ])
    assert res.row_count == 7  # (Bianchi, HoReCa) appears twice


@pytest.mark.parametrize("name", ENGINES)
def test_fill_null_and_drop_nulls(storage, orders, name):
    res = run(name, storage, orders, [{"type": "fill_null", "params": {"columns": {"importo": 0, "canale": "n/d"}}}])
    by = {r["id"]: r for r in res.rows}
    assert _norm(by[3]["importo"]) == 0.0 and by[4]["canale"] == "n/d"
    assert by[1]["canale"] == "GDO" and _norm(by[1]["importo"]) == 100.5
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["importo"] == "float" and fam["canale"] == "str"

    res = run(name, storage, orders, [{"type": "drop_nulls", "params": {"subset": ["importo", "qta"]}}])
    assert ids(res) == [1, 2, 5, 6, 7, 8]
    res = run(name, storage, orders, [{"type": "drop_nulls", "params": {}}])
    assert ids(res) == [1, 5, 7]


# ══════════════════════════════════════════════════════════════════════════════
# Aggregazione
# ══════════════════════════════════════════════════════════════════════════════
def _agg(col, func, alias):
    return {"column": col, "func": func, "alias": alias}


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_basic_aggregates(storage, orders, name):
    ops = [{"type": "group_by", "params": {"by": ["canale"], "aggregations": [
        _agg("importo", "sum", "somma"), _agg("importo", "count", "n_importi"), _agg("importo", "mean", "media"),
        _agg("qta", "min", "qmin"), _agg("qta", "max", "qmax"), _agg("cliente", "n_unique", "n_clienti"),
        _agg("id", "count", "n"),
    ]}}]
    res = check(name, storage, orders, ops, [
        {"canale": "GDO", "somma": 70.7, "n_importi": 3, "media": 70.7 / 3, "qmin": 1, "qmax": 10, "n_clienti": 3, "n": 4},
        {"canale": "HoReCa", "somma": 200.1, "n_importi": 2, "media": 100.05, "qmin": 2, "qmax": 5, "n_clienti": 1, "n": 2},
        {"canale": "Dettaglio", "somma": 100.5, "n_importi": 1, "media": 100.5, "qmin": 10, "qmax": 10, "n_clienti": 1, "n": 1},
        {"canale": None, "somma": 50.25, "n_importi": 1, "media": 50.25, "qmin": None, "qmax": None, "n_clienti": 1, "n": 1},
    ], columns=["canale", "somma", "n_importi", "media", "qmin", "qmax", "n_clienti", "n"])
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["somma"] == "float" and fam["n_importi"] == "int" and fam["n"] == "int" and fam["n_clienti"] == "int"
    assert fam["qmin"] == "int" and fam["qmax"] == "int"


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_datetime_min_max(storage, orders, name):
    T = dt.datetime
    ops = [{"type": "group_by", "params": {"by": ["canale"], "aggregations": [_agg("ts", "min", "primo"), _agg("ts", "max", "ultimo"), _agg("data", "max", "gg")]}}]
    res = check(name, storage, orders, ops, [
        {"canale": "GDO", "primo": T(2024, 1, 5, 10, 30), "ultimo": T(2025, 1, 1, 0, 0, 1), "gg": D(2025, 1, 1)},
        {"canale": "HoReCa", "primo": T(2024, 1, 15, 8, 0), "ultimo": T(2024, 12, 31, 18, 45), "gg": D(2024, 12, 31)},
        {"canale": "Dettaglio", "primo": T(2024, 3, 10), "ultimo": T(2024, 3, 10), "gg": D(2024, 3, 10)},
        {"canale": None, "primo": T(2024, 2, 29, 12), "ultimo": T(2024, 2, 29, 12), "gg": D(2024, 2, 29)},
    ])
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["primo"] == "datetime" and fam["gg"] == "date"


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_all_null_group_is_null(storage, orders, name):
    ops = [{"type": "group_by", "params": {"by": ["cliente"], "aggregations": [
        _agg("importo", "sum", "somma"), _agg("importo", "mean", "media"), _agg("importo", "max", "mx"), _agg("importo", "count", "n"),
    ]}}]
    check(name, storage, orders, ops, [
        {"cliente": "Rossi", "somma": 201.0, "media": 100.5, "mx": 100.5, "n": 2},
        {"cliente": "Bianchi", "somma": 200.1, "media": 100.05, "mx": 200.0, "n": 2},
        {"cliente": "O'Neil", "somma": None, "media": None, "mx": None, "n": 0},
        {"cliente": 'Ac "me"', "somma": 50.25, "media": 50.25, "mx": 50.25, "n": 1},
        {"cliente": "Città Srl", "somma": -30.0, "media": -30.0, "mx": -30.0, "n": 1},
        {"cliente": None, "somma": 0.2, "media": 0.2, "mx": 0.2, "n": 1},
    ])


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_std_var_median(storage, orders, name):
    ops = [{"type": "group_by", "params": {"by": ["canale"], "aggregations": [
        _agg("importo", "std", "sd"), _agg("importo", "var", "vr"), _agg("importo", "median", "med"),
    ]}}]
    gdo = [100.5, -30.0, 0.2]
    horeca = [200.0, 0.1]
    check(name, storage, orders, ops, [
        {"canale": "GDO", "sd": statistics.stdev(gdo), "vr": statistics.variance(gdo), "med": 0.2},
        {"canale": "HoReCa", "sd": statistics.stdev(horeca), "vr": statistics.variance(horeca), "med": 100.05},
        {"canale": "Dettaglio", "sd": None, "vr": None, "med": 100.5},  # one value only: sample std/var undefined
        {"canale": None, "sd": None, "vr": None, "med": 50.25},
    ])


@pytest.mark.parametrize("name", ENGINES)
def test_std_var_of_one_value_are_null_not_nan(storage, orders, name):
    """Sample std/var of a single value: NULL on ALL the engines. ClickHouse gave NaN
    (`stddevSamp` of one value), which «fill the NULLs» downstream does not replace; `check`
    normalizes NaN to None, so here the RAW value is looked at (the user's decision,
    2026-10-10: «just make it null»). In the pivot too."""
    ops = [{"type": "group_by", "params": {"by": ["canale"], "aggregations": [_agg("importo", "std", "sd"), _agg("importo", "var", "vr")]}}]
    res = run(name, storage, orders, ops)
    detail = next(r for r in res.rows if r.get("canale") == "Dettaglio")
    assert detail["sd"] is None and detail["vr"] is None, f"[{name}] {detail}"
    piv = [{"type": "pivot", "params": {"index": ["canale"], "on": ["attivo"], "values": "importo", "func": "std"}}]
    res = run(name, storage, orders, piv)
    raw_values = [v for r in res.rows for k, v in r.items() if k != "canale"]
    assert not any(isinstance(v, float) and math.isnan(v) for v in raw_values), f"[{name}] {res.rows}"


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_multiple_keys_with_nulls(storage, orders, name):
    ops = [{"type": "group_by", "params": {"by": ["canale", "attivo"], "aggregations": [_agg("id", "count", "n")]}}]
    check(name, storage, orders, ops, [
        {"canale": "GDO", "attivo": True, "n": 1},
        {"canale": "GDO", "attivo": None, "n": 2},
        {"canale": "GDO", "attivo": False, "n": 1},
        {"canale": "HoReCa", "attivo": False, "n": 1},
        {"canale": "HoReCa", "attivo": True, "n": 1},
        {"canale": None, "attivo": True, "n": 1},
        {"canale": "Dettaglio", "attivo": True, "n": 1},
    ])


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_decimal_and_bigint(storage, orders, name):
    ops = [{"type": "group_by", "params": {"by": ["canale"], "aggregations": [
        _agg("prezzo", "sum", "p"), _agg("big", "sum", "b"), _agg("big", "max", "bm"),
    ]}}]
    res = check(name, storage, orders, ops, [
        {"canale": "GDO", "p": 100014.4999, "b": BIG + 2 + 5 + 7, "bm": BIG},
        {"canale": "HoReCa", "p": 4.3333, "b": 1 + 6, "bm": 6},
        {"canale": "Dettaglio", "p": 12.5, "b": 4, "bm": 4},
        {"canale": None, "p": 0.0001, "b": 3, "bm": 3},
    ])
    by = {r["canale"]: r for r in res.rows}
    assert isinstance(by["GDO"]["b"], int) and by["GDO"]["b"] == BIG + 14  # exact, not float


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_on_empty_input(storage, orders, name):
    res = run(name, storage, orders, [
        filt("id", "eq", 999),
        {"type": "group_by", "params": {"by": ["canale"], "aggregations": [_agg("importo", "sum", "s")]}},
    ])
    assert res.row_count == 0 and [c.name for c in res.columns] == ["canale", "s"]


# ══════════════════════════════════════════════════════════════════════════════
# Join / union
# ══════════════════════════════════════════════════════════════════════════════
def _join(how, customers, **kw):
    params = {"right": {"source": customers}, "how": how}
    params.update(kw or {"on": ["cliente"]})
    return {"type": "join", "params": params}


@pytest.mark.parametrize("name", ENGINES)
def test_sql_reads_the_tables_it_brings(storage, orders, customers, name):
    """A SQL script on the catalog: the operation brings each datasource as a named table,
    the source among them, and the query joins them by name."""
    tables = {"ds_1": {"source": {"bucket": orders.bucket, "key": orders.key}}, "ds_2": {"source": customers}}
    query = ("SELECT o.id, c.regione FROM ds_1 AS o JOIN ds_2 AS c ON o.cliente = c.cliente "
             "WHERE o.importo > 50")
    check(name, storage, orders, [{"type": "sql", "params": {"query": query, "tables": tables}}],
          [{"id": 1, "regione": "Lazio"}, {"id": 2, "regione": "Lombardia"}, {"id": 5, "regione": "Lazio"}])


@pytest.mark.parametrize("name", ENGINES)
def test_sql_cannot_read_a_table_it_was_not_given(storage, orders, customers, name):
    tables = {"ds_2": {"source": customers}}
    with pytest.raises(Exception):
        run(name, storage, orders, [{"type": "sql", "params": {"query": "SELECT * FROM ds_3", "tables": tables}}])


@pytest.mark.parametrize("name", ENGINES)
def test_join_inner_null_key_never_matches_and_collision_suffix(storage, orders, customers, name):
    res = check(name, storage, orders, [_join("inner", customers)], [
        {"id": 1, "cliente": "Rossi", "regione": "Lazio", "canale": "GDO", "canale_right": "c1"},
        {"id": 2, "cliente": "Bianchi", "regione": "Lombardia", "canale": "HoReCa", "canale_right": "c2"},
        {"id": 3, "cliente": "O'Neil", "regione": "Irlanda", "canale": "GDO", "canale_right": "c3"},
        {"id": 5, "cliente": "Rossi", "regione": "Lazio", "canale": "Dettaglio", "canale_right": "c1"},
        {"id": 7, "cliente": "Bianchi", "regione": "Lombardia", "canale": "HoReCa", "canale_right": "c2"},
    ])
    assert [c.name for c in res.columns] == COLS + ["regione", "canale_right"]


@pytest.mark.parametrize("name", ENGINES)
def test_join_left_missing_side_is_null(storage, orders, customers, name):
    res = check(name, storage, orders, [_join("left", customers)], [
        {"id": 1, "regione": "Lazio", "canale_right": "c1"},
        {"id": 2, "regione": "Lombardia", "canale_right": "c2"},
        {"id": 3, "regione": "Irlanda", "canale_right": "c3"},
        {"id": 4, "regione": None, "canale_right": None},
        {"id": 5, "regione": "Lazio", "canale_right": "c1"},
        {"id": 6, "regione": None, "canale_right": None},
        {"id": 7, "regione": "Lombardia", "canale_right": "c2"},
        {"id": 8, "regione": None, "canale_right": None},
    ])
    assert [c.name for c in res.columns] == COLS + ["regione", "canale_right"]
    by = {r["id"]: r for r in res.rows}
    assert by[8]["cliente"] is None and by[4]["importo"] == 50.25  # the left's columns stay untouched


@pytest.mark.parametrize("name", ENGINES)
def test_join_semi_anti_keep_only_left_columns(storage, orders, customers, name):
    res = run(name, storage, orders, [_join("semi", customers)])
    assert ids(res) == [1, 2, 3, 5, 7] and [c.name for c in res.columns] == COLS
    res = run(name, storage, orders, [_join("anti", customers)])
    assert ids(res) == [4, 6, 8] and [c.name for c in res.columns] == COLS


@pytest.mark.parametrize("name", ENGINES)
def test_join_full_and_right(storage, orders, customers, name):
    res = run(name, storage, orders, [_join("full", customers)])
    assert res.row_count == 10
    regions = sorted((r["regione"] or "") for r in res.rows)
    assert regions == sorted(["Lazio", "Lazio", "Lombardia", "Lombardia", "Irlanda", "Veneto", "Nessuna", "", "", ""])
    # right-only rows: the left's columns are NULL, the key is ONE column, filled
    only_right = [r for r in res.rows if r.get("id") is None]
    assert len(only_right) == 2
    assert sorted((r["cliente"] or "") for r in only_right) == ["", "Verdi"]
    assert "cliente_right" not in [c.name for c in res.columns]

    res = run(name, storage, orders, [_join("right", customers)])
    assert res.row_count == 7 and [c.name for c in res.columns] == COLS + ["regione", "canale_right"]
    assert sorted((r["regione"] or "") for r in res.rows) == sorted(["Lazio", "Lazio", "Lombardia", "Lombardia", "Irlanda", "Veneto", "Nessuna"])
    assert sorted(r["id"] for r in res.rows if r["id"] is not None) == [1, 2, 3, 5, 7]
    # the key is filled in the right-only rows too (Verdi); the right's NULL stays NULL
    assert sorted((r["cliente"] or "") for r in res.rows if r["id"] is None) == ["", "Verdi"]


@pytest.mark.parametrize("name", ENGINES)
def test_join_different_key_names(storage, orders, customers, name):
    right = {"bucket": BUCKET, "key": "datasets/clienti_nome_corr.parquet"}
    res = check(name, storage, orders, [_join("inner", right, left_on=["cliente"], right_on=["nome"])], [
        {"id": 1, "regione": "Lazio"}, {"id": 2, "regione": "Lombardia"}, {"id": 3, "regione": "Irlanda"},
        {"id": 5, "regione": "Lazio"}, {"id": 7, "regione": "Lombardia"},
    ])
    cols = [c.name for c in res.columns]
    # different names: BOTH keys stay (as SQL ON), plus the right's others
    assert cols == COLS + ["nome", "regione", "canale_right"]
    res = run(name, storage, orders, [_join("left", right, left_on=["cliente"], right_on=["nome"])])
    assert res.row_count == 8 and sum(1 for r in res.rows if r["regione"] is None) == 3


@pytest.mark.parametrize("name", ENGINES)
def test_join_duplicates_multiply_rows(storage, orders, customers, name):
    # the right with a duplicate key (Rossi ×2) → every Rossi on the left comes out twice
    dup = pl.DataFrame({"cliente": ["Rossi", "Rossi", "Bianchi"], "tag": ["a", "b", "c"]})
    upload_df(storage, dup, "datasets/dup_corr.parquet")
    right = {"bucket": BUCKET, "key": "datasets/dup_corr.parquet"}
    res = run(name, storage, orders, [_join("inner", right)])
    assert sorted((r["id"], r["tag"]) for r in res.rows) == [(1, "a"), (1, "b"), (2, "c"), (5, "a"), (5, "b"), (7, "c")]
    res = run(name, storage, orders, [_join("left", right)])
    assert res.row_count == 8 + 2  # 1 and 5 double


@pytest.mark.parametrize("name", ENGINES)
def test_union_relaxed_and_strict(storage, orders, customers, name):
    res = run(name, storage, orders, [{"type": "union", "params": {"right": {"source": customers}, "strategy": "relaxed"}}])
    assert [c.name for c in res.columns] == COLS + ["regione"]
    assert res.row_count == 13
    assert sum(1 for r in res.rows if r["id"] is None) == 5 and sum(1 for r in res.rows if r["regione"] is None) == 8
    assert sorted((r["canale"] or "") for r in res.rows if r["id"] is None) == ["c1", "c2", "c3", "c4", "c5"]
    # strict: the same schema (the source with itself, filtered on the right)
    res = run(name, storage, orders, [{"type": "union", "params": {
        "right": {"source": {"bucket": BUCKET, "key": "datasets/ordini_corr.parquet"}, "operations": [filt("id", "le", 2)]},
        "strategy": "strict",
    }}])
    assert [c.name for c in res.columns] == COLS and res.row_count == 10
    assert sorted(r["id"] for r in res.rows) == [1, 1, 2, 2, 3, 4, 5, 6, 7, 8]


# ══════════════════════════════════════════════════════════════════════════════
# Computed columns
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_compute_arithmetic_null_propagation_and_case(storage, orders, name):
    ops = [{"type": "compute", "params": {"columns": [
        {"name": "totale", "expr": "importo * qta"},
        {"name": "quarto", "expr": "importo / 4"},
        {"name": "fascia", "expr": "CASE WHEN importo > 100 THEN 'alto' ELSE 'basso' END"},
        {"name": "meta", "expr": "qta / 4"},
        {"name": "resto", "expr": "qta % 3"},
        {"name": "big1", "expr": "big + 1"},
        {"name": "doppio_totale", "expr": "totale * 2"},  # uses the column defined above
    ]}}]
    res = check(name, storage, orders, ops, [
        {"id": 1, "totale": 1005.0, "quarto": 25.125, "fascia": "alto", "meta": 2.5, "resto": 1, "big1": BIG + 1, "doppio_totale": 2010.0},
        {"id": 2, "totale": 1000.0, "quarto": 50.0, "fascia": "alto", "meta": 1.25, "resto": 2, "big1": 2, "doppio_totale": 2000.0},
        {"id": 3, "totale": None, "quarto": None, "fascia": "basso", "meta": 0.75, "resto": 0, "big1": 3, "doppio_totale": None},
        {"id": 4, "totale": None, "quarto": 12.5625, "fascia": "basso", "meta": None, "resto": None, "big1": 4, "doppio_totale": None},
        {"id": 5, "totale": 1005.0, "quarto": 25.125, "fascia": "alto", "meta": 2.5, "resto": 1, "big1": 5, "doppio_totale": 2010.0},
        {"id": 6, "totale": -30.0, "quarto": -7.5, "fascia": "basso", "meta": 0.25, "resto": 1, "big1": 6, "doppio_totale": -60.0},
        {"id": 7, "totale": 0.2, "quarto": 0.025, "fascia": "basso", "meta": 0.5, "resto": 2, "big1": 7, "doppio_totale": 0.4},
        {"id": 8, "totale": 0.8, "quarto": 0.05, "fascia": "basso", "meta": 1.0, "resto": 1, "big1": 8, "doppio_totale": 1.6},
    ])
    by = {r["id"]: r for r in res.rows}
    assert isinstance(by[1]["big1"], int) and by[1]["big1"] == BIG + 1
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["totale"] == "float" and fam["big1"] == "int" and fam["fascia"] == "str" and fam["resto"] == "int"
    assert [c.name for c in res.columns] == COLS + ["totale", "quarto", "fascia", "meta", "resto", "big1", "doppio_totale"]


@pytest.mark.parametrize("name", ENGINES)
def test_compute_strings_and_decimal(storage, orders, name):
    ops = [{"type": "compute", "params": {"columns": [
        {"name": "etichetta", "expr": "cliente || '-' || canale"},
        {"name": "mai", "expr": "upper(cliente)"},
        {"name": "lung", "expr": "length(cliente)"},
        {"name": "valore", "expr": "prezzo * qta"},
        {"name": "importo", "expr": "importo * 2"},  # overwrites an existing column, in place
    ]}}]
    res = check(name, storage, orders, ops, [
        {"id": 1, "etichetta": "Rossi-GDO", "mai": "ROSSI", "lung": 5, "valore": 125.0, "importo": 201.0},
        {"id": 2, "etichetta": "Bianchi-HoReCa", "mai": "BIANCHI", "lung": 7, "valore": 16.6665, "importo": 400.0},
        {"id": 3, "etichetta": "O'Neil-GDO", "mai": "O'NEIL", "lung": 6, "valore": None, "importo": None},
        {"id": 4, "etichetta": None, "mai": 'AC "ME"', "lung": 7, "valore": None, "importo": 100.5},
        {"id": 5, "etichetta": "Rossi-Dettaglio", "mai": "ROSSI", "lung": 5, "valore": 125.0, "importo": 201.0},
        {"id": 6, "etichetta": "Città Srl-GDO", "mai": "CITTÀ SRL", "lung": 9, "valore": 99999.9999, "importo": -60.0},
        {"id": 7, "etichetta": "Bianchi-HoReCa", "mai": "BIANCHI", "lung": 7, "valore": 2.0, "importo": 0.2},
        {"id": 8, "etichetta": None, "mai": None, "lung": None, "valore": 8.0, "importo": 0.4},
    ])
    assert [c.name for c in res.columns] == COLS + ["etichetta", "mai", "lung", "valore"]


@pytest.mark.parametrize("name", ENGINES)
def test_compute_with_from_inside_scalar_functions(storage, orders, name):
    """EXTRACT(… FROM …) and TRIM(… FROM …) are scalar: every engine accepts them
    (duckdb and BigQuery refused the word FROM until 2026-10-14)."""
    ops = [{"type": "compute", "params": {"columns": [
        {"name": "anno_mese", "expr": "EXTRACT(YEAR FROM data) * 100 + EXTRACT(MONTH FROM data)"},
        {"name": "pulito", "expr": "TRIM(BOTH ' ' FROM note)"},
    ]}}]
    check(name, storage, orders, ops, [
        {"id": 1, "anno_mese": 202401, "pulito": ""}, {"id": 2, "anno_mese": 202401, "pulito": None},
        {"id": 3, "anno_mese": 202402, "pulito": "x"}, {"id": 4, "anno_mese": 202402, "pulito": "spazi"},
        {"id": 5, "anno_mese": 202403, "pulito": ""}, {"id": 6, "anno_mese": None, "pulito": "y"},
        {"id": 7, "anno_mese": 202412, "pulito": "z"}, {"id": 8, "anno_mese": 202501, "pulito": "w"},
    ])


# ══════════════════════════════════════════════════════════════════════════════
# Cast
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_cast_text_to_int_float(storage, orders, name):
    # "x" → NULL (never an error); "2.7" is NOT an integer → NULL; " 4 " → 4 (spaces trimmed); "" → NULL
    res = check(name, storage, orders, [{"type": "cast", "params": {"columns": {"num_txt": "int"}}}], [
        {"id": 1, "num_txt": 1}, {"id": 2, "num_txt": 2}, {"id": 3, "num_txt": None}, {"id": 4, "num_txt": None},
        {"id": 5, "num_txt": 4}, {"id": 6, "num_txt": None}, {"id": 7, "num_txt": -5}, {"id": 8, "num_txt": None},
    ])
    assert _family(next(c.dtype for c in res.columns if c.name == "num_txt")) == "int"
    res = check(name, storage, orders, [{"type": "cast", "params": {"columns": {"num_txt": "float"}}}], [
        {"id": 1, "num_txt": 1.0}, {"id": 2, "num_txt": 2.0}, {"id": 3, "num_txt": None}, {"id": 4, "num_txt": 2.7},
        {"id": 5, "num_txt": 4.0}, {"id": 6, "num_txt": None}, {"id": 7, "num_txt": -5.0}, {"id": 8, "num_txt": None},
    ])
    assert _family(next(c.dtype for c in res.columns if c.name == "num_txt")) == "float"


@pytest.mark.parametrize("name", ENGINES)
def test_cast_float_to_int_truncates(storage, orders, name):
    res = check(name, storage, orders, [{"type": "cast", "params": {"columns": {"importo": "int", "prezzo": "int"}}}], [
        {"id": 1, "importo": 100, "prezzo": 12}, {"id": 2, "importo": 200, "prezzo": 3}, {"id": 3, "importo": None, "prezzo": None},
        {"id": 4, "importo": 50, "prezzo": 0}, {"id": 5, "importo": 100, "prezzo": 12}, {"id": 6, "importo": -30, "prezzo": 99999},
        {"id": 7, "importo": 0, "prezzo": 1}, {"id": 8, "importo": 0, "prezzo": 2},
    ])
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["importo"] == "int" and fam["prezzo"] == "int"


@pytest.mark.parametrize("name", ENGINES)
def test_cast_to_text_and_date_and_bool(storage, orders, name):
    res = check(name, storage, orders, [{"type": "cast", "params": {"columns": {"qta": "str", "data": "str", "importo": "str"}}}], [
        {"id": 1, "qta": "10", "data": "2024-01-05", "importo": "100.5"},
        {"id": 4, "qta": None, "data": "2024-02-29", "importo": "50.25"},
        {"id": 6, "qta": "1", "data": None, "importo": "-30.0"},
        {"id": 7, "qta": "2", "data": "2024-12-31", "importo": "0.1"},
    ] + [{"id": i, "qta": q, "data": d, "importo": im} for i, q, d, im in [
        (2, "5", "2024-01-15", "200.0"), (3, "3", "2024-02-01", None), (5, "10", "2024-03-10", "100.5"), (8, "4", "2025-01-01", "0.2"),
    ]])
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["qta"] == "str" and fam["data"] == "str" and fam["importo"] == "str"

    res = check(name, storage, orders, [{"type": "cast", "params": {"columns": {"data_txt": "date", "attivo": "int"}}}], [
        {"id": 1, "data_txt": D(2024, 1, 5), "attivo": 1}, {"id": 2, "data_txt": None, "attivo": 0},
        {"id": 3, "data_txt": None, "attivo": None}, {"id": 4, "data_txt": None, "attivo": 1},
        {"id": 5, "data_txt": D(2025, 12, 31), "attivo": 1}, {"id": 6, "data_txt": None, "attivo": 0},
        {"id": 7, "data_txt": None, "attivo": 1}, {"id": 8, "data_txt": D(1999, 9, 9), "attivo": None},
    ])
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["data_txt"] == "date" and fam["attivo"] == "int"
    # date → a date filter right after the cast (the type is really temporal)
    res = run(name, storage, orders, [{"type": "cast", "params": {"columns": {"data_txt": "date"}}}, filt("data_txt", "gt", "2000-01-01")])
    assert ids(res) == [1, 5]


@pytest.mark.parametrize("name", ENGINES)
def test_cast_to_datetime(storage, orders, name):
    T = dt.datetime
    res = check(name, storage, orders, [{"type": "cast", "params": {"columns": {"ts_txt": "datetime", "data": "datetime"}}}], [
        {"id": 1, "ts_txt": T(2024, 1, 5, 10, 30), "data": T(2024, 1, 5)},
        {"id": 2, "ts_txt": T(2024, 1, 5, 10, 30), "data": T(2024, 1, 15)},
        {"id": 3, "ts_txt": None, "data": T(2024, 2, 1)},
        {"id": 4, "ts_txt": None, "data": T(2024, 2, 29)},
        {"id": 5, "ts_txt": T(2025, 12, 31, 23, 59, 59), "data": T(2024, 3, 10)},
        {"id": 6, "ts_txt": None, "data": None},
        {"id": 7, "ts_txt": T(1999, 9, 9), "data": T(2024, 12, 31)},
        {"id": 8, "ts_txt": T(2024, 6, 30, 12), "data": T(2025, 1, 1)},
    ])
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["ts_txt"] == "datetime" and fam["data"] == "datetime"
    # the type is really temporal: a filter with an ISO string right after
    res = run(name, storage, orders, [{"type": "cast", "params": {"columns": {"ts_txt": "datetime"}}}, filt("ts_txt", "gt", "2024-01-05T10:00:00")])
    assert ids(res) == [1, 2, 5, 8]


# ══════════════════════════════════════════════════════════════════════════════
# Realistic chains
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_chain_top_clients(storage, orders, name):
    ops = [
        filt("canale", "in", ["GDO", "HoReCa"]),
        {"type": "group_by", "params": {"by": ["cliente"], "aggregations": [_agg("importo", "sum", "fatturato")]}},
        {"type": "sort", "params": {"by": "fatturato", "descending": True}},
        {"type": "limit", "params": {"n": 2}},
    ]
    check(name, storage, orders, ops, [{"cliente": "Bianchi", "fatturato": 200.1}, {"cliente": "Rossi", "fatturato": 100.5}], ordered=True)


@pytest.mark.parametrize("name", ENGINES)
def test_chain_join_then_aggregate_then_pivot(storage, orders, customers, name):
    ops = [
        _join("left", customers),
        {"type": "fill_null", "params": {"columns": {"regione": "n/d"}}},
        {"type": "group_by", "params": {"by": ["regione", "canale"], "aggregations": [_agg("importo", "sum", "tot")]}},
        {"type": "pivot", "params": {"index": ["regione"], "on": "canale", "values": "tot", "func": "sum"}},
    ]
    res = check(name, storage, orders, ops, [
        {"regione": "Lazio", "Dettaglio": 100.5, "GDO": 100.5, "HoReCa": None, "null": None},
        {"regione": "Lombardia", "Dettaglio": None, "GDO": None, "HoReCa": 200.1, "null": None},
        {"regione": "Irlanda", "Dettaglio": None, "GDO": None, "HoReCa": None, "null": None},  # importo NULL → NULL, not 0
        {"regione": "n/d", "Dettaglio": None, "GDO": -29.8, "HoReCa": None, "null": 50.25},
    ], columns=["regione", "Dettaglio", "GDO", "HoReCa", "null"])


@pytest.mark.parametrize("name", ENGINES)
def test_chain_empty_intermediate_result_keeps_schema(storage, orders, customers, name):
    ops = [
        filt("id", "eq", 999),
        _join("left", customers),
        {"type": "compute", "params": {"columns": [{"name": "x", "expr": "importo * 2"}]}},
        {"type": "sort", "params": {"by": "x", "descending": True}},
        {"type": "limit", "params": {"n": 5}},
    ]
    res = run(name, storage, orders, ops)
    assert res.row_count == 0 and [c.name for c in res.columns] == COLS + ["regione", "canale_right", "x"]


# ══════════════════════════════════════════════════════════════════════════════
# Run (writing parquet): the file made has the same values and types as the preview
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_run_output_matches_preview_and_keeps_types(storage, orders, customers, name):
    from app.engine.base import DataSource

    ops = [
        _join("left", customers),
        {"type": "compute", "params": {"columns": [{"name": "valore", "expr": "prezzo * qta"}]}},
        filt("id", "ne", 3),
    ]
    eng = _engine(name, storage)
    dest = DataSource(bucket=BUCKET, key=f"out/corr_{name}.parquet")
    result = eng.run(orders, ops, dest, use_cache=False)
    assert result.rows_written == 7
    import os as _os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".parquet")
    _os.close(fd)
    try:
        storage.download_file(BUCKET, dest.key, path)
        out = pl.read_parquet(path)
    finally:
        _os.unlink(path)
    assert out.height == 7
    assert out.columns == COLS + ["regione", "canale_right", "valore"]
    assert out.schema["data"] == pl.Date and out.schema["attivo"] == pl.Boolean
    assert isinstance(out.schema["ts"], pl.Datetime) and out.schema["ts"].time_zone is None  # naive
    assert out.schema["id"] in (pl.Int64, pl.Int32) and out.schema["big"] == pl.Int64
    assert out.schema["importo"] == pl.Float64 and out.schema["cliente"] == pl.String
    prev = _rows(eng.preview(orders, ops, limit=100, use_cache=False))
    got = [{c: _norm(v) for c, v in r.items()} for r in out.to_dicts()]
    assert sorted(got, key=_key) == sorted(prev, key=_key)
    by = {r["id"]: r for r in got}
    assert by[1]["big"] == BIG and by[1]["regione"] == "Lazio" and by[8]["regione"] is None and by[4]["note"] == "  spazi  "


@pytest.mark.parametrize("name", ENGINES)
def test_join_left_missing_side_of_a_column_computed_on_the_right_is_null(storage, orders, customers, name):
    # a column born on the right (a constant) is not Nullable on ClickHouse: without
    # join_use_nulls the unmatched rows got '' instead of NULL
    right = {"source": customers, "operations": [{"type": "compute", "params": {"columns": [{"name": "etichetta", "expr": "'fissa'"}]}}]}
    res = run(name, storage, orders, [{"type": "join", "params": {"right": right, "how": "left", "on": ["cliente"]}}])
    by = {r["id"]: r for r in _rows(res)}
    assert by[1]["etichetta"] == "fissa"
    assert by[4]["etichetta"] is None and by[6]["etichetta"] is None and by[8]["etichetta"] is None


@pytest.mark.parametrize("name", ENGINES)
def test_cast_of_an_instant_before_1970_to_date_keeps_the_day(storage, name):
    src = upload_df(storage, pl.DataFrame({"id": [1, 2], "istante": [dt.datetime(1965, 6, 1, 10, 30), dt.datetime(2024, 2, 29, 12)]}),
                    "datasets/istanti_corr.parquet")
    res = run(name, storage, src, [{"type": "cast", "params": {"columns": {"istante": "date"}}}])
    assert [_norm(r["istante"]) for r in sorted(_rows(res), key=lambda r: r["id"])] == [_norm(D(1965, 6, 1)), _norm(D(2024, 2, 29))]
