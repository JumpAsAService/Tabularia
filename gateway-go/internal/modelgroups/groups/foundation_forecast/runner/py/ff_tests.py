"""The self-test of Forecast (Timestral) (docs/specs/foundation-forecast.md, «Self-test, reference values, witnesses»): the
checks the group's image runs besides the library's own, registered with tab_selftest. The refusals of reference/expected.json
(`cases`) are run by the library; the rest is here:

  - the weights in the image are the recipe's (config.json and LICENSE of each size present; the check Job hashes the weights);
  - the measures and Wilcoxon's test on fixed numbers against their witnesses (1e-12);
  - the plan: the ladder of contexts, the batches' constant size;
  - for each size the image holds, its reference cases (`sizes`): the runner's quantiles against the official package's,
    computed outside the image (within 1e-6 of the series' largest quantile), its measures, its test, its counts, its reading;
  - with the smallest size present: the same input twice gives the same bits, a series gives the same bits whatever the
    series beside it and the blocks, the runner's numbers are the vendored package's called directly on hand-made arrays,
    the quantiles are sorted and the symmetric averaging is odd, the time's and the series' types are kept, the refusals the
    reference tables cannot make (size_absent, too_many_series, too_slow and no output left), the results within
    max_result_rows.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

import ff_measures as fm
import ff_series as fs
import ff_timestral as ft
from tab_protocol import PROTOCOL, Refusal, catalog
from tab_results import check_declared
from tab_selftest import _expect, register

_CACHE: dict[str, Any] = {}


def _expected(code: Path) -> dict[str, Any]:
    if "expected" not in _CACHE:
        _CACHE["expected"] = json.loads((code / "reference" / "expected.json").read_text(encoding="utf-8"))
    return _CACHE["expected"]


def _task(params: dict[str, Any], *, frame: Any = None, table: Any = None, limits: dict[str, Any] | None = None,
          write_output: bool = False) -> dict[str, Any]:
    return {"protocol": PROTOCOL, "mode": "fit", "group": "foundation_forecast", "group_version": "selftest", "model": "timestral",
            "variant": "default", "params": params, "output": "quantile_forecast", "write_output": write_output, "rows_from": "run",
            "limits": {**catalog()["limits"], **(limits or {})}, "_frame": frame, "_table": table}


def _fit(task: dict[str, Any]) -> tuple[Any, Any]:
    """The runner on a task: its rows, and its output read back (None without write_output)."""
    import pyarrow.parquet as pq

    old = os.environ.get("TABULARIA_OUTPUT")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "output.parquet")
        os.environ["TABULARIA_OUTPUT"] = path
        try:
            got = ft.fit_timestral(task)
            out = pq.read_table(path) if task.get("write_output") else None
            if out is not None:
                _CACHE["row_groups"] = pq.ParquetFile(path).num_row_groups
        finally:
            if old is None:
                os.environ.pop("TABULARIA_OUTPUT", None)
            else:
                os.environ["TABULARIA_OUTPUT"] = old
    problems = check_declared(got["rows"], "timestral", "default")
    _expect(not problems, problems[0] if problems else "")
    return got["rows"], out


def _frame(code: Path, case: dict[str, Any]) -> Any:
    from tab_columns import read_csv

    return read_csv(str(code / "reference" / case["data"]), case.get("types") or {})


def _smallest() -> str | None:
    present = ft.sizes_present()
    return present[0] if present else None


def _with_size(code: Path, size: str, case_id: str) -> dict[str, Any]:
    case = next(c for c in _expected(code)["size_cases"] if c["id"] == case_id)
    return {**case, "params": {**case["params"], "size": size}}


# ── the weights ─────────────────────────────────────────────────────────────────
@register("foundation_forecast: the weights in the image are the recipe's (config.json, LICENSE)")
def _weights(code: Path) -> None:
    import hashlib

    present = ft.sizes_present()
    _expect(present, f"no size under {ft.WEIGHTS}")
    for size in present:
        for name, want in (("config.json", ft.SIZES[size]["config"]), ("LICENSE", ft.LICENCE["sha256"])):
            got = hashlib.sha256((ft.WEIGHTS / size / name).read_bytes()).hexdigest()
            _expect(got == want, f"{size}/{name}: sha256 {got}, expected {want}")


# ── the measures and the test, on fixed numbers ─────────────────────────────────
@register("foundation_forecast: the seasonal naïve, the scale and the measures against their witness")
def _measures(code: Path) -> None:
    cases = _expected(code)["measures"]
    _expect(cases, "no measures' case")
    for c in cases:
        hist = np.array([math.nan if x is None else x for x in c["history"]])
        base = fm.seasonal_naive(hist, len(c["actual"]), c["season"])
        want_base = np.array([math.nan if x is None else x for x in c["baseline"]])
        _expect(np.allclose(base, want_base, rtol=1e-12, atol=1e-12, equal_nan=True), f"the seasonal naïve (m = {c['season']})")
        got = fm.Measured.of(hist, np.array(c["actual"]), np.array(c["quantiles"]), base, c["season"])
        for k, want in c["expect"].items():
            if k == "reason":
                _expect(got.reason == want, f"reason {got.reason}")
                continue
            v = getattr(got, k)
            _expect(abs(v - want) <= 1e-12 * max(1.0, abs(want)), f"m = {c['season']}, {k}: {v!r}, expected {want!r}")


@register("foundation_forecast: Wilcoxon's signed-rank test against scipy (exact, sign patterns, normal)")
def _wilcoxon(code: Path) -> None:
    cases = _expected(code)["wilcoxon"]
    methods = set()
    for c in cases:
        w = fm.wilcoxon(np.array(c["d"]))
        methods.add(w.method)
        _expect(abs(w.statistic - c["statistic"]) <= 1e-12 * max(1, c["statistic"]), f"n {len(c['d'])}: W {w.statistic}")
        _expect(abs(w.p_value - c["p_value"]) <= 1e-12, f"n {len(c['d'])} ({w.method}): p {w.p_value!r}, scipy {c['p_value']!r}")
    _expect(methods == {"exact", "permutation", "normal"}, f"paths: {sorted(methods)}")
    _expect(fm.wilcoxon(np.zeros(3)) is None, "only zeros")


@register("foundation_forecast: the plan: contexts on a ladder, batches of one constant size a shape")
def _plan(code: Path) -> None:
    rungs = sorted({fs.bucket(v) for v in range(1, fs.MAX_CONTEXT + 1)})
    _expect(rungs[0] == 1 and rungs[-1] == fs.MAX_CONTEXT // fs.PATCH and len(rungs) == 36, f"rungs: {rungs}")
    _expect(all(fs.bucket(v) * fs.PATCH >= v for v in range(1, fs.MAX_CONTEXT + 1, 7)), "a context longer than its rung")
    _expect(max(fs.bucket(v) * fs.PATCH / v for v in range(257, fs.MAX_CONTEXT + 1, 3)) <= 1.25, "more than a quarter padded")
    _expect(fs.batch_rows(4, 28, 1) == 16 and fs.batch_rows(32, 28, 1) == 7 and fs.batch_rows(1024, 28, 1) == 1
            and fs.batch_rows(64, 28, 3) == 1 and fs.batch_rows(8, 28, 3) == 9, "batch sizes")


# ── the reference cases of each size ───────────────────────────────────────────
def _quantile(out: Any, q: dict[str, Any], step: int, level: float) -> float:
    names = out.column_names
    rows = out.to_pydict()
    flag = "is_holdout" if q["kind"] == "holdout" else "is_forecast"
    at = [i for i in range(out.num_rows) if rows[flag][i] and ("series" not in names or str(rows["series"][i]) == q["series"])]
    col = "forecast" if level == 0.5 else f"quantile_{fs._level_label(level)}"
    return float(rows[col][at[step - 1]])


def _check_case(code: Path, size: str, case: dict[str, Any], want: dict[str, Any], levels: list[float]) -> None:
    rows, out = _fit(_task({**case["params"], "size": size}, frame=_frame(code, case), write_output=True))
    for q in want["quantiles"]:
        for step, values in zip(q["steps"], q["values"]):
            for level, v in zip(levels, values):
                got = _quantile(out, q, step, level)
                _expect(abs(got - v) <= q["abs"], f"{case['id']}: {q['series']} {q['kind']} step {step} level {level}: {got!r}, "
                                                  f"the official package {v!r} (± {q['abs']:.3g})")
    at = {k: i for i, k in enumerate(rows.key)}
    for e in want["expect"]:
        i = at.get(e["key"])
        _expect(i is not None, f"{case['id']}: no row {e['key']}")
        if "text" in e:
            _expect(rows.text[i] == e["text"], f"{case['id']}: {e['key']} «{rows.text[i]}», expected «{e['text']}»")
        elif "id" in e:
            _expect((rows.text[i] or "").split(" ")[0] == "@" + e["id"], f"{case['id']}: {e['key']} {rows.text[i]}, expected @{e['id']}")
        else:
            got, v = rows.value[i], float(e["value"])
            tol = max(float(e.get("abs", 1e-12)), float(e.get("rel", 1e-9)) * abs(v))
            _expect(abs(got - v) <= tol, f"{case['id']}: {e['key']} {got!r}, expected {v!r} (± {tol:.3g})")


def _register_sizes() -> None:
    for size in ft.sizes_present():
        def check(code: Path, size: str = size) -> None:
            ref = _expected(code)
            smallest = _smallest()
            cases = {c["id"]: c for c in ref["size_cases"]}
            wanted = ref["sizes"].get(size)
            _expect(wanted, f"expected.json has no case of {size}")
            ran = 0
            for want in wanted:
                case = cases[want["id"]]
                if case["every_size"] or size == smallest:
                    _check_case(code, size, case, want, ref["check_levels"])
                    ran += 1
            _expect(ran, "no case ran")
            if size != smallest:
                ft.unload()                 # one size in memory at a time
        register(f"foundation_forecast {size}: the reference cases against the official package")(check)


_register_sizes()


# ── determinism and the batches (the smallest size present) ─────────────────────
@register("foundation_forecast: the same bits twice, whatever the series beside and the blocks; the vendored package's own")
def _determinism(code: Path) -> None:
    size = _smallest()
    case = _with_size(code, size, "three")
    params = {**case["params"], "quantiles": [0.05, 0.1, 0.9, 0.95]}
    _, one = _fit(_task(params, frame=_frame(code, case), write_output=True))
    _, two = _fit(_task(params, frame=_frame(code, case), write_output=True))
    _expect(one.equals(two), "two runs differ")
    _expect(_CACHE.get("row_groups") == 1, f"one block: {_CACHE.get('row_groups')} row groups")
    old = fs.BLOCK_POINTS
    try:
        fs.BLOCK_POINTS = 1                       # each series its own block, its own batches
        _, alone = _fit(_task(params, frame=_frame(code, case), write_output=True))
    finally:
        fs.BLOCK_POINTS = old
    _expect(_CACHE.get("row_groups") == 3, f"a block a series: {_CACHE.get('row_groups')} row groups")
    _expect(one.equals(alone), "a series alone in its batches differs from the same series among the others")
    # the vendored package called directly on arrays made here: each series' context padded on the left to its rung, the
    # batch filled with copies of it to its constant size
    fc, _ = ft.load(size)
    rows = one.to_pydict()
    for label in ("Negozio", "Online", "Telefono"):
        y = np.array([rows["observed"][i] for i in range(one.num_rows) if rows["series"][i] == label and not rows["is_forecast"][i]])
        t = fs.bucket(y.size) * fs.PATCH
        b = fs.batch_rows(fs.bucket(y.size), 14, 1)
        ctx = np.full((b, t), np.nan)
        ctx[:, t - y.size:] = y
        q = fc.forecast(ctx, 14).quantiles[0, 0].numpy()
        got = np.array([[rows[c][i] for c in ("forecast", "quantile_0.05", "quantile_0.9")]
                        for i in range(one.num_rows) if rows["series"][i] == label and rows["is_forecast"][i]])
        want = q[:, [fm.MEDIAN, fm.LOW90, fm.HIGH80]].astype(np.float64)
        _expect(np.array_equal(got, want), f"{label}: the runner and the package called directly differ by "
                                           f"{np.abs(got - want).max():.3g}")
        _expect(bool(np.all(np.diff(q, axis=-1) >= 0)), f"{label}: quantiles not sorted")
        x = y - y.mean()                          # both signs: the clamping at 0 does not apply
        pos = fc.forecast(x[None], 14, clip_nonnegative=False).quantiles
        neg = fc.forecast(-x[None], 14, clip_nonnegative=False).quantiles
        _expect(bool((neg == -pos.flip(-1)).all()), f"{label}: the symmetric averaging is not odd")


@register("foundation_forecast: the time and the series keep the input's own types")
def _types(code: Path) -> None:
    import pyarrow as pa
    from tab_columns import read_csv

    size = _smallest()
    df = read_csv(str(code / "reference" / "data" / "ff_types.csv"), {"value": "number"})
    v = pa.array(df["value"].to_numpy(dtype=float), mask=np.isnan(df["value"].to_numpy(dtype=float)))
    hours = pa.array(np.array(list(df["t_hour"]), dtype="datetime64[us]"))
    times = {"t_int64": (pa.array([int(x) for x in df["t_int64"]], pa.int64()), "index"),
             "t_int": (pa.array([int(x) for x in df["t_int"]], pa.int32()), "index"),
             "t_date": (pa.array(np.array(list(df["t_date"]), dtype="datetime64[D]")).cast(pa.date32()), "day"),
             "t_utc": (hours.cast(pa.timestamp("us", tz="UTC")), "hour"),
             "t_ns": (hours.cast(pa.timestamp("ns")), "hour")}
    series = {"s_text": pa.array(list(df["s_text"]), pa.string()), "s_int64": pa.array([int(x) for x in df["s_int64"]], pa.int64())}
    for tname, (tcol, freq) in times.items():
        for sname, scol in series.items():
            table = pa.table({tname: tcol, sname: scol, "value": v})
            params = {"time": tname, "value": "value", "series": sname, "frequency": freq, "size": size, "horizon": 3, "holdout": 0,
                      "missing": "model", "quantiles": [0.1, 0.9]}
            rows, out = _fit(_task(params, table=table, write_output=True))
            _expect(out.schema.field("time").type == tcol.type and out.schema.field("series").type == scol.type,
                    f"{tname}/{sname}: {out.schema.field('time').type}, {out.schema.field('series').type}")
            d = out.to_pydict()
            fut = [i for i in range(out.num_rows) if d["is_forecast"][i]]
            _expect(len(fut) == 9 and all(d["forecast"][i] is not None for i in fut), f"{tname}/{sname}: the future rows")
            at = {k: i for i, k in enumerate(rows.key)}
            _expect(rows.value[at["model|||n_model_missing"]] == 3 and rows.value[at["model|||n_series"]] == 3,
                    f"{tname}/{sname}: counts")
            # Online's last value is step 21: its future starts at step 22
            first = d["time"][fut[[str(d["series"][i]) for i in fut].index("Online" if sname == "s_text" else "3000000001")]]
            want = {"t_int64": 5_000_000_022, "t_int": 22, "t_date": __import__("datetime").date(2026, 3, 23)}.get(tname)
            if want is not None:
                _expect(first == want, f"{tname}/{sname}: the first future time {first}")


@register("foundation_forecast: refusals of the image and of the step: size_absent, too_many_series, too_slow (nothing left)")
def _refusals(code: Path) -> None:
    size = _smallest()
    case = _with_size(code, size, "three")

    def refused(task: dict[str, Any]) -> Refusal:
        try:
            _fit(task)
        except Refusal as e:
            return e
        raise AssertionError("not refused")

    old = ft.WEIGHTS
    try:
        with tempfile.TemporaryDirectory() as tmp:
            ft.WEIGHTS = Path(tmp)
            e = refused(_task(case["params"], frame=_frame(code, case)))
            _expect(e.code == "size_absent" and e.message.startswith(ft.SIZES[size]["name"] + " was not downloaded"), e.message)
    finally:
        ft.WEIGHTS = old
    e = refused(_task(case["params"], frame=_frame(code, case), limits={"max_series": 2}))
    _expect(e.code == "too_many_series" and "makes 3 series" in e.message, e.message)
    import pyarrow.parquet  # noqa: F401 — loaded before the clock starts

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "output.parquet")
        old_out = os.environ.get("TABULARIA_OUTPUT")
        os.environ["TABULARIA_OUTPUT"] = path
        try:
            task = _task(case["params"], frame=_frame(code, case), limits={"time_budget_seconds": 0.05}, write_output=True)
            task["_started"] = time.monotonic()
            fs_old = fs.BLOCK_POINTS
            fs.BLOCK_POINTS = 1                 # a block written before the projection refuses: it must go
            try:
                ft.fit_timestral(task)
                raise AssertionError("too_slow not refused")
            except Refusal as e:
                _expect(e.code == "too_slow" and "the step allows" in e.message, e.message)
            finally:
                fs.BLOCK_POINTS = fs_old
            _expect(not os.path.exists(path), "an output was left behind")
        finally:
            if old_out is None:
                os.environ.pop("TABULARIA_OUTPUT", None)
            else:
                os.environ["TABULARIA_OUTPUT"] = old_out


@register("foundation_forecast: the results stay within max_result_rows: the history shrinks, then the series' table")
def _room(code: Path) -> None:
    size = _smallest()
    case = _with_size(code, size, "wilcoxon")
    for most, note in ((900, "model|||note_history_shrunk"), (300, "model|||note_series_left_out")):
        rows, _ = _fit(_task(case["params"], frame=_frame(code, case), limits={"max_result_rows": most}))
        _expect(len(rows) <= most, f"{len(rows)} rows, at most {most}")
        _expect(note in rows.key, f"no {note} with {most} rows")
