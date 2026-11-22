"""Forecast (Timestral) (docs/specs/foundation-forecast.md, «The node `timestral`», «The output», «The results table»):
fit_timestral(task).

  1. the options, the table's columns, every series surveyed (ff_series): each refusal comes before any forecast;
  2. the size's weights loaded from the image (/opt/tabularia/weights/<size>, never a download), the threads fixed;
  3. the series forecast block by block (ff_series.plan): from the holdout's cut, measured (ff_measures), and from the end;
     each block written to the output as one row group of a Parquet writer kept open, its series' numbers added to the
     totals, and dropped. The time budget is projected after the first batch and again after each: when the step's time
     would not hold the rest, the node stops (`too_slow`) and leaves no output behind — never half a forecast;
  4. the results: the model's numbers, one row group of numbers per series, the comparison (Wilcoxon's signed-rank test
     across the series: the only verdict), the measures, the curves of the series drawn, the reading (ff_reading).

Nothing is random, so the node has no seed; the threads are the Job's (torch.set_num_threads) and every batch has a shape
that depends on its series alone (ff_series), so the same input gives the same bits. No card: nothing is registered.
"""
from __future__ import annotations

import gc
import json
import math
import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

import ff_measures as fm
import ff_reading
import ff_series as fs
import tab_series
from tab_protocol import ENV_OUTPUT, Refusal, is_uri, limit, threads as task_threads
from tab_results import Rows, add, note, number, p_value, points, rows_start, text

WEIGHTS = Path("/opt/tabularia/weights")
HERE = Path(__file__).resolve().parent
# the weights of each size, as the group's recipe pins them (recipe.json: the self-test checks config.json and LICENSE
# against these; the check Job checks model.safetensors against the recipe)
SIZES = {
    "5m": {"name": "Timestral-5M", "repo": "mistralai/Timestral-5M", "revision": "bdd20cfc15a3fea0e4cb5ccef37f8d3ca7aa335f",
           "sha256": "7ace108c4987e7f2a99a75a445240019d1d31c2117139d1bd05ffdfd25aca738",
           "config": "b5f27f61513f8fd1a3aa996f3720d3679cb40bce5bcb08eb3dc320ce726e7c2f"},
    "28m": {"name": "Timestral-28M", "repo": "mistralai/Timestral-28M", "revision": "49b26409ac87b5935539a5d8ffe7f397b720db47",
            "sha256": "dfc83c6220937ce5fc6687b35a4df94cd25e28a2cfe6fb92a23dad6301b2015d",
            "config": "0cdf44b8ea981191de53078e0a5ecbbd437046bd939a546d5dbe17868f71281f"},
    "123m": {"name": "Timestral-123M", "repo": "mistralai/Timestral-123M", "revision": "5a147287addda85f6acd8da3fb8858fe9af7e8f7",
             "sha256": "599fafa73cadbbc800b5a346ca900fa741ee3e5fd22e3b4a4430e151e74f4bc2",
             "config": "7bb4c042fb2d2ed6469b6758ba380dfeda3ab32df47690a87aec1527bdae79bf"},
    "209m": {"name": "Timestral-209M", "repo": "mistralai/Timestral-209M", "revision": "9874d252ab0aae16f7f7c999f98f98d6ad7d8365",
             "sha256": "1456effb9927e3daa8c7234f10497e3424233869245a9c72b8ecebaf7ad2b7fa",
             "config": "1764139bb6d37ee5ff543f470495c086cfa6c373f7134fc61c7e3d844538bfd8"},
}
LICENCE = {"name": "Modified MIT License (Mistral AI)", "sha256": "648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c"}
# the share of the time left the projected forecast may take (the rest: the measures, the results, the writing)
TIME_SHARE = 0.85
CURVE_LINES = (("median", fm.MEDIAN), ("q0.05", fm.LOW90), ("q0.1", fm.LOW80), ("q0.9", fm.HIGH80), ("q0.95", fm.HIGH90))
FREQUENTLY = {"hour": "hourly", "day": "daily", "week": "weekly", "month": "monthly", "quarter": "quarterly", "year": "yearly",
              "index": "by step"}
