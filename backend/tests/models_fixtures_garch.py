"""The results fixtures of the volatility (GARCH) node and of the risk simulation node (docs/specs/garch-risk.md, «The results
table», «Explaining the method in the report»): one table per variant (`garch-default`, `risk_simulation-garch`,
`risk_simulation-arima`), made from imaginary series, so that the report lane (G4) can be built and `results.py` can be tried
before the runners exist.

    cd backend && python -m tests.models_fixtures garch

The numbers are placeholders made here by formulas, consistent with each other, never the runners' own (the real ones replace them,
lots G1 and G3): a GJR-GARCH(1,1) recursion on deterministic shocks gives the returns and their volatility; the checks are the
spec's formulas applied to them where they are simple (Kupiec's and Christoffersen's likelihood ratios, the portmanteau Q with a χ²
tail) and placeholders where they are not (Nyblom's L, the sign bias); the risk measures are those of 10,000 imaginary paths whose
distribution is a Student t scaled by the horizon. Every verdict is a p-value against the node's alpha. Every row the catalogue
declares for the variant that a run with these parameters writes is here, and the `reading` rows list the templates of the spec as
`@id name=value` (the notes' format).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from tests.models_fixtures_decomposition import Table, _chi2_sf, _stars, note

EPOCH = date(1970, 1, 1)
ALPHA = 0.05

PARAMS: dict[tuple[str, str], dict[str, Any]] = {
    # three years of a durum wheat's daily prices (a trading-day series: `index`, the dates are labels), the last 250 kept out
    ("garch", "default"): {"time": "data", "value": "prezzo", "frequency": "index", "input_kind": "prices", "returns_type": "log",
                           "variance_model": "gjr", "distribution": "sstd", "holdout": 250, "horizon": 10, "output": "volatility"},
    ("risk_simulation", "garch"): {"time": "data", "value": "prezzo", "frequency": "index", "model": "garch", "input_kind": "prices",
                                   "variance_model": "gjr", "distribution": "sstd", "paths": 10_000, "horizon": 10, "holdout": 250,
                                   "threshold": 280.0, "position": 100000, "output": "risk_summary"},
    # a business series on its upper tail: next month's orders, the running total of a SARIMA
    ("risk_simulation", "arima"): {"time": "giorno", "value": "ordini", "frequency": "day", "model": "arima", "p": 1, "d": 0, "q": 1,
                                   "P": 0, "D": 1, "Q": 1, "aggregate": "sum", "tail": "upper", "threshold": 1000, "paths": 10_000,
                                   "horizon": 30, "output": "risk_summary"},
}

_Z_LOW = -2.5  # the 1 % quantile of the imaginary standardized distribution (a skewed t with 5.8 degrees of freedom)


def _noise(i: int, k: float = 12.9898) -> float:
    """A deterministic number in (−0.5, 0.5): the same on every machine."""
    return math.sin(i * k + 4.1414) * 43758.5453 % 1.0 - 0.5


def _shock(i: int) -> float:
    """A standardized shock with heavy-ish tails: a sum of three uniforms scaled to variance 1, a cube added for the tails."""
    u = (_noise(i) + _noise(i + 7919, 78.233) + _noise(i + 104729, 37.719)) * 2.0
    return u + 0.35 * u * abs(u) / 2.0


def _series(n: int, holdout: int) -> dict[str, Any]:
    """Imaginary returns from a GJR-GARCH(1,1) recursion, their conditional volatility and the VaR at 99 %."""
    mu, omega, alpha, beta, gamma = 0.0003, 2.0e-6, 0.04, 0.90, 0.09
    kappa = 0.5
    persistence = alpha + beta + kappa * gamma
    uncond = math.sqrt(omega / (1 - persistence))
    shocks = [_shock(i) for i in range(n)]
    sd = math.sqrt(sum(s * s for s in shocks) / n)
    shocks = [s / sd for s in shocks]
    sigma2, eps, r, sigma, z = uncond ** 2 * 2.2, 0.0, [], [], []
    for t in range(n):
        s = math.sqrt(sigma2)
        eps = s * shocks[t]
        r.append(mu + eps)
        sigma.append(s)
        z.append(shocks[t])
        sigma2 = omega + (alpha + (gamma if eps < 0 else 0.0)) * eps * eps + beta * sigma2
    var = [mu + s * _Z_LOW for s in sigma]
    first_hold = n - holdout
    violations = [1 if r[t] < var[t] else 0 for t in range(n)]
    return {"mu": mu, "omega": omega, "alpha": alpha, "beta": beta, "gamma": gamma, "kappa": kappa, "persistence": persistence,
            "uncond": uncond, "r": r, "sigma": sigma, "z": z, "var": var, "violations": violations, "cut": first_hold,
            "sigma_next": math.sqrt(sigma2)}


def _lr_uc(x: int, big_t: int, p: float) -> float:
    """Kupiec's likelihood ratio (0·ln 0 = 0)."""
    def ll(q: float) -> float:
        return (big_t - x) * math.log(1 - q) + (x * math.log(q) if x else 0.0)
    return -2 * (ll(p) - ll(x / big_t if 0 < x < big_t else min(max(x / big_t, 1e-12), 1 - 1e-12)))


