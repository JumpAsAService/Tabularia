"""
The registry of the chDB engine's operations (embedded ClickHouse), parallel to
`duckdb_ops.py`. Unlike DuckDB (lazy relation objects) chDB is PURE SQL: every operation
receives the previous step's SELECT and returns a new one that wraps it in a subquery —
`SELECT … FROM (prev)`. The chain stays one plan that ClickHouse optimizes and runs
streaming (spilling to disk).

Signature: `(sql, params, ctx)`. `ctx` serves the column introspection
(`ctx.columns_of`) and the MULTI-INPUT operations (join/union) that read the right side
(`ctx.build_right`).

Security: the structural operations use only column names / operators / values (no free
SQL). `compute` takes scalar expressions but FORBIDS subqueries and reading table
functions (file/url/s3/…) → no filesystem, no network. `sql` (a whole query) and
`foreach` are not supported on chDB yet.
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
            f"operation '{name}' is not supported by the chDB engine yet. "
            "Use the Polars or DuckDB engine for this flow (or this node)."
        )
    return fn


# ── SQL helpers (ClickHouse dialect) ─────────────────────────────────────────
def _qi(name: str) -> str:
    """Quotes a ClickHouse identifier with backticks."""
    return "`" + str(name).replace("\\", "\\\\").replace("`", "\\`") + "`"


def _lit(v: Any) -> str:
    """A SQL literal from a Python value (parsed by the frontend already)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
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
    """Wraps a SELECT as a subquery source."""
    return f"({sql})"


# a "try" cast: ClickHouse's *OrNull functions (NULL instead of an error)
_CH_CAST = {
    "int": "toInt64OrNull(toString({c}))",
    "float": "toFloat64OrNull(toString({c}))",
    "str": "CAST({c} AS Nullable(String))",
    "bool": "toUInt8OrNull(toString({c}))",
    "date": "toDate32OrNull(toString({c}))",
    "datetime": "parseDateTimeBestEffortOrNull(toString({c}))",
}


# ── columns ───────────────────────────────────────────────────────────────────
@_register("select")
def op_select(sql, params, ctx):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return f"SELECT {cols} FROM {_sub(sql)}"


@_register("reorder")
def op_reorder(sql, params, ctx):
    # the listed columns first (in order), then the others (EXCEPT the listed ones)
    order = _require(params, "columns")
    cols = ", ".join(_qi(c) for c in order)
    return f"SELECT {cols}, * EXCEPT ({cols}) FROM {_sub(sql)}"


@_register("drop")
def op_drop(sql, params, ctx):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return f"SELECT * EXCEPT ({cols}) FROM {_sub(sql)}"


@_register("rename")
def op_rename(sql, params, ctx):
    mapping = _require(params, "mapping")
    # ClickHouse has no `* RENAME`: the list is rebuilt keeping the order
    cols = ctx.columns_of(sql)
    parts = [
        f"{_qi(c)} AS {_qi(mapping[c])}" if c in mapping else _qi(c) for c in cols
    ]
    return f"SELECT {', '.join(parts)} FROM {_sub(sql)}"


def _cast_expr(c: str, base: str, dt: str) -> str:
    """The same STANDARD as Polars'/DuckDB's cast (see operations.op_cast): failed → NULL;
    text with its spaces trimmed; text → integer only for an integer literal; number →
    integer by truncation; float → text with ".0" on whole values (ClickHouse would print
    200.0 as "200")."""
    is_text = base == "String" or base.startswith("FixedString")
    is_num = base in _CH_FLOATS or base.startswith("Decimal")
    if dt == "int":
        if is_text:
            return f"toInt64OrNull(trimBoth({c}))"
        if is_num:
            return f"toInt64OrNull(toString(trunc({c})))"
        return f"accurateCastOrNull({c}, 'Int64')"
    if dt == "float":
        if is_text:
            return f"toFloat64OrNull(trimBoth({c}))"
        return f"accurateCastOrNull({c}, 'Float64')"
    if dt == "str":
        if base in _CH_FLOATS:
            return (f"if({c} = trunc({c}) AND abs({c}) < 1e15, "
                    f"concat(toString(toInt64({c})), '.0'), toString({c}))")
        return f"CAST({c} AS Nullable(String))"
    if dt == "date":
        if is_text:
            # ClickHouse is lenient ("2024-02-30" → 1 March): valid only when the date
            # read back matches the text, as Polars/DuckDB
            return f"if(toString(toDate32OrNull(trimBoth({c}))) = trimBoth({c}), toDate32OrNull(trimBoth({c})), NULL)"
        return f"toDate32OrNull(toString({c}))"
    if dt == "datetime":
        return f"parseDateTimeBestEffortOrNull(trimBoth(toString({c})))"
    if dt == "bool":
        if is_text:  # the words DuckDB takes («true» was NULL, «2» was 2)
            w = f"lower(trimBoth({c}))"
            yes = ", ".join(f"'{x}'" for x in TEXT_TRUE)
            no = ", ".join(f"'{x}'" for x in TEXT_FALSE)
            return f"CAST(multiIf({w} IN ({yes}), 1, {w} IN ({no}), 0, NULL) AS Nullable(Bool))"
        return f"CAST(if(isNull({c}), NULL, {c} != 0) AS Nullable(Bool))"
    raise EngineError(f"cast: unsupported type '{dt}'")


