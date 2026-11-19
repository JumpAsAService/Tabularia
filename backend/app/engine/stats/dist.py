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
from functools import lru_cache

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


def _log_front(a: float, b: float, x: float, y: float) -> float:
    """ln of x^a·y^b / B(a, b), the factor in front of the fraction. When both parameters are
    large its three terms are each some a·ln 2 and cancel to a small number, which would keep
    the rounding of the large ones (1e-10 relative at a = b = 5e5, a binomial of a million rows
    near its mean): there it is written with Stirling's remainders and ln(1 + u) − u, every term
    small, from λ = a − (a + b)·x read from whichever of x and y is small (as the fraction does),
    while |λ| is below half of each parameter:
    ½·ln(a·b / (2π·(a + b))) − [Δ(a) + Δ(b) − Δ(a + b)] + a·φ(−λ/a) + b·φ(λ/b), φ(u) = ln(1 + u) − u."""
    lam = a - (a + b) * x if x <= 0.5 else (a + b) * y - b
    # near the mean only: far from it the result is itself large, and 1 + λ/a would be read from
    # the rounding of a difference near −1
    if a >= 10.0 and b >= 10.0 and abs(lam) <= 0.5 * min(a, b):
        return (0.5 * (math.log(a) + math.log(b) - math.log(a + b)) - _HALF_LOG_2PI
                - (_stirling_remainder(a) + _stirling_remainder(b) - _stirling_remainder(a + b))
                + a * _log1pmx(-lam / a) + b * _log1pmx(lam / b))
    return a * _log_of(x, y) + b * _log_of(y, x) - lbeta(a, b)


def betainc(a: float, b: float, x: float, y: float) -> float:
    """The regularized incomplete beta I_x(a, b), with `y` = 1 − x given by the caller."""
    if x <= 0.0:
        return 0.0
    if y <= 0.0:
        return 1.0
    front = math.exp(_log_front(a, b, x, y))
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


def t_pdf(t: float, df: float) -> float:
    """The density of T at t, for the curve a report draws:
    (1 + t²/df)^(−(df + 1)/2) / (√df · B(½, df/2)). The constant goes through `lbeta`, not
    through a difference of two `lgamma`, which loses a digit for every digit of `df`."""
    if math.isnan(t) or math.isnan(df):
        return math.nan
    if math.isinf(t):
        return 0.0
    return math.exp(-0.5 * math.log(df) - lbeta(0.5, df / 2.0) - 0.5 * (df + 1.0) * math.log1p(t / df * t))


def norm_pdf(z: float) -> float:
    """The density of the standard normal at z."""
    return math.exp(-0.5 * z * z - _HALF_LOG_2PI)


# ── F ───────────────────────────────────────────────────────────────────────
def f_sf(f: float, d1: float, d2: float) -> float:
    """P(F ≥ f) with `d1` and `d2` degrees of freedom: I_x(d2/2, d1/2) at x = d2/(d2 + d1·f),
    its complement y = d1·f/(d2 + d1·f) written as such, never as 1 − x (with a billion rows
    d2 is a billion, and x is one step from 1)."""
    if math.isnan(f) or math.isnan(d1) or math.isnan(d2):
        return math.nan
    if f <= 0.0:
        return 1.0
    if math.isinf(f):
        return 0.0
    total = d2 + d1 * f
    return betainc(d2 / 2.0, d1 / 2.0, d2 / total, d1 * f / total)


def f_cdf(f: float, d1: float, d2: float) -> float:
    """P(F ≤ f): I_y(d1/2, d2/2), the same two numbers the other way round."""
    if math.isnan(f) or math.isnan(d1) or math.isnan(d2):
        return math.nan
    if f <= 0.0:
        return 0.0
    if math.isinf(f):
        return 1.0
    total = d2 + d1 * f
    return betainc(d1 / 2.0, d2 / 2.0, d1 * f / total, d2 / total)


def f_isf(q: float, d1: float, d2: float) -> float:
    """The f with P(F ≥ f) = q: the upper quantile, asked by its tail."""
    return _inverse_positive(lambda f: f_sf(f, d1, d2), q)


