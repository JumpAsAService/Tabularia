"""The witness of the group «experiments» (docs/specs/ab-testing.md, «Self-test, reference values,
witnesses»): SciPy and statsmodels, a second implementation outside Tabularia and outside R, compute from
the same tables what they can of expected.json (written by make_reference.R in the group's image), and say
where the two differ by more than the spec's tolerance. Run by hand, with the versions of witness.json, in a
scratch environment:

    uv venv /tmp/abw && uv pip install --python /tmp/abw/bin/python numpy==2.5.5 scipy==1.18.1 \\
        statsmodels==0.15.0 mpmath==1.3.0
    /tmp/abw/bin/python -I gateway-go/internal/modelgroups/groups/experiments/reference/make_witness.py

and, for «Welch's t = the Statistics node's», with the engine's own Python (Polars), which runs Tabularia's
stat_t_two_sample on the same rows:

    backend/.venv/bin/python -I gateway-go/internal/modelgroups/groups/experiments/reference/make_witness.py --engine

Lot E2 (the comparisons), per case of expected.json that lists a `reading` (the cases of make_reference.R's
ab_case): the units as the node makes them (pandas: the rows of a unit together, a unit in two variants left
out, a NULL metric left out, the covariate filled with its variant's mean or the unit left out, the cap at
numpy's `inverted_cdf` quantile, which is R's type 1); then

    Welch's t, df, p, interval   scipy.stats.ttest_ind(equal_var=False) and its confidence_interval
                                 (ttest_ind_from_stats for aggregated rows)                        1e-12
    the unpooled z, its interval statsmodels test_proportions_2indep(method="wald"),
                                 confint_proportions_2indep(method="wald")                         1e-12
    the delta method             a transcription from the units' sums; with every denominator 1 the
                                 ratio's variance is the mean's (checked on the case's own units)  1e-12
    CUPED                        statsmodels OLS y ~ C(variant) + x: the variant's coefficient is the
                                 adjusted difference, x's (on the units with a covariate) is θ      1e-12
    Fieller                      a transcription; the interval leaves 0 out ⇔ the test rejects     1e-10
    Holm, BH, Bonferroni         statsmodels multipletests; scipy.stats.false_discovery_control    1e-15
    SRM, the empty covariates    scipy.stats.chisquare, scipy.stats.chi2_contingency(correction=False) 1e-12
    covariate balance            statsmodels anova_oneway(use_var="unequal")                       1e-12
    power at this size, MDE      a transcription with scipy.stats.norm                             1e-9

Lot E8 (the secondary metrics), per case that names some: each metric's units with pandas, Welch's t (SciPy) or the
Wald z (statsmodels), Fieller, one multipletests over every metric × treatment, the decision in each metric's own
direction, the detectable lift;
the same case's primary keys by witness_comparisons above (they are E2's, computed without the secondary metrics).

Lot E4 (the sequential test), per case seq_* (make_reference_sequential.R): every look from the raw rows (the
lift, its delta-method variance, CUPED's θ with statsmodels OLS), the mixture's log Λ, the running minimum of 1/Λ,
the confidence sequence, multipletests at each look, the first crossing, the decision                1e-12
                                 Johari et al.'s balanced formula where the arms are equal           1e-12
                                 the closed form against SciPy's quad over the mixture               1e-10

A tolerance is relative to the expected value (absolute below 1e-300). Each check prints one line; exit 1
when one differs. The Bayesian, sequential and planning parts (lots E3, E4, E5) add their witnesses here,
each as its own function called from main().
"""
from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EXPECTED = json.loads((HERE / "expected.json").read_text(encoding="utf-8"))
ALTERNATIVE = {"two_sided": "two-sided", "greater": "larger", "less": "smaller"}
SCIPY_ALT = {"two_sided": "two-sided", "greater": "greater", "less": "less"}
DEFAULTS = {"alternative": "two_sided", "alpha": 0.05, "correction": "holm", "higher_is_better": True, "srm_alpha": 0.001,
            "min_lift": 0.02, "power": 0.8, "covariate_missing": "fill", "input_shape": "units"}

said: list[tuple[str, str, str, float | None, float | None, float]] = []


def check(case: str, key: str, got: float | None, want: float | None, tol: float, what: str) -> None:
    """One comparison: the witness's value against expected.json's."""
    if want is None or got is None or (isinstance(got, float) and math.isnan(got)):
        ok = (want is None) == (got is None or (isinstance(got, float) and math.isnan(got)))
        said.append(("ok" if ok else "DIFF", case, f"{key} [{what}]", got, want, tol))
        return
    err = abs(got - want) / max(abs(want), 1e-300) if want != 0 else abs(got)
    said.append(("ok" if err <= tol else "DIFF", case, f"{key} [{what}]", got, want, tol))


def expected_of(case: dict[str, Any]) -> dict[str, Any]:
    return {e["key"]: e.get("text", e.get("value")) for e in case["expect"]}


def read(case: dict[str, Any]):
    import pandas as pd
    df = pd.read_csv(HERE / case["data"], dtype=str, keep_default_na=False, na_values=[""])
    for name, kind in (case.get("types") or {}).items():
        if kind in ("number", "integer"):
            df[name] = pd.to_numeric(df[name])
        elif kind == "date":
            df[name] = pd.to_datetime(df[name])
    return df


def units_of(case: dict[str, Any]):
    """The units of a case as the node makes them, and what it needs besides: the labels, the units assigned
    (SRM's), the empty covariates per variant."""
    import numpy as np
    p = {**DEFAULTS, **case["params"]}
    df = read(case)
    v = p["variant"]
    df = df[df[v].notna()]
    if (case.get("types") or {}).get(v) == "integer":  # a whole-number variant is labelled by its digits
        df[v] = df[v].map(lambda x: str(int(x)))
    metric = [p["numerator"], p["denominator"]] if p["metric_type"] == "ratio" else [p.get("metric")]
    if p.get("unit"):
        nvar = df.groupby(p["unit"])[v].nunique()
        df = df[df[p["unit"]].isin(nvar.index[nvar == 1])]
        rows = df
        agg = {v: "first"}
        for m in metric:
            agg[m] = (lambda s: float(s.dropna().gt(0).any())) if p["metric_type"] == "binary" else (lambda s: s.sum(min_count=1))
        df = rows.groupby(p["unit"]).agg(agg).reset_index()
    labels = sorted(df[v].unique(), key=lambda s: s.encode("utf-8"))
    assigned = np.array([float((df[v] == lab).sum()) for lab in labels])
    df = df.dropna(subset=metric)
    empty = given = None
    cov = p.get("covariate")
    own = None
    if cov:
        has = df[cov].notna()
        empty = np.array([float(((df[v] == lab) & ~has).sum()) for lab in labels])
        given = np.array([float(((df[v] == lab) & has).sum()) for lab in labels])
        if p["covariate_missing"] == "exclude":
            df = df[has]
            own = np.ones(len(df), dtype=bool)
        else:
            own = has.to_numpy()
            fill = df[has].groupby(v)[cov].mean()
            df[cov] = df[cov].fillna(df[v].map(fill))
    if p.get("cap_percentile") is not None:
        cap = float(np.quantile(df[p["metric"]].to_numpy(), p["cap_percentile"], method="inverted_cdf"))
        df[p["metric"]] = df[p["metric"]].clip(upper=cap)
    return p, df.reset_index(drop=True), labels, assigned, empty, given, own


def fieller(mt: float, vt: float, mc: float, vc: float, q: float) -> tuple[float | None, float | None]:
    g = q * q * vc / (mc * mc)
    if g >= 1:
        return None, None
    r = mt / mc
    h = (q / abs(mc)) * math.sqrt(vt * (1 - g) + r * r * vc)
    return (r - h) / (1 - g) - 1, (r + h) / (1 - g) - 1


def delta_var(y, x) -> float:
    """The variance of ΣY/ΣX by the delta method on the units' values (Deng, Knoblich, Lu 2018)."""
    import numpy as np
    r = y.sum() / x.sum()
    cov = float(np.cov(y, x)[0, 1]) if len(y) > 1 else 0.0
    return float((np.var(y, ddof=1) - 2 * r * cov + r * r * np.var(x, ddof=1)) / (x.mean() ** 2 * len(y)))


