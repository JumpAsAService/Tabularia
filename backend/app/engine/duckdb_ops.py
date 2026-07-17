"""
Registry delle operazioni per l'engine DuckDB (parallelo a `operations.py` di
Polars). Ogni operazione mappa una relazione DuckDB (LAZY: è un piano, non dati
materializzati) in un'altra, generando SQL con `rel.query(alias, "SELECT … FROM alias")`.

`alias` è un nome di tabella UNICO per step (passato dall'engine): riusare lo
stesso nome in `.query()` concatenate manda DuckDB in ricorsione infinita
("recursively bind view"), quindi ogni operazione riceve il proprio.

v1: SOLO operazioni single-input strutturali (i cui parametri sono nomi di
colonna / operatori / valori, MAI SQL libero) → nessun rischio di lettura file.
Le altre (compute, sql, join, union, pivot, unpivot, foreach) non sono ancora
supportate: `get_duck_operation` solleva un errore chiaro (usa Polars).
"""
from __future__ import annotations

from typing import Any, Callable

import duckdb

from app.engine.exceptions import EngineError

DuckOpFn = Callable[["duckdb.DuckDBPyRelation", dict[str, Any], str], "duckdb.DuckDBPyRelation"]

_REGISTRY: dict[str, DuckOpFn] = {}


def _register(name: str) -> Callable[[DuckOpFn], DuckOpFn]:
    def deco(fn: DuckOpFn) -> DuckOpFn:
        _REGISTRY[name] = fn
        return fn

    return deco


def get_duck_operation(name: str) -> DuckOpFn:
    fn = _REGISTRY.get(name)
    if fn is None:
        raise EngineError(
            f"operazione '{name}' non ancora supportata dall'engine DuckDB. "
            "Usa il motore Polars per questo flusso (o questo nodo)."
        )
    return fn


# ── helper SQL ────────────────────────────────────────────────────────────────
def _qi(name: str) -> str:
    """Quota un identificatore (nome colonna) per SQL."""
    return '"' + str(name).replace('"', '""') + '"'