@_register("cast")
def op_cast(sql, params, ctx):
    cols = _require(params, "columns")
    schema = dict(ctx.schema_of(sql))
    repls = []
    for col, dt in cols.items():
        if str(dt) not in _CH_CAST:
            raise EngineError(f"cast: unsupported type '{dt}'")
        base = _ch_base_type(schema.get(col, ""))
        repls.append(f"{_cast_expr(_qi(col), base, str(dt))} AS {_qi(col)}")
    return f"SELECT * REPLACE ({', '.join(repls)}) FROM {_sub(sql)}"


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
        return f"position(toString({c}), {_lit(value)}) > 0"
    if operator == "starts_with":
        return f"startsWith(toString({c}), {_lit(value)})"
    if operator == "ends_with":
        return f"endsWith(toString({c}), {_lit(value)})"
    ops = {"eq": "=", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
    if operator not in ops:
        raise EngineError(f"filter: unsupported operator '{operator}'")
    return f"{c} {ops[operator]} {_lit(value)}"


@_register("filter")
def op_filter(sql, params, ctx):
    column = _require(params, "column")
    operator = params.get("operator", "eq")
    value = params.get("value")
    check_filter_value(operator, value)
    bad = non_iso_values(operator, value)
    timed = operator not in ("contains", "starts_with", "ends_with") and any(map(_iso_datetime, _as_list(value)))
    if bad or timed:  # only then the column's type matters: one DESCRIBE
        ctype = dict(ctx.schema_of(sql)).get(column, "")
        if bad and re.search(r"\bDate(Time)?(32|64)?\b", ctype):
            raise temporal_value_error(bad[0])
        if timed and "DateTime" in ctype:
            norm = [_datetime_literal(v) if _iso_datetime(v) else v for v in _as_list(value)]
            value = norm if isinstance(value, list) else norm[0]
    pred = _predicate(column, operator, value)
    return f"SELECT * FROM {_sub(sql)} WHERE {pred}"


def _iso_datetime(v: Any) -> bool:
    """An ISO date-time with its time (not a bare date)."""
    if not isinstance(v, str) or len(v) <= 10:
        return False
    from datetime import datetime

    try:
        datetime.fromisoformat(v)
    except ValueError:
        return False
    return True


def _datetime_literal(v: str) -> str:
    """The editor's calendar sends 2024-01-15T05:30, without seconds: ClickHouse reads a
    DateTime only with them («Cannot parse time component»), so every filter on a date-time
    failed on chDB and on an external ClickHouse. The full form, in UTC as the columns are."""
    from datetime import datetime, timezone

    d = datetime.fromisoformat(v)
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d.strftime("%Y-%m-%d %H:%M:%S") + (f".{d.microsecond:06d}" if d.microsecond else "")


# ── ordering / rows ──────────────────────────────────────────────────────────
@_register("sort")
def op_sort(sql, params, ctx):
    # the keys and a direction each (operations.sort_keys: `ignore_missing` too)
    keys = sort_keys(params, ctx.columns_of(sql) if params.get("ignore_missing") else None)
    if not keys:
        return sql
    # cross-engine standard: NULL always last (see operations.op_sort)
    order = ", ".join(f"{_qi(c)} {'DESC' if d else 'ASC'} NULLS LAST" for c, d in keys)
    return f"SELECT * FROM {_sub(sql)} ORDER BY {order}"


@_register("limit")
def op_limit(sql, params, ctx):
    return f"SELECT * FROM {_sub(sql)} LIMIT {int(_require(params, 'n'))}"


@_register("sample")
def op_sample(sql, params, ctx):
    # a deterministic random sample: hash of the row's content (+ seed)
    threshold, seed = sample_threshold(params)
    return f"SELECT * FROM {_sub(sql)} WHERE cityHash64({seed}, *) % {SAMPLE_BUCKETS} < {threshold}"


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


def _aggregato(func: str, col_sql: str, cond: str | None = None) -> str:
    """The aggregation in ClickHouse SQL (with the -If combinator when there is a condition).
    Sample std/var: NULL with fewer than two values, as Polars, DuckDB and BigQuery
    (ClickHouse gives NaN, which «fill the NULLs» downstream does not replace)."""
    suff, extra = ("If", f", {cond}") if cond else ("", "")
    expr_ = f"{_AGG[func]}{suff}({col_sql}{extra})"
    if func in ("std", "var"):
        return f"if(count{suff}({col_sql}{extra}) > 1, {expr_}, NULL)"
    return expr_


@_register("group_by")
def op_group_by(sql, params, ctx):
    by = _as_list(_require(params, "by"))
    aggs = _require(params, "aggregations")
    by_sql = ", ".join(_qi(c) for c in by)
    parts = [by_sql]
    for agg in aggs:
        col, func = agg["column"], agg.get("func", "sum")
        if func not in _AGG:
            raise EngineError(f"group_by: unsupported aggregation function '{func}'")
        alias = agg.get("alias") or f"{col}_{func}"
        expr = f"uniqExact({_qi(col)})" if func == "n_unique" else _aggregato(func, _qi(col))
        parts.append(f"{expr} AS {_qi(alias)}")
    return f"SELECT {', '.join(parts)} FROM {_sub(sql)} GROUP BY {by_sql}"


# ── compute (scalar expressions, WITHOUT subqueries or file/network reads) ────
# the check is in app/engine/expressions.py (it parses the expression, it does not match words)


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
        if forbidden_expression(expr, "clickhouse"):
            raise EngineError(
                f"compute: the expression of '{name}' is not allowed (no subqueries, "
                "tables, table functions or functions that read data outside the row — "
                "scalar expressions only)."
            )
        expr = _utf8_functions(expr)
        if name in existing:
            # an existing column: overwritten IN ITS PLACE (as Polars/DuckDB)
            sql = f"SELECT * REPLACE (({expr}) AS {_qi(name)}) FROM {_sub(sql)}"
        else:
            sql = f"SELECT *, ({expr}) AS {_qi(name)} FROM {_sub(sql)}"
        existing.add(name)
    return sql


# In ClickHouse upper/lower/length/substring/reverse work on BYTES: "Città" →
# "CITTà", length = 6. The *UTF8 variants give the same result as Polars/DuckDB
# (characters): the same compute expression holds everywhere.
_CH_UTF8_FUNCS = {
    "upper": "upperUTF8", "ucase": "upperUTF8", "lower": "lowerUTF8", "lcase": "lowerUTF8",
    "length": "lengthUTF8", "char_length": "lengthUTF8", "character_length": "lengthUTF8",
    "substring": "substringUTF8", "substr": "substringUTF8", "mid": "substringUTF8",
    "reverse": "reverseUTF8",
}
_CH_UTF8_RE = re.compile(r"\b(" + "|".join(_CH_UTF8_FUNCS) + r")\s*\(", re.IGNORECASE)


def _utf8_functions(expr: str) -> str:
    return _CH_UTF8_RE.sub(lambda m: _CH_UTF8_FUNCS[m.group(1).lower()] + "(", expr)


# ── Execute SQL (a free query on the input, ClickHouse dialect) ───────────────
# Table functions of EXTERNAL ACCESS / EXECUTION: forbidden. They do not limit the
# analytical SQL (SELECT/JOIN/GROUP BY/window/subquery/CTE stay free) — they only block
# file reads / SSRF / RCE on the server, which would break the RBAC for everyone.
_SQL_FORBIDDEN_FUNCS = re.compile(
    r"\b(?:file|url|s3|s3Cluster|fileCluster|urlCluster|hdfs|hdfsCluster|"
    r"remote|remoteSecure|cluster|clusterAllReplicas|"
    r"mysql|postgresql|jdbc|odbc|mongodb|redis|sqlite|"
    r"azureBlobStorage|gcs|deltaLake|iceberg|hudi|executable|dictionary)\s*\(",
    re.IGNORECASE,
)
# dangerous statements and keywords (DDL/DML, file writes, settings changes, system)
_SQL_FORBIDDEN_KW = re.compile(
    r"\b(?:insert|attach|detach|create|alter|drop|truncate|optimize|rename|"
    r"grant|revoke|system|use|kill|set|into\s+outfile|into\s+dumpfile)\b",
    re.IGNORECASE,
)


@_register("sql")
def op_sql(sql, params, ctx):
    """A free SQL query (ClickHouse dialect) on the node's input, exposed as the CTE `self`
    (alias `input`) — the same convention as Polars/DuckDB. Composable as any other op
    (it stays a nestable SELECT).

    Security: the analytical SQL is free, but the table functions of external access or
    execution (file/url/s3/remote/executable/…), comments and multiple statements are
    forbidden — so it cannot turn into a file read, SSRF or RCE on the server.
    """
    query = str(_require(params, "query")).strip().rstrip(";").strip()
    if not query:
        raise EngineError("sql: the query is empty")
    # the node TRANSFORMS its input: it must be referred to as `self` or `input`
    if not re.search(r"\bfrom\s+(?:self|input)\b", query, re.IGNORECASE):
        raise EngineError("sql: the query must read from the node's input — use `FROM self` (or `FROM input`).")
    # no comments (they would hide from the checks) and no more than one statement
    if "--" in query or "/*" in query:
        raise EngineError("sql: comments (-- and /* */) are not allowed in the SQL node.")
    if ";" in query:
        raise EngineError("sql: only one SELECT statement is allowed (no ';').")
    if _SQL_FORBIDDEN_FUNCS.search(query) or _SQL_FORBIDDEN_KW.search(query):
        raise EngineError(
            "sql: the query may only read the input. Table functions with external "
            "access (file/url/s3/remote/…), execution (executable) and DDL/DML are not allowed."
        )
    # an ALLOWLIST of the tables: the denylist above stays as the first line, but alone
    # it was not enough (`merge()`, `information_schema`: see sql_guard)
    ensure_reads_only_input(query, "clickhouse")
    # exposes the input as `self` (and the alias `input`); it stays a nestable SELECT.
    # A query with ITS OWN `WITH` gets our CTEs at the head of its list: two WITHs in a
    # row are not valid SQL (found by the dbt export oracle, 2026-10-10: on ClickHouse a
    # sql node with WITH failed with SYNTAX_ERROR, on Polars it did not)
    ours = f"input AS ({sql}), self AS (SELECT * FROM input)"
    own = re.match(r"(?is)^\s*with\s+", query)
    if own:
        return f"WITH {ours}, {query[own.end():]}"
    return f"WITH {ours} {query}"


# ── join / union (they read the right side from the nested source) ─────────────
def _join_condition(params: dict) -> tuple[str, str]:
    """Returns (SQL clause, kind) where kind='using' or 'on'."""
    if "on" in params and params["on"]:
        keys = ", ".join(_qi(c) for c in _as_list(params["on"]))
        return f"USING ({keys})", "using"
    lo = _as_list(_require(params, "left_on"))
    ro = _as_list(_require(params, "right_on"))
    if len(lo) != len(ro):
        raise EngineError("join: left_on and right_on must have the same number of columns")
    cond = " AND ".join(f"l.{_qi(l)} = r.{_qi(r)}" for l, r in zip(lo, ro))
    return f"ON {cond}", "on"


def _join_select(lcols, rcols, skip_right: set, coalesce_keys: set = frozenset()) -> str:
    """The result's columns: all the left's + the right's (USING keys excluded), with the
    _right suffix on the same names — as Polars/DuckDB. In full/right joins the USING keys
    are coalesce(left, right): filled in the rows that exist only on the right too."""
    lset = set(lcols)
    parts = [
        f"coalesce(l.{_qi(c)}, r.{_qi(c)}) AS {_qi(c)}" if c in coalesce_keys else f"l.{_qi(c)}"
        for c in lcols
    ]
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
                f"The cross join would produce {ln * rn:,} rows ({ln:,} × {rn:,}), "
                f"over the limit of {MAX_CROSS_JOIN_ROWS:,}. Add a join condition "
                "or reduce the sources."
            )
        sel = _join_select(lcols, rcols, set())
        return f"SELECT {sel} FROM {L} AS l CROSS JOIN {R} AS r"

    clause, kind = _join_condition(params)
    if how in ("semi", "anti"):
        kw = "LEFT SEMI JOIN" if how == "semi" else "LEFT ANTI JOIN"
        return f"SELECT l.* FROM {L} AS l {kw} {R} AS r {clause}"
    if how not in _JOIN_KW:
        raise EngineError(f"join: unsupported join type '{how}'")
    skip_right = set(_as_list(params["on"])) if kind == "using" else set()
    sel = _join_select(lcols, rcols, skip_right, skip_right if how in ("full", "right") else set())
    return f"SELECT {sel} FROM {L} AS l {_JOIN_KW[how]} {R} AS r {clause}"


