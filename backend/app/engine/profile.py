"""A step's column profile, for the preview's header: how many nulls each column holds, how
many distinct values (estimated), its smallest and largest value, a text's shortest and
longest length; and, asked apart for one column, its most frequent values.

Asked after the preview's rows and after their count, never before them, and as cheap as
the step allows (the same order as the row count, app/engine/rowcount.py):

1. remembered: a profile made before for the same step (the step cache's fingerprint: the
   same engine, source and chain) and the same columns is kept on Valkey as long as the
   step cache keeps its copies;
2. read: when the step is a source, or a step the cache holds, followed only by operations
   that keep every row and every value (select, reorder, drop, rename, sort), the nulls of
   every column and the min and max of its numbers and dates are in the parquet footer
   (the row groups' statistics), read with one ranged GET. A text's min and max are never
   taken from there: writers may cut them or leave them out. A cast or a computed column
   changes values, a filter or a limit changes rows: then nothing is read from a footer;
3. computed: ONE aggregate query for every column, over the chain's first SAMPLE_ROWS rows,
   starting from the nearest step the cache holds (`Engine.profile_aggregates`).

Never said as exact when it is not: the distinct values are the engine's own estimate
(Polars `approx_n_unique`, ClickHouse and chDB `uniq`, BigQuery `APPROX_COUNT_DISTINCT`;
DuckDB counts them, see `sql_aggregate`), always shown as one; and past SAMPLE_ROWS rows the query
sees the first SAMPLE_ROWS only, which the answer says (`sampled`), column by column for
what the footer gave over every row instead (`whole`).

The semantics are the same on every engine (docs/engines, the cross-engine standard): a
null is a missing value (an empty text is not one, a NaN is not one); the distinct values
leave the nulls out; texts compare by their bytes and their length is in characters.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import re
from decimal import Decimal
from typing import Any

from app.engine import rowcount

logger = logging.getLogger(__name__)

# Past this many rows the query looks at the first ones only, and says so. Measured on kind
# (2026-11-08, DuckDB): every column of a million rows of a 42-million row table in about a
# second; the HyperLogLog's error does not depend on the rows it sees.
SAMPLE_ROWS = 1_000_000
TOP_K = 5
PROFILE_PREFIX = "dataprep:stepprofile:"
# The operations after which a footer's statistics still describe the step: every row and
# every value kept, only the columns chosen, named or ordered otherwise.
KEEPS_VALUES = frozenset({"select", "reorder", "drop", "rename", "sort"})

_NUMBER = re.compile(r"^(U?Int(8|16|32|64|128)|Float(32|64)|Decimal)\b")
_TIME = re.compile(r"^(Date|Datetime|Time|Duration)\b")


def kind_of(dtype: str) -> str:
    """What a column is profiled for: «number» and «time» (nulls, distinct, min, max),
    «text» (and its lengths), «bool» (nulls, distinct), «other» (nulls only: lists,
    structs, binaries, categoricals)."""
    d = str(dtype or "").strip()
    if _NUMBER.match(d):
        return "number"
    if _TIME.match(d):
        return "time"
    if d in ("String", "Utf8"):
        return "text"
    if d == "Boolean":
        return "bool"
    return "other"


# ── the aggregates a profile asks for ────────────────────────────────────────
# (alias, function, column): «rows» counts every row; «count» a column's values that are
# not null; «distinct» its distinct values, estimated; «min», «max»; «min_len», «max_len» a
# text's length in characters.
Spec = tuple[str, str, "str | None"]


def sql_aggregate(dialect: str, func: str, col: str | None, quote) -> str:
    """One aggregate in a SQL dialect: «duckdb», «clickhouse» (chDB too), «bigquery»."""
    if func == "rows":
        return "count(*)"
    q = quote(col)
    if func == "count":
        return f"count({q})"
    if func == "distinct":
        # DuckDB's approx_count_distinct was measured far off (1,249 for 1,000 values, 8,571
        # for 10,000, 11 for 10: 2026-11-08, DuckDB 1.5) while counting them exactly over the
        # sample's million rows takes under 0.1 s: DuckDB counts them. The others estimate
        # within about 1% (Polars' HyperLogLog, ClickHouse's adaptive `uniq`, exact below
        # 65,536 values).
        return {"duckdb": f"count(DISTINCT {q})", "clickhouse": f"uniq({q})",
                "bigquery": f"APPROX_COUNT_DISTINCT({q})"}[dialect]
    if func in ("min", "max"):
        return f"{func}({q})"
    length = {"duckdb": f"length({q})", "clickhouse": f"lengthUTF8({q})", "bigquery": f"LENGTH({q})"}[dialect]
    return f"{'min' if func == 'min_len' else 'max'}({length})"


def plan(columns: list[dict[str, str]], footer: dict[str, dict] | None) -> list[Spec]:
    """The aggregates for `columns` ({name, dtype}), leaving out what the footer gave."""
    footer = footer or {}
    specs: list[Spec] = [("rows", "rows", None)]
    for j, c in enumerate(columns):
        name, kind = c["name"], kind_of(c.get("dtype", ""))
        got = footer.get(name) or {}
        if got.get("nulls") is None:
            specs.append((f"c{j}_count", "count", name))
        if kind != "other":
            specs.append((f"c{j}_distinct", "distinct", name))
        if kind == "text" or (kind in ("number", "time") and not got.get("range")):
            specs += [(f"c{j}_min", "min", name), (f"c{j}_max", "max", name)]
        if kind == "text":
            specs += [(f"c{j}_min_len", "min_len", name), (f"c{j}_max_len", "max_len", name)]
    return specs


def plain(v: Any) -> Any:
    """A value as the preview's rows carry it to the browser (pydantic's JSON): a decimal as
    its text, a date and a time in ISO, a datetime naive in UTC (the cross-engine standard)."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, _dt.datetime) and v.tzinfo is not None:
        v = v.astimezone(_dt.UTC).replace(tzinfo=None)
    from pydantic_core import to_jsonable_python

    return to_jsonable_python(v)


