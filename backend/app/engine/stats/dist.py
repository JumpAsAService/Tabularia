"""The continuous distributions of the hypothesis tests: tails, quantiles, and the special
functions under them. Python's standard library only (`math`), the same code for every
engine: it never sees a row of the data, only the handful of numbers a test is made of.

Accuracy contract (tests/test_stats_dist.py): every p-value above 1e-280 within a relative
1e-12 of its 60-digit value, for degrees of freedom up to 1e10 (measured against 60-digit
arithmetic on 2026-11-19: 2e-13 at worst, up to 1e16). It needs saying because a billion rows
are a billion degrees of freedom, where a textbook incomplete beta is wrong at the eighth
digit: there x = df/(df + t²) is one step from 1, the function changes by `df` times what x
changes by, and x itself is rounded at its sixteenth digit. Hence, below:

- `betainc` takes `y = 1 − x` FROM THE CALLER, who knows it exactly (t²/(df + t²)), and never
  computes it by subtraction: the logarithm of x and the fraction's terms are read from y;
- `lbeta` goes through Stirling's remainder and `log1p` when a parameter is large, so that no
  two large numbers are subtracted.
"""
from __future__ import annotations

import math

_SQRT2 = math.sqrt(2.0)
_HALF_LOG_2PI = 0.5 * math.log(2.0 * math.pi)
# the continued fraction stops when a step changes it by less than this (two steps of a
# double: asking for none would wait for a value that may swing by one for ever)
_EPS = 4.5e-16
_MAX_ITERATIONS = 100_000


# ── normal ──────────────────────────────────────────────────────────────────
def norm_sf(z: float) -> float:
    """P(Z ≥ z)."""
    return 0.5 * math.erfc(z / _SQRT2)


def norm_cdf(z: float) -> float:
    """P(Z ≤ z)."""
    return 0.5 * math.erfc(-z / _SQRT2)


def norm_isf(q: float) -> float:
    """The z with P(Z ≥ z) = q."""
    return _inverse_sf(norm_sf, q)


def norm_ppf(p: float) -> float:
    """The z with P(Z ≤ z) = p."""
    return -norm_isf(p)


# ── beta ────────────────────────────────────────────────────────────────────
def _stirling_remainder(z: float) -> float:
    """Δ(z) = lgamma(z) − [(z − ½)·ln z − z + ½·ln 2π], for z ≥ 10 (its series' eight terms
    are below 1e-17 of it there)."""
    w = 1.0 / (z * z)
    return (1.0 / 12.0 + w * (-1.0 / 360.0 + w * (1.0 / 1260.0 + w * (-1.0 / 1680.0 + w * (
        1.0 / 1188.0 + w * (-691.0 / 360360.0 + w * (1.0 / 156.0 + w * (-3617.0 / 122400.0)))))))) / z


def lbeta(a: float, b: float) -> float:
    """ln B(a, b). The plain sum of three `lgamma` when both are small; otherwise the large
    terms are cancelled on paper first (Stirling), and what is left is added."""
    if a > b:
        a, b = b, a
    if b < 10.0:
        return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    if a < 10.0:
        return math.lgamma(a) - (
            (b - 0.5) * math.log1p(a / b) + a * math.log(a + b) - a
            + _stirling_remainder(a + b) - _stirling_remainder(b)
        )
    return (
        _HALF_LOG_2PI - 0.5 * math.log(b)
        + _stirling_remainder(a) + _stirling_remainder(b) - _stirling_remainder(a + b)
        - (a - 0.5) * math.log1p(b / a) - b * math.log1p(a / b)
    )


def _log_of(x: float, y: float) -> float:
    """ln x, with y = 1 − x: near 1 the logarithm is read from what x lacks."""
    return math.log1p(-y) if x > 0.5 else math.log(x)


