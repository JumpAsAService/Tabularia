"""The exact distributions of the tests on counts and on ranks: the binomial test and
Clopper–Pearson's interval of a proportion, Fisher's exact test of a 2×2 table with its
conditional odds ratio and interval (and of an r × c table), the distributions of
Mann–Whitney's U, of the signed rank and of the two-sample Kolmogorov–Smirnov distance on
small samples. Python's standard library only, on the handful of numbers the engine returned
(the successes and the rows; the cells; a rank sum, a distance as a whole number): never a row
of the data.

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


# ── Fisher's exact test of an r × c table (Freeman–Halton) ──────────────────
class TooMuchWork(Exception):
    """The count of an r × c table would take more steps than allowed: the exact test is
    refused (before it starts, when the table's own path through the network is already too
    long, or as soon as the steps run out)."""


def _count_vectors(total: int, caps: tuple[int, ...] | list[int]) -> int:
    """How many ways there are to fill a column of `total` rows under the rows' remaining
    totals `caps` (what `_column_vectors` goes through), counted without going through them:
    ways[s] = the fillings of the rows seen so far that sum to s."""
    ways = [1] + [0] * total
    for cap in caps:
        prefix = [0] * (total + 2)
        for s in range(total + 1):
            prefix[s + 1] = prefix[s] + ways[s]
        ways = [prefix[s + 1] - prefix[max(0, s - cap)] for s in range(total + 1)]
    return ways[total]


def _column_vectors(total: int, caps: tuple[int, ...]):
    """Every way of filling a column of `total` rows under the rows' remaining totals."""
    if len(caps) == 1:
        if total <= caps[0]:
            yield (total,)
        return
    rest = sum(caps[1:])
    for x in range(max(0, total - rest), min(total, caps[0]) + 1):
        for tail in _column_vectors(total - x, caps[1:]):
            yield (x, *tail)