# ── lot E2: the comparisons ──────────────────────────────────────────────────────
def witness_comparisons(case: dict[str, Any]) -> None:
    import numpy as np
    import statsmodels.api as sm
    import statsmodels.formula.api as smf
    from scipy import stats
    from statsmodels.stats.multitest import multipletests
    from statsmodels.stats.oneway import anova_oneway
    from statsmodels.stats.proportion import confint_proportions_2indep, test_proportions_2indep

    cid = case["id"]
    want = expected_of(case)
    if case["params"].get("input_shape") == "aggregated":
        return witness_aggregated(case)
    p, df, labels, assigned, empty, given, own = units_of(case)
    v, kind = p["variant"], p["metric_type"]
    k = len(labels)
    ci = labels.index(p["control"])
    treatments = [j for j in range(k) if j != ci]
    m_t = len(treatments)
    a_int = p["alpha"] / m_t if p["correction"] == "bonferroni" else p["alpha"]
    a_cmp = p["alpha"] if p["correction"] == "none" else p["alpha"] / m_t
    two = p["alternative"] == "two_sided"
    g = np.array([labels.index(x) for x in df[v]])
    n = np.array([float((g == j).sum()) for j in range(k)])
    cuped = bool(p.get("covariate"))

    # SRM
    chi = stats.chisquare(assigned, f_exp=assigned.sum() * _shares(p, labels))
    check(cid, "assumption|srm||statistic", float(chi.statistic), want.get("assumption|srm||statistic"), 1e-12, "scipy chisquare")
    check(cid, "assumption|srm||p_value", float(chi.pvalue), want.get("assumption|srm||p_value"), 1e-12, "scipy chisquare")

    # each variant's estimate and its variance
    if kind == "ratio":
        y, x = df[p["numerator"]].to_numpy(float), df[p["denominator"]].to_numpy(float)
        m = np.array([y[g == j].sum() / x[g == j].sum() for j in range(k)])
        xbar = np.array([x[g == j].mean() for j in range(k)])
        lin = (y - m[g] * x) / xbar[g]
        v_ = np.array([delta_var(y[g == j], x[g == j]) for j in range(k)])
        # a ratio with every denominator 1 is the numerator's mean: its variance is the mean's
        gap = max(abs(delta_var(y[g == j], np.ones(int(n[j]))) / (np.var(y[g == j], ddof=1) / n[j]) - 1) for j in range(k))
        check(cid, "the delta method with denominators of 1", float(gap), 0.0, 1e-12, "the mean's variance")
    else:
        y = df[p["metric"]].to_numpy(float)
        m = np.array([y[g == j].mean() for j in range(k)])
        v_ = m * (1 - m) / n if kind == "binary" else np.array([np.var(y[g == j], ddof=1) / n[j] for j in range(k)])
        lin = y
    for j, lab in enumerate(labels):
        if not cuped:
            check(cid, f"variant|{lab}||mean", float(m[j]), want.get(f"variant|{lab}||mean"), 1e-12, "pandas")
            check(cid, f"variant|{lab}||std_error", float(math.sqrt(v_[j])), want.get(f"variant|{lab}||std_error"), 1e-12, "transcription")
    if cuped:
        c = df[p["covariate"]].to_numpy(float)
        frame = {"lin": lin, "g": g.astype(str), "c": c}
        import pandas as pd
        fr = pd.DataFrame(frame)
        theta = float(smf.ols("lin ~ C(g) + c", data=fr[own]).fit().params["c"])
        check(cid, "model|||theta", theta, want.get("model|||theta"), 1e-12, "statsmodels OLS on the units with a covariate")
        if kind == "continuous":
            fit = smf.ols(f"lin ~ C(g, Treatment({ci})) + c", data=fr).fit()
            for t in treatments:
                key = f"comparison|{labels[t]}|{labels[ci]}|difference"
                check(cid, key, float(fit.params[f"C(g, Treatment({ci}))[T.{t}]"]), want.get(key), 1e-12, "statsmodels OLS y ~ C(variant) + x")
        cbar = c.mean()
        m = np.array([m[j] - theta * (c[g == j].mean() - cbar) for j in range(k)])
        v_ = np.array([np.var((lin - theta * c)[g == j], ddof=1) / n[j] for j in range(k)])
        # a variant's own interval: the grand mean of the covariate is an estimate too (θ² s²_x / N more, lot E10)
        for j, lab in enumerate(labels):
            check(cid, f"variant|{lab}||std_error", float(math.sqrt(v_[j] + theta ** 2 * np.var(c, ddof=1) / len(c))),
                  want.get(f"variant|{lab}||std_error"), 1e-12, "transcription with the covariate's grand mean")
        if empty is not None and empty.sum() > 0:
            ch = stats.chi2_contingency(np.vstack([empty, given]), correction=False)
            check(cid, "assumption|covariate_missing_balance||statistic", float(ch.statistic),
                  want.get("assumption|covariate_missing_balance||statistic"), 1e-12, "scipy chi2_contingency")
            check(cid, "assumption|covariate_missing_balance||p_value", float(ch.pvalue),
                  want.get("assumption|covariate_missing_balance||p_value"), 1e-12, "scipy chi2_contingency")
        a = anova_oneway(c[own], g[own], use_var="unequal")
        check(cid, "assumption|covariate_balance||statistic", float(a.statistic), want.get("assumption|covariate_balance||statistic"), 1e-12,
              "statsmodels anova_oneway")
        check(cid, "assumption|covariate_balance||df_2", float(a.df[1]), want.get("assumption|covariate_balance||df_2"), 1e-12,
              "statsmodels anova_oneway")
        check(cid, "assumption|covariate_balance||p_value", float(a.pvalue), want.get("assumption|covariate_balance||p_value"), 1e-12,
              "statsmodels anova_oneway")

    ps: list[float] = []
    for t in treatments:
        pre = f"comparison|{labels[t]}|{labels[ci]}|"
        d = float(m[t] - m[ci])
        se = math.sqrt(v_[t] + v_[ci])
        if se == 0:
            ps.append(float("nan"))
            continue
        welch = kind == "continuous" and not cuped
        if welch:
            tt = stats.ttest_ind(y[g == t], y[g == ci], equal_var=False, alternative=SCIPY_ALT[p["alternative"]])
            iv = tt.confidence_interval(confidence_level=1 - a_int)
            stat, pv, df_ = float(tt.statistic), float(tt.pvalue), float(tt.df)
            check(cid, pre + "statistic", stat, want.get(pre + "statistic"), 1e-12, "scipy ttest_ind")
            check(cid, pre + "df", df_, want.get(pre + "df"), 1e-12, "scipy ttest_ind")
            check(cid, pre + "p_value", pv, want.get(pre + "p_value"), 1e-12, "scipy ttest_ind")
            check(cid, pre + "difference_ci_low", _finite(iv.low), want.get(pre + "difference_ci_low"), 1e-12, "scipy confidence_interval")
            check(cid, pre + "difference_ci_high", _finite(iv.high), want.get(pre + "difference_ci_high"), 1e-12, "scipy confidence_interval")
            q = float(stats.t.ppf(1 - a_int / 2, df_) if two else stats.t.ppf(1 - a_int, df_))
        else:
            stat = d / se
            if kind == "binary" and not cuped:
                cnt = [float(y[g == j].sum()) for j in (t, ci)]
                r = test_proportions_2indep(cnt[0], n[t], cnt[1], n[ci], method="wald", compare="diff", alternative=ALTERNATIVE[p["alternative"]])
                stat, pv = float(r.statistic), float(r.pvalue)
                lo, hi = confint_proportions_2indep(cnt[0], n[t], cnt[1], n[ci], method="wald", compare="diff",
                                                    alpha=a_int if two else 2 * a_int)
                what = "statsmodels test_proportions_2indep(wald)"
                check(cid, pre + "difference_ci_low", None if p["alternative"] == "less" else float(lo), want.get(pre + "difference_ci_low"), 1e-12,
                      "statsmodels confint_proportions_2indep(wald)")
                check(cid, pre + "difference_ci_high", None if p["alternative"] == "greater" else float(hi), want.get(pre + "difference_ci_high"),
                      1e-12, "statsmodels confint_proportions_2indep(wald)")
            else:
                pv = float(2 * stats.norm.sf(abs(stat)) if two else stats.norm.sf(stat) if p["alternative"] == "greater" else stats.norm.cdf(stat))
                what = "the normal, scipy"
            check(cid, pre + "statistic", stat, want.get(pre + "statistic"), 1e-12, what)
            check(cid, pre + "p_value", pv, want.get(pre + "p_value"), 1e-12, what)
            q = float(stats.norm.ppf(1 - a_int / 2) if two else stats.norm.ppf(1 - a_int))
        ps.append(pv)
        # Fieller, and the identity at ratio 1
        if m[ci] != 0:
            lo, hi = fieller(float(m[t]), float(v_[t]), float(m[ci]), float(v_[ci]), q)
            if not two and lo is not None:
                if (p["alternative"] == "greater") == (m[ci] > 0):
                    hi = None
                else:
                    lo = None
            check(cid, pre + "lift_ci_low", lo, want.get(pre + "lift_ci_low"), 1e-10, "Fieller, transcription")
            check(cid, pre + "lift_ci_high", hi, want.get(pre + "lift_ci_high"), 1e-10, "Fieller, transcription")
            if lo is not None or hi is not None:
                excludes = (lo is not None and lo > 0) or (hi is not None and hi < 0)
                check(cid, pre + "Fieller ⇔ test", float(excludes == (pv < a_int)), 1.0, 0.0, "the interval leaves 0 out ⇔ p < its level")
            # the power at this size
            za = float(stats.norm.ppf(1 - a_cmp / 2) if two else stats.norm.ppf(1 - a_cmp))
            check(cid, pre + "mde", (za + float(stats.norm.ppf(p["power"]))) * se / abs(m[ci]), want.get(pre + "mde"), 1e-9, "transcription")
            delta = p["min_lift"] * abs(m[ci]) / se
            pw = float(stats.norm.cdf(delta - za) + (stats.norm.cdf(-delta - za) if two else 0.0))
            check(cid, pre + "power_at_min_lift", pw, want.get(pre + "power_at_min_lift"), 1e-9, "transcription")
    _corrections(cid, p, ps, [f"comparison|{labels[t]}|{labels[ci]}|p_adjusted" for t in treatments], want)