def _beta_fraction(a: float, b: float, x: float, y: float) -> float:
    """I_x(a, b) / (x^a·y^b / B(a, b)) by a continued fraction, y = 1 − x: the even part of
    the classic one (DiDonato and Morris, 1992), two of its steps at a time. The classic
    fraction adds 1 to terms that are −1 to their last digits when `a` is large and x near
    1; here that sum is made on paper, and what is left of it is λ = a − (a + b)·x, read
    from whichever of x and y is small."""
    lam = a - (a + b) * x if x <= 0.5 else (a + b) * y - b
    c = 1.0 + lam
    c0 = b / a
    c1 = 1.0 + 1.0 / a
    n = 0.0
    p = 1.0
    s = a + 1.0
    # numerators and denominators of two convergents in a row, rescaled at every step
    an, bn, an1, bn1 = 0.0, 1.0, 1.0, c / c1
    r = c1 / c
    for _ in range(_MAX_ITERATIONS):
        n += 1.0
        t = n / a
        w = n * (b - n) * x
        e = a / s
        alpha = (p * (p + c0) * e * e) * (w * x)
        e = (1.0 + t) / (c1 + t + t)
        beta = n + w / s + e * (c + n * (y + 1.0))
        p = 1.0 + t
        s += 2.0
        an, an1 = an1, alpha * an + beta * an1
        bn, bn1 = bn1, alpha * bn + beta * bn1
        previous, r = r, an1 / bn1
        if abs(r - previous) <= _EPS * r:
            break
        an /= bn1
        bn /= bn1
        an1, bn1 = r, 1.0
    return r


def betainc(a: float, b: float, x: float, y: float) -> float:
    """The regularized incomplete beta I_x(a, b), with `y` = 1 − x given by the caller."""
    if x <= 0.0:
        return 0.0
    if y <= 0.0:
        return 1.0
    front = math.exp(a * _log_of(x, y) + b * _log_of(y, x) - lbeta(a, b))
    # the fraction converges fast on this side of the mean, slowly on the other: there the
    # mirrored form is taken, I_x(a, b) = 1 − I_y(b, a) (the comparison is made on the
    # smaller of x and y)
    direct = y > (b + 1.0) / (a + b + 2.0) if x > 0.5 else x < (a + 1.0) / (a + b + 2.0)
    if direct:
        return front * _beta_fraction(a, b, x, y)
    return 1.0 - front * _beta_fraction(b, a, y, x)


# ── Student's t ─────────────────────────────────────────────────────────────
def t_sf(t: float, df: float) -> float:
    """P(T ≥ t) with `df` degrees of freedom (any positive number: Welch's are not whole)."""
    if math.isnan(t) or math.isnan(df):
        return math.nan
    if t == 0.0:
        return 0.5
    t2 = t * t
    if math.isinf(t2):
        tail = 0.0
    else:
        total = df + t2
        tail = 0.5 * betainc(df / 2.0, 0.5, df / total, t2 / total)
    return tail if t > 0.0 else 1.0 - tail


def t_cdf(t: float, df: float) -> float:
    """P(T ≤ t)."""
    return t_sf(-t, df)


def t_isf(q: float, df: float) -> float:
    """The t with P(T ≥ t) = q: the quantile of 1 − q, asked by its tail so that a small
    alpha loses no digit in `1 − alpha`."""
    return _inverse_sf(lambda t: t_sf(t, df), q)


def t_ppf(p: float, df: float) -> float:
    """The quantile: the t with P(T ≤ t) = p."""
    return -t_isf(p, df)


# ── quantiles ───────────────────────────────────────────────────────────────
def _inverse_sf(sf, q: float) -> float:
    """The x with sf(x) = q, for a distribution symmetric about 0: by bisection on `sf`,
    until the interval is one double wide."""
    if math.isnan(q) or q < 0.0 or q > 1.0:
        return math.nan
    if q == 0.0:
        return math.inf
    if q == 1.0:
        return -math.inf
    if q == 0.5:
        return 0.0
    if q > 0.5:
        return -_inverse_sf(sf, 1.0 - q)
    lo, hi = 0.0, 1.0
    while sf(hi) > q:
        lo, hi = hi, hi * 2.0
        if math.isinf(hi):
            return math.inf
    while True:
        mid = 0.5 * (lo + hi)
        if mid <= lo or mid >= hi:
            return hi
        if sf(mid) > q:
            lo = mid
        else:
            hi = mid
