"""
Registry delle operazioni per l'engine chDB (ClickHouse embedded), parallelo a
`duckdb_ops.py`. A differenza di DuckDB (oggetti relation lazy) chDB è PURO SQL:
ogni operazione riceve la SELECT dello step precedente e ne restituisce una nuova
che la avvolge in subquery — `SELECT … FROM (prev)`. La catena resta un unico
piano che ClickHouse ottimizza ed esegue in streaming (spill su disco).

Firma: `(sql, params, ctx)`. `ctx` serve per l'introspezione delle colonne
(`ctx.columns_of`) e per le operazioni MULTI-INPUT (join/union) che leggono il
lato destro (`ctx.build_right`).

Sicurezza: le operazioni strutturali usano solo nomi colonna / operatori /
valori (niente SQL libero). `compute` accetta espressioni scalari ma VIETA
subquery e table function di lettura (file/url/s3/…) → niente filesystem/rete.
`sql` (query intera) e `foreach` non sono ancora supportate su chDB.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from app.engine.context import MAX_CROSS_JOIN_ROWS
from app.engine.exceptions import EngineError
from app.engine.operations import MAX_PIVOT_COLUMNS

ChdbOpFn = Callable[..., str]

_REGISTRY: dict[str, ChdbOpFn] = {}


def _register(name: str) -> Callable[[ChdbOpFn], ChdbOpFn]:
    def deco(fn: ChdbOpFn) -> ChdbOpFn:
        _REGISTRY[name] = fn
        return fn

    return deco


def get_chdb_operation(name: str) -> ChdbOpFn:
    fn = _REGISTRY.get(name)
    if fn is None:
        raise EngineError(
            f"operazione '{name}' non ancora supportata dall'engine chDB. "
            "Usa il motore Polars o DuckDB per questo flusso (o questo nodo)."
        )
    return fn


# ── helper SQL (dialetto ClickHouse) ──────────────────────────────────────────
def _qi(name: str) -> str:
    """Quota un identificatore ClickHouse con i backtick."""
    return "`" + str(name).replace("\\", "\\\\").replace("`", "\\`") + "`"


def _lit(v: Any) -> str:
    """Letterale SQL da un valore Python (già parsato dal frontend)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _require(params: dict, key: str) -> Any:
    if key not in params or params[key] in (None, "", []):
        raise EngineError(f"parametro mancante: '{key}'")
    return params[key]


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


def _sub(sql: str) -> str:
    """Avvolge una SELECT come sorgente di subquery."""
    return f"({sql})"


# cast "try": funzioni *OrNull di ClickHouse (NULL invece di errore)
_CH_CAST = {
    "int": "toInt64OrNull(toString({c}))",
    "float": "toFloat64OrNull(toString({c}))",
    "str": "CAST({c} AS Nullable(String))",
    "bool": "toUInt8OrNull(toString({c}))",
    "date": "toDateOrNull(toString({c}))",
    "datetime": "parseDateTimeBestEffortOrNull(toString({c}))",
}


# ── colonne ───────────────────────────────────────────────────────────────────
@_register("select")
def op_select(sql, params, ctx):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return f"SELECT {cols} FROM {_sub(sql)}"


@_register("drop")
def op_drop(sql, params, ctx):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return f"SELECT * EXCEPT ({cols}) FROM {_sub(sql)}"


@_register("rename")
def op_rename(sql, params, ctx):
    mapping = _require(params, "mapping")
    # ClickHouse non ha `* RENAME`: ricostruiamo la lista mantenendo l'ordine
    cols = ctx.columns_of(sql)
    parts = [
        f"{_qi(c)} AS {_qi(mapping[c])}" if c in mapping else _qi(c) for c in cols
    ]
    return f"SELECT {', '.join(parts)} FROM {_sub(sql)}"


