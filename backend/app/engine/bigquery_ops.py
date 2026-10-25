"""
The registry of the BigQuery engine's operations, parallel to `chdb_ops.py`: pure
SQL (GoogleSQL dialect), every operation receives the previous step's SELECT and wraps
it in a subquery. The chain is one query BigQuery plans and runs at once.

Signature: `(sql, params, ctx)`. `ctx` serves the column introspection
(`ctx.columns_of` / `ctx.schema_of`, which on BigQuery is a DRY RUN: free) and the
MULTI-INPUT operations (join/union) that read the right side (`ctx.build_right`).

The cross-engine standard (see operations.py and test_data_correctness.py): NULLs,
«try» casts (failed → NULL), joins with the _right suffix, pivots with sorted labels,
naive UTC datetimes (BigQuery: DATETIME). Where BigQuery has no exact equivalent it
is said next to the operation.

Column names: BigQuery allows only [A-Za-z0-9_] (and normalizes the parquet's by
itself: "Company Name" → Company_Name). The operations reason in the ORIGINAL names and
every identifier goes through `ctx.qi(name)`, which turns it into the safe name
registered in the context; the engine translates the results back.
`ctx.columns_of`/`schema_of` answer in the original names already.

Security: the structural operations use only column names / operators / values.
`compute` and `sql` take expressions but forbid subqueries, DDL/DML, EXTERNAL_QUERY,
ML/AI and script statements.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from app.engine.context import MAX_CROSS_JOIN_ROWS
from app.engine.expressions import forbidden_expression
from app.engine.exceptions import EngineError
from app.engine.operations import (
    MAX_PIVOT_COLUMNS, PIVOT_LABEL_SEP, SAMPLE_BUCKETS, check_filter_value, check_strict_union, non_iso_values, pivot_label, sample_threshold, sort_keys,
    temporal_value_error, TEXT_TRUE, TEXT_FALSE,
)
from app.engine.sql_guard import ensure_reads_only_input

BqOpFn = Callable[..., str]

_REGISTRY: dict[str, BqOpFn] = {}


def _register(name: str) -> Callable[[BqOpFn], BqOpFn]:
    def deco(fn: BqOpFn) -> BqOpFn:
        _REGISTRY[name] = fn
        return fn

    return deco


def get_bigquery_operation(name: str) -> BqOpFn:
    fn = _REGISTRY.get(name)
    if fn is None:
        raise EngineError(
            f"operation '{name}' is not supported by the BigQuery engine. "
            "Use the Polars or DuckDB engine for this flow (or this node)."
        )
    return fn


# ── SQL helpers (GoogleSQL dialect) ───────────────────────────────────────────
def _qi(name: str) -> str:
    """Quotes an identifier with backticks (GoogleSQL)."""
    return "`" + str(name).replace("\\", "\\\\").replace("`", "\\`") + "`"


def _lit(v: Any) -> str:
    """A SQL literal from a Python value (parsed by the frontend already). BigQuery does NOT
    convert implicitly: a BOOL compares only with TRUE/FALSE."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _require(params: dict, key: str) -> Any:
    if key not in params or params[key] in (None, "", []):
        raise EngineError(f"required parameter missing: '{key}'")
    return params[key]


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


def _sub(sql: str) -> str:
    return f"({sql})"


# BigQuery types (as a job's schema reports them) → family
_BQ_INTS = {"INTEGER", "INT64"}
_BQ_FLOATS = {"FLOAT", "FLOAT64"}
_BQ_DECIMALS = {"NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL"}
_BQ_TEXT = {"STRING"}
_CAST_TYPES = {"int", "float", "str", "bool", "date", "datetime"}


# ── columns ───────────────────────────────────────────────────────────────────
@_register("select")
def op_select(sql, params, ctx):
    cols = ", ".join(ctx.qi(c) for c in _require(params, "columns"))
    return f"SELECT {cols} FROM {_sub(sql)}"