def f_ppf(p: float, d1: float, d2: float) -> float:
    """The f with P(F ≤ f) = p, the lower quantile, as 1 / the upper quantile of F(d2, d1)
    (1/F has that distribution): a small p loses no digit in 1 − p."""
    upper = f_isf(p, d2, d1)
    return math.nan if math.isnan(upper) else 0.0 if math.isinf(upper) else math.inf if upper == 0.0 else 1.0 / upper


def f_pdf(f: float, d1: float, d2: float) -> float:
    """The density of F(d1, d2) at f, for the curve a report draws (+∞ at 0 when d1 < 2)."""
    if math.isnan(f) or math.isnan(d1) or math.isnan(d2):
        return math.nan
    if f < 0.0 or math.isinf(f):
        return 0.0
    if f == 0.0:
        return math.inf if d1 < 2.0 else (1.0 if d1 == 2.0 else 0.0)
    r = d1 * f / d2
    # ln of (d1·f)^(d1/2)·d2^(d2/2) / (d1·f + d2)^((d1 + d2)/2) / (f·B(d1/2, d2/2)), with no large
    # logarithms subtracted: (d1/2)·ln(r/(1 + r)) − (d2/2)·ln(1 + r)
    return math.exp(0.5 * d1 * (math.log(r) - math.log1p(r)) - 0.5 * d2 * math.log1p(r) - math.log(f) - lbeta(0.5 * d1, 0.5 * d2))


# ── the incomplete gamma, chi-square ────────────────────────────────────────
def _log1pmx(u: float) -> float:
    """ln(1 + u) − u, without the cancellation of the two near 0: with v = u/(2 + u),
    ln(1 + u) = 2·atanh(v) = 2·(v + v³/3 + v⁵/5 + …) and 2v − u = −u²/(2 + u), so that
    ln(1 + u) − u = −u²/(2 + u) + 2·(v³/3 + v⁵/5 + …), every term of it small and exact."""
    if abs(u) > 0.5:
        return math.log1p(u) - u
    v = u / (2.0 + u)
    v2 = v * v
    power, total = v * v2, 0.0
    for k in range(3, 80, 2):
        step = power / k
        total += step
        if abs(step) <= 1e-17 * abs(total):
            break
        power *= v2
    return 2.0 * total - u * u / (2.0 + u)


def _gamma_front(a: float, x: float) -> float:
    """ln of x^a·e^(−x) / Γ(a), the factor in front of both forms of the incomplete gamma.
    For a large `a` the large terms cancel on paper (Stirling): a·(ln(1 + u) − u) with
    u = (x − a)/a, ½·ln(a/2π) and the remainder Δ(a), where a plain sum of a·ln x, x and
    lgamma(a) would lose a digit for every digit of a."""
    if a < 10.0:
        return a * math.log(x) - x - math.lgamma(a)
    return a * _log1pmx((x - a) / a) + 0.5 * math.log(a / (2.0 * math.pi)) - _stirling_remainder(a)


def _gamma_series(a: float, x: float) -> float:
    """P(a, x) by its series, for x < a + 1: e^front/a · Σ x^n / ((a + 1)…(a + n))."""
    term = total = 1.0 / a
    ap = a
    # near x = a the terms fall as e^(−n²/2a): some 9·√a of them count
    for _ in range(_MAX_ITERATIONS + int(20.0 * math.sqrt(a))):
        ap += 1.0
        term *= x / ap
        total += term
        if term <= total * 1e-17:
            break
    return total * math.exp(_gamma_front(a, x))


def _gamma_fraction(a: float, x: float) -> float:
    """Q(a, x) by its continued fraction (Legendre's, evaluated by Lentz's method), for
    x ≥ a + 1."""
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, _MAX_ITERATIONS + int(20.0 * math.sqrt(a))):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) <= 3e-16:
            break
    return h * math.exp(_gamma_front(a, x))


def gammainc_p(a: float, x: float) -> float:
    """The regularized lower incomplete gamma P(a, x)."""
    if math.isnan(a) or math.isnan(x):
        return math.nan
    if x <= 0.0:
        return 0.0
    if math.isinf(x):
        return 1.0
    if x < a + 1.0:
        return _gamma_series(a, x)
    return 1.0 - _gamma_fraction(a, x)


