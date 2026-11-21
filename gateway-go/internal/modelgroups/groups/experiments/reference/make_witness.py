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



def main(argv: list[str]) -> int:
    if "--engine" in argv:
        witness_engine()
    else:
        for case in EXPECTED["cases"]:
            if case["model"] == "ab_test" and not case.get("refuses") and "reading" in case:
                (witness_bayes if case["id"].startswith("bayes_") else witness_comparisons)(case)
    width = max(len(s[1]) for s in said)
    for status, case, key, got, want, tol in said:
        print(f"{status:4} {case:{width}} {key}: witness {got!r}, expected {want!r} (tolerance {tol:g})")
    bad = sum(1 for s in said if s[0] != "ok")
    print(f"{len(said)} checks, {bad} differ")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
