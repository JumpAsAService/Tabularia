"""The per-quantity measures of a sampling run (docs/specs/bayes-diagnostics.md, «The measures», lot X1), vectorized over a
sub-block of quantities: the posterior summary (mean, sd, the type 7 quantiles 5, 25, 50, 75, 95 %), the rank-normalized
split R-hat and the bulk and tail effective sample sizes of Vehtari, Gelman, Simpson, Carpenter and Bürkner (2021, «Rank-
normalization, folding, and localization: an improved R-hat for assessing convergence of MCMC», Bayesian Analysis 16(2)),
and the Monte Carlo standard errors of the mean and of the sd. Measures only: no threshold, no verdict.

The definitions are those of the R package posterior 1.8.0 (the spec's decision), transcribed with their source lines in
reference/diagnostics/definitions.md; test_diag.py compares every case of reference/diagnostics/witness.json. Where the
paper leaves a detail open, posterior's choice is this module's:

- the split: each chain's first and last ⌊N/2⌋ draws, the middle draw of an odd N left out (`.split_chains`);
- the rank normalization: Φ⁻¹((r − 3/8) / (S′ + 1/4)), r the average rank among the S′ split draws (`z_scale`), Φ⁻¹ by
  Wichura's AS241 (R's `qnorm`, Python's `NormalDist.inv_cdf`; the image holds no SciPy);
- the folded draws: |θ − median(θ)|, the median of all the draws, before the split (`fold_draws`);
- the autocovariance: by FFT, rescaled so that lag 0 is the variance with divisor N computed directly (`autocovariance`);
- τ̂: Geyer's initial positive then monotone sequence with the improved estimate (the last even term), τ̂ = 2 when no pair
  after the first is summed (R's `1:0`), floored at 1/log₁₀(S′) (`.ess`);
- the tail: min(ESS of I(θ ≤ q₅), ESS of I(θ ≥ q₉₅)), the quantiles of all the draws (`.ess_quantile`);
- `mcse_sd`: √(mean((s − mean s)²) / ESS(s) / mean(s) / 4), s = (θ − mean θ)² (`mcse_sd`, Kenney and Keeping 1951);
- NULL (NaN here) where posterior gives NA: a constant quantity (`is_constant`: a range at most 2.2e-16 of its largest
  absolute value), a constant indicator or square; and, the contract's rule, for every measure but the summary when a split
  chain has fewer than 3 draws; and never an infinite number.

In memory a NULL is NaN: the inputs are finite (the follower refused `not_finite`; `measures` refuses them too), so a NaN
out of here means NULL and nothing else. The arrays are shaped (quantities, chains, draws); no Python loop runs over the
draws or the quantities.
"""
from __future__ import annotations

import math

import numpy as np

PROBS = (0.05, 0.25, 0.5, 0.75, 0.95)
QUANTILE_NAMES = ("q5", "q25", "q50", "q75", "q95")
NUMBERS = ("mean", "sd", *QUANTILE_NAMES, "mcse_mean", "mcse_sd", "rhat", "ess_bulk", "ess_tail")
# the working memory of `measures`, in bytes a value of the sub-block: about 99 measured by tracemalloc on 4 x 1,000 and
# 4 x 10,000 draws (the spec's prototype: 104); test_diag checks the peak stays below it. The second pass sizes its
# sub-blocks with it (`sub_block`)
BYTES_PER_VALUE = 104
_EPS = float(np.finfo(float).eps)

# Wichura (1988), «Algorithm AS241: the percentage points of the normal distribution», Applied Statistics 37(3), 477-484:
# the coefficients of PPND16, highest degree first, as R's qnorm and Python's statistics module evaluate them
_CENTRAL_NUM = (2.5090809287301226727e+3, 3.3430575583588128105e+4, 6.7265770927008700853e+4, 4.5921953931549871457e+4,
                1.3731693765509461125e+4, 1.9715909503065514427e+3, 1.3314166789178437745e+2, 3.3871328727963666080e+0)
_CENTRAL_DEN = (5.2264952788528545610e+3, 2.8729085735721942674e+4, 3.9307895800092710610e+4, 2.1213794301586595867e+4,
                5.3941960214247511077e+3, 6.8718700749205790830e+2, 4.2313330701600911252e+1, 1.0)
_NEAR_NUM = (7.7454501427834140764e-4, 2.2723844989269184583e-2, 2.4178072517745061177e-1, 1.2704582524523683826e+0,
             3.6478483247632046050e+0, 5.7694972214606914055e+0, 4.6303378461565452959e+0, 1.4234371107496835773e+0)
_NEAR_DEN = (1.0507500716444168432e-9, 5.4759380849953449460e-4, 1.5198666563616457197e-2, 1.4810397642748007459e-1,
             6.8976733498510000455e-1, 1.6763848301838038494e+0, 2.0531916266377588219e+0, 1.0)
