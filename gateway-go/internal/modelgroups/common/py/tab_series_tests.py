"""The self-test of the series (tab_series.py, lot F2 of docs/specs/foundation-forecast.md): registered with the
library's own tests by tab_selftest, so every Python group's image checks the module it ships. Each table is built
here, small, with the answer written by hand; the comparison with ts_series.R on the reference tables of both groups
is groups/foundation_forecast/reference/compare_series.py's, outside the images' self-test.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

import tab_series as ts
from tab_selftest import _expect, _refused_with, register

T = "a`b"           # a time column whose name looks like code
V = 'x); system("id")'


def _days(n: int, start: dt.date = dt.date(2026, 1, 5)) -> list[dt.date]:
    return [start + dt.timedelta(days=k) for k in range(n)]


def _table(t, v, **more):
    import pyarrow as pa

    return pa.table({T: t, V: pa.array(v, pa.float64()), **more})


def _one(table, frequency: str, missing: str = "linear", **kw) -> ts.Series:
    return ts.series(ts.columns(table, T, V, frequency), None, missing, **kw)


@register("series: the time keeps its type, an Int64 every digit, a timestamp its unit and zone; gaps filled on the grid")
def _types(code: Path) -> None:
    import pyarrow as pa

    n = 100
    keep = [k for k in range(n) if k != 59]           # a day without a row; the values of 29 and 30 empty
    value = [None if k in (29, 30) else 50.0 + (k % 7) for k in range(n)]
    secs = [int(dt.datetime(2026, 1, 5, tzinfo=dt.UTC).timestamp()) + 3600 * k for k in range(n)]
    types = {
        "int64": (pa.array([5_000_000_000 + k for k in range(n)], pa.int64()), "day"),
        "int32": (pa.array(range(n), pa.int32()), "day"),
        "int16": (pa.array(range(n), pa.int16()), "day"),
        "date32": (pa.array(_days(n), pa.date32()), "day"),
        "ts_us_utc": (pa.array([s * 1_000_000 for s in secs], pa.int64()).cast(pa.timestamp("us", "UTC")), "hour"),
        "ts_ns": (pa.array([s * 1_000_000_000 for s in secs], pa.int64()).cast(pa.timestamp("ns")), "hour"),
        "ts_ms_rome": (pa.array([s * 1000 for s in secs], pa.int64()).cast(pa.timestamp("ms", "Europe/Rome")), "hour"),
        "double": (pa.array([float(k) for k in range(n)], pa.float64()), "day"),
    }
    for name, (full, frequency) in types.items():
        table = _table(full.take(keep), [value[k] for k in keep])
        s = _one(table, frequency)
        _expect(s.n == n and s.n_filled == 3 and s.filled.nonzero()[0].tolist() == [29, 30, 59] and s.n_missing == 0, f"{name}: the gaps")
        arr = s.time_array()
        _expect(arr.type == full.type, f"{name}: the output's time is {arr.type}, the input's {full.type}")
        _expect(arr.equals(full), f"{name}: the times, filled ones included, are the grid's: {arr.to_pylist()[:3]}")
        want = [value[28] + (value[31] - value[28]) * (k - 28) / 3 for k in (29, 30)] + [value[58] + (value[60] - value[58]) / 2]
        _expect(np.allclose(s.z[[29, 30, 59]], want, rtol=1e-15, atol=0) and np.isnan(s.y[[29, 30, 59]]).all(), f"{name}: the line")
        _expect(np.array_equal(s.z[~s.filled], s.y[~s.filled]), f"{name}: the observed values are kept")
    s = _one(_table(types["int64"][0].take(keep), [value[k] for k in keep]), "day")
    _expect(s.time_array().to_pylist()[-1] == 5_000_000_099 and s.time_array().type == pa.int64(), "an Int64 above 2^31 keeps every digit")
    _expect(s.x0()[0] == 5e9 and ts.time_array(s.after([1, 2]), s.axis).to_pylist() == [5_000_000_100, 5_000_000_101], "the x, the future")
    s = _one(_table(types["ts_ms_rome"][0].take(keep), [value[k] for k in keep]), "hour")
    after = s.time_array(s.after(np.arange(1, 4)))
    _expect(after.type == pa.timestamp("ms", "Europe/Rome") and after.to_pylist()[0] == types["ts_ms_rome"][0][99].as_py() + dt.timedelta(hours=1),
            f"the future instants in the input's zone: {after.to_pylist()[:1]}")


@register("series: gaps are refused with R's sentence (naming the series), filled by a straight line, or left for the model")
def _gaps(code: Path) -> None:
    import pyarrow as pa

    months = [dt.date(2015 + m // 12, m % 12 + 1, 1) for m in range(24)]
    value = [None] + [100.0 + m for m in range(1, 24)]
    rows = [m for m in range(24) if m not in (5, 9)]  # two months without a row
    value[14] = None                                   # one empty value
    table = _table(pa.array([months[m] for m in rows], pa.date32()), [value[m] for m in rows])
    msg = _refused_with("gaps", lambda: _one(table, "month", "refuse", fills=("linear", "seasonal")))
    _expect(msg == "the series has 3 periods without a value inside its span (the first: 2015-06-01): fill them in the flow "
                   "(a calendar Left join and Fill nulls with 0 when an empty month means none), or choose missing = linear or "
                   "seasonal", msg)
    msg = _refused_with("gaps", lambda: _one(table, "month", "refuse", label="Online"))
    _expect(msg.startswith("the series «Online» has 3 periods") and msg.endswith("or choose missing = linear or model"), msg)
    s = _one(table, "month", "linear")
    _expect(s.n == 23 and s.n_missing == 1 and s.n_before == 1 and s.n_filled == 3 and s.filled.nonzero()[0].tolist() == [4, 8, 13],
            f"linear: n {s.n}, n_missing {s.n_missing}, filled {s.filled.nonzero()[0].tolist()}")
    _expect(s.z[[4, 8, 13]].tolist() == [105.0, 109.0, 114.0], f"linear: {s.z[[4, 8, 13]].tolist()}")
    _expect(s.time_array().to_pylist()[4] == dt.date(2015, 6, 1) and s.time_array().to_pylist()[0] == dt.date(2015, 2, 1), "the span")
    m = _one(table, "month", "model")
    _expect(m.n_filled == 0 and m.n_model_missing == 3 and not m.filled.any() and np.isnan(m.z[[4, 8, 13]]).all() and m.z is m.y,
            "model: the places stay empty, not copied")
    _expect(m.span_rows.tolist()[4] == -1 and m.span_rows.tolist()[0] == 1, f"the rows of the span: {m.span_rows.tolist()[:6]}")


@register("series: the refusals of ts_series.R, word for word")
def _refusals(code: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    days = pa.array(_days(6), pa.date32())
    ok = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    twice = _table(pa.array(_days(3) + _days(1), pa.date32()), [1.0, 2.0, 3.0, 4.0])
    msg = _refused_with("duplicate_time", lambda: _one(twice, "day"))
    _expect(msg == f"the column «{T}» holds two rows for the period of 2026-01-05: a series has one row per period; aggregate first "
                   "with Group by", msg)
    msg = _refused_with("duplicate_time", lambda: _one(twice, "day", label="Online"))
    _expect("for the period of 2026-01-05 in the series «Online»: a series" in msg, msg)
    weeks = [dt.date(2024, 1, 1) + dt.timedelta(weeks=k) for k in range(6)]
    weeks[3] += dt.timedelta(days=3)
    msg = _refused_with("irregular_time", lambda: _one(_table(pa.array(weeks, pa.date32()), ok), "week"))
    _expect(msg == f"the times of «{T}» are not whole weeks apart (2024-01-25): aggregate them by week first with Group by, or "
                   "choose another frequency", msg)
    msg = _refused_with("frequency_hour", lambda: _one(_table(days, ok), "hour"))
    _expect(msg == f"the column «{T}» holds dates, without hours: choose frequency = day, or a datetime column", msg)
    for bad in (pa.array(["2026-01-05"] * 6), pa.array([True] * 6), pa.array([0.5 + k for k in range(6)])):
        msg = _refused_with("time_type", lambda bad=bad: _one(_table(bad, ok), "day"))
        _expect(msg == f"the column «{T}» is not a date, a datetime or an integer: the time of a series must be one of them", msg)
    msg = _refused_with("not_a_number", lambda: ts.columns(pa.table({T: days, V: pa.array(["1"] * 6)}), T, V, "day"))
    _expect(msg == f"the column «{V}» is not a number", msg)
    msg = _refused_with("infinite", lambda: _one(_table(days, ok[:5] + [float("inf")]), "day"))
    _expect(msg == f"the column «{V}» holds an infinite value: filter it out or replace it before the node", msg)
    msg = _refused_with("no_values", lambda: _one(_table(days, [None] * 6), "day"))
    _expect(msg == f"the column «{V}» has no value to model", msg)
    msg = _refused_with("too_many_periods", lambda: _one(_table(days, ok), "day", max_periods=5))
    _expect(msg == f"the times of «{T}» span 6 days (at most 5): is the frequency right?", msg)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.parquet")
        pq.write_table(_table(days, ok, other=pa.array(range(6))), path)
        old = os.environ.get("TABULARIA_INPUT")
        os.environ["TABULARIA_INPUT"] = path
        try:
            msg = _refused_with("no_column", lambda: ts.read_table({}, [T, "nope"]))
            _expect(msg == "the input has no column «nope»", msg)
            got = ts.read_table({}, [V, T, V])
            _expect(got.column_names == [V, T] and got.column(T).type == pa.date32(), f"the columns asked for: {got.column_names}")
        finally:
            if old is None:
                os.environ.pop("TABULARIA_INPUT", None)
            else:
                os.environ["TABULARIA_INPUT"] = old
    _refused_with("no_column", lambda: ts.read_table({"_table": _table(days, ok)}, [T, "nope"]))


@register("series: a table of many series is split in text order; each has its own span, end and future rows")
def _many(code: Path) -> None:
    import pyarrow as pa

    # three series in an order that is not the time's; «b» ends early, «a» has two future rows, one row has no series
    rows = [("b", 3, 2.0), ("a", 1, 1.0), ("a", 4, None), ("B", 2, 5.0), ("a", 2, 2.0), ("b", 1, None), ("a", 3, 3.0),
            ("a", 5, None), (None, 1, 9.0), ("b", 2, 1.0), ("B", 3, 6.0)]
    names = pa.array([r[0] for r in rows])
    codes = pa.array([None if r[0] is None else 3_000_000_000 + ord(r[0]) for r in rows], pa.int64())
    month_ends = {1: dt.date(2024, 1, 31), 2: dt.date(2024, 2, 29), 3: dt.date(2024, 3, 31), 4: dt.date(2024, 4, 30),
                  5: dt.date(2024, 5, 31)}
    table = _table(pa.array([month_ends[r[1]] for r in rows], pa.date32()), [r[2] for r in rows], s=names)
    labels, parts, empty = ts.split(table.column("s"))
    _expect(labels == ["B", "a", "b"] and empty.tolist() == [8], f"the labels: {labels}, empty {empty.tolist()}")
    _expect([p.tolist() for p in parts] == [[3, 10], [1, 2, 4, 6, 7], [0, 5, 9]], f"the rows: {[p.tolist() for p in parts]}")
    labels64, _, _ = ts.split(codes)
    _expect(labels64 == ["3000000066", "3000000097", "3000000098"], f"Int64 labels: {labels64}")
    cols = ts.columns(table, T, V, "month")
    a = ts.series(cols, parts[1], "refuse", label="a")
    _expect(a.n == 3 and a.n_after == 2 and a.after_rows.tolist() == [2, 7] and a.after_pos.tolist() == [1, 2] and a.n_missing == 2,
            f"«a»: n {a.n}, after {a.after_rows.tolist()} {a.after_pos.tolist()}")
    _expect(ts.time_array(a.after([1, 2]), a.axis).to_pylist() == [dt.date(2024, 4, 30), dt.date(2024, 5, 31)],
            "the future of a series from 31 January keeps the months' last days")
    b = ts.series(cols, parts[2], "refuse", label="b")
    _expect(b.n == 2 and b.n_before == 1 and b.span_rows.tolist() == [9, 0] and b.time_array().to_pylist()[-1] == dt.date(2024, 3, 31),
            f"«b»: n {b.n}, rows {b.span_rows.tolist()}")


@register("series: a long series keeps each bucket's lowest and highest place for its chart")
def _envelope(code: Path) -> None:
    y = np.array([1.0, 1.0, 2.0, 2.0, 1.0] * 4)
    _expect(ts.envelope(y, 4).tolist() == [0, 2, 10, 12], f"ties: {ts.envelope(y, 4).tolist()}")
    y[[0, 2]] = np.nan
    _expect(ts.envelope(y, 4).tolist() == [1, 3, 10, 12], "empty values are never kept")
    _expect(ts.envelope(y, 20).tolist() == [k for k in range(20) if k not in (0, 2)], "all of it, without the empty values")
    spike = np.zeros(100_000)
    spike[54_320] = 9.0
    kept = ts.envelope(spike, 2000)
    _expect(54_320 in kept and kept.size <= 2000, "the spike")


@register("series: the runner's library and the series work in an image without pandas")
def _no_pandas(code: Path) -> None:
    # pyarrow imports pandas by itself whenever it is installed (its pandas shim, on pa.array or a Parquet read): the
    # probe hides it, as an image without it would, and reads, splits and writes a series with what is left
    probe = ("import sys\n"
             "class Hide:\n"
             "    def find_spec(self, name, path=None, target=None):\n"
             "        if name.split('.')[0] == 'pandas':\n"
             "            raise ModuleNotFoundError(\"No module named 'pandas'\")\n"
             "sys.meta_path.insert(0, Hide())\n"
             "sys.path[:0] = [sys.argv[1]]\n"
             "import os, tempfile, numpy as np, pyarrow as pa, pyarrow.parquet as pq, tab_main, tab_series as ts\n"
             "t = pa.Table.from_arrays([pa.array(np.array([1, 2, 4], np.int64)), pa.array(np.array([1.0, 2.0, 4.0])),\n"
             "                          pa.array(np.array([7, 7, 7], np.int64))], names=['t', 'v', 's'])\n"
             "os.environ['TABULARIA_INPUT'] = path = os.path.join(tempfile.mkdtemp(), 'in.parquet')\n"
             "pq.write_table(t, path)\n"
             "t = ts.read_table({}, ['t', 'v', 's'])\n"
             "labels, rows, _ = ts.split(t.column('s'))\n"
             "s = ts.series(ts.columns(t, 't', 'v', 'day'), rows[0], 'linear', label=labels[0])\n"
             "tab_main.write_output(pa.Table.from_arrays([s.time_array(), pa.array(s.z)], names=['t', 'v']), path)\n"
             "print(labels, s.n, s.z[2], ts.envelope(s.z, 2).tolist(), 'pandas' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(code / "common" / "py")], capture_output=True, text=True,
                         timeout=120)
    _expect(out.returncode == 0 and out.stdout.strip() == "['7'] 4 3.0 [0, 3] False", f"{out.stdout.strip()} {out.stderr[-800:]}")
