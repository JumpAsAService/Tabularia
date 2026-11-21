"""The witnesses of the decomposition's series and checks (docs/specs/ts-decomposition.md, «Self-test, reference
values, witnesses»; lot D1): statsmodels, SciPy, pandas and numpy compute, outside R and outside Tabularia, what the
runner's ts_series.R and ts_checks.R compute, from the tables of make_reference_series.R, and write
data/series_witness.json; the group's self-test (ts_series_tests.R, ts_checks_tests.R) compares the runner with it
inside the image, at the tolerances below. Run by hand with the versions of witness.json:

    uv venv /tmp/tsw && uv pip install --python /tmp/tsw/bin/python statsmodels==0.15.0 numpy==2.5.5 \\
        scipy==1.18.1 pandas==3.1.1
    /tmp/tsw/bin/python -I gateway-go/internal/modelgroups/groups/timeseries/reference/make_witness_series.py

    Ljung–Box          acorr_ljungbox(lags=[h]): Q and p                                          1e-12
    largest ACF        statsmodels acf(nlags=h, fft=False): max |ρ̂_k| and k                      1e-12
    ADF                adfuller(regression="c", maxlag=k, autolag=None): τ and the regression's
                       observations                                                               1e-10
                       MacKinnon (2010)'s finite-sample critical values at those observations
                       (mackinnoncrit): urca::punitroot gives back about 1 %, 5 %, 10 % there    measured, said
    KPSS               kpss(regression="c", nlags=l): η and its interpolated p (the same table)  1e-10
    Friedman, KW       scipy.stats.friedmanchisquare on the blocks, scipy.stats.kruskal by
                       position, of the series detrended by a numpy moving average               1e-12
    strengths          seasonal_decompose(two_sided=True) and numpy's variances                   1e-12
    positions          pandas' calendar (isoweekday, month, hour, quarter, the first 1 January)  exact
    envelope           each bucket's lowest and highest instant, by numpy                         exact
    linear fill        pandas interpolate(method="linear"), on the values and on their logs       1e-12
    Box–Cox λ          a transcription of forecast:::guer.cv minimised by SciPy's bounded Brent
                       with optimize()'s tolerance (R's own path), and to 1e-10 (the true minimum) 1e-6; said

Exit 1 when a table is missing; nothing is compared here (the self-test compares).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import optimize, stats
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.tsa.adfvalues import mackinnoncrit, mackinnonp
from statsmodels.tsa.seasonal import seasonal_decompose
from statsmodels.tsa.stattools import acf, adfuller, kpss

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def ma(y: np.ndarray, p: int) -> np.ndarray:
    """The centred moving average of order p (2×p for even p), NaN on the first and last ⌊p/2⌋."""
    w = np.full(p, 1.0 / p) if p % 2 else np.r_[0.5, np.ones(p - 1), 0.5] / p
    h = len(w) // 2
    out = np.full(len(y), np.nan)
    for t in range(h, len(y) - h):
        out[t] = float(np.dot(w, y[t - h : t + h + 1]))
    return out


def seasonality(y: np.ndarray, p: int) -> dict:
    d = y - ma(y, p)
    valid = np.flatnonzero(~np.isnan(d))
    b = len(valid) // p
    start = int(valid[0])
    m = d[start : start + b * p].reshape(b, p)
    fr = stats.friedmanchisquare(*[m[:, j] for j in range(p)])
    groups = [d[valid][(valid % p) == j] for j in range(p)]
    kw = stats.kruskal(*groups)
    return {"period": p, "cycles": b, "friedman": [float(fr.statistic), float(fr.pvalue)],
            "kruskal_wallis": [float(kw.statistic), float(kw.pvalue)]}


def checks(x: np.ndarray) -> dict:
    n = len(x)
    out: dict = {"n": n, "ljung_box": {}, "largest_acf": {}}
    for h in (10, 14):
        lb = acorr_ljungbox(x, lags=[h])
        out["ljung_box"][str(h)] = [float(lb["lb_stat"].iloc[0]), float(lb["lb_pvalue"].iloc[0])]
        r = acf(x, nlags=h, fft=False)[1:]
        k = int(np.argmax(np.abs(r)))
        out["largest_acf"][str(h)] = [float(abs(r[k])), k + 1]
    k = math.floor((n - 1) ** (1 / 3))
    stat, p, used, nobs, crit = adfuller(x, maxlag=k, regression="c", autolag=None, result_object=False)[:5]
    out["adf"] = {"k": k, "tau": float(stat), "nobs": int(nobs), "p_statsmodels_asymptotic": float(p),
                  "crit_nobs": [float(v) for v in mackinnoncrit(N=1, regression="c", nobs=nobs)],
                  "p_mackinnon_1994_asymptotic": float(mackinnonp(stat, regression="c", N=1))}
    lags = math.floor(4 * (n / 100) ** 0.25)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        eta, kp, used_lags, _ = kpss(x, regression="c", nlags=lags)
    out["kpss"] = {"lags": int(used_lags), "eta": float(eta), "p": float(kp)}
    return out


def strengths(y: np.ndarray, p: int) -> dict:
    dec = seasonal_decompose(y, model="additive", period=p, two_sided=True)
    T, S, R = dec.trend, dec.seasonal, dec.resid
    ok = ~np.isnan(T) & ~np.isnan(R)
    ft = max(0.0, 1 - np.var(R[ok], ddof=1) / np.var(T[ok] + R[ok], ddof=1))
    fs = max(0.0, 1 - np.var(R[ok], ddof=1) / np.var(S[ok] + R[ok], ddof=1))
    return {"period": p, "trend": float(ft), "seasonal": float(fs)}


def positions() -> list[dict]:
    """The calendar's position of each instant, from the first cycle, as ts_positions numbers it."""
    cases = []

    def first_cycle(times: pd.DatetimeIndex, p: int, kind: str) -> list[int]:
        cal = {"weekday": lambda t: t.isoweekday(), "month": lambda t: t.month, "hour": lambda t: t.hour + 1,
               "weekday_hour": lambda t: (t.isoweekday() - 1) * 24 + t.hour + 1, "quarter": lambda t: t.quarter}[kind]
        first = [cal(t) for t in times[:p]]
        return [first[i % p] for i in range(len(times))]

    for name, start, freq, n, p, kind, tz in [
        ("daily_7", "2024-01-03", "D", 20, 7, "weekday", None),
        ("monthly_12", "2023-11-01", "MS", 30, 12, "month", None),
        ("hourly_24_rome", "2024-03-29 23:00", "h", 60, 24, "hour", "Europe/Rome"),
        ("hourly_168", "2024-03-27 05:00", "h", 400, 168, "weekday_hour", None),
        ("quarterly_4", "2023-07-01", "QS", 10, 4, "quarter", None),
    ]:
        times = pd.date_range(start, periods=n, freq=freq, tz="UTC" if tz else None)
        local = times.tz_convert(tz) if tz else times
        cases.append({"name": name, "start": start, "frequency": {"D": "day", "MS": "month", "h": "hour", "QS": "quarter"}[freq],
                      "n": n, "period": p, "tz": tz, "kind": kind, "position": first_cycle(local, p, kind)})
    # a day of the year: position 1 is the place of the first 1 January, steps from it
    times = pd.date_range("2023-07-01", periods=800, freq="D")
    jan1 = next(i for i in range(365) if times[i].day_of_year == 1)
    cases.append({"name": "daily_365", "start": "2023-07-01", "frequency": "day", "n": 800, "period": 365, "tz": None,
                  "kind": "yearday", "position": [((i % 365) - jan1) % 365 + 1 for i in range(800)]})
    # a cycle the calendar does not name: steps
    cases.append({"name": "daily_30", "start": "2023-07-01", "frequency": "day", "n": 70, "period": 30, "tz": None,
                  "kind": "step", "position": [i % 30 + 1 for i in range(70)]})
    return cases