def _lit(v: Any) -> str:
    """Letterale SQL da un valore Python (già parsato dal frontend)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _require(params: dict, key: str) -> Any:
    if key not in params or params[key] in (None, "", []):
        raise EngineError(f"parametro mancante: '{key}'")
    return params[key]


# mappa i tipi dell'UI → tipi DuckDB (per il cast)
_DUCK_DTYPES = {
    "int": "BIGINT",
    "float": "DOUBLE",
    "str": "VARCHAR",
    "bool": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
}


# ── colonne ───────────────────────────────────────────────────────────────────
@_register("select")
def op_select(rel, params, a):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return rel.query(a, f"SELECT {cols} FROM {a}")


@_register("drop")
def op_drop(rel, params, a):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return rel.query(a, f"SELECT * EXCLUDE ({cols}) FROM {a}")


@_register("rename")
def op_rename(rel, params, a):
    mapping = _require(params, "mapping")
    pairs = ", ".join(f"{_qi(old)} AS {_qi(new)}" for old, new in mapping.items())
    return rel.query(a, f"SELECT * RENAME ({pairs}) FROM {a}")


@_register("cast")
def op_cast(rel, params, a):
    cols = _require(params, "columns")  # {colonna: dtype-ui}
    repls = []
    for col, dt in cols.items():
        duck_t = _DUCK_DTYPES.get(str(dt))
        if not duck_t:
            raise EngineError(f"cast: tipo non supportato '{dt}'")
        repls.append(f"TRY_CAST({_qi(col)} AS {duck_t}) AS {_qi(col)}")
    return rel.query(a, f"SELECT * REPLACE ({', '.join(repls)}) FROM {a}")


# ── filtri ────────────────────────────────────────────────────────────────────
def _predicate(column: str, operator: str, value: Any) -> str:
    c = _qi(column)
    if operator == "is_null":
        return f"{c} IS NULL"
    if operator == "is_not_null":
        return f"{c} IS NOT NULL"
    if operator in ("in", "not_in"):
        vals = value if isinstance(value, list) else [value]
        joined = ", ".join(_lit(v) for v in vals)
        return f"{c} {'NOT IN' if operator == 'not_in' else 'IN'} ({joined})"
    if operator == "between":
        lo, hi = (list(value) + [None, None])[:2] if isinstance(value, list) else (value, value)
        return f"{c} BETWEEN {_lit(lo)} AND {_lit(hi)}"
    if operator == "contains":
        return f"contains(CAST({c} AS VARCHAR), {_lit(value)})"
    if operator == "starts_with":
        return f"starts_with(CAST({c} AS VARCHAR), {_lit(value)})"
    if operator == "ends_with":
        return f"ends_with(CAST({c} AS VARCHAR), {_lit(value)})"
    ops = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
    if operator not in ops:
        raise EngineError(f"filter: operatore non supportato '{operator}'")
    return f"{c} {ops[operator]} {_lit(value)}"


@_register("filter")
def op_filter(rel, params, a):
    column = _require(params, "column")
    operator = params.get("operator", "eq")
    value = params.get("value")
    return rel.query(a, f"SELECT * FROM {a} WHERE {_predicate(column, operator, value)}")


# ── riordino / righe ──────────────────────────────────────────────────────────
@_register("sort")
def op_sort(rel, params, a):
    by = _require(params, "by")
    cols = by if isinstance(by, list) else [by]
    direction = "DESC" if params.get("descending") else "ASC"
    order = ", ".join(f"{_qi(c)} {direction}" for c in cols)
    return rel.query(a, f"SELECT * FROM {a} ORDER BY {order}")


@_register("limit")
def op_limit(rel, params, a):
    n = int(_require(params, "n"))
    return rel.query(a, f"SELECT * FROM {a} LIMIT {n}")


@_register("unique")
def op_unique(rel, params, a):
    subset = params.get("subset")
    if subset:
        part = ", ".join(_qi(c) for c in subset)
        return rel.query(
            a, f"SELECT * FROM {a} QUALIFY row_number() OVER (PARTITION BY {part}) = 1"
        )
    return rel.query(a, f"SELECT DISTINCT * FROM {a}")


@_register("fill_null")
def op_fill_null(rel, params, a):
    cols = _require(params, "columns")  # {colonna: valore}
    repls = ", ".join(f"COALESCE({_qi(c)}, {_lit(v)}) AS {_qi(c)}" for c, v in cols.items())
    return rel.query(a, f"SELECT * REPLACE ({repls}) FROM {a}")


@_register("drop_nulls")
def op_drop_nulls(rel, params, a):
    subset = params.get("subset") or list(rel.columns)
    cond = " AND ".join(f"{_qi(c)} IS NOT NULL" for c in subset)
    return rel.query(a, f"SELECT * FROM {a} WHERE {cond}")


# ── aggregazione ──────────────────────────────────────────────────────────────
_AGG = {
    "sum": "sum", "mean": "avg", "min": "min", "max": "max", "count": "count",
    "median": "median", "std": "stddev", "var": "variance",
    "first": "first", "last": "last", "n_unique": "count",
}


@_register("group_by")
def op_group_by(rel, params, a):
    by = _require(params, "by")
    by = by if isinstance(by, list) else [by]
    aggs = _require(params, "aggregations")
    by_sql = ", ".join(_qi(c) for c in by)
    parts = [by_sql]
    for agg in aggs:
        col, func = agg["column"], agg.get("func", "sum")
        if func not in _AGG:
            raise EngineError(f"group_by: funzione non supportata '{func}'")
        alias = agg.get("alias") or f"{col}_{func}"
        expr = (
            f"count(DISTINCT {_qi(col)})" if func == "n_unique" else f"{_AGG[func]}({_qi(col)})"
        )
        parts.append(f"{expr} AS {_qi(alias)}")
    return rel.query(a, f"SELECT {', '.join(parts)} FROM {a} GROUP BY {by_sql}")
