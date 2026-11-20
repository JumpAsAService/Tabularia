"""The second witness of the group «survival» (docs/specs/statistical-models.md, «Reference
values»): lifelines, a second implementation, computes what it can of expected.json from the
same tables, OUTSIDE Tabularia, and says where the two differ by more than 1e-6 (relative).
Run by hand, with the versions of witness.json, in a scratch environment:

    uv venv /tmp/witness && uv pip install --python /tmp/witness/bin/python lifelines==0.30.3
    /tmp/witness/bin/python -I gateway-go/internal/modelgroups/groups/survival/reference/make_witness.py

Kaplan–Meier (lane M4), per case of expected.json: per stratum the survival at the times
asked, the quartiles and the median, the restricted mean, the curve and the Nelson–Aalen
cumulative hazard at the steps expected.json names; the log-rank test (Peto's: lifelines'
Fleming–Harrington with p = 1, q = 0, which is survdiff's ρ = 1) and its pairs. The
intervals only where both use the same transformation: lifelines' is log(−log) (R's
conf_type = "log-log"). Where a curve is exactly at a quartile over a step, R takes the
middle of the step and lifelines its start: the witness applies R's rule and prints both.

Cox (lane M5), per case of expected.json whose ties are Efron's (lifelines knows no other):
the coefficients, the hazard ratios and their intervals, the standard errors (the robust ones
with `cluster_col` or `robust`), the log partial likelihood, the AIC, the likelihood-ratio and
the Wald tests, the concordance, by CoxPHFitter (strata, clusters) and CoxTimeVaryingFitter
(start–stop rows).
What the two define differently is printed beside, not compared: the test of proportional
hazards (lifelines' proportional_hazard_test is the approximation R's cox.zph used before
survival 3.0, which now computes the score test exactly); the robust standard errors and what
uses them (the intervals, the Wald test) where times are tied: R's score residuals carry Efron's
correction for ties, lifelines' do not (on the same rows with the ties broken the two agree to
1e-9). lifelines' CoxTimeVaryingFitter has no robust errors: on start–stop rows the classical
ones are compared.
Parametric survival (lot P1), per case of expected.json fitted with Weibull, log-normal,
log-logistic or the generalized gamma without strata: lifelines' WeibullAFTFitter,
LogNormalAFTFitter, LogLogisticAFTFitter and GeneralizedGammaRegressionFitter on the same design
(numbers, treatment dummies, products for the interactions; case weights as weights_col): the
log-likelihood, the location's coefficients (the intercept included: log scale, meanlog, mu, the
same accelerated-failure-time scale), their standard errors, the shape (lifelines' rho_, beta_,
sigma_ on the log scale; the generalized gamma's lambda_ is flexsurv's Q). Its own optimiser: an
estimate is compared to 1e-5 relative or a thousandth of its standard error, whichever is wider.
The restricted mean's default horizon is the least of the strata's largest times (the runner's and
survRM2's); the witness first took the whole table's largest time, which reads a stratum past its own
last time (lifelines carries the last step on, R's rmean does not the same way): four values differed
by that, a definition of the witness's, not an error of the runner. Exit 1 when a value differs.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from lifelines import (CoxPHFitter, CoxTimeVaryingFitter, GeneralizedGammaRegressionFitter, KaplanMeierFitter,
                       LogLogisticAFTFitter, LogNormalAFTFitter, NelsonAalenFitter, WeibullAFTFitter)
from lifelines.exceptions import ConvergenceError
from lifelines.statistics import multivariate_logrank_test, pairwise_logrank_test, proportional_hazard_test
from lifelines.utils import restricted_mean_survival_time

HERE = Path(__file__).resolve().parent
TOL = 1e-6
PS_TOL = 1e-5      # lifelines' own optimiser for the parametric models
PS_TOL_SE = 1e-3   # or a thousandth of the estimate's standard error, for an estimate near 0
PS_GTOLS = (1e-9, 1e-7, 1e-5)  # scipy's BFGS: its default (1e-5) stops 1e-2 from a maximum; the smallest it reaches is kept


def label(t: float) -> str:
    return str(int(t)) if float(t).is_integer() else repr(float(t))


def is_event(column: pd.Series, value, kind: str) -> np.ndarray:
    if kind == "boolean":
        return (column.astype(str).str.lower() == str(value).lower()).to_numpy()
    if kind in ("number", "integer"):
        return (pd.to_numeric(column) == float(value)).to_numpy()
    return (column.astype(str) == str(value)).to_numpy()


def km_case(case: dict) -> dict[str, float]:
    """What lifelines says of the keys of a Kaplan–Meier case."""
    p, types = case["params"], case["types"]
    df = pd.read_csv(HERE / case["data"], keep_default_na=False, na_values=[""], dtype=str)
    used = [p["time"], p["event"], *([p["strata"]] if p.get("strata") else [])]
    df = df.dropna(subset=used)
    time = pd.to_numeric(df[p["time"]]).to_numpy()
    event = is_event(df[p["event"]], p["event_value"], types[p["event"]]).astype(int)
    alpha = p.get("alpha", 0.05)
    loglog = p.get("conf_type", "log") == "log-log"
    strata = sorted(df[p["strata"]].unique()) if p.get("strata") else ["all"]
    groups = df[p["strata"]].to_numpy() if p.get("strata") else np.array(["all"] * len(df))
    # the runner's default horizon, and survRM2's: the least of the strata's largest times, so that every
    # stratum's mean is read inside its own follow-up (the whole table's largest time would take the
    # restricted mean of a stratum past its last time, which lifelines then extends with the last step)
    tau = p.get("rmst_tau", float(min(time[groups == s].max() for s in strata)))
    out: dict[str, float] = {}
    for s in strata:
        name = f"{p['strata']}={s}" if p.get("strata") else "all"
        m = groups == s
        kmf = KaplanMeierFitter(alpha=alpha).fit(time[m], event[m])
        key = f"stratum|{name}||"
        sf = kmf.survival_function_.iloc[:, 0]
        ci = kmf.confidence_interval_survival_function_
        for t in p.get("times", []):
            at = kmf.survival_function_at_times(t).iloc[0]
            past = t > time[m].max() and at > 0
            out[key + f"surv_at_{label(t)}"] = math.nan if past else float(at)
            if loglog and not past:
                row = ci[ci.index <= t].iloc[-1]
                out[key + f"surv_at_{label(t)}_low"] = float(row.iloc[0])
                out[key + f"surv_at_{label(t)}_high"] = float(row.iloc[1])
        for stat, q in (("q25", 0.75), ("median", 0.5), ("q75", 0.25)):
            # lifelines: the first time the curve is at or below q. Where the curve is exactly q,
            # R takes the middle between that time and the next step down (quantile.survfit).
            below = sf[sf <= q + 1e-12]
            first = float(below.index[0]) if len(below) else math.nan
            if len(below) and abs(below.iloc[0] - q) < 1e-12:
                later = sf.index[sf < q - 1e-12]  # where the curve next steps down
                out[f"{key}{stat} (as lifelines)"] = first
                first = (first + float(later[0])) / 2 if len(later) else first
            out[key + stat] = first
        out[key + "rmst"] = float(restricted_mean_survival_time(kmf, t=tau))
        # the curve's and the cumulative hazard's steps: every distinct time, from 0
        steps = sf.index.to_numpy()
        naf = NelsonAalenFitter(nelson_aalen_smoothing=False).fit(time[m], event[m])
        ch = naf.cumulative_hazard_.iloc[:, 0]
        for i, t in enumerate(steps):
            out[f"curve|km|{name}|surv|{i}"] = float(sf.loc[t])
            out[f"curve|cumhaz|{name}|cumhaz|{i}"] = float(ch.loc[t])
    if len(strata) >= 2:
        peto = p.get("test") == "peto"
        kw = {"weightings": "fleming-harrington", "p": 1, "q": 0} if peto else {}
        test = multivariate_logrank_test(time, groups, event, **kw)
        out["model|||test_statistic"] = float(test.test_statistic)
        out["model|||test_p_value"] = float(test.p_value)
        if p.get("pairwise"):
            pairs = pairwise_logrank_test(time, groups, event, **kw).summary
            for (a, b), row in pairs.iterrows():
                key = f"pair|{p['strata']}={a}|{p['strata']}={b}|"
                out[key + "statistic"] = float(row["test_statistic"])
                out[key + "p_value"] = float(row["p"])
    return out


def cox_case(case: dict) -> tuple[dict[str, float], dict[str, float]]:
    """What lifelines says of the keys of a Cox case (Efron's ties only), and what it defines
    differently (said, not compared)."""
    p, types = case["params"], case["types"]
    df = pd.read_csv(HERE / case["data"], keep_default_na=False, na_values=[""], dtype=str)
    predictors = list(p["predictors"])
    used = [p["time"], p["event"], *predictors, *[p[k] for k in ("start", "strata", "cluster") if p.get(k)]]
    df = df.dropna(subset=used).reset_index(drop=True)
    frame = pd.DataFrame({"T": pd.to_numeric(df[p["time"]]),
                          "E": is_event(df[p["event"]], p["event_value"], types[p["event"]]).astype(int)})
    names: list[tuple[str, str | None, str]] = []  # (term, level, the frame's column) in the runner's order
    for name in predictors:
        kind = types[name]
        if kind in ("number", "integer"):
            frame[f"x{len(names)}"] = pd.to_numeric(df[name])
            names.append((name, None, f"x{len(names)}"))
            continue
        labels = df[name].str.lower() if kind == "boolean" else df[name]
        levels = sorted(labels.unique())
        reference = str(p.get("reference", {}).get(name, levels[0]))
        for level in [lv for lv in levels if lv != reference]:
            frame[f"x{len(names)}"] = (labels == level).astype(float)
            names.append((name, level, f"x{len(names)}"))
    robust = p.get("se") == "robust"
    alpha = p.get("alpha", 0.05)
    options = {"precision": 1e-12, "max_steps": 500}
    if p.get("start"):
        frame["S"] = pd.to_numeric(df[p["start"]])
        frame["id"] = df[p["cluster"]] if p.get("cluster") else np.arange(len(df))
        fit = CoxTimeVaryingFitter(alpha=alpha).fit(frame, event_col="E", start_col="S", stop_col="T", id_col="id",
                                                     fit_options=options)
        robust = False  # not in lifelines for start–stop rows: the classical errors are compared
    else:
        kw: dict = {}
        if p.get("strata"):
            frame["G"] = df[p["strata"]]
            kw["strata"] = ["G"]
        if p.get("cluster"):
            frame["C"] = df[p["cluster"]]
            kw["cluster_col"] = "C"
        fit = CoxPHFitter(alpha=alpha).fit(frame, duration_col="T", event_col="E", robust=robust, fit_options=options, **kw)
    # the robust errors (and the intervals and the Wald test that use them) are compared only
    # where no event time is tied; on start–stop rows lifelines has none: R's are not witnessed
    tied = bool(frame.loc[frame["E"] == 1, "T"].duplicated().any())
    out: dict[str, float] = {}
    beside: dict[str, float] = {}
    uses_robust = out if not tied else beside
    if p.get("start") and p.get("se") == "robust":
        uses_robust = {}
    by_se = uses_robust if robust or p.get("se") == "robust" else out
    se = fit.standard_errors_
    ci = fit.confidence_intervals_
    for term, level, column in names:
        key = f"coef|{term}|{level or ''}|"
        b = float(fit.params_[column])
        out[key + "estimate"] = b
        out[key + "hazard_ratio"] = math.exp(b)
        by_se[key + "hr_ci_low"] = math.exp(float(ci.loc[column].iloc[0]))
        by_se[key + "hr_ci_high"] = math.exp(float(ci.loc[column].iloc[1]))
        if robust:
            by_se[key + "robust_se"] = float(se[column])
        else:
            out[key + "std_error"] = float(se[column])
    out["model|||log_lik"] = float(fit.log_likelihood_)
    test = fit.log_likelihood_ratio_test()
    out["model|||lr_statistic"] = float(test.test_statistic)
    out["model|||lr_p_value"] = float(test.p_value)
    b = fit.params_.to_numpy()
    by_se["model|||wald_statistic"] = float(b @ np.linalg.solve(fit.variance_matrix_.to_numpy(), b))
    if not p.get("start"):
        out["model|||aic"] = float(fit.AIC_partial_)
        out["model|||concordance"] = float(fit.concordance_index_)
        zph = proportional_hazard_test(fit, frame, time_transform="km")
        for (term, level, column) in names:
            if level is None or sum(1 for n in names if n[0] == term) == 1:
                beside[f"assumption|schoenfeld|{term}||p_value".replace("||", "|", 1)] = float(zph.summary.loc[column, "p"])
    return out, beside


PS_FITTERS = {  # lifelines' class, its location's and its shape's parameter, the shape's name in the runner, log scale
    "weibull": (WeibullAFTFitter, "lambda_", "rho_", "shape"),
    "lognormal": (LogNormalAFTFitter, "mu_", "sigma_", "sdlog"),
    "loglogistic": (LogLogisticAFTFitter, "alpha_", "beta_", "shape"),
}


def ps_case(case: dict) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """What lifelines says of the keys of a parametric survival case: (values compared, their
    standard errors, values said beside). Said beside, not compared: the estimates of a fit whose
    log-likelihood lifelines leaves below R's (its log-logistic stops 2.7e-5 below on
    ps_loglogistic, the same likelihood evaluated at both points: its optimiser, on covariates it
    normalizes, stops short) and the standard errors of its generalized gamma (its variance
    disagrees with the inverse Hessian of both R constructions, which agree with each other to
    1e-7; lifelines calls that fitter experimental)."""
    p, types = case["params"], case["types"]
    df = pd.read_csv(HERE / case["data"], keep_default_na=False, na_values=[""], dtype=str)
    predictors = list(p.get("predictors", []))
    used = [p["time"], p["event"], *predictors, *([p["weights"]] if p.get("weights") else [])]
    df = df.dropna(subset=used).reset_index(drop=True)
    df = df[pd.to_numeric(df[p["time"]]) > 0].reset_index(drop=True)
    frame = pd.DataFrame({"T": pd.to_numeric(df[p["time"]]),
                          "E": is_event(df[p["event"]], p["event_value"], types[p["event"]]).astype(int)})
    parts: dict[str, list[tuple[str | None, pd.Series]]] = {}
    for name in predictors:
        kind = types[name]
        if kind in ("number", "integer"):
            parts[name] = [(None, pd.to_numeric(df[name]))]
            continue
        labels = df[name].str.lower() if kind == "boolean" else df[name]
        levels = sorted(labels.unique())
        reference = str(p.get("reference", {}).get(name, levels[0]))
        parts[name] = [(level, (labels == level).astype(float)) for level in levels if level != reference]
    names: list[tuple[str, str | None, str]] = []  # (term, level, the frame's column) in the runner's order
    for name in predictors:
        for level, x in parts[name]:
            frame[f"x{len(names)}"] = x.to_numpy()
            names.append((name, level, f"x{len(names)}"))
    for a, b in p.get("interactions", []):
        for la, xa in parts[a]:
            for lb, xb in parts[b]:
                level = " × ".join(v for v in (la, lb) if v is not None) or None
                frame[f"x{len(names)}"] = (xa * xb).to_numpy()
                names.append((f"{a} × {b}", level, f"x{len(names)}"))
    kw: dict = {}
    if p.get("weights"):
        frame["W"] = pd.to_numeric(df[p["weights"]])
        kw["weights_col"] = "W"
    dist = p.get("distribution", "weibull")
    columns = [c for _, _, c in names]

    def fitted(gtol: float):
        options = {"gtol": gtol, "maxiter": 5000}
        if dist == "gengamma":
            regressors = {"mu_": " + ".join(columns) if columns else "1", "sigma_": "1", "lambda_": "1"}
            return GeneralizedGammaRegressionFitter(alpha=p.get("alpha", 0.05)).fit(frame, "T", "E", regressors=regressors,
                                                                                    fit_options=options, **kw)
        return PS_FITTERS[dist][0](alpha=p.get("alpha", 0.05)).fit(frame, "T", "E", fit_options=options, **kw)

    fit = None
    for gtol in PS_GTOLS:
        try:
            fit = fitted(gtol)
            break
        except ConvergenceError:
            print(f"NOTE {case['id']}: lifelines does not reach gtol {gtol:g}")
    if fit is None:
        raise RuntimeError(f"{case['id']}: lifelines does not converge")
    location, shape, shape_name = ("mu_", "sigma_", "sigma") if dist == "gengamma" else PS_FITTERS[dist][1:]
    r_ll = next((e["value"] for e in case["expect"] if e["key"] == "model|||log_lik"), None)
    if r_ll is None:  # a case of the checks (lot P2) or of the choice: no fit of its own to witness
        return {}, {}, {}
    short = fit.log_likelihood_ < r_ll - 1e-9 * abs(r_ll)
    if short:
        print(f"STOPS SHORT {case['id']}: lifelines' log-likelihood {fit.log_likelihood_!r}, R's {r_ll!r} "
              f"({(r_ll - fit.log_likelihood_) / abs(r_ll):.2g} relative below): its estimates are said beside")
    out: dict[str, float] = {"model|||log_lik": float(fit.log_likelihood_)}
    se: dict[str, float] = {}
    beside: dict[str, float] = {}
    estimates = beside if short else out
    for term, level, column in [("(Intercept)", None, "Intercept"), *names]:
        key = f"coef|{term}|{level or ''}|"
        estimates[key + "estimate"] = float(fit.params_[(location, column)])
        se[key + "estimate"] = float(fit.standard_errors_[(location, column)])
        (beside if short or dist == "gengamma" else out)[key + "std_error"] = se[key + "estimate"]
    estimates[f"parameter|{shape_name}||estimate"] = math.exp(float(fit.params_[(shape, "Intercept")]))
    if dist == "gengamma":
        estimates["parameter|Q||estimate"] = float(fit.params_[("lambda_", "Intercept")])
        se["parameter|Q||estimate"] = float(fit.standard_errors_[("lambda_", "Intercept")])
    return out, se, beside


def main() -> int:
    expected = json.loads((HERE / "expected.json").read_text(encoding="utf-8"))
    bad = compared = 0
    worst = 0.0
    per_model: dict[str, list[float]] = {}  # model → [values compared, the largest relative difference]
    ps_in_se = [0.0]  # parametric survival: the largest difference of an estimate in its standard errors
    for case in expected["cases"]:
        if case.get("refuses") or case["model"] == "cox" and case["params"].get("ties", "efron") != "efron":
            continue
        if case["model"] == "parametric_survival" and (case["params"].get("strata") or case["params"].get(
                "distribution", "weibull") not in ("weibull", "lognormal", "loglogistic", "gengamma")):
            continue
        beside: dict[str, float] = {}
        se_of: dict[str, float] = {}
        if case["model"] == "kaplan_meier":
            said = km_case(case)
        elif case["model"] == "parametric_survival":
            said, se_of, beside = ps_case(case)
        else:
            said, beside = cox_case(case)
        want_of = {e["key"]: e.get("value") for e in case["expect"]}
        for key, value in beside.items():
            print(f"DEFINED APART {case['id']} {key}: R {want_of.get(key)!r}, lifelines {value!r}")
        for e in case["expect"]:
            if "text" in e or e["key"] not in said:
                continue
            own = said.get(e["key"] + " (as lifelines)")
            if own is not None:
                print(f"MIDDLE OF A STEP {case['id']} {e['key']}: R {e['value']!r}, lifelines' first time {own!r}, "
                      f"the step's middle {said[e['key']]!r}")
            want, got = e["value"], said[e["key"]]
            compared += 1
            mine = per_model.setdefault(case["model"], [0, 0.0])
            mine[0] += 1
            if want is None or math.isnan(got):
                ok = want is None and math.isnan(got)
            elif case["model"] == "parametric_survival":
                ok = abs(got - want) <= max(1e-12, PS_TOL * abs(want), PS_TOL_SE * se_of.get(e["key"], 0.0))
                mine[1] = max(mine[1], abs(got - want) / max(abs(want), 1e-12))
                if e["key"] in se_of:
                    ps_in_se[0] = max(ps_in_se[0], abs(got - want) / se_of[e["key"]])
            else:
                ok = abs(got - want) <= max(1e-12, TOL * abs(want))
                worst = max(worst, abs(got - want) / max(abs(want), 1e-12))
                mine[1] = max(mine[1], abs(got - want) / max(abs(want), 1e-12))
            if not ok:
                bad += 1
                print(f"DIFFERS {case['id']} {e['key']}: R {want!r}, lifelines {got!r}")
    for model, (n, w) in per_model.items():
        print(f"  {model}: {n} values, the largest relative difference {w:.3g}")
    if "parametric_survival" in per_model:
        print(f"  parametric_survival: the largest difference of an estimate, in its standard errors {ps_in_se[0]:.3g}")
    print(f"{compared} values compared with lifelines, {bad} differ by more than their tolerance; the largest relative difference "
          f"{worst:.3g}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