def assemble(columns: list[dict[str, str]], row: dict[str, Any], footer: dict | None, sample: int,
             total: int | None) -> dict[str, Any]:
    """The answer: the query's row of aggregates and the footer's statistics, column by column."""
    seen = int(row.get("rows") or 0)
    sampled = seen >= sample and (total is None or total > seen)
    fcols = (footer or {}).get("columns") or {}
    out: dict[str, Any] = {}
    for j, c in enumerate(columns):
        name, kind = c["name"], kind_of(c.get("dtype", ""))
        got = fcols.get(name) or {}
        whole: list[str] = []
        if got.get("nulls") is not None:
            nulls, of = int(got["nulls"]), int(footer["rows"])
            whole.append("nulls")
        else:
            nulls, of = seen - int(row.get(f"c{j}_count") or 0), seen
            if not sampled:
                whole.append("nulls")
        col: dict[str, Any] = {"kind": kind, "nulls": nulls, "of": of}
        if kind != "other":
            d = row.get(f"c{j}_distinct")
            col["distinct"] = int(d) if d is not None else None
        if kind in ("number", "time", "text"):
            if got.get("range"):
                col["min"], col["max"] = got["min"], got["max"]
                whole += ["min", "max"]
            else:
                col["min"], col["max"] = plain(row.get(f"c{j}_min")), plain(row.get(f"c{j}_max"))
                if not sampled:
                    whole += ["min", "max"]
        if kind == "text":
            lo, hi = row.get(f"c{j}_min_len"), row.get(f"c{j}_max_len")
            col["min_len"] = int(lo) if lo is not None else None
            col["max_len"] = int(hi) if hi is not None else None
        col["whole"] = whole
        out[name] = col
    return {"rows": seen, "sampled": sampled, "sample_rows": sample, "total": total,
            "footer": bool(footer), "columns": out}