_FAR_NUM = (2.0103343992922881327e-7, 2.7115555687434875782e-5, 1.2426609473880784386e-3, 2.6532189526576123093e-2,
            2.9656057182850489123e-1, 1.7848265399172913358e+0, 5.4637849111641143699e+0, 6.6579046435011037772e+0)
_FAR_DEN = (2.0442631033899397856e-15, 1.4215117583164458887e-7, 1.8463183175100546818e-5, 7.8686913114561325910e-4,
            1.4875361290850614853e-2, 1.3692988092273580531e-1, 5.9983220655588793769e-1, 1.0)


def _horner(coefficients: tuple[float, ...], r: np.ndarray) -> np.ndarray:
    out = np.full_like(r, coefficients[0])
    for c in coefficients[1:]:
        out *= r
        out += c
    return out


def inv_cdf(p: np.ndarray) -> np.ndarray:
    """The standard normal quantile Φ⁻¹(p), 0 < p < 1, elementwise (AS241, as `statistics.NormalDist().inv_cdf`)."""
    p = np.asarray(p, dtype=float)
    q = p - 0.5
    out = np.empty_like(p)
    central = np.abs(q) <= 0.425
    if central.any():
        qc = q[central]
        r = 0.180625 - qc * qc
        out[central] = _horner(_CENTRAL_NUM, r) * qc / _horner(_CENTRAL_DEN, r)
    tail = ~central
    if tail.any():
        qt = q[tail]
        r = np.sqrt(-np.log(np.where(qt <= 0.0, p[tail], 1.0 - p[tail])))
        near = r <= 5.0
        x = np.empty_like(r)
        rn = r[near] - 1.6
        x[near] = _horner(_NEAR_NUM, rn) / _horner(_NEAR_DEN, rn)
        rf = r[~near] - 5.0
        x[~near] = _horner(_FAR_NUM, rf) / _horner(_FAR_DEN, rf)
        out[tail] = np.where(qt < 0.0, -x, x)
    return out


def _mean(x: np.ndarray, axis) -> np.ndarray:
    """The mean with one correcting pass, as R's mean (whose sums are long doubles) refines its own."""
    m = x.mean(axis=axis, keepdims=True)
    m = m + (x - m).mean(axis=axis, keepdims=True)
    return m


def is_constant(x: np.ndarray) -> np.ndarray:
    """posterior's `is_constant` per quantity (axis 0): the range is one value, or at most 2.2e-16 of its largest |value|."""
    flat = x.reshape(x.shape[0], -1)
    lo, hi = flat.min(axis=1), flat.max(axis=1)
    spread = hi - lo
    with np.errstate(invalid="ignore", over="ignore"):
        return (lo == hi) | (np.isfinite(spread) & (spread <= _EPS * np.maximum(np.abs(lo), np.abs(hi))))


def split_chains(x: np.ndarray) -> np.ndarray:
    """(q, m, n) → (q, 2m, ⌊n/2⌋): each chain's first and last ⌊n/2⌋ draws (an odd n's middle draw left out); n = 1 kept."""
    n = x.shape[2]
    if n == 1:
        return x
    half = n // 2
    return np.concatenate((x[:, :, :half], x[:, :, n - half:]), axis=1)


def average_ranks(x: np.ndarray) -> np.ndarray:
    """The ranks 1…k of each row of x (q, k), ties given their average rank (R's `rank(ties.method = "average")`)."""
    q, k = x.shape
    order = np.argsort(x, axis=1, kind="stable")
    s = np.take_along_axis(x, order, axis=1)
    index = np.broadcast_to(np.arange(k), (q, k))
    differs = s[:, 1:] != s[:, :-1]
    starts = np.zeros((q, k), dtype=np.int64)
    starts[:, 1:] = np.where(differs, index[:, 1:], 0)
    np.maximum.accumulate(starts, axis=1, out=starts)
    ends = np.full((q, k), k - 1, dtype=np.int64)
    ends[:, :-1] = np.where(differs, index[:, :-1], k - 1)
    ends = np.minimum.accumulate(ends[:, ::-1], axis=1)[:, ::-1]
    ranks = np.empty((q, k))
    np.put_along_axis(ranks, order, (starts + ends) / 2.0 + 1.0, axis=1)
    return ranks


def z_scale(x: np.ndarray) -> np.ndarray:
    """posterior's `z_scale`: the normal scores of the average ranks among all the values of each quantity."""
    q = x.shape[0]
    flat = x.reshape(q, -1)
    s = flat.shape[1]
    return inv_cdf((average_ranks(flat) - 0.375) / (s - 2 * 0.375 + 1)).reshape(x.shape)


