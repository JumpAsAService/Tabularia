"""
Registry delle operazioni per l'engine DuckDB (parallelo a `operations.py` di
Polars). Ogni operazione mappa una relazione DuckDB (LAZY: è un piano, non dati
materializzati) in un'altra, generando SQL con `rel.query(alias, "SELECT … FROM alias")`.

Firma: `(rel, params, alias, ctx)`. `alias` è un nome di tabella UNICO per step
(riusarlo in `.query()` concatenate manda DuckDB in ricorsione infinita). `ctx`
serve alle operazioni MULTI-INPUT (join/union) per costruire il lato destro
(scan della sorgente annidata + sotto-catena).

Sicurezza: le operazioni strutturali usano solo nomi di colonna / operatori /
valori (nessun SQL libero). `compute` accetta espressioni SQL scalari ma VIETA
sottoquery e funzioni di lettura file → niente accesso al filesystem. `sql`
(query intera) e `foreach` (control-flow) non sono ancora supportate.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import duckdb

from app.engine.context import MAX_CROSS_JOIN_ROWS
from app.engine.exceptions import EngineError
from app.engine.operations import MAX_PIVOT_COLUMNS

DuckOpFn = Callable[..., "duckdb.DuckDBPyRelation"]

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


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


_DUCK_DTYPES = {
    "int": "BIGINT", "float": "DOUBLE", "str": "VARCHAR",
    "bool": "BOOLEAN", "date": "DATE", "datetime": "TIMESTAMP",
}


# ── colonne ───────────────────────────────────────────────────────────────────
@_register("select")
def op_select(rel, params, a, ctx):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return rel.query(a, f"SELECT {cols} FROM {a}")


@_register("drop")
def op_drop(rel, params, a, ctx):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return rel.query(a, f"SELECT * EXCLUDE ({cols}) FROM {a}")


@_register("rename")
def op_rename(rel, params, a, ctx):
    mapping = _require(params, "mapping")
    pairs = ", ".join(f"{_qi(old)} AS {_qi(new)}" for old, new in mapping.items())
    return rel.query(a, f"SELECT * RENAME ({pairs}) FROM {a}")


@_register("cast")
def op_cast(rel, params, a, ctx):
    cols = _require(params, "columns")
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
        joined = ", ".join(_lit(v) for v in _as_list(value))
        return f"{c} {'NOT IN' if operator == 'not_in' else 'IN'} ({joined})"
    if operator == "between":
        lo, hi = (_as_list(value) + [None, None])[:2]
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
def op_filter(rel, params, a, ctx):
    column = _require(params, "column")
    operator = params.get("operator", "eq")
    return rel.query(a, f"SELECT * FROM {a} WHERE {_predicate(column, operator, params.get('value'))}")


# ── riordino / righe ──────────────────────────────────────────────────────────
@_register("sort")
def op_sort(rel, params, a, ctx):
    cols = _as_list(_require(params, "by"))
    direction = "DESC" if params.get("descending") else "ASC"
    order = ", ".join(f"{_qi(c)} {direction}" for c in cols)
    return rel.query(a, f"SELECT * FROM {a} ORDER BY {order}")


@_register("limit")
def op_limit(rel, params, a, ctx):
    return rel.query(a, f"SELECT * FROM {a} LIMIT {int(_require(params, 'n'))}")


@_register("unique")
def op_unique(rel, params, a, ctx):
    subset = params.get("subset")
    if subset:
        part = ", ".join(_qi(c) for c in subset)
        return rel.query(a, f"SELECT * FROM {a} QUALIFY row_number() OVER (PARTITION BY {part}) = 1")
    return rel.query(a, f"SELECT DISTINCT * FROM {a}")


@_register("fill_null")
def op_fill_null(rel, params, a, ctx):
    cols = _require(params, "columns")
    repls = ", ".join(f"COALESCE({_qi(c)}, {_lit(v)}) AS {_qi(c)}" for c, v in cols.items())
    return rel.query(a, f"SELECT * REPLACE ({repls}) FROM {a}")


@_register("drop_nulls")
def op_drop_nulls(rel, params, a, ctx):
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
def op_group_by(rel, params, a, ctx):
    by = _as_list(_require(params, "by"))
    aggs = _require(params, "aggregations")
    by_sql = ", ".join(_qi(c) for c in by)
    parts = [by_sql]
    for agg in aggs:
        col, func = agg["column"], agg.get("func", "sum")
        if func not in _AGG:
            raise EngineError(f"group_by: funzione non supportata '{func}'")
        alias = agg.get("alias") or f"{col}_{func}"
        expr = f"count(DISTINCT {_qi(col)})" if func == "n_unique" else f"{_AGG[func]}({_qi(col)})"
        parts.append(f"{expr} AS {_qi(alias)}")
    return rel.query(a, f"SELECT {', '.join(parts)} FROM {a} GROUP BY {by_sql}")


# ── compute (espressioni SQL scalari, SENZA sottoquery né lettura file) ────────
_FORBIDDEN_IN_EXPR = re.compile(
    r"\b(?:select|from|attach|copy|install|load|pragma|"
    r"(?:read|scan)_(?:csv|parquet|ipc|ndjson|json)|read_(?:csv|json)_auto|parquet_scan)\b",
    re.IGNORECASE,
)


@_register("compute")
def op_compute(rel, params, a, ctx):
    columns = _require(params, "columns")  # [{name, expr}]
    if not isinstance(columns, list):
        raise EngineError("compute: definisci almeno una colonna calcolata")
    for i, c in enumerate(columns):
        name = str(c.get("name") or "").strip()
        expr = str(c.get("expr") or "").strip()
        if not name or not expr:
            raise EngineError("compute: nome ed espressione sono obbligatori")
        if _FORBIDDEN_IN_EXPR.search(expr):
            raise EngineError(
                f"compute: espressione di '{name}' non consentita (niente sottoquery, "
                "FROM o funzioni di lettura file — solo espressioni scalari)."
            )
        step = f"{a}_{i}"
        exclude = f" EXCLUDE ({_qi(name)})" if name in rel.columns else ""
        rel = rel.query(step, f"SELECT *{exclude}, ({expr}) AS {_qi(name)} FROM {step}")
    return rel


# ── join / union (leggono il lato destro dalla sorgente annidata) ─────────────
def _join_condition(ln: str, rn: str, params: dict) -> tuple[str, str]:
    """Torna (clausola SQL, kind) dove kind='using' o 'on'."""
    if "on" in params and params["on"]:
        keys = ", ".join(_qi(c) for c in _as_list(params["on"]))
        return f"USING ({keys})", "using"
    lo = _as_list(_require(params, "left_on"))
    ro = _as_list(_require(params, "right_on"))
    if len(lo) != len(ro):
        raise EngineError("join: left_on e right_on devono avere lo stesso numero di colonne")
    cond = " AND ".join(f"{ln}.{_qi(l)} = {rn}.{_qi(r)}" for l, r in zip(lo, ro))
    return f"ON {cond}", "on"


def _join_select(ln: str, lcols, rn: str, rcols, skip_right: set) -> str:
    """Colonne del risultato: tutte da sinistra + quelle di destra (chiavi USING
    escluse), con suffisso _right sulle omonime — come Polars."""
    lset = set(lcols)
    parts = [f"{ln}.*"]
    for c in rcols:
        if c in skip_right:
            continue
        parts.append(f"{rn}.{_qi(c)} AS {_qi(c + '_right')}" if c in lset else f"{rn}.{_qi(c)}")
    return ", ".join(parts)


_JOIN_KW = {"inner": "INNER JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL OUTER JOIN"}


@_register("join")
def op_join(rel, params, a, ctx):
    right = ctx.build_right(_require(params, "right"))
    how = params.get("how", "inner")
    ln, rn = ctx.register(rel), ctx.register(right)
    lcols, rcols = list(rel.columns), list(right.columns)

    if how == "cross":
        ln_n = ctx.con.sql(f"SELECT count(*) FROM {ln}").fetchone()[0]
        rn_n = ctx.con.sql(f"SELECT count(*) FROM {rn}").fetchone()[0]
        if MAX_CROSS_JOIN_ROWS and ln_n * rn_n > MAX_CROSS_JOIN_ROWS:
            raise EngineError(
                f"Il cross join produrrebbe {ln_n * rn_n:,} righe ({ln_n:,} × {rn_n:,}), "
                f"oltre il limite di {MAX_CROSS_JOIN_ROWS:,}. Aggiungi una condizione di join "
                "o riduci le sorgenti."
            )
        sel = _join_select(ln, lcols, rn, rcols, set())
        return ctx.con.sql(f"SELECT {sel} FROM {ln} CROSS JOIN {rn}")

    clause, kind = _join_condition(ln, rn, params)
    if how in ("semi", "anti"):
        return ctx.con.sql(f"SELECT {ln}.* FROM {ln} {how.upper()} JOIN {rn} {clause}")
    if how not in _JOIN_KW:
        raise EngineError(f"join: tipo non supportato '{how}'")
    # con USING le chiavi (stesse su entrambi) restano una sola volta
    skip_right = set(_as_list(params["on"])) if kind == "using" else set()
    sel = _join_select(ln, lcols, rn, rcols, skip_right)
    return ctx.con.sql(f"SELECT {sel} FROM {ln} {_JOIN_KW[how]} {rn} {clause}")


@_register("union")
def op_union(rel, params, a, ctx):
    right = ctx.build_right(_require(params, "right"))
    ln, rn = ctx.register(rel), ctx.register(right)
    # relaxed = allinea per nome (colonne mancanti → null); strict = schema identico
    op = "UNION ALL" if params.get("strategy") == "strict" else "UNION ALL BY NAME"
    return ctx.con.sql(f"SELECT * FROM {ln} {op} SELECT * FROM {rn}")


# ── pivot / unpivot (righe↔colonne) ───────────────────────────────────────────
@_register("pivot")
def op_pivot(rel, params, a, ctx):
    index = _as_list(_require(params, "index"))
    on = _require(params, "on")
    values = _require(params, "values")
    func = params.get("func", "sum")
    if func not in _AGG:
        raise EngineError(f"pivot: funzione non supportata '{func}'")
    name = ctx.register(rel)
    n_cols = ctx.con.sql(f"SELECT count(DISTINCT {_qi(on)}) FROM {name}").fetchone()[0]
    if n_cols > MAX_PIVOT_COLUMNS:
        raise EngineError(
            f"pivot: '{on}' ha {n_cols} valori distinti ({n_cols} colonne nuove, "
            f"massimo {MAX_PIVOT_COLUMNS}). È la colonna giusta?"
        )
    idx_sql = ", ".join(_qi(c) for c in index)
    return ctx.con.sql(
        f"PIVOT {name} ON {_qi(on)} USING {_AGG[func]}({_qi(values)}) GROUP BY {idx_sql}"
    )


@_register("unpivot")
def op_unpivot(rel, params, a, ctx):
    on = params.get("on")
    index = params.get("index") or []
    var = params.get("variable_name") or "variable"
    val = params.get("value_name") or "value"
    melt = on if on else [c for c in rel.columns if c not in set(index)]
    if not melt:
        raise EngineError("unpivot: nessuna colonna da sciogliere")
    on_sql = ", ".join(_qi(c) for c in melt)
    name = ctx.register(rel)
    return ctx.con.sql(
        f"UNPIVOT (SELECT * FROM {name}) ON {on_sql} INTO NAME {_qi(var)} VALUE {_qi(val)}"
    )