def witness_aggregated(case: dict[str, Any]) -> None:
    """Aggregated rows: SciPy from the sums alone (ttest_ind_from_stats; the proportions' z from the counts)."""
    import numpy as np
    from scipy import stats
    from statsmodels.stats.proportion import test_proportions_2indep

    cid = case["id"]
    want = expected_of(case)
    p = {**DEFAULTS, **case["params"]}
    df = read(case)
    v = p["variant"]
    labels = sorted(df[v].dropna().unique(), key=lambda s: s.encode("utf-8"))
    n = np.array([df.loc[df[v] == lab, p["units_count"]].sum() for lab in labels], dtype=float)
    s = np.array([df.loc[df[v] == lab, p["metric_sum"]].sum() for lab in labels], dtype=float)
    ci = labels.index(p["control"])
    chi = stats.chisquare(n, f_exp=n.sum() * _shares(p, labels))
    check(cid, "assumption|srm||p_value", float(chi.pvalue), want.get("assumption|srm||p_value"), 1e-12, "scipy chisquare")
    ps = []
    for t in [j for j in range(len(labels)) if j != ci]:
        pre = f"comparison|{labels[t]}|{labels[ci]}|"
        if p["metric_type"] == "binary":
            r = test_proportions_2indep(s[t], n[t], s[ci], n[ci], method="wald", compare="diff")
            check(cid, pre + "statistic", float(r.statistic), want.get(pre + "statistic"), 1e-12, "statsmodels test_proportions_2indep(wald)")
            check(cid, pre + "p_value", float(r.pvalue), want.get(pre + "p_value"), 1e-12, "statsmodels test_proportions_2indep(wald)")
            ps.append(float(r.pvalue))
        else:
            q = np.array([df.loc[df[v] == lab, p["metric_sum_squares"]].sum() for lab in labels], dtype=float)
            mean = s / n
            sd = np.sqrt((q - s * s / n) / (n - 1))
            r = stats.ttest_ind_from_stats(mean[t], sd[t], n[t], mean[ci], sd[ci], n[ci], equal_var=False)
            check(cid, pre + "statistic", float(r.statistic), want.get(pre + "statistic"), 1e-12, "scipy ttest_ind_from_stats")
            check(cid, pre + "p_value", float(r.pvalue), want.get(pre + "p_value"), 1e-12, "scipy ttest_ind_from_stats")
            ps.append(float(r.pvalue))
    _corrections(cid, p, ps, [f"comparison|{labels[t]}|{labels[ci]}|p_adjusted" for t in range(len(labels)) if t != ci], want)


def _corrections(cid: str, p: dict[str, Any], ps: list[float], keys: list[str], want: dict[str, Any]) -> None:
    """The correction itself, on expected.json's own p-values (the witness's p-values are compared above, at
    1e-12: a correction is a few multiplications, its witness at 1e-15 needs the same inputs)."""
    import numpy as np
    from scipy import stats
    from statsmodels.stats.multitest import multipletests
    ps = [want.get(key.replace("|p_adjusted", "|p_value")) for key in keys]
    ps = [float("nan") if x is None else float(x) for x in ps]
    ok = [i for i, x in enumerate(ps) if not math.isnan(x)]
    if not ok:
        return
    how = p["correction"]
    arr = np.array([ps[i] for i in ok])
    if how == "none":
        adj = arr
    else:
        adj = multipletests(arr, method={"holm": "holm", "benjamini_hochberg": "fdr_bh", "bonferroni": "bonferroni"}[how])[1]
        if how == "benjamini_hochberg":
            check(cid, "BH by two witnesses", float(np.max(np.abs(stats.false_discovery_control(arr) - adj))), 0.0, 1e-15,
                  "scipy false_discovery_control = statsmodels fdr_bh")
    for i, a in zip(ok, adj):
        w = want.get(keys[i])
        check(cid, keys[i], float(a), w, 1e-15 if w and w > 1e-3 else 1e-12, f"statsmodels multipletests ({how})")


def _shares(p: dict[str, Any], labels: list[str]):
    import numpy as np
    w = p.get("expected_shares") or {}
    x = np.array([float(w.get(lab, 1.0)) for lab in labels]) if w else np.ones(len(labels))
    return x / x.sum()


def _finite(x: float) -> float | None:
    return float(x) if math.isfinite(x) else None


# ── Welch's t of the A/B node = the Statistics node's (the engine's stat_t_two_sample) ──
def _quantile1(values: list[float], prob: float) -> float:
    """R's quantile of type 1 (a value of the data: the inverse of the empirical distribution), without numpy."""
    xs = sorted(values)
    nppm = len(xs) * prob
    j = math.floor(nppm + 4 * sys.float_info.epsilon)
    return xs[min(max(j + (nppm > j), 1), len(xs)) - 1]