@_register("union")
def op_union(sql, params, ctx):
    right = ctx.build_right(_require(params, "right"))
    if params.get("strategy") == "strict":
        # the same columns and types, in any order: joined by NAME (by position they
        # were swapped silently)
        left_s, right_s = ctx.schema_of(sql), ctx.schema_of(right)
        check_strict_union([(n, _bare_type(t)) for n, t in left_s], [(n, _bare_type(t)) for n, t in right_s])
        cols = ", ".join(_qi(n) for n, _ in left_s)
        return f"SELECT {cols} FROM {_sub(sql)} UNION ALL SELECT {cols} FROM {_sub(right)}"
    # relaxed: aligned by NAME (ClickHouse has no UNION ALL BY NAME) → for every column
    # of the union, each side selects it, or NULL when it lacks it
    lcols, rcols = ctx.columns_of(sql), ctx.columns_of(right)
    allcols = lcols + [c for c in rcols if c not in set(lcols)]
    lset, rset = set(lcols), set(rcols)
    lsel = ", ".join((_qi(c) if c in lset else "NULL") + f" AS {_qi(c)}" for c in allcols)
    rsel = ", ".join((_qi(c) if c in rset else "NULL") + f" AS {_qi(c)}" for c in allcols)
    return f"SELECT {lsel} FROM {_sub(sql)} UNION ALL SELECT {rsel} FROM {_sub(right)}"


