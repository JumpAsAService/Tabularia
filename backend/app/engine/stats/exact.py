"""The exact distributions of the tests on counts: the binomial test and Clopper–Pearson's
interval of a proportion, Fisher's exact test of a 2×2 table with its conditional odds ratio
and interval. Python's standard library only, on the handful of counts the engine returned
(the successes and the rows; the four cells): never a row of the data.

The rules are R's (`binom.test`, `fisher.test`), so that a reader can check a number there:

- a two-sided p-value is the probability of every outcome no more likely than the one
  observed, «no more likely» with a relative slack of 1e-7 (two outcomes equal on paper may
  differ in their last digits);
- the odds ratio is the conditional maximum-likelihood estimate, the root of «the mean of the
  non-central hypergeometric is the observed count»; its interval is made of the non-central
  tails. R finds those roots to some 1e-4 (`uniroot`'s tolerance); here they are found to the
  last digit of their logarithm, so R's values agree to that 1e-4 only.

How it stays fast up to `EXACT_SUPPORT` outcomes: the probabilities are those of a log-concave
distribution, which lives on a few standard deviations around its mode. Every sum starts at
the mode (or at the outcome that bounds a tail) and walks outward until what is left is below
e^-60 of what it has: a table of a million possible outcomes costs a few thousand terms.
"""
from __future__ import annotations

import math
from typing import NamedTuple

from app.engine.stats import dist

# two outcomes whose probabilities are this close are taken as equally likely (R's relErr)
_RELATIVE_SLACK = 1e-7
# a term this far below the largest (in natural logarithm) no longer changes a sum of doubles
_NEGLIGIBLE = 60.0


def log_choose(n: int, k: int) -> float:
    """ln C(n, k) = −ln(n + 1) − ln B(k + 1, n − k + 1), through `dist.lbeta`, which subtracts no
    two large numbers."""
    return -math.log(n + 1.0) - dist.lbeta(k + 1.0, n - k + 1.0)


# ── the binomial ────────────────────────────────────────────────────────────
def binom_logpmf(k: int, n: int, p: float) -> float:
    """ln P(X = k) for X ~ binomial(n, p), 0 < p < 1."""
    return log_choose(n, k) + k * math.log(p) + (n - k) * math.log1p(-p)


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X ≥ k) = I_p(k, n − k + 1)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return dist.betainc(float(k), float(n - k + 1), p, 1.0 - p)


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X ≤ k) = I_(1 − p)(n − k, k + 1)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return dist.betainc(float(n - k), float(k + 1), 1.0 - p, p)


def _first(lo: int, hi: int, test) -> int:
    """The first integer of [lo, hi] for which `test` holds (it holds from there on), or hi + 1."""
    while lo <= hi:
        mid = (lo + hi) // 2
        if test(mid):
            hi = mid - 1
        else:
            lo = mid + 1
    return lo


def binomial_test(k: int, n: int, p: float, alternative: str) -> float:
    """The p-value of k successes in n against H0: proportion = p (R's `binom.test`). The
    two-sided one adds the other tail's outcomes no more likely than k: as the probabilities
    rise to the mode and fall after it, where that tail starts is found by bisection."""
    if alternative == "less":
        return binom_cdf(k, n, p)
    if alternative == "greater":
        return binom_sf(k, n, p)
    m = n * p
    if k == m:
        return 1.0
    limit = binom_logpmf(k, n, p) + math.log1p(_RELATIVE_SLACK)
    if k < m:
        # the outcomes above the mean no more likely than k: from the first of them to n
        first = _first(math.ceil(m), n, lambda i: binom_logpmf(i, n, p) <= limit)
        return min(1.0, binom_cdf(k, n, p) + binom_sf(first, n, p))
    # below the mean: from 0 to the last outcome no more likely than k
    last = _first(0, math.floor(m), lambda i: binom_logpmf(i, n, p) > limit) - 1
    return min(1.0, binom_cdf(last, n, p) + binom_sf(k, n, p))


def clopper_pearson(k: int, n: int, alpha: float, alternative: str) -> tuple[float, float]:
    """Clopper–Pearson's interval of a proportion, k successes in n, at 1 − alpha: the
    proportions whose binomial tails at k are alpha/2 (one tail alpha for a one-sided one,
    the other side its bound, 0 or 1)."""
    tail = alpha / 2.0 if alternative == "two_sided" else alpha
    low = 0.0 if k == 0 or alternative == "less" else dist.beta_ppf(tail, float(k), float(n - k + 1))
    high = 1.0 if k == n or alternative == "greater" else dist.beta_isf(tail, float(k + 1), float(n - k))
    return low, high


# ── Fisher's exact test ─────────────────────────────────────────────────────
class Fisher(NamedTuple):
    p_value: float
    odds_ratio: float  # the conditional estimate: 0 or +inf at an end of the support
    ci_low: float
    ci_high: float  # +inf when the interval has no upper bound
    support: int


def fisher_support(a: int, b: int, c: int, d: int) -> int:
    """How many values the count of the first cell may take with the table's margins."""
    m, n, k = a + c, b + d, a + b
    return min(k, m) - max(0, k - n) + 1