def envelope(y: np.ndarray, most: int) -> list[int]:
    n = len(y)
    if n <= most:
        return [i + 1 for i in range(n) if not np.isnan(y[i])]
    buckets = most // 2
    keep: set[int] = set()
    for b in range(buckets):
        idx = [i for i in range(n) if (i * buckets) // n == b and not np.isnan(y[i])]
        if idx:
            vals = y[idx]
            keep.add(idx[int(np.argmin(vals))] + 1)
            keep.add(idx[int(np.argmax(vals))] + 1)
    return sorted(keep)


def gaps() -> dict:
    df = pd.read_csv(DATA / "dec_gaps.csv", dtype={"month": str})
    df["month"] = pd.to_datetime(df["month"])
    grid = pd.date_range("2015-01-01", "2024-12-01", freq="MS")
    y = df.set_index("month")["boxes"].reindex(grid)
    first, last = y.first_valid_index(), y.last_valid_index()
    span = y.loc[first:last]
    gaps_at = [i + 1 for i, v in enumerate(span.to_numpy()) if np.isnan(v)]
    linear = span.interpolate(method="linear").to_numpy()
    log_linear = np.log(span).interpolate(method="linear").to_numpy()
    x = span.to_numpy()

    def guer_cv(lam: float, period: int = 12) -> float:
        nobsf = len(x)
        nyr = nobsf // period
        nobst = nyr * period
        mat = x[nobsf - nobst :].reshape(nyr, period)  # R's matrix(x, period, nyr): one column a year
        mean = np.nanmean(mat, axis=1)
        sd = np.nanstd(mat, axis=1, ddof=1)
        rat = sd / mean ** (1 - lam)
        return float(np.nanstd(rat, ddof=1) / np.nanmean(rat))

    r_tol = np.finfo(float).eps ** 0.25
    lam_r_path = optimize.minimize_scalar(guer_cv, bounds=(-1, 2), method="bounded", options={"xatol": r_tol}).x
    lam_tight = optimize.minimize_scalar(guer_cv, bounds=(-1, 2), method="bounded", options={"xatol": 1e-10}).x
    return {"first": str(first.date()), "n": len(span), "gaps": gaps_at, "n_missing": int(y.loc[:first].isna().sum() + y.loc[last:].isna().sum()), "linear": [float(linear[i - 1]) for i in gaps_at],
            "log_linear": [float(log_linear[i - 1]) for i in gaps_at], "lambda_r_path": float(lam_r_path),
            "lambda_tight": float(lam_tight), "cv_tight": guer_cv(float(lam_tight))}


def main() -> int:
    checks_csv = DATA / "dec_checks.csv"
    if not checks_csv.exists() or not (DATA / "dec_gaps.csv").exists():
        print("run make_reference_series.R first", file=sys.stderr)
        return 1
    df = pd.read_csv(checks_csv)
    out: dict = {"generated": {"by": "make_witness_series.py", "statsmodels": __import__("statsmodels").__version__,
                               "scipy": __import__("scipy").__version__, "numpy": np.__version__, "pandas": pd.__version__},
                 "checks": {}, "seasonality": {}, "strengths": {}}
    for col in ["white", "ar", "walk", "weekly", "noseason", "mild"]:
        out["checks"][col] = checks(df[col].to_numpy(float))
    for col, p in [("weekly", 7), ("noseason", 7), ("white", 7), ("noseason", 12), ("weekly", 12)]:
        out["seasonality"][f"{col}_{p}"] = seasonality(df[col].to_numpy(float), p)
    out["strengths"]["weekly_7"] = strengths(df["weekly"].to_numpy(float), 7)
    out["positions"] = positions()
    walk = df["walk"].to_numpy(float)
    holed = walk.copy()
    holed[10:16] = np.nan
    out["envelope"] = {"walk_100": envelope(walk, 100), "walk_holed_100": envelope(holed, 100), "walk_401": envelope(walk, 401)}
    out["gaps"] = gaps()
    (DATA / "series_witness.json").write_text(json.dumps(out, separators=(",", ":")) + "\n", encoding="utf-8")
    print("written data/series_witness.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
