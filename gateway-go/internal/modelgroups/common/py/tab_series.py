"""The series of a time column (docs/specs/foundation-forecast.md, «The series»; lot F2): the Python twin of
groups/timeseries/runner/R/ts_series.R, with its codes, its sentences and its numbers — the grid of a frequency, a
time held twice or off the grid refused, the span from the first to the last observed value, the periods inside it
without a value (refused, filled by a straight line, or left empty for a model that reads them), the input's own time
type written back (an Int64 stays Int64, a timestamp keeps its unit and its zone), and the envelope thinning of a long
series for its chart. A table of many series is split by its series column; each series follows the rules alone.

  read_table(task, columns)          the node's columns as a pyarrow table (the step's Parquet file, or a self-test's)
  columns(table, time, value, frequency)
                                     the time and the value as arrays, once per table, with the column refusals
  split(column)                      the rows of each series, its labels in text order (catalog.label_of's)
  series(cols, rows, missing, …)     one series on its grid: span, gaps, fill, the rows after its last value
  time_array(ticks, axis)            times as an Arrow array of the input field's own type (ts_time_array)
  envelope(y, max)                   the instants of a long series kept for its chart (ts_envelope)

Nothing here needs pandas: the arrays are numpy views of the Arrow buffers where the type allows, and a series holds
its own span only (a few arrays of its length), never a copy of the table. A time is held as its «ticks»: the
integer of an integer column, the days of a date, the units of a timestamp (seconds, ms, µs or ns since 1970 in
UTC), the double of a whole-valued number column. The arithmetic that decides the grid is R's, in the same doubles
(seconds as a double, days = seconds / 86400, an hour's period = days · 24, a tolerance of 1e-9 of a period), so that
a time R calls off the grid is off the grid here too.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np

from tab_columns import kind_of_arrow, label as value_label
from tab_protocol import ENV_INPUT, Refusal, is_uri

# places of the grid, gaps included (ts_series.R's TS_MAX_PERIODS)
TS_MAX_PERIODS = 200_000

UNIT = {"hour": "hours", "day": "days", "week": "weeks", "month": "months", "quarter": "quarters", "year": "years",
        "index": "steps"}
ONE = {"hour": "hour", "day": "day", "week": "week", "month": "month", "quarter": "quarter", "year": "year",
       "index": "step"}
FREQUENCIES = tuple(UNIT)
MISSING = ("refuse", "linear", "model")

_PER_SECOND = {"s": 1, "ms": 1_000, "us": 1_000_000, "ns": 1_000_000_000}
_MONTHS = {"month": 1, "quarter": 3, "year": 12}
_STEP_SECONDS = {"hour": 3600, "day": 86400, "week": 604800}
_EPOCH_YM = 1970 * 12  # .ts_ym of 1970-01


# ── the input ─────────────────────────────────────────────────────────────────
def read_table(task: dict[str, Any], columns: Sequence[str]):
    """The node's columns as a pyarrow table, each once, in the order asked: a self-test's table (task["_table"]) or
    frame (task["_frame"]), else the step's Parquet file (TABULARIA_INPUT), whose schema is read first so that a
    column it lacks is refused before any byte of data is (ts_read)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    wanted = list(dict.fromkeys(columns))
    if task.get("_table") is not None:
        table = task["_table"]
    elif task.get("_frame") is not None:
        frame = task["_frame"]
        _have(wanted, list(frame.columns))
        table = pa.Table.from_pandas(frame[wanted], preserve_index=False)
    else:
        path = os.environ.get(ENV_INPUT, "")
        if not path:
            raise Refusal("no_input", "the step has no input (TABULARIA_INPUT is empty)")
        filesystem = None
        where = path
        if is_uri(path):
            from pyarrow import fs as pafs

            filesystem, where = pafs.FileSystem.from_uri(path)
        _have(wanted, pq.read_schema(where, filesystem=filesystem).names)
        table = pq.read_table(where, columns=wanted, filesystem=filesystem)
    _have(wanted, table.column_names)
    return table.select(wanted)


