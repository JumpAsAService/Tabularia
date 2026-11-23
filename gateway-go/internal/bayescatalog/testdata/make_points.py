#!/usr/bin/env python3
"""The witness's points of the distribution catalogue (docs/specs/bayes-distribution-catalog.md,
«Testing strategy» and «Commands»; docs/specs/bayes-image.md, the self-test's check 7).

SciPy computes, outside Tabularia's image, the log-density (or log-mass), the density (or mass),
the log-CDF and the CDF of every distribution of catalog.json that the browser draws or that Stan
evaluates, and the quantiles of the browser's ones, at points chosen by the probability levels
1e-6, 0.01, 0.5, 0.99 and 1 - 1e-6 of a few argument sets (extreme ones included). The output,
points.json, is read back by the frontend test and copied byte for byte into the group's reference
data. Run it by hand; nothing in Tabularia runs it.

Run from the repository root, with the pins the boosting group's witness already uses:

    uv run --no-project --with scipy==1.18.1 --with numpy==2.5.5 \\
      python gateway-go/internal/bayescatalog/testdata/make_points.py

or in any environment that has those two versions (uv venv, then uv pip install). The script
writes points.json next to itself, prints a count per distribution, and checks four closed forms
first (a gamma density, an exponential CDF, the mean and variance of the negative binomial in
its mu and phi form, a binomial mass); it exits 1 when one of them or a value is not finite.

Parametrizations (the catalogue's order of arguments; Stan's names in its manual):
  normal(mu, sigma)                scipy.stats.norm(loc=mu, scale=sigma)
  student_t(nu, mu, sigma)         scipy.stats.t(df=nu, loc=mu, scale=sigma)
  cauchy(mu, sigma)                scipy.stats.cauchy(loc=mu, scale=sigma)
  exponential(beta), rate beta     scipy.stats.expon(scale=1/beta)
  gamma(alpha, beta), RATE beta    scipy.stats.gamma(a=alpha, scale=1/beta)
  inv_gamma(alpha, beta), SCALE beta   scipy.stats.invgamma(a=alpha, scale=beta)
  lognormal(mu, sigma)             scipy.stats.lognorm(s=sigma, scale=exp(mu))
  beta(alpha, beta)                scipy.stats.beta(a=alpha, b=beta)
  uniform(alpha, beta)             scipy.stats.uniform(loc=alpha, scale=beta - alpha)
  std_normal()                     scipy.stats.norm()
  bernoulli_logit(alpha)           p = expit(alpha)
  binomial_logit(N, alpha)         binom(N, expit(alpha))
  poisson_log(alpha)               poisson(exp(alpha))
  neg_binomial_2_log(eta, phi)     mu = exp(eta), variance mu + mu^2/phi, nbinom(n=phi, p=phi/(phi+mu))
  lkj_corr_cholesky(eta), K        MARGINAL: one correlation r = Omega[2,1] of a K x K correlation
                                   matrix; u = (r + 1)/2 ~ Beta(eta - 1 + K/2, eta - 1 + K/2), so the
                                   density of r is 1/2 times the Beta density of u
  multi_normal_cholesky(mu, L)     MARGINAL: component k is normal(mu[k], sd_k) with sd_k the norm of
                                   row k of L (L is the Cholesky factor of the covariance)

points.json, format 1:
  "witness":  the versions of Python, SciPy and NumPy that computed it;
  "cases":    one object per point, with
                "dist"      the catalogue's id;
                "kind"      "density" (a continuous distribution), "mass" (a discrete one),
                            "marginal" (LKJ or the multivariate normal, see above);
                "args"      the arguments in the catalogue's order (vectors and matrices as lists);
                "x"         the point (a real, an integer, a correlation r, or a component);
                "p"         the probability level the point is the quantile of, or null;
                "logpdf", "pdf" (density kinds) or "logpmf", "pmf" (mass kind): the log-density
                            (or log-mass) and its exponential; "pdf" is written only while it is
                            above 1e-300, below that it is absent and not compared;
                "logcdf", "cdf": where the catalogue has a CDF (lcdf), its log and its value (the
                            same underflow rule); absent otherwise;
                "k"         for multi_normal_cholesky, the component;
                "dim"       for lkj_corr_cholesky, the dimension K;
                "stan"      where Stan's own function is known to be less accurate than the self-test's declared
                            tolerances (docs/specs/bayes-image.md, check 7), the wider relative tolerance of its
                            "logpdf_rel" or "logcdf_rel" and the "reason", with the error measured; absent otherwise
                            (STAN_TOLERANCES below; the browser's test does not read it).
  "quantiles" are the cases with "p" set: quantile(p, args) = x.
Every number is written with Python's repr, so it reads back as the same double.
"""
from __future__ import annotations

import json
import math
import platform
import sys
from pathlib import Path

