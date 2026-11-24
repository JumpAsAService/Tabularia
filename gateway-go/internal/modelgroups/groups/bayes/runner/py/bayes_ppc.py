"""The second pass over the checks file (docs/specs/bayes-predictive-checks.md, «The second pass over the checks file»; lot P3b): the
`ppc` (or `prior_check`) rows and the overlaid densities of the predictive checks. Only charts: a histogram of each statistic's
replicated draws with T(y) beside it, and the observed density with a few replicated ones. **No p-value, no probability, no share** is
computed or written; the draws only give counts, which a chart draws.

    report = bayes_checks.plan(task).rows(checks_path, writer, "ppc", seconds_left)      # or "prior_check"

THE FILE. `checks.parquet` is the draws file of `bayes_draws.DrawsWriter` with the check columns only: quantity-major row groups, a block
of draws each (chain, first draw, count in its metadata). Its columns are named `Ppc_o<k>_<name>[i]`: a statistic one column, the bins'
counts `Ppc_o<k>_count[1…slots]`, each checked unit's sum `Ppc_o<k>_usum[1…units]`; the order is checked once against what the plan
knows (ManifestMismatch otherwise). Only the `value` column is read, a row group at a time: memory is one row group and the accumulators.

THE DROPPED DRAWS. A draw whose generated quantities threw (a generator refusing its arguments) is written by CmdStan with every
generated quantity NaN: such a draw has a non-finite value in some column, and is **counted and left out** of everything; the note
`checks_dropped` says how many (the writer of the file must not refuse a non-finite check: `n_unchecked` = every column).

TWO PASSES over the file. The first finds, for each series (the statistics and the checked units' means), the smallest and largest of its
valid draws and counts the valid draws; the second counts them into 30 equal bins between the smaller of the smallest and T(y) and the
larger of the largest and T(y) (so the histogram holds T(y); a range of zero is widened by 0.5 each side) and picks the bins' counts of
the `density_draws` valid draws evenly spaced in the chain-major order (all of chain 1's valid draws, then chain 2's…: the same whatever
the file's blocks). Nothing is held but
the series' counts and those few draws' bin counts.

THE ROWS (per checked outcome `<y>`, the outcome's Stan identifier):
  `<section>|<y>||n`                               the outcome's rows (lot Q4: the report's count of the values drawn outside the data's
                                                   range needs it, an outcome with a lag having fewer rows than the model's `n`)
  `<section>|<y>:<stat>||observed`                 T(y)
  `<section>|<y>:<stat>||count|<i>`                x0, x1 the bin's edges, value its count of replicated draws (at most 30 rows)
  `<section>|<y>:unit_mean|<unit>|observed` and `…|count|<i>`   the same for each checked unit's mean
  `curve|ppc_density:<y>|observed|value|<i>` and `…|draw_<j>|value|<i>` (`prior_density` for `prior_check`): x0 the bin's middle (the outer
      two slots, «below» and «above» the observed range, at its edges), value count / (rows × width)
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

import bayes_charts
from bayes_draws import read_metadata
from bayes_task import ManifestMismatch, note_line
from tab_protocol import Refusal

SECTIONS = {"ppc": ("ppc_density", "predictive"), "prior_check": ("prior_density", "prior predictive")}
DEFAULT_STAT_BINS = 30
DEFAULT_DENSITY_DRAWS = 20
_NAME = re.compile(r"^(?:Ppc|Prior)_o([0-9]+)_([a-z0-9]+)\[([0-9]+)\]$")


@dataclass
class PpcReport:
    """What the pass tells its caller (facts of the run)."""
    draws: int = 0                 # the draws the file holds
    valid: int = 0                 # of them, those whose generated quantities are numbers
    dropped: int = 0               # the others, left out
    rows: int = 0                  # rows written (statistics, counts, curves)
    charts: list[str] = field(default_factory=list)
    seconds: float = 0.0
    passes: int = 2


@dataclass
class _Series:
    """A statistic's (or a unit's mean's) column and what the chart compares it with."""
    outcome: int                   # index in the plan's outcomes
    stat: str                      # mean, sd, … or unit_mean
    position: int                  # the column's place in the file's quantities
    observed: float
    unit: str | None = None
    rows: int = 1                  # a unit's rows: the sum divided by it
    lo: float = math.inf
    hi: float = -math.inf
    edges: np.ndarray | None = None
    counts: np.ndarray | None = None


