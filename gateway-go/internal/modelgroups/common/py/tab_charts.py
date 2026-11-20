"""The points of the charts, capped (the twin of common/R/charts.R; docs: «Points are capped»). A
runner sends numbers and points, never pictures: the report engine draws them. Per chart at most
max_chart_points; above it a sample drawn with the run's generator, always keeping the most
extreme points, labelled with their row numbers; a histogram counts all the values; a curve of
many steps (a learning curve) is thinned to every k-th step, plus the steps that must be seen.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np


def cap(n: int, rng: Any, most: int = 2000, score: Any = None, keep: int = 10) -> np.ndarray:
    """The indices (from 0, increasing) of the points to draw: all up to `most`; above it the
    `keep` largest by |score| (|residual|…) and a sample of the rest drawn with `rng` (the run's
    generator: tab_protocol.rng)."""
    if n <= most:
        return np.arange(n)
    extreme = np.array([], dtype=np.int64)
    if score is not None:
        s = np.abs(np.asarray(score, dtype=float))
        extreme = np.lexsort((np.arange(n), -s))[:keep]  # ties: the first rows
    rest = np.setdiff1d(np.arange(n), extreme)
    drawn = rng.choice(rest, size=most - len(extreme), replace=False)
    return np.sort(np.concatenate([extreme, drawn]))


def extreme_labels(score: Any, rows: Any, keep: int = 10) -> list[str | None]:
    """The labels of the extreme points (their row numbers in the input, from 1), None elsewhere."""
    s = np.abs(np.asarray(score, dtype=float))
    out: list[str | None] = [None] * len(s)
    for i in np.lexsort((np.arange(len(s)), -s))[:keep]:
        out[i] = str(int(np.asarray(rows)[i]))
    return out


def hist(x: Any, min_bins: int = 5, max_bins: int = 30, bins: int | None = None,
         lo: float | None = None, hi: float | None = None) -> dict[str, np.ndarray]:
    """A histogram of all the finite values: `bins` intervals (by default between `min_bins` and
    `max_bins`, Sturges' rule within them, as the statistics' reports count) from `lo` to `hi`
    (the values' range by default); intervals (a, b], the first [a, b], as R's hist."""
    v = np.asarray(x, dtype=float)
    v = v[np.isfinite(v)]
    if bins is None:
        if not len(v):
            return {"x0": np.array([]), "x1": np.array([]), "count": np.array([])}
        bins = min(max_bins, max(min_bins, math.ceil(math.log2(len(v)) + 1)))
    low = float(v.min()) if lo is None else lo
    high = float(v.max()) if hi is None else hi
    if low == high:
        low, high = low - 0.5, high + 0.5
    breaks = np.linspace(low, high, bins + 1)
    i = np.searchsorted(breaks, v, side="left")  # breaks[i-1] < v <= breaks[i]
    i = np.clip(i, 1, bins) - 1
    inside = (v >= low) & (v <= high)
    counts = np.bincount(i[inside], minlength=bins).astype(float)
    return {"x0": breaks[:-1], "x1": breaks[1:], "count": counts}


def thin(n: int, most: int = 2000, keep: Iterable[int] = ()) -> np.ndarray:
    """The indices (from 0, increasing) of a curve of `n` steps drawn with at most `most` points:
    all of them up to `most`; above, every k-th step from the first plus the last and the steps
    of `keep` (the best round), k chosen so that they make at most `most`."""
    if n <= most:
        return np.arange(n)
    must = sorted({n - 1, *(int(k) for k in keep if 0 <= int(k) < n)})
    step = math.ceil(n / max(1, most - len(must)))
    return np.unique(np.concatenate([np.arange(0, n, step), np.asarray(must, dtype=np.int64)]))
