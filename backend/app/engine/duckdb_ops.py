"""
The registry of the DuckDB engine's operations (parallel to Polars' `operations.py`).
Every operation maps a DuckDB relation (LAZY: a plan, not materialized data) into
another, generating SQL with `rel.query(alias, "SELECT … FROM alias")`.

Signature: `(rel, params, alias, ctx)`. `alias` is a table name UNIQUE per step (reusing
it in chained `.query()` sends DuckDB into endless recursion). `ctx` serves the
MULTI-INPUT operations (join/union) to build the right side (a scan of the nested source
+ a sub-chain).

Security: the structural operations use only column names / operators / values (no free
SQL). `compute` takes scalar SQL expressions but FORBIDS subqueries and file-reading
functions → no access to the filesystem. `sql` (a whole query) and `foreach` (control
flow) are not supported yet.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from typing import Any, Callable

import duckdb
import pyarrow.parquet as _pq

from app.engine import duckdb_budget
from app.engine.context import MAX_CROSS_JOIN_ROWS
from app.engine.expressions import forbidden_expression
from app.engine.exceptions import EngineError
from app.engine.operations import (
    MAX_FOREACH_ITERATIONS,
    MAX_FOREACH_TOTAL,
    MAX_PIVOT_COLUMNS,
    _reject_dynamic_source_keys,
    check_filter_value,
    check_strict_union,
    non_iso_values,
    temporal_value_error,
    _substitute,
    PIVOT_LABEL_SEP,
    PIVOT_VALUES_COLUMN,
    SAMPLE_BUCKETS,
    check_pivot_labels,
    pivot_measure_label,
    pivot_params,
    sample_threshold,
    sort_keys,
)
from app.engine.sql_guard import table_names
from app.engine.stats.aggregates import SqlStatSource
from app.engine.stats.catalog import stat_operations
from app.engine.stats.compute import run_hypothesis_test
from app.engine.stats.output import to_arrow

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
            f"operation '{name}' is not supported by the DuckDB engine yet. "
            "Use the Polars engine for this flow (or this node)."
        )
    return fn


# ── helper SQL ────────────────────────────────────────────────────────────────
def _qi(name: str) -> str:
    """Quotes an identifier (a column name) for SQL."""
    return '"' + str(name).replace('"', '""') + '"'


def _lit(v: Any) -> str:
    """A SQL literal from a Python value (parsed by the frontend already)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _require(params: dict, key: str) -> Any:
    if key not in params or params[key] in (None, "", []):
        raise EngineError(f"required parameter missing: '{key}'")
    return params[key]


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


_DUCK_DTYPES = {
    "int": "BIGINT", "float": "DOUBLE", "str": "VARCHAR",
    "bool": "BOOLEAN", "date": "DATE", "datetime": "TIMESTAMP",
}


# ── columns ───────────────────────────────────────────────────────────────────
@_register("select")
def op_select(rel, params, a, ctx):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return rel.query(a, f"SELECT {cols} FROM {a}")


@_register("reorder")
def op_reorder(rel, params, a, ctx):
    # the listed columns first (in order), then all the others (EXCLUDE the listed ones)
    order = _require(params, "columns")
    q = ", ".join(_qi(c) for c in order)
    return rel.query(a, f"SELECT {q}, * EXCLUDE ({q}) FROM {a}")


@_register("drop")
def op_drop(rel, params, a, ctx):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return rel.query(a, f"SELECT * EXCLUDE ({cols}) FROM {a}")


@_register("rename")
def op_rename(rel, params, a, ctx):
    mapping = _require(params, "mapping")
    pairs = ", ".join(f"{_qi(old)} AS {_qi(new)}" for old, new in mapping.items())
    return rel.query(a, f"SELECT * RENAME ({pairs}) FROM {a}")


