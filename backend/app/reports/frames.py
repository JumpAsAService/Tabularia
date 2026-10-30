"""A report's data in columns, the calculations in DuckDB (docs/specs/reports.md, «Data in
columns»).

A dataset is read as an Arrow table and stays one: a million rows of eight columns are some
70 MB of columns, where as Python's dicts they were over a gigabyte. DuckDB, inside the
worker's process, reads the tables where they are and works out what runs over every row
— a table's filter, its sort keys and group keys, a chart's categories, a calculated field,
the values an aggregate takes — from expressions app/reports/sql.py translates; numpy puts
the rows in order (the orders themselves are decided by Python on the distinct values, so
«é» and «E» sort as the interpreter sorts them). Python sees only what goes on the page:
the detail rows a table shows, one value per group, per category, per KPI.

Sets of rows (`SqlRows`) stand where the interpreter had lists of dicts: the groups of a
level, the categories of a chart, a dataset. Those of one kind form a `Family` and are
aggregated together, one GROUP BY for every group of a level; the evaluator
(app/reports/expressions.py) asks a set for an aggregate, the first row's field, a running
total, and walks the rest of the expression itself — the arithmetic around the aggregates,
the formats, the labels — as before. Sums and averages of decimals are folded by Python
over the values DuckDB computed, in the rows' order: Python's `sum` compensates its
rounding in that order, and a total is then the interpreter's to the last bit.

Whatever cannot be done here with the interpreter's result for certain raises `Fallback`,
and the model (app/reports/model.py) works that item out the interpreter's way, over the
dataset's rows as Python values: a different engine, never a different result.
"""
from __future__ import annotations

import logging
import math
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import cmp_to_key
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from app.reports import definition as rd
from app.reports import expressions as ex
from app.reports import formats
from app.reports import sql as rsql
from app.reports.errors import ReportError

logger = logging.getLogger(__name__)

ORD = "__tabularia_ord"  # a dataset's rows in the order they were read
_INTERNAL = "__tabularia_"
INT, FLOAT, NUM, TEXT, BOOL, DATE, DATETIME, NULL = (rsql.INT, rsql.FLOAT, rsql.NUM, rsql.TEXT, rsql.BOOL,
                                                       rsql.DATE, rsql.DATETIME, rsql.NULL)
_ARROW = {INT: pa.int64(), FLOAT: pa.float64(), NUM: pa.float64(), TEXT: pa.string(), BOOL: pa.bool_(),
          DATE: pa.date32(), DATETIME: pa.timestamp("us"), NULL: pa.null()}

# the switch the tests turn to compare the two ways (tests/reports_both_ways.py); None = in columns
COLUMNS: bool | None = None


def enabled() -> bool:
    return COLUMNS is not False


class Fallback(Exception):
    """This item cannot be worked out in DuckDB with the interpreter's result for certain."""


# ── plain values (what the interpreter works on) ─────────────────────────────
def plain(value: Any, tz: ZoneInfo) -> Any:
    """A value as the expressions see it: decimals as floats, date-times in the report's
    time zone and without it (one without a zone is a UTC instant, the engines' standard:
    app/engine/temporal.py), no infinities."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(tz).replace(tzinfo=None)
    if isinstance(value, date):
        return value
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None
    return str(value)


# ── a dataset in columns ─────────────────────────────────────────────────────
class Table:
    """A dataset's columns as Arrow, each of one kind (sql.py's), a column that holds ints
    and floats together with the flags of its ints beside it, and ORD."""

    def __init__(self, columns: dict[str, pa.Array], kinds: dict[str, str], flags: dict[str, pa.Array]):
        self.kinds = dict(kinds)
        self._columns = dict(columns)
        self._flags = dict(flags)
        self.n = len(next(iter(columns.values()))) if columns else 0
        self._rows: list[dict] | None = None
        self._arrow: pa.Table | None = None
        self.version = 0

    @property
    def names(self) -> list[str]:
        return list(self._columns)

    def column_name(self, name: str) -> str:
        """A column's name in DuckDB, by its place: DuckDB's names ignore case («Month» beside
        «month» is one column there), and a subquery takes one of its own for an outer one."""
        return f"{_INTERNAL}c{list(self._columns).index(name)}"

    def flag_name(self, name: str) -> str:
        return f"{_INTERNAL}int_{list(self._columns).index(name)}"

    def arrow(self) -> pa.Table:
        if self._arrow is None:
            self._arrow = self._make_arrow()
        return self._arrow

    def _make_arrow(self) -> pa.Table:
        names, arrays = [], []
        for name, values in self._columns.items():
            names.append(self.column_name(name))
            arrays.append(values)
        for name, flag in self._flags.items():
            names.append(self.flag_name(name))
            arrays.append(flag)
        names.append(ORD)
        arrays.append(pa.array(np.arange(self.n, dtype=np.int64)))
        return pa.Table.from_arrays(arrays, names=names)

    def add(self, name: str, values: pa.Array, kind: str, flag: pa.Array | None = None) -> None:
        """A calculated field: a new column, or one with that name replaced."""
        self._columns[name] = values
        self.kinds[name] = kind
        self._flags.pop(name, None)
        if flag is not None:
            self._flags[name] = flag
        self._rows = None
        self._arrow = None
        self.version += 1

    def column(self, name: str) -> tuple[pa.Array, pa.Array | None]:
        return self._columns[name], self._flags.get(name)

    def rows(self) -> list[dict]:
        """The rows as Python's dicts, as the interpreter reads them (an item it works out)."""
        if self._rows is None:
            self._rows = rows_of(list(self._columns.items()), self._flags)
        return self._rows

    def rows_at(self, idx: np.ndarray) -> list[dict]:
        """The rows at these positions, in this order."""
        if len(idx) == 0:
            return []
        take = pa.array(idx, pa.int64())
        cols = [(n, v.take(take)) for n, v in self._columns.items()]
        flags = {n: f.take(take) for n, f in self._flags.items()}
        return rows_of(cols, flags)