@_register("reorder")
def op_reorder(sql, params, ctx):
    order = _require(params, "columns")
    cols = ", ".join(ctx.qi(c) for c in order)
    return f"SELECT {cols}, * EXCEPT ({cols}) FROM {_sub(sql)}"


@_register("drop")
def op_drop(sql, params, ctx):
    cols = ", ".join(ctx.qi(c) for c in _require(params, "columns"))
    return f"SELECT * EXCEPT ({cols}) FROM {_sub(sql)}"


@_register("rename")
def op_rename(sql, params, ctx):
    mapping = _require(params, "mapping")
    cols = ctx.columns_of(sql)
    parts = [f"{ctx.qi(c)} AS {ctx.qi(mapping[c])}" if c in mapping else ctx.qi(c) for c in cols]
    return f"SELECT {', '.join(parts)} FROM {_sub(sql)}"


def _cast_expr(c: str, base: str, dt: str) -> str:
    """The same STANDARD as Polars'/DuckDB's/ClickHouse's cast: failed → NULL (SAFE_CAST);
    text with its spaces trimmed; text → integer only for an integer literal; number →
    integer by truncation (CAST would round); float → text with ".0" on whole values
    (BigQuery would print 200.0 as "200")."""
    is_text = base in _BQ_TEXT
    is_float = base in _BQ_FLOATS or base in _BQ_DECIMALS
    if dt == "int":
        if is_text:
            return f"SAFE_CAST(TRIM({c}) AS INT64)"
        if is_float:
            return f"SAFE_CAST(TRUNC({c}) AS INT64)"
        return f"SAFE_CAST({c} AS INT64)"
    if dt == "float":
        if is_text:
            return f"SAFE_CAST(TRIM({c}) AS FLOAT64)"
        return f"SAFE_CAST({c} AS FLOAT64)"
    if dt == "str":
        if base in _BQ_FLOATS:
            return (f"IF({c} = TRUNC({c}) AND ABS({c}) < 1e15, "
                    f"CONCAT(CAST(CAST({c} AS INT64) AS STRING), '.0'), CAST({c} AS STRING))")
        return f"SAFE_CAST({c} AS STRING)"
    if dt == "date":
        if is_text:
            # SAFE_CAST accepts only YYYY-MM-DD and refuses impossible dates
            # ("2024-02-30" → NULL), as Polars/DuckDB
            return f"SAFE_CAST(TRIM({c}) AS DATE)"
        if base == "TIMESTAMP":
            return f"DATE({c}, 'UTC')"
        return f"SAFE_CAST({c} AS DATE)"
    if dt == "datetime":
        if is_text:
            # ISO with 'T' or a space; the rest → NULL (Polars/DuckDB: ISO only)
            return f"SAFE_CAST(TRIM({c}) AS DATETIME)"
        if base == "TIMESTAMP":
            return f"DATETIME({c}, 'UTC')"
        return f"SAFE_CAST({c} AS DATETIME)"
    if dt == "bool":
        if is_text:  # the words DuckDB takes, as the other engines (SAFE_CAST: true/false only)
            w = f"LOWER(TRIM({c}))"
            yes = ", ".join(f"'{x}'" for x in TEXT_TRUE)
            no = ", ".join(f"'{x}'" for x in TEXT_FALSE)
            return f"CASE WHEN {w} IN ({yes}) THEN TRUE WHEN {w} IN ({no}) THEN FALSE END"
        if base in _BQ_INTS or is_float:
            return f"SAFE_CAST(SAFE_CAST({c} AS INT64) AS BOOL)"
        return f"SAFE_CAST({c} AS BOOL)"
    raise EngineError(f"cast: unsupported type '{dt}'")