# ── the footer ──────────────────────────────────────────────────────────────
def footer_stats(storage, bucket: str, key: str) -> dict[str, Any] | None:
    """{"rows", "columns": {name: {"nulls", "range", "min", "max"}}} from a parquet's footer:
    the nulls when every row group says them, the min and max when every row group with a
    value says them. Only the flat columns; a statistic that is missing is None, never a
    guess. None when the footer cannot be read."""
    meta = rowcount.parquet_metadata(storage, bucket, key)
    if meta is None:
        return None
    try:
        arrow = meta.schema.to_arrow_schema()
        flat = {f.name for f in arrow if not (hasattr(f.type, "num_fields") and f.type.num_fields)
                and not str(f.type).startswith(("list", "large_list", "struct", "map", "fixed_size_list"))}
        cols: dict[str, dict[str, Any]] = {}
        for j in range(meta.num_columns):
            path = meta.schema.column(j).path
            if path not in flat or path in cols:
                continue
            nulls: int | None = 0
            lo = hi = None
            ranged = True
            for i in range(meta.num_row_groups):
                rg = meta.row_group(i)
                st = rg.column(j).statistics
                if st is None or not st.has_null_count:
                    nulls = None
                    ranged = False
                    break
                nulls += int(st.null_count)
                if st.null_count >= rg.num_rows:
                    continue  # nothing but nulls here: no min and max to have
                if not st.has_min_max:
                    ranged = False
                    continue
                a, b = _stat_value(st.min), _stat_value(st.max)
                if a is _NO or b is _NO:
                    ranged = False
                    continue
                lo = a if lo is None or a < lo else lo
                hi = b if hi is None or b > hi else hi
            cols[path] = {"nulls": nulls, "range": bool(ranged and lo is not None),
                          "min": plain(lo) if ranged else None, "max": plain(hi) if ranged else None}
        return {"rows": int(meta.num_rows), "columns": cols}
    except Exception as e:  # noqa: BLE001 — a footer we cannot read is a query instead
        logger.info("profile: the statistics of %s/%s were not read (%s)", bucket, key, e)
        return None


_NO = object()


def _stat_value(v: Any) -> Any:
    """A statistic as a comparable Python value, or _NO (bytes, an unknown type)."""
    if isinstance(v, (bool, int, float, Decimal, _dt.date, _dt.time, _dt.timedelta)):
        if isinstance(v, float) and math.isnan(v):
            return _NO
        return v
    to_py = getattr(v, "to_pydatetime", None)  # a pandas Timestamp (nanoseconds)
    if to_py is not None:
        return to_py()
    return _NO


def from_footer(engine_name: str, bucket: str, key: str, operations: list[dict[str, Any]],
                columns: list[dict[str, str]], storage, cache) -> dict[str, Any] | None:
    """The footer's statistics for the chain's last step, by its own column names: the
    source's, or a cached step's, when everything after it keeps rows and values (renames
    followed). Only the numbers' and dates' ranges are kept. None when no footer applies."""
    first = len(operations)
    while first > 0 and operations[first - 1].get("type") in KEEPS_VALUES:
        first -= 1
    hashes = rowcount.step_hashes(engine_name, bucket, key, operations) if operations else []
    for k in range(len(operations), first - 1, -1):
        if k == 0:
            stats = footer_stats(storage, bucket, key)
        elif cache is not None and cache.has(hashes[k - 1]):
            stats = footer_stats(storage, cache.bucket, cache.object_key(hashes[k - 1]))
        else:
            continue
        if stats is None:
            continue
        kinds = {c["name"]: kind_of(c.get("dtype", "")) for c in columns}
        out: dict[str, dict] = {}
        for c in columns:
            origin = _origin(c["name"], operations[k:])
            got = stats["columns"].get(origin) if origin is not None else None
            if got is None:
                continue
            ranged = got["range"] and kinds[c["name"]] in ("number", "time")
            out[c["name"]] = {"nulls": got["nulls"], "range": ranged,
                              "min": got["min"] if ranged else None, "max": got["max"] if ranged else None}
        return {"rows": stats["rows"], "columns": out, "step": k}
    return None


def _origin(name: str, tail: list[dict[str, Any]]) -> str | None:
    """The name a column of the last step had before `tail` (its renames read backwards)."""
    for op in reversed(tail):
        if op.get("type") != "rename":
            continue
        mapping = (op.get("params") or {}).get("mapping") or {}
        back = {str(v): str(k) for k, v in mapping.items()}
        if name in back:
            name = back[name]
        elif name in mapping:
            return None  # renamed away: the column of this name came from nowhere
    return name


