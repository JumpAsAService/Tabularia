"""The input of Forecast (Timestral) (docs/specs/foundation-forecast.md, «The series», «Covariates», «The forecast»): the
node's options, the series and their covariates, the cut of the holdout, the plan of the forecasts (blocks of series, batches
of one constant size) and the arrays of a batch.

The table is read once, as Arrow columns (tab_series.read_table), with one numpy array of ticks and one of values beside it
and one array a covariate. Nothing else of the table is held: the survey reads each series once to check it and keeps a few
numbers of it (`Meta`), the forecast rebuilds the series of one block at a time.

  options(task)                    the parameters, with the catalogue's defaults, and the runner's own refusals
  covariates(table, opts, limits)  each covariate as one array over the table's rows (numbers, 0/1, codes of the labels)
  survey(...)                      every series checked before any forecast: refusals first, never a half forecast
  plan(metas, ...)                 the blocks, the batches and their cost
  batch_arrays(...)                the arrays of one batch, padded on the left and with copies to its constant size

Batches (docs/specs/foundation-forecast.md, «As built / F0», «Determinism»): the library's kernels choose their blocking by
the batch's shape, so a series' bits would depend on the series beside it. Here every forecast runs in a batch whose shape is
a function of the series alone: its context is padded on the left with missing values to the next rung of a ladder of patch
counts (`bucket`; the model never reads leading missing patches: they are not valid keys of its attention), its horizon is
its own, and the batch has the constant size `batch_rows(...)` of that shape, the last one filled with copies of its last
series. A series then gives the same bits whatever the table around it, its place in it and the blocks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterator

import numpy as np

import tab_series
from tab_columns import kind_of_arrow, label as value_label
from tab_protocol import Refusal, catalog, limit
from ff_measures import LEVELS

PATCH = 32
MAX_CONTEXT = 32_768                 # the model's trained context: the most recent values are read
MAX_PERIODS = 1_000_000              # places of a series' grid: the group's max_rows (tab_series' own is 200,000)
# patches × rows of a batch: B·V·(P + Hp) at most this, at least one series. Measured per batch size (2 threads): with
# 100 values a batch of 16 costs 6 to 14 % of 16 batches of one, so short series go 16 at a time; with 1,024 values a batch of
# 7 or 8 costs within 5–15 % per series of one of 16 (Timestral-28M, -209M) and half its time when it holds one series alone
BATCH_TOKENS = 256
BATCH_MAX = 16                       # the constant size of short series' batches (F0's measurements)
# the places a block of series holds at most (spans, holdouts, horizons): what is rebuilt and kept while it is forecast
BLOCK_POINTS = 400_000


# ── the options ─────────────────────────────────────────────────────────────────
@dataclass
class Options:
    time: str
    value: str
    series: str | None
    frequency: str
    season: int
    size: str
    horizon: int
    quantiles: list[float]           # the levels written, increasing, without 0.5
    past: list[str]
    known: list[str]
    holdout: int | None              # None: the horizon (with known covariates: each series' future rows)
    missing: str
    symmetric: bool
    clip: bool
    history: bool
    alpha: float
    output: str

    @property
    def covariates(self) -> list[str]:
        return [*self.past, *self.known]


def _level_label(x: float) -> str:
    """A level as the catalogue names its column (`quantile_0.05`): repr of the double, a whole number without «.0»."""
    return str(int(x)) if float(x).is_integer() else repr(float(x))


def options(task: dict[str, Any]) -> Options:
    """The node's parameters with the catalogue's defaults (a self-test's task holds only some), and what the catalogue
    cannot refuse: a level that is not one of the 21 (`quantile_level`), more covariates than `max_covariates` together."""
    spec = next(m for m in catalog()["models"] if m["id"] == "timestral")
    p = {**spec["defaults"], **(task.get("params") or {})}
    frequency = str(p["frequency"])
    season = p.get("season")
    if season is None:
        season = int(catalog()["season_of_frequency"].get(frequency, 1))
    chosen: list[float] = []
    for q in p.get("quantiles") or []:
        q = float(q)
        match = next((lv for lv in LEVELS if abs(lv - q) < 1e-12), None)
        if match is None:
            said = ", ".join(_level_label(lv) for lv in LEVELS)
            raise Refusal("quantile_level", f"Timestral forecasts the levels {said}: {_level_label(q)} is not one",
                          {"level": q})
        if match != 0.5 and match not in chosen:
            chosen.append(match)
    past, known = list(p.get("past_covariates") or []), list(p.get("known_covariates") or [])
    most = int(limit(task, "max_covariates"))
    if len(past) + len(known) > most:
        raise Refusal("too_many_covariates", f"the node reads at most {most} covariates, past and known together: "
                                             f"{len(past) + len(known)} were chosen", {"count": len(past) + len(known), "limit": most})
    holdout = p.get("holdout")
    return Options(time=str(p["time"]), value=str(p["value"]), series=p.get("series") or None, frequency=frequency,
                   season=max(1, int(season)), size=str(p.get("size") or "28m"), horizon=int(p.get("horizon") or 28),
                   quantiles=sorted(chosen), past=past, known=known, holdout=None if holdout is None else int(holdout),
                   missing=str(p.get("missing") or "refuse"), symmetric=bool(p.get("symmetric_averaging", True)),
                   clip=bool(p.get("clip_nonnegative", True)), history=bool(p.get("history", True)),
                   alpha=float(p.get("alpha", 0.05)), output=str(p.get("output") or "quantile_forecast"))


# ── the covariates ──────────────────────────────────────────────────────────────
@dataclass
class Covariate:
    """A covariate column as one array over the table's rows: a number as it is, a boolean as 0/1, a text as the code of
    its label in text order (0 … k−1); NaN where empty. `categorical` for a text (target-encoded by the library)."""

    name: str
    known: bool
    kind: str
    values: np.ndarray
    categorical: bool
    levels: list[str] = field(default_factory=list)
    n_missing: int = 0               # empty values handed to the model, counted as the forecasts read them


def covariates(table: Any, opts: Options, task: dict[str, Any]) -> list[Covariate]:
    """The covariates in the order past, then known (each list as given): `empty_covariate` for a column with no value,
    `covariate_type` for a column that is not a number, a boolean or a text, `too_many_levels` for a text of more labels
    than `max_levels_trees`."""
    import pyarrow as pa
    import pyarrow.compute as pc

    out: list[Covariate] = []
    most = int(limit(task, "max_levels_trees"))
    for name, known in [*((n, False) for n in opts.past), *((n, True) for n in opts.known)]:
        col = table.column(name)
        kind = kind_of_arrow(col.type)
        if col.null_count == len(col):
            raise Refusal("empty_covariate", f"the column «{name}» has no value: drop it from the covariates", {"column": name})
        if kind in ("number", "integer"):
            v = pc.cast(col, pa.float64()).to_numpy(zero_copy_only=False)
            if np.isinf(v).any():
                raise Refusal("infinite", f"the column «{name}» holds an infinite value: filter it out or replace it before "
                                          "the node", {"column": name})
            if np.isnan(v).all():
                raise Refusal("empty_covariate", f"the column «{name}» has no value: drop it from the covariates", {"column": name})
            out.append(Covariate(name, known, "number", v, False))
        elif kind == "boolean":
            v = pc.cast(col, pa.float64()).to_numpy(zero_copy_only=False)
            out.append(Covariate(name, known, "boolean", v, False))
        elif kind == "text":
            if isinstance(col, pa.ChunkedArray):
                col = col.combine_chunks()
            enc = pc.dictionary_encode(col, null_encoding="mask")
            names = [value_label(x) for x in enc.dictionary.to_pylist()]
            if len(names) > most:
                raise Refusal("too_many_levels", f"the column «{name}» has {len(names):,} values: is it an identifier?",
                              {"column": name, "levels": len(names), "limit": most})
            order = sorted(range(len(names)), key=lambda i: names[i])
            code = np.empty(len(names), dtype=np.float64)
            code[order] = np.arange(len(names), dtype=np.float64)
            idx = enc.indices
            missing = np.asarray(pc.is_null(idx).to_numpy(zero_copy_only=False), dtype=bool)
            v = code[idx.fill_null(0).to_numpy(zero_copy_only=False).astype(np.int64)] if names else np.zeros(len(col))
            v[missing] = np.nan
            out.append(Covariate(name, known, "text", v, True, [names[i] for i in order]))
        else:
            raise Refusal("covariate_type", f"the column «{name}» is not a number, a boolean or a text: a covariate must be one "
                                            "of them", {"column": name})
    return out


# ── the series, surveyed ───────────────────────────────────────────────────────
@dataclass
class Meta:
    """What the forecast needs of a series, kept for every series (a few numbers): its label, its span `n`, the places
    of its last value's time, the holdout `k` (0: none) and whether it is measured (`reason` otherwise), the horizon `h`,
    the contexts the two forecasts read, Σ|y| (the order of the charts), the counts."""

    index: int
    label: str | None
    n: int
    h: int
    k: int                           # the holdout forecast and measured (0: none)
    asked: int                       # the holdout asked of it (0: none; more than k when the series is too short)
    reason: str | None
    context: int                     # the values the forecast reads (at most MAX_CONTEXT)
    holdout_context: int             # the values the holdout's forecast reads (0: no holdout forecast)
    total: float
    first_x0: float
    last_x0: float
    last_tick: Any
    n_missing: int
    n_after: int
    n_filled: int
    n_model_missing: int
    n_future: int                    # future rows read for the known covariates
    nonnegative: bool


@dataclass
class Survey:
    """Every series' Meta, and what the table holds besides: the rows whose series is empty."""

    metas: list[Meta]
    labels: list[str] | None
    rows: list[np.ndarray] | None
    no_series: int


def series_of(cols: tab_series.Columns, rows: np.ndarray | None, opts: Options, label: str | None) -> tab_series.Series:
    return tab_series.series(cols, rows, opts.missing, label=label, fills=("linear", "model"), max_periods=MAX_PERIODS)


def _future(s: tab_series.Series, opts: Options, task: dict[str, Any]) -> int:
    """The future rows of a series with known covariates: complete on the grid after its last value, at most the longest
    horizon; their count is its horizon."""
    who = f"the series «{s.label}»" if s.label is not None else "the series"
    whose = f"of «{s.label}»" if s.label is not None else "of the series"
    m = int(s.after_rows.size)
    if m == 0:
        raise Refusal("known_future_missing", f"{who} has no row after its last value: with known covariates the future rows "
                                              "hold them", {"series": s.label})
    pos = np.asarray(s.after_pos, dtype=np.int64)
    want = np.arange(1, m + 1, dtype=np.int64)
    if not np.array_equal(pos, want):
        first = int(want[np.flatnonzero(pos != want)[0]])
        at = s.axis.label(s.after(first)[0])
        raise Refusal("known_future_gap", f"the future rows {whose} skip {at}: the future must be complete on the grid",
                      {"series": s.label, "time": at})
    most = int(limit(task, "max_foundation_horizon"))
    if m > most:
        raise Refusal("known_future_long", f"{who} has {m:,} future rows; Timestral forecasts at most {most:,} steps",
                      {"series": s.label, "rows": m, "limit": most})
    return m


def survey(table: Any, cols: tab_series.Columns, opts: Options, task: dict[str, Any]) -> Survey:
    """Every series checked, in text order, before anything is forecast (each refusal of the series' rules, the known
    future's), with what the plan, the notes and the charts need of it; the arrays of a series are dropped after it."""
    if opts.series:
        labels, rows, empty = tab_series.split(table.column(opts.series))
        most = int(limit(task, "max_series"))
        if len(labels) > most:
            raise Refusal("too_many_series", f"the column «{opts.series}» makes {len(labels):,} series; this node forecasts at "
                                             f"most {most:,}: filter or aggregate first",
                          {"column": opts.series, "series": len(labels), "limit": most})
        no_series = int(empty.size)
    else:
        labels, rows, no_series = None, None, 0
    count = len(labels) if labels is not None else 1
    metas: list[Meta] = []
    for i in range(count):
        label = labels[i] if labels is not None else None
        s = series_of(cols, rows[i] if rows is not None else None, opts, label)
        metas.append(_meta(i, s, opts, task))
        del s
    return Survey(metas, labels, rows, no_series)


def _meta(i: int, s: tab_series.Series, opts: Options, task: dict[str, Any]) -> Meta:
    n = s.n
    h = _future(s, opts, task) if opts.known else opts.horizon
    k = (h if opts.known else opts.horizon) if opts.holdout is None else opts.holdout
    reason = None
    asked = max(0, k)
    if k <= 0:
        k, reason = 0, "no_holdout"
    elif n < k + opts.season + 1:
        k, reason = 0, "too_short"
    observed = s.y[~np.isnan(s.y)]
    x0 = s.x0()
    return Meta(index=i, label=s.label, n=n, h=h, k=k, asked=asked, reason=reason, context=min(n, MAX_CONTEXT),
                holdout_context=min(n - k, MAX_CONTEXT) if k else 0, total=float(np.abs(observed).sum()),
                first_x0=float(x0[0]), last_x0=float(x0[-1]), last_tick=s.times[-1], n_missing=s.n_missing,
                n_after=s.n_after, n_filled=s.n_filled, n_model_missing=s.n_model_missing,
                n_future=h if opts.known else 0, nonnegative=bool((observed >= 0).all()))


# ── the plan: blocks, batches, their cost ───────────────────────────────────────
def bucket(values: int) -> int:
    """The patches a context of `values` values is padded to: the next rung of 1, 2, 3 … 8, then four rungs a doubling
    (10, 12, 14, 16, 20, 24, 28, 32, 40 …), at most MAX_CONTEXT / PATCH; at most a quarter more than it needs."""
    p = max(1, math.ceil(values / PATCH))
    if p <= 8:
        return p
    step = 1 << ((p - 1).bit_length() - 3)     # a quarter of the power of two below p: four rungs a doubling
    return min(MAX_CONTEXT // PATCH, -(-p // step) * step)


def batch_rows(patches: int, horizon: int, rows_per_series: int) -> int:
    """The constant size of a batch of this shape: at most BATCH_MAX series, at most BATCH_TOKENS patches × rows, one at
    least. A function of the shape alone, so that a series' bits never depend on the others."""
    per = rows_per_series * (patches + math.ceil(horizon / PATCH))
    return max(1, min(BATCH_MAX, BATCH_TOKENS // per))


@dataclass
class Job:
    """One forecast: a series (`meta`), from its holdout's cut (`holdout`) or from its end, `context` values read, `h` steps."""

    meta: Meta
    holdout: bool
    context: int
    h: int

    @property
    def key(self) -> tuple[int, int]:
        return bucket(self.context), self.h


@dataclass
class Batch:
    jobs: list[Job]
    size: int                        # the constant size: the jobs, then copies of the last
    patches: int
    h: int

    def cost(self, rows_per_series: int, passes: int) -> float:
        """Patches × rows the model reads, the unit of the time budget's projection."""
        return float(self.size * rows_per_series * (self.patches + math.ceil(self.h / PATCH)) * passes)


@dataclass
class Block:
    """Series that follow each other in text order, forecast together and written as one row group."""

    metas: list[Meta]
    batches: list[Batch]


def plan(metas: list[Meta], rows_per_series: int) -> Iterator[Block]:
    """The blocks of series (at most BLOCK_POINTS places each, at least one series) and the batches of each, by shape, in
    the order of their shapes. The same series give the same plan."""
    block: list[Meta] = []
    points = 0
    for m in metas:
        size = m.n + m.h + m.k
        if block and points + size > BLOCK_POINTS:
            yield _block(block, rows_per_series)
            block, points = [], 0
        block.append(m)
        points += size
    if block:
        yield _block(block, rows_per_series)


def _block(metas: list[Meta], rows_per_series: int) -> Block:
    groups: dict[tuple[int, int], list[Job]] = {}
    for m in metas:
        if m.k:
            j = Job(m, True, m.holdout_context, m.k)
            groups.setdefault(j.key, []).append(j)
        j = Job(m, False, m.context, m.h)
        groups.setdefault(j.key, []).append(j)
    batches: list[Batch] = []
    for (patches, h), jobs in sorted(groups.items()):
        size = batch_rows(patches, h, rows_per_series)
        for at in range(0, len(jobs), size):
            batches.append(Batch(jobs[at:at + size], size, patches, h))
    return Block(metas, batches)


# ── the arrays of a batch ─────────────────────────────────────────────────────
@dataclass
class Arrays:
    """The library's inputs for one batch, `size` rows (the jobs, then copies of the last), the context padded on the left
    with NaN to `patches` × PATCH values: the target (B, T), the covariates (B, C, T) or (B, C, T + H), None without."""

    context: np.ndarray
    past: np.ndarray | None
    past_cat: np.ndarray | None
    known: np.ndarray | None
    known_cat: np.ndarray | None


def _rows_values(cov: Covariate, rows: np.ndarray) -> np.ndarray:
    """A covariate's values at input rows, NaN where there is no row (-1)."""
    out = np.full(rows.size, np.nan)
    ok = rows >= 0
    out[ok] = cov.values[rows[ok]]
    return out


def job_arrays(job: Job, s: tab_series.Series, covs: list[Covariate]) -> tuple[np.ndarray, list[np.ndarray]]:
    """One forecast's target context and its covariates' rows (past: the context; known: the context and the horizon),
    unpadded, in the order of `covs`."""
    end = s.n - (job.meta.k if job.holdout else 0)
    start = end - job.context
    target = s.z[start:end]
    rows: list[np.ndarray] = []
    for c in covs:
        if not c.known:
            rows.append(_rows_values(c, s.span_rows[start:end]))
        elif job.holdout:
            rows.append(_rows_values(c, s.span_rows[start:end + job.h]))
        else:
            rows.append(np.concatenate([_rows_values(c, s.span_rows[start:end]), c.values[s.after_rows[:job.h]]]))
    return target, rows


def batch_arrays(batch: Batch, series: dict[int, tab_series.Series], covs: list[Covariate]) -> Arrays:
    """The arrays of a batch: each job's, padded on the left with NaN to the batch's patches, then copies of the last job
    up to the batch's constant size. Counts the covariates' empty values the forecasts from the end read."""
    t = batch.patches * PATCH
    b = batch.size
    context = np.full((b, t), np.nan, dtype=np.float64)
    groups = {"past": [i for i, c in enumerate(covs) if not c.known and not c.categorical],
              "past_cat": [i for i, c in enumerate(covs) if not c.known and c.categorical],
              "known": [i for i, c in enumerate(covs) if c.known and not c.categorical],
              "known_cat": [i for i, c in enumerate(covs) if c.known and c.categorical]}
    out = {g: (np.full((b, len(ix), t + (batch.h if g.startswith("known") else 0)), np.nan) if ix else None)
           for g, ix in groups.items()}
    for r, job in enumerate(batch.jobs):
        target, rows = job_arrays(job, series[job.meta.index], covs)
        context[r, t - target.size:] = target
        for g, ix in groups.items():
            if out[g] is None:
                continue
            width = out[g].shape[-1]
            for j, ci in enumerate(ix):
                v = rows[ci]
                out[g][r, j, width - v.size:] = v
                if not job.holdout:
                    covs[ci].n_missing += int(np.count_nonzero(np.isnan(v)))
    last = len(batch.jobs) - 1
    if b > len(batch.jobs):
        context[last + 1:] = context[last]
        for g in out:
            if out[g] is not None:
                out[g][last + 1:] = out[g][last]
    return Arrays(context, out["past"], out["past_cat"], out["known"], out["known_cat"])
