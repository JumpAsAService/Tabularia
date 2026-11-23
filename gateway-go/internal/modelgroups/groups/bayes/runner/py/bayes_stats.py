"""The statistics of the self-test, in numpy alone (the image holds no SciPy): the effective sample size of a quantity's draws and
the Monte Carlo standard errors of its mean and standard deviation (check 4), and the one-sample Kolmogorov–Smirnov test against
the uniform distribution with the exact distribution of its statistic (check 8).

The effective sample size is Stan's (the multi-chain autocorrelation of Vehtari, Gelman, Simpson, Carpenter and Bürkner 2021,
without the rank normalization, truncated by Geyer's initial monotone sequence). The exact distribution of the two-sided
statistic D_n is the algorithm of Marsaglia, Tsang and Wang (2003, «Evaluating Kolmogorov's distribution», Journal of
Statistical Software 8(18)), without its asymptotic shortcut; the suite compares it with SciPy's `kstwo` outside the image
(reference/test_bayes_runner.py).
"""
from __future__ import annotations

import math

import numpy as np


def _autocovariance(x: np.ndarray) -> np.ndarray:
    n = len(x)
    y = x - x.mean()
    f = np.fft.rfft(y, 2 * n)
    return np.fft.irfft(f * np.conj(f))[:n] / n


def ess(chains: np.ndarray) -> float:
    """The effective sample size of draws shaped (chains, draws)."""
    m, n = chains.shape
    acov = np.array([_autocovariance(c) for c in chains])
    means = chains.mean(axis=1)
    w = float((acov[:, 0] * n / (n - 1)).mean())
    var_plus = w * (n - 1) / n + (float(means.var(ddof=1)) if m > 1 else 0.0)
    if var_plus <= 0:
        return float(m * n)
    rho = 1.0 - (w - acov.mean(axis=0)) / var_plus
    rho[0] = 1.0
    total, previous, t = 0.0, math.inf, 0
    while t + 1 < n:
        pair = float(rho[t] + rho[t + 1])
        if pair < 0:
            break
        pair = min(pair, previous)
        total += pair
        previous = pair
        t += 2
    tau = max(-1.0 + 2.0 * total, 1.0 / math.log10(m * n))
    return m * n / tau


def mean_and_sd(chains: np.ndarray) -> dict[str, float]:
    """The posterior mean and standard deviation of draws shaped (chains, draws), with their Monte Carlo standard errors: the mean's
    sd / sqrt(ESS); the sd's from the second central moment's (its own ESS), by the delta method."""
    x = chains.ravel()
    mean = float(x.mean())
    sd = float(x.std(ddof=1))
    second = (chains - mean) ** 2
    mcse_mean = sd / math.sqrt(ess(chains))
    mcse_var = float(second.ravel().std(ddof=1)) / math.sqrt(ess(second))
    return {"mean": mean, "sd": sd, "mcse_mean": mcse_mean, "mcse_sd": mcse_var / (2.0 * sd), "ess": ess(chains)}


def ks_statistic(u: np.ndarray) -> float:
    """D_n = sup |F_n(u) - u| of values in [0, 1] against the uniform distribution."""
    u = np.sort(np.asarray(u, dtype=float))
    n = len(u)
    i = np.arange(1, n + 1)
    return float(max((i / n - u).max(), (u - (i - 1) / n).max()))


def _power(a: np.ndarray, n: int) -> tuple[np.ndarray, int]:
    """a ** n as (matrix, decimal exponent), rescaled by 1e-140 whenever its middle element passes 1e140 (Marsaglia et al.)."""
    if n == 1:
        return a.copy(), 0
    v, ev = _power(a, n // 2)
    b = v @ v
    eb = 2 * ev
    if n % 2 == 0:
        v, ev = b, eb
    else:
        v, ev = a @ b, eb
    m = a.shape[0]
    if v[m // 2, m // 2] > 1e140:
        v = v * 1e-140
        ev += 140
    return v, ev


def ks_cdf(n: int, d: float) -> float:
    """P(D_n < d), exactly (Marsaglia, Tsang and Wang 2003)."""
    if d <= 0:
        return 0.0
    if d >= 1:
        return 1.0
    k = int(n * d) + 1
    m = 2 * k - 1
    h = k - n * d
    i = np.arange(m)[:, None]
    j = np.arange(m)[None, :]
    H = np.where(i - j + 1 >= 0, 1.0, 0.0)
    H[:, 0] -= h ** (np.arange(m) + 1)
    H[m - 1, :] -= h ** (m - np.arange(m))
    if 2 * h - 1 > 0:
        H[m - 1, 0] += (2 * h - 1) ** m
    # each element below the diagonal and on the one above it divided by (i - j + 1)!, as a factor exp(-log((i - j + 1)!)),
    # which underflows to 0 where the factorial passes a double, as the original's divisions one by one do
    gap = (i - j + 1).astype(float)
    below = gap > 0
    H[below] *= np.exp(-np.vectorize(math.lgamma)(gap[below] + 1.0))
    q, eq = _power(H, n)
    s = float(q[k - 1, k - 1])
    for t in range(1, n + 1):
        s = s * t / n
        if s < 1e-140:
            s *= 1e140
            eq -= 140
    return s * 10.0 ** eq


def ks_uniform(u: np.ndarray) -> tuple[float, float]:
    """The statistic and the p-value of the two-sided one-sample test of u against the uniform distribution on [0, 1]."""
    d = ks_statistic(u)
    return d, max(0.0, 1.0 - ks_cdf(len(u), d))