def _cast_expr(q: str, src: str, duck_t: str) -> str:
    """The same STANDARD as Polars' cast (see operations.op_cast): failed → NULL; text with
    its spaces trimmed; text → integer only for an integer literal; number → integer by
    truncation."""
    src = src.upper()
    is_text = src.startswith(("VARCHAR", "STRING", "TEXT", "CHAR"))
    is_num = src in _DK_FLOATS or src.startswith("DECIMAL")
    if duck_t == "BIGINT" and is_text:
        return f"CASE WHEN regexp_matches(trim({q}), '^[+-]?[0-9]+$') THEN TRY_CAST(trim({q}) AS BIGINT) END"
    if duck_t == "BIGINT" and is_num:
        return f"TRY_CAST(trunc({q}) AS BIGINT)"
    if is_text:
        return f"TRY_CAST(trim({q}) AS {duck_t})"
    return f"TRY_CAST({q} AS {duck_t})"


@_register("cast")
def op_cast(rel, params, a, ctx):
    cols = _require(params, "columns")
    types = dict(zip(rel.columns, (str(t) for t in rel.types)))
    repls = []
    for col, dt in cols.items():
        duck_t = _DUCK_DTYPES.get(str(dt))
        if not duck_t:
            raise EngineError(f"cast: unsupported type '{dt}'")
        repls.append(f"{_cast_expr(_qi(col), types.get(col, ''), duck_t)} AS {_qi(col)}")
    return rel.query(a, f"SELECT * REPLACE ({', '.join(repls)}) FROM {a}")


# ── filters ────────────────────────────────────────────────────────────────────
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
        raise EngineError(f"filter: unsupported operator '{operator}'")
    return f"{c} {ops[operator]} {_lit(value)}"


@_register("filter")
def op_filter(rel, params, a, ctx):
    column = _require(params, "column")
    operator = params.get("operator", "eq")
    check_filter_value(operator, params.get("value"))
    bad = non_iso_values(operator, params.get("value"))
    if bad and dict(zip(rel.columns, (str(t).upper() for t in rel.types))).get(column, "").startswith(("DATE", "TIME")):
        raise temporal_value_error(bad[0])
    return rel.query(a, f"SELECT * FROM {a} WHERE {_predicate(column, operator, params.get('value'))}")


# ── ordering / rows ──────────────────────────────────────────────────────────
@_register("sort")
def op_sort(rel, params, a, ctx):
    # the keys and a direction each (operations.sort_keys: `ignore_missing` too)
    keys = sort_keys(params, rel.columns)
    if not keys:
        return rel
    # cross-engine standard: NULL always last (see operations.op_sort)
    order = ", ".join(f"{_qi(c)} {'DESC' if d else 'ASC'} NULLS LAST" for c, d in keys)
    return rel.query(a, f"SELECT * FROM {a} ORDER BY {order}")


@_register("limit")
def op_limit(rel, params, a, ctx):
    return rel.query(a, f"SELECT * FROM {a} LIMIT {int(_require(params, 'n'))}")


@_register("sample")
def op_sample(rel, params, a, ctx):
    # a reproducible random sample: bernoulli with a seed (one pass, streaming)
    threshold, seed = sample_threshold(params)
    pct = threshold / SAMPLE_BUCKETS * 100
    return rel.query(a, f"SELECT * FROM {a} USING SAMPLE {pct:.4f} PERCENT (bernoulli, {seed})")


# The rows' own order (that of the file, through filters and joins): `row_number() OVER ()`
# follows it (preserve_insertion_order); DuckDB's first()/last() and a window without an
# ORDER BY took any row of the group, Polars the first one.
_RN = "__tabularia_rn"


def _numbered(a: str) -> str:
    return f"(SELECT *, row_number() OVER () AS {_RN} FROM {a})"


@_register("unique")
def op_unique(rel, params, a, ctx):
    subset = params.get("subset")
    if subset:
        part = ", ".join(_qi(c) for c in subset)
        # the first row of each group, in the data's order (as Polars keeps the first)
        return rel.query(a, f"SELECT * EXCLUDE ({_RN}) FROM {_numbered(a)} "
                            f"QUALIFY row_number() OVER (PARTITION BY {part} ORDER BY {_RN}) = 1")
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