def gammainc_q(a: float, x: float) -> float:
    """The regularized upper incomplete gamma Q(a, x) = 1 − P(a, x): the series below
    x = a + 1, the continued fraction above (each where it converges fast, and the one that
    is subtracted from 1 there is never the small one)."""
    if math.isnan(a) or math.isnan(x):
        return math.nan
    if x <= 0.0:
        return 1.0
    if math.isinf(x):
        return 0.0
    if x < a + 1.0:
        return 1.0 - _gamma_series(a, x)
    return _gamma_fraction(a, x)


def chi2_sf(x: float, df: float) -> float:
    """P(X ≥ x) for a chi-square with `df` degrees of freedom."""
    return gammainc_q(df / 2.0, x / 2.0)


def chi2_cdf(x: float, df: float) -> float:
    """P(X ≤ x)."""
    return gammainc_p(df / 2.0, x / 2.0)


def chi2_isf(q: float, df: float) -> float:
    """The x with P(X ≥ x) = q, asked by its tail."""
    return _inverse_positive(lambda x: chi2_sf(x, df), q)


def chi2_pdf(x: float, df: float) -> float:
    """The density of a chi-square with `df` degrees of freedom at x (+∞ at 0 when df < 2)."""
    if math.isnan(x) or math.isnan(df):
        return math.nan
    if x < 0.0 or math.isinf(x):
        return 0.0
    if x == 0.0:
        return math.inf if df < 2.0 else (0.5 if df == 2.0 else 0.0)
    a = 0.5 * df
    # (x/2)^(a − 1)·e^(−x/2) / (2·Γ(a)): the front factor of the incomplete gamma, over x
    return math.exp(_gamma_front(a, 0.5 * x)) / x


# ── Kolmogorov's limit (the two-sample Kolmogorov–Smirnov test on large samples) ──
# λ = D·√(n₁·n₂/(n₁ + n₂)). Two-sided: P(K ≥ λ) = 2·∑ (−1)^(k−1)·exp(−2k²λ²), a series whose terms
# fall fast for λ ≥ 1; below 1 it is read from the other form of the same function (Jacobi's
# theta), P(K < λ) = √(2π)/λ·∑ exp(−(2k − 1)²π²/(8λ²)), whose terms fall fast there. Each is
# summed to the end (R's `ks.test` stops the first series early, at 1e-6: its asymptotic
# p-values differ from these in the sixth digit). One-sided: P(K⁺ ≥ λ) = exp(−2λ²).
def _kolmogorov_theta(lam: float) -> tuple[float, float]:
    """(P(K < λ), its derivative) by the theta form, for 0 < λ < 1."""
    total = slope = 0.0
    k = 1
    while True:
        a = (2 * k - 1) ** 2 * math.pi ** 2 / 8.0
        term = math.exp(-a / (lam * lam))
        total += term
        slope += term * (2.0 * a / lam ** 3 - 1.0 / lam)
        if term < 1e-17 * total or k > 1000:
            break
        k += 1
    c = math.sqrt(2.0 * math.pi)
    return c / lam * total, c / lam * slope


def kolmogorov_sf(lam: float, two_sided: bool = True) -> float:
    """P(K ≥ λ) of Kolmogorov's limit (two-sided), or of Smirnov's one-sided one, exp(−2λ²)."""
    if math.isnan(lam):
        return math.nan
    if lam <= 0.0:
        return 1.0
    if not two_sided:
        return math.exp(-2.0 * lam * lam)
    if lam < 1.0:
        return min(1.0, max(0.0, 1.0 - _kolmogorov_theta(lam)[0]))
    total, k = 0.0, 1
    while True:
        term = math.exp(-2.0 * k * k * lam * lam)
        total += term if k % 2 else -term
        if term < 1e-17 * abs(total) or k > 1000:
            break
        k += 1
    return min(1.0, 2.0 * total)