def _bare_type(t: str) -> str:
    """The type without Nullable(…)/LowCardinality(…): a side with NULLs and one without
    hold the same type."""
    while True:
        m = re.fullmatch(r"(?:Nullable|LowCardinality)\((.*)\)", t.strip())
        if not m:
            return t.strip()
        t = m.group(1)


# ── pivot / unpivot (rows↔columns) ────────────────────────────────────────────
# The same cross-engine standard as operations.py (`pivot_label`): column names, NULL
# semantics and types match Polars/DuckDB, so a flow designed on one engine runs the
# same on ClickHouse in production.
@_register("pivot")
def op_pivot(sql, params, ctx):
    index = _as_list(_require(params, "index"))
    on = _as_list(_require(params, "on"))  # several columns = combinations
    values = _require(params, "values")
    func = params.get("func", "sum")
    if func not in _AGG:
        raise EngineError(f"pivot: unsupported aggregation function '{func}'")
    base = _sub(sql)
    on_sql = ", ".join(_qi(c) for c in on)
    n_cols = ctx.scalar(f"SELECT count() FROM (SELECT DISTINCT {on_sql} FROM {base})")
    if n_cols > MAX_PIVOT_COLUMNS:
        raise EngineError(
            f"pivot: the chosen columns have {n_cols} distinct combinations ({n_cols} new "
            f"columns, maximum {MAX_PIVOT_COLUMNS}). Are these the right columns?"
        )
    combos = ctx.distinct_rows(base, on)  # ClickHouse has no PIVOT: conditional columns
    # the new columns' order = the labels' TEXT order (as Polars and DuckDB, which sort
    # the string key): "10" < "2", "null" between "false" and "true"
    combos = sorted(combos, key=lambda combo: tuple(pivot_label(v) for v in combo))
    idx_sql = ", ".join(_qi(c) for c in index)
    cols = [idx_sql] if idx_sql else []
    for combo in combos:
        cond = " AND ".join(
            f"{_qi(c)} IS NULL" if v is None else f"{_qi(c)} = {_lit(v)}" for c, v in zip(on, combo)
        )
        colname = PIVOT_LABEL_SEP.join(pivot_label(v) for v in combo)
        # the -If combinator: aggregates only the combination's rows; no rows (or only
        # NULLs) → NULL, except count/n_unique → 0 (Int64 as elsewhere)
        expr = _aggregato(func, _qi(values), cond)
        if func in ("count", "n_unique"):
            expr = f"toInt64({expr})"
        cols.append(f"{expr} AS {_qi(colname)}")
    return f"SELECT {', '.join(cols)} FROM {base} GROUP BY {idx_sql}"


