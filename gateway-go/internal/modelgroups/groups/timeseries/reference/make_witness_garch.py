"""The witness of the volatility (GARCH) node (docs/specs/garch-risk.md, «Self-test, reference values, witnesses»; lot G1): outside
R and outside Tabularia, each `garch_*` case of expected.json is computed again from the same CSV and compared with what the case
expects. Run by hand with the versions of witness.json:

    uv venv /tmp/gw && uv pip install --python /tmp/gw/bin/python statsmodels==0.15.0 numpy==2.5.5 scipy==1.18.1 \\
        pandas==3.1.1 arch==9.0.0
    /tmp/gw/bin/python -I gateway-go/internal/modelgroups/groups/timeseries/reference/make_witness_garch.py

    likelihood   a transcription of rugarch's recursions (the ARMA mean in deviations, the variance started at the training residuals'
                 mean square, the densities: normal, Student t scaled to variance 1, Fernández & Steel's skewed t as rugarch's dsstd)
                 evaluated at the case's estimates; and Python `arch` (GARCH, GJR with o = 1, EGARCH) on the cases with a constant mean
                 and a normal or t law, its backcast set so that its first variance is rugarch's
    estimates    the transcription maximised by SciPy (L-BFGS-B from a start 10 % away), on r / s as the runner does
    errors       the sandwich H⁻¹(GᵀG)H⁻¹ (robust) or H⁻¹ (classical) from numerical scores and Hessian of the transcription
    measures     persistence, κ = E[z² 1(z < 0)] by quadrature of the fitted density, half-life, the long-run σ, σ_T and σ_{T+1}
    checks       the weighted Ljung–Box (Fisher & Gallagher's gamma law), Engle's ARCH-LM (statsmodels OLS), Engle & Ng's sign bias
                 (statsmodels OLS, Wald), Nyblom's statistic from the transcription's numerical scores and its p-value by Imhof's
                 inversion of the eigenvalue series (truncated, the tail's mean added): the spec's road, not the runner's
    backtest     Kupiec, Christoffersen by hand (scipy.stats.chi2)

Exit 1 when a number differs by more than its tolerance; the worst difference of each kind is printed.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import integrate, optimize, special, stats
from statsmodels.regression.linear_model import OLS

HERE = Path(__file__).resolve().parent
TOL = {"optimum": 1e-8, "loglik": 1e-8, "arch_loglik": 1e-8, "estimate": 1e-4, "std_error": 1e-3, "measure": 1e-6, "check": 1e-6, "nyblom": 1e-5,
       "nyblom_p": 1e-5, "backtest": 1e-10}
# What the witness finds and the runner keeps, because the runner is rugarch's fit (written in the spec's «As built / G1»):
#   garch_prices_simple  rugarch's solvers stop at the persistence constraint (α₁ = 0, P = 0.999) on these 300 simple returns, with a
#                        log-likelihood 0.0138 below the interior maximum SciPy finds (α₁ = 0, β₁ = 0.967): the node says the bound
#                        with its fact `at_bound`; the case's estimates are rugarch's
#   EGARCH's errors      rugarch's Hessian starts Richardson's steps at 10 % of each parameter, so β₁ = 0.97 is moved past 1, where its
#                        recursion and a plain transcription part: its standard errors are not compared (they are by the self-test,
#                        J V Jᵀ of rugarch's own Hessian)
KNOWN = {"garch_prices_simple": ("optimum", "estimate")}
worst: dict[str, float] = {}
failed: list[str] = []
known: list[str] = []
case_id = ""


def check(kind: str, what: str, got: float, want: float, scale: float | None = None) -> None:
    gap = abs(got - want) / (abs(want) if scale is None else scale) if want != 0 or scale else abs(got)
    if kind in KNOWN.get(case_id, ()):
        if not gap <= TOL[kind]:
            known.append(f"{what}: {got!r}, rugarch's {want!r} ({kind} gap {gap:.3g})")
        return
    worst[kind] = max(worst.get(kind, 0.0), gap)
    if not gap <= TOL[kind]:
        failed.append(f"{what}: {got!r}, expected {want!r} ({kind} gap {gap:.3g})")


# ── the densities, as rugarch writes them ───────────────────────────────────────────────────────────────────────────
def logpdf(z: np.ndarray, dist: str, shape: float, skew: float) -> np.ndarray:
    if dist == "norm":
        return stats.norm.logpdf(z)
    if dist == "std":
        return std_logpdf(z, shape)
    # fGarch/rugarch dsstd: the standardized t skewed by ξ, moved and scaled to mean 0 and variance 1
    nu, xi = shape, skew
    m1 = 2 * math.sqrt(nu - 2) / (nu - 1) / special.beta(0.5, nu / 2)
    mu = m1 * (xi - 1 / xi)
    sigma = math.sqrt((1 - m1**2) * (xi**2 + 1 / xi**2) + 2 * m1**2 - 1)
    x = z * sigma + mu
    big = np.where(x >= 0, xi, 1 / xi)
    return math.log(2 / (xi + 1 / xi)) + std_logpdf(x / big, nu) + math.log(sigma)


def std_logpdf(z: np.ndarray, nu: float) -> np.ndarray:
    return (special.gammaln((nu + 1) / 2) - special.gammaln(nu / 2) - 0.5 * math.log(math.pi * (nu - 2))
            - (nu + 1) / 2 * np.log1p(z**2 / (nu - 2)))


def abs_mean(dist: str, shape: float, skew: float) -> float:
    if dist == "norm":
        return math.sqrt(2 / math.pi)
    return integrate.quad(lambda z: abs(z) * math.exp(logpdf(np.array([z]), dist, shape, skew)[0]), -np.inf, np.inf, epsabs=1e-13)[0]


def kappa(dist: str, shape: float, skew: float) -> float:
    return integrate.quad(lambda z: z * z * math.exp(logpdf(np.array([z]), dist, shape, skew)[0]), -np.inf, 0, epsabs=1e-13)[0]


# ── the recursion ───────────────────────────────────────────────────────────────────────────────────────────────────
class Model:
    def __init__(self, params: dict, r: np.ndarray, train: int):
        self.vm = params.get("variance_model", "garch")
        self.q = int(params.get("arch_order", 1))
        self.p = 0 if self.vm == "arch" else int(params.get("garch_order", 1))
        self.ar = int(params.get("ar_order", 0))
        self.ma = int(params.get("ma_order", 0))
        self.mean = bool(params.get("mean", True))
        self.dist = params.get("distribution", "std")
        self.r, self.train = r, train
        self.names = ((["mu"] if self.mean else []) + [f"ar{i}" for i in range(1, self.ar + 1)] + [f"ma{i}" for i in range(1, self.ma + 1)]
                      + ["omega"] + [f"alpha{i}" for i in range(1, self.q + 1)] + [f"beta{i}" for i in range(1, self.p + 1)]
                      + ([f"gamma{i}" for i in range(1, self.q + 1)] if self.vm in ("gjr", "egarch") else [])
                      + (["skew"] if self.dist == "sstd" else []) + (["shape"] if self.dist in ("std", "sstd") else []))

    def run(self, th: dict, y: np.ndarray, n_old: int):
        """μ_t, ε_t, σ²_t for every t of y: the mean in deviations with the lags it has, the variance started at the mean square of
        the first n_old residuals for its first max(p, q) instants, then the recursion."""
        n = len(y)
        mu0 = th.get("mu", 0.0)
        mean = np.empty(n)
        eps = np.empty(n)
        for t in range(n):
            m = mu0
            for i in range(1, self.ar + 1):
                if t - i >= 0:
                    m += th[f"ar{i}"] * (y[t - i] - mu0)
            for j in range(1, self.ma + 1):
                if t - j >= 0:
                    m += th[f"ma{j}"] * eps[t - j]
            mean[t] = m
            eps[t] = y[t] - m
        h0 = float(np.mean(eps[:n_old] ** 2))
        lag = max(self.p, self.q)
        h = np.empty(n)
        shape, skew = th.get("shape", 0.0), th.get("skew", 1.0)
        ez = abs_mean(self.dist, shape, skew) if self.vm == "egarch" else 0.0
        for t in range(n):
            if t < lag:
                h[t] = h0
                continue
            if self.vm == "egarch":
                v = th["omega"]
                for i in range(1, self.q + 1):
                    z = eps[t - i] / math.sqrt(h[t - i])
                    v += th[f"alpha{i}"] * z + th[f"gamma{i}"] * (abs(z) - ez)
                for j in range(1, self.p + 1):
                    v += th[f"beta{j}"] * math.log(h[t - j])
                h[t] = math.exp(min(max(v, -700.0), 700.0))
            else:
                v = th["omega"]
                for i in range(1, self.q + 1):
                    e2 = eps[t - i] ** 2
                    v += th[f"alpha{i}"] * e2 + (th[f"gamma{i}"] * e2 * (eps[t - i] < 0) if self.vm == "gjr" else 0.0)
                for j in range(1, self.p + 1):
                    v += th[f"beta{j}"] * h[t - j]
                h[t] = v
        return mean, eps, h

    def loglik_terms(self, th: dict, y: np.ndarray) -> np.ndarray:
        _, eps, h = self.run(th, y, len(y))
        z = eps / np.sqrt(h)
        return logpdf(z, self.dist, th.get("shape", 0.0), th.get("skew", 1.0)) - 0.5 * np.log(h)

    def persistence(self, th: dict) -> float:
        if self.vm == "egarch":
            return sum(th[f"beta{j}"] for j in range(1, self.p + 1))
        k = kappa(self.dist, th.get("shape", 0.0), th.get("skew", 1.0)) if self.vm == "gjr" else 0.0
        return (sum(th[f"alpha{i}"] for i in range(1, self.q + 1)) + sum(th[f"beta{j}"] for j in range(1, self.p + 1))
                + k * sum(th.get(f"gamma{i}", 0.0) for i in range(1, self.q + 1)))


def to_scaled(names: list[str], th: dict, s: float, vm: str) -> dict:
    out = dict(th)
    if "mu" in out:
        out["mu"] = th["mu"] / s
    if vm == "egarch":
        out["omega"] = th["omega"] - (1 - sum(th[n] for n in names if n.startswith("beta"))) * math.log(s * s)
    else:
        out["omega"] = th["omega"] / (s * s)
    return out


# numDeriv's Richardson extrapolation (genD, jacobian), as rugarch calls it for its Hessian (d = 0.1) and its scores (d = 1e-4):
# steps h = |d·x| (+ eps where |x| < zero.tol), r = 4 halvings, the 4^m extrapolation. Two numerical Hessians of the same likelihood
# taken with other steps differ by a few per cent where the likelihood has kinks (EGARCH's |z|, GJR's 1(ε < 0)): the witness takes
# the same road on the transcribed likelihood, so what it checks is the likelihood and the sandwich, not the step.
ZERO_TOL = math.sqrt(np.finfo(float).eps / 7e-7)


def _richardson(a: list) -> np.ndarray:
    a = [np.asarray(v, dtype=float) for v in a]
    r = len(a)
    for m in range(1, r):
        a = [(a[k + 1] * 4**m - a[k]) / (4**m - 1) for k in range(r - m)]
    return a[0]


def _h0(x: np.ndarray, d: float, eps: float = 1e-4) -> np.ndarray:
    return np.abs(d * x) + eps * (np.abs(x) < ZERO_TOL)


def num_scores(f, x: np.ndarray, d: float = 1e-4, r: int = 4) -> np.ndarray:
    """numDeriv::jacobian(method = "Richardson"): one column per parameter."""
    h0 = _h0(x, d)
    cols = []
    for i in range(len(x)):
        h, a = h0.copy(), []
        for _ in range(r):
            e = np.zeros(len(x))
            e[i] = h[i]
            a.append((f(x + e) - f(x - e)) / (2 * h[i]))
            h = h / 2
        cols.append(_richardson(a))
    return np.column_stack(cols)


def num_hessian(f, x: np.ndarray, d: float = 0.1, r: int = 4) -> np.ndarray:
    """numDeriv::hessian(method = "Richardson") through genD."""
    n = len(x)
    h0 = _h0(x, d)
    f0 = f(x)
    diag = np.empty(n)
    for i in range(n):
        h, a = h0.copy(), []
        for _ in range(r):
            e = np.zeros(n)
            e[i] = h[i]
            a.append((f(x + e) - 2 * f0 + f(x - e)) / h[i] ** 2)
            h = h / 2
        diag[i] = _richardson(a)
    H = np.diag(diag)
    for i in range(n):
        for j in range(i):
            h, a = h0.copy(), []
            for _ in range(r):
                e = np.zeros(n)
                e[i], e[j] = h[i], h[j]
                a.append((f(x + e) - 2 * f0 + f(x - e) - diag[i] * h[i] ** 2 - diag[j] * h[j] ** 2) / (2 * h[i] * h[j]))
                h = h / 2
            H[i, j] = H[j, i] = _richardson(a)
    return H


# ── the checks ──────────────────────────────────────────────────────────────────────────────────────────────────────
def acf(x: np.ndarray, m: int) -> np.ndarray:
    d = x - x.mean()
    c0 = np.dot(d, d)
    return np.array([np.dot(d[k:], d[:-k]) / c0 for k in range(1, m + 1)])


def weighted_lb(x: np.ndarray, m: int, K: int) -> tuple[float, float]:
    n = len(x)
    k = np.arange(1, m + 1)
    q = n * (n + 2) * np.sum((m - k + 1) / m * acf(x, m) ** 2 / (n - k))
    d = 2 * m * m + 3 * m + 1 - 6 * m * K
    return q, stats.gamma.sf(q, 0.75 * (m + 1) ** 2 * m / d, scale=(2 / 3) * d / (m * (m + 1)))


def arch_lm(z: np.ndarray, lags: int) -> tuple[float, float]:
    e2 = z**2
    y = e2[lags:]
    X = np.column_stack([np.ones(len(y))] + [e2[lags - j:-j] for j in range(1, lags + 1)])
    fit = OLS(y, X).fit()
    stat = len(y) * fit.rsquared
    return stat, stats.chi2.sf(stat, lags)


def sign_bias(z: np.ndarray) -> tuple[float, float]:
    lag = z[:-1]
    neg = (lag < 0).astype(float)
    X = np.column_stack([np.ones(len(lag)), neg, neg * lag, (1 - neg) * lag])
    fit = OLS(z[1:] ** 2, X).fit()
    b = fit.params[1:]
    w = float(b @ np.linalg.solve(fit.cov_params()[1:, 1:], b))
    return w, stats.chi2.sf(w, 3)


def imhof_sf(x: float, k: int, J: int = 2000) -> float:
    """P(Σ_j χ²_{k,j} / (j²π²) > x) by Imhof's (1961) formula with J eigenvalues, the tail's mean added to the constant."""
    lam = 1 / (np.arange(1, J + 1) ** 2 * math.pi**2)
    tail = k * (1 / 6 - lam.sum())  # Σ_{j≥1} 1/(j²π²) = 1/6
    y = x - tail

    def f(u):
        theta = 0.5 * k * np.sum(np.arctan(lam * u)) - 0.5 * y * u
        rho = np.exp(0.25 * k * np.sum(np.log1p((lam * u) ** 2)))
        return math.sin(theta) / (u * rho)

    val, _ = integrate.quad(f, 0, np.inf, limit=2000, epsabs=1e-12)
    return 0.5 + val / math.pi


def backtest(r: np.ndarray, var: np.ndarray, level: float) -> dict:
    hit = (r < var).astype(int)
    T, x, p = len(hit), int(hit.sum()), 1 - level
    nl = lambda n, q: 0.0 if n == 0 else n * math.log(q)  # noqa: E731
    out = {"violations": x, "kupiec": -2 * (nl(T - x, 1 - p) + nl(x, p)) + 2 * (nl(T - x, 1 - x / T) + nl(x, x / T))}
    a, b = hit[:-1], hit[1:]
    n00, n01, n10, n11 = (int(np.sum((a == i) & (b == j))) for i, j in ((0, 0), (0, 1), (1, 0), (1, 1)))
    if x:
        p01, p11, pi = n01 / (n00 + n01), (n11 / (n10 + n11) if n10 + n11 else 0.0), (n01 + n11) / (T - 1)
        mk = nl(n00, 1 - p01) + nl(n01, p01) + nl(n10, 1 - p11) + nl(n11, p11)
        out["ind"] = -2 * (nl(n00 + n10, 1 - pi) + nl(n01 + n11, pi)) + 2 * mk
        out["cc"] = -2 * (nl(n00 + n10, 1 - p) + nl(n01 + n11, p)) + 2 * mk
    return out


# ── Python `arch` on the cases it has ───────────────────────────────────────────────────────────────────────────────
def arch_loglik(mod: Model, th: dict, y: np.ndarray) -> float | None:
    from arch import arch_model

    if mod.ar or mod.ma or not mod.mean or mod.dist == "sstd" or mod.p != 1 or mod.q != 1:
        return None
    _, eps, h = mod.run(th, y, mod.train)
    h0 = h[0]
    if mod.vm == "egarch":
        am = arch_model(y, mean="Constant", vol="EGARCH", p=1, o=1, q=1, dist="normal" if mod.dist == "norm" else "t", rescale=False)
        bc = (math.log(h0) - th["omega"]) / th["beta1"]
        params = [th["mu"], th["omega"], th["gamma1"], th["alpha1"], th["beta1"]]
    else:
        o = 1 if mod.vm == "gjr" else 0
        am = arch_model(y, mean="Constant", vol="GARCH", p=1, o=o, q=1, dist="normal" if mod.dist == "norm" else "t", rescale=False)
        gam = th.get("gamma1", 0.0)
        bc = (h0 - th["omega"]) / (th["alpha1"] + 0.5 * gam + th["beta1"])
        params = [th["mu"], th["omega"], th["alpha1"]] + ([gam] if o else []) + [th["beta1"]]
    if mod.dist == "std":
        params.append(th["shape"])
    # arch starts its recursion at t = 0 from the backcast; rugarch holds σ²_0 = h0: the backcast that gives arch's σ²_0 = h0.
    # arch's own variance recursion and density, on the training part
    resids = y[: mod.train] - params[0]
    nv = am.volatility.num_params
    sigma2 = np.empty(mod.train)
    am.volatility.compute_variance(np.array(params[1:1 + nv]), resids, sigma2, bc, am.volatility.variance_bounds(resids))
    return float(am.distribution.loglikelihood(np.array(params[1 + nv:]), resids, sigma2))


def main() -> int:
    ref = json.loads((HERE / "expected.json").read_text(encoding="utf-8"))
    for case in ref["cases"]:
        if case["model"] != "garch" or case.get("refuses"):
            continue
        want = {e["key"]: e for e in case["expect"]}
        params = case["params"]
        raw = pd.read_csv(HERE / case["data"])
        x = raw[params["value"]].to_numpy(float)
        prices = params.get("input_kind") == "prices"
        r = (np.diff(np.log(x)) if params.get("returns_type", "log") == "log" else x[1:] / x[:-1] - 1) if prices else x
        n, k = len(r), int(params.get("holdout", 0))
        train = n - k
        s = float(np.std(r[:train], ddof=1))
        mod = Model(params, r, train)
        th = {name: want[f"coef|{name}||estimate"]["value"] for name in mod.names}
        cid = case["id"]
        global case_id
        case_id = cid
        # the likelihood on the series' units at the case's estimates
        ll = float(np.sum(mod.loglik_terms(th, r[:train])))
        check("loglik", f"{cid} log-likelihood", ll, want["model|||log_lik"]["value"])
        al = arch_loglik(mod, th, r)
        if al is not None:
            check("arch_loglik", f"{cid} arch's log-likelihood", al, want["model|||log_lik"]["value"])
        # the estimates: the transcription maximised on r / s, from a start 10 % away
        y = r[:train] / s
        names = mod.names
        start = to_scaled(names, th, s, mod.vm)
        x0 = np.array([start[n_] * (0.9 if i % 2 else 1.1) for i, n_ in enumerate(names)])
        bounds = [(-1, 1) if n_.startswith(("ar", "ma")) else (2.1, 100) if n_ == "shape" else (0.1, 10) if n_ == "skew" else
                  (None, None) if n_ in ("mu",) or mod.vm == "egarch" else (1e-12, None) for n_ in names]
        if mod.vm == "egarch":
            bounds = [(-1, 1) if n_.startswith(("ar", "ma", "beta")) else b for n_, b in zip(names, bounds)]

        def nll(v):
            try:
                with np.errstate(all="ignore"):
                    val = -np.sum(mod.loglik_terms(dict(zip(names, v)), y))
            except (ValueError, OverflowError, ZeroDivisionError):
                return 1e10
            return val if np.isfinite(val) else 1e10

        # rugarch's constraint: the persistence at most 0.999 (.garchconbounds)
        cons = [] if mod.vm == "egarch" else [{"type": "ineq", "fun": lambda v: 0.999 - mod.persistence(dict(zip(names, v)))}]
        opt = optimize.minimize(nll, x0, method="SLSQP", bounds=bounds, constraints=cons, options={"ftol": 1e-14, "maxiter": 2000})
        opt = optimize.minimize(nll, opt.x, method="SLSQP", bounds=bounds, constraints=cons, options={"ftol": 1e-15, "maxiter": 2000})
        est_s = dict(zip(names, opt.x))
        theta_hat = dict(est_s)
        if "mu" in theta_hat:
            theta_hat["mu"] *= s
        theta_hat["omega"] = (est_s["omega"] + (1 - sum(v for n_, v in est_s.items() if n_.startswith("beta"))) * math.log(s * s)
                              if mod.vm == "egarch" else est_s["omega"] * s * s)
        # a parameter at a bound of the solver (0 for ω, α, β, γ, or the persistence at 0.999): its standard error is not reliable,
        # in rugarch nor here (the fact `at_bound`); the others are compared
        sc = to_scaled(names, th, s, mod.vm)
        at = {n_ for n_ in names if mod.vm != "egarch" and n_.startswith(("omega", "alpha", "beta", "gamma")) and abs(sc[n_]) < 1e-6}
        bound = bool(at) or (mod.vm != "egarch" and mod.persistence(th) >= 0.999 - 1e-6)
        # standard errors from the transcription on r / s (parameters of order one), brought to the series' units by J
        v0 = np.array([sc[n_] for n_ in names])
        terms = lambda v: mod.loglik_terms(dict(zip(names, v)), y)  # noqa: E731
        G = num_scores(terms, v0)
        H = num_hessian(lambda v: float(np.sum(terms(v))), v0)
        Hi = np.linalg.inv(-H)
        Vs = Hi @ (G.T @ G) @ Hi if params.get("se_type", "robust") == "robust" else Hi
        J = np.diag([s if n_ == "mu" else (s * s if n_ == "omega" and mod.vm != "egarch" else 1.0) for n_ in names])
        if mod.vm == "egarch":
            for jx, n_ in enumerate(names):
                if n_.startswith("beta"):
                    J[names.index("omega"), jx] = -math.log(s * s)
        V = J @ Vs @ J.T
        # the two optimizers' likelihoods: SciPy's may not be above rugarch's by more than its own tolerance
        ll_r, ll_s = -nll(np.array([sc[n_] for n_ in names])), -opt.fun
        check("optimum", f"{cid} SciPy's maximum above rugarch's", max(0.0, ll_s - ll_r), 0.0, scale=abs(ll_r))
        for i, name in enumerate(names):
            se = want[f"coef|{name}||std_error"]["value"]
            if name not in at:
                # on a flat ridge two optimizers stop apart at the same likelihood: within 1e-4 of the estimate or 1e-3 of its error
                check("estimate", f"{cid} {name}", theta_hat[name], th[name], scale=max(abs(th[name]), 10 * se))
            if not bound and mod.vm != "egarch":
                check("std_error", f"{cid} se {name}", math.sqrt(V[i, i]), se)
        # the measures
        P = mod.persistence(th)
        check("measure", f"{cid} persistence", P, want["model|||persistence"]["value"])
        if "model|||half_life" in want:
            check("measure", f"{cid} half-life", math.log(0.5) / math.log(P), want["model|||half_life"]["value"])
        if mod.vm == "gjr":
            check("measure", f"{cid} kappa", kappa(mod.dist, th["shape"], th["skew"]), want["model|||kappa"]["value"])
        if mod.vm != "egarch":
            check("measure", f"{cid} long-run sigma", math.sqrt(th["omega"] / (1 - P)), want["model|||uncond_sigma"]["value"])
        mean, eps, h = mod.run(th, r, train)
        sig = np.sqrt(h)
        check("measure", f"{cid} sigma_T", sig[-1], want["model|||sigma_last"]["value"])
        # σ_{T+1}: one more step of the recursion
        ext_mod_r = np.append(r, 0.0)
        _, _, h1 = mod.run(th, ext_mod_r, train)
        check("measure", f"{cid} sigma_T+1", math.sqrt(h1[-1]), want["model|||sigma_next"]["value"])
        # the checks of the training part's z
        z = eps[:train] / sig[:train]
        m = int(want["model|||lb_lag_used"]["value"])
        kz, kz2 = mod.ar + mod.ma, mod.q + mod.p
        for key, (stat, p) in (("ljung_box_z", weighted_lb(z, m, kz)), ("ljung_box_z2", weighted_lb(z**2, m, kz2)),
                               ("arch_lm", arch_lm(z, 12)), ("sign_bias", sign_bias(z))):
            check("check", f"{cid} {key} statistic", stat, want[f"assumption|{key}||statistic"]["value"])
            check("check", f"{cid} {key} p", p, want[f"assumption|{key}||p_value"]["value"], scale=1.0)
        # Nyblom: the statistic from the scores of the transcription, its p-value by Imhof
        Gs = num_scores(lambda v: mod.loglik_terms(dict(zip(names, v)), y), np.array([to_scaled(names, th, s, mod.vm)[n_] for n_ in names]))
        S = np.cumsum(Gs, axis=0)
        L = float(np.trace(S.T @ S @ np.linalg.inv(Gs.T @ Gs))) / train
        check("nyblom", f"{cid} Nyblom's statistic", L, want["assumption|nyblom||statistic"]["value"])
        check("nyblom_p", f"{cid} Nyblom's p", imhof_sf(want["assumption|nyblom||statistic"]["value"], len(names)),
              want["assumption|nyblom||p_value"]["value"], scale=1.0)
        if k:
            dist_q = {"norm": lambda a: stats.norm.ppf(a), "std": lambda a: stats.t.ppf(a, th["shape"]) * math.sqrt((th["shape"] - 2) / th["shape"])}
            if mod.dist in dist_q:
                var = mean + sig * dist_q[mod.dist](1 - float(params.get("var_level", 0.99)))
                bt = backtest(r[train:], var[train:], float(params.get("var_level", 0.99)))
                check("backtest", f"{cid} violations", bt["violations"], want["backtest|counts||violations"]["value"], scale=1.0)
                for key, name in (("kupiec", "kupiec"), ("ind", "christoffersen_ind"), ("cc", "christoffersen_cc")):
                    if key in bt:
                        check("backtest", f"{cid} {name}", bt[key], want[f"backtest|{name}||statistic"]["value"], scale=max(1.0, bt[key]))
    # the bridge law at Anderson & Darling's (1952) exact quantiles of ∫B²
    for q, p in ((0.34730, 0.10), (0.46136, 0.05), (0.74346, 0.01)):
        check("nyblom_p", f"Imhof at {q}", imhof_sf(q, 1), p, scale=1.0 / 0.2)
    for kind, gap in sorted(worst.items()):
        print(f"{kind:12s} worst {gap:.3g} (tolerance {TOL[kind]:g})")
    for line in known:
        print("known gap", line)
    for line in failed:
        print("FAILED", line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