H0 = "the differences of MASE between Timestral and the seasonal naïve are symmetric around 0"
ADVICE = {"one_series": "one series gives one comparison: the test needs several series (or several forecast origins)",
          "no_holdout": "no value was kept out: there are no errors to compare",
          "": "fewer than two series have their errors on the holdout: the test needs at least two"}


# ── the model ─────────────────────────────────────────────────────────────────
_LOADED: dict[str, Any] = {"size": None, "forecaster": None}


def sizes_present() -> list[str]:
    return [s for s in SIZES if (WEIGHTS / s / "model.safetensors").is_file()]


def set_threads(n: int) -> None:
    """The Job's threads, before anything is forecast; one thread between operations (the interop pool can be set once
    a process: a self-test's second fit keeps it)."""
    import torch

    torch.set_num_threads(int(n))
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass


def load(size: str) -> tuple[Any, float]:
    """The forecaster of a size, read from the image's folder (a local directory: the vendored loader never reaches the
    Hub), and the seconds it took; the size loaded before is dropped first (one size in memory at a time)."""
    if _LOADED["size"] == size:
        return _LOADED["forecaster"], 0.0
    _LOADED["size"], _LOADED["forecaster"] = None, None
    gc.collect()
    import torch
    from timestral import TimestralForecaster

    torch.manual_seed(0)                      # nothing draws a number; fixed all the same
    t0 = time.monotonic()
    fc = TimestralForecaster.from_pretrained(str(WEIGHTS / size), device="cpu")
    _LOADED["size"], _LOADED["forecaster"] = size, fc
    return fc, time.monotonic() - t0


def unload() -> None:
    _LOADED["size"], _LOADED["forecaster"] = None, None
    gc.collect()


def forecast(fc: Any, a: fs.Arrays, h: int, opts: fs.Options) -> np.ndarray:
    """One batch: the quantiles (rows, h, 21) as float32, the library's own numbers."""
    import torch

    with torch.inference_mode():
        f = fc.forecast(a.context, h, past_covariates=a.past, known_covariates=a.known, categorical_past_covariates=a.past_cat,
                        categorical_known_covariates=a.known_cat, symmetric_averaging=opts.symmetric, clip_nonnegative=opts.clip)
    return f.quantiles[:, 0].numpy()


def software() -> str:
    import torch

    commit = "a534bdbd"
    try:
        commit = json.loads((HERE / "timestral" / "vendor.json").read_text(encoding="utf-8"))["commit"][:8]
    except (OSError, ValueError, KeyError):
        pass
    import timestral

    return f"Python {platform.python_version()}; torch {torch.__version__}; timestral {timestral.__version__} ({commit})"


def cpu_capability() -> str:
    import torch

    try:
        return str(torch.backends.cpu.get_cpu_capability())
    except Exception:  # noqa: BLE001 — an older torch: the report says nothing
        return "unknown"


# ── the time budget ─────────────────────────────────────────────────────────────
class Budget:
    """The step's time (limits.time_budget_seconds, counted from the runner's start, task["_started"]): after each batch
    the rest of the forecast is projected at the speed measured so far, by the patches × rows the model reads; when it
    passes TIME_SHARE of the time left, `too_slow`. Without a budget (a self-test) nothing is checked."""

    def __init__(self, task: dict[str, Any], total_cost: float, n_series: int, size_name: str, threads: int):
        s = (task.get("limits") or {}).get("time_budget_seconds")
        self.seconds = float(s) if s else None
        started = task.get("_started")
        self.started = time.monotonic() if started is None else float(started)
        self.total = total_cost
        self.done = 0.0
        self.spent = 0.0
        self.forecasts = 0
        self.n_series, self.size_name, self.threads = n_series, size_name, threads

    def after(self, cost: float, seconds: float, forecasts: int) -> None:
        self.done += cost
        self.spent += seconds
        self.forecasts += forecasts
        if self.seconds is None or self.done <= 0:
            return
        now = time.monotonic()
        rest = self.spent / self.done * max(0.0, self.total - self.done)
        left = self.started + self.seconds - now
        if rest > TIME_SHARE * left:
            need = (now - self.started) + rest
            each = self.spent / max(1, self.forecasts)
            cpus = f"{self.threads} CPU" + ("" if self.threads == 1 else "s")
            raise Refusal("too_slow", f"at the speed of the first {self.forecasts:,} forecasts ({each:.2g} s each with "
                                      f"{self.size_name} on {cpus}), the {self.n_series:,} series need about {_duration(need)}; "
                                      f"the step allows {_duration(self.seconds)}: choose a smaller size, fewer series, or a "
                                      "longer timeout",
                          {"forecasts": self.forecasts, "seconds_each": each, "series": self.n_series, "need_seconds": need,
                           "allowed_seconds": self.seconds, "size": self.size_name, "threads": self.threads})