def kolmogorov_pdf(lam: float, two_sided: bool = True) -> float:
    """The density of Kolmogorov's limit (or of Smirnov's one-sided, 4λ·exp(−2λ²)) at λ."""
    if math.isnan(lam):
        return math.nan
    if lam <= 0.0 or math.isinf(lam):
        return 0.0
    if not two_sided:
        return 4.0 * lam * math.exp(-2.0 * lam * lam)
    if lam < 1.0:
        return _kolmogorov_theta(lam)[1]
    total, k = 0.0, 1
    while True:
        term = k * k * math.exp(-2.0 * k * k * lam * lam)
        total += term if k % 2 else -term
        if term < 1e-17 * abs(total) or k > 1000:
            break
        k += 1
    return 8.0 * lam * total


def kolmogorov_isf(q: float, two_sided: bool = True) -> float:
    """The λ with P(K ≥ λ) = q, asked by its tail."""
    if not two_sided and 0.0 < q <= 1.0:
        return math.sqrt(-0.5 * math.log(q))
    return _inverse_positive(lambda x: kolmogorov_sf(x, two_sided), q)


# ── the studentized range (Tukey's HSD, Games–Howell) ───────────────────────
# Q = (range of k standard normals) / S, S = √(χ²_df / df) independent of them. No engine has
# it: two nested integrals, with the standard library. Only the upper tail is computed,
# P(Q > q) — a p-value and the quantile 1 − α both read it — so that a small p-value is a
# sum of small positive numbers, never 1 less a number near 1 (which would leave the
# rounding of the 1 as its only digits, and two engines' p-values apart by orders of
# magnitude).
#
#   P(Q > q) = ∫ g(s) · T_k(q·s) ds,   T_k(w) = P(range > w)
#            = k ∫ φ(u) · [Q(u)^(k−1) − (Q(u) − Q(u + w))^(k−1)] du     (Q the normal's tail)
#
# with the bracket written b^(k−1)·(1 − (1 − r)^(k−1)), b = Q(u), r = Q(u + w)/b: no
# subtraction of two near numbers. T_k is tabled once per k, as ln T_k at w = j/32 for
# 0 ≤ w ≤ 14 (Gauss–Legendre, 8 nodes on panels of width ½ over [−8.5 − w/2, 8.5], where
# the integrand lives), and read by six-point Lagrange interpolation; beyond 14 it falls as
# the tail of the difference of two normals, Q(w/√2). The outer integral is taken in
# t = √s (no singular slope at 0 for a small df) by 16-node Gauss–Legendre panels over
# [max(0, 1 − 9/√df), 1 + 9/√df] (df ≥ 20) or [0, 1 + 12/√df], cut where q·s passes 40,
# S's density in logs, with the large terms cancelled on paper for df ≥ 20.
_SR_STEP = 32  # table points per unit of w
_SR_MAX = 14.0
_SR_POINTS = int(_SR_MAX * _SR_STEP) + 1


def _gauss_legendre(n: int) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """The nodes and weights of n-point Gauss–Legendre on [−1, 1] (Newton on P_n)."""
    nodes, weights = [], []
    for i in range(1, n + 1):
        x = math.cos(math.pi * (i - 0.25) / (n + 0.5))
        for _ in range(100):
            p0, p1 = 1.0, x
            for m in range(2, n + 1):
                p0, p1 = p1, ((2 * m - 1) * x * p1 - (m - 1) * p0) / m
            dp = n * (x * p1 - p0) / (x * x - 1.0)
            step = p1 / dp
            x -= step
            if abs(step) < 1e-16:
                break
        p0, p1 = 1.0, x
        for m in range(2, n + 1):
            p0, p1 = p1, ((2 * m - 1) * x * p1 - (m - 1) * p0) / m
        dp = n * (x * p1 - p0) / (x * x - 1.0)
        nodes.append(x)
        weights.append(2.0 / ((1.0 - x * x) * dp * dp))
    return tuple(nodes), tuple(weights)


_GL8 = _gauss_legendre(8)
_GL16 = _gauss_legendre(16)