def witness_engine() -> None:
    import polars as pl
    sys.path.insert(0, str(HERE.parents[5] / "backend"))
    from app.engine.stats.compute import run_hypothesis_test
    from app.engine.stats.output import OUTPUT_COLUMNS
    from app.engine.stats.polars_source import PolarsStatSource

    names = [c[0] for c in OUTPUT_COLUMNS]
    for case in EXPECTED["cases"]:
        prm = case["params"]
        if case.get("refuses") or case["model"] != "ab_test" or prm.get("metric_type") != "continuous" or prm.get("covariate") \
                or prm.get("input_shape") == "aggregated" or prm.get("unit") or "reading" not in case:
            continue
        p = {**DEFAULTS, **prm}
        with open(HERE / case["data"], encoding="utf-8", newline="") as f:  # the csv module: R doubles a quote in a name
            head, *body = list(csv.reader(f))
        df = pl.DataFrame({h: [r[i] or None for r in body] for i, h in enumerate(head)}, schema={h: pl.String for h in head})
        df = df.with_columns(pl.col(p["metric"]).cast(pl.Float64))
        df = df.filter(pl.col(p["variant"]).is_not_null() & pl.col(p["metric"]).is_not_null())
        if p.get("cap_percentile") is not None:
            cap = _quantile1(df[p["metric"]].to_list(), p["cap_percentile"])
            df = df.with_columns(pl.col(p["metric"]).clip(upper_bound=cap))
        labels = sorted(df[p["variant"]].unique().to_list(), key=lambda s: s.encode("utf-8"))
        m_t = len(labels) - 1
        a_int = p["alpha"] / m_t if p["correction"] == "bonferroni" else p["alpha"]
        want = expected_of(case)
        for t in labels:
            if t == p["control"]:
                continue
            pre = f"comparison|{t}|{p['control']}|"
            try:
                rows = run_hypothesis_test(PolarsStatSource(df.lazy()), "t_two_sample",
                                           {"value": p["metric"], "group": p["variant"], "groups": [t, p["control"]], "var_equal": False,
                                            "alternative": p["alternative"], "alpha": a_int}, "polars")
            except Exception as e:  # the Statistics node refuses where the A/B node says «not computed»
                check(case["id"], pre + "statistic", None, want.get(pre + "statistic"), 0.0, f"Tabularia's stat_t_two_sample refused: {e}")
                continue
            got = {r[names.index("stat")]: r[names.index("value")] for r in rows if r[names.index("section")] == "test"}
            for stat, key in (("estimate", "difference"), ("std_error", "difference_std_error"), ("statistic", "statistic"), ("df", "df"),
                              ("p_value", "p_value"), ("ci_low", "difference_ci_low"), ("ci_high", "difference_ci_high")):
                value = got.get(stat)
                check(case["id"], pre + key, None if value is None or not math.isfinite(value) else float(value), want.get(pre + key), 1e-12,
                      "Tabularia's stat_t_two_sample (Welch)")


# ── lot E3: the Bayesian reading ─────────────────────────────────────────────────
def check_abs(case: str, key: str, got: float | None, want: float | None, tol: float, what: str) -> None:
    """A comparison at an absolute tolerance (the spec's 1e-10 for probabilities and losses)."""
    if want is None or got is None:
        said.append(("ok" if want is None and got is None else "DIFF", case, f"{key} [{what}]", got, want, tol))
        return
    said.append(("ok" if abs(got - want) <= tol else "DIFF", case, f"{key} [{what}]", got, want, tol))


def witness_bayes(case: dict[str, Any]) -> None:
    """The Bayesian measures of a case of make_reference_bayes.R, by mpmath (30 digits, tanh-sinh on the pieces
    between the posteriors' quantiles) and, for two Beta posteriors with a whole alpha, Miller's sum and
    Stucchio's identity at 50 digits; the posteriors from the raw rows as the node makes them (pandas, statsmodels
    for CUPED). The credible intervals, the means and the sds with SciPy."""
    import mpmath as mp
    import numpy as np
    import pandas as pd
    import statsmodels.formula.api as smf
    from scipy import stats

    cid = case["id"]
    want = expected_of(case)
    p, df, labels, assigned, empty, given, own = units_of(case)
    v, kind = p["variant"], p["metric_type"]
    k = len(labels)
    ci = labels.index(p["control"])
    higher = bool(p.get("higher_is_better", True))
    alpha = float(p["alpha"])
    threshold = float(p.get("loss_threshold", 0.001))
    g = np.array([labels.index(x) for x in df[v]])
    n = np.array([float((g == j).sum()) for j in range(k)])
    cuped = bool(p.get("covariate"))
    beta = kind == "binary" and not cuped

    if beta:
        a0, b0 = float(p.get("prior_alpha", 1.0)), float(p.get("prior_beta", 1.0))
        conv = np.array([float(df[p["metric"]].to_numpy(float)[g == j].sum()) for j in range(k)])
        pa, pb = a0 + conv, b0 + n - conv
        mean = pa / (pa + pb)
        sd = np.sqrt(pa * pb / ((pa + pb) ** 2 * (pa + pb + 1)))
        frozen = [stats.beta(pa[j], pb[j]) for j in range(k)]
    else:
        if kind == "ratio":
            y, x = df[p["numerator"]].to_numpy(float), df[p["denominator"]].to_numpy(float)
            mean = np.array([y[g == j].sum() / x[g == j].sum() for j in range(k)])
            var = np.array([delta_var(y[g == j], x[g == j]) for j in range(k)])
            lin = (y - mean[g] * x) / np.array([x[g == j].mean() for j in range(k)])[g]
        else:
            y = df[p["metric"]].to_numpy(float)
            mean = np.array([y[g == j].mean() for j in range(k)])
            var = mean * (1 - mean) / n if kind == "binary" else np.array([np.var(y[g == j], ddof=1) / n[j] for j in range(k)])
            lin = y
        if cuped:
            c = df[p["covariate"]].to_numpy(float)
            fr = pd.DataFrame({"lin": lin, "g": g.astype(str), "c": c})
            theta = float(smf.ols("lin ~ C(g) + c", data=fr[own]).fit().params["c"])
            mean = np.array([mean[j] - theta * (c[g == j].mean() - c.mean()) for j in range(k)])
            var = np.array([np.var((lin - theta * c)[g == j], ddof=1) / n[j] for j in range(k)])
        sd = np.sqrt(var)
        frozen = [stats.norm(mean[j], sd[j]) for j in range(k)]

    mp.mp.dps = 30

    def lower(j: int, x):
        return mp.betainc(pa[j], pb[j], 0, x, regularized=True) if beta else mp.ncdf(x, mean[j], sd[j])

    def pdf(j: int, x):
        return mp.exp((pa[j] - 1) * mp.log(x) + (pb[j] - 1) * mp.log1p(-x) - mp.log(mp.beta(pa[j], pb[j]))) if beta else mp.npdf(x, mean[j], sd[j])

    def behind(j: int, x):
        return lower(j, x) if higher else 1 - lower(j, x)

    def points(js):
        probs = [1e-15, 1e-6, 1e-3, 0.05, 0.25, 0.5, 0.75, 0.95, 1 - 1e-3]
        q = sorted({float(frozen[j].ppf(pr)) for j in js for pr in probs} | {float(frozen[j].isf(1e-15)) for j in js} | {float(frozen[j].isf(1e-6)) for j in js})
        return [mp.mpf(x) for x in q]

    def integral(f, js):
        return float(mp.quad(f, points(js), maxdegree=7))

    base = abs(float(mean[ci]))
    best_p, best_l = [], []
    everyone = list(range(k))
    for j in everyone:
        others = [o for o in everyone if o != j]
        pb_j = integral(lambda x, j=j, others=others: pdf(j, x) * mp.fprod(behind(o, x) for o in others), everyone)
        lb_j = integral(lambda x, j=j: behind(j, x) - mp.fprod(behind(o, x) for o in everyone), everyone)
        best_p.append(pb_j)
        best_l.append(lb_j)
        pre = f"bayes|{labels[j]}||"
        check(cid, pre + "posterior_mean", float(mean[j]), want.get(pre + "posterior_mean"), 1e-12, "scipy/numpy")
        check(cid, pre + "posterior_sd", float(sd[j]), want.get(pre + "posterior_sd"), 1e-12, "scipy/numpy")
        check(cid, pre + "cred_low", float(frozen[j].ppf(alpha / 2)), want.get(pre + "cred_low"), 1e-12, "scipy ppf")
        check(cid, pre + "cred_high", float(frozen[j].isf(alpha / 2)), want.get(pre + "cred_high"), 1e-12, "scipy isf")
        check_abs(cid, pre + "prob_best", pb_j, want.get(pre + "prob_best"), 1e-10, "mpmath quad")
        check_abs(cid, pre + "expected_loss_best", lb_j, want.get(pre + "expected_loss_best"), 1e-10, "mpmath quad")
        check_abs(cid, pre + "expected_loss_best_relative", lb_j / base, want.get(pre + "expected_loss_best_relative"), 1e-10, "mpmath quad")
        if j == ci:
            continue
        t, c_ = j, ci
        if beta and higher and float(pa[t]) == round(float(pa[t])) and float(pa[c_]) == round(float(pa[c_])) and k == 2:
            # Miller's sum and Stucchio's identity, 50 digits
            with mp.workdps(50):
                def gt(ax, bx, ay, by):
                    ax, bx, ay, by = (mp.mpf(float(z)) for z in (ax, bx, ay, by))
                    return mp.fsum(mp.exp(mp.log(mp.beta(ay + i, by + bx)) - mp.log(bx + i) - mp.log(mp.beta(1 + i, bx)) - mp.log(mp.beta(ay, by)))
                                   for i in range(int(ax)))
                mt, mc = mp.mpf(float(mean[t])), mp.mpf(float(mean[c_]))
                at, bt, ac, bc = pa[t], pb[t], pa[c_], pb[c_]
                beat = float(gt(at, bt, ac, bc))
                loss = float(mc * gt(ac + 1, bc, at, bt) - mt * gt(ac, bc, at + 1, bt))
                keep = float(mt * gt(at + 1, bt, ac, bc) - mc * gt(at, bt, ac + 1, bc))
            what = "Miller's sum, Stucchio's identity (mpmath, 50 digits)"
            check_abs(cid, f"{pre}[quadrature against Miller]", integral(lambda x: pdf(t, x) * behind(c_, x), [t, c_]), beat, 1e-10, "mpmath quad = Miller")
        else:
            beat = integral(lambda x: pdf(t, x) * behind(c_, x), [t, c_])
            loss = integral(lambda x: behind(t, x) * (1 - behind(c_, x)), [t, c_])
            keep = integral(lambda x: behind(c_, x) * (1 - behind(t, x)), [t, c_])
            what = "mpmath quad"
        check_abs(cid, pre + "prob_beat_control", beat, want.get(pre + "prob_beat_control"), 1e-10, what)
        check_abs(cid, pre + "expected_loss", loss, want.get(pre + "expected_loss"), 1e-10, what)
        check_abs(cid, pre + "expected_loss_relative", loss / base, want.get(pre + "expected_loss_relative"), 1e-10, what)
        check_abs(cid, pre + "expected_loss_control", keep, want.get(pre + "expected_loss_control"), 1e-10, what)
        check_abs(cid, pre + "expected_loss_control_relative", keep / base, want.get(pre + "expected_loss_control_relative"), 1e-10, what)
        said.append(("ok" if (loss / base <= threshold) == (want.get(pre + "loss_position") == "below") else "DIFF", cid, f"{pre}loss_position [threshold {threshold:g}]",
                     loss / base, want.get(pre + "loss_position"), 0.0))
    check_abs(cid, "Σ P(best) − 1", sum(best_p) - 1.0, 0.0, 1e-10, "mpmath quad")
    check_abs(cid, "Σ expected values' P(best) − 1", sum(want[f"bayes|{lab}||prob_best"] for lab in labels) - 1.0, 0.0, 1e-10, "expected.json")