def _columns(plan: Any, names: list[str]) -> tuple[list[_Series], list[np.ndarray]]:
    """The series and, for each outcome, the positions of its bins' counts; the file's names against what the plan knows."""
    found: dict[tuple[int, str], dict[int, int]] = {}
    for position, name in enumerate(names):
        m = _NAME.match(name)
        if m:
            found.setdefault((int(m.group(1)), m.group(2)), {})[int(m.group(3))] = position
    series: list[_Series] = []
    count_positions: list[np.ndarray] = []
    for n, o in enumerate(plan.outcomes):
        if o.bins is None or o.counts is None:
            raise ManifestMismatch(f"the checks of {o.stan} were not observed in pass A")
        for stat in o.statistics:
            at = found.get((o.k, stat))
            if at is None or sorted(at) != [1]:
                raise ManifestMismatch(f"the checks file has no single column for {stat} of the outcome {o.k}")
            series.append(_Series(n, stat, at[1], float(o.observed[stat])))
        at = found.get((o.k, "count"))
        if at is None or sorted(at) != list(range(1, o.bins.slots + 1)):
            raise ManifestMismatch(f"the checks file has not the {o.bins.slots} bin counts of the outcome {o.k}")
        count_positions.append(np.array([at[i] for i in range(1, o.bins.slots + 1)], dtype=np.int64))
        if o.unit_texts:
            at = found.get((o.k, "usum"))
            if at is None or sorted(at) != list(range(1, o.units + 1)):
                raise ManifestMismatch(f"the checks file has not the {o.units} unit sums of the outcome {o.k}")
            means = o.unit_means()
            for slot, text in enumerate(o.unit_texts, 1):          # the slot of the other units is never drawn
                series.append(_Series(n, "unit_mean", at[slot], float(means[slot - 1]), text, int(o.unit_rows[slot - 1])))
    return series, count_positions


def _names(pf: pq.ParquetFile, first_count: int) -> list[str]:
    """The quantities' names, in the order of the first row group (quantity-major: each quantity's draws are contiguous)."""
    column = pf.read_row_group(0, columns=["quantity"]).column(0).combine_chunks()
    if pa.types.is_dictionary(column.type):
        codes = column.indices.to_numpy(zero_copy_only=False).astype(np.int64)
        labels = column.dictionary.to_pylist()
    else:
        unique, codes = np.unique(np.array(column.to_pylist(), dtype=object), return_inverse=True)
        labels = list(unique)
    p = len(labels)
    if codes.shape[0] != p * first_count:
        raise ManifestMismatch(f"the first row group of the checks has {codes.shape[0]} rows, not {p} quantities × {first_count} draws")
    grid = codes.reshape(p, first_count)
    if not (grid == grid[:, :1]).all():
        raise ManifestMismatch("the checks file is not quantity-major: a quantity's draws are not contiguous in its first row group")
    return [labels[i] for i in grid[:, 0]]


def _series_values(grid: np.ndarray, series: list[_Series], valid: np.ndarray) -> np.ndarray:
    """(series, valid draws): the statistics' draws and the units' sums over their rows."""
    out = np.empty((len(series), int(valid.sum())))
    for m, s in enumerate(series):
        v = grid[s.position][valid]
        out[m] = v / s.rows if s.stat == "unit_mean" else v
    return out