def quantiles(sorted_draws: np.ndarray, probs=PROBS) -> np.ndarray:
    """Hyndman and Fan's type 7 quantiles of each sorted row (q, S) → (q, len(probs)), in R's arithmetic."""
    q, s = sorted_draws.shape
    out = np.empty((q, len(probs)))
    for j, p in enumerate(probs):
        index = 1.0 + max(s - 1, 0) * p
        lo, hi = math.floor(index), math.ceil(index)
        a = sorted_draws[:, lo - 1]
        if index > lo:
            b = sorted_draws[:, hi - 1]
            h = index - lo
            out[:, j] = np.where(b != a, (1.0 - h) * a + h * b, a)
        else:
            out[:, j] = a
    return out


def median(sorted_draws: np.ndarray) -> np.ndarray:
    """R's median of each sorted row: the middle value, or the mean of the two middle values."""
    s = sorted_draws.shape[1]
    half = (s + 1) // 2
    if s % 2 == 1:
        return sorted_draws[:, half - 1].copy()
    return (sorted_draws[:, half - 1] + sorted_draws[:, half]) / 2.0


def rhat_basic(x: np.ndarray) -> np.ndarray:
    """posterior's `.rhat` of chains (q, m, n): √((n·var(chain means) / mean(chain variances) + n − 1) / n); NaN when the
    quantity's values are constant."""
    q, m, n = x.shape
    with np.errstate(divide="ignore", invalid="ignore"):
        means = _mean(x, axis=2)
        within = ((x - means) ** 2).sum(axis=2) / (n - 1)
        means = means[:, :, 0]
        between = n * ((means - _mean(means, axis=1)) ** 2).sum(axis=1) / (m - 1)
        out = np.sqrt((between / within.mean(axis=1) + n - 1) / n)
    out[is_constant(x)] = np.nan
    return out


def _next_fast(n: int) -> int:
    """The smallest 2^a 3^b 5^c at least n (R's `nextn`)."""
    v = max(1, n)
    while True:
        r = v
        for p in (2, 3, 5):
            while r % p == 0:
                r //= p
        if r == 1:
            return v
        v += 1


def autocovariance(x: np.ndarray) -> np.ndarray:
    """posterior's `autocovariance` of every chain (q, m, n) at lags 0 … n − 1: by FFT, padded to 2·nextn(n), lag 0 rescaled to
    the variance with divisor n; a chain of zero variance gives zeros."""
    n = x.shape[2]
    yc = x - _mean(x, axis=2)
    variance = (yc * yc).sum(axis=2, keepdims=True) / n
    size = 2 * _next_fast(n)
    spectrum = np.fft.rfft(yc, size, axis=2)
    del yc
    power = spectrum.real ** 2 + spectrum.imag ** 2
    del spectrum                                   # the largest array of the computation: freed at once
    ac = np.fft.irfft(power, size, axis=2)[:, :, :n]
    del power
    with np.errstate(divide="ignore", invalid="ignore"):
        ac = ac / ac[:, :, :1] * variance
    ac[np.broadcast_to(variance == 0.0, ac.shape)] = 0.0
    return ac