# ── lot E4: the sequential test ──────────────────────────────────────────────────
def witness_sequential(case: dict[str, Any]) -> None:
    """The sequential test of a case of make_reference_sequential.R (docs/specs/ab-testing.md, «Sequential:
    always-valid p-values»), from the raw rows of every look (pandas, numpy; statsmodels OLS for CUPED's θ;
    statsmodels multipletests at each look): the lift and its delta-method variance, the mixture's log Λ, the
    running minimum of 1/Λ, the confidence sequence intersected over the looks, the first crossing, the decision;
    where every look has as many units in each arm, Johari et al.'s balanced formula Λ_n = √(V/(V + nτ²))
    exp(n²τ²Δ̄²/(2V(V + nτ²))) with V = n·v; and the closed form against the integral over the mixture (SciPy's
    quad). 1e-12 relative, the integral 1e-10."""
    import numpy as np
    import pandas as pd
    import statsmodels.formula.api as smf
    from scipy import integrate
    from statsmodels.stats.multitest import multipletests

    cid = case["id"]
    want = expected_of(case)
    p = {**DEFAULTS, **case["params"]}
    tau = float(p.get("sequential_tau", 0.05))
    kind, v = p["metric_type"], p["variant"]
    aggregated = p["input_shape"] == "aggregated"
    if aggregated:
        df = read(case)
        df = df[df[v].notna()]
        labels = sorted(df[v].unique(), key=lambda s: s.encode("utf-8"))
        own = None
    else:
        p, df, labels, _, _, _, own = units_of(case)
    k = len(labels)
    ci = labels.index(p["control"])
    tr = [j for j in range(k) if j != ci]
    a_int = p["alpha"] / len(tr) if p["correction"] == "bonferroni" else p["alpha"]
    g = np.array([labels.index(x) for x in df[v]])
    if p.get("date"):
        day = ((df[p["date"]] - pd.Timestamp("1970-01-01")).dt.days).to_numpy(float)
    else:
        day = np.zeros(len(df))
    first, last = day.min(), day.max()
    span = last - first + 1
    period = math.ceil(span / 1000) if span > 1000 else 1
    look = np.floor((day - first) / period).astype(int)
    D = int((span - 1) // period) + 1
    day_of = [min(first + (l + 1) * period - 1, last) for l in range(D)]
    check(cid, "model|||n_looks", float(D), want.get("model|||n_looks"), 0, "the days, grouped beyond 1,000")

    def estimates(sel):
        gs = g[sel]
        if aggregated:
            n = np.array([df[p["units_count"]].to_numpy(float)[sel][gs == j].sum() for j in range(k)])
            s = np.array([df[p["metric_sum"]].to_numpy(float)[sel][gs == j].sum() for j in range(k)])
            m = s / n
            if kind == "binary":
                return m, m * (1 - m) / n, n
            q = np.array([df[p["metric_sum_squares"]].to_numpy(float)[sel][gs == j].sum() for j in range(k)])
            return m, (q - s * s / n) / (n - 1) / n, n
        n = np.array([float((gs == j).sum()) for j in range(k)])
        if kind == "ratio":
            y, x = df[p["numerator"]].to_numpy(float)[sel], df[p["denominator"]].to_numpy(float)[sel]
            m = np.array([y[gs == j].sum() / x[gs == j].sum() for j in range(k)])
            var = np.array([delta_var(y[gs == j], x[gs == j]) for j in range(k)])
            lin = (y - m[gs] * x) / np.array([x[gs == j].mean() for j in range(k)])[gs]
        else:
            y = df[p["metric"]].to_numpy(float)[sel]
            m = np.array([y[gs == j].mean() for j in range(k)])
            var = m * (1 - m) / n if kind == "binary" else np.array([np.var(y[gs == j], ddof=1) / n[j] for j in range(k)])
            lin = y
        if p.get("covariate"):
            c = df[p["covariate"]].to_numpy(float)[sel]
            fr = pd.DataFrame({"lin": lin, "g": gs.astype(str), "c": c})
            theta = float(smf.ols("lin ~ C(g) + c", data=fr[own[sel]]).fit().params["c"])
            m = m - theta * (np.array([c[gs == j].mean() for j in range(k)]) - c.mean())
            var = np.array([np.var(lin[gs == j] - theta * c[gs == j], ddof=1) for j in range(k)]) / n
        return m, var, n

    lift = np.full((len(tr), D), np.nan)
    lv = np.full((len(tr), D), np.nan)
    mc = np.full((len(tr), D), np.nan)
    balanced = True
    for l in range(D):
        sel = look <= l
        n = np.array([float((g[sel] == j).sum()) for j in range(k)]) if not aggregated else \
            np.array([df[p["units_count"]].to_numpy(float)[sel][g[sel] == j].sum() for j in range(k)])
        if (n < 2).any() or (own is not None and min(float((g[sel & own] == j).sum()) for j in range(k)) < 2):
            continue
        m, var, n = estimates(sel)
        balanced = balanced and len(set(n.tolist())) == 1
        if m[ci] == 0:
            continue
        for i, t in enumerate(tr):
            w = var[t] / m[ci] ** 2 + m[t] ** 2 * var[ci] / m[ci] ** 4
            if w > 0:
                lift[i, l], lv[i, l], mc[i, l] = m[t] / m[ci] - 1, w, m[ci]
                if balanced:
                    # Johari et al.'s balanced form, n units in each arm, V = n v
                    nn = n[t]
                    V = nn * w
                    johari = 0.5 * math.log(V / (V + nn * tau ** 2)) + nn ** 2 * tau ** 2 * lift[i, l] ** 2 / (2 * V * (V + nn * tau ** 2))
                    lv_ = w
                    mine = -0.5 * math.log1p(tau ** 2 / lv_) + tau ** 2 * lift[i, l] ** 2 / (2 * lv_ * (lv_ + tau ** 2))
                    check(cid, f"look {l + 1} {labels[t]}: log Λ", float(mine), float(johari), 1e-12, "Johari et al.'s balanced formula")
    logl = 0.5 * np.log(lv / (lv + tau ** 2)) + tau ** 2 * lift ** 2 / (2 * lv * (lv + tau ** 2))
    half = np.sqrt(lv * (lv + tau ** 2) / tau ** 2 * (2 * math.log(1 / a_int) + np.log((lv + tau ** 2) / lv)))
    praw = np.ones((len(tr), D))
    low = np.full((len(tr), D), np.nan)
    high = np.full((len(tr), D), np.nan)
    for i in range(len(tr)):
        run, lo, hi = 1.0, -math.inf, math.inf
        for l in range(D):
            if not math.isnan(logl[i, l]):
                run = min(run, math.exp(-logl[i, l]))
                lo, hi = max(lo, lift[i, l] - half[i, l]), min(hi, lift[i, l] + half[i, l])
            praw[i, l] = run
            if math.isfinite(lo) and math.isfinite(hi) and lo <= hi:
                low[i, l], high[i, l] = lo, hi
        if np.isnan(logl[i]).all():
            praw[i, :] = np.nan
    how = {"holm": "holm", "benjamini_hochberg": "fdr_bh", "bonferroni": "bonferroni", "none": None}[p["correction"]]
    padj = praw.copy()
    for l in range(D):
        ok = ~np.isnan(praw[:, l])
        if how and ok.any():
            padj[ok, l] = multipletests(praw[ok, l], method=how)[1]
    for i, t in enumerate(tr):
        lab = labels[t]
        key = f"sequential|{lab}||"
        mono = bool(np.all(np.diff(padj[i]) <= 0)) and bool(np.all(np.diff(praw[i]) <= 0))
        check(cid, key + " p non-increasing over the looks", 0.0 if mono else 1.0, 0.0, 0, "raw and adjusted")
        for stat, value, what in (("p_value", praw[i, -1], "the running minimum of 1/Λ"), ("p_adjusted", padj[i, -1], f"multipletests ({how})"),
                                  ("log_lambda", logl[i, -1], "the mixture's closed form"), ("cs_low", low[i, -1], "the sequence, intersected"),
                                  ("cs_high", high[i, -1], "the sequence, intersected")):
            check(cid, key + stat, None if math.isnan(value) else float(value), want.get(key + stat), 1e-12, what)
        hit = [l for l in range(D) if padj[i, l] < p["alpha"]]
        crossing = float(day_of[hit[0]]) if hit and p.get("date") else None
        check(cid, key + "first_crossing", crossing, want.get(key + "first_crossing"), 0, "the first look below alpha, its last day")
        # the duality: the sequence leaves 0 out exactly when the always-valid p is at most a_int
        empty = math.isnan(low[i, -1]) and math.isnan(high[i, -1]) and not np.isnan(logl[i]).all()  # some look's interval left 0 out
        out = empty or (not math.isnan(low[i, -1]) and low[i, -1] > 0) or (not math.isnan(high[i, -1]) and high[i, -1] < 0)
        check(cid, key + " sequence ⇔ p", 0.0 if out == (praw[i, -1] <= a_int) else 1.0, 0.0, 0, "0 left out ⇔ p ≤ alpha")
        last = max([l for l in range(D) if not math.isnan(lift[i, l])], default=None)
        if last is not None:
            if low[i, -1] > 0:  # a NaN compares False
                up = True
            elif high[i, -1] < 0:
                up = False
            else:
                up = bool(lift[i, last] > 0)
            if mc[i, last] < 0:
                up = not up
            dec = "not_computed" if math.isnan(padj[i, -1]) else ("better" if up == bool(p["higher_is_better"]) else "worse") if padj[i, -1] < p["alpha"] else "not_significant"
            said.append(("ok" if dec == want.get(key + "decision") else "DIFF", cid, key + "decision [the corrected p against alpha]", dec,
                         want.get(key + "decision"), 0))
            # the closed form against the integral of the likelihood ratio over θ ~ N(0, τ²), at the last look
            lam, w = lift[i, last], lv[i, last]
            centre, spread = lam * tau ** 2 / (w + tau ** 2), math.sqrt(w * tau ** 2 / (w + tau ** 2))  # θ's posterior
            # the integrand divided by its value at the centre, so that a Λ beyond a double's range (seq_drift) is a log
            top = centre * lam / w - centre * centre / (2 * w) - centre * centre / (2 * tau ** 2)
            f = lambda th: math.exp(th * lam / w - th * th / (2 * w) - th * th / (2 * tau ** 2) - top) / (tau * math.sqrt(2 * math.pi))
            value = integrate.quad(f, centre - 40 * spread, centre + 40 * spread, epsabs=0, epsrel=1e-13, limit=200, points=[centre])[0]
            check(cid, key + "log_lambda (the integral)", math.log(value) + top, float(logl[i, last]), 1e-10, "scipy quad over the mixture")
        at = [l for l in range(D) if not math.isnan(lift[i, l])]
        for x, l in enumerate(at):
            for chart, stat, series in (("sequential_p", "y", padj), ("sequential_lift", "y", lift), ("sequential_lift", "low", low),
                                        ("sequential_lift", "high", high)):
                ck = f"curve|{chart}|{lab}|{stat}|{x}"
                if ck in want:
                    check(cid, ck, float(series[i, l]), want[ck], 1e-12, "the look's long way")


# ── lot E5: the planning node ─────────────────────────────────────────────────────
PLAN_DEFAULTS = {"solve_for": "sample_size", "min_lift": 0.02, "arms": 2, "alpha": 0.05, "power": 0.8, "alternative": "two_sided",
                 "correction": "holm", "traffic_share": 1.0}


def witness_plan(case: dict[str, Any]) -> None:
    """The planning node (lot E5) from the raw rows, with statsmodels' NormalIndPower for the sizes and the detectable lift
    (1e-9; a binary treatment's variance moves with the lift, which NormalIndPower takes as the common standard deviation
    that gives the same variance of the difference: s_e² = (v_c / w_c + v_t / w_t) / (1 / w_c + 1 / w_t)); a transcription
    of the equation with scipy's brentq for the binary lift; OLS for CUPED's θ and the variance it takes out."""
    import numpy as np
    import statsmodels.api as sm
    from scipy.optimize import brentq
    from statsmodels.stats.power import NormalIndPower

    cid = case["id"]
    p = {**PLAN_DEFAULTS, **case["params"]}
    df = read(case)
    kind = p["metric_type"]
    want = expected_of(case)
    if kind == "ratio":
        y, x = df[p["numerator"]].to_numpy(float), df[p["denominator"]].to_numpy(float)
        m0 = y.sum() / x.sum()
        s0 = math.sqrt((np.var(y, ddof=1) - 2 * m0 * np.cov(y, x)[0, 1] + m0 ** 2 * np.var(x, ddof=1)) / x.mean() ** 2)
    else:
        y = df[p["metric"]].to_numpy(float)
        m0 = y.mean()
        s0 = math.sqrt(m0 * (1 - m0)) if kind == "binary" else float(np.std(y, ddof=1))
    check(cid, "model|||baseline_mean", m0, want["model|||baseline_mean"], 1e-12, "the history's mean")
    check(cid, "model|||baseline_sd", s0, want["model|||baseline_sd"], 1e-12, "the history's standard deviation (delta method for a ratio)")
    keep = 1.0
    if p.get("covariate"):
        c = df[p["covariate"]].to_numpy(float)
        own = ~np.isnan(c)
        fit = sm.OLS(y[own], sm.add_constant(c[own])).fit()
        theta = float(fit.params[1])
        filled = np.where(own, c, c[own].mean())
        r = 1 - float(np.var(y - theta * filled, ddof=1) / np.var(y, ddof=1))
        keep = 1 - r
        check(cid, "model|||variance_reduction", r, want["model|||variance_reduction"], 1e-12, "1 − var(y − θc) / var(y), θ = OLS slope on the units with a covariate")
        rho = float(np.corrcoef(y[own], c[own])[0, 1])
        check(cid, "model|||covariate_correlation", rho, want["model|||covariate_correlation"], 1e-12, "the correlation on the units with a covariate")
    arms = int(p["arms"])
    wc = p.get("control_share") or 1 / arms
    wt = (1 - wc) / (arms - 1)
    a1 = p["alpha"] if p["correction"] == "none" else p["alpha"] / (arms - 1)
    check(cid, "model|||alpha_per_comparison", a1, want["model|||alpha_per_comparison"], 1e-15, "alpha / (arms − 1)")
    alpha_sm = a1 / 2 if p["alternative"] == "two_sided" else a1  # NormalIndPower's «larger» is the one tail the plan's formula uses
    dated = bool(p.get("date"))
    if dated:
        span = (df[p["date"]].max() - df[p["date"]].min()).days + 1
        daily = len(df) / span * p["traffic_share"]
        check(cid, "model|||daily_units", daily, want["model|||daily_units"], 1e-12, "units / days of history × traffic share")
    pw = NormalIndPower()

    def spread(L: float, sc: float) -> float:
        vt = (m0 * (1 + L)) * (1 - m0 * (1 + L)) * sc if kind == "binary" else s0 ** 2 * sc
        return math.sqrt((s0 ** 2 * sc / wc + vt / wt) / (1 / wc + 1 / wt))

    def sizes(L: float, sc: float = 1.0) -> tuple[int, int, int]:
        n_c = pw.solve_power(effect_size=abs(m0) * L / spread(L, sc), alpha=alpha_sm, power=p["power"], ratio=wt / wc, alternative="larger")
        n_c, n_t = math.ceil(n_c - 1e-9), math.ceil(n_c * wt / wc - 1e-9)
        return n_t, n_c, n_c + (arms - 1) * n_t

    def mde(n_c: float, n_t: float, sc: float = 1.0) -> float:
        top = (1 - m0) / m0 if kind == "binary" else 1e3
        f = lambda L: pw.power(abs(m0) * L / spread(L, sc), nobs1=n_c, alpha=alpha_sm, ratio=n_t / n_c, alternative="larger") - p["power"]  # noqa: E731
        return brentq(f, 1e-9, top, xtol=1e-15, rtol=1e-14)

    if p["solve_for"] == "sample_size":
        L = p["min_lift"]
        n_t, n_c, total = sizes(L)
        n_c0, n_t0 = n_c, n_t
    else:
        n_t0 = p["units_per_variant"] if p["solve_for"] == "mde_for_units" else p["days"] * daily * wt
        n_c0 = n_t0 * wc / wt
        L = mde(n_c0, n_t0)
        n_t, n_c = (n_t0, math.ceil(n_c0 - 1e-9)) if p["solve_for"] == "mde_for_units" else (math.floor(n_t0), math.floor(n_c0))
        total = n_c + (arms - 1) * n_t
    check(cid, "model|||mde", L, want["model|||mde"], 1e-9, "the lift asked or the lift detectable (NormalIndPower / brentq on its power)")
    check(cid, "model|||mde_absolute", abs(m0) * L, want["model|||mde_absolute"], 1e-9, "baseline × lift")
    for key, got in (("n_per_variant", n_t), ("n_control", n_c), ("n_total", total)):
        check(cid, f"model|||{key}", float(got), want[f"model|||{key}"], 0.0, "units: NormalIndPower.solve_power, rounded up")
    if dated:
        days = p["days"] if p["solve_for"] == "mde_for_days" else math.ceil(total / daily - 1e-9)
        check(cid, "model|||days", float(days), want["model|||days"], 0.0, "days = ⌈units / units a day⌉")
        check(cid, "model|||weeks", float(math.ceil(days / 7)), want["model|||weeks"], 0.0, "weeks = ⌈days / 7⌉")
    if keep != 1.0:
        n_t1, _, total1 = sizes(L, keep)
        check(cid, "model|||n_per_variant_cuped", float(n_t1), want["model|||n_per_variant_cuped"], 0.0, "the same with the variance × (1 − r)")
        check(cid, "model|||n_total_cuped", float(total1), want["model|||n_total_cuped"], 0.0, "the same, in all")
        check(cid, "model|||mde_cuped", mde(n_c0, n_t0, keep), want["model|||mde_cuped"], 1e-9, "the lift detectable at these units with the variance × (1 − r)")
        if dated:
            check(cid, "model|||days_cuped", float(math.ceil(total1 / daily - 1e-9)), want["model|||days_cuped"], 0.0, "days with CUPED")
    for key in want:  # the grid's rows the case lists: the sizes at that lift
        if key.startswith("plan|") and key.endswith("||lift"):
            lab, lift = key.split("|")[1], want[key]
            n_t_g, _, total_g = sizes(lift)
            check(cid, f"plan|{lab}||n_per_variant", float(n_t_g), want[f"plan|{lab}||n_per_variant"], 0.0, "the grid's units")
            check(cid, f"plan|{lab}||n_total", float(total_g), want[f"plan|{lab}||n_total"], 0.0, "the grid's units in all")
            if dated:
                check(cid, f"plan|{lab}||days", float(math.ceil(total_g / daily - 1e-9)), want[f"plan|{lab}||days"], 0.0, "the grid's days")
            if keep != 1.0:
                check(cid, f"plan|{lab}||n_per_variant_cuped", float(sizes(lift, keep)[0]), want[f"plan|{lab}||n_per_variant_cuped"], 0.0, "the grid's units with CUPED")


def witness_plan_rule(case: dict[str, Any]) -> None:
    """Kohavi et al.'s rule: two equal arms, α = 0.05 two-sided, power 0.8 need 16 σ² / Δ² units each (the formula's
    2 (1.95996 + 0.84162)² = 15.70, within 2 %), on the case's own history; and the statsmodels two-sided solution, which also
    counts the other tail, differs from the one-tailed formula by about a millionth."""
    import numpy as np
    from statsmodels.stats.power import NormalIndPower

    df = read(case)
    y = df[case["params"]["metric"]].to_numpy(float)
    s0, m0, lift = float(np.std(y, ddof=1)), float(y.mean()), 0.05
    rule = 16 * s0 ** 2 / (m0 * lift) ** 2
    two = NormalIndPower().solve_power(effect_size=m0 * lift / s0, alpha=0.05, power=0.8, ratio=1.0, alternative="two-sided")
    one = 2 * (1.959963984540054 + 0.8416212335729143) ** 2 * s0 ** 2 / (m0 * lift) ** 2
    check("plan_rule", "16σ²/Δ²", one, rule, 0.02, "n per arm = 2 (z_a + z_b)² σ²/Δ² against 16 σ²/Δ²")
    check("plan_rule", "two-sided solve_power", two, one, 1e-5, "statsmodels counts the opposite tail too")


# ── lot E8: the secondary metrics ───────────────────────────────────────────────
def witness_secondary(case: dict[str, Any]) -> None:
    """Each secondary metric's units with pandas (the rows of a unit together, a unit in two variants left out, a unit
    without a value of the metric left out of it), its comparisons with SciPy's Welch t (a number) or statsmodels' Wald z
    (a true/false column), Fieller, ONE multipletests over every metric × every treatment, the decision, the detectable
    lift at alpha / M."""
    import numpy as np
    import pandas as pd
    from scipy import stats
    from statsmodels.stats.multitest import multipletests
    from statsmodels.stats.proportion import confint_proportions_2indep, test_proportions_2indep

    cid, want = case["id"], expected_of(case)
    p = {**DEFAULTS, **case["params"]}
    v, unit, types = p["variant"], p.get("unit"), case["types"]
    df = read(case)
    df = df[df[v].notna()]
    labels = sorted(df[v].unique(), key=lambda s: s.encode("utf-8"))
    ci = labels.index(p["control"])
    tr = [j for j in range(len(labels)) if j != ci]
    if unit:
        nvar = df.groupby(unit)[v].nunique()
        df = df[df[unit].isin(nvar.index[nvar == 1])]
    two, alt = p["alternative"] == "two_sided", p["alternative"]
    res = []
    for col in p["secondary_metrics"]:
        binary = types[col] == "boolean"
        x = df[[v, col] + ([unit] if unit else [])].dropna(subset=[col]).copy()
        x[col] = x[col].map(lambda s: float(str(s).lower() in ("true", "1"))) if binary else x[col].astype(float)
        if unit:
            x = x.groupby(unit).agg({v: "first", col: (lambda s: float((s > 0).any())) if binary else "sum"}).reset_index()
        g = np.array([labels.index(lab) for lab in x[v]])
        y = x[col].to_numpy(float)
        for t in tr:
            yt, yc = y[g == t], y[g == ci]
            if binary:
                r = test_proportions_2indep(yt.sum(), len(yt), yc.sum(), len(yc), method="wald", compare="diff", alternative=ALTERNATIVE[alt])
                res.append({"col": col, "t": t, "binary": True, "yt": yt, "yc": yc, "stat": float(r.statistic), "p": float(r.pvalue)})
            else:
                r = stats.ttest_ind(yt, yc, equal_var=False, alternative=SCIPY_ALT[alt])
                res.append({"col": col, "t": t, "binary": False, "yt": yt, "yc": yc, "stat": float(r.statistic), "p": float(r.pvalue), "tt": r})
    M = len(res)
    how = p["correction"]
    # the correction on expected.json's own p-values (as _corrections: the p-values themselves are compared at 1e-12 below)
    ps = np.array([want[f"secondary|{r['col']}|{labels[r['t']]}|p_value"] for r in res])
    adj = ps if how == "none" else multipletests(ps, method={"holm": "holm", "benjamini_hochberg": "fdr_bh", "bonferroni": "bonferroni"}[how])[1]
    a_int = p["alpha"] / M if how == "bonferroni" else p["alpha"]
    a_cmp = p["alpha"] if how == "none" else p["alpha"] / M
    za = float(stats.norm.ppf(1 - a_cmp / 2) if two else stats.norm.ppf(1 - a_cmp))
    for r, a in zip(res, adj):
        key = f"secondary|{r['col']}|{labels[r['t']]}|"
        yt, yc = r["yt"], r["yc"]
        mt, mc = yt.mean(), yc.mean()
        if r["binary"]:
            vt, vc = mt * (1 - mt) / len(yt), mc * (1 - mc) / len(yc)
            lo, hi = confint_proportions_2indep(yt.sum(), len(yt), yc.sum(), len(yc), method="wald", compare="diff", alpha=a_int if two else 2 * a_int)
            lo, hi = (None if alt == "less" else float(lo)), (None if alt == "greater" else float(hi))
            q = float(stats.norm.ppf(1 - a_int / 2) if two else stats.norm.ppf(1 - a_int))
            what = "statsmodels test_proportions_2indep(wald)"
        else:
            vt, vc = np.var(yt, ddof=1) / len(yt), np.var(yc, ddof=1) / len(yc)
            iv = r["tt"].confidence_interval(confidence_level=1 - a_int)
            lo, hi = _finite(iv.low), _finite(iv.high)
            q = float(stats.t.ppf(1 - a_int / 2, r["tt"].df) if two else stats.t.ppf(1 - a_int, r["tt"].df))
            what = "scipy ttest_ind"
        se = math.sqrt(vt + vc)
        check(cid, key + "statistic", r["stat"], want.get(key + "statistic"), 1e-12, what)
        check(cid, key + "p_value", r["p"], want.get(key + "p_value"), 1e-12, what)
        check(cid, key + "difference_ci_low", lo, want.get(key + "difference_ci_low"), 1e-12, what + ", its interval")
        check(cid, key + "difference_ci_high", hi, want.get(key + "difference_ci_high"), 1e-12, what + ", its interval")
        w = want.get(key + "p_adjusted")
        check(cid, key + "p_adjusted", float(a), w, 1e-15 if w and w > 1e-3 else 1e-12, f"statsmodels multipletests ({how}) over {M}")
        check(cid, key + "mean_treatment", float(mt), want.get(key + "mean_treatment"), 1e-12, "pandas")
        check(cid, key + "mean_control", float(mc), want.get(key + "mean_control"), 1e-12, "pandas")
        check(cid, key + "lift", float(mt / mc - 1), want.get(key + "lift"), 1e-12, "the means")
        check(cid, key + "lift_std_error", math.sqrt(vt / mc ** 2 + mt ** 2 * vc / mc ** 4), want.get(key + "lift_std_error"), 1e-12, "the delta method")
        flo, fhi = fieller(float(mt), float(vt), float(mc), float(vc), q)
        if not two and flo is not None:
            if (alt == "greater") == (mc > 0):
                fhi = None
            else:
                flo = None
        check(cid, key + "lift_ci_low", flo, want.get(key + "lift_ci_low"), 1e-10, "Fieller, transcription")
        check(cid, key + "lift_ci_high", fhi, want.get(key + "lift_ci_high"), 1e-10, "Fieller, transcription")
        check(cid, key + "mde", (za + float(stats.norm.ppf(p["power"]))) * se / abs(mc), want.get(key + "mde"), 1e-9, f"transcription at alpha / {M}")
        # each metric in its own direction (secondary_directions), the primary's where none is given
        own = (p.get("secondary_directions") or {}).get(r["col"])
        higher = bool(p["higher_is_better"]) if own is None else own == "higher"
        dec = ("better" if (mt > mc) == higher else "worse") if a < p["alpha"] else "not_significant"
        said.append(("ok" if dec == want.get(key + "decision") else "DIFF", cid, key + "decision [the adjusted p against alpha, in its direction]",
                     dec, want.get(key + "decision"), 0))


def main(argv: list[str]) -> int:
    if "--engine" in argv:
        witness_engine()
    else:
        for case in EXPECTED["cases"]:
            if case["model"] == "ab_test" and not case.get("refuses") and "reading" in case:
                run = witness_bayes if case["id"].startswith("bayes_") else witness_sequential if case["id"].startswith("seq_") else witness_comparisons
                run(case)
                if case["params"].get("secondary_metrics"):  # lot E8: the primary above, its guardrails here
                    witness_secondary(case)
    if "--engine" not in argv:
        for case in EXPECTED["cases"]:  # lot E5: the planning node
            if case["model"] == "ab_plan" and not case.get("refuses") and "reading" in case:
                witness_plan(case)
                if case["id"] == "plan_continuous_no_date":
                    witness_plan_rule(case)
    width = max(len(s[1]) for s in said)
    for status, case, key, got, want, tol in said:
        print(f"{status:4} {case:{width}} {key}: witness {got!r}, expected {want!r} (tolerance {tol:g})")
    bad = sum(1 for s in said if s[0] != "ok")
    print(f"{len(said)} checks, {bad} differ")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