def _have(wanted: Iterable[str], names: Sequence[str]) -> None:
    have = set(names)
    for name in wanted:
        if name not in have:
            raise Refusal("no_column", f"the input has no column «{name}»", {"column": name})


# ── the time: its kind, its ticks, its grid ───────────────────────────────────
@dataclass(frozen=True)
class Axis:
    """The time column's kind (date, datetime, integer), the frequency, and the Arrow type the output writes back.
    `storage` says what a tick is: "int" (an integer column's value), "float" (a whole-valued number column's),
    "days" (a date's days since 1970-01-01), "ticks" (a timestamp's units since 1970 in UTC, `per_second` a second)."""

    kind: str
    frequency: str
    type: Any
    storage: str
    per_second: int = 1

    # as.numeric of R's value: the integer, the days of a date, the seconds of a datetime (a double)
    def numeric(self, t: np.ndarray) -> np.ndarray:
        x = t.astype(np.float64)
        return x / self.per_second if self.storage == "ticks" else x

    def _days(self, t: np.ndarray) -> np.ndarray:
        """Whole days since 1970-01-01 in UTC (POSIXlt's date of a datetime: the floor)."""
        if self.storage == "days":
            return t.astype(np.int64)
        return np.floor_divide(t, 86400 * self.per_second)

    def _ym(self, t: np.ndarray) -> np.ndarray:
        """.ts_ym: year · 12 + month (0–11) of each time, in UTC, as a double."""
        months = self._days(t).astype("datetime64[D]").astype("datetime64[M]").astype(np.int64)
        return (months + _EPOCH_YM).astype(np.float64)

    def period(self, t: np.ndarray, num: np.ndarray) -> np.ndarray:
        """ts_period: the period of each time as a double (whole for times on the grid); `num` is numeric(t)."""
        if self.kind == "integer":
            return num
        f = self.frequency
        if f == "index":  # rank(…, ties.method = "first") − 1 of times already sorted
            return np.arange(t.size, dtype=np.float64)
        if f in _MONTHS:
            ym = self._ym(t)
            return ym if f == "month" else np.floor(ym / _MONTHS[f])
        days = num if self.kind == "date" else num / 86400
        if f == "hour":
            return days * 24
        if f == "day":
            return days
        if f == "week":
            return days / 7
        raise ValueError(f"unknown frequency {f}")

    def after(self, t0: Any, k: np.ndarray) -> np.ndarray:
        """ts_time_after: the times k periods after the time t0 (one tick), as ticks of the same storage."""
        k = np.asarray(k, dtype=np.int64)
        if self.kind == "integer":
            return t0 + k.astype(np.float64) if self.storage == "float" else np.int64(t0) + k
        months = _MONTHS.get(self.frequency)
        if months is not None:
            day0 = int(self._days(np.array([t0], dtype=np.int64))[0])
            d0 = np.datetime64(day0, "D")
            first = d0.astype("datetime64[M]")
            mday = int((d0 - first.astype("datetime64[D]")).astype(np.int64)) + 1
            m = first + k * months
            start = m.astype("datetime64[D]")
            length = ((m + 1).astype("datetime64[D]") - start).astype(np.int64)
            days = start.astype(np.int64) + np.minimum(mday, length) - 1
            if self.kind == "date":
                return days
            per_day = 86400 * self.per_second
            return days * per_day + (np.int64(t0) - day0 * per_day)
        step = _STEP_SECONDS.get(self.frequency, 86400)
        if self.kind == "date":
            return np.int64(t0) + (k * step) // 86400
        return np.int64(t0) + k * (step * self.per_second)

    def x0(self, t: np.ndarray) -> np.ndarray:
        """ts_x0: the x of a chart, days since 1970-01-01 (a double) for a date or datetime, the integer itself."""
        x = self.numeric(t)
        return x / 86400 if self.kind == "datetime" else x

    def label(self, t: Any) -> str:
        """tab_label of one time: 2026-01-05, «2026-01-05 13:00:00» (UTC, whole seconds), 5000000000, 5.0."""
        if self.kind == "date":
            return (dt.date(1970, 1, 1) + dt.timedelta(days=int(t))).isoformat()
        if self.kind == "datetime":
            secs = int(t) // self.per_second
            return (dt.datetime(1970, 1, 1) + dt.timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S")
        if self.storage == "float":
            return value_label(float(t))
        return str(int(t))


def time_kind(col: Any, time: str, frequency: str) -> str:
    """ts_time_kind: date, datetime or integer (a number column whose values are all whole); anything else, and a
    date with frequency = hour, is refused."""
    import pyarrow as pa
    import pyarrow.compute as pc

    kind = kind_of_arrow(col.type)
    if kind == "number":
        x = pc.cast(col, pa.float64()).to_numpy(zero_copy_only=False)
        with np.errstate(invalid="ignore"):
            if bool(np.all(np.isnan(x) | (x == np.trunc(x)))):
                kind = "integer"
    if kind not in ("date", "datetime", "integer"):
        raise Refusal("time_type", f"the column «{time}» is not a date, a datetime or an integer: the time of a series "
                                   "must be one of them", {"column": time})
    if kind == "date" and frequency == "hour":
        raise Refusal("frequency_hour", f"the column «{time}» holds dates, without hours: choose frequency = day, or a "
                                        "datetime column", {"column": time})
    return kind


def _ticks(col: Any, kind: str, frequency: str) -> tuple[Axis, np.ndarray, np.ndarray]:
    """The axis of a time column, its ticks (int64, or float64 for a number column) and which rows have a time."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.types as pt

    t = col.type
    has = ~np.asarray(pc.is_null(col, nan_is_null=True).to_numpy(zero_copy_only=False), dtype=bool)
    if kind == "date":
        days = pc.cast(col, pa.date32()) if pt.is_date64(t) else col
        ticks = pc.cast(days, pa.int32()).fill_null(0).to_numpy(zero_copy_only=False).astype(np.int64)
        return Axis(kind, frequency, t, "days"), ticks, has
    if kind == "datetime":
        ticks = pc.cast(col, pa.int64()).fill_null(0).to_numpy(zero_copy_only=False)
        return Axis(kind, frequency, t, "ticks", _PER_SECOND[t.unit]), ticks, has
    if pt.is_integer(t):
        ticks = pc.cast(col, pa.int64()).fill_null(0).to_numpy(zero_copy_only=False)
        return Axis(kind, frequency, t, "int"), ticks, has
    ticks = pc.cast(col, pa.float64()).to_numpy(zero_copy_only=False)
    return Axis(kind, frequency, t, "float"), np.where(has, ticks, 0.0), has


@dataclass
class Columns:
    """The time and the value of a table as arrays, made once for all its series: `ticks` and `has_time` per row,
    `values` the value as a double (NaN where it is empty)."""

    time: str
    value: str
    axis: Axis
    ticks: np.ndarray
    has_time: np.ndarray
    values: np.ndarray

    @property
    def rows(self) -> int:
        return int(self.ticks.size)


def columns(table: Any, time: str, value: str, frequency: str) -> Columns:
    """The time and the value of the table, with ts_series' column refusals in its order: the time's kind
    (`time_type`, `frequency_hour`), the value not a number (`not_a_number`), an infinite value (`infinite`)."""
    import pyarrow as pa
    import pyarrow.compute as pc

    if frequency not in FREQUENCIES:
        raise ValueError(f"unknown frequency {frequency}")
    tcol = table.column(time)
    kind = time_kind(tcol, time, frequency)
    vcol = table.column(value)
    if kind_of_arrow(vcol.type) not in ("number", "integer"):
        raise Refusal("not_a_number", f"the column «{value}» is not a number", {"column": value})
    values = pc.cast(vcol, pa.float64()).to_numpy(zero_copy_only=False)
    if np.isinf(values).any():
        raise Refusal("infinite", f"the column «{value}» holds an infinite value: filter it out or replace it before the node",
                      {"column": value})
    axis, ticks, has = _ticks(tcol, kind, frequency)
    return Columns(time, value, axis, ticks, has, values)


# ── the series of a table ─────────────────────────────────────────────────────
def split(col: Any) -> tuple[list[str], list[np.ndarray], np.ndarray]:
    """The series of a series column: their labels (catalog.label_of's: «true», 3000000001, a text as it is) in text
    order, the rows of each (ascending row numbers, views of one array), and the rows whose series is empty."""
    import pyarrow as pa
    import pyarrow.compute as pc

    if isinstance(col, pa.ChunkedArray):
        col = col.combine_chunks()
    enc = pc.dictionary_encode(col, null_encoding="mask")
    names = [value_label(v) for v in enc.dictionary.to_pylist()]
    rank = np.empty(len(names), dtype=np.int64)
    order_names = sorted(range(len(names)), key=lambda i: names[i])
    rank[order_names] = np.arange(len(names), dtype=np.int64)
    idx = enc.indices
    empty = np.flatnonzero(np.asarray(pc.is_null(idx).to_numpy(zero_copy_only=False), dtype=bool))
    code = idx.fill_null(0).to_numpy(zero_copy_only=False).astype(np.int64)
    code = rank[code] if len(names) else code
    if empty.size:
        code[empty] = -1
    order = np.argsort(code, kind="stable")
    bounds = np.searchsorted(code[order], np.arange(len(names) + 1, dtype=np.int64), side="left")
    rows = [order[bounds[i]:bounds[i + 1]] for i in range(len(names))]
    return [names[i] for i in order_names], rows, empty


@dataclass
class Series:
    """One series from its first to its last observed value, one place a period (ts_series' list, and what a forecast
    needs besides):

      times       the span's ticks: the row's own time where there is a row, the grid's elsewhere
      y           the observed values, NaN where a period has none
      z           the values a method reads: y itself, or y with its gaps filled (missing = linear)
      filled      which places were filled (missing = linear)
      span_rows   the input row of each place (-1 where none)
      after_rows  the input rows after the last observed value, in time order, and after_pos their places after it
                  (1 = the next period): a forecast's future rows
      n_missing   rows without a time, before the first and after the last observed value (R's count)
      n_filled    places filled (linear); n_model_missing: places left empty for the model (missing = model)
    """

    axis: Axis
    time: str
    value: str
    label: str | None
    missing: str
    times: np.ndarray
    y: np.ndarray
    z: np.ndarray
    filled: np.ndarray
    span_rows: np.ndarray
    after_rows: np.ndarray
    after_pos: np.ndarray
    grid_start: Any
    first: int
    n_no_time: int
    n_before: int
    n_after: int
    n_filled: int
    n_model_missing: int

    @property
    def kind(self) -> str:
        return self.axis.kind

    @property
    def frequency(self) -> str:
        return self.axis.frequency

    @property
    def n(self) -> int:
        return int(self.y.size)

    @property
    def n_missing(self) -> int:
        return self.n_no_time + self.n_before + self.n_after

    @property
    def gaps(self) -> np.ndarray:
        return np.flatnonzero(np.isnan(self.y))

    def x0(self) -> np.ndarray:
        return self.axis.x0(self.times)

    def after(self, k: np.ndarray | int) -> np.ndarray:
        """The ticks of the periods k after the last observed value (k = 1: the next), on the series' own grid (a
        monthly series from 31 January keeps the last day of each month)."""
        k = np.atleast_1d(np.asarray(k, dtype=np.int64))
        return self.axis.after(self.grid_start, (self.first - 1) + (self.n - 1) + k)

    def time_array(self, ticks: np.ndarray | None = None):
        """The span's times (or the ticks given) as an Arrow array of the input's own type."""
        return time_array(self.times if ticks is None else ticks, self.axis)


def _in(label: str | None) -> str:
    return "" if label is None else f" in the series «{label}»"


def _with(params: dict[str, Any], label: str | None) -> dict[str, Any]:
    return params if label is None else {**params, "series": label}


def _grid(axis: Axis, t: np.ndarray, time: str, max_periods: int, label: str | None):
    """ts_grid: the order of the rows by time (stable), the place of each (1 = the first time) and the grid's length;
    a time held twice, a time off the grid and a grid longer than max_periods are refused."""
    order = np.argsort(t, kind="stable")
    ts = t[order]
    num = axis.numeric(ts)
    period = axis.period(ts, num)
    if ts.size > 1:
        twice = np.flatnonzero((period[1:] == period[:-1]) | (num[1:] == num[:-1]))
        if twice.size:
            at = axis.label(ts[twice[0] + 1])
            raise Refusal("duplicate_time", f"the column «{time}» holds two rows for the period of {at}{_in(label)}: a series "
                                            "has one row per period; aggregate first with Group by",
                          _with({"column": time, "time": at}, label))
    pos = period - period[0]
    del num, period
    whole = np.round(pos)
    pos -= whole                               # the distance to the grid, in place: one array of the series at a time
    np.abs(pos, out=pos)
    off = np.flatnonzero(pos > 1e-9)
    del pos
    if off.size:
        at = axis.label(ts[off[0]])
        unit = UNIT[axis.frequency]
        raise Refusal("irregular_time", f"the times of «{time}» are not whole {unit} apart ({at}){_in(label)}: aggregate them "
                                        f"by {unit.removesuffix('s')} first with Group by, or choose another frequency",
                      _with({"column": time, "time": at}, label))
    pos = whole.astype(np.int64)
    del whole
    pos += 1
    periods = int(pos[-1])                     # the times are sorted: the last place is the grid's length
    if periods > max_periods:
        raise Refusal("too_many_periods", f"the times of «{time}» span {periods} {UNIT[axis.frequency]}{_in(label)} (at most "
                                          f"{max_periods}): is the frequency right?", _with({"periods": periods}, label))
    return order, pos, ts


def _linear(z: np.ndarray, gaps: np.ndarray) -> None:
    """stats::approx between the two observed neighbours of each gap, in R's arithmetic:
    y[i] + (y[j] − y[i]) · ((v − x[i]) / (x[j] − x[i])). A gap is inside the span: it has both."""
    idx = np.flatnonzero(~np.isnan(z))
    j = np.searchsorted(idx, gaps)
    left, right = idx[j - 1], idx[j]
    xl, xr, v = left.astype(np.float64), right.astype(np.float64), gaps.astype(np.float64)
    z[gaps] = z[left] + (z[right] - z[left]) * ((v - xl) / (xr - xl))


def series(cols: Columns, rows: np.ndarray | None = None, missing: str = "refuse", *, label: str | None = None,
           fills: Sequence[str] = ("linear", "model"), max_periods: int = TS_MAX_PERIODS) -> Series:
    """One series (the rows given, the whole table without them) on its grid, ts_series' rules in its order: no value
    (`no_values`), the grid (`duplicate_time`, `irregular_time`, `too_many_periods`), the span, the periods inside it
    without a value (`gaps` with missing = refuse; `linear` fills them; `model` leaves them empty, NaN, for a model
    that reads its missing values). `label` names the series in the sentences (none: one series, R's sentences word
    for word); `fills` are the node's other choices of `missing`, named by the `gaps` sentence's way out."""
    if missing not in MISSING:
        raise ValueError(f"unknown missing {missing}")
    if rows is None:
        t_all, has, v_all, row_ids = cols.ticks, cols.has_time, cols.values, None
    else:
        t_all, has, v_all, row_ids = cols.ticks[rows], cols.has_time[rows], cols.values[rows], rows
    every = bool(has.all())                    # no empty time: no index of the rows with one
    with_time = None if every else np.flatnonzero(has)
    n_no_time = 0 if every else int(has.size - with_time.size)
    t = t_all if every else t_all[with_time]
    if t.size == 0 or bool(np.isnan(v_all if every else v_all[with_time]).all()):
        raise Refusal("no_values", f"the column «{cols.value}» has no value to model{_in(label)}",
                      _with({"column": cols.value}, label))
    axis = cols.axis
    order, pos, ts = _grid(axis, t, cols.time, max_periods, label)
    del t
    at = order if every else with_time[order]  # the series' own row numbers, in time order
    v = v_all[at]
    seen = ~np.isnan(v)
    # the places are increasing: the observed span is the rows lo … hi of the time order, those before it lo rows,
    # those after it the rest (no mask of the series' length)
    lo = int(np.argmax(seen))
    hi = seen.size - 1 - int(np.argmax(seen[::-1]))
    del seen
    first, last = int(pos[lo]), int(pos[hi])
    n = last - first + 1
    times = axis.after(ts[0], np.arange(first - 1, last, dtype=np.int64))
    p_in = pos[lo:hi + 1] - first
    times[p_in] = ts[lo:hi + 1]
    y = np.full(n, np.nan)
    y[p_in] = v[lo:hi + 1]
    ids = at if row_ids is None else row_ids[at]
    span_rows = np.full(n, -1, dtype=np.int64)
    span_rows[p_in] = ids[lo:hi + 1]
    del p_in, v
    gaps = np.flatnonzero(np.isnan(y))
    if gaps.size and missing == "refuse":
        when = axis.label(times[gaps[0]])
        unit = "period" if gaps.size == 1 else "periods"
        who = "the series" if label is None else f"the series «{label}»"
        ways = " or ".join(fills)
        raise Refusal("gaps", f"{who} has {gaps.size} {unit} without a value inside its span (the first: {when}): fill them in "
                              f"the flow (a calendar Left join and Fill nulls with 0 when an empty {ONE[axis.frequency]} means "
                              f"none), or choose missing = {ways}", _with({"n": int(gaps.size), "time": when}, label))
    filled = np.zeros(n, dtype=bool)
    z = y
    if gaps.size and missing == "linear":
        filled[gaps] = True
        z = y.copy()
        _linear(z, gaps)
    return Series(axis=axis, time=cols.time, value=cols.value, label=label, missing=missing, times=times, y=y, z=z,
                  filled=filled, span_rows=span_rows, after_rows=ids[hi + 1:].copy(), after_pos=pos[hi + 1:] - last,
                  grid_start=ts[0], first=first, n_no_time=n_no_time, n_before=lo, n_after=int(pos.size - hi - 1),
                  n_filled=int(gaps.size) if missing == "linear" else 0,
                  n_model_missing=int(gaps.size) if missing == "model" else 0)


# ── the output's time ─────────────────────────────────────────────────────────
def time_array(ticks: np.ndarray, axis: Axis):
    """ts_time_array: the ticks as an Arrow array of the input field's own type (Int64, Int32, Int16, date32,
    timestamp with its unit and zone, double), cast safely: a value that does not fit is an error, never a change."""
    import pyarrow as pa
    import pyarrow.types as pt

    t = axis.type
    if axis.storage == "days":
        a = pa.array(np.asarray(ticks, dtype=np.int32), type=pa.date32())
        return a if pt.is_date32(t) else a.cast(t)
    if axis.storage == "ticks":
        return pa.array(np.asarray(ticks, dtype=np.int64), type=t)
    if axis.storage == "int":
        a = pa.array(np.asarray(ticks, dtype=np.int64))
        return a if a.type == t else a.cast(t, safe=True)
    a = pa.array(np.asarray(ticks, dtype=np.float64))
    return a if a.type == t else a.cast(t, safe=True)


# ── a long series for its chart ───────────────────────────────────────────────
def envelope(y: np.ndarray, max_points: int = 2000) -> np.ndarray:
    """ts_envelope: the places of y to draw (0-based, in time order): all of them up to `max_points`; above it ⌊max/2⌋
    buckets of (nearly) equal length, each keeping its lowest and its highest value (one place when they are the same,
    the first of equal values), so that a spike a plain thinning would step over stays. Empty values are never kept."""
    y = np.asarray(y, dtype=np.float64)
    n = y.size
    max_points = max(2, int(max_points))
    ok = ~np.isnan(y)
    if n <= max_points:
        return np.flatnonzero(ok)
    buckets = max_points // 2
    i = np.flatnonzero(ok)
    b = (i * buckets) // n
    v = y[i]
    low = np.lexsort((i, v, b))
    high = np.lexsort((i, -v, b))
    first_low = low[np.r_[True, b[low][1:] != b[low][:-1]]] if low.size else low
    first_high = high[np.r_[True, b[high][1:] != b[high][:-1]]] if high.size else high
    return np.unique(np.concatenate((i[first_low], i[first_high])))