# ClickHouse types → family, for the value's supertype in unpivot
_CH_INTS = {f"Int{b}" for b in (8, 16, 32, 64, 128, 256)} | {f"UInt{b}" for b in (8, 16, 32, 64, 128, 256)}
_CH_FLOATS = {"Float32", "Float64"}


def temporal_safe_sql(ctx, sql: str) -> str:
    """ClickHouse exports `Date` as UInt16 and `DateTime` (32 bit) as UInt32 — INTEGERS — to
    Arrow/Parquet (19727 instead of 2024-01-05, 1704450600 instead of 2024-01-05
    10:30:00): every column of those types, made by casts, computes or date functions, is
    promoted to Date32/DateTime64, which come out as true DATE/TIMESTAMP. To apply to every
    FINAL SQL (preview, run, cache): the sources read from parquet are Date32/DateTime64
    already."""
    repls = []
    for name, t in ctx.schema_of(sql):
        base = _ch_base_type(t)
        if base == "Date":
            repls.append(f"toDate32({_qi(name)}) AS {_qi(name)}")
        elif base == "DateTime" or base.startswith("DateTime("):
            repls.append(f"toDateTime64({_qi(name)}, 0) AS {_qi(name)}")
    if not repls:
        return sql
    return f"SELECT * REPLACE ({', '.join(repls)}) FROM {_sub(sql)}"