def _duration(seconds: float) -> str:
    if seconds < 120:
        return f"{max(1, round(seconds))} seconds"
    return f"{round(seconds / 60)} minutes"


# ── the output, by blocks ───────────────────────────────────────────────────────
class Output:
    """The forecast for the next step: one Parquet writer kept open, one row group a block. `abort` removes what was
    written (a refusal or a crash after the first block leaves nothing behind)."""

    def __init__(self, path: str, schema: Any):
        self.path, self.schema = path, schema
        self.writer = None
        self.rows = 0

    def write(self, table: Any) -> None:
        import pyarrow.parquet as pq

        if self.writer is None:
            if is_uri(self.path):
                from pyarrow import fs as pafs

                filesystem, where = pafs.FileSystem.from_uri(self.path)
                self.writer = pq.ParquetWriter(where, self.schema, filesystem=filesystem)
            else:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                self.writer = pq.ParquetWriter(self.path, self.schema)
        if table.num_rows:
            self.writer.write_table(table, row_group_size=table.num_rows)
        self.rows += table.num_rows

    def close(self) -> None:
        if self.writer is None:
            self.write(self.schema.empty_table())
        self.writer.close()

    def abort(self) -> None:
        try:
            if self.writer is not None:
                self.writer.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if is_uri(self.path):
                from pyarrow import fs as pafs

                filesystem, where = pafs.FileSystem.from_uri(self.path)
                filesystem.delete_file(where)
            else:
                Path(self.path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — nothing was there
            pass


def output_schema(table: Any, opts: fs.Options) -> Any:
    import pyarrow as pa

    fields = []
    if opts.series:
        fields.append(pa.field("series", table.schema.field(opts.series).type))
    fields.append(pa.field("time", table.schema.field(opts.time).type))
    fields += [pa.field(n, pa.float64()) for n in ("observed", "forecast", *quantile_columns(opts), "baseline")]
    fields += [pa.field(n, pa.bool_()) for n in ("filled", "is_holdout", "is_forecast")]
    return pa.schema(fields)


def quantile_columns(opts: fs.Options) -> list[str]:
    return [f"quantile_{fs._level_label(q)}" for q in opts.quantiles]


# ── one series' share of the results ────────────────────────────────────────────
@dataclass
class Done:
    """What stays of a series once its block is written: its measures and what the reading and the charts need."""

    meta: fs.Meta
    measured: fm.Measured
    curve: dict[str, Any] | None = None   # the series drawn: x and y of each line
    band: dict[str, Any] | None = None


@dataclass
class Run:
    task: dict[str, Any]
    opts: fs.Options
    table: Any
    cols: tab_series.Columns
    covs: list[fs.Covariate]
    survey: fs.Survey
    drawn: set[int] = field(default_factory=set)
    done: list[Done] = field(default_factory=list)
    across: fm.Across = field(default_factory=fm.Across)
    batches: int = 0
    seconds_forecast: float = 0.0


def display(meta: fs.Meta, opts: fs.Options) -> str:
    """A series' label in the results: its value of the series column, the value column's name for the one series."""
    return meta.label if meta.label is not None else opts.value


def _history_window(meta: fs.Meta, opts: fs.Options) -> int:
    return max(3 * meta.h, 4 * opts.season, 2 * meta.k, 1)


def _series_block(run: Run, block: fs.Block, fc: Any, budget: Budget, out: Output | None) -> None:
    """Forecasts one block, measures its series, writes its rows to the output, keeps their numbers."""
    opts = run.opts
    series = {m.index: fs.series_of(run.cols, run.survey.rows[m.index] if run.survey.rows is not None else None, opts, m.label)
              for m in block.metas}
    rows_per_series = 1 + len(run.covs)
    passes = 2 if opts.symmetric else 1
    got: dict[tuple[int, bool], np.ndarray] = {}
    for batch in block.batches:
        arrays = fs.batch_arrays(batch, series, run.covs)
        t0 = time.monotonic()
        try:
            q = forecast(fc, arrays, batch.h, opts)
        except ValueError as e:
            if "degenerate" not in str(e):
                raise
            raise _degenerate(fc, batch, series, run) from None
        seconds = time.monotonic() - t0
        run.seconds_forecast += seconds
        run.batches += 1
        for r, job in enumerate(batch.jobs):
            got[(job.meta.index, job.holdout)] = q[r]
        del arrays, q
        budget.after(batch.cost(rows_per_series, passes), seconds, len(batch.jobs))
    parts = []
    for m in block.metas:
        s = series[m.index]
        final = got.pop((m.index, False))
        held = got.pop((m.index, True), None)
        base_future = fm.seasonal_naive(s.z, m.h, opts.season)
        cut = s.n - m.k
        measured = fm.Measured(reason=m.reason) if held is None else None
        base_held = None
        if held is not None:
            base_held = fm.seasonal_naive(s.z[:cut], m.k, opts.season)
            measured = fm.Measured.of(s.y[:cut], s.y[cut:], held, base_held, opts.season)
        run.across.add(measured)
        d = Done(m, measured)
        if m.index in run.drawn:
            d.curve, d.band = _curve(s, m, final, held, base_future, base_held, opts)
        run.done.append(d)
        if out is not None:
            parts.append(_rows_out(run, s, m, final, held, base_future, base_held))
        del series[m.index]
    if out is not None and parts:
        import pyarrow as pa

        table = pa.concat_tables(parts)
        if opts.series:
            # the series' own value, of the input's own type: one take of the column a block (the first row of each series)
            firsts = np.array([int(run.survey.rows[m.index][0]) for m in block.metas], dtype=np.int64)
            counts = np.array([p.num_rows for p in parts], dtype=np.int64)
            col = run.table.column(opts.series).take(pa.array(np.repeat(firsts, counts)))
            table = table.add_column(0, out.schema.field(0), col)
        out.write(table)


def _degenerate(fc: Any, batch: fs.Batch, series: dict[int, tab_series.Series], run: Run) -> Refusal:
    """The library's «degenerate» ValueError, named: the first series of the batch that fails alone."""
    for job in batch.jobs:
        one = fs.Batch([job], 1, batch.patches, batch.h)
        try:
            forecast(fc, fs.batch_arrays(one, series, [fs.Covariate(c.name, c.known, c.kind, c.values, c.categorical)
                                                       for c in run.covs]), batch.h, run.opts)
        except ValueError:
            label = display(job.meta, run.opts)
            return Refusal("degenerate", f"Timestral could not forecast «{label}»: its values are degenerate (the library "
                                         "produced no number)", {"series": label})
    label = display(batch.jobs[0].meta, run.opts)
    return Refusal("degenerate", f"Timestral could not forecast «{label}»: its values are degenerate (the library produced "
                                 "no number)", {"series": label})


def _curve(s: tab_series.Series, m: fs.Meta, final: np.ndarray, held: np.ndarray | None, base: np.ndarray,
           base_held: np.ndarray | None, opts: fs.Options) -> tuple[dict[str, Any], dict[str, Any]]:
    """The lines of a series drawn (the history's window, the forecast, the holdout) and its reading's numbers."""
    x0 = s.x0()
    w = min(s.n, _history_window(m, opts))
    future_x = s.axis.x0(s.after(np.arange(1, m.h + 1)))
    lines: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for name, i in CURVE_LINES:
        lines[name] = (future_x, final[:, i].astype(np.float64))
    lines["baseline"] = (future_x, base)
    if held is not None:
        hx = x0[s.n - m.k:]
        for name, i in CURVE_LINES:
            lines["holdout_" + name] = (hx, held[:, i].astype(np.float64))
        lines["holdout_baseline"] = (hx, base_held)
    curve = {"history": (x0[-w:].copy(), s.y[-w:].copy()), "lines": lines}
    at = float(future_x[0])
    band = {"label": display(m, opts), "at": at, "median": float(final[0, fm.MEDIAN]), "low80": float(final[0, fm.LOW80]),
            "high80": float(final[0, fm.HIGH80]), "low90": float(final[0, fm.LOW90]), "high90": float(final[0, fm.HIGH90])}
    return curve, band


def _rows_out(run: Run, s: tab_series.Series, m: fs.Meta, final: np.ndarray, held: np.ndarray | None, base_future: np.ndarray,
              base_held: np.ndarray | None) -> Any:
    """The output's rows of one series: the history (with `history`), the holdout, the future."""
    import pyarrow as pa

    opts = run.opts
    cut = s.n - (m.k if held is not None else 0)
    start = 0 if opts.history else cut
    span = np.arange(start, s.n)
    n_span, h = span.size, m.h
    total = n_span + h
    times = pa.concat_arrays([s.time_array(s.times[start:]), s.time_array(s.after(np.arange(1, h + 1)))])
    observed = np.concatenate([s.y[start:], np.full(h, np.nan)])
    qcols = [fm.LEVELS.index(q) for q in opts.quantiles]
    med = np.full(total, np.nan)
    quant = np.full((total, len(qcols)), np.nan)
    base = np.full(total, np.nan)
    held_from = cut - start
    if held is not None:
        med[held_from:n_span] = held[:, fm.MEDIAN]
        quant[held_from:n_span] = held[:, qcols]
        base[held_from:n_span] = base_held
    med[n_span:] = final[:, fm.MEDIAN]
    quant[n_span:] = final[:, qcols]
    base[n_span:] = base_future
    filled = np.concatenate([s.filled[start:], np.zeros(h, dtype=bool)])
    is_holdout = np.zeros(total, dtype=bool)
    if held is not None:
        is_holdout[held_from:n_span] = True
    is_forecast = np.zeros(total, dtype=bool)
    is_forecast[n_span:] = True

    def num(a: np.ndarray) -> Any:
        return pa.array(a, type=pa.float64(), mask=np.isnan(a))

    cols: list[Any] = [times, num(observed), num(med), *[num(quant[:, j].astype(np.float64)) for j in range(len(qcols))], num(base),
             pa.array(filled), pa.array(is_holdout), pa.array(is_forecast)]
    schema = output_schema(run.table, opts)
    if opts.series:                  # the series column is put in front by the block, in one take
        schema = schema.remove(0)
    return pa.Table.from_arrays(cols, schema=schema)


# ── fit ─────────────────────────────────────────────────────────────────────────
def fit_timestral(task: dict[str, Any]) -> dict[str, Any]:
    opts = fs.options(task)
    names = [opts.time, opts.value, *([opts.series] if opts.series else []), *opts.covariates]
    table = tab_series.read_table(task, names)
    cols = tab_series.columns(table, opts.time, opts.value, opts.frequency)
    covs = fs.covariates(table, opts, task)
    survey = fs.survey(table, cols, opts, task)
    if opts.size not in SIZES or opts.size not in sizes_present():
        present = [SIZES[s]["name"].removeprefix("Timestral-") for s in sizes_present()]
        choose = (" or ".join(present) if len(present) <= 2 else ", ".join(present[:-1]) + " or " + present[-1]) if present else ""
        name = SIZES.get(opts.size, {}).get("name", opts.size)
        raise Refusal("size_absent", f"{name} was not downloaded by this installation: " + (f"choose {choose}, or " if choose else "")
                      + "ask an administrator to add it", {"size": opts.size, "present": sizes_present()})
    threads = task_threads(task)
    set_threads(threads)
    fc, seconds_load = load(opts.size)
    run = Run(task, opts, table, cols, covs, survey)
    metas = survey.metas
    order = sorted(metas, key=lambda m: (-m.total, display(m, opts)))
    run.drawn = {m.index for m in order[:int(limit(task, "max_series_charts"))]}
    rows_per_series = 1 + len(covs)
    passes = 2 if opts.symmetric else 1
    blocks = list(fs.plan(metas, rows_per_series))
    total_cost = sum(b.cost(rows_per_series, passes) for blk in blocks for b in blk.batches)
    budget = Budget(task, total_cost, len(metas), SIZES[opts.size]["name"], threads)
    path = os.environ.get(ENV_OUTPUT, "") if task.get("write_output") and opts.output == "quantile_forecast" else ""
    out = Output(path, output_schema(table, opts)) if path else None
    try:
        for blk in blocks:
            _series_block(run, blk, fc, budget, out)
        if out is not None:
            out.close()
    except BaseException:
        if out is not None:
            out.abort()
        raise
    rows = results(run, seconds_load, threads, budget)
    return {"rows": rows, "output": None, "card": None, "model_file": None}


# ── the results ─────────────────────────────────────────────────────────────────
def _verdict(run: Run) -> tuple[str, str | None, fm.Wilcoxon | None]:
    opts, a = run.opts, run.across
    if not any(m.asked for m in run.survey.metas):
        return "not_computed", "no_holdout", None
    if a.n < 2:
        return "not_computed", "one_series" if len(run.survey.metas) == 1 else "", None
    test = fm.wilcoxon(np.asarray(a.diffs))
    if test is None:
        return "not_computed", "", None
    return ("failed" if test.p_value < opts.alpha else "ok"), None, test


def results(run: Run, seconds_load: float, threads: int, budget: Budget) -> Rows:
    task, opts = run.task, run.opts
    metas = run.survey.metas
    size = SIZES[opts.size]
    rows = rows_start(task)
    n_series = len(metas)
    by = f" · {n_series:,} series by {opts.series}" if opts.series else ""
    unit = tab_series.UNIT[opts.frequency]
    h_max = max(m.h for m in metas)
    k_max = max((m.asked for m in metas), default=0)
    formula = f"{opts.value} ~ {size['name']}{by} · {FREQUENTLY[opts.frequency]} · {h_max:,} {unit} ahead"
    texts = {"formula": formula, "software": software(), "frequency": opts.frequency, "time_kind": run.cols.axis.kind,
             "missing": opts.missing, "size": opts.size, "weights": f"{size['repo']}@{size['revision'][:8]}",
             "weights_sha256": size["sha256"], "licence": LICENCE["name"], "licence_sha256": LICENCE["sha256"],
             "cpu_capability": cpu_capability()}
    for k, v in texts.items():
        text(rows, "model", k, v)
    a = run.across.numbers()
    n_points = sum(m.n for m in metas)
    numbers = {"n": n_points, "n_series": n_series, "n_points": n_points,
               "n_missing": sum(m.n_missing for m in metas) + run.survey.no_series, "n_filled": sum(m.n_filled for m in metas),
               "n_model_missing": sum(m.n_model_missing for m in metas), "n_future_rows": sum(m.n_future for m in metas),
               "horizon": h_max, "holdout": k_max, "season": opts.season, "alpha": opts.alpha, "threads": threads,
               "batches": run.batches, "seconds_load": round(seconds_load, 3), "seconds_forecast": round(run.seconds_forecast, 3),
               "n_quantiles": len(opts.quantiles), **a}
    for k, v in numbers.items():
        number(rows, "model", k, v)
    _notes(run, rows, budget)
    verdict, advice_id, test = _verdict(run)
    # the comparison: the only verdict
    lv = "baseline_comparison"
    text(rows, "assumption", "h0", H0, level=lv)
    if test is not None:
        number(rows, "assumption", "statistic", test.statistic, level=lv)
        p_value(rows, "assumption", test.p_value, level=lv)
        # the side of the test: the report's «ahead» reads it, never the median's sign
        number(rows, "assumption", "rank_biserial", test.rank_biserial, level=lv)
    text(rows, "assumption", "verdict", verdict, level=lv)
    if verdict == "not_computed":
        text(rows, "assumption", "advice", ADVICE[advice_id or ""], level=lv)
        if advice_id:
            text(rows, "assumption", "advice_id", advice_id, level=lv)
    number(rows, "assumption", "count", test.n if test is not None else run.across.n, level=lv)
    median_d = fm.median(run.across.diffs)
    if run.across.diffs:
        number(rows, "assumption", "value", median_d, level=lv)
    # the measures, without verdict
    for stat, key in (("coverage_80", "coverage_80"), ("coverage_90", "coverage_90"), ("relative_mase", "relative_mase_geomean")):
        if math.isfinite(a[key]):
            number(rows, "assumption", "value", a[key], level=stat)
    ctx = _reading_context(run, a, test, verdict, median_d)
    blocks = ff_reading.blocks(ctx)
    reading = ff_reading.write(Rows(), blocks)
    _series_and_curves(run, rows, len(reading))
    _append(rows, reading)
    return rows


def _append(rows: Rows, more: Rows) -> None:
    for name in ("section", "level", "level_2", "stat", "value", "text", "x0", "x1", "key"):
        getattr(rows, name).extend(getattr(more, name))


def _notes(run: Run, rows: Rows, budget: Budget) -> None:
    opts, metas = run.opts, run.survey.metas
    last = max(m.last_x0 for m in metas)
    early = sum(m.last_x0 < last for m in metas)
    if early:
        # a time of positions has no date: its own sentence, as the summary's «_steps» ones
        said = "ends_early_steps" if run.cols.axis.kind == "integer" else "ends_early"
        note(rows, "ends_early", ff_reading.line(said, count=early, last=last))
    if not opts.known:
        after = sum(m.n_after for m in metas)
        if after:
            note(rows, "future_left_out", ff_reading.line("future_left_out", count=after))
    cut = [m for m in metas if m.n > fs.MAX_CONTEXT]
    for i, m in enumerate(cut[:ff_reading.CONTEXT_LINES], start=1):
        note(rows, f"context_cut_{i}", ff_reading.line("context_cut", used=m.context, n=m.n, series=display(m, opts)))
    for i, c in enumerate(run.covs, start=1):
        if c.n_missing:
            note(rows, f"covariate_missing_{i}", ff_reading.line("covariate_missing", count=c.n_missing, covariate=c.name))
    if budget.seconds is not None:
        used = time.monotonic() - budget.started
        note(rows, "time_budget", ff_reading.line("time_budget", seconds=round(used, 1), limit=budget.seconds))


def _reading_context(run: Run, a: dict[str, float], test: fm.Wilcoxon | None, verdict: str, median_d: float) -> ff_reading.Context:
    opts, metas = run.opts, run.survey.metas
    bands = [d.band for d in sorted((d for d in run.done if d.band is not None), key=lambda d: (-d.meta.total, display(d.meta, opts)))]
    asked = [d for d in run.done if d.meta.asked]
    cuts = [(display(m, opts), fs.MAX_CONTEXT, m.n) for m in metas if m.n > fs.MAX_CONTEXT]
    accepted = None
    lic = run.task.get("licence") or {}
    if lic.get("by") and lic.get("at"):
        try:
            import datetime as dt

            day = dt.date.fromisoformat(str(lic["at"])[:10])
            accepted = {"by": lic["by"], "on": (day - dt.date(1970, 1, 1)).days}
        except ValueError:
            accepted = None
    return ff_reading.Context(
        size_name=SIZES[opts.size]["name"], value=opts.value, series=opts.series, n_series=len(metas),
        horizon=max(m.h for m in metas), last=max(m.last_x0 for m in metas), integer_time=run.cols.axis.kind == "integer",
        context=max(m.context for m in metas), past=list(opts.past), known=list(opts.known),
        n_filled=sum(m.n_filled for m in metas), n_model_missing=sum(m.n_model_missing for m in metas), bands=bands,
        holdout=max((m.asked for m in metas), default=0),
        across=a, n_not_measured=sum(not d.measured.measured for d in asked), steps=run.across.steps, in80=run.across.in80,
        in90=run.across.in90, test=test, better=run.across.better, verdict=verdict, median_d=median_d, alpha=opts.alpha, cuts=cuts,
        clipped=opts.clip and any(m.nonnegative for m in metas), licence=LICENCE["name"], accepted=accepted, missing=opts.missing)


SERIES_NUMBERS = ("n", "n_used", "n_missing", "n_filled", "first", "last", "horizon", "scale", "mase", "baseline_mase",
                  "relative_mase", "smape", "baseline_smape", "pinball", "wql", "coverage_80", "coverage_90", "total")


def _series_values(d: Done) -> dict[str, float]:
    m, x = d.meta, d.measured
    return {"n": m.n, "n_used": m.context, "n_missing": m.n_missing, "n_filled": m.n_filled, "first": m.first_x0,
            "last": m.last_x0, "horizon": m.h, "scale": x.scale, "mase": x.mase, "baseline_mase": x.baseline_mase,
            "relative_mase": x.relative_mase, "smape": x.smape, "baseline_smape": x.baseline_smape, "pinball": x.pinball,
            "wql": x.wql, "coverage_80": x.coverage_80, "coverage_90": x.coverage_90, "total": m.total}


def _series_and_curves(run: Run, rows: Rows, reading_rows: int) -> None:
    """The section `series` (every series, in text order; the first by Σ|y| when the results would pass max_result_rows,
    with a note) and `series_curve` (the series drawn, by Σ|y|, their history thinned to what is left of max_result_rows:
    the measures never shrink)."""
    opts = run.opts
    most = int(limit(run.task, "max_result_rows"))
    done = sorted(run.done, key=lambda d: d.meta.index)
    values = [{k: v for k, v in _series_values(d).items() if v is not None and not (isinstance(v, float) and math.isnan(v))}
              for d in done]
    each = [len(v) + (1 if d.measured.reason else 0) for v, d in zip(values, done)]
    drawn = sorted((d for d in done if d.curve is not None), key=lambda d: (-d.meta.total, display(d.meta, opts)))
    lines = [sum(x.size for x, _ in d.curve["lines"].values()) for d in drawn]
    room = most - len(rows) - reading_rows - 2           # the notes written below, at most two
    # the series' table first (the measures never shrink); when even it does not fit beside the lines of the first series
    # drawn, the series with the largest Σ|y| that do, and a note
    first = (sum(lines[:1]) + 2) if drawn else 0
    done_kept = list(zip(done, values))
    used = sum(each)
    if used > room - first:
        by_total = sorted(range(len(done)), key=lambda i: (-done[i].meta.total, display(done[i].meta, opts)))
        used, chosen = 0, set()
        for i in by_total:
            if used + each[i] > room - first:
                break
            used += each[i]
            chosen.add(i)
        # said in the page's language by the report's template «series_kept»
        note(rows, "series_left_out", ff_reading.line("series_kept", kept=len(chosen), total=len(done), most=most))
        done_kept = [(d, v) for i, (d, v) in enumerate(zip(done, values)) if i in chosen]
    room -= used
    # then the series drawn whose lines fit with two points of history each, in the order of Σ|y|; their history shares the rest
    while drawn and sum(lines) + 2 * len(drawn) > room:
        drawn.pop()
        lines.pop()
    # one call a statistic (a row per series that has it): thousands of series in a few calls
    labels = [display(d.meta, opts) for d, _ in done_kept]
    for stat in SERIES_NUMBERS:
        have = [i for i, (_, v) in enumerate(done_kept) if stat in v]
        if have:
            add(rows, "series", [stat] * len(have), value=[done_kept[i][1][stat] for i in have], level=[labels[i] for i in have])
    why = [i for i, (d, _) in enumerate(done_kept) if d.measured.reason]
    if why:
        add(rows, "series", ["not_measured"] * len(why), text=[done_kept[i][0].measured.reason for i in why],
            level=[labels[i] for i in why])
    room_history = room - sum(lines)
    full = int(limit(run.task, "max_chart_points"))
    share = full if not drawn else max(2, min(full, room_history // len(drawn)))
    shrunk = False
    for d in drawn:
        label = display(d.meta, opts)
        hx, hy = d.curve["history"]
        keep = tab_series.envelope(hy, share)
        if keep.size < np.count_nonzero(~np.isnan(hy)) and share < full:
            shrunk = True
        points(rows, "series_curve", label, "y", hx[keep], hy[keep], level_2="observed")
        for name, (x, y) in d.curve["lines"].items():
            points(rows, "series_curve", label, "y", x, y, level_2=name)
    if shrunk:
        note(rows, "history_shrunk", ff_reading.line("history_shrunk", points=share))
