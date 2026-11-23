"""The charts' draws of a Bayesian model (docs/specs/bayes-diagnostics.md, «The charts», lot X3): thinning for the traces and the pairs,
the rank histograms, the Gaussian kernel density and the units' points. Every series is a drawing, never a measure: nothing here
is compared with a threshold or a reference value, and no text says whether a chart is good.

The functions take the draws of ONE quantity (chains, draws) (or its pooled draws) and return small arrays: the thinned draws a
chart keeps (a chain's `bayes_trace_points`, a density's points, a pair's `bayes_pair_points`) are all that stays in memory of the
second pass's blocks. The `write_*` functions turn them into rows of the ten columns through a ResultsWriter-like object (`points`).

The ranks of a histogram are the average ranks of the pooled draws (the rank normalization's, `bayes_diag.average_ranks`): all S
draws of the quantity, each chain's ranks counted in `bins` equal bins of 1…S. The density is a Gaussian kernel with Silverman's
bandwidth h = 0.9·min(sd, IQR/1.34)·n^(−1/5) (Silverman 1986); a draw further than 8h from a point contributes less than 1e-14 of
its peak there, so each point sums the sorted draws of its window only, in chunks (exact to that tolerance, never a quadratic
array).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

import bayes_diag

NORMAL_SPAN = 8.0       # kernel windows: exp(-32) = 1.3e-14
CHUNK = 1 << 20         # values a window's sum holds at a time (8 MiB)


def thin_indices(n: int, k: int) -> np.ndarray:
    """At most `k` evenly spaced indices of 0…n−1, the last included (all of them when n ≤ k)."""
    if n <= 0 or k <= 0:
        return np.zeros(0, dtype=np.int64)
    if n <= k:
        return np.arange(n, dtype=np.int64)
    if k == 1:
        return np.array([n - 1], dtype=np.int64)
    return np.rint(np.linspace(0, n - 1, k)).astype(np.int64)


def rank_histogram(draws: np.ndarray, bins: int) -> tuple[np.ndarray, np.ndarray]:
    """Draws (chains, n) → (counts (chains, bins), edges (bins + 1,)): the average ranks of the pooled draws, each chain's ranks
    counted in `bins` equal bins of 1…S (a rank of S in the last bin)."""
    m = draws.shape[0]
    s = draws.size
    ranks = bayes_diag.average_ranks(draws.reshape(1, s))[0].reshape(draws.shape)
    edges = 1.0 + (s - 1.0) * np.arange(bins + 1) / bins
    if s <= 1:
        index = np.zeros(draws.shape, dtype=np.int64)
    else:
        index = np.minimum(np.floor((ranks - 1.0) * bins / (s - 1.0)).astype(np.int64), bins - 1)
    counts = np.zeros((m, bins), dtype=np.int64)
    for c in range(m):
        counts[c] = np.bincount(index[c], minlength=bins)
    return counts, edges


def bandwidth(sorted_draws: np.ndarray) -> float:
    """Silverman's rule of thumb on sorted draws; 0 when the draws are constant (no density)."""
    n = sorted_draws.shape[0]
    if n < 2:
        return 0.0
    sd = float(np.std(sorted_draws, ddof=1))
    q25, q75 = (float(v) for v in bayes_diag.quantiles(sorted_draws[None, :], (0.25, 0.75))[0])
    spread = min(sd, (q75 - q25) / 1.34) if q75 > q25 else sd
    h = 0.9 * spread * n ** -0.2
    return h if math.isfinite(h) and h > 0 else 0.0


def kernel_density(sorted_draws: np.ndarray, grid: np.ndarray, h: float) -> np.ndarray:
    """The Gaussian kernel density of sorted draws at `grid`, with bandwidth `h` > 0."""
    n = sorted_draws.shape[0]
    out = np.zeros(grid.shape[0])
    lo = np.searchsorted(sorted_draws, grid - NORMAL_SPAN * h, side="left")
    hi = np.searchsorted(sorted_draws, grid + NORMAL_SPAN * h, side="right")
    for j, g in enumerate(grid):
        total = 0.0
        for a in range(int(lo[j]), int(hi[j]), CHUNK):
            z = (sorted_draws[a:min(a + CHUNK, int(hi[j]))] - g) / h
            total += float(np.exp(-0.5 * z * z).sum())
        out[j] = total
    return out / (n * h * math.sqrt(2.0 * math.pi))


class Density:
    """The density chart of a quantity: the grid and the curves, and what the range cut (a count of draws, a fact)."""

    def __init__(self, grid: np.ndarray, posterior: np.ndarray, prior: np.ndarray | None, cut_posterior: int, cut_prior: int) -> None:
        self.grid, self.posterior, self.prior = grid, posterior, prior
        self.cut_posterior, self.cut_prior = cut_posterior, cut_prior