def _transitions(v: list[int]) -> tuple[int, int, int, int]:
    n = [[0, 0], [0, 0]]
    for a, b in zip(v, v[1:]):
        n[a][b] += 1
    return n[0][0], n[0][1], n[1][0], n[1][1]


def _lr_ind(n00: int, n01: int, n10: int, n11: int) -> float:
    def xlog(c: float, q: float) -> float:
        return c * math.log(q) if c else 0.0
    pi = (n01 + n11) / (n00 + n01 + n10 + n11)
    p01 = n01 / (n00 + n01) if n00 + n01 else 0.0
    p11 = n11 / (n10 + n11) if n10 + n11 else 0.0
    h0 = xlog(n00 + n10, 1 - pi) + xlog(n01 + n11, pi)
    h1 = xlog(n00, 1 - p01) + xlog(n01, p01) + xlog(n10, 1 - p11) + xlog(n11, p11)
    return max(0.0, -2 * (h0 - h1))


def _longest_run(v: list[int]) -> int:
    best = run = 0
    for x in v:
        run = run + 1 if x else 0
        best = max(best, run)
    return best


def _acf(v: list[float], k: int) -> float:
    m = sum(v) / len(v)
    den = sum((x - m) ** 2 for x in v)
    return sum((v[i] - m) * (v[i + k] - m) for i in range(len(v) - k)) / den