@lru_cache(maxsize=64)
def _range_tail_table(k: int) -> tuple[float, ...]:
    """ln T_k(w) at w = j/32, j = 0 … 448: the tail of the range of k standard normals."""
    nodes, weights = _GL8
    out = []
    for j in range(_SR_POINTS):
        w = j / _SR_STEP
        if j == 0:
            out.append(0.0)
            continue
        lower = -8.5 - 0.5 * math.ceil(w)
        total = 0.0
        panels = int(round((8.5 - lower) / 0.5))
        for p in range(panels):
            mid = lower + 0.5 * p + 0.25
            for x, wt in zip(nodes, weights):
                u = mid + 0.25 * x
                b = norm_sf(u)
                r = norm_sf(u + w) / b
                inner = -math.expm1((k - 1) * math.log1p(-r)) if r < 1.0 else 1.0
                total += wt * math.exp(-0.5 * u * u - _HALF_LOG_2PI + (k - 1) * math.log(b)) * inner
        out.append(math.log(k * 0.25 * total))
    return tuple(out)


def _log_norm_sf(z: float) -> float:
    """ln Q(z), also where Q(z) is below the smallest double."""
    if z < 35.0:
        return math.log(norm_sf(z))
    zz = z * z
    return -0.5 * zz - math.log(z) - _HALF_LOG_2PI + math.log1p(-1.0 / zz + 3.0 / (zz * zz))


_LAGRANGE_DENOMINATORS = (-120.0, 24.0, -12.0, 12.0, -24.0, 120.0)


def _range_log_tail(k: int, w: float) -> float:
    """ln T_k(w), from the table."""
    if w <= 0.0:
        return 0.0
    table = _range_tail_table(k)
    if w >= _SR_MAX:
        return table[-1] + _log_norm_sf(w / _SQRT2) - _log_norm_sf(_SR_MAX / _SQRT2)
    x = w * _SR_STEP
    whole = int(x)
    if x == whole:
        return table[whole]
    j = min(max(whole - 2, 0), _SR_POINTS - 6)
    t = x - j
    d = (t, t - 1.0, t - 2.0, t - 3.0, t - 4.0, t - 5.0)
    product = d[0] * d[1] * d[2] * d[3] * d[4] * d[5]
    total = 0.0
    for i in range(6):
        total += table[j + i] / (d[i] * _LAGRANGE_DENOMINATORS[i])
    return min(0.0, total * product)


def _log_scale_density(s: float, df: float) -> float:
    """ln g(s): the density of S = √(χ²_df / df)."""
    if df >= 20.0:
        u = s - 1.0
        return (math.log(2.0) - math.log(s) + 0.5 * math.log(df / (4.0 * math.pi)) - _stirling_remainder(0.5 * df)
                + df * (_log1pmx(u) - 0.5 * u * u))
    return (math.log(2.0) + 0.5 * df * math.log(0.5 * df) - math.lgamma(0.5 * df) + (df - 1.0) * math.log(s)
            - 0.5 * df * s * s)


def srange_sf(q: float, k: int, df: float) -> float:
    """P(Q > q) for the studentized range of k means with `df` degrees of freedom."""
    if math.isnan(q) or math.isnan(df) or k < 2:
        return math.nan
    if q <= 0.0:
        return 1.0
    if math.isinf(q):
        return 0.0
    if df >= 20.0:
        lo, hi = max(0.0, 1.0 - 9.0 / math.sqrt(df)), 1.0 + 9.0 / math.sqrt(df)
    else:
        lo, hi = 0.0, 1.0 + 12.0 / math.sqrt(df)
    # beyond q·s = 40 the range's tail is below e^(−400): a p-value of that size is no number
    hi = min(hi, 40.0 / q)
    if hi <= lo:
        return 0.0
    tlo, thi = math.sqrt(lo), math.sqrt(hi)
    # panels enough for the range's tail to change little over one (q·s) and for S's density
    # to be three of its standard deviations wide at most (in t, 1/(2·√(2·df)))
    panels = min(96, max(4, math.ceil(q * (hi - lo)), math.ceil((thi - tlo) * 2.0 * math.sqrt(2.0 * df) / 3.0)))
    width = (thi - tlo) / panels
    nodes, weights = _GL16
    total = 0.0
    for p in range(panels):
        mid = tlo + (p + 0.5) * width
        for x, wt in zip(nodes, weights):
            t = mid + 0.5 * width * x
            if t <= 0.0:
                continue
            s = t * t
            total += wt * 2.0 * t * math.exp(_log_scale_density(s, df) + _range_log_tail(k, q * s))
    return min(1.0, max(0.0, 0.5 * width * total))