def _spread(total: int, caps: list[int] | tuple[int, ...], even: bool, lf: list[float]) -> float:
    """−Σ ln x! of one line (a column, or a row) of `total` under `caps` that is the largest
    (`even`: as even as the caps allow, −ln x! being concave: water-filling) or the smallest
    (filled one cap after the other, the largest first). `lf`: ln n! by n."""
    if even:
        xs = [0] * len(caps)
        left = total
        order = sorted(range(len(caps)), key=lambda i: caps[i])
        for pos, i in enumerate(order):  # the smallest caps fill first, the rest share what is left
            xs[i] = min(caps[i], left // (len(order) - pos))
            left -= xs[i]
        for i in sorted(range(len(caps)), key=lambda i: xs[i]):  # what the division left over, one by one
            if left == 0:
                break
            if xs[i] < caps[i]:
                xs[i] += 1
                left -= 1
        return -sum(lf[x] for x in xs)
    out, left = 0.0, total
    for cap in sorted(caps, reverse=True):
        x = min(cap, left)
        out -= lf[x]
        left -= x
    return out


def fisher_rxc(table: list[list[int]], max_steps: int) -> tuple[float, int]:
    """The two-sided p-value of Fisher's exact test of an r × c table (Freeman–Halton, R's
    `fisher.test`): the probability, given the margins, of every table no more likely than the
    one observed (R's slack of 1e-7). The network algorithm (Mehta and Patel, 1983): the
    columns are filled one after the other, the nodes of a stage are the row totals left
    (sorted: the rest does not depend on their order) with the log-weights of the paths that
    reach them; a node whose every completion is no more likely than the table observed adds
    the closed mass of its completions, one whose every completion is more likely adds
    nothing, the others are expanded. The bounds are those of each column alone (valid, if
    not the tightest). Returns (p-value, nodes created).

    What it costs is counted in steps, every one as it is taken: each cell of a way of filling
    a column gone through (r steps a way: it takes some r times as long as the rest), a path
    extended by one, a node remembered, a line bounded. Time and memory follow the steps (the
    nodes, the ways remembered, the bounds are each fewer than them: some 1 µs and at most
    250 bytes a step), and past
    `max_steps` the count stops with TooMuchWork. Before it starts, the steps the table
    observed's own path surely costs (`_observed_path_steps`, counted without going through
    them) are compared with `max_steps`: a table far too large is refused at once."""
    if len(table) > len(table[0]):  # the shorter side as the rows: smaller nodes
        table = [list(col) for col in zip(*table)]
    rows = [sum(r) for r in table]
    # the columns from the largest total, the observed table's columns in that order
    observed_cols = sorted(zip(*table), key=sum, reverse=True)
    cols = [sum(col) for col in observed_cols]
    total = sum(rows)
    lf = [0.0] * (total + 1)
    for n in range(2, total + 1):
        lf[n] = lf[n - 1] + math.log(n)
    observed = -sum(lf[x] for r in table for x in r)
    limit = observed + math.log1p(_RELATIVE_SLACK)
    # the log of the sum of Π 1/x! over every table with these margins: N! / (Π R! Π C!)
    log_all = lf[total] - sum(lf[r] for r in rows) - sum(lf[c] for c in cols)
    tail_cols = [cols[j:] for j in range(len(cols))]
    log_tail = [sum(lf[c] for c in cols[j:]) for j in range(len(cols))]
    bounds: dict[tuple, tuple[float, float]] = {}
    width, steps = len(rows), 0

    def bound(left: tuple[int, ...], j: int) -> tuple[float, float]:
        """The largest and the smallest −Σ ln x! of a completion: each line alone (the columns
        under the rows' totals, the rows under the columns'), the tighter of the two; exact for
        the last column, which the rows' totals fill."""
        nonlocal steps
        key = (left, j)
        got = bounds.get(key)
        if got is None:
            if j == len(cols) - 1:
                exact_ = -sum(lf[r] for r in left)
                got = (exact_, exact_)
                steps += 1
            else:
                rest = tail_cols[j]
                steps += 2 * (len(rest) + len(left))  # a step per line bounded
                if steps > max_steps:
                    raise TooMuchWork(steps)
                high = min(sum(_spread(c, left, True, lf) for c in rest), sum(_spread(r, rest, True, lf) for r in left))
                low = max(sum(_spread(c, left, False, lf) for c in rest), sum(_spread(r, rest, False, lf) for r in left))
                got = (high, low)
            bounds[key] = got
        return got

    # Before the count: the observed table's path is a path of the network, and every node of
    # it that is not closed has every way of filling its next column gone through (r steps
    # each), and each extends at least that path (one more). Those steps are a floor of the
    # count's, found without going through them.
    floor, past, left = 0, 0.0, tuple(rows)
    for j, col in enumerate(observed_cols[:-1]):
        high, _ = bound(tuple(sorted(left)), j)
        if past + high <= limit:  # closed: every completion counts
            break
        floor += (width + 1) * _count_vectors(cols[j], left)
        if floor > max_steps:
            raise TooMuchWork(floor)
        past -= sum(lf[x] for x in col)
        left = tuple(r - x for r, x in zip(left, col))

    p_value = 0.0
    stage: dict[tuple[int, ...], dict[int, list[float]]] = {tuple(sorted(rows)): {0: [0.0, 1.0]}}
    nodes = 0
    for j, c in enumerate(cols):
        following: dict[tuple[int, ...], dict[int, list[float]]] = {}
        for left, pasts in stage.items():
            high, low = bound(left, j)
            mass = lf[sum(left)] - sum(lf[r] for r in left) - log_tail[j]  # ln Σ Π 1/x! of the completions
            vectors = None
            for past, count in pasts.values():
                if past + high <= limit:  # every completion counts
                    p_value += count * math.exp(past + mass - log_all)
                    continue
                if past + low > limit:  # none does
                    continue
                if vectors is None:
                    vectors = []
                    for xs in _column_vectors(c, left):  # a step per cell, counted as it is gone through
                        steps += width
                        if steps > max_steps:
                            raise TooMuchWork(steps)
                        vectors.append((-sum(lf[x] for x in xs), tuple(sorted(r - x for r, x in zip(left, xs)))))
                steps += len(vectors)  # a step per path extended, counted before they are
                if steps > max_steps:
                    raise TooMuchWork(steps)
                for weight, after in vectors:
                    w = past + weight
                    slot = following.setdefault(after, {})
                    key = round(w * 1e9)  # paths of the same weight are one, their counts added
                    if key in slot:
                        slot[key][1] += count
                    else:  # a node remembered: a step more
                        nodes += 1
                        steps += 1
                        if steps > max_steps:
                            raise TooMuchWork(steps)
                        slot[key] = [w, count]
        stage = following
    # the paths that reached the end: each a whole table
    for pasts in stage.values():
        for past, count in pasts.values():
            if past <= limit:
                p_value += count * math.exp(past - log_all)
    return min(1.0, p_value), nodes


# ── ranks: Mann–Whitney's U, the signed rank ────────────────────────────────
# Without ties every arrangement of the ranks is as likely under H0, and a p-value is a count of
# arrangements over their number: whole numbers (Python's), divided once at the end, so that a
# p-value is the double nearest to the exact fraction. The two-sided one is R's: twice the
# smaller tail, at most 1.
def _from_counts(counts: list[int], v: int, alternative: str) -> float:
    total = sum(counts)
    lower = sum(counts[:v + 1])  # P(V ≤ v)
    upper = total - lower + counts[v]  # P(V ≥ v)
    if alternative == "less":
        return lower / total
    if alternative == "greater":
        return upper / total
    return min(1.0, 2 * min(lower, upper) / total)


def mann_whitney_counts(n1: int, n2: int) -> list[int]:
    """How many of the C(n1 + n2, n1) arrangements of two samples give each U, 0 to n1·n2: the
    coefficients of the Gaussian binomial ∏ (1 − q^(n2 + i)) / (1 − q^i), i = 1…n1, each factor
    multiplied then divided on an array of whole numbers (every division is exact)."""
    top = n1 * n2
    poly = [1] + [0] * (top + n1 + 1)
    for i in range(1, n1 + 1):
        s = n2 + i
        for k in range(len(poly) - 1, s - 1, -1):  # × (1 − q^s)
            poly[k] -= poly[k - s]
        for k in range(i, len(poly)):  # ÷ (1 − q^i): b[k] = a[k] + b[k − i]
            poly[k] += poly[k - i]
    return poly[:top + 1]


def mann_whitney_exact(u: int, n1: int, n2: int, alternative: str) -> float:
    """The p-value of U = u (R's W: the first sample's rank sum less n1(n1 + 1)/2), without ties:
    `less` P(U ≤ u), `greater` P(U ≥ u), two-sided min(1, 2·min of the two)."""
    return _from_counts(mann_whitney_counts(n1, n2), int(u), alternative)


def signed_rank_counts(n: int) -> list[int]:
    """How many of the 2^n subsets of {1, …, n} have each sum, 0 to n(n + 1)/2: a 0/1 knapsack on
    an array of whole numbers."""
    top = n * (n + 1) // 2
    counts = [1] + [0] * top
    for k in range(1, n + 1):
        for s in range(top, k - 1, -1):
            counts[s] += counts[s - k]
    return counts


def signed_rank_exact(v: int, n: int, alternative: str) -> float:
    """The p-value of the signed rank V = v (the sum of the ranks of the positive differences),
    n differences, no zero, no tie: as `mann_whitney_exact`."""
    return _from_counts(signed_rank_counts(n), int(v), alternative)


# ── the two-sample Kolmogorov–Smirnov distance ──────────────────────────────
def ks_exact(distance: int, n1: int, n2: int, alternative: str) -> float:
    """P(D ≥ d) for two samples of n1 and n2 values with no tie, `distance` = d·n1·n2 (a whole
    number: the engine returns the distance so). Every order of the pooled values is a path from
    (0, 0) to (n1, n2), a step right for a value of the first sample, up for one of the second;
    at (i, j) the two distribution functions differ by (i·n2 − j·n1)/(n1·n2). The paths that
    never reach the distance — |i·n2 − j·n1| < distance (`two_sided`), i·n2 − j·n1 < distance
    (`greater`: F1 − F2), j·n1 − i·n2 < distance (`less`) — are counted row by row in whole
    numbers; P(D ≥ d) = 1 − their share, as one fraction (R's `psmirnov` counts the same paths
    in doubles)."""
    def inside(i: int, j: int) -> bool:
        gap = i * n2 - j * n1
        if alternative == "greater":
            return gap < distance
        if alternative == "less":
            return -gap < distance
        return abs(gap) < distance

    row = [0] * (n2 + 1)
    for j in range(n2 + 1):
        row[j] = (row[j - 1] if j else 1) if inside(0, j) else 0
    for i in range(1, n1 + 1):
        row[0] = row[0] if inside(i, 0) else 0
        for j in range(1, n2 + 1):
            row[j] = row[j] + row[j - 1] if inside(i, j) else 0
    total = math.comb(n1 + n2, n1)
    return (total - row[n2]) / total


def ks_asymptotic(d: float, n1: int, n2: int, alternative: str) -> float:
    """P(D ≥ d) by Kolmogorov's limit at λ = d·√(n1·n2/(n1 + n2)) (`dist.kolmogorov_sf`), one-sided
    exp(−2λ²) (λ² written as d²·n1·n2/(n1 + n2), as R's `ks.test` does)."""
    if alternative != "two_sided":
        return math.exp(-2.0 * (n1 * n2 / (n1 + n2)) * (d * d)) if d > 0.0 else 1.0
    return dist.kolmogorov_sf(d * math.sqrt(n1 * n2 / (n1 + n2)))
