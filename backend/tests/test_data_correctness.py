"""CORRETTEZZA DEI DATI del core, con ORACOLO CALCOLATO A MANO, su tutti gli engine.

A differenza dei test di parità (che confrontano gli engine fra loro) qui ogni
caso dichiara il risultato ATTESO, calcolato a mano su un dataset piccolo ma
"cattivo": NULL ovunque, stringhe con apostrofi/virgolette/accenti/spazi, la
stringa vuota diversa da NULL, nomi di colonna con spazi e maiuscole, decimali
(numeric di Postgres), interi oltre 2^53, date/booleani con NULL.

Semantica di riferimento (= SQL, che è ciò che gira in produzione):
- confronti con NULL sono falsi (ne/not_in NON tengono i NULL);
- gli aggregati ignorano i NULL; count/n_unique non contano i NULL;
  sum/mean/min/max di un gruppo tutto NULL → NULL; std/var campionari (n-1),
  NULL con meno di 2 valori; median = interpolazione lineare (R7);
- ordinamento: NULL SEMPRE in coda (asc e desc): "top N" non prende i NULL;
- cast falliti → NULL (mai errore); numeri → interi per troncamento;
  stringhe numeriche con spazi attorno → tagliate;
- join: chiave NULL non incrocia mai; lato mancante → NULL (mai default);
  colonna omonima non-chiave a destra → suffisso `_right`.

DuckDB/chDB girano se i pacchetti sono installati (nel container sì); ClickHouse
esterno se `CLICKHOUSE_TEST_HOST` è impostata (vedi test_clickhouse_engine.py).
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


ENGINES = [
    "polars",
    pytest.param("duckdb", marks=pytest.mark.skipif(importlib.util.find_spec("duckdb") is None, reason="duckdb assente")),
    pytest.param("chdb", marks=pytest.mark.skipif(importlib.util.find_spec("chdb") is None, reason="chdb assente")),
    pytest.param("clickhouse", marks=pytest.mark.skipif(not os.getenv("CLICKHOUSE_TEST_HOST"), reason="serve CLICKHOUSE_TEST_HOST")),
]

D = dt.date
BIG = 9007199254740993  # 2^53 + 1: non rappresentabile come float64


# ── Dataset "ordini": 8 righe, 15 colonne ──────────────────────────────────────
ORDINI = pl.DataFrame({
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

COLS = ORDINI.columns

CLIENTI = pl.DataFrame({
    "cliente": ["Rossi", "Bianchi", "O'Neil", "Verdi", None],
    "regione": ["Lazio", "Lombardia", "Irlanda", "Veneto", "Nessuna"],
    "canale": ["c1", "c2", "c3", "c4", "c5"],  # omonima NON chiave → `canale_right`
})


@pytest.fixture
def ordini(storage):
    return upload_df(storage, ORDINI, "datasets/ordini_corr.parquet")


@pytest.fixture
def clienti(storage):
    upload_df(storage, CLIENTI, "datasets/clienti_corr.parquet")
    upload_df(storage, CLIENTI.rename({"cliente": "nome"}), "datasets/clienti_nome_corr.parquet")
    return {"bucket": "data-prep", "key": "datasets/clienti_corr.parquet"}


# ── Normalizzazione dei risultati ─────────────────────────────────────────────
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
    """Confronta il risultato con le righe attese (sottoinsieme di colonne ok)."""
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
# Selezione / rinomina / nomi "difficili"
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_select_reorder_drop_rename_with_hard_names(storage, ordini, name):
    res = run(name, storage, ordini, [{"type": "select", "params": {"columns": ["Ragione Sociale", "id"]}}])
    assert [c.name for c in res.columns] == ["Ragione Sociale", "id"] and res.row_count == 8
    res = run(name, storage, ordini, [{"type": "reorder", "params": {"columns": ["note", "id", "cliente"]}}])
    assert [c.name for c in res.columns][:3] == ["note", "id", "cliente"] and len(res.columns) == 15
    res = run(name, storage, ordini, [{"type": "drop", "params": {"columns": ["Ragione Sociale", "prezzo"]}}])
    assert [c.name for c in res.columns] == [c for c in COLS if c not in ("Ragione Sociale", "prezzo")]
    # rinomina verso un nome con spazi, parentesi e simbolo, poi filtro su quello
    res = check(name, storage, ordini, [
        {"type": "rename", "params": {"mapping": {"importo": "Importo (€)"}}},
        filt("Importo (€)", "gt", 100),
    ], [{"id": 1, "Importo (€)": 100.5}, {"id": 2, "Importo (€)": 200.0}, {"id": 5, "Importo (€)": 100.5}])
    assert "importo" not in [c.name for c in res.columns]


@pytest.mark.parametrize("name", ENGINES)
def test_values_survive_untouched(storage, ordini, name):
    """Nessuna operazione: i valori tornano ESATTI (spazi, vuoto ≠ NULL, big int, decimali)."""
    res = run(name, storage, ordini, [])
    by = {r["id"]: r for r in res.rows}
    assert by[4]["note"] == "  spazi  "
    assert by[1]["note"] == "" and by[2]["note"] is None
    assert by[3]["cliente"] == "O'Neil" and by[4]["cliente"] == 'Ac "me"' and by[6]["cliente"] == "Città Srl"
    assert by[1]["big"] == BIG and isinstance(by[1]["big"], int)
    assert _norm(by[6]["prezzo"]) == 99999.9999 and _norm(by[4]["prezzo"]) == 0.0001
    assert _norm(by[1]["data"]) == "2024-01-05" and by[6]["data"] is None
    # datetime NAIVE (istante UTC, senza fuso): nessun "+00:00", nessun epoch
    assert _norm(by[1]["ts"]) == "2024-01-05T10:30:00" and _norm(by[8]["ts"]) == "2025-01-01T00:00:01" and by[6]["ts"] is None
    assert by[1]["attivo"] is True and by[2]["attivo"] is False and by[3]["attivo"] is None
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["id"] == "int" and fam["importo"] == "float" and fam["data"] == "date"
    assert fam["attivo"] == "bool" and fam["cliente"] == "str" and fam["prezzo"] == "float"
    assert fam["ts"] == "datetime"


# ══════════════════════════════════════════════════════════════════════════════
# Filtri
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
}


@pytest.mark.parametrize("case", list(FILTER_CASES))
@pytest.mark.parametrize("name", ENGINES)
def test_filter(storage, ordini, name, case):
    op, want = FILTER_CASES[case]
    res = run(name, storage, ordini, [op])
    assert ids(res) == want, f"[{name}] {case}"
    assert [c.name for c in res.columns] == COLS  # il filtro non tocca lo schema


@pytest.mark.parametrize("name", ENGINES)
def test_filter_chain_is_and(storage, ordini, name):
    res = run(name, storage, ordini, [filt("canale", "eq", "GDO"), filt("importo", "is_not_null"), filt("qta", "gt", 1)])
    assert ids(res) == [1, 8]


# ══════════════════════════════════════════════════════════════════════════════
# Ordinamento, limit, top-N: i NULL vanno SEMPRE in coda
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_sort_desc_nulls_last_top_n(storage, ordini, name):
    res = run(name, storage, ordini, [
        {"type": "sort", "params": {"by": ["importo", "id"], "descending": True}},
        {"type": "limit", "params": {"n": 3}},
    ])
    assert [r["id"] for r in res.rows] == [2, 5, 1]


@pytest.mark.parametrize("name", ENGINES)
def test_sort_asc_nulls_last_bottom_n(storage, ordini, name):
    res = run(name, storage, ordini, [
        {"type": "sort", "params": {"by": "importo", "descending": False}},
        {"type": "limit", "params": {"n": 2}},
    ])
    assert [r["id"] for r in res.rows] == [6, 7]


@pytest.mark.parametrize("name", ENGINES)
def test_sort_full_order_with_nulls_and_strings(storage, ordini, name):
    res = run(name, storage, ordini, [{"type": "sort", "params": {"by": "importo", "descending": False}}])
    assert [r["id"] for r in res.rows][-1] == 3  # il NULL chiude
    assert [r["id"] for r in res.rows][:3] == [6, 7, 8]
    res = run(name, storage, ordini, [{"type": "sort", "params": {"by": "importo", "descending": True}}])
    assert [r["id"] for r in res.rows][-1] == 3 and [r["id"] for r in res.rows][0] == 2
    res = run(name, storage, ordini, [{"type": "sort", "params": {"by": "data", "descending": True}}])
    assert [r["id"] for r in res.rows][:2] == [8, 7] and [r["id"] for r in res.rows][-1] == 6


@pytest.mark.parametrize("name", ENGINES)
def test_limit(storage, ordini, name):
    assert run(name, storage, ordini, [{"type": "limit", "params": {"n": 3}}]).row_count == 3
    assert run(name, storage, ordini, [{"type": "limit", "params": {"n": 100}}]).row_count == 8


# ══════════════════════════════════════════════════════════════════════════════
# Unique / null handling
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_unique_subset_keeps_null_group(storage, ordini, name):
    res = run(name, storage, ordini, [{"type": "unique", "params": {"subset": ["cliente"]}}])
    assert sorted((r["cliente"] or "") for r in res.rows) == sorted(["Rossi", "Bianchi", "O'Neil", 'Ac "me"', "Città Srl", ""])
    assert [c.name for c in res.columns] == COLS


@pytest.mark.parametrize("name", ENGINES)
def test_unique_full_rows(storage, ordini, name):
    res = run(name, storage, ordini, [
        {"type": "select", "params": {"columns": ["cliente", "canale"]}},
        {"type": "unique", "params": {}},
    ])
    assert res.row_count == 7  # (Bianchi, HoReCa) compare due volte


@pytest.mark.parametrize("name", ENGINES)
def test_fill_null_and_drop_nulls(storage, ordini, name):
    res = run(name, storage, ordini, [{"type": "fill_null", "params": {"columns": {"importo": 0, "canale": "n/d"}}}])
    by = {r["id"]: r for r in res.rows}
    assert _norm(by[3]["importo"]) == 0.0 and by[4]["canale"] == "n/d"
    assert by[1]["canale"] == "GDO" and _norm(by[1]["importo"]) == 100.5
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["importo"] == "float" and fam["canale"] == "str"

    res = run(name, storage, ordini, [{"type": "drop_nulls", "params": {"subset": ["importo", "qta"]}}])
    assert ids(res) == [1, 2, 5, 6, 7, 8]
    res = run(name, storage, ordini, [{"type": "drop_nulls", "params": {}}])
    assert ids(res) == [1, 5, 7]


# ══════════════════════════════════════════════════════════════════════════════
# Aggregazione
# ══════════════════════════════════════════════════════════════════════════════
def _agg(col, func, alias):
    return {"column": col, "func": func, "alias": alias}


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_basic_aggregates(storage, ordini, name):
    ops = [{"type": "group_by", "params": {"by": ["canale"], "aggregations": [
        _agg("importo", "sum", "somma"), _agg("importo", "count", "n_importi"), _agg("importo", "mean", "media"),
        _agg("qta", "min", "qmin"), _agg("qta", "max", "qmax"), _agg("cliente", "n_unique", "n_clienti"),
        _agg("id", "count", "n"),
    ]}}]
    res = check(name, storage, ordini, ops, [
        {"canale": "GDO", "somma": 70.7, "n_importi": 3, "media": 70.7 / 3, "qmin": 1, "qmax": 10, "n_clienti": 3, "n": 4},
        {"canale": "HoReCa", "somma": 200.1, "n_importi": 2, "media": 100.05, "qmin": 2, "qmax": 5, "n_clienti": 1, "n": 2},
        {"canale": "Dettaglio", "somma": 100.5, "n_importi": 1, "media": 100.5, "qmin": 10, "qmax": 10, "n_clienti": 1, "n": 1},
        {"canale": None, "somma": 50.25, "n_importi": 1, "media": 50.25, "qmin": None, "qmax": None, "n_clienti": 1, "n": 1},
    ], columns=["canale", "somma", "n_importi", "media", "qmin", "qmax", "n_clienti", "n"])
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["somma"] == "float" and fam["n_importi"] == "int" and fam["n"] == "int" and fam["n_clienti"] == "int"
    assert fam["qmin"] == "int" and fam["qmax"] == "int"


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_datetime_min_max(storage, ordini, name):
    T = dt.datetime
    ops = [{"type": "group_by", "params": {"by": ["canale"], "aggregations": [_agg("ts", "min", "primo"), _agg("ts", "max", "ultimo"), _agg("data", "max", "gg")]}}]
    res = check(name, storage, ordini, ops, [
        {"canale": "GDO", "primo": T(2024, 1, 5, 10, 30), "ultimo": T(2025, 1, 1, 0, 0, 1), "gg": D(2025, 1, 1)},
        {"canale": "HoReCa", "primo": T(2024, 1, 15, 8, 0), "ultimo": T(2024, 12, 31, 18, 45), "gg": D(2024, 12, 31)},
        {"canale": "Dettaglio", "primo": T(2024, 3, 10), "ultimo": T(2024, 3, 10), "gg": D(2024, 3, 10)},
        {"canale": None, "primo": T(2024, 2, 29, 12), "ultimo": T(2024, 2, 29, 12), "gg": D(2024, 2, 29)},
    ])
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["primo"] == "datetime" and fam["gg"] == "date"


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_all_null_group_is_null(storage, ordini, name):
    ops = [{"type": "group_by", "params": {"by": ["cliente"], "aggregations": [
        _agg("importo", "sum", "somma"), _agg("importo", "mean", "media"), _agg("importo", "max", "mx"), _agg("importo", "count", "n"),
    ]}}]
    check(name, storage, ordini, ops, [
        {"cliente": "Rossi", "somma": 201.0, "media": 100.5, "mx": 100.5, "n": 2},
        {"cliente": "Bianchi", "somma": 200.1, "media": 100.05, "mx": 200.0, "n": 2},
        {"cliente": "O'Neil", "somma": None, "media": None, "mx": None, "n": 0},
        {"cliente": 'Ac "me"', "somma": 50.25, "media": 50.25, "mx": 50.25, "n": 1},
        {"cliente": "Città Srl", "somma": -30.0, "media": -30.0, "mx": -30.0, "n": 1},
        {"cliente": None, "somma": 0.2, "media": 0.2, "mx": 0.2, "n": 1},
    ])


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_std_var_median(storage, ordini, name):
    ops = [{"type": "group_by", "params": {"by": ["canale"], "aggregations": [
        _agg("importo", "std", "sd"), _agg("importo", "var", "vr"), _agg("importo", "median", "med"),
    ]}}]
    gdo = [100.5, -30.0, 0.2]
    horeca = [200.0, 0.1]
    check(name, storage, ordini, ops, [
        {"canale": "GDO", "sd": statistics.stdev(gdo), "vr": statistics.variance(gdo), "med": 0.2},
        {"canale": "HoReCa", "sd": statistics.stdev(horeca), "vr": statistics.variance(horeca), "med": 100.05},
        {"canale": "Dettaglio", "sd": None, "vr": None, "med": 100.5},  # 1 solo valore: std/var campionarie non definite
        {"canale": None, "sd": None, "vr": None, "med": 50.25},
    ])


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_multiple_keys_with_nulls(storage, ordini, name):
    ops = [{"type": "group_by", "params": {"by": ["canale", "attivo"], "aggregations": [_agg("id", "count", "n")]}}]
    check(name, storage, ordini, ops, [
        {"canale": "GDO", "attivo": True, "n": 1},
        {"canale": "GDO", "attivo": None, "n": 2},
        {"canale": "GDO", "attivo": False, "n": 1},
        {"canale": "HoReCa", "attivo": False, "n": 1},
        {"canale": "HoReCa", "attivo": True, "n": 1},
        {"canale": None, "attivo": True, "n": 1},
        {"canale": "Dettaglio", "attivo": True, "n": 1},
    ])


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_decimal_and_bigint(storage, ordini, name):
    ops = [{"type": "group_by", "params": {"by": ["canale"], "aggregations": [
        _agg("prezzo", "sum", "p"), _agg("big", "sum", "b"), _agg("big", "max", "bm"),
    ]}}]
    res = check(name, storage, ordini, ops, [
        {"canale": "GDO", "p": 100014.4999, "b": BIG + 2 + 5 + 7, "bm": BIG},
        {"canale": "HoReCa", "p": 4.3333, "b": 1 + 6, "bm": 6},
        {"canale": "Dettaglio", "p": 12.5, "b": 4, "bm": 4},
        {"canale": None, "p": 0.0001, "b": 3, "bm": 3},
    ])
    by = {r["canale"]: r for r in res.rows}
    assert isinstance(by["GDO"]["b"], int) and by["GDO"]["b"] == BIG + 14  # esatto, non float


@pytest.mark.parametrize("name", ENGINES)
def test_group_by_on_empty_input(storage, ordini, name):
    res = run(name, storage, ordini, [
        filt("id", "eq", 999),
        {"type": "group_by", "params": {"by": ["canale"], "aggregations": [_agg("importo", "sum", "s")]}},
    ])
    assert res.row_count == 0 and [c.name for c in res.columns] == ["canale", "s"]


# ══════════════════════════════════════════════════════════════════════════════
# Join / union
# ══════════════════════════════════════════════════════════════════════════════
def _join(how, clienti, **kw):
    params = {"right": {"source": clienti}, "how": how}
    params.update(kw or {"on": ["cliente"]})
    return {"type": "join", "params": params}


@pytest.mark.parametrize("name", ENGINES)
def test_join_inner_null_key_never_matches_and_collision_suffix(storage, ordini, clienti, name):
    res = check(name, storage, ordini, [_join("inner", clienti)], [
        {"id": 1, "cliente": "Rossi", "regione": "Lazio", "canale": "GDO", "canale_right": "c1"},
        {"id": 2, "cliente": "Bianchi", "regione": "Lombardia", "canale": "HoReCa", "canale_right": "c2"},
        {"id": 3, "cliente": "O'Neil", "regione": "Irlanda", "canale": "GDO", "canale_right": "c3"},
        {"id": 5, "cliente": "Rossi", "regione": "Lazio", "canale": "Dettaglio", "canale_right": "c1"},
        {"id": 7, "cliente": "Bianchi", "regione": "Lombardia", "canale": "HoReCa", "canale_right": "c2"},
    ])
    assert [c.name for c in res.columns] == COLS + ["regione", "canale_right"]


@pytest.mark.parametrize("name", ENGINES)
def test_join_left_missing_side_is_null(storage, ordini, clienti, name):
    res = check(name, storage, ordini, [_join("left", clienti)], [
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
    assert by[8]["cliente"] is None and by[4]["importo"] == 50.25  # le colonne di sinistra restano intatte


@pytest.mark.parametrize("name", ENGINES)
def test_join_semi_anti_keep_only_left_columns(storage, ordini, clienti, name):
    res = run(name, storage, ordini, [_join("semi", clienti)])
    assert ids(res) == [1, 2, 3, 5, 7] and [c.name for c in res.columns] == COLS
    res = run(name, storage, ordini, [_join("anti", clienti)])
    assert ids(res) == [4, 6, 8] and [c.name for c in res.columns] == COLS


@pytest.mark.parametrize("name", ENGINES)
def test_join_full_and_right(storage, ordini, clienti, name):
    res = run(name, storage, ordini, [_join("full", clienti)])
    assert res.row_count == 10
    regioni = sorted((r["regione"] or "") for r in res.rows)
    assert regioni == sorted(["Lazio", "Lazio", "Lombardia", "Lombardia", "Irlanda", "Veneto", "Nessuna", "", "", ""])
    # righe solo-destra: le colonne di sinistra sono NULL, la chiave è UNA sola colonna valorizzata
    only_right = [r for r in res.rows if r.get("id") is None]
    assert len(only_right) == 2
    assert sorted((r["cliente"] or "") for r in only_right) == ["", "Verdi"]
    assert "cliente_right" not in [c.name for c in res.columns]

    res = run(name, storage, ordini, [_join("right", clienti)])
    assert res.row_count == 7 and [c.name for c in res.columns] == COLS + ["regione", "canale_right"]
    assert sorted((r["regione"] or "") for r in res.rows) == sorted(["Lazio", "Lazio", "Lombardia", "Lombardia", "Irlanda", "Veneto", "Nessuna"])
    assert sorted(r["id"] for r in res.rows if r["id"] is not None) == [1, 2, 3, 5, 7]
    # la chiave è valorizzata anche nelle righe solo-destra (Verdi); il NULL di destra resta NULL
    assert sorted((r["cliente"] or "") for r in res.rows if r["id"] is None) == ["", "Verdi"]


@pytest.mark.parametrize("name", ENGINES)
def test_join_different_key_names(storage, ordini, clienti, name):
    right = {"bucket": "data-prep", "key": "datasets/clienti_nome_corr.parquet"}
    res = check(name, storage, ordini, [_join("inner", right, left_on=["cliente"], right_on=["nome"])], [
        {"id": 1, "regione": "Lazio"}, {"id": 2, "regione": "Lombardia"}, {"id": 3, "regione": "Irlanda"},
        {"id": 5, "regione": "Lazio"}, {"id": 7, "regione": "Lombardia"},
    ])
    cols = [c.name for c in res.columns]
    # nomi diversi: restano ENTRAMBE le chiavi (come SQL ON), più le altre di destra
    assert cols == COLS + ["nome", "regione", "canale_right"]
    res = run(name, storage, ordini, [_join("left", right, left_on=["cliente"], right_on=["nome"])])
    assert res.row_count == 8 and sum(1 for r in res.rows if r["regione"] is None) == 3


@pytest.mark.parametrize("name", ENGINES)
def test_join_duplicates_multiply_rows(storage, ordini, clienti, name):
    # destra con chiave duplicata (Rossi ×2) → ogni Rossi di sinistra esce 2 volte
    dup = pl.DataFrame({"cliente": ["Rossi", "Rossi", "Bianchi"], "tag": ["a", "b", "c"]})
    upload_df(storage, dup, "datasets/dup_corr.parquet")
    right = {"bucket": "data-prep", "key": "datasets/dup_corr.parquet"}
    res = run(name, storage, ordini, [_join("inner", right)])
    assert sorted((r["id"], r["tag"]) for r in res.rows) == [(1, "a"), (1, "b"), (2, "c"), (5, "a"), (5, "b"), (7, "c")]
    res = run(name, storage, ordini, [_join("left", right)])
    assert res.row_count == 8 + 2  # 1 e 5 raddoppiano


@pytest.mark.parametrize("name", ENGINES)
def test_union_relaxed_and_strict(storage, ordini, clienti, name):
    res = run(name, storage, ordini, [{"type": "union", "params": {"right": {"source": clienti}, "strategy": "relaxed"}}])
    assert [c.name for c in res.columns] == COLS + ["regione"]
    assert res.row_count == 13
    assert sum(1 for r in res.rows if r["id"] is None) == 5 and sum(1 for r in res.rows if r["regione"] is None) == 8
    assert sorted((r["canale"] or "") for r in res.rows if r["id"] is None) == ["c1", "c2", "c3", "c4", "c5"]
    # strict: stesso schema (la sorgente con sé stessa, filtrata a destra)
    res = run(name, storage, ordini, [{"type": "union", "params": {
        "right": {"source": {"bucket": "data-prep", "key": "datasets/ordini_corr.parquet"}, "operations": [filt("id", "le", 2)]},
        "strategy": "strict",
    }}])
    assert [c.name for c in res.columns] == COLS and res.row_count == 10
    assert sorted(r["id"] for r in res.rows) == [1, 1, 2, 2, 3, 4, 5, 6, 7, 8]


# ══════════════════════════════════════════════════════════════════════════════
# Colonne calcolate
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_compute_arithmetic_null_propagation_and_case(storage, ordini, name):
    ops = [{"type": "compute", "params": {"columns": [
        {"name": "totale", "expr": "importo * qta"},
        {"name": "quarto", "expr": "importo / 4"},
        {"name": "fascia", "expr": "CASE WHEN importo > 100 THEN 'alto' ELSE 'basso' END"},
        {"name": "meta", "expr": "qta / 4"},
        {"name": "resto", "expr": "qta % 3"},
        {"name": "big1", "expr": "big + 1"},
        {"name": "doppio_totale", "expr": "totale * 2"},  # usa la colonna definita sopra
    ]}}]
    res = check(name, storage, ordini, ops, [
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
def test_compute_strings_and_decimal(storage, ordini, name):
    ops = [{"type": "compute", "params": {"columns": [
        {"name": "etichetta", "expr": "cliente || '-' || canale"},
        {"name": "mai", "expr": "upper(cliente)"},
        {"name": "lung", "expr": "length(cliente)"},
        {"name": "valore", "expr": "prezzo * qta"},
        {"name": "importo", "expr": "importo * 2"},  # sovrascrive una colonna esistente, in posizione
    ]}}]
    res = check(name, storage, ordini, ops, [
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


# ══════════════════════════════════════════════════════════════════════════════
# Cast
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_cast_text_to_int_float(storage, ordini, name):
    # "x" → NULL (mai errore); "2.7" NON è un intero → NULL; " 4 " → 4 (spazi tagliati); "" → NULL
    res = check(name, storage, ordini, [{"type": "cast", "params": {"columns": {"num_txt": "int"}}}], [
        {"id": 1, "num_txt": 1}, {"id": 2, "num_txt": 2}, {"id": 3, "num_txt": None}, {"id": 4, "num_txt": None},
        {"id": 5, "num_txt": 4}, {"id": 6, "num_txt": None}, {"id": 7, "num_txt": -5}, {"id": 8, "num_txt": None},
    ])
    assert _family(next(c.dtype for c in res.columns if c.name == "num_txt")) == "int"
    res = check(name, storage, ordini, [{"type": "cast", "params": {"columns": {"num_txt": "float"}}}], [
        {"id": 1, "num_txt": 1.0}, {"id": 2, "num_txt": 2.0}, {"id": 3, "num_txt": None}, {"id": 4, "num_txt": 2.7},
        {"id": 5, "num_txt": 4.0}, {"id": 6, "num_txt": None}, {"id": 7, "num_txt": -5.0}, {"id": 8, "num_txt": None},
    ])
    assert _family(next(c.dtype for c in res.columns if c.name == "num_txt")) == "float"


@pytest.mark.parametrize("name", ENGINES)
def test_cast_float_to_int_truncates(storage, ordini, name):
    res = check(name, storage, ordini, [{"type": "cast", "params": {"columns": {"importo": "int", "prezzo": "int"}}}], [
        {"id": 1, "importo": 100, "prezzo": 12}, {"id": 2, "importo": 200, "prezzo": 3}, {"id": 3, "importo": None, "prezzo": None},
        {"id": 4, "importo": 50, "prezzo": 0}, {"id": 5, "importo": 100, "prezzo": 12}, {"id": 6, "importo": -30, "prezzo": 99999},
        {"id": 7, "importo": 0, "prezzo": 1}, {"id": 8, "importo": 0, "prezzo": 2},
    ])
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["importo"] == "int" and fam["prezzo"] == "int"


@pytest.mark.parametrize("name", ENGINES)
def test_cast_to_text_and_date_and_bool(storage, ordini, name):
    res = check(name, storage, ordini, [{"type": "cast", "params": {"columns": {"qta": "str", "data": "str", "importo": "str"}}}], [
        {"id": 1, "qta": "10", "data": "2024-01-05", "importo": "100.5"},
        {"id": 4, "qta": None, "data": "2024-02-29", "importo": "50.25"},
        {"id": 6, "qta": "1", "data": None, "importo": "-30.0"},
        {"id": 7, "qta": "2", "data": "2024-12-31", "importo": "0.1"},
    ] + [{"id": i, "qta": q, "data": d, "importo": im} for i, q, d, im in [
        (2, "5", "2024-01-15", "200.0"), (3, "3", "2024-02-01", None), (5, "10", "2024-03-10", "100.5"), (8, "4", "2025-01-01", "0.2"),
    ]])
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["qta"] == "str" and fam["data"] == "str" and fam["importo"] == "str"

    res = check(name, storage, ordini, [{"type": "cast", "params": {"columns": {"data_txt": "date", "attivo": "int"}}}], [
        {"id": 1, "data_txt": D(2024, 1, 5), "attivo": 1}, {"id": 2, "data_txt": None, "attivo": 0},
        {"id": 3, "data_txt": None, "attivo": None}, {"id": 4, "data_txt": None, "attivo": 1},
        {"id": 5, "data_txt": D(2025, 12, 31), "attivo": 1}, {"id": 6, "data_txt": None, "attivo": 0},
        {"id": 7, "data_txt": None, "attivo": 1}, {"id": 8, "data_txt": D(1999, 9, 9), "attivo": None},
    ])
    fam = {c.name: _family(c.dtype) for c in res.columns}
    assert fam["data_txt"] == "date" and fam["attivo"] == "int"
    # data → filtro per data subito dopo il cast (il tipo è davvero temporale)
    res = run(name, storage, ordini, [{"type": "cast", "params": {"columns": {"data_txt": "date"}}}, filt("data_txt", "gt", "2000-01-01")])
    assert ids(res) == [1, 5]


@pytest.mark.parametrize("name", ENGINES)
def test_cast_to_datetime(storage, ordini, name):
    T = dt.datetime
    res = check(name, storage, ordini, [{"type": "cast", "params": {"columns": {"ts_txt": "datetime", "data": "datetime"}}}], [
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
    # il tipo è davvero temporale: filtro con stringa ISO subito dopo
    res = run(name, storage, ordini, [{"type": "cast", "params": {"columns": {"ts_txt": "datetime"}}}, filt("ts_txt", "gt", "2024-01-05T10:00:00")])
    assert ids(res) == [1, 2, 5, 8]


# ══════════════════════════════════════════════════════════════════════════════
# Catene realistiche
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_chain_top_clients(storage, ordini, name):
    ops = [
        filt("canale", "in", ["GDO", "HoReCa"]),
        {"type": "group_by", "params": {"by": ["cliente"], "aggregations": [_agg("importo", "sum", "fatturato")]}},
        {"type": "sort", "params": {"by": "fatturato", "descending": True}},
        {"type": "limit", "params": {"n": 2}},
    ]
    check(name, storage, ordini, ops, [{"cliente": "Bianchi", "fatturato": 200.1}, {"cliente": "Rossi", "fatturato": 100.5}], ordered=True)


@pytest.mark.parametrize("name", ENGINES)
def test_chain_join_then_aggregate_then_pivot(storage, ordini, clienti, name):
    ops = [
        _join("left", clienti),
        {"type": "fill_null", "params": {"columns": {"regione": "n/d"}}},
        {"type": "group_by", "params": {"by": ["regione", "canale"], "aggregations": [_agg("importo", "sum", "tot")]}},
        {"type": "pivot", "params": {"index": ["regione"], "on": "canale", "values": "tot", "func": "sum"}},
    ]
    res = check(name, storage, ordini, ops, [
        {"regione": "Lazio", "Dettaglio": 100.5, "GDO": 100.5, "HoReCa": None, "null": None},
        {"regione": "Lombardia", "Dettaglio": None, "GDO": None, "HoReCa": 200.1, "null": None},
        {"regione": "Irlanda", "Dettaglio": None, "GDO": None, "HoReCa": None, "null": None},  # importo NULL → NULL, non 0
        {"regione": "n/d", "Dettaglio": None, "GDO": -29.8, "HoReCa": None, "null": 50.25},
    ], columns=["regione", "Dettaglio", "GDO", "HoReCa", "null"])


@pytest.mark.parametrize("name", ENGINES)
def test_chain_empty_intermediate_result_keeps_schema(storage, ordini, clienti, name):
    ops = [
        filt("id", "eq", 999),
        _join("left", clienti),
        {"type": "compute", "params": {"columns": [{"name": "x", "expr": "importo * 2"}]}},
        {"type": "sort", "params": {"by": "x", "descending": True}},
        {"type": "limit", "params": {"n": 5}},
    ]
    res = run(name, storage, ordini, ops)
    assert res.row_count == 0 and [c.name for c in res.columns] == COLS + ["regione", "canale_right", "x"]


# ══════════════════════════════════════════════════════════════════════════════
# Run (scrittura parquet): il file prodotto ha gli stessi valori e tipi della preview
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name", ENGINES)
def test_run_output_matches_preview_and_keeps_types(storage, ordini, clienti, name):
    from app.engine.base import DataSource

    ops = [
        _join("left", clienti),
        {"type": "compute", "params": {"columns": [{"name": "valore", "expr": "prezzo * qta"}]}},
        filt("id", "ne", 3),
    ]
    eng = _engine(name, storage)
    dest = DataSource(bucket="data-prep", key=f"out/corr_{name}.parquet")
    result = eng.run(ordini, ops, dest, use_cache=False)
    assert result.rows_written == 7
    import os as _os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".parquet")
    _os.close(fd)
    try:
        storage.download_file("data-prep", dest.key, path)
        out = pl.read_parquet(path)
    finally:
        _os.unlink(path)
    assert out.height == 7
    assert out.columns == COLS + ["regione", "canale_right", "valore"]
    assert out.schema["data"] == pl.Date and out.schema["attivo"] == pl.Boolean
    assert isinstance(out.schema["ts"], pl.Datetime) and out.schema["ts"].time_zone is None  # naive
    assert out.schema["id"] in (pl.Int64, pl.Int32) and out.schema["big"] == pl.Int64
    assert out.schema["importo"] == pl.Float64 and out.schema["cliente"] == pl.String
    prev = _rows(eng.preview(ordini, ops, limit=100, use_cache=False))
    got = [{c: _norm(v) for c, v in r.items()} for r in out.to_dicts()]
    assert sorted(got, key=_key) == sorted(prev, key=_key)
    by = {r["id"]: r for r in got}
    assert by[1]["big"] == BIG and by[1]["regione"] == "Lazio" and by[8]["regione"] is None and by[4]["note"] == "  spazi  "