def second_pass(plan: Any, checks_path: str, writer: Any, section: str, seconds_left: float | None = None, *,
                read_row_group: Callable[..., Any] | None = None) -> PpcReport:
    """Reads the checks file twice and writes the rows of `section` (`ppc` or `prior_check`) and its density charts; see the module."""
    if section not in SECTIONS:
        raise ValueError(f"section {section!r}")
    chart = SECTIONS[section][0]
    t_start = time.monotonic()
    report = PpcReport()
    if not plan.outcomes:
        return report
    checks = plan.task.checks if isinstance(plan.task.checks, dict) else {}
    stat_bins = int(checks.get("stat_bins") or DEFAULT_STAT_BINS)
    density_draws = int(checks.get("density_draws") or DEFAULT_DENSITY_DRAWS)
    meta = read_metadata(checks_path)
    groups = [tuple(int(v) for v in g) for g in meta["row_groups"]]
    pf = pq.ParquetFile(checks_path)
    if pf.metadata.num_row_groups != len(groups) or not groups:
        raise ManifestMismatch("the checks file's row groups are not the ones its metadata lists")
    read = read_row_group or pf.read_row_group
    names = _names(pf, groups[0][2])
    if int(meta.get("quantities", len(names))) != len(names):
        raise ManifestMismatch(f"the checks file holds {meta.get('quantities')} quantities and its first row group {len(names)}")
    series, count_positions = _columns(plan, names)
    p = len(names)
    report.draws = sum(g[2] for g in groups)

    def projected(t0: float) -> None:
        estimate = (time.monotonic() - t0) * len(groups) * 2
        if seconds_left is not None and estimate > seconds_left:
            raise Refusal("too_slow", f"the second pass of the predictive checks would take about {int(estimate)} seconds and "
                                      f"{int(seconds_left)} are left: sample fewer draws or give the run more time",
                          {"projected_seconds": int(estimate), "seconds_left": int(seconds_left)})

    def block(i: int) -> tuple[np.ndarray, np.ndarray]:
        count = groups[i][2]
        values = read(i, columns=["value"]).column(0).to_numpy()
        if values.shape[0] != p * count:
            raise ManifestMismatch(f"the row group {i + 1} of the checks holds {values.shape[0]} values, not {p} × {count}")
        grid = values.reshape(p, count)
        return grid, np.isfinite(grid).all(axis=0)

    # pass 1: the valid draws (in all and by chain) and each series' range
    by_chain: dict[int, int] = {}
    for i in range(len(groups)):
        t0 = time.monotonic()
        grid, valid = block(i)
        nv = int(valid.sum())
        report.valid += nv
        by_chain[groups[i][0]] = by_chain.get(groups[i][0], 0) + nv
        if nv:
            values = _series_values(grid, series, valid)
            lows, highs = values.min(axis=1), values.max(axis=1)
            for m, s in enumerate(series):
                s.lo, s.hi = min(s.lo, float(lows[m])), max(s.hi, float(highs[m]))
        del grid, valid
        if i == 0:
            projected(t0)
    report.dropped = report.draws - report.valid
    for s in series:
        lo, hi = (min(s.lo, s.observed), max(s.hi, s.observed)) if report.valid else (s.observed, s.observed)
        if not hi > lo:
            lo, hi = lo - 0.5, hi + 0.5
        s.edges = lo + (hi - lo) * np.arange(stat_bins + 1) / stat_bins
        s.edges[-1] = hi
        s.counts = np.zeros(stat_bins, dtype=np.int64)

    # pass 2: the series' bin counts, and the bin counts of the evenly spaced draws
    chosen = bayes_charts.thin_indices(report.valid, density_draws)
    kept = [np.zeros((chosen.shape[0], c.shape[0])) for c in count_positions]
    offset, running = {}, {}
    for chain in sorted(by_chain):
        offset[chain], running[chain] = sum(by_chain[c] for c in by_chain if c < chain), 0
    for i in range(len(groups)):
        if seconds_left is not None and time.monotonic() - t_start > seconds_left:
            raise Refusal("too_slow", "the second pass of the predictive checks passed the time left: give the run more time",
                          {"seconds_left": int(seconds_left)})
        grid, valid = block(i)
        nv = int(valid.sum())
        if nv:
            values = _series_values(grid, series, valid)
            for m, s in enumerate(series):
                width = (s.edges[-1] - s.edges[0]) / stat_bins
                index = np.clip(np.floor((values[m] - s.edges[0]) / width).astype(np.int64), 0, stat_bins - 1)
                s.counts += np.bincount(index, minlength=stat_bins)
            chain = groups[i][0]
            seen = offset[chain] + running[chain]                         # the position of this block's first valid draw
            running[chain] += nv
            first = int(np.searchsorted(chosen, seen, side="left"))
            last = int(np.searchsorted(chosen, seen + nv, side="left"))
            if last > first:
                local = chosen[first:last] - seen                        # among this block's valid draws
                at = np.flatnonzero(valid)[local]
                for n, positions in enumerate(count_positions):
                    kept[n][first:last] = grid[positions][:, at].T
        del grid, valid

    # the rows
    for n, o in enumerate(plan.outcomes):
        writer.number(section, "n", float(o.n), level=o.stan)
        report.rows += 1
        for s in (s for s in series if s.outcome == n):
            level = f"{o.stan}:{s.stat}"
            writer.number(section, "observed", s.observed, level=level, level_2=s.unit)
            report.rows += 1
            if report.valid:
                writer.points(section, level, "count", s.edges[:-1], s.counts.astype(float), level_2=s.unit, x1=s.edges[1:])
                report.rows += stat_bins
        _density(writer, o, kept[n], chart, report)
    if report.dropped:
        writer.note("checks_dropped", note_line("checks_dropped", dropped=report.dropped, draws=report.draws, valid=report.valid))
    report.seconds = time.monotonic() - t_start
    return report


def _density(writer: Any, o: Any, draws: np.ndarray, chart: str, report: PpcReport) -> None:
    """The observed density and the chosen draws' on the outcome's bins: count / (rows × width) at the bins' middles, the two outer
    slots (below and above the observed range) at the range's edges."""
    edges = o.bins.edges()
    x = np.concatenate(([edges[0]], (edges[:-1] + edges[1:]) / 2.0, [edges[-1]]))
    scale = 1.0 / (o.n * o.bins.width)
    name = f"{chart}:{o.stan}"
    writer.points("curve", name, "value", x, o.counts * scale, level_2="observed")
    for j in range(draws.shape[0]):
        writer.points("curve", name, "value", x, draws[j] * scale, level_2=f"draw_{j + 1}")
    report.rows += x.shape[0] * (1 + draws.shape[0])
    report.charts.append(name)