class _Hypergeometric:
    """The count X of the first cell of a 2×2 table given its margins, under an odds ratio
    e^u: P(X = i) ∝ C(m, i)·C(n, k − i)·e^(u·i), i from `lo` to `hi` (m, n: the columns' totals,
    k: the first row's). The logarithms of the binomial coefficients are remembered."""

    def __init__(self, m: int, n: int, k: int):
        self.m, self.n, self.k = m, n, k
        self.lo, self.hi = max(0, k - n), min(k, m)
        self._log: dict[int, float] = {}

    def log_dc(self, i: int) -> float:
        v = self._log.get(i)
        if v is None:
            v = self._log[i] = log_choose(self.m, i) + log_choose(self.n, self.k - i)
        return v

    def _step(self, i: int, u: float) -> float:
        """ln P(X = i + 1) − ln P(X = i): it decreases with i (the distribution is log-concave)."""
        m, n, k = self.m, self.n, self.k
        return math.log(m - i) + math.log(k - i) - math.log(i + 1.0) - math.log(n - k + i + 1.0) + u

    def mode(self, u: float) -> int:
        return _first(self.lo, self.hi - 1, lambda i: self._step(i, u) < 0.0)

    def window(self, u: float) -> tuple[int, list[float]]:
        """The outcomes where the probability is within e^-60 of the largest: the first of
        them, and their logarithms less the largest's (the mode's)."""
        top = self.mode(u)
        peak = self.log_dc(top) + u * top
        below: list[float] = []
        i = top - 1
        while i >= self.lo:
            v = self.log_dc(i) + u * i - peak
            if v < -_NEGLIGIBLE:
                break
            below.append(v)
            i -= 1
        above: list[float] = []
        i = top + 1
        while i <= self.hi:
            v = self.log_dc(i) + u * i - peak
            if v < -_NEGLIGIBLE:
                break
            above.append(v)
            i += 1
        return top - len(below), below[::-1] + [0.0] + above

    def mean(self, u: float) -> float:
        first, logs = self.window(u)
        weights = [math.exp(v) for v in logs]
        return sum((first + j) * w for j, w in enumerate(weights)) / sum(weights)

    def tail(self, x: int, u: float, upper: bool) -> float:
        """P(X ≥ x) (`upper`) or P(X ≤ x) under the odds ratio e^u; outside the window 0 or 1."""
        first, logs = self.window(u)
        last = first + len(logs) - 1
        if (x > last) if upper else (x < first):
            return 0.0
        if (x <= first) if upper else (x >= last):
            return 1.0
        weights = [math.exp(v) for v in logs]
        j = x - first
        part = sum(weights[j:]) if upper else sum(weights[:j + 1])
        return part / sum(weights)

    def central_sum(self, start: int, step: int, floor: float) -> float:
        """Σ e^(ln P(X = i) − `floor`) at the odds ratio 1, from `start` outward by `step`, until
        what is left no longer counts: the terms of a tail, past its first, only fall."""
        total, i = 0.0, start
        while self.lo <= i <= self.hi:
            term = math.exp(self.log_dc(i) - floor)
            total += term
            if term < total * math.exp(-_NEGLIGIBLE):
                break
            i += step
        return total


def _solve(f, target: float) -> float:
    """The u where the increasing function f reaches `target`: bracketed, then bisected until
    the interval is one double wide (as the distributions' quantiles)."""
    lo, hi = -1.0, 1.0
    while f(lo) > target:
        lo *= 2.0
        if lo < -1e4:
            return -math.inf
    while f(hi) < target:
        hi *= 2.0
        if hi > 1e4:
            return math.inf
    while True:
        mid = 0.5 * (lo + hi)
        if mid <= lo or mid >= hi:
            return mid
        if f(mid) < target:
            lo = mid
        else:
            hi = mid


def fisher_exact(a: int, b: int, c: int, d: int, alternative: str, alpha: float) -> Fisher:
    """Fisher's exact test of the 2×2 table [[a, b], [c, d]] (R's `fisher.test`): its p-value,
    the conditional maximum-likelihood odds ratio and its interval at 1 − alpha."""
    h = _Hypergeometric(a + c, b + d, a + b)
    x, lo, hi = a, h.lo, h.hi
    # the p-value: the distribution at odds ratio 1, normalised over its window
    first, logs = h.window(0.0)
    peak = h.log_dc(first + logs.index(0.0))
    total = sum(math.exp(v) for v in logs)
    if alternative == "less":
        p_value = h.central_sum(x, -1, peak) / total
    elif alternative == "greater":
        p_value = h.central_sum(x, 1, peak) / total
    else:
        top = first + logs.index(0.0)
        limit = h.log_dc(x) + math.log1p(_RELATIVE_SLACK)
        # the outcomes no more likely than x: [lo, left] and [right, hi], each found by bisection
        left = _first(lo, top, lambda i: h.log_dc(i) > limit) - 1
        right = _first(max(top, left + 1), hi, lambda i: h.log_dc(i) <= limit)
        part = (h.central_sum(left, -1, peak) if left >= lo else 0.0) + (h.central_sum(right, 1, peak) if right <= hi else 0.0)
        p_value = part / total
    p_value = min(1.0, p_value)

    def estimate() -> float:
        if x == lo:
            return 0.0
        if x == hi:
            return math.inf
        return math.exp(_solve(h.mean, float(x)))

    def upper_bound(level: float) -> float:  # the odds ratio whose lower tail at x is `level`
        if x == hi:
            return math.inf
        return math.exp(_solve(lambda u: -h.tail(x, u, upper=False), -level))

    def lower_bound(level: float) -> float:  # the odds ratio whose upper tail at x is `level`
        if x == lo:
            return 0.0
        return math.exp(_solve(lambda u: h.tail(x, u, upper=True), level))

    if alternative == "less":
        ci = (0.0, upper_bound(alpha))
    elif alternative == "greater":
        ci = (lower_bound(alpha), math.inf)
    else:
        ci = (lower_bound(alpha / 2.0), upper_bound(alpha / 2.0))
    return Fisher(p_value, estimate(), ci[0], ci[1], hi - lo + 1)