def density(posterior: np.ndarray, prior: np.ndarray | None, points: int) -> Density | None:
    """The density curves of a quantity's pooled draws and, when there are some, of its prior's. The range spans the posterior's
    0.5–99.5 % quantiles widened to the prior's 5–95 %; the draws outside are cut (counted). None when the posterior's draws are
    constant (no bandwidth: there is no curve to draw)."""
    ordered = np.sort(np.asarray(posterior, dtype=float).reshape(-1))
    h = bandwidth(ordered)
    if h == 0.0:
        return None
    lo, hi = (float(v) for v in bayes_diag.quantiles(ordered[None, :], (0.005, 0.995))[0])
    prior_sorted = None
    if prior is not None and np.asarray(prior).size > 1:
        prior_sorted = np.sort(np.asarray(prior, dtype=float).reshape(-1))
        plo, phi = (float(v) for v in bayes_diag.quantiles(prior_sorted[None, :], (0.05, 0.95))[0])
        lo, hi = min(lo, plo), max(hi, phi)
    if not hi > lo:
        return None
    grid = np.linspace(lo, hi, points)
    cut_post = int((ordered < lo).sum() + (ordered > hi).sum())
    curve = kernel_density(ordered, grid, h)
    prior_curve, cut_prior = None, 0
    if prior_sorted is not None:
        hp = bandwidth(prior_sorted)
        if hp > 0.0:
            prior_curve = kernel_density(prior_sorted, grid, hp)
            cut_prior = int((prior_sorted < lo).sum() + (prior_sorted > hi).sum())
    return Density(grid, curve, prior_curve, cut_post, cut_prior)


def unit_selection(medians: np.ndarray, cap: int, seed: int) -> np.ndarray:
    """The positions (in the plate's order) a `units:` chart shows: all of them up to `cap`; above it the ten medians furthest from
    the median of the medians always, the rest a seeded sample without replacement (the models spec's rule, a smaller cap)."""
    n = medians.shape[0]
    if n <= cap:
        return np.arange(n)
    finite = np.where(np.isfinite(medians), medians, np.nanmedian(medians) if np.isfinite(medians).any() else 0.0)
    distance = np.abs(finite - np.median(finite))
    keep = min(10, cap)
    extreme = np.argsort(-distance, kind="stable")[:keep]
    rest = np.setdiff1d(np.arange(n), extreme)
    chosen = np.random.default_rng(seed).choice(rest, size=cap - keep, replace=False)
    return np.sort(np.concatenate((extreme, chosen)))


# ── the rows ──────────────────────────────────────────────────────────────────
def write_trace(w: Any, name: str, draws: np.ndarray, k: int) -> None:
    """`curve|trace:<name>|<chain>|value|<i>`: each chain's `k` thinned draws, x0 the draw's number (from 1)."""
    index = thin_indices(draws.shape[1], k)
    for c in range(draws.shape[0]):
        w.points("curve", f"trace:{name}", "value", index + 1.0, draws[c, index], level_2=str(c + 1))


def write_rank(w: Any, name: str, draws: np.ndarray, bins: int) -> None:
    """`hist|rank:<name>|<chain>|count|<i>`: x0 and x1 the bin's edges in 1…S, value the chain's count."""
    counts, edges = rank_histogram(draws, bins)
    for c in range(counts.shape[0]):
        w.points("hist", f"rank:{name}", "count", edges[:-1], counts[c].astype(float), level_2=str(c + 1), x1=edges[1:])


def write_density(w: Any, name: str, d: Density) -> None:
    """`curve|density:<name>|posterior|value|<i>` (and `prior`): x0 the grid, value the density."""
    w.points("curve", f"density:{name}", "value", d.grid, d.posterior, level_2="posterior")
    if d.prior is not None:
        w.points("curve", f"density:{name}", "value", d.grid, d.prior, level_2="prior")


def write_pair(w: Any, a: str, b: str, xa: np.ndarray, xb: np.ndarray, divergent: np.ndarray) -> None:
    """`curve|pairs:<a>:<b>|draw|value|<i>` and `…|divergent|…`: x0 the draw of a, value the draw of b."""
    ok = ~divergent
    w.points("curve", f"pairs:{a}:{b}", "value", xa[ok], xb[ok], level_2="draw")
    w.points("curve", f"pairs:{a}:{b}", "value", xa[divergent], xb[divergent], level_2="divergent")


def write_units(w: Any, name: str, units: list[str | None], q5: np.ndarray, q50: np.ndarray, q95: np.ndarray, cap: int, seed: int) -> None:
    """`point|units:<name>|median|y|<i>` (and `low`, `high`): x0 the unit's rank by its median, text the unit, value the summary's
    q50 (q5, q95)."""
    shown = unit_selection(q50, cap, seed)
    order = np.argsort(q50, kind="stable")
    rank = np.empty(q50.shape[0])
    rank[order] = np.arange(1, q50.shape[0] + 1)
    shown = shown[np.argsort(rank[shown], kind="stable")]
    labels = [units[i] if units[i] is not None else str(i + 1) for i in shown]
    for series, values in (("median", q50), ("low", q5), ("high", q95)):
        w.points("point", f"units:{name}", "y", rank[shown], values[shown], level_2=series, labels=labels)