def build(model: str, variant: str, params: dict[str, Any], spec: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
    if model == "risk_simulation" and variant == "arima":
        return _build_arima_risk(model, variant, params, spec, checked)
    t = Table()
    sec = spec["sections"]
    risk = model == "risk_simulation"
    prices = checked["input_kind"] == "prices"
    n_total = 764 if prices else 763
    n, holdout, horizon = 763, checked["holdout"], checked["horizon"]
    s = _series(n, holdout)
    cut, big_t = s["cut"], holdout
    start = (date(2023, 7, 4) - EPOCH).days  # the first return is the second price's: 4 Jul 2023
    days = [start + i + (i // 5) * 2 for i in range(n)]  # trading days: Monday to Friday
    alpha, level = checked["alpha"], checked["var_level"]
    lb_lag = min(10, n // 5) + 3
    arch_lags = 12
    notes: list[tuple[str, str | None, str]] = []

    def say(block: str, level_2: str | None, id_: str, **values: Any) -> None:
        notes.append((block, level_2, note(id_, **values)))

    name = "GJR-GARCH(1,1), skewed t"
    nu, xi = 5.8, 0.93
    half_life = math.log(0.5) / math.log(s["persistence"])
    # ── the model ───────────────────────────────────────────────────────────────────────────────────────────────────────
    texts = {
        "model": model, "variant": variant, "formula": "r = μ + ε, σ² = ω + (α₁ + γ₁·I(ε₋₁ < 0))·ε²₋₁ + β₁·σ²₋₁, z ~ skewed t",
        "software": "R 4.6.0; rugarch 1.5-6; Rsolnp 2.0.1; FinTS 0.4-9", "group_version": "1", "rows_from": "trial",
        "note_1": "A placeholder note: this table is a fixture.", "model_name": name, "variance_model": checked["variance_model"],
        "distribution": checked["distribution"], "input_kind": checked["input_kind"], "returns_type": checked.get("returns_type", "log"),
        "se_type": checked["se_type"], "frequency": checked["frequency"], "time_kind": "date", "solver": "solnp",
        "risk_variable": "the cumulative return", "tail": checked.get("tail", "lower"),
        "innovations": checked.get("innovations", "parametric"), "aggregate": "relative_change",
    }
    numbers = {
        "n": n, "n_missing": 0, "first": days[0], "last": days[-1], "alpha": alpha, "scale": 0.0152, "holdout": holdout,
        "horizon": horizon, "var_level": level, "log_lik": 2410.5, "aic": -4806.9, "bic": -4775.2, "persistence": s["persistence"],
        "half_life": half_life, "half_life_time": half_life, "uncond_sigma": s["uncond"], "sigma_last": s["sigma"][-1],
        "sigma_next": s["sigma_next"], "lb_lag_used": lb_lag, "arch_lags_used": arch_lags, "kappa": s["kappa"],
        "paths": checked.get("paths"), "seed": checked["seed"], "seed_block": 1000, "batches": 1, "batch_paths": 10_000, "bins": 1024,
        "es_level": checked.get("es_level"), "threshold": checked.get("threshold"), "position": checked.get("position"),
        "last_value": 298.1, "simulated_values": (checked.get("paths") or 0) * horizon, "paths_bytes": 2_900_000,
        "seconds_simulation": 0.4, "seconds_pass_2": 0.2,
    }
    for stat in sec["model"]["texts"]:
        t.add("model", "note_1" if stat == "note_*" else stat, text=note_text(texts, stat))
    for stat in sec["model"]["numbers"]:
        if numbers.get(stat) is not None:
            t.add("model", stat, numbers[stat])

    # ── the coefficients (the series' units) ────────────────────────────────────────────────────────────────────────────
    coefs = [("mu", s["mu"], 0.00021), ("omega", s["omega"], 8.1e-7), ("alpha1", s["alpha"], 0.011), ("beta1", s["beta"], 0.021),
             ("gamma1", s["gamma"], 0.034), ("shape", nu, 1.3), ("skew", xi, 0.04)]
    for term, est, se in coefs:
        z_stat = est / se
        p = math.erfc(abs(z_stat) / math.sqrt(2))
        for stat, value in (("estimate", est), ("std_error", se), ("statistic", z_stat), ("p_value", p),
                            ("ci_low", est - 1.96 * se), ("ci_high", est + 1.96 * se)):
            t.add("coef", stat, value, level=term)
        t.add("coef", "significance", text=_stars(p), level=term)

    # ── the checks ──────────────────────────────────────────────────────────────────────────────────────────────────────
    def test(check: str, level_2: str | None, h0: str, statistic: float, df: int | None, p: float, advice: str, lag: int | None = None,
             p_adjusted: float | None = None) -> str:
        verdict = "ok" if p >= alpha else "failed"
        t.add("assumption", "h0", text=h0, level=check, level_2=level_2)
        t.add("assumption", "statistic", statistic, level=check, level_2=level_2)
        if df is not None:
            t.add("assumption", "df", df, level=check, level_2=level_2)
        t.add("assumption", "p_value", p, level=check, level_2=level_2)
        if p_adjusted is not None:
            t.add("assumption", "p_adjusted", p_adjusted, level=check, level_2=level_2)
        t.add("assumption", "significance", text=_stars(p), level=check, level_2=level_2)
        t.add("assumption", "verdict", text=verdict, level=check, level_2=level_2)
        if lag is not None:
            t.add("assumption", "lag", lag, level=check, level_2=level_2)
        if verdict == "failed":
            t.add("assumption", "advice", text=advice, level=check, level_2=level_2)
            t.add("assumption", "advice_id", text=check + "_failed", level=check, level_2=level_2)
        return verdict

    z = s["z"][:cut]
    q_z = n * (n + 2) * sum((lb_lag - k + 1) / lb_lag * _acf(z, k) ** 2 / (n - k) for k in range(1, lb_lag + 1))
    z2 = [x * x for x in z]
    q_z2 = n * (n + 2) * sum((lb_lag - k + 1) / lb_lag * _acf(z2, k) ** 2 / (n - k) for k in range(1, lb_lag + 1))
    p_z, p_z2 = _chi2_sf(q_z, lb_lag - 1), _chi2_sf(q_z2, lb_lag - 4)
    test("ljung_box_z", None, f"the standardized residuals are not autocorrelated up to lag {lb_lag}", q_z, lb_lag - 1, p_z,
         "structure left in the mean: raise the AR or MA order", lag=lb_lag)
    test("ljung_box_z2", None, f"the squared standardized residuals are not autocorrelated up to lag {lb_lag}", q_z2, lb_lag - 4, p_z2,
         "volatility the model misses: raise an order, or try GJR or EGARCH", lag=lb_lag)
    lm = 11.3
    p_lm = _chi2_sf(lm, arch_lags)
    test("arch_lm", None, "there is no ARCH effect left in the standardized residuals", lm, arch_lags, p_lm,
         "volatility the model misses: raise an order, or try GJR or EGARCH", lag=arch_lags)
    p_ny = 0.21
    test("nyblom", None, "every parameter is constant over the sample", 1.84, None, p_ny,
         "the parameters move over the sample: fit a shorter, more recent period, or read the parameters named below")
    for j, (term, _, _) in enumerate(coefs):
        p_i = [0.62, 0.48, 0.33, 0.27, 0.71, 0.55, 0.40][j]
        test("nyblom", term, f"{term} is constant over the sample", 0.12 + 0.07 * j, None, p_i,
             "the parameters move over the sample: fit a shorter, more recent period, or read the parameters named below",
             p_adjusted=min(1.0, p_i * (7 - j)))
    p_sb = 0.37
    test("sign_bias", None, "the volatility responds alike to falls and rises", 3.1, 3, p_sb,
         "the asymmetry left is not captured by the model's terms")
    t.add("assumption", "value", 0.0, level="at_bound")
    t.add("assumption", "count", 0, level="at_bound")
    t.add("assumption", "value", s["persistence"], level="variance_stationary")
    t.add("assumption", "count", 1, level="variance_stationary")
    if not risk:
        say("summary", None, "summary_holdout", model=name, n=n, series=checked["value"], first=float(days[0]), last=float(days[-1]), holdout=holdout)
    say("persistence", None, "persistence", persistence=s["persistence"], half_life=half_life, sigma_now=s["sigma_next"], uncond=s["uncond"])
    say("persistence", None, "sigma_now_above" if s["sigma_next"] > s["uncond"] else "sigma_now_below", sigma=s["sigma_next"], uncond=s["uncond"])
    say("asymmetry", None, "asymmetry_gjr", gamma=s["gamma"], ratio=(s["alpha"] + s["gamma"]) / s["alpha"], size=0.02)
    say("asymmetry", None, "sign_bias_not_supported" if p_sb >= alpha else "sign_bias_supported", p_value=p_sb, alpha=alpha)
    say("distribution", None, "distribution_sstd", shape=nu, low=4.2, high=8.9, skew=xi)
    say("residuals", None, "lb_z_ok" if p_z >= alpha else "lb_z_failed", lag=lb_lag, p_value=p_z, alpha=alpha)
    say("residuals", None, "lb_z2_ok" if p_z2 >= alpha else "lb_z2_failed", lag=lb_lag, p_value=p_z2, alpha=alpha)
    say("residuals", None, "arch_lm_ok" if p_lm >= alpha else "arch_lm_failed", lag=arch_lags, p_value=p_lm, alpha=alpha)
    say("stability", None, "nyblom_stable" if p_ny >= alpha else "nyblom_moves", p_value=p_ny, alpha=alpha)

    # ── the backtest of the 1-step VaR on the holdout ───────────────────────────────────────────────────────────────────
    hold = s["violations"][cut:]
    x = sum(hold)
    p_exp = 1 - level
    n00, n01, n10, n11 = _transitions(hold)
    lr_uc, lr_ind = _lr_uc(x, big_t, p_exp), _lr_ind(n00, n01, n10, n11)
    lr_uc_1 = _lr_uc(n01 + n11, big_t - 1, p_exp) if big_t > 1 else 0.0
    rows = [("kupiec", f"the violation rate is {p_exp:g}", lr_uc, 1, _chi2_sf(lr_uc, 1)),
            ("christoffersen_ind", "a violation today does not change tomorrow's chance", lr_ind, 1, _chi2_sf(lr_ind, 1)),
            ("christoffersen_cc", "the rate is right and the violations do not cluster", lr_uc_1 + lr_ind, 2, _chi2_sf(lr_uc_1 + lr_ind, 2))]
    verdicts = {}
    for check, h0, stat, df, p in rows:
        verdicts[check] = "ok" if p >= alpha else "failed"
        t.add("backtest", "h0", text=h0, level=check)
        t.add("backtest", "statistic", stat, level=check)
        t.add("backtest", "df", df, level=check)
        t.add("backtest", "p_value", p, level=check)
        t.add("backtest", "significance", text=_stars(p), level=check)
        t.add("backtest", "verdict", text=verdicts[check], level=check)
    for stat, value in (("violations", x), ("expected_violations", big_t * p_exp), ("violation_rate", x / big_t), ("n00", n00), ("n01", n01),
                        ("n10", n10), ("n11", n11), ("longest_run", _longest_run(hold))):
        t.add("backtest", stat, value, level="counts")
    say("backtest", None, "backtest_coverage_ok" if rows[0][4] >= alpha else "backtest_coverage_failed", holdout=big_t, level=level,
        violations=x, expected=big_t * p_exp, p_value=rows[0][4], alpha=alpha)
    say("backtest", None, "backtest_independence_ok" if rows[1][4] >= alpha else "backtest_independence_failed", p_value=rows[1][4], alpha=alpha)
    say("backtest", None, "backtest_expected", expected=big_t * p_exp)

    # ── the charts' lines ───────────────────────────────────────────────────────────────────────────────────────────────
    if "curve" in sec and not risk:
        _garch_charts(t, s, days, cut, n, horizon, alpha, coefs)
    if "lag" in sec:
        for chart, v in (("z_acf", z), ("z2_acf", z2)):
            for k in range(1, 16):
                t.add("lag", "acf", _acf(v, k), level=chart, x0=k, index=k)
            t.add("lag", "bound", 1.96 / math.sqrt(n), level=chart)
    if "hist" in sec and not risk:
        edges = [-4 + 0.4 * i for i in range(21)]
        for i in range(20):
            count = sum(1 for v in z if edges[i] <= v < edges[i + 1])
            t.add("hist", "count", count, level="std_residual", x0=edges[i], x1=edges[i + 1], index=i)
    if "point" in sec:
        for level_name, values in (("std_residual", z), ("violation", [float(v) for v in s["violations"][-200:]])):
            for i, v in enumerate(values[-200:]):
                t.add("point", "y", v, level=level_name, x0=float(days[len(days) - len(values[-200:]) + i]), index=i)
        for i in range(60):
            q = (i + 0.5) / 60
            t.add("point", "y", sorted(z)[int(q * len(z))], level="qq", x0=_probit(q), index=i)
    if risk:
        _risk_rows(t, say, s, checked, days, n, alpha, horizon, level, holdout, name)
    # the method's and the formulas' readings
    say("method", None, "method_garch" if not risk else "method_risk")
    say("formulas", None, "formula_garch" if not risk else "formula_risk")
    counters: dict[tuple[str, str | None], int] = {}
    for block, level_2, text in notes:
        counters[(block, level_2)] = counters.get((block, level_2), 0) + 1
        if block in sec["reading"]["levels"]:
            t.add("reading", f"line_{counters[(block, level_2)]}", text=text, level=block, level_2=level_2)
    return {"model": model, "variant": variant, "params": params, "rows": t.rows}


def note_text(texts: dict[str, str], stat: str) -> str:
    """The text a `model` stat says: `note_*` is the first note."""
    return texts["note_1"] if stat == "note_*" else texts.get(stat, stat)


def _probit(p: float) -> float:
    """The normal quantile of p, by bisection of the error function (a placeholder: a chart's x)."""
    lo, hi = -8.0, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if 0.5 * math.erfc(-mid / math.sqrt(2)) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _garch_charts(t: Table, s: dict[str, Any], days: list[int], cut: int, n: int, horizon: int, alpha: float,
                  coefs: list[tuple[str, float, float]]) -> None:
    """The GARCH node's own curves: the returns with their band, σ with its forecast and long-run level, the news impact curve, the
    backtest's VaR and the fitted density, on the last 200 days."""
    shown = range(n - 200, n)
    zq = 1.959964
    for i, k in enumerate(shown):
        x = float(days[k])
        t.add("curve", "y", s["r"][k], level="returns", level_2="return", x0=x, index=i)
        t.add("curve", "y", s["mu"] - zq * s["sigma"][k], level="returns", level_2="band_low", x0=x, index=i)
        t.add("curve", "y", s["mu"] + zq * s["sigma"][k], level="returns", level_2="band_high", x0=x, index=i)
        t.add("curve", "y", s["sigma"][k], level="sigma", level_2="sigma", x0=x, index=i)
        t.add("curve", "y", s["uncond"], level="sigma", level_2="uncond", x0=x, index=i)
        if k >= cut:
            j = k - cut
            t.add("curve", "y", s["r"][k], level="backtest", level_2="return", x0=x, index=j)
            t.add("curve", "y", s["var"][k], level="backtest", level_2="var", x0=x, index=j)
    sigma_k, last = s["sigma_next"], float(days[-1])
    path = []
    s2 = sigma_k ** 2
    for h in range(horizon):
        path.append(math.sqrt(s2))
        s2 = s["uncond"] ** 2 + s["persistence"] * (s2 - s["uncond"] ** 2)
    for h, v in enumerate(path):
        t.add("curve", "y", v, level="sigma", level_2="forecast", x0=last + h + 1, index=200 + h)
    for i in range(101):
        eps = (-4 + 0.08 * i) * s["uncond"]
        t.add("curve", "y", s["omega"] + (s["alpha"] + (s["gamma"] if eps < 0 else 0)) * eps * eps + s["beta"] * s["uncond"] ** 2,
              level="news_impact", level_2="sigma2", x0=eps / s["uncond"], index=i)
    for i in range(101):
        v = -4 + 0.08 * i
        t.add("curve", "y", math.exp(-v * v / 2) / math.sqrt(2 * math.pi), level="density", level_2="density", x0=v, index=i)


def _risk_rows(t: Table, say, s: dict[str, Any], checked: dict[str, Any], days: list[int], n: int, alpha: float, horizon: int,
               level: float, holdout: int, name: str) -> None:
    """The risk section, the fan and the histograms of the GARCH variant: 10,000 imaginary paths of ten days."""
    paths, es_level = checked["paths"], checked["es_level"]
    scale = s["sigma_next"] * math.sqrt(horizon)
    q_var = _Z_LOW * 0.93 * scale
    es = q_var * 1.22
    p_1 = 1 - level
    se_mc = scale * math.sqrt(p_1 * (1 - p_1) / paths) / 0.0143
    t.add("risk", "value", q_var, level="var")
    t.add("risk", "low", q_var - 1.96 * se_mc, level="var")
    t.add("risk", "high", q_var + 1.96 * se_mc, level="var")
    t.add("risk", "value", es, level="es")
    t.add("risk", "low", es - 1.96 * se_mc * 0.8, level="es")
    t.add("risk", "high", es + 1.96 * se_mc * 0.8, level="es")
    t.add("risk", "se", se_mc * 0.8, level="es")
    t.add("risk", "count", paths * (1 - es_level), level="es")
    cross = 0.073
    for stat, v in (("value", cross), ("low", 0.068), ("high", 0.078), ("count", paths * cross)):
        t.add("risk", stat, v, level="prob_crossed")
    for stat, v in (("value", 0.049), ("low", 0.0449), ("high", 0.0534), ("count", paths * 0.049)):
        t.add("risk", stat, v, level="prob_beyond")
    for stat, v in (("value", 0.021), ("median", 0.021), ("low", 0.0205), ("high", 0.0215)):
        t.add("risk", stat, v, level="drawdown")
    for stat, v in (("mean", 0.0031), ("sd", scale), ("skewness", -0.4), ("kurtosis", 2.1)):
        t.add("risk", stat, v, level="moments")
    for h in (1, horizon):
        t.add("risk", "value", _Z_LOW * 0.93 * s["sigma_next"] * math.sqrt(h), level="var", level_2=str(h))
        t.add("risk", "value", _Z_LOW * 0.93 * s["sigma_next"] * math.sqrt(h) * 1.22, level="es", level_2=str(h))
    last = float(days[-1])
    qs = [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
    zs = {0.01: -2.33, 0.05: -1.645, 0.25: -0.674, 0.5: 0.0, 0.75: 0.674, 0.95: 1.645, 0.99: 2.33}
    for h in range(1, horizon + 1):
        sd_h = s["sigma_next"] * math.sqrt(h)
        for q in qs:
            t.add("curve", "y", zs[q] * sd_h, level="fan", level_2=f"quantile_{q:g}", x0=last + h, index=h - 1)
        t.add("curve", "band_error", 4.0e-6 * math.sqrt(h), level="fan", level_2="band_error", x0=last + h, index=h - 1)
        for i in range(1, 4):
            t.add("curve", "y", 0.4 * sd_h * _noise(h * 31 + i * 17) * 4, level="fan", level_2=f"path_{i}", x0=last + h, index=h - 1)
        t.add("curve", "y", _Z_LOW * 0.93 * sd_h, level="term", level_2="var", x0=last + h, index=h - 1)
        t.add("curve", "y", _Z_LOW * 0.93 * sd_h * 1.22, level="term", level_2="es", x0=last + h, index=h - 1)
        t.add("curve", "y", min(1.0, 0.0075 * h), level="term", level_2="prob_crossed", x0=last + h, index=h - 1)
    for k in range(n - 100, n):
        t.add("curve", "y", s["r"][k], level="fan", level_2="history", x0=float(days[k]), index=1000 + k - (n - 100))
    for k in range(n - holdout, n):
        j = k - (n - holdout)
        if j < 120:
            t.add("curve", "y", s["r"][k], level="backtest", level_2="return", x0=float(days[k]), index=j)
            t.add("curve", "y", s["var"][k], level="backtest", level_2="var", x0=float(days[k]), index=j)
    for chart, width in (("horizon", scale), ("drawdown", 0.04)):
        for i in range(60):
            x0 = (-4 + 8 * i / 60) * width if chart == "horizon" else width * i / 6
            x1 = (-4 + 8 * (i + 1) / 60) * width if chart == "horizon" else width * (i + 1) / 6
            mid = (-4 + 8 * (i + 0.5) / 60)
            count = paths * 0.133 * math.exp(-mid * mid / 2) / 7.5 if chart == "horizon" else paths * 0.1 * math.exp(-i / 9)
            t.add("hist", "count", round(count), level=chart, x0=x0, x1=x1, index=i)
    price = 298.1
    say("summary", None, "summary_paths", paths=paths, steps=horizon, model=name, series=checked["value"], seed=checked["seed"])
    say("var", None, "var_loss", level=level, steps=horizon, value=q_var, low=q_var - 1.96 * se_mc, high=q_var + 1.96 * se_mc, paths=paths,
        money=-q_var * float(checked["position"] or 0.0))
    say("es", None, "es_loss", level=es_level, quantile=q_var * 0.8, value=es)
    say("threshold", None, "threshold_crossed", threshold=checked["threshold"], steps=horizon, prob_crossed=cross, low=0.068, high=0.078,
        prob_beyond=0.049, last=price)
    say("drawdown", None, "drawdown_half", half=0.021, tail=0.087, steps=horizon)
    say("shape", None, "shape_skew_kurtosis", skewness=-0.4, kurtosis=2.1, steps=horizon)
    say("fan", None, "fan_bands", error=4.0e-6)
    say("model", None, "model_checks", p_z2=0.43, p_nyblom=0.21, alpha=alpha)


def _build_arima_risk(model: str, variant: str, params: dict[str, Any], spec: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
    """The risk node's ARIMA variant: next month's orders, a running total on the upper tail, a SARIMA(1,0,1)(0,1,1)[7]."""
    t = Table()
    sec = spec["sections"]
    n, horizon, paths = 365, checked["horizon"], checked["paths"]
    alpha, level, es_level = checked["alpha"], checked["var_level"], checked["es_level"]
    start = (date(2025, 7, 1) - EPOCH).days
    days = [start + i for i in range(n)]
    y = [40 + 8 * math.sin(2 * math.pi * i / 7) + 0.02 * i + 5 * _noise(i) for i in range(n)]
    total_mean = sum(y[-7:]) / 7 * horizon
    sd_total = 4.1 * math.sqrt(horizon) * 2.2
    notes: list[tuple[str, str | None, str]] = []

    def say(block: str, level_2: str | None, id_: str, **values: Any) -> None:
        notes.append((block, level_2, note(id_, **values)))

    texts = {"model": model, "variant": variant, "formula": "(1 − φ₁B)(1 − B⁷)y = (1 + θ₁B)(1 + Θ₁B⁷)ε",
             "software": "R 4.6.0; forecast 9.0.2", "group_version": "1", "rows_from": "trial",
             "note_1": "A placeholder note: this table is a fixture.", "model_name": "SARIMA(1,0,1)(0,1,1)[7]", "frequency": "day",
             "transform": "none", "time_kind": "date", "risk_variable": "the running total", "tail": "upper",
             "innovations": "parametric", "aggregate": "sum", "criterion": "aicc"}
    numbers = {"n": n, "n_missing": 0, "p": 1, "d": 0, "q": 1, "P": 0, "D": 1, "Q": 1, "s": 7, "log_lik": -1032.4, "aic": 2072.8,
               "aicc": 2073.0, "bic": 2092.1, "sigma2": 16.8, "rmse": 4.1, "mae": 3.2, "mape": 8.9, "mase": 0.78, "holdout": 0,
               "horizon": horizon, "paths": paths, "seed": checked["seed"], "seed_block": 1000, "batches": 1, "batch_paths": 10_000,
               "bins": 1024, "var_level": level, "es_level": es_level, "threshold": checked["threshold"], "last_value": y[-1],
               "simulated_values": paths * horizon, "paths_bytes": 8_700_000, "seconds_simulation": 0.9, "seconds_pass_2": 0.5,
               "ar_root_modulus_1": 1.31, "ma_root_modulus_1": 1.22}
    for stat in sec["model"]["texts"]:
        if stat == "note_*":
            t.add("model", "note_1", text=texts["note_1"])
        elif stat in texts:
            t.add("model", stat, text=texts[stat])
    for stat in sec["model"]["numbers"]:
        if stat in numbers:
            t.add("model", stat, numbers[stat])
    for term, est, se in (("ar1", 0.62, 0.07), ("ma1", -0.41, 0.09), ("sma1", -0.83, 0.05)):
        z_stat = est / se
        p = math.erfc(abs(z_stat) / math.sqrt(2))
        for stat, v in (("estimate", est), ("std_error", se), ("statistic", z_stat), ("p_value", p), ("ci_low", est - 1.96 * se),
                        ("ci_high", est + 1.96 * se)):
            t.add("coef", stat, v, level=term)
        t.add("coef", "significance", text=_stars(p), level=term)
    for check, h0, stat, df, p in (("ljung_box_10", "the residuals are not autocorrelated up to lag 10", 9.8, 7, 0.20),
                                   ("ljung_box_2s", "the residuals are not autocorrelated up to lag 14", 14.2, 11, 0.22),
                                   ("arch_lm", "there is no ARCH effect in the residuals", 8.1, 7, 0.32),
                                   ("jarque_bera", "the residuals are normal", 1.9, 2, 0.39)):
        t.add("assumption", "h0", text=h0, level=check)
        t.add("assumption", "statistic", stat, level=check)
        t.add("assumption", "df", df, level=check)
        t.add("assumption", "p_value", p, level=check)
        t.add("assumption", "significance", text=_stars(p), level=check)
        t.add("assumption", "verdict", text="ok" if p >= alpha else "failed", level=check)
    cross = 0.31
    upper = total_mean + 2.326 * sd_total
    es = total_mean + 2.7 * sd_total
    for stat, v in (("value", upper), ("low", upper - 0.03 * sd_total), ("high", upper + 0.03 * sd_total)):
        t.add("risk", stat, v, level="var")
    for stat, v in (("value", es), ("low", es - 0.04 * sd_total), ("high", es + 0.04 * sd_total), ("se", 0.02 * sd_total),
                    ("count", paths * (1 - es_level))):
        t.add("risk", stat, v, level="es")
    for stat, v in (("value", cross), ("low", 0.301), ("high", 0.319), ("count", paths * cross)):
        t.add("risk", stat, v, level="prob_crossed")
    for stat, v in (("value", cross * 0.9), ("low", 0.28), ("high", 0.30), ("count", paths * cross * 0.9)):
        t.add("risk", stat, v, level="prob_beyond")
    for stat, v in (("mean", total_mean), ("sd", sd_total), ("skewness", 0.05), ("kurtosis", 0.1)):
        t.add("risk", stat, v, level="moments")
    last = float(days[-1])
    for h in range(1, horizon + 1):
        mean_h, sd_h = total_mean * h / horizon, 4.1 * math.sqrt(h) * 2.2
        for q, zq in ((0.01, -2.33), (0.05, -1.645), (0.25, -0.674), (0.5, 0.0), (0.75, 0.674), (0.95, 1.645), (0.99, 2.33)):
            t.add("curve", "y", mean_h + zq * sd_h, level="fan", level_2=f"quantile_{q:g}", x0=last + h, index=h - 1)
        t.add("curve", "band_error", 0.002 * h, level="fan", level_2="band_error", x0=last + h, index=h - 1)
        t.add("curve", "y", mean_h + 2.326 * sd_h, level="term", level_2="var", x0=last + h, index=h - 1)
        t.add("curve", "y", mean_h + 2.7 * sd_h, level="term", level_2="es", x0=last + h, index=h - 1)
        t.add("curve", "y", min(1.0, 0.0125 * h * h / 30), level="term", level_2="prob_crossed", x0=last + h, index=h - 1)
    for i in range(60):
        mid = -4 + 8 * (i + 0.5) / 60
        t.add("hist", "count", round(paths * 0.133 * math.exp(-mid * mid / 2) / 7.5), level="horizon",
              x0=total_mean + (-4 + 8 * i / 60) * sd_total, x1=total_mean + (-4 + 8 * (i + 1) / 60) * sd_total, index=i)
    say("summary", None, "summary_paths", paths=paths, steps=horizon, model="SARIMA(1,0,1)(0,1,1)[7]", series=checked["value"], seed=checked["seed"])
    say("var", None, "var_gain", level=level, steps=horizon, value=upper, low=upper - 0.03 * sd_total, high=upper + 0.03 * sd_total, paths=paths)
    say("es", None, "es_gain", level=es_level, value=es)
    say("threshold", None, "threshold_crossed", threshold=checked["threshold"], steps=horizon, prob_crossed=cross, low=0.301, high=0.319,
        prob_beyond=cross * 0.9, last=y[-1])
    say("shape", None, "shape_skew_kurtosis", skewness=0.05, kurtosis=0.1, steps=horizon)
    say("fan", None, "fan_bands", error=0.002)
    say("model", None, "model_checks", p_ljung=0.20, alpha=alpha)
    say("method", None, "method_risk")
    say("formulas", None, "formula_risk")
    counters: dict[tuple[str, str | None], int] = {}
    for block, level_2, text in notes:
        counters[(block, level_2)] = counters.get((block, level_2), 0) + 1
        t.add("reading", f"line_{counters[(block, level_2)]}", text=text, level=block, level_2=level_2)
    return {"model": model, "variant": variant, "params": params, "rows": t.rows}