def to_python(values: pa.Array, flag: pa.Array | None) -> list:
    """An Arrow column as Python's values; the floats of a NUM column that were ints, ints;
    a sum of ints (DuckDB's HUGEINT, a decimal of no scale in Arrow) an int."""
    try:
        out = values.to_pylist()
    except (ValueError, OverflowError) as e:  # a date Python cannot hold
        raise Fallback(f"a value Python cannot hold: {e}") from None
    if pa.types.is_decimal(values.type):
        return [None if v is None else int(v) if values.type.scale == 0 else float(v) for v in out]
    if flag is not None:
        ints = np.flatnonzero(flag.fill_null(False).to_numpy(zero_copy_only=False))
        for i in ints:
            v = out[i]
            if v is not None:
                out[i] = int(v)
    return out


def rows_of(columns: list[tuple[str, pa.Array]], flags: dict[str, pa.Array]) -> list[dict]:
    if not columns:
        return []
    names = [n for n, _ in columns]
    lists = [to_python(v, flags.get(n)) for n, v in columns]
    return [dict(zip(names, values)) for values in zip(*lists)]


def _classify(values: list) -> tuple[str, pa.Array, pa.Array | None] | None:
    """A column of Python's plain values as Arrow, or None when its values are of kinds no
    column holds together (a text and a number): the interpreter keeps that dataset."""
    seen = set()
    for v in values:
        if v is not None:
            seen.add(rsql.kind_of_value(v))
    if not seen:
        return NULL, pa.nulls(len(values)), None
    if seen <= {INT, FLOAT}:
        if seen == {INT}:
            try:
                return INT, pa.array(values, pa.int64()), None
            except (OverflowError, pa.ArrowInvalid):
                return None
        if seen == {FLOAT}:
            return FLOAT, pa.array(values, pa.float64()), None
        if any(isinstance(v, int) and not isinstance(v, bool) and abs(v) > 2**53 for v in values):
            return None
        flags = pa.array([None if v is None else isinstance(v, int) for v in values], pa.bool_())
        return NUM, pa.array([None if v is None else float(v) for v in values], pa.float64()), flags
    if len(seen) != 1:
        return None
    kind = seen.pop()
    if kind == "other" or (kind == DATETIME and any(v is not None and v.tzinfo for v in values)):
        return None
    try:
        return kind, pa.array(values, _ARROW[kind]), None
    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):
        return None


def from_rows(rows: list[dict], names: Iterable[str] = ()) -> Table | None:
    """A dataset given as Python's plain rows (fixed data, the tests), in columns; None
    when a column mixes kinds."""
    order: dict[str, None] = dict.fromkeys(names)
    for r in rows:
        for k in r:
            if k not in order:
                order[k] = None
    if any(n.startswith(_INTERNAL) for n in order):
        return None
    columns, kinds, flags = {}, {}, {}
    for name in order:
        typed = _classify([r.get(name) for r in rows])
        if typed is None:
            return None
        kinds[name], columns[name], flag = typed
        if flag is not None:
            flags[name] = flag
    if not columns:
        columns["__tabularia_none"] = pa.nulls(len(rows))
        kinds["__tabularia_none"] = NULL
    return Table(columns, kinds, flags)