def _ch_base_type(t: str) -> str:
    """Removes the Nullable(...) / LowCardinality(...) wrappers."""
    t = t.strip()
    for w in ("Nullable(", "LowCardinality("):
        while t.startswith(w) and t.endswith(")"):
            t = t[len(w):-1].strip()
    return t


def unpivot_value_type(types: list[str]) -> str:
    """The unpivoted value's supertype (the same rule as Polars/DuckDB): all integers →
    Int64; all numeric → Float64; all of one type → that one; otherwise String."""
    bases = [_ch_base_type(t) for t in types]
    if all(b in _CH_INTS for b in bases):
        return "Int64"
    if all(b in _CH_INTS or b in _CH_FLOATS or b.startswith("Decimal") for b in bases):
        return "Float64"
    if len(set(bases)) == 1:
        return bases[0]
    return "String"


@_register("unpivot")
def op_unpivot(sql, params, ctx):
    on = params.get("on")
    index = params.get("index") or []
    var = params.get("variable_name") or "variable"
    val = params.get("value_name") or "value"
    schema = dict(ctx.schema_of(sql))  # name → ClickHouse type
    allcols = list(schema)
    melt = on if on else [c for c in allcols if c not in set(index)]
    if not melt:
        raise EngineError("unpivot: no columns to unpivot")
    missing = [c for c in melt if c not in schema]
    if missing:
        raise EngineError(f"unpivot: columns not found: {', '.join(missing)}")
    # as Polars: ONLY the index columns stay (the other columns not unpivoted are lost)
    keep = [c for c in allcols if c in set(index)]
    keep_sql = "".join(f"{_qi(c)}, " for c in keep)
    # arrayJoin over tuples (name, value at the common supertype): one row per column
    vtype = unpivot_value_type([schema[c] for c in melt])

    def _val(c: str) -> str:
        if vtype == "String" and _ch_base_type(schema[c]) in _CH_FLOATS:
            # ClickHouse prints 7.0 as "7": aligned to Polars/DuckDB ("7.0")
            q = _qi(c)
            return (f"if({q} = trunc({q}) AND abs({q}) < 1e15, "
                    f"concat(toString(toInt64({q})), '.0'), toString({q}))")
        return f"CAST({_qi(c)} AS Nullable({vtype}))"

    tuples = ", ".join(f"({_lit(c)}, {_val(c)})" for c in melt)
    base = _sub(sql)
    return (
        f"SELECT {keep_sql}tup.1 AS {_qi(var)}, tup.2 AS {_qi(val)} "
        f"FROM {base} ARRAY JOIN [{tuples}] AS tup"
    )
