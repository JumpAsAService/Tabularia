"""The measures of Forecast (Timestral) on the holdout, and its one test (docs/specs/foundation-forecast.md, «The baseline and
the measures», «The comparison test»): the seasonal naïve forecast, the scale of a series, MASE, sMAPE, the pinball loss and
the weighted quantile loss, the coverage of the 80 % and 90 % bands — measures, without verdict — and Wilcoxon's signed-rank
test across the series, whose p-value against the node's alpha is the only verdict of the node.

Nothing here keeps a series: `Measured.of` reads one series' holdout and returns its numbers, `Across` adds them up series by
series (sums and counts, never the series), `wilcoxon` reads one difference per series. Written with numpy only; the witness
(reference/make_reference.py) computes the same numbers again by the formulas and with scipy.stats.wilcoxon.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# the 21 quantile levels of Timestral (config.json's quantile_knots, the same in the four sizes)
LEVELS = (0.01, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99)
MEDIAN = LEVELS.index(0.5)
LOW80, HIGH80 = LEVELS.index(0.1), LEVELS.index(0.9)
LOW90, HIGH90 = LEVELS.index(0.05), LEVELS.index(0.95)
_Q = np.asarray(LEVELS, dtype=np.float64)

# Wilcoxon's exact distribution up to this many differences (scipy's `method = "auto"`), the enumeration of the sign
# patterns up to this many when there are ties or zeros (scipy's permutation test, exact below 2**13 patterns)
EXACT_MAX = 50
PERMUTATION_MAX = 13


# ── the baseline and the scale ─────────────────────────────────────────────────
def seasonal_naive(history: np.ndarray, steps: int, season: int) -> np.ndarray:
    """The seasonal naïve forecast of `steps` steps after `history`: ŷ_{T+h} = y_{T+h−m⌈h/m⌉}, the last season repeated (the
    last value with m = 1). A value missing there (missing = model) is taken one season earlier, and so on; NaN when the
    history holds none."""
    y = np.asarray(history, dtype=np.float64)
    m = max(1, int(season))
    t = y.size
    h = np.arange(1, steps + 1, dtype=np.int64)
    at = t - 1 + h - m * ((h + m - 1) // m)       # 0-based: y[T + h − m⌈h/m⌉ − 1]
    out = np.full(steps, np.nan)
    ok = at >= 0
    out[ok] = y[at[ok]]
    gone = np.flatnonzero(np.isnan(out) & ok)
    for i in gone:                                 # only with missing values: a few places
        j = at[i] - m
        while j >= 0 and np.isnan(y[j]):
            j -= m
        if j >= 0:
            out[i] = y[j]
    return out


def scale(history: np.ndarray, season: int) -> float:
    """sᵢ: the mean of |y_τ − y_{τ−m}| over the history, the pairs whose two values were observed (the in-sample seasonal
    naïve's error, m = 1 without a season); NaN without such a pair."""
    y = np.asarray(history, dtype=np.float64)
    m = max(1, int(season))
    if y.size <= m:
        return math.nan
    d = np.abs(y[m:] - y[:-m])
    d = d[~np.isnan(d)]
    return float(d.mean()) if d.size else math.nan


# ── one series ─────────────────────────────────────────────────────────────────
@dataclass
class Measured:
    """The measures of one series on its holdout (NaN where they cannot be said), and the sums the series adds to the totals.
    `steps`: the holdout's values measured (observed, with a baseline), `in80`/`in90` of them inside the bands."""

    scale: float = math.nan
    mase: float = math.nan
    baseline_mase: float = math.nan
    relative_mase: float = math.nan
    smape: float = math.nan
    baseline_smape: float = math.nan
    pinball: float = math.nan
    wql: float = math.nan
    coverage_80: float = math.nan
    coverage_90: float = math.nan
    steps: int = 0
    in80: int = 0
    in90: int = 0
    abs_sum: float = 0.0           # Σ|y_t| over the steps measured
    smape_zero: int = 0            # steps left out of sMAPE: the value and the forecast both 0
    reason: str | None = None      # why it is not measured: too_short, zero_scale, other

    @classmethod
    def of(cls, history: np.ndarray, actual: np.ndarray, quantiles: np.ndarray, baseline: np.ndarray, season: int) -> "Measured":
        """The measures of one series: `history` the values before the holdout (NaN where none), `actual` the holdout's
        values, `quantiles` its forecast (steps × 21, any float type), `baseline` the seasonal naïve's forecast of it."""
        s = scale(history, season)
        if not math.isfinite(s):
            return cls(reason="too_short")
        if s == 0:
            return cls(scale=s, reason="zero_scale")
        y = np.asarray(actual, dtype=np.float64)
        q = np.asarray(quantiles, dtype=np.float64)
        b = np.asarray(baseline, dtype=np.float64)
        ok = ~np.isnan(y) & ~np.isnan(b)
        if not ok.any():
            return cls(scale=s, reason="other")
        y, q, b = y[ok], q[ok], b[ok]
        med = q[:, MEDIAN]
        err, berr = np.abs(y - med), np.abs(y - b)
        mase, bmase = float(err.mean() / s), float(berr.mean() / s)
        u = y[:, None] - q
        pinball = float(np.maximum(_Q * u, (_Q - 1.0) * u).mean())
        abs_sum = float(np.abs(y).sum())
        in80 = int(np.count_nonzero((q[:, LOW80] <= y) & (y <= q[:, HIGH80])))
        in90 = int(np.count_nonzero((q[:, LOW90] <= y) & (y <= q[:, HIGH90])))
        smape, zero = _smape(y, med)
        bsmape, _ = _smape(y, b)
        n = int(y.size)
        mean_abs = abs_sum / n
        return cls(scale=s, mase=mase, baseline_mase=bmase, relative_mase=mase / bmase if bmase > 0 else math.nan,
                   smape=smape, baseline_smape=bsmape, pinball=pinball, wql=pinball / mean_abs if mean_abs > 0 else math.nan,
                   coverage_80=in80 / n, coverage_90=in90 / n, steps=n, in80=in80, in90=in90, abs_sum=abs_sum, smape_zero=zero)

    @property
    def measured(self) -> bool:
        return self.reason is None


def _smape(y: np.ndarray, f: np.ndarray) -> tuple[float, int]:
    """sMAPE in %, the steps where the value and the forecast are both 0 left out (and counted)."""
    den = np.abs(y) + np.abs(f)
    keep = den > 0
    if not keep.any():
        return math.nan, int(y.size)
    return float((200.0 * np.abs(y - f)[keep] / den[keep]).mean()), int(np.count_nonzero(~keep))


# ── across the series ─────────────────────────────────────────────────────────
@dataclass
class Across:
    """The measures across the series, added series by series: the means of MASE and sMAPE, the geometric mean of the
    relative MASE, the pooled coverages, the pooled weighted quantile loss Σ pinball·steps / Σ|y| (each series' WQL weighted
    by the size of its values), and one difference of MASE per series for the test."""

    n: int = 0
    mase: float = 0.0
    baseline_mase: float = 0.0
    smape: float = 0.0
    smape_n: int = 0
    baseline_smape: float = 0.0
    baseline_smape_n: int = 0
    log_relative: float = 0.0
    relative_n: int = 0
    pinball_steps: float = 0.0
    abs_sum: float = 0.0
    steps: int = 0
    in80: int = 0
    in90: int = 0
    better: int = 0
    diffs: list[float] = field(default_factory=list)

    def add(self, m: Measured) -> None:
        if not m.measured:
            return
        self.n += 1
        self.mase += m.mase
        self.baseline_mase += m.baseline_mase
        if math.isfinite(m.smape):
            self.smape += m.smape
            self.smape_n += 1
        if math.isfinite(m.baseline_smape):
            self.baseline_smape += m.baseline_smape
            self.baseline_smape_n += 1
        if math.isfinite(m.relative_mase) and m.relative_mase > 0:
            self.log_relative += math.log(m.relative_mase)
            self.relative_n += 1
        self.pinball_steps += m.pinball * m.steps
        self.abs_sum += m.abs_sum
        self.steps += m.steps
        self.in80 += m.in80
        self.in90 += m.in90
        self.better += int(m.mase < m.baseline_mase)
        self.diffs.append(m.mase - m.baseline_mase)

    def numbers(self) -> dict[str, float]:
        """The model section's numbers (NaN where no series was measured)."""
        nan = math.nan

        def mean(total: float, n: int) -> float:
            return total / n if n else nan

        return {"mase": mean(self.mase, self.n), "baseline_mase": mean(self.baseline_mase, self.n),
                "relative_mase_geomean": math.exp(self.log_relative / self.relative_n) if self.relative_n else nan,
                "smape": mean(self.smape, self.smape_n), "baseline_smape": mean(self.baseline_smape, self.baseline_smape_n),
                "wql": self.pinball_steps / self.abs_sum if self.abs_sum > 0 else nan,
                "coverage_80": mean(self.in80, self.steps), "coverage_90": mean(self.in90, self.steps), "n_measured": self.n}


# ── Wilcoxon's signed-rank test ─────────────────────────────────────────────────
@dataclass
class Wilcoxon:
    """The test on the differences d (zeros left out, `zeros` of them): W+ and W− the sums of the ranks of |d| of the
    positive and the negative ones, `statistic` the smaller, `p_value` two-sided, `method` exact (the distribution of the
    sign patterns, no tie and no zero, at most 50), permutation (the 2ⁿ sign patterns counted, with ties or zeros, at most
    13 differences) or normal (the normal approximation with the tie correction, no continuity correction), `min_p` the
    smallest p-value the test could give with these differences."""

    n: int
    zeros: int
    w_plus: float
    w_minus: float
    statistic: float
    p_value: float
    method: str
    min_p: float
    z: float = math.nan

    @property
    def rank_biserial(self) -> float:
        """(W+ − W−) / (W+ + W−), the matched-pairs rank-biserial correlation: its sign is the side the test leans to
        (negative: the negative differences have the larger ranks). The side of a rejection is read here, not from the
        median of the differences, which can lie on the other side when the large differences are few (lot F9)."""
        total = self.w_plus + self.w_minus
        return (self.w_plus - self.w_minus) / total if total > 0 else 0.0


def _ranks(a: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The average ranks of a (1 = the smallest) and the sizes of the groups of ties."""
    order = np.argsort(a, kind="mergesort")
    s = a[order]
    starts = np.flatnonzero(np.r_[True, s[1:] != s[:-1]])
    sizes = np.diff(np.r_[starts, s.size])
    avg = starts + (sizes + 1) / 2.0               # the mean of the positions start+1 … start+size
    ranks = np.empty(a.size)
    ranks[order] = np.repeat(avg, sizes)
    return ranks, sizes


def _sign_counts(units: np.ndarray) -> np.ndarray:
    """The number of sign patterns giving each sum Σ (the units of the positive ones), 0 … Σ units, by dynamic programming
    (whole numbers: the ranks in halves)."""
    total = int(units.sum())
    counts = np.zeros(total + 1, dtype=np.int64)
    counts[0] = 1
    for u in units.astype(np.int64):
        counts[u:] = counts[u:] + counts[:total + 1 - u].copy()
    return counts


def wilcoxon(d: np.ndarray) -> Wilcoxon | None:
    """Wilcoxon's signed-rank test, two-sided, as scipy.stats.wilcoxon(d, zero_method="wilcox", correction=False,
    method="auto") computes it; None without a non-zero difference."""
    d = np.asarray(d, dtype=np.float64)
    total = int(d.size)
    nz = d[d != 0]
    zeros = total - int(nz.size)
    n = int(nz.size)
    if n == 0:
        return None
    ranks, sizes = _ranks(np.abs(nz))
    w_plus = float(ranks[nz > 0].sum())
    w_minus = float(ranks[nz < 0].sum())
    ties = bool((sizes > 1).any())
    mean = n * (n + 1) / 4.0
    se = math.sqrt((n * (n + 1) * (2 * n + 1) - float((sizes.astype(np.float64) ** 3 - sizes).sum()) / 2.0) / 24.0)
    if total <= EXACT_MAX and not (ties or zeros):
        method = "exact"
    elif (ties or zeros) and total <= PERMUTATION_MAX:
        method = "permutation"
    else:
        method = "normal"
    z = (w_plus - mean) / se if se > 0 else math.nan
    if method == "normal":
        p = math.erfc(abs(z) / math.sqrt(2.0)) if math.isfinite(z) else 1.0
        low = math.erfc((mean / se) / math.sqrt(2.0)) if se > 0 else 1.0
        return Wilcoxon(n, zeros, w_plus, w_minus, min(w_plus, w_minus), min(1.0, p), method, min(1.0, low), z)
    # the sign patterns of the n non-zero differences (a zero's two signs change nothing: the shares are those of n)
    units = np.rint(ranks * 2).astype(np.int64)    # average ranks are whole or halves
    counts = _sign_counts(units)
    k = int(round(w_plus * 2))
    patterns = float(2 ** n)
    # scipy: 2·min(P(S ≤ W+), P(S ≥ W+)) over the patterns (its exact path rounds a W+ of halves, which it never has)
    below, above = counts[:k + 1].sum(), counts[k:].sum()
    p = min(1.0, 2.0 * min(float(below), float(above)) / patterns)
    low = min(1.0, 2.0 * float(min(counts[0], counts[-1])) / patterns)
    return Wilcoxon(n, zeros, w_plus, w_minus, min(w_plus, w_minus), p, method, low, z)


def median(x: list[float] | np.ndarray) -> float:
    a = np.asarray(x, dtype=np.float64)
    return float(np.median(a)) if a.size else math.nan