def _plan_operators(node, out: list) -> list:
    out.append(str(node.get("name", "?")).strip())
    for child in node.get("children") or []:
        _plan_operators(child, out)
    return out


def _never_null(ctx, rel, columns) -> frozenset[str]:
    """The columns of `rel`, among `columns`, that DuckDB itself can PROVE hold no NULL.

    Asked with EXPLAIN, which reads no data: `SELECT 1 FROM rel WHERE column IS NULL`. When the
    optimizer can prove from what it knows (a parquet file's footer says how many NULLs each
    column has) that the WHOLE query gives no row, its plan is an empty result and nothing else.
    That, and only that, is the proof: an empty result somewhere inside a plan proves nothing.
    A union whose first table has no NULLs and whose second has them is planned as a union of
    an empty result and a scan, and so is a table appended to one that lacks the column; a left
    join whose right side is known to be empty has an empty result under it while its left
    column is full of NULLs (all checked on DuckDB 1.5: taking any empty result for the proof
    counted 200 where there were 166 values). Proved this way: a column of a file, also after
    filters and projections. Not proved, and so counted as before: after a join, a union, an
    aggregation, a computed column, a cast. Anything unexpected (an error, a plan written
    another way) is «not proved».

    It serves `count(column)`: without NULLs it is the number of rows, and the column needs
    not be read. On a billion rows counted by a 4 GB identifier, read from the object storage,
    that was the difference between an answer in half a minute and none in two."""
    wanted = [c for c in dict.fromkeys(columns) if c in rel.columns]
    if not wanted:
        return frozenset()
    proved = set()
    try:
        name = ctx.register(rel)
        for c in wanted:
            raw = ctx.con.sql(f"EXPLAIN (FORMAT JSON) SELECT 1 FROM {name} WHERE {_qi(c)} IS NULL").fetchall()
            tree = json.loads(raw[0][-1])
            operators = _plan_operators(tree[0] if isinstance(tree, list) else tree, [])
            if "EMPTY_RESULT" in operators and set(operators) <= {"EMPTY_RESULT", "PROJECTION"}:
                proved.add(c)
    except Exception:  # noqa: BLE001 — a doubt: the column is counted as before
        return frozenset(proved)
    return frozenset(proved)


def _agg_expr(func: str, col: str, never_null: frozenset = frozenset()) -> str:
    """The aggregate of a group_by or a pivot: n_unique is a DISTINCT count (the pivot
    used the plain count of _AGG, counting repeats); first and last follow the rows'
    order (the input numbered by `_numbered`); the count of a column proved without NULLs
    is the count of the rows (`_never_null`)."""
    if func == "count" and col in never_null:
        return "count(*)"
    if func == "n_unique":
        return f"count(DISTINCT {_qi(col)})"
    if func in ("first", "last"):
        return f"{func}({_qi(col)} ORDER BY {_RN})"
    return f"{_AGG[func]}({_qi(col)})"


def _source_for(a: str, funcs) -> str:
    """The input of an aggregation: numbered when first or last need the order."""
    return _numbered(a) if any(f in ("first", "last") for f in funcs) else a


@_register("group_by")
def op_group_by(rel, params, a, ctx):
    by = _as_list(_require(params, "by"))
    aggs = _require(params, "aggregations")
    by_sql = ", ".join(_qi(c) for c in by)
    parts = [by_sql]
    never_null = _never_null(ctx, rel, [g["column"] for g in aggs if g.get("func", "sum") == "count"])
    for agg in aggs:
        col, func = agg["column"], agg.get("func", "sum")
        if func not in _AGG:
            raise EngineError(f"group_by: unsupported aggregation function '{func}'")
        alias = agg.get("alias") or f"{col}_{func}"
        parts.append(f"{_agg_expr(func, col, never_null)} AS {_qi(alias)}")
    src = _source_for(a, [g.get("func", "sum") for g in aggs])
    return _recast_hugeint(ctx, rel.query(a, f"SELECT {', '.join(parts)} FROM {src} GROUP BY {by_sql}"))