def _tau(rho: np.ndarray, n: int) -> np.ndarray:
    """posterior's τ̂ from the autocorrelations ρ̂ (q, n), ρ̂₀ = 1: Geyer's initial positive sequence of the pairs
    (ρ̂₂ₖ, ρ̂₂ₖ₊₁) while the previous pair's sum is positive and 2k ≤ n − 4, its monotone version (each pair's sum at most the
    previous one's: a running minimum), plus the last even term when positive or kept; τ̂ = 2 when no pair after the first
    is summed (R's 1:0 reads lag 0 alone)."""
    q = rho.shape[0]
    pairs = n // 2
    kmax = max(0, (n - 4) // 2)
    sums = rho[:, 0:2 * pairs:2] + rho[:, 1:2 * pairs:2]
    considered = sums[:, :kmax + 1]
    stop = ~(considered > 0.0)
    kstar = np.where(stop.any(axis=1), stop.argmax(axis=1), kmax)
    rows = np.arange(q)
    even = rho[rows, 2 * kstar]
    kept = (even > 0.0) | ((kstar >= 1) & (sums[rows, kstar] >= 0.0))
    last = np.where(kept, even, 0.0)
    running = np.cumsum(np.minimum.accumulate(considered, axis=1), axis=1)
    summed = np.where(kstar >= 1, running[rows, np.maximum(kstar - 1, 0)], 0.0)
    return np.where(kstar >= 1, -1.0 + 2.0 * summed + last, 2.0)


def ess_basic(x: np.ndarray) -> np.ndarray:
    """posterior's `.ess` of chains (q, m, n) already split: S / τ̂, τ̂ floored at 1/log₁₀(S), S = m·n; NaN when n < 3 or the
    quantity's values are constant."""
    q, m, n = x.shape
    out = np.full(q, np.nan)
    if n < 3:
        return out
    ok = ~is_constant(x)
    if not ok.any():
        return out
    y = x[ok] if not ok.all() else x
    acov = autocovariance(y).mean(axis=1)                # (q', n): the chains' mean autocovariance at each lag
    mean_var = acov[:, 0] * n / (n - 1)
    var_plus = mean_var * (n - 1) / n
    if m > 1:
        means = _mean(y, axis=2)[:, :, 0]
        var_plus = var_plus + ((means - _mean(means, axis=1)) ** 2).sum(axis=1) / (m - 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rho = 1.0 - (mean_var[:, None] - acov) / var_plus[:, None]
    del acov
    rho[:, 0] = 1.0
    tau = _tau(rho, n)
    s = m * n
    tau = np.where(tau < 1.0 / math.log10(s), 1.0 / math.log10(s), tau)
    out[ok] = s / tau
    return out


def measures(draws: np.ndarray, *, diagnostics: bool = True) -> dict[str, np.ndarray]:
    """The summary's numbers of a sub-block of quantities, draws shaped (quantities, chains, draws per chain): a dict of the
    names of NUMBERS, each an array (quantities,), NaN for NULL. `diagnostics` False (the prior predictive mode: independent
    draws) gives the mean, the sd and the quantiles alone, the rest NaN."""
    x = np.asarray(draws, dtype=float)
    if x.ndim != 3 or 0 in x.shape:
        raise ValueError(f"draws must be shaped (quantities, chains, draws), not {x.shape}")
    if not np.isfinite(x).all():
        raise ValueError("a draw is not finite: the follower refuses it (not_finite) before any measure")
    q, chains, n = x.shape
    s = chains * n
    flat = x.reshape(q, s)
    out: dict[str, np.ndarray] = {}
    mean = _mean(flat, axis=1)
    centred = flat - mean
    mean = mean[:, 0]
    out["mean"] = mean
    with np.errstate(divide="ignore", invalid="ignore"):
        sd = np.sqrt((centred * centred).sum(axis=1) / (s - 1)) if s > 1 else np.full(q, np.nan)
    out["sd"] = sd
    ordered = np.sort(flat, axis=1)
    qs = quantiles(ordered, PROBS)
    for j, name in enumerate(QUANTILE_NAMES):
        out[name] = qs[:, j]
    nulls = np.full(q, np.nan)
    if not diagnostics:
        for name in ("mcse_mean", "mcse_sd", "rhat", "ess_bulk", "ess_tail"):
            out[name] = nulls.copy()
        return out
    constant = is_constant(x)
    split = split_chains(x)
    # R-hat: the bulk's and the folded draws' (around the median of all the draws)
    z = z_scale(split)
    rhat_bulk = rhat_basic(z)
    ess_bulk = ess_basic(z)
    del z
    folded = np.abs(x - median(ordered)[:, None, None])
    rhat_tail = rhat_basic(z_scale(split_chains(folded)))
    del folded, ordered
    # the tail: the indicators of the 5 % and the 95 % quantiles of all the draws, the upper one mirrored
    ess_low = ess_basic(split_chains((x <= qs[:, 0, None, None]).astype(float)))
    ess_high = ess_basic(split_chains((x >= qs[:, 4, None, None]).astype(float)))
    # the Monte Carlo standard errors
    ess_mean = ess_basic(split)
    del split
    squares = (centred * centred).reshape(x.shape)
    del centred
    ess_squares = ess_basic(split_chains(squares))
    with np.errstate(divide="ignore", invalid="ignore"):
        variance = _mean(squares.reshape(q, s), axis=1)
        var_var = _mean((squares.reshape(q, s) - variance) ** 2, axis=1)[:, 0] / ess_squares
        variance = variance[:, 0]
        out["mcse_mean"] = sd / np.sqrt(ess_mean)
        out["mcse_sd"] = np.sqrt(var_var / variance / 4.0)
    out["rhat"] = np.maximum(rhat_bulk, rhat_tail)
    out["ess_bulk"] = ess_bulk
    out["ess_tail"] = np.minimum(ess_low, ess_high)
    undefined = constant | ((n // 2 if n > 1 else 1) < 3)   # the contract: fewer than 3 draws a split chain
    for name in ("mcse_mean", "mcse_sd", "rhat", "ess_bulk", "ess_tail"):
        v = out[name]
        v[undefined | ~np.isfinite(v)] = np.nan
    return out


def sub_block(compute_bytes: int, draws_per_quantity: int) -> int:
    """How many quantities a sub-block of `measures` holds within `compute_bytes` (at least one)."""
    return max(1, int(compute_bytes) // (BYTES_PER_VALUE * max(1, int(draws_per_quantity))))
