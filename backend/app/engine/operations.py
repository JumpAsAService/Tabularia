"""
The registry of the flow's operations.

Every operation is a pure function `(lf, params, ctx) -> lf` that maps a node of the IR
(a `params` dict) to a transformation on a `LazyFrame`. Adding an operation = registering
a function with `@register("name")`.

A deliberately declarative design: no `eval` of arbitrary code (security + JSON
serializability for Celery). The transformations are a closed, verified set. The one
controlled exception: `compute` parses SQL expressions with `pl.sql_expr` — a parser of
expressions ONLY (no statements, no I/O, no Python), so the perimeter stays closed.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import polars as pl

from app.engine.base import DataSource
from app.engine.context import OperationContext
from app.engine.dtypes import resolve_dtype as _dtype
from app.engine.exceptions import EngineError, UnknownOperationError

# the type of an operation function
OperationFn = Callable[[pl.LazyFrame, dict[str, Any], OperationContext], pl.LazyFrame]

_REGISTRY: dict[str, OperationFn] = {}


def register(name: str) -> Callable[[OperationFn], OperationFn]:
    def deco(fn: OperationFn) -> OperationFn:
        _REGISTRY[name] = fn
        return fn

    return deco


def get_operation(name: str) -> OperationFn:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownOperationError(name) from None


def available_operations() -> list[str]:
    return sorted(_REGISTRY)


def _require(params: dict[str, Any], key: str) -> Any:
    if key not in params:
        raise EngineError(f"required parameter missing: '{key}'")
    return params[key]


# ─────────────────────────────────────────────────────────────────────────────
# Selecting / dropping / renaming columns
# ─────────────────────────────────────────────────────────────────────────────
@register("select")
def op_select(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    return lf.select(_require(params, "columns"))


@register("reorder")
def op_reorder(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # reorder: the listed columns first (in this order), then ALL the others (so the
    # columns added upstream after the configuration are not lost).
    order = _require(params, "columns")
    return lf.select([*order, pl.exclude(order)])


@register("drop")
def op_drop(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    return lf.drop(_require(params, "columns"))


@register("rename")
def op_rename(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    return lf.rename(_require(params, "mapping"))


@register("cast")
def op_cast(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"columns": {"col": "int", "other": "float"}}
    #
    # The CROSS-ENGINE STANDARD of the cast (the same in DuckDB/chDB/ClickHouse):
    # - an impossible conversion → NULL, never an error ("x" → NULL, "2024-02-30" → NULL);
    # - text → number/date: surrounding spaces trimmed (" 4 " → 4);
    # - text → integer: integer literals ONLY ("2.7" → NULL, neither 3 nor 2);
    # - number (float/decimal) → integer: TRUNCATION toward zero (100.9 → 100,
    #   -2.7 → -2); out of range → NULL.
    columns: dict[str, str] = _require(params, "columns")
    schema = lf.collect_schema()
    exprs: list[pl.Expr] = []
    for c, t in columns.items():
        target = _dtype(t)
        src = schema.get(c)
        e = pl.col(c)
        if src == pl.String:
            e = e.str.strip_chars()
            if target.is_integer():
                e = pl.when(e.str.contains(r"^[+-]?\d+$")).then(e).otherwise(None)
            elif isinstance(target, pl.Datetime) or target == pl.Datetime:
                # "2024-01-05 10:30:00" (a space) is as good as "2024-01-05T10:30:00"
                e = e.str.replace(r"^(\d{4}-\d{2}-\d{2})\s+", "${1}T")
            if target == pl.Boolean:
                # Polars does not cast text to bool: the words DuckDB takes, else NULL
                exprs.append(e.str.to_lowercase().replace_strict(TEXT_TRUE + TEXT_FALSE,
                                                                [True] * len(TEXT_TRUE) + [False] * len(TEXT_FALSE),
                                                                default=None, return_dtype=pl.Boolean).alias(c))
                continue
            e = e.cast(target, strict=False)
        elif target.is_integer() and src is not None and (src.is_float() or src.is_decimal()):
            e = e.cast(pl.Float64).cast(target, strict=False)  # truncation toward zero
        else:
            e = e.cast(target, strict=False)
        exprs.append(e.alias(c))
    return lf.with_columns(exprs)


# ─────────────────────────────────────────────────────────────────────────────
# Filtering rows
# ─────────────────────────────────────────────────────────────────────────────
# the text a cast to bool reads, on every engine (DuckDB's own rule, case aside)
TEXT_TRUE = ["true", "t", "1", "yes", "y"]
TEXT_FALSE = ["false", "f", "0", "no", "n"]


def _coerce_value(value: Any, dtype: pl.DataType | None) -> Any:
    """Fits the filter's value to the column's type.

    The form always sends ISO strings ("2024-01-15", "2024-01-15T10:30:00"): when the
    column is temporal they are converted to NATIVE dates/datetimes, because Polars
    refuses to compare a date with a string directly.
    """
    if not isinstance(value, str) or dtype is None or not dtype.is_temporal():
        return value
    from datetime import date, datetime, time

    try:
        if dtype == pl.Date:
            return date.fromisoformat(value)
        if isinstance(dtype, pl.Datetime):
            return datetime.fromisoformat(value)
        if dtype == pl.Time:
            return time.fromisoformat(value)
    except ValueError:
        raise temporal_value_error(value)
    return value


def temporal_value_error(value: Any) -> EngineError:
    return EngineError(
        f"value '{value}' is not valid for a date or time column: "
        f"use the ISO format (e.g. 2024-01-15 or 2024-01-15T10:30:00)"
    )


# operators whose value is text whatever the column (or that have none)
_TEXT_OPERATORS = {"contains", "starts_with", "ends_with", "is_null", "is_not_null"}


def non_iso_values(operator: str, value: Any) -> list[str]:
    """The values of a filter that are not an ISO date, date-time or time. The SQL engines
    compare a date column with the text as it is, and a value like «15/01/2024» gave the
    database's own error, the query in it: they look at the column's type only then
    (with ISO values, nothing to look up) and raise temporal_value_error, as Polars does."""
    if operator in _TEXT_OPERATORS:
        return []
    from datetime import datetime, time

    out = []
    for v in value if isinstance(value, list) else [value]:
        if not isinstance(v, str):
            continue
        try:
            datetime.fromisoformat(v)
        except ValueError:
            try:
                time.fromisoformat(v)
            except ValueError:
                out.append(v)
    return out


def _predicate(column: str, operator: str, value: Any, dtype: pl.DataType | None = None) -> pl.Expr:
    col = pl.col(column)
    match operator:
        case "eq" | "==":
            return col == _coerce_value(value, dtype)
        case "ne" | "!=":
            return col != _coerce_value(value, dtype)
        case "gt" | ">":
            return col > _coerce_value(value, dtype)
        case "ge" | ">=":
            return col >= _coerce_value(value, dtype)
        case "lt" | "<":
            return col < _coerce_value(value, dtype)
        case "le" | "<=":
            return col <= _coerce_value(value, dtype)
        case "in":
            return col.is_in([_coerce_value(v, dtype) for v in value])
        case "not_in":
            return ~col.is_in([_coerce_value(v, dtype) for v in value])
        case "between":
            # explicit pl.lit: is_between reads bare strings as column NAMES, not
            # as values
            lo, hi = (_coerce_value(v, dtype) for v in (value[0], value[1]))
            return col.is_between(pl.lit(lo), pl.lit(hi))
        case "contains":
            # the text as written: as a regular expression «Speck+Brie» or «1.5» matched
            # other rows (or none), and «Vino (» was an error (DuckDB and ClickHouse: literal)
            return col.cast(pl.String).str.contains(str(value), literal=True)
        case "starts_with":
            return col.cast(pl.String).str.starts_with(str(value))
        case "ends_with":
            return col.cast(pl.String).str.ends_with(str(value))
        case "is_null":
            return col.is_null()
        case "is_not_null":
            return col.is_not_null()
        case _:
            raise EngineError(f"unsupported filter operator: '{operator}'")


def check_filter_value(operator: str, value: Any) -> None:
    """Shared by every engine: a filter whose operator needs a value but has none is
    refused. Compared with NULL it kept zero rows without a word, and a run then
    published an empty datasource."""
    if operator in ("is_null", "is_not_null"):
        return
    if value is None:
        raise EngineError(f"filter: choose a value for the operator '{operator}'")
    if operator in ("in", "not_in") and value == []:
        raise EngineError(f"filter: choose at least one value for '{operator}'")
    if operator == "between" and (not isinstance(value, list) or len(value) != 2):
        raise EngineError("filter: 'between' needs two values, from and to")


@register("filter")
def op_filter(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"column": "eta", "operator": "gt", "value": 18}
    #         {"column": "data", "operator": "between", "value": ["2024-01-01", "2024-12-31"]}
    column = _require(params, "column")
    operator = _require(params, "operator")
    value = params.get("value")
    check_filter_value(operator, value)
    # the dtype converts the ISO strings into dates/datetimes (schema only, no data)
    dtype = lf.collect_schema().get(column)
    return lf.filter(_predicate(column, operator, value, dtype))


# ─────────────────────────────────────────────────────────────────────────────
# Ordinamento / limit / deduplica / null
# ─────────────────────────────────────────────────────────────────────────────
def sort_keys(params: dict[str, Any], have: Any = None) -> list[tuple[str, bool]]:
    """(column, descending) of a sort, shared by every engine and the dbt export.
    `descending`: a list, one per column (the editor's sort on several columns), or one
    yes/no for all (flows saved before); a list shorter than the columns is ascending
    for the rest. `ignore_missing` (set ONLY by the gateway's publish, never by a person):
    the columns no longer in the chain (`have`) are left out — a sort key declared on the
    Output and since renamed or removed must not fail every run, and the checklist shows
    only the columns that exist. A sort asked for explicitly stays strict."""
    by = params.get("by")
    if by in (None, "", []):
        raise EngineError("required parameter missing: 'by'")
    by = by if isinstance(by, list) else [by]
    desc = params.get("descending", False)
    dirs = [bool(x) for x in desc] if isinstance(desc, list) else [bool(desc)] * len(by)
    dirs += [False] * (len(by) - len(dirs))
    keys = list(zip(by, dirs))
    if params.get("ignore_missing") and have is not None:
        keys = [(c, d) for c, d in keys if c in set(have)]
    return keys


@register("sort")
def op_sort(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    keys = sort_keys(params, lf.collect_schema().names() if params.get("ignore_missing") else None)
    if not keys:
        return lf
    # CROSS-ENGINE STANDARD: NULLs ALWAYS go last (asc and desc), so a "top N" (sort
    # desc + limit) is never made of NULLs. DuckDB/ClickHouse use an explicit NULLS
    # LAST. One direction per column.
    return lf.sort([c for c, _ in keys], descending=[d for _, d in keys], nulls_last=True)


@register("limit")
def op_limit(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    return lf.limit(_require(params, "n"))


SAMPLE_BUCKETS = 10_000


def sample_threshold(params: dict[str, Any]) -> tuple[int, int]:
    """(threshold over SAMPLE_BUCKETS, seed) from `fraction` (0..1] and `seed`."""
    try:
        fraction = float(_require(params, "fraction"))
    except (TypeError, ValueError):
        raise EngineError("sample: 'fraction' must be a number between 0 and 1")
    if not 0 < fraction <= 1:
        raise EngineError("sample: 'fraction' must be a number between 0 and 1")
    seed = int(params.get("seed") or 42)
    return max(1, int(round(fraction * SAMPLE_BUCKETS))), seed


@register("sample")
def op_sample(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    """A RANDOM but DETERMINISTIC sample of a fraction of the rows: the choice depends on
    the hash of the row's content (+ seed), not on a generator → the same rows at every
    preview, streaming-friendly (no row index, no shuffle). Used by the DEVELOPMENT
    sampling of the source nodes; never injected in production (see the gateway's
    flow_resolver)."""
    threshold, seed = sample_threshold(params)
    return lf.filter(pl.struct(pl.all()).hash(seed) % SAMPLE_BUCKETS < threshold)


@register("unique")
def op_unique(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # the first row of each group, in the data's order: «any» kept a different one from
    # run to run (and from DuckDB's)
    return lf.unique(subset=params.get("subset"), keep=params.get("keep", "first"), maintain_order=True)


@register("fill_null")
def op_fill_null(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # either {"value": 0} on all, or {"columns": {"col": 0, "other": "n/a"}}
    if "columns" in params:
        cols: dict[str, Any] = params["columns"]
        return lf.with_columns([pl.col(c).fill_null(v) for c, v in cols.items()])
    return lf.fill_null(_require(params, "value"))


@register("drop_nulls")
def op_drop_nulls(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    return lf.drop_nulls(subset=params.get("subset"))


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────
# The CROSS-ENGINE STANDARD of the aggregates = SQL semantics (what runs in
# production on DuckDB/ClickHouse): NULLs are ignored; sum of a group of ONLY NULLs →
# NULL (Polars would give 0); n_unique does NOT count NULL (Polars does); sample
# std/var (n-1). The pivot too, which uses this table.
_AGG: dict[str, Callable[[str], pl.Expr]] = {
    "sum": lambda c: pl.when(pl.col(c).count() > 0).then(pl.col(c).sum()).otherwise(None),
    "mean": lambda c: pl.col(c).mean(),
    "avg": lambda c: pl.col(c).mean(),
    "min": lambda c: pl.col(c).min(),
    "max": lambda c: pl.col(c).max(),
    "count": lambda c: pl.col(c).count(),
    "median": lambda c: pl.col(c).median(),
    "std": lambda c: pl.col(c).std(),
    "var": lambda c: pl.col(c).var(),
    "first": lambda c: pl.col(c).first(),
    "last": lambda c: pl.col(c).last(),
    "n_unique": lambda c: pl.col(c).drop_nulls().n_unique(),
}


@register("group_by")
def op_group_by(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"by": ["paese"], "aggregations": [{"column": "vendite", "func": "sum", "alias": "tot"}]}
    by = _require(params, "by")
    aggs: list[dict[str, Any]] = _require(params, "aggregations")
    exprs: list[pl.Expr] = []
    for a in aggs:
        col = _require(a, "column")
        func = _require(a, "func")
        if func not in _AGG:
            raise EngineError(f"group_by: unsupported aggregation function '{func}'")
        alias = a.get("alias") or f"{col}_{func}"
        exprs.append(_AGG[func](col).alias(alias))
    return lf.group_by(by).agg(exprs)


# ─────────────────────────────────────────────────────────────────────────────
# Reshape: pivot (rows → columns) and unpivot (columns → rows)
# ─────────────────────────────────────────────────────────────────────────────
# every distinct value of `on` becomes a new column: beyond this it is almost surely
# the wrong column, not a reshape anyone wants
MAX_PIVOT_COLUMNS = 500


# ── The CROSS-ENGINE standard of the pivot (Polars, DuckDB, chDB, ClickHouse) ─
# With a development engine ≠ the production engine the same flow must make THE SAME
# columns everywhere (the nodes downstream refer to them by name). Rules:
# - column name = the value as text (`pivot_label`: null→"null", bool→true/false,
#   ISO dates); several `on` columns → labels joined by "_" and ONLY the combinations
#   present in the data (no Cartesian product);
# - SQL semantics: a missing group (index × on) → NULL; present but with all values
#   NULL → NULL (sum too); `count`/`n_unique` → 0 and type Int64.
PIVOT_LABEL_SEP = "_"


def pivot_label(v: Any) -> str:
    """The column label of a pivot value, the same in every engine."""
    import datetime as _dt

    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, _dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, _dt.date):
        return v.isoformat()
    return str(v)


@register("pivot")
def op_pivot(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"index": ["paese"], "on": "anno", "values": "vendite", "func": "sum"}
    index = _require(params, "index")
    on = _require(params, "on")
    values = _require(params, "values")
    func = params.get("func") or "sum"
    if isinstance(index, str):
        index = [index]
    on = on if isinstance(on, list) else [on]  # several columns = combinations of the values
    if not index:
        raise EngineError("pivot: choose at least one index column (the row keys)")
    if not on:
        raise EngineError("pivot: choose at least one column to spread into new columns")
    if func not in _AGG:
        raise EngineError(f"pivot: unsupported aggregation function '{func}'")

    # 1) the bulk of the work stays lazy/STREAMING: the aggregation shrinks the dataset
    #    to (index groups × combinations of `on`) rows — small by construction,
    #    whatever the size of the input
    agg_expr = _AGG[func](values)
    if func == "sum":
        # semantica SQL (come DuckDB/ClickHouse): sum di soli NULL → NULL, non 0
        agg_expr = pl.when(pl.col(values).count() > 0).then(agg_expr).otherwise(None)
    elif func in ("count", "n_unique"):
        agg_expr = agg_expr.cast(pl.Int64)
    aggregated = (
        lf.group_by(index + on).agg(agg_expr.alias(values)).collect(engine="streaming")
    )

    n_cols = aggregated.select(on).unique().height  # distinct combinations = new columns
    if n_cols > MAX_PIVOT_COLUMNS:
        raise EngineError(
            f"pivot: the chosen columns have {n_cols} distinct combinations, that is {n_cols} "
            f"new columns (maximum {MAX_PIVOT_COLUMNS}). Are these the right columns?"
        )

    # 2) the reshape proper is eager (pivot does not exist lazily) but works on the
    #    aggregated result; "first" is a no-op: the (index, on) pairs are unique after
    #    the group_by. The new columns take the cross-engine label (see
    #    `pivot_label`) computed as the key column.
    key = "__pivot_on__"
    label = pl.concat_str(
        [pl.col(c).cast(pl.Utf8).fill_null("null") for c in on], separator=PIVOT_LABEL_SEP
    ).alias(key)
    aggregated = aggregated.with_columns(label).drop(on)
    out = aggregated.pivot(key, index=index, values=values, aggregate_function="first", sort_columns=True)
    if func in ("count", "n_unique"):  # a missing group = 0 rows, not "unknown"
        out = out.with_columns(pl.exclude(index).fill_null(0))
    return out.lazy()


@register("unpivot")
def op_unpivot(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"index": ["id"], "on": ["jan", "feb"] (empty = all the others),
    #          "variable_name": "month", "value_name": "value"}
    index = params.get("index") or None
    on = params.get("on") or None
    return lf.unpivot(
        on=on,
        index=index,
        variable_name=params.get("variable_name") or "variable",
        value_name=params.get("value_name") or "value",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Join (reads a second source through the context)
# ─────────────────────────────────────────────────────────────────────────────
def _build_right(right_ref: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    """
    Builds the LazyFrame of the join's right side.

    Two formats:
    - sub-flow: {"source": {bucket, key}, "operations": [{type, params}, ...]}
      → a scan of the source + the chain applied (a transformed branch).
    - legacy:   {"bucket": ..., "key": ...}
      → a plain scan of a parquet.
    """
    if "source" in right_ref:
        rlf = ctx.scan(DataSource(**right_ref["source"]))
        for op in right_ref.get("operations", []):
            fn = get_operation(op["type"])
            rlf = fn(rlf, op.get("params", {}), ctx)
        return rlf
    return ctx.scan(DataSource(**right_ref))


def _cross_join(lf: pl.LazyFrame, right_lf: pl.LazyFrame, ctx: OperationContext) -> pl.LazyFrame:
    """Cross join (the L×R Cartesian product) with a guard against OOM.

    Counts the two sides' rows first and, when the product goes over the cap, REFUSES
    with a clear message instead of blowing up the RAM (the cross join is the no. 1
    cause of freezes). No sampling: the preview shows the TRUE result (or the error),
    never a misleading sample. The count streams: on parquet it is almost free
    (metadata); on the left side it runs the upstream chain once (the insurance's
    price)."""
    cap = ctx.max_cross_join_rows
    if cap and cap > 0:
        left_n = lf.select(pl.len()).collect(engine="streaming").item()
        right_n = right_lf.select(pl.len()).collect(engine="streaming").item()
        if left_n * right_n > cap:
            raise EngineError(
                f"The cross join would produce {left_n * right_n:,} rows "
                f"({left_n:,} × {right_n:,}), over the limit of {cap:,}. "
                "Add a join condition (key) or reduce the sources "
                "(filter/limit) before joining them."
            )
    return lf.join(right_lf, how="cross")


@register("join")
def op_join(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"right": <source or sub-flow>,
    #          "on": ["id"]  (or "left_on"/"right_on"),
    #          "how": "inner"}
    right_ref = _require(params, "right")
    right_lf = _build_right(right_ref, ctx)
    how = params.get("how", "inner")

    # the cross join uses no key columns — and can explode: guarded separately
    if how == "cross":
        return _cross_join(lf, right_lf, ctx)

    # The CROSS-ENGINE STANDARD of the join's columns (as SQL USING / ON):
    # - `on` (same names): the key is ONE column, filled in the right-only rows of
    #   full/right joins too (coalesced);
    # - `left_on`/`right_on` (different names): BOTH key columns stay;
    # - a non-key column with the same name on the right → suffix `_right`.
    if how in ("semi", "anti"):
        if "on" in params:
            return lf.join(right_lf, on=params["on"], how=how)
        return lf.join(right_lf, left_on=_require(params, "left_on"), right_on=_require(params, "right_on"), how=how)
    left_cols = lf.collect_schema().names()
    if "on" in params:
        out = lf.join(right_lf, on=params["on"], how=how, coalesce=True)
    else:
        left_on = _require(params, "left_on")
        right_on = _require(params, "right_on")
        out = lf.join(right_lf, left_on=left_on, right_on=right_on, how=how, coalesce=False)
    # column order = the left's (in its order) + the right's new ones, on every kind of
    # join (Polars' right join would move the key after the left's columns)
    out_cols = out.collect_schema().names()
    left_set = set(left_cols)
    return out.select([c for c in left_cols if c in out_cols] + [c for c in out_cols if c not in left_set])


# ─────────────────────────────────────────────────────────────────────────────
# Union: appends the rows of another branch
# ─────────────────────────────────────────────────────────────────────────────
@register("union")
def op_union(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    """
    params: {"right": <source or sub-flow, as for the join>, "strategy": "relaxed"}

    Appends the right branch's rows under the chain's. "relaxed" (the default) aligns
    the columns by name: the ones missing on a side become null, the types are
    reconciled to the supertype. "strict" requires identical schemas. A union of N
    sources = union nodes in a chain. All lazy.
    """
    right_ref = _require(params, "right")
    right_lf = _build_right(right_ref, ctx)
    strategy = params.get("strategy") or "relaxed"
    if strategy not in ("relaxed", "strict"):
        raise EngineError(f"union: unsupported strategy: '{strategy}' (use relaxed or strict)")
    if strategy == "relaxed":
        return pl.concat([lf, right_lf], how="diagonal_relaxed")
    left_s, right_s = lf.collect_schema(), right_lf.collect_schema()
    check_strict_union([(n, str(t)) for n, t in left_s.items()], [(n, str(t)) for n, t in right_s.items()])
    # by name, in the order of the chain (the right one may list them in another)
    return pl.concat([lf, right_lf.select(list(left_s.names()))], how="vertical")


def check_strict_union(left: list[tuple[str, str]], right: list[tuple[str, str]]) -> None:
    """A strict union takes two inputs with the same columns, of the same types, in any
    order, and puts them together by name. Shared by every engine: DuckDB and ClickHouse
    put them together by position (two columns in another order were swapped, the names
    of the products in the ids, without a word), Polars stopped with its plan."""
    lnames, rnames = [n for n, _ in left], [n for n, _ in right]
    missing = [n for n in lnames if n not in rnames]
    extra = [n for n in rnames if n not in lnames]
    if missing or extra:
        said = "; ".join(x for x in (f"missing on the right: {', '.join(missing)}" if missing else "",
                                     f"only on the right: {', '.join(extra)}" if extra else "") if x)
        raise EngineError(f"union (identical schemas): the two inputs have different columns ({said}). "
                          "Choose «Align by name» to fill what is missing with empty values")
    rtypes = dict(right)
    differ = [f"{n} ({t} / {rtypes[n]})" for n, t in left if rtypes[n] != t]
    if differ:
        raise EngineError(f"union (identical schemas): these columns have different types: {', '.join(differ)}. "
                          "Convert them first, or choose «Align by name»")


# ─────────────────────────────────────────────────────────────────────────────
# Computed columns: SQL expressions (Polars dialect) → lazy expressions
# ─────────────────────────────────────────────────────────────────────────────
@register("compute")
def op_compute(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    """
    params: {"columns": [{"name": "margin", "expr": "price - cost"}, ...]}

    The expressions are SQL in Polars' dialect: arithmetic, CASE WHEN, string and date
    functions, window functions with OVER (PARTITION BY / ORDER BY → running semantics
    by rows). `pl.sql_expr` parses ONLY expressions: no statements, no access to
    tables. The columns apply IN SEQUENCE: an expression may use those defined before;
    an existing name overwrites the column. Everything stays lazy.
    """
    columns = _require(params, "columns")
    if not isinstance(columns, list) or not columns:
        raise EngineError("compute: define at least one calculated column")
    for c in columns:
        name = str(c.get("name") or "").strip()
        expr_s = str(c.get("expr") or "").strip()
        if not name:
            raise EngineError("compute: every calculated column needs a name")
        if not expr_s:
            raise EngineError(f"compute: the expression of '{name}' is empty")
        try:
            expr = pl.sql_expr(expr_s)
        except Exception as e:
            raise EngineError(
                f"compute: the expression of '{name}' is not valid: {e}"
            ) from e
        lf = lf.with_columns(expr.alias(name))
    return lf


# ─────────────────────────────────────────────────────────────────────────────
# Execute SQL: a SQL query (Polars dialect) on the incoming frame
# ─────────────────────────────────────────────────────────────────────────────
# Table functions that read FILES from the worker's disk: Polars SQL supports them,
# but here they are FORBIDDEN — a query could read secrets or other people's parquet
# (`SELECT * FROM read_csv('/app/.../secrets.toml')`). The only table available is
# the node's input.
_SQL_FILE_FUNCS = re.compile(r"\b(?:read|scan)_(?:csv|parquet|ipc|ndjson|json)\s*\(", re.IGNORECASE)


@register("sql")
def op_sql(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    """
    params: {"query": "SELECT ... FROM self ..."}

    Runs a SQL query in Polars' dialect on the incoming frame, exposed as the table
    `self` (alias `input`). Polars SQL is an engine CLOSED over the frames in memory —
    no Python, no I/O — and stays LAZY (`SQLContext.execute` returns a LazyFrame →
    streamable and cacheable), in line with the declarative engine. The table functions
    that read files are blocked upstream.
    """
    query = str(_require(params, "query") or "").strip()
    if not query:
        raise EngineError("sql: the query is empty")
    if _SQL_FILE_FUNCS.search(query):
        raise EngineError(
            "sql: file reading functions (read_csv/read_parquet/…) are not "
            "allowed. The only table available is the node's input (`self`)."
        )
    try:
        result = pl.SQLContext(frames={"self": lf, "input": lf}).execute(query)
        result.collect_schema()  # validates syntax and columns AT ONCE (metadata, no data)
        return result
    except EngineError:
        raise
    except Exception as e:
        raise EngineError(f"sql: the query is not valid: {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# Foreach: a loop with placeholders (SSIS container style)
# ─────────────────────────────────────────────────────────────────────────────
# per-level guard: a huge driver or item list would make thousands of chains
MAX_FOREACH_ITERATIONS = 1000
# cumulative guard over a run's WHOLE chain: nested foreach loops multiply
# (1000^depth), so the per-level limit alone is not enough. It counts all the
# iterations built (outer × inner) and stops the explosion.
MAX_FOREACH_TOTAL = 100_000

# tolerant: spaces around the key ("{{ ProductKey }}") and keys with spaces or
# punctuation in the name ("{{Customer ID}}" — it happens in column names)
_PLACEHOLDER = re.compile(r"\{\{\s*(.+?)\s*\}\}")


def _reject_dynamic_source_keys(obj: Any) -> None:
    """Forbids the `{{...}}` placeholders in the fields that ADDRESS the storage
    (`key`/`bucket`). A source whose path is resolved from the rows' DATA would escape
    the gateway's static authorization (which scans the payload only): one could read
    another project's parquet by putting its key in a cell of the driver. The sources'
    paths must come from the flow's graph (concrete and authorized), never from the
    data.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("key", "bucket") and isinstance(v, str) and _PLACEHOLDER.search(v):
                raise EngineError(
                    "placeholders are not allowed in source paths (key/bucket): "
                    "a source cannot be chosen by the data of the iteration"
                )
            _reject_dynamic_source_keys(v)
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            _reject_dynamic_source_keys(x)


def _substitute(obj: Any, item: dict[str, Any]) -> Any:
    """Replaces the {{key}} placeholders in the body's params, recursively.

    - a string that is ONLY a placeholder ("{{threshold}}") → the original value, its
      type kept (a number stays a number: useful for filter/limit);
    - a placeholder within text ("sales_{{country}}") → a text replacement;
    - dict/list → recursive descent.
    """
    if isinstance(obj, str):
        m = _PLACEHOLDER.fullmatch(obj.strip())
        if m:
            key = m.group(1)
            if key not in item:
                raise EngineError(
                    f"placeholder '{{{{{key}}}}}' not found: available {sorted(item)}"
                )
            return item[key]
        def _repl(mm):
            key = mm.group(1)
            if key not in item:
                raise EngineError(
                    f"placeholder '{{{{{key}}}}}' not found: available {sorted(item)}"
                )
            return str(item[key])
        return _PLACEHOLDER.sub(_repl, obj)
    if isinstance(obj, list):
        return [_substitute(x, item) for x in obj]
    if isinstance(obj, dict):
        # the KEYS may be placeholders too (rename/cast/fill_null:
        # {"{{column}}": ...}); a key must stay a string
        out = {}
        for k, v in obj.items():
            nk = _substitute(k, item) if isinstance(k, str) else k
            out[nk if isinstance(nk, str) else str(nk)] = _substitute(v, item)
        return out
    return obj


@register("foreach")
def op_foreach(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    """
    Runs the body for every iteration and APPENDS the results (concat).

    params:
    - "body": [{type, params}, ...] — the body's operations; the values use the
      {{key}} placeholders;
    - the iterations come from ONE of the two:
      "driver": a sub-flow ({"source": {...}, "operations": [...]}) — every ROW is an
        iteration, the COLUMNS are the placeholders available;
      "items": a static list of dicts [{key: value, ...}, ...];
    - "add_keys_as_columns": true → adds the iteration's keys as literal columns to the
      output (to tell the appended blocks apart).

    The result stays LAZY: N chains + concat, streaming is kept. `diagonal_relaxed`
    tolerates small schema differences between iterations.
    """
    body = _require(params, "body")
    items = params.get("items")
    if not items and "driver" in params:
        driver_df = _build_right(params["driver"], ctx).collect(engine="streaming")
        if driver_df.height > MAX_FOREACH_ITERATIONS:
            raise EngineError(
                f"the driver has {driver_df.height} rows: over the limit of "
                f"{MAX_FOREACH_ITERATIONS} iterations. Reduce the driver (unique/filter)."
            )
        items = driver_df.to_dicts()
    if not items:
        raise EngineError(
            "foreach has no iterations: connect a driver (the top input) "
            "or define 'items' in the parameters"
        )
    # the per-level cap on STATIC items too (the driver branch is capped above already)
    if len(items) > MAX_FOREACH_ITERATIONS:
        raise EngineError(
            f"foreach: {len(items)} iterations, over the limit of {MAX_FOREACH_ITERATIONS}"
        )
    if not body:
        raise EngineError("foreach has an empty body: drag some operations into the container")
    # a source in the body cannot have its path decided by the data (an RBAC bypass)
    _reject_dynamic_source_keys(body)

    # cumulative budget: nested foreach loops are rebuilt at every outer iteration, so
    # outer×inner goes through here and is cut before the OOM.
    # (in a real run ctx is always there; None only in the tests of the pure ops)
    if ctx is not None:
        ctx.foreach_iterations += len(items)
        if ctx.foreach_iterations > MAX_FOREACH_TOTAL:
            raise EngineError(
                f"foreach: too many iterations in total ({ctx.foreach_iterations}) — "
                f"the nesting is too deep (limit {MAX_FOREACH_TOTAL})"
            )

    add_keys = params.get("add_keys_as_columns", False)
    parts: list[pl.LazyFrame] = []
    for idx, item in enumerate(items):
        sub = lf
        try:
            for op in _substitute(body, item):
                fn = get_operation(op["type"])
                sub = fn(sub, op.get("params", {}), ctx)
        except EngineError as e:
            raise EngineError(f"iteration {idx + 1}/{len(items)}: {e}") from e
        if add_keys:
            sub = sub.with_columns([pl.lit(v).alias(str(k)) for k, v in item.items()])
        parts.append(sub)
    return pl.concat(parts, how="diagonal_relaxed")