def _recast_hugeint(ctx, rel):
    """A sum of integers → HUGEINT (int128, which Arrow/Polars read as DECIMAL and the preview
    as float: 2^53+1 would lose precision): back to BIGINT as in the other engines."""
    if not any(str(t).upper() == "HUGEINT" for t in rel.types):
        return rel
    name = ctx.register(rel)
    casts = ", ".join(
        f"CAST({_qi(c)} AS BIGINT) AS {_qi(c)}" if str(t).upper() == "HUGEINT" else _qi(c)
        for c, t in zip(rel.columns, rel.types)
    )
    return ctx.con.sql(f"SELECT {casts} FROM {name}")


# ── compute (scalar SQL expressions, WITHOUT subqueries or file reads) ───────
# the expression is parsed, not searched for words: EXTRACT(YEAR FROM d) is scalar, and
# one the parser cannot read is refused (see app/engine/expressions.py, whose list of
# functions covers read_*, parquet_scan, scan_*)
@_register("compute")
def op_compute(rel, params, a, ctx):
    columns = _require(params, "columns")  # [{name, expr}]
    if not isinstance(columns, list):
        raise EngineError("compute: define at least one calculated column")
    for i, c in enumerate(columns):
        name = str(c.get("name") or "").strip()
        expr = str(c.get("expr") or "").strip()
        if not name or not expr:
            raise EngineError("compute: name and expression are required")
        if forbidden_expression(expr, "duckdb"):
            raise EngineError(
                f"compute: the expression of '{name}' is not allowed, or cannot be read (no "
                "subqueries, FROM or file reading functions — scalar expressions only)."
            )
        step = f"{a}_{i}"
        if name in rel.columns:
            # an existing column: overwritten IN ITS PLACE (as Polars)
            rel = rel.query(step, f"SELECT * REPLACE (({expr}) AS {_qi(name)}) FROM {step}")
        else:
            rel = rel.query(step, f"SELECT *, ({expr}) AS {_qi(name)} FROM {step}")
    return rel


# ── join / union (they read the right side from the nested source) ─────────────
def _join_condition(ln: str, rn: str, params: dict) -> tuple[str, str]:
    """Returns (SQL clause, kind) where kind='using' or 'on'."""
    if "on" in params and params["on"]:
        keys = ", ".join(_qi(c) for c in _as_list(params["on"]))
        return f"USING ({keys})", "using"
    lo = _as_list(_require(params, "left_on"))
    ro = _as_list(_require(params, "right_on"))
    if len(lo) != len(ro):
        raise EngineError("join: left_on and right_on must have the same number of columns")
    cond = " AND ".join(f"{ln}.{_qi(l)} = {rn}.{_qi(r)}" for l, r in zip(lo, ro))
    return f"ON {cond}", "on"


def _join_select(ln: str, lcols, rn: str, rcols, skip_right: set, coalesce_keys: set = frozenset()) -> str:
    """The result's columns: all the left's + the right's (USING keys excluded), with the
    _right suffix on the same names — as Polars. In full/right joins the USING keys are
    COALESCE(left, right): filled in the rows that exist only on the right too."""
    lset = set(lcols)
    parts = [
        f"COALESCE({ln}.{_qi(c)}, {rn}.{_qi(c)}) AS {_qi(c)}" if c in coalesce_keys else f"{ln}.{_qi(c)}"
        for c in lcols
    ]
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
                f"The cross join would produce {ln_n * rn_n:,} rows ({ln_n:,} × {rn_n:,}), "
                f"over the limit of {MAX_CROSS_JOIN_ROWS:,}. Add a join condition "
                "or reduce the sources."
            )
        sel = _join_select(ln, lcols, rn, rcols, set())
        return ctx.con.sql(f"SELECT {sel} FROM {ln} CROSS JOIN {rn}")

    clause, kind = _join_condition(ln, rn, params)
    if how in ("semi", "anti"):
        return ctx.con.sql(f"SELECT {ln}.* FROM {ln} {how.upper()} JOIN {rn} {clause}")
    if how not in _JOIN_KW:
        raise EngineError(f"join: unsupported join type '{how}'")
    # with USING the keys (the same on both sides) appear once
    skip_right = set(_as_list(params["on"])) if kind == "using" else set()
    sel = _join_select(ln, lcols, rn, rcols, skip_right, skip_right if how in ("full", "right") else set())
    return ctx.con.sql(f"SELECT {sel} FROM {ln} {_JOIN_KW[how]} {rn} {clause}")