# ── remembered ──────────────────────────────────────────────────────────────
def profile_key(engine_name: str, bucket: str, key: str, operations: list[dict[str, Any]],
                columns: list[dict[str, str]], column: str | None = None) -> str:
    """Where a step's profile (or one column's most frequent values) is remembered."""
    step = rowcount.count_key(engine_name, bucket, key, operations)[len(rowcount.COUNT_PREFIX):]
    what = json.dumps([[c["name"], c.get("dtype", "")] for c in columns] if column is None else ["top", column, TOP_K],
                      ensure_ascii=False)
    digest = hashlib.sha256(f"{SAMPLE_ROWS}|{what}".encode()).hexdigest()[:24]
    return f"{PROFILE_PREFIX}{step}:{digest}"


def remembered(key: str, client=None) -> dict[str, Any] | None:
    try:
        raw = (client or rowcount._redis()).get(key)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 — Valkey that does not answer: not remembered
        return None


def remember(key: str, doc: dict[str, Any], client=None) -> None:
    try:
        (client or rowcount._redis()).set(key, json.dumps(doc, ensure_ascii=False), ex=rowcount._ttl())
    except Exception:  # noqa: BLE001, S110 — a courtesy: the next time it is made again
        pass


# ── made ────────────────────────────────────────────────────────────────────
def _total(engine_name: str, bucket: str, key: str, operations, storage, cache, client) -> int | None:
    """The step's rows when known without counting (remembered, or read from a footer)."""
    try:
        got = rowcount.known(engine_name, bucket, key, operations, 1, storage, cache, client)
    except Exception:  # noqa: BLE001
        return None
    return got[0] if got is not None and not got[1] else None


def profile(engine, engine_name: str, bucket: str, key: str, operations: list[dict[str, Any]],
            columns: list[dict[str, str]], storage, use_cache: bool = True, client=None) -> dict[str, Any]:
    """The step's profile, made and remembered (see the module's docstring)."""
    from app.engine.base import DataSource

    mem = profile_key(engine_name, bucket, key, operations, columns)
    got = remembered(mem, client)
    if got is not None:
        return {**got, "how": "remembered"}
    cache = getattr(engine, "cache", None) if engine_name != "bigquery" else None
    footer = from_footer(engine_name, bucket, key, operations, columns, storage, cache)
    total = footer["rows"] if footer else _total(engine_name, bucket, key, operations, storage, cache, client)
    specs = plan(columns, (footer or {}).get("columns"))
    row = engine.profile_aggregates(DataSource(bucket=bucket, key=key), operations, specs, SAMPLE_ROWS,
                                    use_cache=use_cache)
    doc = assemble(columns, row, footer, SAMPLE_ROWS, total)
    remember(mem, doc, client)
    return {**doc, "how": "computed"}


def top_values(engine, engine_name: str, bucket: str, key: str, operations: list[dict[str, Any]],
               column: str, use_cache: bool = True, client=None) -> dict[str, Any]:
    """One column's TOP_K most frequent values (nulls left out) with how many rows hold each,
    over the same rows as the profile (the chain's first SAMPLE_ROWS); the most frequent
    first, a tie by the value."""
    from app.engine.base import DataSource

    mem = profile_key(engine_name, bucket, key, operations, [], column)
    got = remembered(mem, client)
    if got is not None:
        return {**got, "how": "remembered"}
    pairs = engine.profile_top(DataSource(bucket=bucket, key=key), operations, column, SAMPLE_ROWS, TOP_K,
                               use_cache=use_cache)
    pairs = sorted(((plain(v), int(n)) for v, n in pairs), key=lambda p: (-p[1], _order(p[0])))[:TOP_K]
    doc = {"column": column, "values": [{"value": v, "count": n} for v, n in pairs], "sample_rows": SAMPLE_ROWS}
    remember(mem, doc, client)
    return {**doc, "how": "computed"}


def _order(v: Any) -> tuple:
    """A tie's order, the same for every type a column holds (numbers, then texts)."""
    if isinstance(v, bool):
        return (0, int(v), "")
    if isinstance(v, (int, float)):
        return (0, v, "")
    return (1, 0, str(v))