import numpy as np
import scipy
from scipy import stats
from scipy.special import expit

OUT = Path(__file__).with_name("points.json")
P_LEVELS = [1e-6, 0.01, 0.5, 0.99, 1 - 1e-6]
UNDERFLOW = 1e-300


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def close(a: float, b: float, rel: float = 1e-12) -> None:
    if not math.isclose(a, b, rel_tol=rel, abs_tol=1e-300):
        fail(f"closed form differs: {a!r} against {b!r}")


def self_checks() -> None:
    """Four values by the closed form, against the parametrizations above."""
    # gamma(2, rate 3) at 1: 3^2 * 1 * e^-3 / Gamma(2) = 9 e^-3
    close(float(stats.gamma(a=2, scale=1 / 3).pdf(1.0)), 9 * math.exp(-3))
    # exponential(rate 2) at 1: CDF 1 - e^-2
    close(float(stats.expon(scale=1 / 2).cdf(1.0)), 1 - math.exp(-2), rel=1e-12)
    # neg_binomial_2(mu, phi): mean mu, variance mu + mu^2/phi
    mu, phi = 4.0, 2.5
    mean, var = stats.nbinom.stats(phi, phi / (phi + mu), moments="mv")
    close(float(mean), mu)
    close(float(var), mu + mu * mu / phi)
    # binomial_logit(N = 1, alpha = 0): p = 1/2, so the mass at 0 is 1/2
    close(math.exp(float(stats.binom.logpmf(0, 1, expit(0.0)))), 0.5)