@_register("union")
def op_union(rel, params, a, ctx):
    right = ctx.build_right(_require(params, "right"))
    ln, rn = ctx.register(rel), ctx.register(right)
    # relaxed = aligned by name (missing columns → null); strict = the same columns and
    # types, in any order, joined by name (by position the columns were swapped silently)
    if params.get("strategy") == "strict":
        check_strict_union(list(zip(rel.columns, map(str, rel.types))), list(zip(right.columns, map(str, right.types))))
    return ctx.con.sql(f"SELECT * FROM {ln} UNION ALL BY NAME SELECT * FROM {rn}")


# ── pivot / unpivot (rows↔columns) ───────────────────────────────────────────
@_register("pivot")
def op_pivot(rel, params, a, ctx):
    index, on, values, func = pivot_params(params)  # several `on` columns = combinations
    if func not in _AGG:
        raise EngineError(f"pivot: unsupported aggregation function '{func}'")
    name = ctx.register(rel)
    out = _qi(PIVOT_VALUES_COLUMN)
    idx_sql = ", ".join(_qi(c) for c in index)
    measure_sql = _agg_expr(func, values, _never_null(ctx, rel, [values]) if func == "count" else frozenset())
    if not on:  # rows only: a group by with one measure, named `values`
        return _recast_hugeint(ctx, ctx.con.sql(
            f"SELECT {idx_sql}, {measure_sql} AS {out} FROM {_source_for(name, [func])} GROUP BY {idx_sql}"
        ))
    # the cross-engine label (see operations.pivot_label): the value as text,
    # NULL → 'null', several columns joined by "_" (only the combinations present)
    on_sql = f" || '{PIVOT_LABEL_SEP}' || ".join(f"coalesce(CAST({_qi(c)} AS VARCHAR), 'null')" for c in on)
    # The new columns are found ONCE, here, and given to PIVOT by name. Left to find them,
    # PIVOT ran the chain a second time for the same list before running it a third time to
    # fill the table. The list is in the order PIVOT itself would use (the labels' own).
    labels = [r[0] for r in ctx.con.sql(
        f"SELECT l FROM (SELECT DISTINCT {on_sql} AS l FROM {name} LIMIT {MAX_PIVOT_COLUMNS + 1}) ORDER BY l"
    ).fetchall()]
    n_cols = len(labels)
    if n_cols > MAX_PIVOT_COLUMNS:  # how many exactly is worth a count only to say it
        n_cols = ctx.con.sql(f"SELECT count(*) FROM (SELECT DISTINCT {on_sql} FROM {name})").fetchone()[0]
        raise EngineError(
            f"pivot: the chosen columns have {n_cols} distinct combinations ({n_cols} new "
            f"columns, maximum {MAX_PIVOT_COLUMNS}). Are these the right columns?"
        )
    in_sql = ", ".join(_lit(str(l)) for l in labels)
    if index:
        if not labels:  # no rows at all: the row fields alone, as PIVOT gave them
            return ctx.con.sql(f"SELECT DISTINCT {idx_sql} FROM {name} LIMIT 0")
        piv = ctx.con.sql(
            f"PIVOT {_source_for(name, [func])} ON {on_sql} IN ({in_sql}) USING {measure_sql} GROUP BY {idx_sql}"
        )
        # a sum of integers → HUGEINT (a decimal for Polars): back to BIGINT as the other engines
        return _recast_hugeint(ctx, piv)
    # columns only: ONE row, led by the measure's label (a constant group: without a
    # GROUP BY DuckDB's PIVOT would group by every other column)
    if ctx.con.sql(f"SELECT count(*) FROM {name} WHERE {on_sql} = {_lit(PIVOT_VALUES_COLUMN)}").fetchone()[0]:
        check_pivot_labels([PIVOT_VALUES_COLUMN])
    measure = _lit(pivot_measure_label(func, values))
    if not n_cols:  # no rows at all: the label alone
        return ctx.con.sql(f"SELECT {measure} AS {out}")
    grp = "__pivot_measure__"
    piv = ctx.register(ctx.con.sql(
        f"PIVOT (SELECT *, {measure} AS {grp} FROM {_source_for(name, [func])}) "
        f"ON {on_sql} IN ({in_sql}) USING {measure_sql} GROUP BY {grp}"
    ))
    return _recast_hugeint(ctx, ctx.con.sql(f"SELECT {grp} AS {out}, * EXCLUDE ({grp}) FROM {piv}"))