def srange_cdf(q: float, k: int, df: float) -> float:
    """P(Q ≤ q) (= 1 − the tail: to an absolute 2e-10)."""
    return 1.0 - srange_sf(q, k, df)


def srange_isf(alpha: float, k: int, df: float) -> float:
    """The q with P(Q > q) = alpha: Tukey's and Games–Howell's critical value for a level
    1 − alpha. Bracketed, then regula falsi (the Illinois variant) on ln P(Q > q), to a
    relative 1e-12 of q."""
    if math.isnan(alpha) or not 0.0 < alpha < 1.0:
        return math.nan if math.isnan(alpha) or alpha < 0.0 or alpha > 1.0 else (math.inf if alpha == 0.0 else 0.0)
    target = math.log(alpha)

    def f(q: float) -> float:
        tail = srange_sf(q, k, df)
        return (math.log(tail) if tail > 0.0 else -1e300) - target

    lo, flo = 0.0, -target
    hi = 4.0
    fhi = f(hi)
    while fhi > 0.0:
        lo, flo = hi, fhi
        hi *= 2.0
        fhi = f(hi)
        if hi > 1e6:
            return math.inf
    side = 0
    for _ in range(200):
        q = (lo * fhi - hi * flo) / (fhi - flo)
        if not lo < q < hi:
            q = 0.5 * (lo + hi)
        fq = f(q)
        if fq == 0.0 or hi - lo <= 1e-12 * hi:
            return q
        if fq > 0.0:
            lo, flo = q, fq
            if side == 1:
                fhi *= 0.5
            side = 1
        else:
            hi, fhi = q, fq
            if side == -1:
                flo *= 0.5
            side = -1
        if hi - lo <= 1e-12 * hi:
            return 0.5 * (lo + hi)
    return 0.5 * (lo + hi)


def srange_ppf(p: float, k: int, df: float) -> float:
    """The q with P(Q ≤ q) = p, asked by its tail."""
    return srange_isf(1.0 - p, k, df)


# ── the beta's quantiles (Clopper–Pearson's interval) ───────────────────────
def beta_ppf(q: float, a: float, b: float) -> float:
    """The x with I_x(a, b) = q: by bisection on x, until the interval is one double wide. Its
    complement 1 − x is exact for every x the bisection tries above ½ (both are doubles), and
    a small x is found to its last digit: the bisection halves its interval down to it."""
    if math.isnan(q) or q < 0.0 or q > 1.0:
        return math.nan
    if q == 0.0:
        return 0.0
    if q == 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    while True:
        mid = 0.5 * (lo + hi)
        if mid <= lo or mid >= hi:
            return hi if betainc(a, b, hi, 1.0 - hi) - q < q - betainc(a, b, lo, 1.0 - lo) else lo
        if betainc(a, b, mid, 1.0 - mid) < q:
            lo = mid
        else:
            hi = mid


def beta_isf(q: float, a: float, b: float) -> float:
    """The x with 1 − I_x(a, b) = q, asked by its tail: 1 − the y with I_y(b, a) = q (1 − I_x(a, b)
    is I_(1 − x)(b, a)), so that a small q loses no digit in 1 − q."""
    y = beta_ppf(q, b, a)
    return math.nan if math.isnan(y) else 1.0 - y


# ── quantiles ───────────────────────────────────────────────────────────────
def _inverse_positive(sf, q: float) -> float:
    """The x ≥ 0 with sf(x) = q, for a decreasing tail from sf(0) = 1: by bisection, until
    the interval is one double wide (as `_inverse_sf`)."""
    if math.isnan(q) or q < 0.0 or q > 1.0:
        return math.nan
    if q == 0.0:
        return math.inf
    if q == 1.0:
        return 0.0
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
