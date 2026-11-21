"""The witness of the decomposition's STL, classical and MSTL (docs/specs/ts-decomposition.md, «Self-test, reference values,
witnesses»; lot D2): statsmodels, outside R and outside Tabularia, decomposes each case of expected.json again from the
same CSV and compares what the cases expect (the points of each curve, the seasons' peaks and amplitudes, the strengths, the
test statistics of the remainder, the robust weights). Run by hand with the versions of witness.json:

    uv venv /tmp/tsw && uv pip install --python /tmp/tsw/bin/python statsmodels==0.15.0 numpy==2.5.5 \\
        scipy==1.18.1 pandas==3.1.1
    /tmp/tsw/bin/python -I gateway-go/internal/modelgroups/groups/timeseries/reference/make_witness_decomposition.py

    classical    seasonal_decompose(model, period, two_sided=True)                               TOLERANCE["classical"]
    STL          STL(period, seasonal, trend, low_pass = nextodd(p), seasonal_deg 0, trend_deg 1, low_pass_deg 1, jumps ⌈w/10⌉,
                 robust).fit(inner_iter, outer_iter) with R's (2 and 0; robust: 1 and 15): both are ports of the same Fortran
    periodic     STL with the window 10n + 1 and the seasonal replaced by the mean of each position, as R's stl does
    MSTL         forecast::mstl's loop (the passes over the periods, the seasonal added back, one STL, subtracted) on
                 statsmodels' STL; statsmodels' own MSTL gives the same numbers without `robust` (checked below)
    Ljung–Box    acorr_ljungbox(lags=[h]) on the remainder; ADF adfuller(regression="c", maxlag=k, autolag=None); KPSS
                 kpss(regression="c", nlags=l), l = ⌊4 (n/100)^¼⌋ (tseries' short rule); Friedman, Kruskal–Wallis: SciPy on
                 the series detrended by a numpy moving average

Measured on 2026-11-21 (statsmodels 0.15.0, R 4.6.0, forecast 9.0.2): classical 2e-15, STL 6e-15, the checks on their remainders 1.6e-12; robust STL 1.1e-3 and MSTL with an odd period 4e-4 (see TOLERANCE).
Exit 1 when a number differs by more than its tolerance; the worst relative difference of each kind is printed.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.tsa.seasonal import MSTL, STL, seasonal_decompose
from statsmodels.tsa.stattools import adfuller, kpss

HERE = Path(__file__).resolve().parent
# relative, against the largest value of the series' own kind (a point near 0 is read on the series' scale, not on itself)
# `*_robust`: statsmodels' robust STL is not R's to the bit (measured below: the same loops, other robustness weights'
# arithmetic); `*_odd`: statsmodels refuses a low-pass window equal to an odd period (R's nextodd(7) is 7), so an odd period
# gets p + 2 there; `checks_loose` are the tests on a remainder that differs in those two ways. Measured 2026-11-21.
TOLERANCE = {"classical": 1e-12, "stl": 1e-12, "stl_robust": 2e-3, "mstl": 1e-12, "mstl_robust": 2e-3, "mstl_odd": 2e-3, "mstl_robust_odd": 1e-3,
             "checks": 1e-10, "checks_loose": 0.2}


def odd(w: float) -> int:
    w = int(w)
    return max(3, w + 1 if w % 2 == 0 else w)


def nextodd(x: float) -> int:
    x = int(round(x))
    return x + 1 if x % 2 == 0 else x


def stl(y: np.ndarray, p: int, s_window: int | str, t_window: int | str, robust: bool):
    """stats::stl(ts(y, frequency = p), s.window, t.window, robust): its trend, seasonal, remainder and weights."""
    n = len(y)
    periodic = s_window == "periodic"
    s = 10 * n + 1 if periodic else odd(s_window)
    t = nextodd(math.ceil(1.5 * p / (1 - 1.5 / s))) if t_window == "auto" else odd(t_window)
    low = nextodd(p) if p % 2 == 0 else p + 2
    res = STL(y, period=p, seasonal=s, trend=t, low_pass=low, seasonal_deg=0, trend_deg=1, low_pass_deg=1, robust=robust,
              seasonal_jump=math.ceil(s / 10), trend_jump=math.ceil(t / 10), low_pass_jump=math.ceil(low / 10)).fit(
        inner_iter=1 if robust else 2, outer_iter=15 if robust else 0)
    seasonal = np.asarray(res.seasonal)
    if periodic:
        pos = np.arange(n) % p
        seasonal = np.array([seasonal[pos == k].mean() for k in range(p)])[pos]
    trend = np.asarray(res.trend)
    return trend, seasonal, y - seasonal - trend, np.asarray(res.weights), t


def mstl(y: np.ndarray, periods: list[int], windows: list[int], iterate: int, robust: bool, t_window: int | str):
    seas = [np.zeros(len(y)) for _ in periods]
    deseas = y.copy()
    for _ in range(iterate):
        for i, p in enumerate(periods):
            deseas = deseas + seas[i]
            trend, seas[i], _, weights, t = stl(deseas, p, odd(windows[i]), t_window, robust)
            deseas = deseas - seas[i]
    return trend, seas, deseas - trend, weights, t


def box_cox(y: np.ndarray, lam: float) -> np.ndarray:
    return np.log(y) if lam == 0 else (y**lam - 1) / lam


def inv_box_cox(z: np.ndarray, lam: float) -> np.ndarray:
    return np.exp(z) if lam == 0 else (lam * z + 1) ** (1 / lam)


def strength(a: np.ndarray, r: np.ndarray) -> float:
    ok = ~np.isnan(a) & ~np.isnan(r)
    return max(0.0, 1 - np.var(r[ok], ddof=1) / np.var(a[ok] + r[ok], ddof=1))


def ma(y: np.ndarray, p: int) -> np.ndarray:
    w = np.full(p, 1.0 / p) if p % 2 else np.r_[0.5, np.ones(p - 1), 0.5] / p
    h = len(w) // 2
    out = np.full(len(y), np.nan)
    for t in range(h, len(y) - h):
        out[t] = float(np.dot(w, y[t - h : t + h + 1]))
    return out


def seasonality(z: np.ndarray, p: int, test: str, mult: bool) -> tuple[float, float]:
    m = ma(z, p)
    d = z / m if mult else z - m
    valid = np.flatnonzero(~np.isnan(d))
    b = len(valid) // p
    if test == "kruskal_wallis":
        res = stats.kruskal(*[d[valid][(valid % p) == k] for k in range(p)])
    else:
        block = d[valid[0] : valid[0] + b * p].reshape(b, p)
        res = stats.friedmanchisquare(*[block[:, k] for k in range(p)])
    return float(res.statistic), float(res.pvalue)


import warnings
warnings.filterwarnings("ignore")


def main() -> int:
    expected = json.loads((HERE / "expected.json").read_text(encoding="utf-8"))
    worst: dict[str, float] = {}
    failed = False

    def check(kind: str, case: str, key: str, got: float, want: float, scale: float | None = None) -> None:
        nonlocal failed
        rel = abs(got - want) / max(abs(want) if scale is None else scale, 1e-300)
        worst[kind] = max(worst.get(kind, 0.0), rel)
        if rel > TOLERANCE[kind]:
            failed = True
            print(f"  {case} {key}: {got!r} against {want!r} (relative {rel:.2e} > {TOLERANCE[kind]:g})")

    for case in expected["cases"]:
        if not case["id"].startswith("dec_") or case.get("refuses") or case["params"].get("method") == "emd":
            continue
        p = {"method": "stl", "robust": False, "s_window": 11, "t_window": "auto", "iterate": 2, "type": "additive",
             "transform": "none", "box_cox_lambda": "auto", "seasonality_test": "friedman", "lb_lag": "auto", "alpha": 0.05,
             **case["params"]}
        want = {e["key"]: e for e in case["expect"]}
        table = pd.read_csv(HERE / case["data"], dtype=str, keep_default_na=False)
        y = table.iloc[:, 1].astype(float).to_numpy()
        first_day = pd.Timestamp(table.iloc[0, 0])
        n = len(y)
        lam = {"log": 0.0}.get(p["transform"], p["box_cox_lambda"] if p["transform"] == "box_cox" else None)
        z = y if lam is None else box_cox(y, lam)
        method, mult = p["method"], p["method"] == "classical" and p["type"] == "multiplicative"
        periods = sorted(p["periods"]) if method == "mstl" else [p.get("period") or {"month": 12, "day": 7, "quarter": 4, "week": 52, "hour": 24}[p["frequency"]]]
        weights = None
        if method == "classical":
            res = seasonal_decompose(z, model=p["type"], period=periods[0], two_sided=True)
            trend, seasonal, rem = np.asarray(res.trend), [np.asarray(res.seasonal)], np.asarray(res.resid)
        elif method == "stl":
            trend, s, rem, weights, tw = stl(z, periods[0], p["s_window"], p["t_window"], p["robust"])
            seasonal = [s]
        else:
            windows = p.get("s_windows") or [7 + 4 * (i + 1) for i in range(len(periods))]
            trend, seasonal, rem, weights, tw = mstl(z, periods, windows, p["iterate"], p["robust"], p["t_window"])
        kind = method + ("_robust" if p["robust"] and method != "classical" else "") + ("_odd" if method == "mstl" and any(q % 2 for q in periods) else "")
        loose = "checks_loose" if kind != method else "checks"
        scale = float(np.nanmax(np.abs(z)))
        print(case["id"])
        # the curves' points: the node's thinning is none below 2,000 instants, so the index is the instant
        adjusted = z / seasonal[0] if mult else z - sum(seasonal)
        adjusted = inv_box_cox(adjusted, lam) if lam is not None else adjusted
        series = {"trend": trend, "remainder": rem, **{f"seasonal_{q}": seasonal[i] for i, q in enumerate(periods)}}
        for key, e in want.items():
            parts = key.split("|")
            if parts[0] == "curve":
                v = adjusted if parts[1] == "adjusted" else series[parts[2]]
                sc = float(np.nanmax(np.abs(adjusted))) if parts[1] == "adjusted" else scale
                # the index counts the kept points: a classical decomposition's empty ends are not there
                check(kind, case["id"], key, float(v[~np.isnan(v)][int(parts[4])]), e["value"], sc)
        for i, q in enumerate(periods):
            # a week is named by the calendar (1 = Monday); the other cycles count from the first instant
            at = (np.arange(n) + first_day.isoweekday() - 1) % 7 if (q == 7 and p["frequency"] == "day") else np.arange(n) % q
            shape = np.array([seasonal[i][at == k].mean() for k in range(q)])
            hi, lo = int(np.argmax(shape)), int(np.argmin(shape))
            for stat, got in (("peak_value", shape[hi]), ("trough_value", shape[lo]),
                              ("amplitude", shape[hi] / shape[lo] if mult else shape[hi] - shape[lo])):
                check(kind, case["id"], f"season {q} {stat}", float(got), want[f"season|{q}||{stat}"]["value"])
            assert hi + 1 == want[f"season|{q}||peak_position"]["value"] and lo + 1 == want[f"season|{q}||trough_position"]["value"]
            lg = np.log if mult else (lambda x: x)
            check(kind, case["id"], f"seasonal_strength {q}", strength(lg(seasonal[i]), lg(rem)), want[f"assumption|seasonal_strength|{q}|value"]["value"])
        lg = np.log if mult else (lambda x: x)
        check(kind, case["id"], "trend_strength", strength(lg(trend), lg(rem)), want["assumption|trend_strength||value"]["value"])
        if "model|||t_window_used" in want:
            assert tw == want["model|||t_window_used"]["value"], (tw, want["model|||t_window_used"])
        if weights is not None and "assumption|robust_weights||value" in want:
            assert int((weights == 0).sum()) == want["assumption|robust_weights||value"]["value"], "robust weights"
        # the checks on the remainder (statsmodels' own)
        r = rem[~np.isnan(rem)]
        nr = len(r)
        h = min(1000, min(2 * max(periods), nr // 5))
        lb = acorr_ljungbox(r, lags=[h]).iloc[0]
        check(loose, case["id"], "ljung_box", float(lb["lb_stat"]), want["assumption|ljung_box||statistic"]["value"])
        k = int(math.floor((nr - 1) ** (1 / 3)))
        tau = adfuller(r, maxlag=k, regression="c", autolag=None)[0]
        check(loose, case["id"], "adf", float(tau), want["assumption|adf||statistic"]["value"])
        lags = int(math.floor(4 * (nr / 100) ** 0.25))
        eta = kpss(r, regression="c", nlags=lags)[0]
        check(loose, case["id"], "kpss", float(eta), want["assumption|kpss||statistic"]["value"])
        for q in periods:
            key = f"assumption|seasonality|{q}|statistic"
            if key in want:
                stat, pv = seasonality(z, q, p["seasonality_test"], mult)
                check(loose, case["id"], key, stat, want[key]["value"])
    print("worst relative differences:", {k: f"{v:.2e}" for k, v in worst.items()}, "tolerances:", TOLERANCE)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