_DK_INTS = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}
_DK_FLOATS = {"FLOAT", "DOUBLE", "REAL"}


def unpivot_value_type(types: list[str]) -> str:
    """The unpivoted value's supertype (the same rule as Polars/ClickHouse)."""
    bases = [t.upper() for t in types]
    if all(b in _DK_INTS for b in bases):
        return "BIGINT"
    if all(b in _DK_INTS or b in _DK_FLOATS or b.startswith("DECIMAL") for b in bases):
        return "DOUBLE"
    if len(set(bases)) == 1:
        return bases[0]
    return "VARCHAR"


@_register("unpivot")
def op_unpivot(rel, params, a, ctx):
    on = params.get("on")
    index = params.get("index") or []
    var = params.get("variable_name") or "variable"
    val = params.get("value_name") or "value"
    melt = on if on else [c for c in rel.columns if c not in set(index)]
    if not melt:
        raise EngineError("unpivot: no columns to unpivot")
    types = dict(zip(rel.columns, (str(t) for t in rel.types)))
    missing = [c for c in melt if c not in types]
    if missing:
        raise EngineError(f"unpivot: columns not found: {', '.join(missing)}")
    # as Polars: ONLY the index columns stay; the unpivoted ones go to the common
    # supertype (UNPIVOT wants types that unify)
    vtype = unpivot_value_type([types[c] for c in melt])
    keep = [c for c in rel.columns if c in set(index)]
    sel = ", ".join([_qi(c) for c in keep] + [f"CAST({_qi(c)} AS {vtype}) AS {_qi(c)}" for c in melt])
    on_sql = ", ".join(_qi(c) for c in melt)
    name = ctx.register(rel)
    # the SQL-standard form: the only one that takes INCLUDE NULLS (the syntax
    # `UNPIVOT … ON … INTO` drops the rows with a NULL value, Polars/ClickHouse do not)
    return ctx.con.sql(
        f"SELECT * FROM (SELECT {sel} FROM {name}) "
        f"UNPIVOT INCLUDE NULLS ({_qi(val)} FOR {_qi(var)} IN ({on_sql}))"
    )


# ── Execute SQL (whole query in a SANDBOX) ────────────────────────────────────
_NAMES_INPUT = re.compile(r"\b(?:self|input)\b", re.IGNORECASE)

# The column types a parquet file gives back the same whether DuckDB or pyarrow wrote it
# (checked type by type on DuckDB 1.5: tests/test_duckdb_tuning.py does it again).
_WRITTEN_ALIKE = frozenset({
    "BOOLEAN", "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
    "FLOAT", "DOUBLE", "VARCHAR", "DATE", "TIME", "TIMESTAMP", "TIMESTAMP_MS", "TIMESTAMP_NS",
    "TIMESTAMP WITH TIME ZONE", "BLOB",
})
# …and the ones DuckDB writes another way, with the cast that gives what pyarrow gave: a sum of
# integers (HUGEINT) is a DECIMAL(38,0) through Arrow and would be a DOUBLE, rounded, from DuckDB
_WRITTEN_AS = {"HUGEINT": "DECIMAL(38,0)", "UUID": "VARCHAR"}