@_register("cast")
def op_cast(sql, params, ctx):
    cols = _require(params, "columns")
    repls = []
    for col, dt in cols.items():
        tmpl = _CH_CAST.get(str(dt))
        if not tmpl:
            raise EngineError(f"cast: tipo non supportato '{dt}'")
        repls.append(f"{tmpl.format(c=_qi(col))} AS {_qi(col)}")
    return f"SELECT * REPLACE ({', '.join(repls)}) FROM {_sub(sql)}"


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
        return f"position(toString({c}), {_lit(value)}) > 0"
    if operator == "starts_with":
        return f"startsWith(toString({c}), {_lit(value)})"
    if operator == "ends_with":
        return f"endsWith(toString({c}), {_lit(value)})"
    ops = {"eq": "=", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
    if operator not in ops:
        raise EngineError(f"filter: operatore non supportato '{operator}'")
    return f"{c} {ops[operator]} {_lit(value)}"


@_register("filter")
def op_filter(sql, params, ctx):
    column = _require(params, "column")
    operator = params.get("operator", "eq")
    pred = _predicate(column, operator, params.get("value"))
    return f"SELECT * FROM {_sub(sql)} WHERE {pred}"


# ── riordino / righe ──────────────────────────────────────────────────────────
@_register("sort")
def op_sort(sql, params, ctx):
    cols = _as_list(_require(params, "by"))
    direction = "DESC" if params.get("descending") else "ASC"
    order = ", ".join(f"{_qi(c)} {direction}" for c in cols)
    return f"SELECT * FROM {_sub(sql)} ORDER BY {order}"


@_register("limit")
def op_limit(sql, params, ctx):
    return f"SELECT * FROM {_sub(sql)} LIMIT {int(_require(params, 'n'))}"


@_register("unique")
def op_unique(sql, params, ctx):
    subset = params.get("subset")
    if subset:
        part = ", ".join(_qi(c) for c in subset)
        return f"SELECT * FROM {_sub(sql)} LIMIT 1 BY {part}"
    return f"SELECT DISTINCT * FROM {_sub(sql)}"


@_register("fill_null")
def op_fill_null(sql, params, ctx):
    cols = _require(params, "columns")
    repls = ", ".join(f"coalesce({_qi(c)}, {_lit(v)}) AS {_qi(c)}" for c, v in cols.items())
    return f"SELECT * REPLACE ({repls}) FROM {_sub(sql)}"


@_register("drop_nulls")
def op_drop_nulls(sql, params, ctx):
    subset = params.get("subset") or ctx.columns_of(sql)
    cond = " AND ".join(f"{_qi(c)} IS NOT NULL" for c in subset)
    return f"SELECT * FROM {_sub(sql)} WHERE {cond}"


# ── aggregazione ──────────────────────────────────────────────────────────────
_AGG = {
    "sum": "sum", "mean": "avg", "min": "min", "max": "max", "count": "count",
    "median": "median", "std": "stddevSamp", "var": "varSamp",
    "first": "any", "last": "anyLast", "n_unique": "uniqExact",
}


@_register("group_by")
def op_group_by(sql, params, ctx):
    by = _as_list(_require(params, "by"))
    aggs = _require(params, "aggregations")
    by_sql = ", ".join(_qi(c) for c in by)
    parts = [by_sql]
    for agg in aggs:
        col, func = agg["column"], agg.get("func", "sum")
        if func not in _AGG:
            raise EngineError(f"group_by: funzione non supportata '{func}'")
        alias = agg.get("alias") or f"{col}_{func}"
        expr = f"uniqExact({_qi(col)})" if func == "n_unique" else f"{_AGG[func]}({_qi(col)})"
        parts.append(f"{expr} AS {_qi(alias)}")
    return f"SELECT {', '.join(parts)} FROM {_sub(sql)} GROUP BY {by_sql}"


# ── compute (espressioni scalari, SENZA subquery né lettura file/rete) ─────────
_FORBIDDEN_IN_EXPR = re.compile(
    r"\b(?:select|from|with|insert|attach|create|"
    r"file|url|s3|hdfs|remote|remoteSecure|mysql|postgresql|jdbc|odbc|"
    r"clusterAllReplicas|cluster|dictionary|merge|numbers|zeros)\b",
    re.IGNORECASE,
)


@_register("compute")
def op_compute(sql, params, ctx):
    columns = _require(params, "columns")  # [{name, expr}]
    if not isinstance(columns, list):
        raise EngineError("compute: definisci almeno una colonna calcolata")
    existing = set(ctx.columns_of(sql))
    for c in columns:
        name = str(c.get("name") or "").strip()
        expr = str(c.get("expr") or "").strip()
        if not name or not expr:
            raise EngineError("compute: nome ed espressione sono obbligatori")
        if _FORBIDDEN_IN_EXPR.search(expr):
            raise EngineError(
                f"compute: espressione di '{name}' non consentita (niente subquery, "
                "FROM o table function di lettura — solo espressioni scalari)."
            )
        exclude = f" EXCEPT ({_qi(name)})" if name in existing else ""
        sql = f"SELECT *{exclude}, ({expr}) AS {_qi(name)} FROM {_sub(sql)}"
        existing.add(name)
    return sql


# ── join / union (leggono il lato destro dalla sorgente annidata) ─────────────
def _join_condition(params: dict) -> tuple[str, str]:
    """Torna (clausola SQL, kind) dove kind='using' o 'on'."""
    if "on" in params and params["on"]:
        keys = ", ".join(_qi(c) for c in _as_list(params["on"]))
        return f"USING ({keys})", "using"
    lo = _as_list(_require(params, "left_on"))
    ro = _as_list(_require(params, "right_on"))
    if len(lo) != len(ro):
        raise EngineError("join: left_on e right_on devono avere lo stesso numero di colonne")
    cond = " AND ".join(f"l.{_qi(l)} = r.{_qi(r)}" for l, r in zip(lo, ro))
    return f"ON {cond}", "on"


def _join_select(lcols, rcols, skip_right: set) -> str:
    """Colonne del risultato: tutte da sinistra + quelle di destra (chiavi USING
    escluse), con suffisso _right sulle omonime — come Polars/DuckDB."""
    lset = set(lcols)
    parts = ["l.*"]
    for c in rcols:
        if c in skip_right:
            continue
        parts.append(f"r.{_qi(c)} AS {_qi(c + '_right')}" if c in lset else f"r.{_qi(c)}")
    return ", ".join(parts)


_JOIN_KW = {"inner": "INNER JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL OUTER JOIN"}


@_register("join")
def op_join(sql, params, ctx):
    right = ctx.build_right(_require(params, "right"))
    how = params.get("how", "inner")
    lcols, rcols = ctx.columns_of(sql), ctx.columns_of(right)
    L, R = _sub(sql), _sub(right)

    if how == "cross":
        ln = ctx.scalar(f"SELECT count() FROM {L}")
        rn = ctx.scalar(f"SELECT count() FROM {R}")
        if MAX_CROSS_JOIN_ROWS and ln * rn > MAX_CROSS_JOIN_ROWS:
            raise EngineError(
                f"Il cross join produrrebbe {ln * rn:,} righe ({ln:,} × {rn:,}), "
                f"oltre il limite di {MAX_CROSS_JOIN_ROWS:,}. Aggiungi una condizione di join "
                "o riduci le sorgenti."
            )
        sel = _join_select(lcols, rcols, set())
        return f"SELECT {sel} FROM {L} AS l CROSS JOIN {R} AS r"

    clause, kind = _join_condition(params)
    if how in ("semi", "anti"):
        kw = "LEFT SEMI JOIN" if how == "semi" else "LEFT ANTI JOIN"
        return f"SELECT l.* FROM {L} AS l {kw} {R} AS r {clause}"
    if how not in _JOIN_KW:
        raise EngineError(f"join: tipo non supportato '{how}'")
    skip_right = set(_as_list(params["on"])) if kind == "using" else set()
    sel = _join_select(lcols, rcols, skip_right)
    return f"SELECT {sel} FROM {L} AS l {_JOIN_KW[how]} {R} AS r {clause}"


@_register("union")
def op_union(sql, params, ctx):
    right = ctx.build_right(_require(params, "right"))
    if params.get("strategy") == "strict":
        # schema identico: UNION ALL posizionale
        return f"SELECT * FROM {_sub(sql)} UNION ALL SELECT * FROM {_sub(right)}"
    # relaxed: allinea per NOME (ClickHouse non ha UNION ALL BY NAME) → per ogni
    # colonna dell'unione, ciascun lato la seleziona o NULL se assente
    lcols, rcols = ctx.columns_of(sql), ctx.columns_of(right)
    allcols = lcols + [c for c in rcols if c not in set(lcols)]
    lset, rset = set(lcols), set(rcols)
    lsel = ", ".join((_qi(c) if c in lset else "NULL") + f" AS {_qi(c)}" for c in allcols)
    rsel = ", ".join((_qi(c) if c in rset else "NULL") + f" AS {_qi(c)}" for c in allcols)
    return f"SELECT {lsel} FROM {_sub(sql)} UNION ALL SELECT {rsel} FROM {_sub(right)}"


# ── pivot / unpivot (righe↔colonne) ───────────────────────────────────────────
_AGG_IF = {"sum": "sumIf", "mean": "avgIf", "min": "minIf", "max": "maxIf", "count": "countIf"}


@_register("pivot")
def op_pivot(sql, params, ctx):
    index = _as_list(_require(params, "index"))
    on = _as_list(_require(params, "on"))  # più colonne = combinazioni
    values = _require(params, "values")
    func = params.get("func", "sum")
    agg_if = _AGG_IF.get(func)
    if not agg_if:
        raise EngineError(f"pivot: funzione non supportata su chDB '{func}' (usa sum/mean/min/max/count)")
    base = _sub(sql)
    on_sql = ", ".join(_qi(c) for c in on)
    n_cols = ctx.scalar(f"SELECT count() FROM (SELECT DISTINCT {on_sql} FROM {base})")
    if n_cols > MAX_PIVOT_COLUMNS:
        raise EngineError(
            f"pivot: le colonne scelte hanno {n_cols} combinazioni distinte ({n_cols} colonne "
            f"nuove, massimo {MAX_PIVOT_COLUMNS}). Sono le colonne giuste?"
        )
    combos = ctx.distinct_rows(base, on)  # ClickHouse non ha PIVOT: colonne condizionali
    idx_sql = ", ".join(_qi(c) for c in index)
    cols = [idx_sql] if idx_sql else []
    for combo in combos:
        cond = " AND ".join(f"{_qi(c)} = {_lit(v)}" for c, v in zip(on, combo))
        colname = " / ".join("" if v is None else str(v) for v in combo)
        cols.append(f"{agg_if}({_qi(values)}, {cond}) AS {_qi(colname)}")
    return f"SELECT {', '.join(cols)} FROM {base} GROUP BY {idx_sql}"


@_register("unpivot")
def op_unpivot(sql, params, ctx):
    on = params.get("on")
    index = params.get("index") or []
    var = params.get("variable_name") or "variable"
    val = params.get("value_name") or "value"
    allcols = ctx.columns_of(sql)
    melt = on if on else [c for c in allcols if c not in set(index)]
    if not melt:
        raise EngineError("unpivot: nessuna colonna da sciogliere")
    keep = [c for c in allcols if c in set(index)] if index else \
        [c for c in allcols if c not in set(melt)]
    keep_sql = "".join(f"{_qi(c)}, " for c in keep)
    # arrayJoin su tuple (nome, valore-as-String): una riga per colonna sciolta
    tuples = ", ".join(f"({_lit(c)}, toString({_qi(c)}))" for c in melt)
    base = _sub(sql)
    return (
        f"SELECT {keep_sql}tup.1 AS {_qi(var)}, tup.2 AS {_qi(val)} "
        f"FROM {base} ARRAY JOIN [{tuples}] AS tup"
    )
