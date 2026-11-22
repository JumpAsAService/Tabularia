"""The Python series against R's (docs/specs/foundation-forecast.md, lot F2): common/py/tab_series.py and
groups/timeseries/runner/R/ts_series.R fed the same Parquet files, their outputs compared exactly — the counts, the
codes and sentences of the refusals, the span's times as Arrow arrays of the input's own type, the values, the filled
places, the x of a chart, the envelope's places. compare_series.sh runs the three steps, each in its image:

  python compare_series.py cases   <work>   (any Python with pyarrow: the foundation_forecast image) the inputs
  python compare_series.py python  <work>   (the foundation_forecast image) tab_series.py on each case
  Rscript compare_series.R         <work>   (the timeseries image) ts_series.R on each case
  python compare_series.py compare <work>   the two compared; exit 1 on the first difference listed

The modelgroups folder is the environment's TABULARIA_MODELGROUPS (default: three folders up from this file). The
cases are the self-test's of ts_series.R (dec_types.csv as eight Arrow types, dec_gaps.csv, the refusals), the
foundation forecast's table of types (ff_types.csv: three series, six time types, read as one table and split), and
a few grids that test the calendar (the end of a month, a quarter, a year, an instant in Rome, an index). A case with
missing = model has no R twin: it is compared with R's linear case of the same input, whose values before the fill
and whose times it must equal.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.feather as feather
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
MG = Path(os.environ.get("TABULARIA_MODELGROUPS") or HERE.parents[2])
TS_DATA = MG / "groups" / "timeseries" / "reference" / "data"
FF_DATA = MG / "groups" / "foundation_forecast" / "reference" / "data"
R_FILLS = ("linear", "seasonal")  # the decomposition's other choices of `missing`, which ts_series.R's sentence names


def _csv(path: Path) -> dict[str, list[str | None]]:
    """A reference CSV as texts, column by column (an empty cell is None)."""
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return {name: [r[name] if r[name] != "" else None for r in rows] for name in rows[0]}


def _hours(xs, unit: str, tz: str | None):
    """«2026-01-05 13:00:00» read as UTC, as a timestamp of `unit` and zone `tz`."""
    secs = [None if x is None else int(dt.datetime.fromisoformat(x).replace(tzinfo=dt.UTC).timestamp()) for x in xs]
    per = {"s": 1, "ms": 1000, "us": 1_000_000, "ns": 1_000_000_000}[unit]
    return pa.array([None if s is None else s * per for s in secs], type=pa.int64()).cast(pa.timestamp(unit, tz))


def _dates(xs):
    return pa.array([None if x is None else dt.date.fromisoformat(x) for x in xs], type=pa.date32())


def _numbers(xs):
    return pa.array([None if x is None else float(x) for x in xs], type=pa.float64())


def _time_types(raw: dict[str, list]) -> dict[str, tuple[pa.Array, str]]:
    """The time columns of dec_types.csv or ff_types.csv as each Arrow type, with its frequency."""
    ints = [int(x) for x in raw["t_int"]]
    return {
        "int64": (pa.array([int(x) for x in raw["t_int64"]], type=pa.int64()), "day"),
        "int32": (pa.array(ints, type=pa.int32()), "day"),
        "int16": (pa.array(ints, type=pa.int16()), "day"),
        "date32": (_dates(raw["t_date"]), "day"),
        "ts_us_utc": (_hours(raw["t_hour"], "us", "UTC"), "hour"),
        "ts_ns": (_hours(raw["t_hour"], "ns", None), "hour"),
        "ts_ms_rome": (_hours(raw["t_hour"], "ms", "Europe/Rome"), "hour"),
        "double": (pa.array([float(x) for x in ints], type=pa.float64()), "day"),
    }


# ── the cases ─────────────────────────────────────────────────────────────────
def cases(work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []

    def add(cid: str, table: pa.Table, frequency: str, missing: str, time: str = "t", value: str = "v", **extra) -> None:
        name = f"in_{cid}.parquet"
        pq.write_table(table, work / name)
        out.append({"id": cid, "input": name, "time": time, "value": value, "frequency": frequency, "missing": missing, **extra})

    # dec_types: the self-test's eight types, the row 60 dropped, the values of rows 30 and 31 empty
    raw = _csv(TS_DATA / "dec_types.csv")
    keep = [i for i in range(len(raw["t_int"])) if i != 59]
    value = _numbers(raw["value"]).take(keep)
    for name, (t, frequency) in _time_types(raw).items():
        table = pa.table({"t": t.take(keep), "v": value})
        for missing in ("linear", "model") + (("refuse",) if name in ("int64", "date32", "ts_ms_rome", "double") else ()):
            add(f"dec_types_{name}_{missing}", table, frequency, missing)

    # dec_gaps: months, the first empty, two months without a row, one empty value
    raw = _csv(TS_DATA / "dec_gaps.csv")
    gaps = pa.table({"month": _dates(raw["month"]), "boxes": _numbers(raw["boxes"])})
    for missing in ("refuse", "linear", "model"):
        add(f"dec_gaps_{missing}", gaps, "month", missing, "month", "boxes")
    # the refusals of the self-test, and more
    add("refuse_duplicate", pa.concat_tables([gaps, gaps.slice(2, 1)]), "month", "linear", "month", "boxes")
    weeks = [dt.date(2024, 1, 1) + dt.timedelta(weeks=k) for k in range(20)]
    weeks[3] += dt.timedelta(days=3)
    add("refuse_irregular_week", pa.table({"t": pa.array(weeks, pa.date32()), "v": [k + 1.5 for k in range(20)]}), "week", "linear")
    add("refuse_frequency_hour", gaps, "hour", "linear", "month", "boxes")
    add("refuse_time_type", pa.table({"month": pc.cast(gaps.column("month"), pa.string()), "boxes": gaps.column("boxes")}),
        "month", "linear", "month", "boxes")
    add("refuse_no_values", pa.table({"month": gaps.column("month"), "boxes": pa.nulls(gaps.num_rows, pa.float64())}),
        "month", "linear", "month", "boxes")
    add("refuse_not_a_number", pa.table({"month": gaps.column("month"), "boxes": pc.cast(gaps.column("boxes"), pa.string())}),
        "month", "linear", "month", "boxes")
    add("refuse_no_column", gaps, "month", "linear", "nope", "boxes")
    boxes = gaps.column("boxes").to_pylist()
    boxes[7] = math.inf
    add("refuse_infinite", pa.table({"month": gaps.column("month"), "boxes": pa.array(boxes, pa.float64())}), "month", "linear",
        "month", "boxes")
    add("refuse_too_many_periods", gaps, "month", "linear", "month", "boxes", max_periods=50)
    add("refuse_bool_time", pa.table({"t": [True, False, True], "v": [1.0, 2.0, 3.0]}), "day", "linear")
    add("refuse_fractional_time", pa.table({"t": [1.0, 2.5, 3.0], "v": [1.0, 2.0, 3.0]}), "day", "linear")
    hours = [f"2026-02-0{d} 09:00:00" for d in range(1, 8)]
    hours[4] = "2026-02-05 13:00:00"
    add("refuse_irregular_day_datetime", pa.table({"t": _hours(hours, "us", "UTC"), "v": [float(k) for k in range(7)]}),
        "day", "linear")
    add("refuse_duplicate_month_datetime", pa.table({"t": _hours(["2026-01-03 00:00:00", "2026-02-01 00:00:00",
                                                                   "2026-02-27 12:00:00"], "s", None), "v": [1.0, 2.0, 3.0]}),
        "month", "linear")
    add("refuse_duplicate_int64", pa.table({"t": pa.array([5_000_000_001, 5_000_000_000, 5_000_000_001], pa.int64()),
                                            "v": [1.0, 2.0, 3.0]}), "day", "linear")
    # a time without a value, rows without a time
    months = gaps.column("month").to_pylist()
    months[5] = None
    months[40] = None
    add("null_times", pa.table({"month": pa.array(months, pa.date32()), "boxes": gaps.column("boxes")}), "month", "linear",
        "month", "boxes")

    # the calendar: the end of a month, a quarter, a year, an instant in Rome, a week of instants, an index
    def dates(xs):
        return pa.array([dt.date.fromisoformat(x) for x in xs], pa.date32())

    month_end = ["2024-01-31", "2024-03-31", "2024-04-30", "2024-06-30", "2024-07-31", "2024-08-31", "2025-02-28"]
    add("month_end", pa.table({"t": dates(month_end), "v": [1.0, 3.0, 4.0, 6.0, 7.0, 8.0, 14.0]}), "month", "linear")
    add("month_end_rome", pa.table({"t": _hours(["2024-01-31 10:30:00", "2024-03-31 10:30:00", "2024-04-30 10:30:00",
                                                 "2024-07-31 10:30:00"], "us", "Europe/Rome"), "v": [2.0, 4.0, 3.0, 9.0]}),
        "month", "linear")
    add("quarter", pa.table({"t": dates(["2023-02-15", "2023-05-20", "2023-11-01", "2024-02-10", "2024-12-31"]),
                             "v": [10.0, 12.0, 11.0, 15.0, 9.5]}), "quarter", "linear")
    add("year", pa.table({"t": dates(["2018-06-30", "2019-06-30", "2021-01-01", "2022-06-30"]), "v": [1.0, 2.0, 4.0, 3.0]}),
        "year", "linear")
    add("week_seconds", pa.table({"t": _hours(["2026-01-04 06:00:00", "2026-01-11 06:00:00", "2026-01-25 06:00:00",
                                               "2026-02-01 06:00:00"], "s", None), "v": [1.0, 2.0, 5.0, 4.0]}), "week", "linear")
    add("index_double", pa.table({"t": [20.0, 3.0, 7.0, 10.0, 40.0], "v": [5.0, 1.0, None, 3.0, 2.0]}), "index", "linear")
    add("index_date", pa.table({"t": dates(["2026-01-09", "2026-01-01", "2026-01-02", "2026-01-30"]),
                                "v": [None, 2.0, None, 1.0]}), "index", "linear")
    add("hour_rome_dst", pa.table({"t": _hours(["2026-03-28 23:00:00", "2026-03-29 00:00:00", "2026-03-29 03:00:00",
                                                "2026-03-29 04:00:00"], "ms", "Europe/Rome"), "v": [1.0, 2.0, 8.0, 3.0]}),
        "hour", "linear")

    # ff_types: one table of three series; R reads each series alone, Python the table split by its series column
    raw = _csv(FF_DATA / "ff_types.csv")
    value = _numbers(raw["value"])
    for name, (t, frequency) in _time_types(raw).items():
        if name in ("int16", "double"):
            continue
        whole = pa.table({"s_text": pa.array(raw["s_text"]), "s_int64": pa.array([int(x) for x in raw["s_int64"]], pa.int64()),
                          "t": t, "v": value})
        pq.write_table(whole, work / f"ff_types_{name}.parquet")
        for label in sorted(set(raw["s_text"])):
            mask = pc.equal(whole.column("s_text"), label)
            part = whole.filter(mask).select(["t", "v"])
            for missing in ("refuse", "linear", "model"):
                add(f"ff_types_{name}_{label}_{missing}", part, frequency, missing, whole=f"ff_types_{name}.parquet",
                    series=label, rows=np.flatnonzero(mask.to_numpy(zero_copy_only=False)).tolist())

    # the envelope: R's self-test vectors, and ties
    walk = _numbers(_csv(TS_DATA / "dec_checks.csv")["walk"]).to_pylist()
    holed = list(walk)
    holed[10:16] = [None] * 6
    spike = [0.0] * 100_000
    spike[54_320] = 9.0
    ties = [1.0, 1.0, 2.0, 2.0, 1.0] * 200
    envelopes = {"walk_100": (walk, 100), "walk_holed_100": (holed, 100), "walk_401": (walk, 401), "walk_3": (walk, 3),
                 "spike_2000": (spike, 2000), "ties_50": (ties, 50), "ties_51": (ties, 51)}
    cols = {k: pa.array(v + [None] * (100_000 - len(v)), pa.float64()) for k, (v, _) in envelopes.items()}
    feather.write_feather(pa.table(cols), work / "envelopes.arrow")
    env = [{"id": k, "n": len(v), "max": m} for k, (v, m) in envelopes.items()]
    (work / "cases.json").write_text(json.dumps({"cases": out, "envelopes": env}, indent=1), encoding="utf-8")
    print(f"cases: {len(out)} series, {len(env)} envelopes")


# ── the Python side ───────────────────────────────────────────────────────────
def python(work: Path) -> None:
    sys.path[:0] = [str(MG / "common" / "py")]
    import tab_series as ts
    from tab_protocol import Refusal

    spec = json.loads((work / "cases.json").read_text(encoding="utf-8"))
    results = {}
    splits: dict[str, tuple] = {}
    for case in spec["cases"]:
        cid = case["id"]
        try:
            if "whole" in case:  # the table of many series, split by its series column
                if case["whole"] not in splits:
                    os.environ["TABULARIA_INPUT"] = str(work / case["whole"])
                    table = ts.read_table({}, ["s_text", "t", "v"])
                    labels, rows, empty = ts.split(table.column("s_text"))
                    splits[case["whole"]] = (table, dict(zip(labels, rows)), empty)
                table, by_label, empty = splits[case["whole"]]
                rows = by_label[case["series"]]
                assert rows.tolist() == case["rows"] and empty.size == 0, f"{cid}: the split's rows"
                cols = ts.columns(table, case["time"], case["value"], case["frequency"])
            else:
                os.environ["TABULARIA_INPUT"] = str(work / case["input"])
                table = ts.read_table({}, [case["time"], case["value"]])
                cols = ts.columns(table, case["time"], case["value"], case["frequency"])
                rows = None
            s = ts.series(cols, rows, case["missing"], fills=R_FILLS, max_periods=case.get("max_periods", ts.TS_MAX_PERIODS))
            feather.write_feather(pa.table({"time": s.time_array(), "y": s.y, "z": s.z, "filled": s.filled, "x0": s.x0()}),
                                  work / f"py_{cid}.arrow")
            results[cid] = {"ok": True, "kind": s.kind, "n": s.n, "n_missing": s.n_missing,
                            "n_filled": s.n_filled if case["missing"] != "model" else s.n_model_missing}
        except Refusal as e:
            results[cid] = {"ok": False, "code": e.code, "message": e.message, "params": e.params}
    env = feather.read_table(work / "envelopes.arrow")
    results["envelopes"] = {e["id"]: (ts.envelope(env.column(e["id"]).to_numpy(zero_copy_only=False)[:e["n"]], e["max"]) + 1).tolist()
                            for e in spec["envelopes"]}
    (work / "py_out.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    print(f"python: {len(results) - 1} series")


# ── the comparison ────────────────────────────────────────────────────────────
def _same_doubles(a: np.ndarray, b: np.ndarray) -> bool:
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if a.shape != b.shape or not np.array_equal(np.isnan(a), np.isnan(b)):
        return False
    ok = ~np.isnan(a)
    return bool(np.array_equal(a[ok].view(np.int64), b[ok].view(np.int64)))


def _int64_label(p: dict, q: dict) -> bool:
    """R's one known difference: tab_label (common/R/columns.R) writes a 64-bit integer (bit64's integer64, which
    ts_read asks arrow for) as a double, «5000000029.0», where the engine and tab_series write «5000000029». The
    sentences are otherwise the same, word for word."""
    t = (p.get("params") or {}).get("time")
    if p.get("ok", True) or q.get("ok", True) or t is None or not t.lstrip("-").isdigit():
        return False
    dotted = {**p, "message": p["message"].replace(t, t + ".0"), "params": {**p["params"], "time": t + ".0"}}
    return dotted == q


def compare(work: Path) -> int:
    spec = json.loads((work / "cases.json").read_text(encoding="utf-8"))
    py = json.loads((work / "py_out.json").read_text(encoding="utf-8"))
    r = json.loads((work / "r_out.json").read_text(encoding="utf-8"))
    problems: list[str] = []
    known: list[str] = []
    counts = {"series": 0, "refusals": 0, "model": 0, "values": 0}
    for case in spec["cases"]:
        cid = case["id"]
        model = case["missing"] == "model"
        twin = cid.removesuffix("_model") + "_linear" if model else cid
        p, q = py[cid], r[twin]
        if not q.get("ok", False) and model and q.get("code") != "gaps":
            # an input R refuses before its gaps is refused the same way by any choice of missing
            if p != q:
                problems.append(f"{cid}: python {p}, R {q}")
            continue
        if not p["ok"] or not q["ok"]:
            if p != q and "int64" in cid and _int64_label(p, q):
                known.append(f"{cid}: «{p['params']['time']}», R «{q['params']['time']}»")
            elif p != q:
                problems.append(f"{cid}: python {p}\n    R      {q}")
            counts["refusals"] += 1
            continue
        if p != q:
            problems.append(f"{cid}: python {p}, R {q}")
        a = feather.read_table(work / f"py_{cid}.arrow")
        b = feather.read_table(work / f"r_{twin}.arrow")
        ta, tb = a.column("time").combine_chunks(), b.column("time").combine_chunks()
        if ta.type != tb.type or not ta.equals(tb):
            problems.append(f"{cid}: the times differ: {ta.type} {ta.to_pylist()[:4]}… against R's {tb.type} {tb.to_pylist()[:4]}…")
        for name in ("y", "x0") + (() if model else ("z",)):
            if not _same_doubles(a.column(name).to_numpy(), b.column(name).to_numpy()):
                problems.append(f"{cid}: {name} differs")
        fa = a.column("filled").to_numpy(zero_copy_only=False)
        fb = b.column("filled").to_numpy(zero_copy_only=False)
        if model:
            za = a.column("z").to_numpy()
            if fa.any() or not np.array_equal(np.isnan(za), fb) or not _same_doubles(za, a.column("y").to_numpy()):
                problems.append(f"{cid}: missing = model must leave R's filled places empty")
            counts["model"] += 1
        elif not np.array_equal(fa, fb):
            problems.append(f"{cid}: filled differs")
        counts["series"] += 1
        counts["values"] += a.num_rows
    for e in spec["envelopes"]:
        if py["envelopes"][e["id"]] != r["envelopes"][e["id"]]:
            problems.append(f"envelope {e['id']}: python {py['envelopes'][e['id']][:10]}…, R {r['envelopes'][e['id']][:10]}…")
    for line in known:
        print("KNOWN (R labels an Int64 time as a double) " + line)
    for line in problems:
        print("DIFFERENT " + line)
    print(f"compared: {len(spec['cases'])} cases ({counts['series']} series with {counts['values']} places, of which "
          f"{counts['model']} with missing = model against R's linear; {counts['refusals']} refusals), "
          f"{len(spec['envelopes'])} envelopes; {len(problems)} differences, {len(known)} known")
    return 1 if problems else 0


if __name__ == "__main__":
    step, where = sys.argv[1], Path(sys.argv[2])
    if step == "cases":
        cases(where)
    elif step == "python":
        python(where)
    elif step == "compare":
        sys.exit(compare(where))
    else:
        sys.exit(f"unknown step {step}")