def native_write_projection(columns: list[str], types: list[str]) -> str | None:
    """The SELECT list with which DuckDB itself writes a result to parquet EXACTLY as the Python
    writer would, or None when it cannot be said (a type not checked, two columns whose names
    differ at most by case): then Python writes it, as before."""
    if len({c.lower() for c in columns}) != len(columns):
        return None  # `a` and `A` are one name to DuckDB: a list of names would write `a` twice
    parts = []
    for name, t in zip(columns, types):
        t = str(t).upper()
        if t in _WRITTEN_ALIKE or t.startswith("DECIMAL"):
            parts.append(_qi(name))
        elif t in _WRITTEN_AS:
            parts.append(f"CAST({_qi(name)} AS {_WRITTEN_AS[t]}) AS {_qi(name)}")
        else:
            return None
    return ", ".join(parts)


@_register("sql")
def op_sql(rel, params, a, ctx):
    """Runs a DuckDB SQL query on the incoming frame (table `self`, alias `input`), and on
    the `tables` the operation brings (a SQL script on the catalog), in an isolated
    SANDBOX connection.

    Security: the input and the tables are written to parquet files of this run, each read
    by a view; then every external access is switched off but for those files
    (`allowed_paths` + `enable_external_access=false` + `lock_configuration=true`):
    read_csv/read_parquet of anything else, attach, copy, install, httpfs are blocked and
    cannot be turned back on. The views read in place only the columns and row groups the
    query needs: copying every column into the sandbox's own tables made a GROUP BY over 60
    million rows take 45-81 s and some 3 GB (bug hunt 2026-11-03).

    The result is written by the sandbox's own DuckDB into ONE more file of this run, allowed
    like the inputs (a path only this run knows, and all it could hold is the query's own
    result): streaming, out of core, on every thread. It used to be handed to Python batch by
    batch and written by pyarrow on one thread: over a join of 42 million rows, 24.7 s of the
    step's 42, against 7.7 s. Python still writes what the two would not write alike
    (`native_write_projection`)."""
    query = str(_require(params, "query")).strip()
    if not query:
        raise EngineError("sql: the query is empty")

    # the input is copied in only when the query names it: a SQL script on the catalog
    # reads its datasources as `tables`, the first of them being the input too, and
    # copying it twice doubled the wait of a preview on a large datasource
    in_tmp = None
    if _NAMES_INPUT.search(query):
        in_tmp = ctx.tempfile()
        rel.write_parquet(in_tmp)  # materializes the input (streaming to disk)
    others = {}
    for name, ref in table_names(params).items():  # built as a join's right side, then copied in
        others[name] = ctx.tempfile()
        ctx.build_right(ref).write_parquet(others[name])

    # A database on disk with this process's budget and its own spill directory, set before
    # the lock: a query's joins and groups spill there.
    work_dir = tempfile.mkdtemp(prefix="duckdb-sql-")
    sbx = duckdb.connect(os.path.join(work_dir, "sandbox.duckdb"))
    try:
        sbx.execute(f"SET temp_directory = '{os.path.join(work_dir, 'spill')}'")
        duckdb_budget.apply(sbx)
        out_tmp = ctx.tempfile()
        files = ([in_tmp] if in_tmp is not None else []) + list(others.values()) + [out_tmp]
        # this run's own temporary files, named by ctx.tempfile: no quote inside
        sbx.execute("SET allowed_paths=[" + ", ".join(f"'{f}'" for f in files) + "]")
        if in_tmp is not None:
            sbx.execute(f"CREATE VIEW self AS SELECT * FROM read_parquet('{in_tmp}')")
            sbx.execute("CREATE VIEW input AS SELECT * FROM self")
        for name, path in others.items():  # names checked by table_names: plain lowercase
            sbx.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path}')")
        sbx.execute("SET enable_external_access=false")  # no other file, network, attach or install
        sbx.execute("SET lock_configuration=true")  # and the query cannot turn it back on
        try:
            result = sbx.sql(query)
            if ctx.limit_output:  # in a preview, last node: do not compute the whole result
                result = result.limit(ctx.limit_output)
            native = native_write_projection(list(result.columns), [str(t) for t in result.types])
            if native is not None:
                # onto the path itself: over a file that is there already DuckDB writes beside it
                # first and renames, and the name beside it is not one the sandbox may touch
                if os.path.exists(out_tmp):
                    os.remove(out_tmp)
                result.project(native).write_parquet(out_tmp)
            else:
                reader = result.to_arrow_reader(1_000_000)  # streaming Arrow
                writer = _pq.ParquetWriter(out_tmp, reader.schema)  # written by Python
                try:
                    for batch in reader:
                        writer.write_batch(batch)
                finally:
                    writer.close()
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(f"sql: the query is not valid or not allowed: {e}") from e
    finally:
        sbx.close()
        shutil.rmtree(work_dir, ignore_errors=True)
    return ctx.con.read_parquet(out_tmp)