def num(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        fail(f"a value is not finite: {value!r}")
    return value


def density_case(cases: list, dist: str, kind: str, args: list, x: float, frozen, p=None, has_cdf=False,
                 **extra) -> None:
    """A continuous point: log-density, density and, where the catalogue has one, the CDF."""
    case = {"dist": dist, "kind": kind, "args": args, "x": num(x), "p": p}
    case.update(extra)
    logpdf = num(frozen.logpdf(x))
    case["logpdf"] = logpdf
    if logpdf > math.log(UNDERFLOW):
        case["pdf"] = num(math.exp(logpdf))
    if has_cdf:
        logcdf = num(frozen.logcdf(x))
        case["logcdf"] = logcdf
        if logcdf > math.log(UNDERFLOW):
            case["cdf"] = num(math.exp(logcdf))
    cases.append(case)


def mass_case(cases: list, dist: str, args: list, x: int, logpmf: float, **extra) -> None:
    case = {"dist": dist, "kind": "mass", "args": args, "x": int(x), "p": None}
    case.update(extra)
    logpmf = num(logpmf)
    case["logpmf"] = logpmf
    if logpmf > math.log(UNDERFLOW):
        case["pmf"] = num(math.exp(logpmf))
    cases.append(case)


# The nine prior distributions the browser draws exactly, and std_normal (internal, evaluated by
# Stan only). The argument sets include the extremes the frontend test asks for.
CONTINUOUS = {
    "normal": ([[0.0, 1e-3], [0.0, 1e3], [-2.0, 0.5]],
               lambda a: stats.norm(loc=a[0], scale=a[1])),
    "student_t": ([[1.0, 0.0, 1.0], [2.0, 1.5, 1e-3], [30.0, -1.0, 1e3]],
                  lambda a: stats.t(df=a[0], loc=a[1], scale=a[2])),
    "cauchy": ([[0.0, 1e-3], [2.0, 1e3], [-1.0, 1.0]],
               lambda a: stats.cauchy(loc=a[0], scale=a[1])),
    "exponential": ([[1e-3], [1.0], [1e3]],
                    lambda a: stats.expon(scale=1 / a[0])),
    "gamma": ([[0.5, 1.0], [50.0, 2.0], [2.0, 3.0]],
              lambda a: stats.gamma(a=a[0], scale=1 / a[1])),
    "inv_gamma": ([[0.5, 1.0], [50.0, 2.0], [3.0, 0.1]],
                  lambda a: stats.invgamma(a=a[0], scale=a[1])),
    "lognormal": ([[0.0, 1e-3], [1.0, 0.5], [-1.0, 3.0]],
                  lambda a: stats.lognorm(s=a[1], scale=math.exp(a[0]))),
    "beta": ([[0.5, 0.5], [50.0, 2.0], [2.0, 5.0]],
             lambda a: stats.beta(a=a[0], b=a[1])),
    "uniform": ([[-1.0, 1.0], [0.0, 1e3], [-1e-3, 1e-3]],
                lambda a: stats.uniform(loc=a[0], scale=a[1] - a[0])),
    "std_normal": ([[]], lambda a: stats.norm()),
}


# The cases where Stan 2.40's function differs from SciPy's more than the self-test's declared tolerances (check 7 of
# docs/specs/bayes-image.md: a relative 1e-10 for the log-density, 1e-8 for the log-CDF), each with the wider tolerance it
# gets and why: measured on the pinned CmdStan by that self-test (lot B0, 2026-11-23). Keyed by the distribution, its
# arguments and the probability level of the point.
STAN_TOLERANCES = {
    ("student_t", (1.0, 0.0, 1.0), 1e-6): {
        "logcdf_rel": 1e-5,
        "reason": "Stan 2.40's student_t_lcdf with nu = 1 in the far tail: relative error 2.1e-7 measured against the "
                  "closed form log(1e-6) of the Cauchy CDF at its 1e-6 quantile"},
    ("student_t", (1.0, 0.0, 1.0), 1 - 1e-6): {
        "logcdf_rel": 1e-5,
        "reason": "Stan 2.40's student_t_lcdf with nu = 1 in the far tail: relative error 2.9e-6 measured against the "
                  "closed form log(1 - 1e-6) of the Cauchy CDF at its 1 - 1e-6 quantile"},
}


def stan_tolerance(dist: str, args: list, p) -> dict:
    found = STAN_TOLERANCES.get((dist, tuple(args), p))
    return {"stan": found} if found else {}


def main() -> None:
    self_checks()
    cases: list = []

    for dist, (sets, make) in CONTINUOUS.items():
        for args in sets:
            frozen = make(args)
            for p in P_LEVELS:
                x = float(frozen.ppf(p))
                density_case(cases, dist, "density", args, x, frozen, p=p, has_cdf=True,
                             **stan_tolerance(dist, args, p))

    # LKJ: one correlation r of a K x K matrix; u = (r + 1)/2 ~ Beta(a, a), a = eta - 1 + K/2
    for dim, eta in [(2, 1.0), (3, 2.0), (5, 0.5)]:
        a = eta - 1 + dim / 2
        beta = stats.beta(a=a, b=a)
        for p in P_LEVELS:
            x = 2 * float(beta.ppf(p)) - 1
            u = (x + 1) / 2
            logpdf = num(math.log(0.5) + float(beta.logpdf(u)))
            case = {"dist": "lkj_corr_cholesky", "kind": "marginal", "args": [eta], "dim": dim,
                    "x": num(x), "p": p, "logpdf": logpdf}
            if logpdf > math.log(UNDERFLOW):
                case["pdf"] = num(math.exp(logpdf))
            cases.append(case)

    # Multivariate normal: marginal of component k, sd_k the norm of row k of the Cholesky factor L
    mu = [0.5, -1.0, 2.0]
    lower = [[1.0, 0.0, 0.0], [0.3, 0.8, 0.0], [-0.5, 0.2, 1.2]]
    for k in range(3):
        sd = float(np.sqrt(np.sum(np.array(lower[k]) ** 2)))
        frozen = stats.norm(loc=mu[k], scale=sd)
        for p in P_LEVELS:
            x = float(frozen.ppf(p))
            density_case(cases, "multi_normal_cholesky", "marginal", [mu, lower], x, frozen, p=p,
                         has_cdf=False, k=k)

    # Discrete ones: Stan's _lpmf; the catalogue has no CDF for them
    for alpha in [-3.0, 0.7, 2.5]:
        for x in (0, 1):
            mass_case(cases, "bernoulli_logit", [alpha], x,
                      stats.bernoulli.logpmf(x, expit(alpha)))
    for n, alpha in [(10, -1.5), (100, 3.0), (1, 0.2)]:
        for x in sorted({0, 1, n // 2, n}):
            mass_case(cases, "binomial_logit", [n, alpha], x,
                      stats.binom.logpmf(x, n, expit(alpha)))
    for alpha in [-2.0, 0.0, 3.0]:
        for x in (0, 1, 5, 30):
            mass_case(cases, "poisson_log", [alpha], x, stats.poisson.logpmf(x, math.exp(alpha)))
    for eta, phi in [(-1.0, 0.5), (2.0, 10.0), (0.0, 1e3)]:
        mu_nb = math.exp(eta)
        for x in (0, 1, 7, 100):
            mass_case(cases, "neg_binomial_2_log", [eta, phi], x,
                      stats.nbinom.logpmf(x, phi, phi / (phi + mu_nb)))

    OUT.write_text(json.dumps({
        "format_version": 1,
        "witness": {
            "python": platform.python_version(),
            "scipy": scipy.__version__,
            "numpy": np.__version__,
        },
        "cases": cases,
    }, indent=1, allow_nan=False) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for case in cases:
        counts[case["dist"]] = counts.get(case["dist"], 0) + 1
    for dist, count in counts.items():
        print(f"{dist}: {count}")
    print(f"{len(cases)} cases written to {OUT.name}")


if __name__ == "__main__":
    main()