def _local_us(utc_us: np.ndarray, valid: np.ndarray, tz: ZoneInfo) -> np.ndarray:
    """UTC instants in microseconds as wall-clock microseconds of `tz`, with the offsets of
    Python's zoneinfo (as datetime.astimezone): worked out once per day of the data, and at
    the instant it changes on a day that changes it."""
    if not valid.any():
        return utc_us
    day_us = 86_400_000_000
    days = np.floor_divide(utc_us, day_us)
    uniq = np.unique(days[valid])
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def offset(us: int) -> int:
        at = epoch + timedelta(microseconds=int(us))
        return int(at.astimezone(tz).utcoffset() // timedelta(microseconds=1))

    before = np.empty(len(uniq), np.int64)
    after = np.empty(len(uniq), np.int64)
    change = np.empty(len(uniq), np.int64)
    for i, d in enumerate(uniq.tolist()):
        start, end = d * day_us, (d + 1) * day_us - 1
        a, b = offset(start), offset(end)
        before[i], after[i], change[i] = a, b, end + 1
        if a != b:  # the first microsecond of the day with the new offset
            lo, hi = start, end
            while lo < hi:
                mid = (lo + hi) // 2
                if offset(mid) == b:
                    hi = mid
                else:
                    lo = mid + 1
            change[i] = lo
    pos = np.searchsorted(uniq, days)
    pos = np.clip(pos, 0, len(uniq) - 1)
    off = np.where(utc_us >= change[pos], after[pos], before[pos])
    return np.where(valid, utc_us + off, utc_us)


def _datetimes(arr: pa.Array, tz: ZoneInfo) -> pa.Array:
    t = arr.type
    unit = {"s": 1_000_000, "ms": 1_000, "us": 1, "ns": None}[t.unit]
    raw = arr.cast(pa.int64()).to_numpy(zero_copy_only=False) if arr.null_count == 0 else \
        arr.cast(pa.int64()).fill_null(0).to_numpy(zero_copy_only=False)
    us = np.floor_divide(raw, 1000) if unit is None else raw * unit
    valid = ~np.asarray(arr.is_null().to_numpy(zero_copy_only=False), dtype=bool)
    if str(tz) != "UTC":
        us = _local_us(us, valid, tz)
    return pa.array(us, pa.timestamp("us"), mask=~valid)


def _texts(arr: pa.Array) -> pa.Array:
    """A column of texts as it is, or each distinct text once and the rows by number, when
    they repeat (a region, a category: a tenth of the memory, and DuckDB reads either)."""
    if pa.types.is_string_view(arr.type):
        arr = arr.cast(pa.large_string())
    if len(arr) < 1000:
        return arr
    enc = pc.dictionary_encode(arr)
    return enc if len(enc.dictionary) * 4 < len(arr) else arr


def from_arrow(table: pa.Table, tz: ZoneInfo) -> Table | None:
    """A dataset read as Arrow (a query's DataFrame, a catalog chain's), in columns of the
    interpreter's values: plain() on whole columns."""
    columns, kinds, flags = {}, {}, {}
    for name, col in zip(table.column_names, table.columns):
        if name.startswith(_INTERNAL):
            return None
        arr = col.combine_chunks() if isinstance(col, pa.ChunkedArray) else col
        t = arr.type
        if pa.types.is_dictionary(t):
            arr = arr.cast(t.value_type)
            t = arr.type
        if pa.types.is_integer(t):
            try:
                arr, kind = arr.cast(pa.int64()), INT
            except (pa.ArrowInvalid, OverflowError):
                typed = _classify(arr.to_pylist())  # a uint64 past 2**63: Python's ints
                if typed is None:
                    return None
                kind, arr, _ = typed
        elif pa.types.is_floating(t):
            arr = arr.cast(pa.float64())
            finite = pc.is_finite(arr)
            arr = pc.if_else(finite, arr, pa.scalar(None, pa.float64()))
            kind = FLOAT
        elif pa.types.is_decimal(t):
            # float(Decimal) parses its exact text: so does this, correctly rounded
            arr, kind = arr.cast(pa.string()).cast(pa.float64()), FLOAT
        elif pa.types.is_boolean(t):
            kind = BOOL
        elif pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_string_view(t):
            arr, kind = _texts(arr), TEXT
        elif pa.types.is_date(t):
            arr, kind = arr.cast(pa.date32()), DATE
        elif pa.types.is_timestamp(t):
            arr, kind = _datetimes(arr, tz), DATETIME
        elif pa.types.is_null(t):
            kind = NULL
        elif pa.types.is_binary(t) or pa.types.is_large_binary(t) or pa.types.is_fixed_size_binary(t) \
                or pa.types.is_binary_view(t):
            arr, kind = pa.nulls(len(arr)), NULL
        else:  # times, durations, lists, structs: what plain() makes of each value
            typed = _classify([plain(v, tz) for v in arr.to_pylist()])
            if typed is None:
                return None
            kind, arr, flag = typed
            if flag is not None:
                flags[name] = flag
        columns[name], kinds[name] = arr, kind
    if not columns:
        return None
    return Table(columns, kinds, flags)


def from_frame(df: Any, tz: ZoneInfo) -> Table | None:
    """A Polars DataFrame (a query on a connection, a catalog chain) in columns; None when
    one of its columns is of a kind the columns do not hold (the interpreter reads it, and
    finds the frame whole). The frame is emptied as it is read, column by column: a text is
    copied into Arrow's layout, and the frame's own goes at once, so the two are never whole
    together."""
    if df.width == 0 or any(t == pl_object() for t in df.dtypes):
        return None
    names, arrays = [], []
    while df.width:
        name = df.columns[0]
        arrays.append(df.drop_in_place(name).to_arrow())
        names.append(name)
    arrow = pa.Table.from_arrays(arrays, names=names)
    table = from_arrow(arrow, tz)
    if table is None:
        import polars as pl

        for column in pl.from_arrow(arrow).get_columns():
            df.insert_column(df.width, column)
    return table


def pl_object() -> Any:
    import polars as pl

    return pl.Object


# ── the order of values, as the interpreter has it ───────────────────────────
def sort_key(v: Any) -> tuple:
    """model.sort_key: a key that orders any value, NULL marked so it can go last."""
    if v is None:
        return (1, 0, 0)
    if isinstance(v, bool):
        return (0, 0, int(v))
    if isinstance(v, (int, float)):
        return (0, 1, v)
    if isinstance(v, datetime):
        return (0, 2, v)
    if isinstance(v, date):
        return (0, 2, datetime(v.year, v.month, v.day))
    if isinstance(v, str):
        return (0, 3, v.casefold(), v)
    return (0, 4, str(v))


def compare(a: tuple, b: tuple, direction: str) -> int:
    """model._compare: NULL last in both directions."""
    if a == b:
        return 0
    if a[0] != b[0]:
        return 1 if a[0] else -1
    r = -1 if a < b else 1
    return r if direction == "asc" else -r


def encode(values: pa.Array) -> tuple[np.ndarray, list]:
    """Every value as the index of its distinct value, the distinct values in the order
    they first come (NULL one of them)."""
    if pa.types.is_null(values.type):
        values = values.cast(pa.string())
    if pa.types.is_dictionary(values.type):
        values = values.cast(values.type.value_type)
    enc = pc.dictionary_encode(values, null_encoding="encode")
    ids = enc.indices.to_numpy(zero_copy_only=False).astype(np.int64) if len(values) else np.empty(0, np.int64)
    return ids, enc.dictionary.to_pylist()


def lexical(keys: list[np.ndarray], n: int) -> np.ndarray:
    """The positions that put the rows in the order of `keys` (the first the most
    significant; each a rank, 0 the first), ties in the order they are: a stable sort per
    key from the last, by counting where the ranks are few."""
    order = np.arange(n, dtype=np.int64)
    for key in reversed(keys):
        k = key[order]
        if n and k.max(initial=0) < 2**16:
            k = k.astype(np.uint16)  # numpy sorts small integers by counting (radix), stably
        order = order[np.argsort(k, kind="stable")]
    return order


def _ranks(values: pa.Array, direction: str) -> tuple[np.ndarray, list]:
    """A rank per value (values Python sorts as equal, one rank) in the order `direction`
    gives them, NULL last; and the value of each rank. Python orders the distinct values."""
    ids, distinct = encode(values)
    order = sorted(range(len(distinct)), key=cmp_to_key(
        lambda a, b: compare(sort_key(distinct[a]), sort_key(distinct[b]), direction)))
    rank_of = np.empty(len(distinct), np.int64)
    named: list = []
    prev = None
    for i in order:
        k = sort_key(distinct[i])
        if prev is None or k != prev:
            named.append(distinct[i])
            prev = k
        rank_of[i] = len(named) - 1
    return rank_of[ids] if len(ids) else np.empty(0, np.int64), named


# ── the session ──────────────────────────────────────────────────────────────
class Store:
    """The datasets of one render in a DuckDB of the worker's process: an in-memory database
    with the worker's share of memory and CPUs (app/engine/duckdb_budget.py), spilling into
    a directory of its own; closed with the render."""

    def __init__(self, tables: dict[str, Table | None], loc: formats.Locale, report_names: Iterable[str]):
        import duckdb

        self.tables = tables
        self.loc = loc
        self.names = frozenset(report_names)
        self.tmp = tempfile.mkdtemp(prefix="report-duckdb-")
        self.con = duckdb.connect(":memory:")
        self.con.execute(f"SET temp_directory = '{self.tmp}'")
        try:
            from app.engine import duckdb_budget

            duckdb_budget.apply(self.con)
        except Exception as e:  # noqa: BLE001 — DuckDB's defaults are a budget too
            logger.warning("report: the DuckDB budget could not be set (%s)", e)
        # it reads only the tables registered here: no file, URL or extension — defence in
        # depth behind the translator, which writes every name and text quoted
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")
        self._n = 0
        self._rels: dict[str, tuple[int, Rel]] = {}
        self._rows: dict[str, tuple[int, SqlRows]] = {}

    def close(self) -> None:
        try:
            self.con.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def register(self, prefix: str, table: pa.Table) -> str:
        self._n += 1
        name = f"{prefix}_{self._n}"
        self.con.register(name, table)
        return name

    def drop(self, name: str) -> None:
        try:
            self.con.unregister(name)
        except Exception:  # noqa: BLE001
            pass

    def query(self, text: str) -> pa.Table:
        import duckdb

        try:
            return self.con.execute(text).to_arrow_table()
        except duckdb.Error as e:
            raise Fallback(f"DuckDB: {e}") from None

    def rel(self, dataset: str) -> "Rel":
        """The dataset as a relation of DuckDB (registered again after a calculated field)."""
        table = self.tables.get(dataset)
        if table is None:
            raise Fallback(f"the dataset «{dataset}» is not in columns")
        hit = self._rels.get(dataset)
        if hit is None or hit[0] != table.version:
            if hit is not None:
                self.drop(hit[1].name)
            hit = (table.version, Rel(self, dataset, table))
            self._rels[dataset] = hit
        return hit[1]

    def datasets(self, data: dict[str, Any]) -> Mapping[str, Any]:
        """Every dataset by name, for an aggregate that names one: its rows in DuckDB as
        they are now, or the interpreter's when it keeps that dataset."""
        store = self

        class Live(Mapping):
            def __getitem__(self, name: str) -> Any:
                if store.tables.get(name) is not None:
                    return store.rows(name)
                return data[name].rows

            def __iter__(self) -> Iterator[str]:
                return iter(data)

            def __len__(self) -> int:
                return len(data)

        return Live()

    def rows(self, dataset: str) -> "SqlRows":
        """A dataset's rows, in the order they were read."""
        rel = self.rel(dataset)
        hit = self._rows.get(dataset)
        if hit is None or hit[0] != rel.table.version:
            fam = Family(rel, np.arange(rel.n, dtype=np.int64), np.array([0, rel.n], np.int64), [None])
            hit = (rel.table.version, fam.members[0])
            self._rows[dataset] = hit
        return hit[1]


class Rel:
    """A dataset registered in DuckDB, and what is computed over each of its rows."""

    def __init__(self, store: Store, dataset: str, table: Table):
        self.store = store
        self.dataset = dataset
        self.table = table
        self.n = table.n
        self.name = store.register("ds", table.arrow())
        self.cols = {name: rsql.Col(rsql.quote(table.column_name(name)), kind,
                                    rsql.quote(table.flag_name(name)) if name in table._flags else None)
                     for name, kind in table.kinds.items()}
        self._values: dict[tuple[str, str], tuple[pa.Array, pa.Array | None]] = {}
        self._maps: dict[tuple, str] = {}

    def translate(self, node: ex.Node, ctx: ex.Context, rownumber: str | None = None) -> rsql.Sql:
        """The expression over a row of this dataset; constants evaluated in `ctx` (its
        scopes are those the interpreter would have)."""
        const_ctx = ctx.at(row=None)
        evaluate = ex.Evaluator()
        try:
            return rsql.Translator(self.cols, self.store.loc, lambda n: evaluate(n, const_ctx), self, rownumber,
                                   datasets=frozenset(ctx.datasets) if ctx.datasets else self.store.names)(node)
        except rsql.Untranslatable as e:
            raise Fallback(str(e)) from None

    def values(self, s: rsql.Sql) -> tuple[pa.Array, pa.Array | None]:
        """The expression's value on every row, in the rows' order, and for NUM its flags."""
        key = (s.text, s.isint if s.kind == NUM else "")
        hit = self._values.get(key)
        if hit is not None:
            return hit
        if s.kind == NULL:
            hit = (pa.nulls(self.n), None)
        else:
            flag = f", {s.isint} AS f" if s.kind == NUM else ""
            # a projection keeps the rows' order (preserve_insertion_order); one that looks a
            # value up joins, and is put back in order by the rows' numbers
            joins = "(SELECT " in s.text
            order = f", {rsql.quote(ORD)} AS o" if joins else ""
            out = self.store.query(f"SELECT {s.text} AS v{flag}{order} FROM {self.name}")
            v = out.column("v").combine_chunks()
            f = out.column("f").combine_chunks() if s.kind == NUM else None
            if len(v) != self.n:  # pragma: no cover — a projection keeps every row
                raise Fallback("a projection lost rows")
            if joins:
                o = out.column("o").to_numpy()
                if len(o) > 1 and not (np.diff(o) > 0).all():
                    back = pa.array(np.argsort(o, kind="stable"))
                    v = v.take(back)
                    f = f.take(back) if f is not None else None
            hit = (v, f)
        self._values[key] = hit
        return hit

    def python_values(self, s: rsql.Sql, idx: np.ndarray) -> list:
        v, f = self.values(s)
        take = pa.array(idx, pa.int64())
        return to_python(v.take(take), f.take(take) if f is not None else None)

    # sql.Mapper: what only Python computes, once per distinct value
    def lookup(self, key: str, fn: Callable[[Any], Any], arg: rsql.Sql, out: str) -> str:
        num = arg.kind == NUM
        cache = (key, arg.text, arg.isint if num else "")
        name = self._maps.get(cache)
        if name is None:
            flag = f", {arg.isint} AS f" if num else ""
            got = self.store.query(f"SELECT DISTINCT {arg.text} AS k{flag} FROM {self.name}")
            k = got.column("k").combine_chunks()
            f = got.column("f").combine_chunks() if num else None
            values = to_python(k, f)
            try:
                mapped = [None if v is None else fn(v) for v in values]
                v_arr = pa.array(mapped, pa.string() if out == "VARCHAR" else None)
            except (ex.EvalError, ValueError, TypeError, OverflowError, pa.ArrowInvalid) as e:
                raise Fallback(f"a value only Python computes: {e}") from None
            arrays, names = [k, v_arr], ["k", "v"]
            if f is not None:
                arrays.insert(1, f)
                names.insert(1, "f")
            name = self.store.register("map", pa.Table.from_arrays(arrays, names=names))
            self._maps[cache] = name
        cond = f"m.k IS NOT DISTINCT FROM ({arg.text})"
        if num:
            cond += f" AND m.f IS NOT DISTINCT FROM ({arg.isint})"
        return f"(SELECT m.v FROM {name} m WHERE {cond})"


# ── sets of rows ─────────────────────────────────────────────────────────────
class Family:
    """Sets of rows of one dataset worked out together: every group of a level, every
    category of a chart. `perm` holds the rows of every member, member after member, each
    in its own order; `bounds` where each begins; `keys` what names each."""

    def __init__(self, rel: Rel, perm: np.ndarray, bounds: np.ndarray, keys: list):
        self.rel = rel
        self.perm = perm
        self.bounds = bounds
        self.keys = keys
        self.members = [SqlRows(self, i) for i in range(len(keys))]
        self._name: str | None = None
        self._aggs: dict[str, list] = {}
        self._firsts: dict[str, list] = {}
        self._running: dict[tuple, list] = {}
        self.cross: dict[int, tuple] = {}  # the intersections with another family (SqlData.intersect)

    def size(self, i: int) -> int:
        return int(self.bounds[i + 1] - self.bounds[i])

    def idx(self, i: int) -> np.ndarray:
        return self.perm[self.bounds[i]:self.bounds[i + 1]]

    def member_of(self) -> np.ndarray:
        """The member every row of the dataset belongs to, -1 for none."""
        out = np.full(self.rel.n, -1, np.int32)
        sizes = np.diff(self.bounds)
        out[self.perm] = np.repeat(np.arange(len(sizes), dtype=np.int32), sizes)
        return out

    def position(self) -> np.ndarray:
        out = np.full(self.rel.n, -1, np.int64)
        out[self.perm] = np.arange(len(self.perm), dtype=np.int64)
        return out

    def relation(self) -> str:
        """The dataset with the member and the position of each row: what GROUP BY reads."""
        if self._name is None:
            base = self.rel.table.arrow()
            base = base.append_column("__m", pa.array(self.member_of()))
            base = base.append_column("__o", pa.array(self.position()))
            self._name = self.rel.store.register("fam", base)
        return self._name

    # what the evaluator asks
    def aggregate(self, node: ex.Call, ctx: ex.Context) -> list:
        named = ex.scope_dataset(node, ctx.datasets)
        args = node.args[:-1] if named is not None else node.args
        key = node.name + "|" + (_canon(args[0]) if args else "")
        hit = self._aggs.get(key)
        if hit is None:
            hit = self._aggregate(node.name, args[0] if args else None, ctx)
            self._aggs[key] = hit
        return hit

    def _aggregate(self, name: str, arg: ex.Node | None, ctx: ex.Context) -> list:
        k = len(self.members)
        if arg is None:  # COUNT(): the rows
            return [self.size(i) for i in range(k)]
        if _has_rownumber(arg):  # the interpreter's ROWNUMBER inside is the caller's, not the rows'
            raise Fallback("ROWNUMBER() inside an aggregate")
        x = self.rel.translate(arg, ctx.at(row=None, scopes=[], running=None, running_index=-1))
        empty = 0 if name in ("COUNT", "COUNTDISTINCT") else None
        if x.kind == NULL:
            return [empty] * k
        if name in ("SUM", "AVG") and x.kind not in (INT, FLOAT, NUM):
            raise Fallback(f"{name} of a {x.kind}")
        fold = (name in ("SUM", "AVG") and x.kind in (FLOAT, NUM)) or (name in ("MIN", "MAX") and x.kind == NUM)
        if fold:
            return self._fold(name, x)
        if x.kind == rsql.LIST:
            raise Fallback("an aggregate of a parameter of many values")
        sql = {
            "COUNT": f"count({x.text})",
            "COUNTDISTINCT": f"count(DISTINCT {x.text})",
            "SUM": f"sum({x.text})",
            "AVG": f"CAST(sum({x.text}) AS DOUBLE) / count({x.text})",
            "MIN": f"min({x.text})",
            "MAX": f"max({x.text})",
            "FIRST": f"arg_min({x.text}, __o)",
            "LAST": f"arg_max({x.text}, __o)",
        }[name]
        flag = ""
        if x.kind == NUM and name in ("FIRST", "LAST"):
            fn = "arg_min" if name == "FIRST" else "arg_max"
            flag = f", {fn}({x.isint}, __o) FILTER (WHERE {x.text} IS NOT NULL) AS f"
        got = self.rel.store.query(f"SELECT __m AS m, {sql} AS v{flag} FROM {self.relation()} WHERE __m >= 0 "
                                   f"GROUP BY __m")
        out: list = [empty] * k
        ms = got.column("m").to_pylist()
        vs = to_python(got.column("v").combine_chunks(), got.column("f").combine_chunks() if flag else None)
        for m, v in zip(ms, vs):
            out[m] = v
        return out

    def _fold(self, name: str, x: rsql.Sql) -> list:
        """A sum, an average (of decimals), a min or max (of ints and floats together):
        Python's own over the values in each member's order — the interpreter's result."""
        v, f = self.rel.values(x)
        take = pa.array(self.perm, pa.int64())
        ordered = v.take(take)
        flags = f.take(take) if f is not None else None
        if flags is None and x.kind == FLOAT:
            arr = ordered.to_numpy(zero_copy_only=False)  # NULL is NaN here, and no value is
            starts, ends = self.bounds[:-1], self.bounds[1:]
            if ordered.null_count:
                valid = ~np.isnan(arr)
                arr = arr[valid]
                kept = np.concatenate([[0], np.cumsum(valid)])
                starts, ends = kept[starts], kept[ends]
            return [_sum_or_mean(name, arr[a:b].tolist()) for a, b in zip(starts.tolist(), ends.tolist())]
        values = to_python(ordered, flags)
        out = []
        for i in range(len(self.members)):
            vals = [v for v in values[self.bounds[i]:self.bounds[i + 1]] if v is not None]
            try:
                out.append(_sum_or_mean(name, vals) if name in ("SUM", "AVG") else ex._aggregate(name, vals))
            except ex.EvalError as e:  # pragma: no cover — the kinds were checked
                raise Fallback(str(e)) from None
        return out

    def first(self, name: str) -> list:
        """The field of each member's first row (a field outside a row)."""
        hit = self._firsts.get(name)
        if hit is None:
            if name not in self.rel.table.kinds:
                hit = [None] * len(self.members)
            else:
                values, flag = self.rel.table.column(name)
                starts = [int(self.perm[self.bounds[i]]) for i in range(len(self.members)) if self.size(i)]
                take = pa.array(starts, pa.int64())
                got = iter(to_python(values.take(take), flag.take(take) if flag is not None else None))
                hit = [next(got) if self.size(i) else None for i in range(len(self.members))]
            self._firsts[name] = hit
        return hit

    def running(self, inner: ex.Call, ctx: ex.Context, i: int) -> list:
        """RUNNING(agg) over a member's rows: expressions._running of their values."""
        key = (i, inner.name, _canon(inner.args[0]) if inner.args else "")
        hit = self._running.get(key)
        if hit is None:
            if inner.args:
                if _has_rownumber(inner.args[0]):
                    raise Fallback("ROWNUMBER() inside an aggregate")
                x = self.rel.translate(inner.args[0], ctx.at(row=None, scopes=[], running=None, running_index=-1))
                values = self.rel.python_values(x, self.idx(i)) if x.kind != NULL else [None] * self.size(i)
                name = inner.name
            else:
                values, name = [1] * self.size(i), "COUNT"
            try:
                hit = ex._running(name, values)
            except ex.EvalError as e:
                raise Fallback(str(e)) from None
            self._running[key] = hit
        return hit


class SqlRows:
    """One set of rows that stays in DuckDB: a member of its family."""

    __slots__ = ("family", "i")

    def __init__(self, family: Family, i: int):
        self.family = family
        self.i = i

    def __len__(self) -> int:
        return self.family.size(self.i)

    def __bool__(self) -> bool:
        return len(self) > 0

    def __repr__(self) -> str:
        return f"SqlRows({self.family.rel.dataset}, {self.family.keys[self.i]!r}, {len(self)} rows)"

    @property
    def idx(self) -> np.ndarray:
        return self.family.idx(self.i)

    @property
    def key(self) -> Any:
        return self.family.keys[self.i]

    def aggregate(self, node: ex.Call, ctx: ex.Context) -> Any:
        return self.family.aggregate(node, ctx)[self.i]

    def first_value(self, name: str) -> Any:
        return self.family.first(name)[self.i]

    def running(self, inner: ex.Call, ctx: ex.Context) -> list:
        return self.family.running(inner, ctx, self.i)

    def rows(self) -> list[dict]:
        return self.family.rel.table.rows_at(self.idx)


def _sum_or_mean(name: str, numbers: list) -> Any:
    """expressions._aggregate of numbers that are numbers: the same `sum` over the same
    values in the same order (its rounding depends on both), without checking each one."""
    if not numbers:
        return None
    total = sum(numbers)
    return total if name == "SUM" else total / len(numbers)


def _canon(node: ex.Node) -> str:
    """An expression written back canonically: the same aggregate in two places is
    computed once."""
    if isinstance(node, ex.Lit):
        return f"L{type(node.value).__name__}:{node.value!r}"
    if isinstance(node, ex.Field):
        return f"F{node.name!r}"
    if isinstance(node, ex.Placeholder):
        return f"P{node.raw!r}"
    if isinstance(node, ex.Unary):
        return f"U{node.op}({_canon(node.arg)})"
    if isinstance(node, ex.Binary):
        return f"B{node.op}({_canon(node.left)},{_canon(node.right)})"
    assert isinstance(node, ex.Call)
    return f"C{node.name}(" + ",".join(_canon(a) for a in node.args) + ")"


def _has_rownumber(node: ex.Node) -> bool:
    if isinstance(node, ex.Call):
        return node.name == "ROWNUMBER" or any(_has_rownumber(a) for a in node.args)
    if isinstance(node, ex.Unary):
        return _has_rownumber(node.arg)
    if isinstance(node, ex.Binary):
        return _has_rownumber(node.left) or _has_rownumber(node.right)
    return False


def _rows_of(rows: Any) -> SqlRows:
    if not isinstance(rows, SqlRows):
        raise Fallback("rows of the interpreter where DuckDB's are wanted")
    return rows


# ── what the model asks (model.PyData has the interpreter's answers) ────────
class SqlData:
    """The model's questions about rows, answered in DuckDB."""

    def __init__(self, store: Store, evaluate: ex.Evaluator):
        self.store = store
        self.ev = evaluate

    def rows(self, name: str | None) -> Any:
        if name is None:
            return []
        if name not in self.store.tables:
            return []
        return self.store.rows(name)

    # per-row values
    def _keys(self, rows: SqlRows, node: ex.Node, ctx: ex.Context, as_key: bool = True) -> tuple[pa.Array, str]:
        """The expression's value on each row of the set, in its order."""
        rel = rows.family.rel
        s = rel.translate(node, ctx)
        if as_key and s.kind == NUM:
            raise Fallback("a key that is an int or a float row by row")
        if as_key and s.kind == FLOAT:  # -0.0 and 0.0 are one key, as for Python's dict
            s = rsql.Sql(f"({s.text} + CAST(0.0 AS DOUBLE))", FLOAT)
        if s.kind == rsql.LIST:
            raise Fallback("a parameter of many values as a key")
        values, _ = rel.values(s)
        return values.take(pa.array(rows.idx, pa.int64())), s.kind

    def where(self, rows: Any, node: ex.Node, ctx: ex.Context, item: str) -> SqlRows:
        rows = _rows_of(rows)
        rel = rows.family.rel
        s = rel.translate(node, ctx)
        if s.kind not in (BOOL, NULL):
            raise Fallback("a filter that is not true or false")
        if s.kind == NULL:
            keep = np.empty(0, np.int64)
        else:
            values, _ = rel.values(s)
            mask = values.take(pa.array(rows.idx, pa.int64())).fill_null(False).to_numpy(zero_copy_only=False)
            keep = rows.idx[np.asarray(mask, dtype=bool)]
        return Family(rel, keep, np.array([0, len(keep)], np.int64), [None]).members[0]

    def sort(self, rows: Any, keys: list[tuple[ex.Node, str]], ctx: ex.Context, item: str) -> SqlRows:
        rows = _rows_of(rows)
        rel = rows.family.rel
        columns = []
        for node, direction in keys:
            values, _ = self._keys(rows, node, ctx)
            ranks, _ = _ranks(values, direction)
            columns.append(ranks)
        if not columns:
            return rows
        n = len(rows)
        order = lexical(columns, n)
        perm = rows.idx[order]
        return Family(rel, perm, np.array([0, n], np.int64), [None]).members[0]

    def tree(self, rows: Any, levels: list[tuple[ex.Node, str]], ctx: ex.Context,
             item: str) -> tuple[list[dict], Any]:
        """The groups of every level and their rows, in the groups' order; and the table's
        rows one group after the other (the outermost groups' running totals go over them)."""
        rows = _rows_of(rows)
        rel = rows.family.rel
        n = len(rows)
        ranks, names = [], []
        for node, direction in levels:
            values, _ = self._keys(rows, node, ctx)
            r, distinct = _ranks(values, direction)
            ranks.append(r)
            names.append(distinct)
        order = lexical(ranks, n)
        perm = rows.idx[order]
        sorted_ranks = [r[order] for r in ranks]
        families = []
        for depth in range(1, len(levels) + 1):
            if n:
                change = np.zeros(n, bool)
                change[0] = True
                for r in sorted_ranks[:depth]:
                    change[1:] |= r[1:] != r[:-1]
                starts = np.flatnonzero(change)
            else:
                starts = np.empty(0, np.int64)
            bounds = np.append(starts, n).astype(np.int64)
            keys = [tuple(names[lv][int(sorted_ranks[lv][s])] for lv in range(depth)) for s in starts]
            families.append(Family(rel, perm, bounds, keys))
        all_rows = Family(rel, perm, np.array([0, n], np.int64), [None]).members[0]

        def build(depth: int, lo: int, hi: int) -> list[dict]:
            fam = families[depth]
            out = []
            i = int(np.searchsorted(fam.bounds[:-1], lo))
            while i < len(fam.members) and fam.bounds[i] < hi:
                member = fam.members[i]
                children = build(depth + 1, int(fam.bounds[i]), int(fam.bounds[i + 1])) \
                    if depth + 1 < len(families) else None
                out.append({"key": fam.keys[i][-1], "rows": member, "children": children})
                i += 1
            return out

        return (build(0, 0, n) if families else []), all_rows

    def details(self, rows: Any) -> list[dict]:
        return _rows_of(rows).rows()

    def partition(self, rows: Any, node: ex.Node, ctx: ex.Context, item: str, where: str) -> list[tuple[Any, Any]]:
        """The rows by the value of `node`, each value where it first comes."""
        rows = _rows_of(rows)
        values, _ = self._keys(rows, node, ctx)
        ids, keys = encode(values)  # in the order they come
        order = lexical([ids], len(ids))
        perm = rows.idx[order]
        bounds = np.concatenate([[0], np.cumsum(np.bincount(ids, minlength=len(keys)))]).astype(np.int64)
        fam = Family(rows.family.rel, perm, bounds, keys)
        return [(k, m) for k, m in zip(keys, fam.members)]

    def union(self, parts: list[Any]) -> Any:
        """The rows of these sets, one set after the other (a chart's «Other»)."""
        if not parts:
            return []
        parts = [_rows_of(p) for p in parts]
        perm = np.concatenate([p.idx for p in parts]) if parts else np.empty(0, np.int64)
        return Family(parts[0].family.rel, perm, np.array([0, len(perm)], np.int64), [None]).members[0]

    def intersect(self, a: Any, b: Any) -> Any:
        """The rows of `a` that are in `b`, in `a`'s order; all the pairs of their two
        families are worked out together."""
        if isinstance(a, list) and not a:
            return []
        a, b = _rows_of(a), _rows_of(b)
        fa, fb = a.family, b.family
        hit = fa.cross.get(id(fb))
        if hit is None:
            in_b = fb.member_of()
            members_a = np.repeat(np.arange(len(fa.members), dtype=np.int64), np.diff(fa.bounds))
            mb = in_b[fa.perm].astype(np.int64)
            keep = mb >= 0
            perm, ma, mb = fa.perm[keep], members_a[keep], mb[keep]
            order = lexical([ma, mb], len(perm))
            perm, ma, mb = perm[order], ma[order], mb[order]
            pairs = ma * max(len(fb.members), 1) + mb
            if len(pairs):
                change = np.ones(len(pairs), bool)
                change[1:] = pairs[1:] != pairs[:-1]
                starts = np.flatnonzero(change)
            else:
                starts = np.empty(0, np.int64)
            keys = [(int(ma[s]), int(mb[s])) for s in starts]
            fam = Family(fa.rel, perm, np.append(starts, len(perm)).astype(np.int64), keys)
            hit = ({k: m for k, m in zip(keys, fam.members)}, fb)  # fb kept alive with its id
            fa.cross[id(fb)] = hit
        return hit[0].get((a.i, b.i), [])

    def points(self, rows: Any, x: ex.Node, y: ex.Node, ctx: ex.Context, item: str) -> list[tuple[Any, Any]]:
        rows = _rows_of(rows)
        rel = rows.family.rel
        xs = rel.python_values(rel.translate(x, ctx), rows.idx)
        ys = rel.python_values(rel.translate(y, ctx), rows.idx)
        return list(zip(xs, ys))

    def matrix(self, rows: Any, levels: list[ex.Node], ctx: ex.Context, item: str) -> tuple[list[tuple], dict, Any]:
        """A matrix's columns: the distinct paths of values, in order, and the family of the
        table's rows by them."""
        rows = _rows_of(rows)
        n = len(rows)
        encoded, dicts = [], []
        for node in levels:
            values, _ = self._keys(rows, node, ctx)
            ids, distinct = encode(values)
            dicts.append(distinct)
            encoded.append(ids)
        if n:
            # the path of every row as one number, its levels' distinct values by position
            space = math.prod(len(distinct) for distinct in dicts)
            if space >= 2**62:
                raise Fallback("too many paths of values for a matrix's columns")
            code = np.zeros(n, np.int64)
            for ids, distinct in zip(encoded, dicts):
                code = code * len(distinct) + ids
            if space <= max(4 * n, 1 << 20):  # by counting
                present = np.flatnonzero(np.bincount(code, minlength=space))
                where = np.full(space, -1, np.int64)
                where[present] = np.arange(len(present))
                inverse = where[code]
            else:
                present, inverse = np.unique(code, return_inverse=True)
            raw = {}
            paths = []
            for c in present.tolist():
                parts = []
                for distinct in reversed(dicts):
                    c, part = divmod(c, len(distinct))
                    parts.append(distinct[part])
                tup = tuple(reversed(parts))
                paths.append(tuple(sort_key(v) for v in tup))
                raw[paths[-1]] = tup
            order = sorted(raw, key=cmp_to_key(lambda a, b: next(
                (c for c in (compare(x, y, "asc") for x, y in zip(a, b)) if c), 0)))
            if len(order) > rd.MAX_MATRIX_COLUMNS:  # the model says so
                return order, raw, None
            place = {sk: j for j, sk in enumerate(order)}
            cols = np.array([place[p] for p in paths], np.int64)[inverse.reshape(-1)]
        else:
            order, raw, cols = [], {}, np.empty(0, np.int64)
        perm = rows.idx[lexical([cols], n)]
        bounds = np.concatenate([[0], np.cumsum(np.bincount(cols, minlength=len(order)))]).astype(np.int64)
        fam = Family(rows.family.rel, perm, bounds, list(order))
        return order, raw, fam

    def matrix_sub(self, scope: Any, columns: Any, j: int) -> Any:
        fam: Family = columns
        if not fam.members:
            return []
        part = fam.members[j]
        if not part:
            return []
        return self.intersect(scope, part)


def build_store(report: Any, data: dict[str, Any], loc: formats.Locale) -> Store:
    """The datasets of a render in columns (those whose values are of one kind per column)."""
    tables: dict[str, Table | None] = {}
    for name, ds in data.items():
        table = getattr(ds, "table", None)
        if table is None and ds.rows is not None:
            table = from_rows(ds.rows, ds.columns)
        tables[name] = table
    return Store(tables, loc, [d.name for d in report.datasets])


def add_calculated(report: Any, data: dict[str, Any], store: Store, ctx: ex.Context) -> None:
    """The report's own fields, in DuckDB, in their order (each may use the ones before; an
    aggregate in one works on the whole dataset). One DuckDB cannot write the interpreter's
    way is worked out by the interpreter, row by row; the dataset leaves the columns when its
    values are of more than one kind."""
    from app.reports import definition as rd

    for d in report.datasets:
        ds = data.get(d.name)
        if ds is None or not d.calculated:
            continue
        for c in d.calculated:
            node = rd.parse(c.expr)
            table = store.tables.get(d.name)
            done = False
            if table is not None:
                try:
                    rel = store.rel(d.name)
                    rows = store.rows(d.name)
                    s = rel.translate(node, ctx.at(scopes=[rows]), rownumber=f"({rsql.quote(ORD)} + 1)")
                    if s.kind == rsql.LIST:
                        raise Fallback("a parameter of many values as a field")
                    values, flag = rel.values(s)
                    if s.kind == NULL:
                        table.add(c.name, pa.nulls(table.n), NULL)
                    else:
                        table.add(c.name, values.cast(_ARROW[s.kind]), s.kind, flag)
                    done = True
                except Fallback as e:
                    logger.info("report: dataset «%s», calculated field «%s» by the interpreter: %s", d.name, c.name, e)
            if not done:
                rows = table.rows() if table is not None else ds.rows
                c_ctx = ctx.at(scopes=[rows])
                evaluate = ex.Evaluator()
                values = []
                for i, row in enumerate(rows):
                    c_ctx.row = row
                    c_ctx.rownumber = i + 1
                    try:
                        values.append(evaluate(node, c_ctx))
                    except ex.EvalError as e:
                        raise ReportError(f"dataset «{d.name}», calculated field «{c.name}», row {i + 1}: {e}") \
                            from None
                if table is not None:
                    typed = _classify(values)
                    if typed is None:
                        store.tables[d.name] = None  # the interpreter keeps this dataset
                        for row, v in zip(rows, values):
                            row[c.name] = v
                        ds.rows = rows
                        ds.table = None
                    else:
                        kind, arr, flag = typed
                        table.add(c.name, arr, kind, flag)
                else:
                    for row, v in zip(rows, values):
                        row[c.name] = v
            ds.columns[c.name] = ""