# ── foreach (a loop with placeholders, SSIS container style) ────────────────────
@_register("foreach")
def op_foreach(rel, params, a, ctx):
    """Runs the body for every iteration and APPENDS the results (UNION ALL BY NAME). The
    iterations come from `items` (a static list of dicts) or from a `driver` (a
    sub-flow: every row = an iteration, the columns = the `{{key}}` placeholders). It
    reuses Polars' engine-agnostic substitution and safety."""
    body = _require(params, "body")
    items = params.get("items")
    if not items and "driver" in params:
        drv = ctx.build_right(params["driver"]).limit(MAX_FOREACH_ITERATIONS + 1).pl()
        if drv.height > MAX_FOREACH_ITERATIONS:
            raise EngineError(
                f"the driver has more than {MAX_FOREACH_ITERATIONS} rows: reduce the driver (unique/filter)."
            )
        items = drv.to_dicts()
    if not items:
        raise EngineError(
            "foreach has no iterations: connect a driver (the top input) or define 'items'"
        )
    if len(items) > MAX_FOREACH_ITERATIONS:
        raise EngineError(f"foreach: {len(items)} iterations, over the limit of {MAX_FOREACH_ITERATIONS}")
    if not body:
        raise EngineError("foreach has an empty body: drag some operations into the container")
    _reject_dynamic_source_keys(body)  # a source cannot take its path from the data

    ctx.foreach_iterations += len(items)
    if ctx.foreach_iterations > MAX_FOREACH_TOTAL:
        raise EngineError(
            f"foreach: too many iterations in total ({ctx.foreach_iterations}) — the nesting is too deep"
        )

    add_keys = params.get("add_keys_as_columns", False)
    parts = []
    for idx, item in enumerate(items):
        try:
            sub = ctx.apply(rel, _substitute(body, item))
        except EngineError as e:
            raise EngineError(f"iteration {idx + 1}/{len(items)}: {e}") from e
        if add_keys:
            lits = ", ".join(f"{_lit(v)} AS {_qi(str(k))}" for k, v in item.items())
            alias = ctx.uid("fk")
            sub = sub.query(alias, f"SELECT *, {lits} FROM {alias}")
        parts.append(sub)

    if len(parts) == 1:
        return parts[0]
    names = [ctx.register(p) for p in parts]  # UNION ALL BY NAME (like diagonal_relaxed)
    return ctx.con.sql(" UNION ALL BY NAME ".join(f"SELECT * FROM {n}" for n in names))


# ── statistics: one operation per hypothesis test (`stat_` + the test's id) ────
# The aggregates are queries DuckDB runs on the relation so far (at most a row per group
# comes back); the output is the small table of the results, read from Arrow. See
# app/engine/stats; the operations are those of its catalogue.
def _stat_operation(test: str) -> DuckOpFn:
    def op(rel, params, a, ctx):
        name = ctx.register(rel)
        source = SqlStatSource(
            "duckdb", lambda q: ctx.con.sql(q).fetchall(), _qi, _lit,
            f"SELECT * FROM {name}", list(zip(rel.columns, map(str, rel.types))),
        )
        return ctx.con.from_arrow(to_arrow(run_hypothesis_test(source, test, params, "duckdb")))

    return op


for _name, _test in stat_operations().items():
    _register(_name)(_stat_operation(_test))
