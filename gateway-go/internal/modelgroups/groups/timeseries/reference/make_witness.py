"""The second witness of the ARIMA reference values: statsmodels' SARIMAX, outside Tabularia
(docs/specs/statistical-models.md, «Reference values»: 1e-4 relative on the coefficients).

    python make_witness.py          # in an environment with the versions of witness.json

It fits each case of expected.json again on the same CSV, as a regression with SARIMA
errors (a column of ones for R's intercept, 1, 2, 3… for its drift, the Box–Cox λ that R
estimated), compares every coefficient with R's and exits 1 when one differs by more than
the tolerance. The two estimators initialize the likelihood differently (R: a diffuse prior
of variance 1e6 on the differenced states; statsmodels: the differenced series), so the
agreement is close, not exact: said, not hidden. Measured on 2026-11-20 (statsmodels 0.15.0,
forecast 9.0.2): every coefficient within 2.5e-5 except the seasonal case's ar1 (4.2e-4) and
ma1 (6.2e-4), where the two optima differ by 0.002 standard errors; R's optimizer with a
tighter tolerance or another initial covariance (SSinit) lands on the same point, so it is
the likelihoods that differ, not the convergence.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from statsmodels.tsa.statespace.sarimax import SARIMAX

HERE = Path(__file__).resolve().parent
TOLERANCE = 1e-4


def box_cox(x: np.ndarray, lam: float) -> np.ndarray:
    return np.log(x) if lam == 0 else (np.sign(x) * np.abs(x) ** lam - 1) / lam


def series(case: dict) -> tuple[np.ndarray, np.ndarray | None, list[str], tuple, tuple]:
    """The endogenous series on its grid, the regressors, their names as R names them, the orders."""
    p = case["params"]
    df = pd.read_csv(HERE / case["data"], dtype=str, keep_default_na=False)
    num = lambda c: pd.to_numeric(df[c].replace("", np.nan))  # noqa: E731
    order = (p.get("p", 1), p.get("d", 0), p.get("q", 0))
    s = p.get("s") or {"month": 12, "quarter": 4, "week": 52, "index": 1}[p["frequency"]]
    seasonal = (p.get("P", 0), p.get("D", 0), p.get("Q", 0), s if s > 1 else 0)
    if p["frequency"] == "week":
        days = (pd.to_datetime(df[p["time"]]) - pd.to_datetime(df[p["time"]].iloc[0])).dt.days.to_numpy()
        at = days // 7
    else:
        at = np.arange(len(df))
    y = np.full(at.max() + 1, np.nan)
    y[at] = num(p["value"]).to_numpy()
    exog = p.get("exog") or []
    X = np.full((len(y), len(exog)), np.nan)
    for j, e in enumerate(exog):
        X[at, j] = num(e).to_numpy()
    last = int(np.max(np.nonzero(~np.isnan(y))))
    n = last + 1 - p.get("holdout", 0)
    y, X = y[:n], X[:n]
    gap = np.isnan(X).any(axis=1)  # a period without its regressors is a missing value (R: NA in xreg)
    y[gap], X[gap] = np.nan, 0.0
    lam = {"log": 0.0}.get(p.get("transform", "none"))
    if p.get("transform") == "box_cox":
        lam = next(e["value"] for e in case["expect"] if e["key"] == "model|||box_cox_lambda")
    if lam is not None:
        y = box_cox(y, lam)
    names, cols = [], []
    if order[1] + seasonal[1] == 0 and p.get("mean", True):
        names.append("intercept")
        cols.append(np.ones(n))
    if p.get("drift") and order[1] + seasonal[1] <= 1:
        names.append("drift")
        cols.append(np.arange(1, n + 1, dtype=float))
    for j, e in enumerate(exog):
        names.append(e)
        cols.append(X[:, j])
    return y, (np.column_stack(cols) if cols else None), names, order, seasonal


def main() -> int:
    expected = json.loads((HERE / "expected.json").read_text(encoding="utf-8"))
    worst, failed = 0.0, False
    for case in expected["cases"]:
        if case.get("refuses") or case["model"] != "arima" or case["params"].get("orders", "manual") != "manual":
            continue
        y, X, xnames, order, seasonal = series(case)
        # R's likelihood of a differenced model is the differenced series' (its diffuse prior on
        # the d + D·s first states): statsmodels' simple differencing is the same likelihood
        model = SARIMAX(y, exog=X, order=order, seasonal_order=seasonal, trend="n",
                        simple_differencing=order[1] + seasonal[1] > 0)
        res = model.fit(disp=False, method="lbfgs", maxiter=2000, pgtol=1e-12, factr=10)
        # polished from its own optimum: on a flat likelihood L-BFGS stops early
        res = model.fit(start_params=res.params, disp=False, method="bfgs", maxiter=2000, gtol=1e-10)
        params = dict(zip(res.model.param_names, res.params))
        params.update({f"x{j + 1}": v for j, v in enumerate(res.params[: len(xnames)])})
        r = {e["key"].split("|")[1]: e["value"] for e in case["expect"] if e["key"].startswith("coef|") and e["key"].endswith("|estimate")}
        mapping = {f"ar{i}": f"ar.L{i}" for i in range(1, 6)} | {f"ma{i}": f"ma.L{i}" for i in range(1, 6)}
        mapping |= {f"sar{i}": f"ar.S.L{seasonal[3] * i}" for i in range(1, 3)} | {f"sma{i}": f"ma.S.L{seasonal[3] * i}" for i in range(1, 3)}
        mapping |= {name: f"x{j + 1}" for j, name in enumerate(xnames)}
        print(case["id"])
        for term, value in r.items():
            other = params[mapping[term]]
            rel = abs(other - value) / abs(value)
            worst = max(worst, rel)
            failed |= rel > TOLERANCE
            print(f"  {term:12s} R {value: .10f}   statsmodels {other: .10f}   relative {rel:.2e}{'  > 1e-4' if rel > TOLERANCE else ''}")
    print(f"worst relative difference {worst:.2e} (tolerance {TOLERANCE:g})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