@_register("cast")
def op_cast(sql, params, ctx):
    cols = _require(params, "columns")
    schema = dict(ctx.schema_of(sql))
    repls = []
    for col, dt in cols.items():
        if str(dt) not in _CAST_TYPES:
            raise EngineError(f"cast: unsupported type '{dt}'")
        base = str(schema.get(col, "")).upper()
        repls.append(f"{_cast_expr(ctx.qi(col), base, str(dt))} AS {ctx.qi(col)}")
    return f"SELECT * REPLACE ({', '.join(repls)}) FROM {_sub(sql)}"


# ── filters ────────────────────────────────────────────────────────────────────
def _predicate(ctx, column: str, operator: str, value: Any) -> str:
    c = ctx.qi(column)
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
        return f"STRPOS(CAST({c} AS STRING), {_lit(value)}) > 0"
    if operator == "starts_with":
        return f"STARTS_WITH(CAST({c} AS STRING), {_lit(value)})"
    if operator == "ends_with":
        return f"ENDS_WITH(CAST({c} AS STRING), {_lit(value)})"
    ops = {"eq": "=", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
    if operator not in ops:
        raise EngineError(f"filter: unsupported operator '{operator}'")
    return f"{c} {ops[operator]} {_lit(value)}"


@_register("filter")
def op_filter(sql, params, ctx):
    column = _require(params, "column")
    operator = params.get("operator", "eq")
    check_filter_value(operator, params.get("value"))
    bad = non_iso_values(operator, params.get("value"))
    if bad and dict(ctx.schema_of(sql)).get(column, "").upper() in ("DATE", "DATETIME", "TIMESTAMP", "TIME"):
        raise temporal_value_error(bad[0])
    pred = _predicate(ctx, column, operator, params.get("value"))
    return f"SELECT * FROM {_sub(sql)} WHERE {pred}"


# ── ordering / rows ──────────────────────────────────────────────────────────
@_register("sort")
def op_sort(sql, params, ctx):
    # the keys and a direction each (operations.sort_keys: `ignore_missing` too)
    keys = sort_keys(params, ctx.columns_of(sql) if params.get("ignore_missing") else None)
    if not keys:
        return sql
    order = ", ".join(f"{ctx.qi(c)} {'DESC' if d else 'ASC'} NULLS LAST" for c, d in keys)
    return f"SELECT * FROM {_sub(sql)} ORDER BY {order}"


@_register("limit")
def op_limit(sql, params, ctx):
    return f"SELECT * FROM {_sub(sql)} LIMIT {int(_require(params, 'n'))}"


@_register("sample")
def op_sample(sql, params, ctx):
    # a DETERMINISTIC random sample: hash of the row's content (+ seed). Other rows than
    # the other engines' (another hash), but the same at every run.
    threshold, seed = sample_threshold(params)
    return (
        f"SELECT * FROM {_sub(sql)} AS _r WHERE "
        f"MOD(ABS(FARM_FINGERPRINT(CONCAT({_lit(str(seed))}, TO_JSON_STRING(_r)))), {SAMPLE_BUCKETS}) < {threshold}"
    )


@_register("unique")
def op_unique(sql, params, ctx):
    subset = params.get("subset")
    if subset:
        part = ", ".join(ctx.qi(c) for c in subset)
        # one row per combination (which one, as on ClickHouse, is not guaranteed)
        return f"SELECT * FROM {_sub(sql)} QUALIFY ROW_NUMBER() OVER (PARTITION BY {part}) = 1"
    return f"SELECT DISTINCT * FROM {_sub(sql)}"


@_register("fill_null")
def op_fill_null(sql, params, ctx):
    cols = _require(params, "columns")
    repls = ", ".join(f"COALESCE({ctx.qi(c)}, {_lit(v)}) AS {ctx.qi(c)}" for c, v in cols.items())
    return f"SELECT * REPLACE ({repls}) FROM {_sub(sql)}"


@_register("drop_nulls")
def op_drop_nulls(sql, params, ctx):
    subset = params.get("subset") or ctx.columns_of(sql)
    cond = " AND ".join(f"{ctx.qi(c)} IS NOT NULL" for c in subset)
    return f"SELECT * FROM {_sub(sql)} WHERE {cond}"


# ── aggregation ───────────────────────────────────────────────────────────────
# `median` has no EXACT aggregate in BigQuery: it is computed separately with
# PERCENTILE_CONT (analytic) and joined by key (see op_group_by).
_AGG = {
    "sum": "SUM", "mean": "AVG", "min": "MIN", "max": "MAX", "count": "COUNT",
    "std": "STDDEV_SAMP", "var": "VAR_SAMP",
    "first": "ANY_VALUE", "last": "ANY_VALUE", "n_unique": "COUNT_DISTINCT", "median": "MEDIAN",
}


def _agg_expr(func: str, col_sql: str) -> str:
    if func == "n_unique":
        return f"COUNT(DISTINCT {col_sql})"
    return f"{_AGG[func]}({col_sql})"


@_register("group_by")
def op_group_by(sql, params, ctx):
    by = _as_list(_require(params, "by"))
    aggs = _require(params, "aggregations")
    by_sql = ", ".join(ctx.qi(c) for c in by)
    base = _sub(sql)
    out: list[str] = [f"a.{ctx.qi(c)}" for c in by]
    parts = [by_sql]
    medians: list[tuple[str, str]] = []  # (column, alias)
    for agg in aggs:
        col, func = agg["column"], agg.get("func", "sum")
        if func not in _AGG:
            raise EngineError(f"group_by: unsupported aggregation function '{func}'")
        alias = agg.get("alias") or f"{col}_{func}"
        if func == "median":
            medians.append((col, alias))
            out.append(f"m.{ctx.qi(alias)}")
            continue
        parts.append(f"{_agg_expr(func, ctx.qi(col))} AS {ctx.qi(alias)}")
        out.append(f"a.{ctx.qi(alias)}")
    agg_sql = f"SELECT {', '.join(parts)} FROM {base} GROUP BY {by_sql}"
    if not medians:
        return agg_sql
    # the EXACT median: PERCENTILE_CONT is analytic only → one row per key (DISTINCT)
    # and a join on the keys with NULL-safe semantics
    med_cols = ", ".join(f"PERCENTILE_CONT({ctx.qi(c)}, 0.5) OVER (PARTITION BY {by_sql}) AS {ctx.qi(a)}" for c, a in medians)
    med_sql = f"SELECT DISTINCT {by_sql}, {med_cols} FROM {base}"
    on = " AND ".join(f"a.{ctx.qi(c)} IS NOT DISTINCT FROM m.{ctx.qi(c)}" for c in by)
    return f"SELECT {', '.join(out)} FROM ({agg_sql}) AS a LEFT JOIN ({med_sql}) AS m ON {on}"


# ── compute (scalar expressions, WITHOUT subqueries or external access) ──────────
# The expression is written in the other engines' dialect (DuckDB/Polars SQL, then
# translated below): it is parsed in that dialect, so EXTRACT(YEAR FROM d) passes
# and a subquery or a table function does not (see app/engine/expressions.py).
# These names reach outside the row only on BigQuery: models, Vertex AI, the
# service account's identity.
_BIGQUERY_ONLY = re.compile(r"\b(?:external_query|ml\.|ai\.|session_user)", re.IGNORECASE)


def to_bigquery_expr(expr: str) -> str:
    """Translates a scalar expression from the other engines' dialect (DuckDB/Polars SQL:
    `%`, strftime, date_part…) into GoogleSQL with sqlglot. Best effort: when it cannot
    be parsed it stays as it is (perhaps it is GoogleSQL already)."""
    try:
        import sqlglot

        out = sqlglot.transpile(f"SELECT {expr}", read="duckdb", write="bigquery")[0]
    except Exception:  # noqa: BLE001
        return expr
    return out[7:] if out.upper().startswith("SELECT ") else expr


@_register("compute")
def op_compute(sql, params, ctx):
    columns = _require(params, "columns")  # [{name, expr}]
    if not isinstance(columns, list):
        raise EngineError("compute: define at least one calculated column")
    existing = set(ctx.columns_of(sql))
    for c in columns:
        name = str(c.get("name") or "").strip()
        expr = str(c.get("expr") or "").strip()
        if not name or not expr:
            raise EngineError("compute: name and expression are required")
        if forbidden_expression(expr, "duckdb") or _BIGQUERY_ONLY.search(expr):
            raise EngineError(
                f"compute: the expression of '{name}' is not allowed (no subqueries, "
                "FROM or external functions — scalar expressions only)."
            )
        expr = ctx.map_quoted(to_bigquery_expr(expr))
        if name in existing:
            sql = f"SELECT * REPLACE (({expr}) AS {ctx.qi(name)}) FROM {_sub(sql)}"
        else:
            sql = f"SELECT *, ({expr}) AS {ctx.qi(name)} FROM {_sub(sql)}"
        existing.add(name)
    return sql


# ── Execute SQL (a free query on the input, GoogleSQL dialect) ─────────────────
_SQL_FORBIDDEN = re.compile(
    r"\b(?:insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|"
    r"export|load|call|execute|declare|begin|set|external_query|ml\.|ai\.|"
    r"information_schema|session_user)\b",
    re.IGNORECASE,
)


@_register("sql")
def op_sql(sql, params, ctx):
    """A free query on the node's input, exposed as the CTE `self` (alias `input`), the
    other engines' convention. Composable as any other op."""
    query = str(_require(params, "query")).strip().rstrip(";").strip()
    if not query:
        raise EngineError("sql: the query is empty")
    if not re.search(r"\bfrom\s+(?:self|input)\b", query, re.IGNORECASE):
        raise EngineError("sql: the query must read from the node's input — use `FROM self` (or `FROM input`).")
    if "--" in query or "/*" in query:
        raise EngineError("sql: comments (-- and /* */) are not allowed in the SQL node.")
    if ";" in query:
        raise EngineError("sql: only one SELECT statement is allowed (no ';').")
    if _SQL_FORBIDDEN.search(query) or re.search(r"`[^`]*\.[^`]*`", query):
        raise EngineError(
            "sql: the query may only read the node's input. DDL/DML, scripts, "
            "EXTERNAL_QUERY, ML/AI and references to the project's tables are not allowed."
        )
    # an ALLOWLIST: `dataset.table` can be written WITHOUT backticks too, and the other
    # users' step cache lives in a dataset of the same project
    ensure_reads_only_input(query, "bigquery")
    return f"WITH input AS ({sql}), self AS (SELECT * FROM input) {ctx.map_quoted(query)}"


# ── join / union ──────────────────────────────────────────────────────────────
def _join_condition(ctx, params: dict) -> tuple[str, str, list[tuple[str, str]]]:
    """Returns (SQL clause, kind, key pairs) with kind='using' or 'on'."""
    if "on" in params and params["on"]:
        keys = _as_list(params["on"])
        return f"USING ({', '.join(ctx.qi(c) for c in keys)})", "using", [(k, k) for k in keys]
    lo = _as_list(_require(params, "left_on"))
    ro = _as_list(_require(params, "right_on"))
    if len(lo) != len(ro):
        raise EngineError("join: left_on and right_on must have the same number of columns")
    cond = " AND ".join(f"l.{ctx.qi(l)} = r.{ctx.qi(r)}" for l, r in zip(lo, ro))
    return f"ON {cond}", "on", list(zip(lo, ro))


def _join_select(ctx, lcols, rcols, skip_right: set, coalesce_keys: set = frozenset()) -> str:
    lset = set(lcols)
    parts = [
        f"COALESCE(l.{ctx.qi(c)}, r.{ctx.qi(c)}) AS {ctx.qi(c)}" if c in coalesce_keys else f"l.{ctx.qi(c)}"
        for c in lcols
    ]
    for c in rcols:
        if c in skip_right:
            continue
        parts.append(f"r.{ctx.qi(c)} AS {ctx.qi(c + '_right')}" if c in lset else f"r.{ctx.qi(c)}")
    return ", ".join(parts)


_JOIN_KW = {"inner": "INNER JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL OUTER JOIN"}


@_register("join")
def op_join(sql, params, ctx):
    right = ctx.build_right(_require(params, "right"))
    how = params.get("how", "inner")
    lcols, rcols = ctx.columns_of(sql), ctx.columns_of(right)
    L, R = _sub(sql), _sub(right)

    if how == "cross":
        ln = ctx.scalar(f"SELECT COUNT(*) FROM {L}")
        rn = ctx.scalar(f"SELECT COUNT(*) FROM {R}")
        if MAX_CROSS_JOIN_ROWS and ln * rn > MAX_CROSS_JOIN_ROWS:
            raise EngineError(
                f"The cross join would produce {ln * rn:,} rows ({ln:,} × {rn:,}), "
                f"over the limit of {MAX_CROSS_JOIN_ROWS:,}. Add a join condition "
                "or reduce the sources."
            )
        sel = _join_select(ctx, lcols, rcols, set())
        return f"SELECT {sel} FROM {L} AS l CROSS JOIN {R} AS r"

    clause, kind, pairs = _join_condition(ctx, params)
    if how in ("semi", "anti"):
        # BigQuery has no SEMI/ANTI JOIN: EXISTS with the same keys
        cond = " AND ".join(f"l.{ctx.qi(l)} = r.{ctx.qi(r)}" for l, r in pairs)
        kw = "EXISTS" if how == "semi" else "NOT EXISTS"
        return f"SELECT l.* FROM {L} AS l WHERE {kw} (SELECT 1 FROM {R} AS r WHERE {cond})"
    if how not in _JOIN_KW:
        raise EngineError(f"join: unsupported join type '{how}'")
    skip_right = {k for k, _ in pairs} if kind == "using" else set()
    sel = _join_select(ctx, lcols, rcols, skip_right, skip_right if how in ("full", "right") else set())
    return f"SELECT {sel} FROM {L} AS l {_JOIN_KW[how]} {R} AS r {clause}"


@_register("union")
def op_union(sql, params, ctx):
    right = ctx.build_right(_require(params, "right"))
    if params.get("strategy") == "strict":
        # same columns and types in any order, put together by NAME (by position swapped them)
        left_s, right_s = ctx.schema_of(sql), ctx.schema_of(right)
        check_strict_union(list(left_s), list(right_s))
        cols = ", ".join(ctx.qi(n) for n, _ in left_s)
        return f"SELECT {cols} FROM {_sub(sql)} UNION ALL SELECT {cols} FROM {_sub(right)}"
    lcols, rcols = ctx.columns_of(sql), ctx.columns_of(right)
    allcols = lcols + [c for c in rcols if c not in set(lcols)]
    lset, rset = set(lcols), set(rcols)
    lsel = ", ".join((ctx.qi(c) if c in lset else "NULL") + f" AS {ctx.qi(c)}" for c in allcols)
    rsel = ", ".join((ctx.qi(c) if c in rset else "NULL") + f" AS {ctx.qi(c)}" for c in allcols)
    return f"SELECT {lsel} FROM {_sub(sql)} UNION ALL SELECT {rsel} FROM {_sub(right)}"


# ── pivot / unpivot ───────────────────────────────────────────────────────────
@_register("pivot")
def op_pivot(sql, params, ctx):
    index = _as_list(_require(params, "index"))
    on = _as_list(_require(params, "on"))
    values = _require(params, "values")
    func = params.get("func", "sum")
    if func not in _AGG:
        raise EngineError(f"pivot: unsupported aggregation function '{func}'")
    if func == "median":
        raise EngineError("pivot: the median is not available in a pivot on BigQuery (use another engine).")
    base = _sub(sql)
    on_sql = ", ".join(ctx.qi(c) for c in on)
    n_cols = ctx.scalar(f"SELECT COUNT(*) FROM (SELECT DISTINCT {on_sql} FROM {base})")
    if n_cols > MAX_PIVOT_COLUMNS:
        raise EngineError(
            f"pivot: the chosen columns have {n_cols} distinct combinations ({n_cols} new "
            f"columns, maximum {MAX_PIVOT_COLUMNS}). Are these the right columns?"
        )
    combos = ctx.distinct_rows(base, on)
    combos = sorted(combos, key=lambda combo: tuple(pivot_label(v) for v in combo))
    idx_sql = ", ".join(ctx.qi(c) for c in index)
    cols = [idx_sql] if idx_sql else []
    v = ctx.qi(values)
    for combo in combos:
        cond = " AND ".join(
            f"{ctx.qi(c)} IS NULL" if val is None else f"{ctx.qi(c)} = {_lit(val)}" for c, val in zip(on, combo)
        )
        colname = PIVOT_LABEL_SEP.join(pivot_label(val) for val in combo)
        # conditional aggregation: no rows (or only NULLs) → NULL, except
        # count/n_unique → 0, as in the other engines
        expr = _agg_expr(func, f"IF({cond}, {v}, NULL)")
        cols.append(f"{expr} AS {ctx.qi(colname)}")
    group = f" GROUP BY {idx_sql}" if idx_sql else ""
    return f"SELECT {', '.join(cols)} FROM {base}{group}"


def unpivot_value_type(types: list[str]) -> str:
    """The unpivoted value's supertype (the other engines' rule): all integers → INT64; all
    numeric → FLOAT64; all alike → that one; otherwise STRING."""
    bases = [str(t).upper() for t in types]
    if all(b in _BQ_INTS for b in bases):
        return "INT64"
    if all(b in _BQ_INTS or b in _BQ_FLOATS or b in _BQ_DECIMALS for b in bases):
        return "FLOAT64"
    if len(set(bases)) == 1:
        return bases[0]
    return "STRING"


@_register("unpivot")
def op_unpivot(sql, params, ctx):
    on = params.get("on")
    index = params.get("index") or []
    var = params.get("variable_name") or "variable"
    val = params.get("value_name") or "value"
    schema = dict(ctx.schema_of(sql))
    allcols = list(schema)
    melt = on if on else [c for c in allcols if c not in set(index)]
    if not melt:
        raise EngineError("unpivot: no columns to unpivot")
    missing = [c for c in melt if c not in schema]
    if missing:
        raise EngineError(f"unpivot: columns not found: {', '.join(missing)}")
    keep = [c for c in allcols if c in set(index)]
    vtype = unpivot_value_type([schema[c] for c in melt])

    def _val(c: str) -> str:
        q = ctx.qi(c)
        if vtype == "STRING" and str(schema[c]).upper() in _BQ_FLOATS:
            return (f"IF({q} = TRUNC({q}) AND ABS({q}) < 1e15, "
                    f"CONCAT(CAST(CAST({q} AS INT64) AS STRING), '.0'), CAST({q} AS STRING)) AS {q}")
        return f"CAST({q} AS {vtype}) AS {q}"

    # the native UNPIVOT wants columns of one type: they are brought to the supertype first
    inner_cols = [ctx.qi(c) for c in keep] + [_val(c) for c in melt]
    inner = f"SELECT {', '.join(inner_cols)} FROM {_sub(sql)}"
    keep_sql = "".join(f"{ctx.qi(c)}, " for c in keep)
    return (
        f"SELECT {keep_sql}{ctx.qi(var)}, {ctx.qi(val)} FROM ({inner}) "
        f"UNPIVOT INCLUDE NULLS ({ctx.qi(val)} FOR {ctx.qi(var)} IN ({', '.join(ctx.qi(c) for c in melt)}))"
    )
