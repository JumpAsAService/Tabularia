"""The results fixtures of the experiments group (docs/specs/ab-testing.md, «The output»): one table per
node and variant, made from one small imaginary experiment, so that the report lane can be built and
`results.py` can be tried before the runner exists.

    cd backend && python -m tests.models_fixtures ab_test ab_plan

The numbers are the formulas of the spec applied to the experiment's sufficient statistics (an unpooled z
or Welch's t with the normal for its p-value, Fieller's interval, Holm, Pearson's χ², the normal posterior,
the mixture SPRT): consistent with each other, not the runner's own (the real ones replace them, lot E2 on).
Every row the catalogue declares for the variant that a run with these parameters writes is here, and the
`reading` rows list the templates of «Explaining the method in the report» a run says, as `@id name=value`
(the notes' format: a space, a comma and a percent in a value are %20, %2C and %25).
"""
from __future__ import annotations

import math
from datetime import date
from statistics import NormalDist
from typing import Any
from urllib.parse import quote

N = NormalDist()
DAY0 = (date(2026, 3, 2) - date(1970, 1, 1)).days  # the experiment's first day, as the charts' `x_time` read it
DAYS = 14
VARIANTS = ["A", "B", "C"]  # A is the control

# the parameters of each fixture: the variant they make is in the file's name
PARAMS: dict[tuple[str, str], dict[str, Any]] = {
    ("ab_test", "binary"): {"variant": "variant", "control": "A", "metric_type": "binary", "metric": "converted", "unit": "user_id",
                            "date": "entry_day", "correction": "holm", "bayes": True, "sequential": True,
                            "expected_shares": {"A": 1, "B": 1, "C": 1}, "min_lift": 0.02, "output": "variants"},
    ("ab_test", "continuous"): {"variant": "variant", "control": "A", "metric_type": "continuous", "metric": "revenue",
                                "unit": "user_id", "date": "entry_day", "covariate": "revenue_pre", "covariate_missing": "fill",
                                "cap_percentile": 0.999, "correction": "benjamini_hochberg", "bayes": True, "sequential": True,
                                "secondary_metrics": ["converted", "sessions"], "output": "variants"},
    ("ab_test", "ratio"): {"variant": "variant", "control": "A", "metric_type": "ratio", "numerator": "clicks",
                           "denominator": "impressions", "unit": "user_id", "date": "entry_day", "covariate": "clicks_pre",
                           "covariate_missing": "exclude", "correction": "bonferroni", "alternative": "two_sided", "bayes": True,
                           "higher_is_better": True, "expected_shares": {"A": 2, "B": 1, "C": 1}},
    ("ab_plan", "binary"): {"metric_type": "binary", "metric": "converted", "unit": "user_id", "date": "first_visit",
                            "solve_for": "sample_size", "min_lift": 0.02, "arms": 2, "output": "plan"},
    ("ab_plan", "continuous"): {"metric_type": "continuous", "metric": "revenue", "unit": "user_id", "date": "first_visit",
                                "covariate": "revenue_pre", "cap_percentile": 0.999, "solve_for": "mde_for_days", "days": 28,
                                "arms": 3, "correction": "benjamini_hochberg", "traffic_share": 0.5},
    ("ab_plan", "ratio"): {"metric_type": "ratio", "numerator": "clicks", "denominator": "impressions", "unit": "user_id",
                           "date": "first_visit", "solve_for": "mde_for_units", "units_per_variant": 20000, "arms": 4,
                           "control_share": 0.4},
}


def _enc(v: Any) -> str:
    """A value of a note: a number keeps 17 digits, a text its characters with %, space and comma encoded."""
    if isinstance(v, float):
        return repr(v)
    return quote(str(v), safe="").replace("%2F", "/")


def note(id_: str, **values: Any) -> str:
    return "@" + id_ + "".join(f" {k}={_enc(v)}" for k, v in values.items())


class Table:
    def __init__(self) -> None:
        self.rows: list[list[Any]] = []
        self.seen: dict[str, int] = {}

    def add(self, section: str, stat: str, value: Any = None, text: Any = None, level: str | None = None,
            level_2: str | None = None, x0: Any = None, x1: Any = None, index: int | None = None) -> None:
        key = "|".join((section, level or "", level_2 or "", stat)) + ("" if index is None else f"|{index}")
        v = None if value is None else round(float(value), 12) + 0.0
        self.rows.append([section, level, level_2, stat, v, text, x0, x1, len(self.rows), key])


def _stars(p: float) -> str:
    return next((c for t, c in ((0.001, "***"), (0.01, "**"), (0.05, "*"), (0.1, ".")) if p < t), "")


def _p_two(z: float) -> float:
    return 2 * (1 - N.cdf(abs(z)))


def _chi2_p(x: float, df: int) -> float:
    """The upper tail of a χ² with 1 or 2 degrees of freedom (closed forms; a fixture's number)."""
    return math.erfc(math.sqrt(x / 2)) if df == 1 else math.exp(-x / 2) if df == 2 else 0.5 * math.exp(-x / 2)


def _holm(ps: list[float]) -> list[float]:
    order = sorted(range(len(ps)), key=ps.__getitem__)
    out, running = [0.0] * len(ps), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(ps) - rank) * ps[i]))
        out[i] = running
    return out


def _adjust(ps: list[float], how: str) -> list[float]:
    m = len(ps)
    if how == "none":
        return list(ps)
    if how == "bonferroni":
        return [min(1.0, m * p) for p in ps]
    if how == "benjamini_hochberg":
        order = sorted(range(m), key=ps.__getitem__, reverse=True)
        out, running = [0.0] * m, 1.0
        for k, i in enumerate(order):
            running = min(running, ps[i] * m / (m - k))
            out[i] = running
        return out
    return _holm(ps)


def _fieller(mt: float, vt: float, mc: float, vc: float, q: float) -> tuple[float, float]:
    r, g = mt / mc, q * q * vc / (mc * mc)
    half = (q / mc) * math.sqrt(vt * (1 - g) + r * r * vc)
    return (r - half) / (1 - g) - 1, (r + half) / (1 - g) - 1


# ── the imaginary experiment: units, means and variances per variant ───────────────────────────────────────────
def _experiment(metric_type: str, srm_failed: bool) -> dict[str, Any]:
    n_assigned = [12515, 12480, 12390] if not srm_failed else [25100, 11900, 12500]
    if metric_type == "binary":
        conv = [453, 545, 440]
        n = n_assigned
        m = [c / k for c, k in zip(conv, n)]
        v = [p * (1 - p) / k for p, k in zip(m, n)]
        return {"n": n, "n_assigned": n_assigned, "m": m, "v": v, "extra": {"conversions": conv}}
    if metric_type == "continuous":
        n = [k - d for k, d in zip(n_assigned, (31, 28, 33))]
        m = [41.2, 43.1, 40.5]
        sd = [88.0, 91.0, 85.5]
        # adjusted by CUPED: the variance falls by about a quarter
        return {"n": n, "n_assigned": n_assigned, "m": [x - 0.6 * (i == 1) for i, x in enumerate(m)],
                "v": [(s * s * 0.77) / k for s, k in zip(sd, n)], "extra": {"mean_unadjusted": m, "sd": sd,
                "v_unadjusted": [s * s / k for s, k in zip(sd, n)]}}
    n = [k - d for k, d in zip(n_assigned, (210, 190, 205))]  # units without the covariate are excluded
    m = [0.0412, 0.0437, 0.0398]
    return {"n": n, "n_assigned": n_assigned, "m": m, "v": [(0.0016 * mu / 0.04) / k for mu, k in zip(m, n)],
            "extra": {"numerator_sum": [mu * 9.6 * k for mu, k in zip(m, n)], "denominator_sum": [9.6 * k for k in n]}}


def _analysis(metric_type: str, ex: dict[str, Any], alpha: float, how: str) -> dict[str, Any]:
    """The treatments against the control (index 0), with the corrections, the lifts and the power."""
    q = N.inv_cdf(1 - alpha / 2)
    rows = []
    for t in (1, 2):
        d = ex["m"][t] - ex["m"][0]
        se = math.sqrt(ex["v"][t] + ex["v"][0])
        stat = d / se
        p = _p_two(stat)
        lo, hi = _fieller(ex["m"][t], ex["v"][t], ex["m"][0], ex["v"][0], q)
        lift = ex["m"][t] / ex["m"][0] - 1
        lift_se = math.sqrt(ex["v"][t] / ex["m"][0] ** 2 + ex["m"][t] ** 2 * ex["v"][0] / ex["m"][0] ** 4)
        mde = (q + N.inv_cdf(0.8)) * se / abs(ex["m"][0])
        power = N.cdf(0.02 * abs(ex["m"][0]) / se - q) + N.cdf(-0.02 * abs(ex["m"][0]) / se - q)
        df = None
        if metric_type == "continuous":
            df = (ex["v"][t] + ex["v"][0]) ** 2 / (ex["v"][t] ** 2 / (ex["n"][t] - 1) + ex["v"][0] ** 2 / (ex["n"][0] - 1))
        rows.append({"t": t, "d": d, "se": se, "stat": stat, "df": df, "p": p, "lo": lo, "hi": hi, "lift": lift, "lift_se": lift_se,
                     "d_lo": d - q * se, "d_hi": d + q * se, "mde": mde, "power": power})
    adj = _adjust([r["p"] for r in rows], how)
    for r, a in zip(rows, adj):
        r["adj"] = a
        r["decision"] = "better" if a < alpha and r["d"] > 0 else "worse" if a < alpha else "not_significant"
    return {"rows": rows, "q": q}


def _norm_bayes(ex: dict[str, Any], rows: list[dict[str, Any]], alpha: float) -> dict[int, dict[str, float]]:
    """Posterior means ~ N(m, v) (the normal approximation, flat prior): P(beat), the losses, P(best) by a grid."""
    out: dict[int, dict[str, float]] = {}
    sd = [math.sqrt(v) for v in ex["v"]]
    lo, hi = min(m - 8 * s for m, s in zip(ex["m"], sd)), max(m + 8 * s for m, s in zip(ex["m"], sd))
    grid = [lo + (hi - lo) * i / 2000 for i in range(2001)]
    step = (hi - lo) / 2000
    best = [0.0] * 3
    emax_extra = 0.0
    for x in grid:
        cdfs = [NormalDist(m, s).cdf(x) for m, s in zip(ex["m"], sd)]
        pdfs = [NormalDist(m, s).pdf(x) for m, s in zip(ex["m"], sd)]
        for j in range(3):
            prod = 1.0
            for k in range(3):
                if k != j:
                    prod *= cdfs[k]
            best[j] += pdfs[j] * prod * step
        emax_extra += (1 - cdfs[0] * cdfs[1] * cdfs[2]) * step
    emax = lo + emax_extra
    for j in range(3):
        mu, s = ex["m"][j], sd[j]
        d = {"posterior_mean": mu, "posterior_sd": s, "cred_low": NormalDist(mu, s).inv_cdf(alpha / 2),
             "cred_high": NormalDist(mu, s).inv_cdf(1 - alpha / 2), "prob_best": best[j], "expected_loss_best": emax - mu,
             "expected_loss_best_relative": (emax - mu) / ex["m"][0]}
        if j:
            dd, ss = ex["m"][j] - ex["m"][0], math.sqrt(ex["v"][j] + ex["v"][0])
            loss = ss * N.pdf(dd / ss) - dd * N.cdf(-dd / ss)  # E[(mu_c - mu_t)+]
            loss_c = ss * N.pdf(dd / ss) + dd * N.cdf(dd / ss)  # E[(mu_t - mu_c)+]
            d.update(prob_beat_control=N.cdf(dd / ss), expected_loss=loss, expected_loss_relative=loss / ex["m"][0],
                     expected_loss_control=loss_c, expected_loss_control_relative=loss_c / ex["m"][0])
        out[j] = d
    return out


def _mixture(lift: float, v_lift: float, tau: float, alpha: float) -> tuple[float, float, float]:
    """log Λ, and the confidence sequence's half-width, of the mixture SPRT on a relative lift."""
    log_l = 0.5 * math.log(v_lift / (v_lift + tau * tau)) + tau * tau * lift * lift / (2 * v_lift * (v_lift + tau * tau))
    half = math.sqrt(v_lift * (v_lift + tau * tau) / (tau * tau) * (2 * math.log(1 / alpha) + math.log((v_lift + tau * tau) / v_lift)))
    return log_l, lift - half, lift + half


# ── ab_test ────────────────────────────────────────────────────────────────────────────────────────────────────
def build_ab_test(variant: str, params: dict[str, Any], spec: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
    metric_type = variant
    srm_failed = metric_type == "ratio"  # one fixture opens with the SRM band
    alpha, srm_alpha, how = checked["alpha"], checked["srm_alpha"], checked["correction"]
    ex = _experiment(metric_type, srm_failed)
    an = _analysis(metric_type, ex, alpha, how)
    cuped = "covariate" in checked and checked["covariate"] is not None
    cap = checked.get("cap_percentile") is not None
    shares = checked.get("expected_shares") or {}
    weights = [shares.get(v, 1.0) for v in VARIANTS]
    expected = [w / sum(weights) for w in weights]
    total = sum(ex["n_assigned"])
    chi2 = sum((n - total * e) ** 2 / (total * e) for n, e in zip(ex["n_assigned"], expected))
    srm_p = _chi2_p(chi2, 2)
    t = Table()
    decl = spec["sections"]
    better = [r for r in an["rows"] if r["decision"] == "better"]
    worse = [r for r in an["rows"] if r["decision"] == "worse"]
    headline = "srm_failed" if srm_p < srm_alpha else "better" if better else "worse" if worse else "no_difference"
    winners = [VARIANTS[r["t"]] for r in (better or worse)]
    pct = lambda x: f"{x * 100:+.1f} %"  # noqa: E731
    label = {"binary": "conversion rate", "continuous": "revenue per user", "ratio": "click-through rate"}[metric_type]
    metric = params.get("metric") or params.get("numerator")
    formula = {"binary": f"conversion = mean of «{metric}» per user · control «A» · {how}",
               "continuous": f"revenue = mean of «{metric}» per user · control «A» · {how} · CUPED on «{params.get('covariate')}»",
               "ratio": f"ctr = Σ «clicks» / Σ «impressions» · control «A» · {how} · CUPED on «clicks_pre»"}[metric_type]
    model_texts = {"model": "ab_test", "variant": variant, "rows_from": "run", "group_version": "1", "formula": formula,
                   "software": "R 4.6.0; stats 4.6.0; arrow 26.0.1", "metric_type": metric_type, "input_shape": "units",
                   "control": "A", "alternative": "two_sided", "correction": how, "decision": headline,
                   "decision_variants": ",".join(quote(w, safe="") for w in winners),
                   "note_1": note("metric_missing", count=37, variants="A,B")}
    if cuped:
        model_texts["covariate_missing"] = checked["covariate_missing"]
    if cap:
        model_texts["note_2"] = note("cap_applied", percentile=0.999, value=1840.5, count=96)
    n_used = sum(ex["n"])
    model_numbers: dict[str, float] = {"alpha": alpha, "srm_alpha": srm_alpha, "n": n_used, "n_assigned": total,
                                       "n_missing": total - n_used, "n_variants": 3, "n_crossover": 12,
                                       "min_lift": checked["min_lift"], "power": checked["power"],
                                       "loss_threshold": checked.get("loss_threshold"), "sequential_tau": checked.get("sequential_tau"),
                                       "n_looks": DAYS, "first_day": DAY0, "last_day": DAY0 + DAYS - 1}
    if metric_type == "binary":
        model_numbers.update(prior_alpha=1.0, prior_beta=1.0)
    if cuped:
        filled = 4210 if checked["covariate_missing"] == "fill" else 0
        model_numbers.update(theta=0.41, covariate_correlation=0.48, n_covariate_filled=filled,
                             n_covariate_excluded=0 if filled else 605)
    if cap:
        model_numbers.update(cap_value=1840.5, n_capped=96)
    for stat in decl["model"]["texts"]:
        if stat in model_texts:
            t.add("model", stat, text=model_texts[stat])
    for stat in decl["model"]["numbers"]:
        if model_numbers.get(stat) is not None:
            t.add("model", stat, model_numbers[stat])
    # per variant
    sd = ex["extra"].get("sd")
    for j, name in enumerate(VARIANTS):
        se = math.sqrt(ex["v"][j])
        vals: dict[str, float] = {"is_control": 1.0 if j == 0 else 0.0, "n": ex["n"][j], "n_assigned": ex["n_assigned"][j],
                                  "n_missing": ex["n_assigned"][j] - ex["n"][j], "share": ex["n_assigned"][j] / total,
                                  "expected_share": expected[j], "mean": ex["m"][j], "std_error": se,
                                  "ci_low": ex["m"][j] - an["q"] * se, "ci_high": ex["m"][j] + an["q"] * se,
                                  "std_dev": math.sqrt(ex["m"][j] * (1 - ex["m"][j])) if metric_type == "binary" else
                                  (sd[j] if sd else se * math.sqrt(ex["n"][j]))}
        if metric_type == "binary":
            vals["conversions"] = ex["extra"]["conversions"][j]
        if metric_type == "continuous":
            vals.update(skewness=[6.1, 5.8, 6.4][j], n_capped=[41, 55, 0][j] if cap else 0)
        if metric_type == "ratio":
            vals.update(numerator_sum=ex["extra"]["numerator_sum"][j], denominator_sum=ex["extra"]["denominator_sum"][j])
        if cuped:
            vals.update(covariate_mean=[12.3, 12.4, 12.2][j], variance_reduction=0.23 + 0.01 * j,
                        n_covariate_filled=[1410, 1400, 1400][j] if checked["covariate_missing"] == "fill" else 0,
                        n_covariate_excluded=0 if checked["covariate_missing"] == "fill" else [210, 190, 205][j])
            if checked["covariate_missing"] == "fill":
                vals["covariate_fill_value"] = [12.3, 12.4, 12.2][j]
            if metric_type == "continuous":
                vals.update(mean_unadjusted=ex["extra"]["mean_unadjusted"][j], std_error_unadjusted=math.sqrt(ex["extra"]["v_unadjusted"][j]))
            else:
                vals.update(mean_unadjusted=ex["m"][j] * 1.002, std_error_unadjusted=se * 1.15)
        for stat in decl["variant"]["numbers"]:
            if stat in vals:
                t.add("variant", stat, vals[stat], level=name)
    # per treatment
    for r in an["rows"]:
        name = VARIANTS[r["t"]]
        vals = {"difference": r["d"], "difference_std_error": r["se"], "difference_ci_low": r["d_lo"], "difference_ci_high": r["d_hi"],
                "lift": r["lift"], "lift_std_error": r["lift_se"], "lift_ci_low": r["lo"], "lift_ci_high": r["hi"],
                "statistic": r["stat"], "p_value": r["p"], "p_adjusted": r["adj"], "mde": r["mde"], "power_at_min_lift": r["power"]}
        if r["df"] is not None:
            vals["df"] = r["df"]
        for stat in decl["comparison"]["numbers"]:
            if stat in vals:
                t.add("comparison", stat, vals[stat], level=name, level_2="A")
        t.add("comparison", "significance", text=_stars(r["p"]), level=name, level_2="A")
        t.add("comparison", "p_adjusted_significance", text=_stars(r["adj"]), level=name, level_2="A")
        t.add("comparison", "decision", text=r["decision"], level=name, level_2="A")
        above = r["lo"] > checked["min_lift"]
        t.add("comparison", "min_lift_position", text="above" if above else "straddles" if r["hi"] > checked["min_lift"] else "below",
              level=name, level_2="A")
    # Bayes
    if checked["bayes"]:
        post = _norm_bayes(ex, an["rows"], alpha)
        for j, name in enumerate(VARIANTS):
            for stat in decl["bayes"]["numbers"]:
                if stat in post[j]:
                    t.add("bayes", stat, post[j][stat], level=name)
            if j:
                rel = post[j]["expected_loss_relative"]
                t.add("bayes", "loss_position", text="below" if rel < checked.get("loss_threshold", 0.001) else "above", level=name)
    # sequential
    seq: dict[int, dict[str, list[float]]] = {}
    if checked["sequential"]:
        tau = checked.get("sequential_tau", 0.05)
        adj_p: list[float] = []
        for r in an["rows"]:
            lifts, lows, highs, logs = [], [], [], []
            for look in range(1, DAYS + 1):
                frac = look / DAYS
                v_l = (r["lift_se"] ** 2) / frac
                lift = r["lift"] + 0.03 * math.cos(look * 1.7) * (1 - frac)
                log_l, lo, hi = _mixture(lift, v_l, tau, alpha)
                lifts.append(lift)
                lows.append(lo)
                highs.append(hi)
                logs.append(log_l)
            p_seq, running = [], 1.0
            for log_l in logs:
                running = min(running, math.exp(-log_l) if log_l > 0 else 1.0)
                p_seq.append(running)
            seq[r["t"]] = {"y": lifts, "low": [max(a, b) for a, b in zip(lows, [-9] * DAYS)], "high": highs, "p": p_seq, "log": logs}
            adj_p.append(p_seq[-1])
        adj = _adjust(adj_p, how)
        for r, a in zip(an["rows"], adj):
            name, s = VARIANTS[r["t"]], seq[r["t"]]
            for stat, value in (("p_value", s["p"][-1]), ("p_adjusted", a), ("log_lambda", s["log"][-1]),
                                ("cs_low", s["low"][-1]), ("cs_high", s["high"][-1])):
                t.add("sequential", stat, value, level=name)
            crossing = next((i for i, p in enumerate(s["p"]) if p < alpha), None)
            if crossing is not None:
                t.add("sequential", "first_crossing", DAY0 + crossing, level=name)
            t.add("sequential", "significance", text=_stars(a), level=name)
            t.add("sequential", "decision", text="better" if a < alpha and r["d"] > 0 else "worse" if a < alpha else "not_significant", level=name)
    # secondary metrics: the first treatment's comparison of each, adjusted among themselves
    if checked["secondary_metrics"]:
        sec = []
        for k, col in enumerate(checked["secondary_metrics"]):
            for r in an["rows"]:
                p = min(1.0, r["p"] * (1.7 + 0.9 * k) if k else r["p"] * 0.4)
                sec.append((col, VARIANTS[r["t"]], p, r["d"] * (1 if k == 0 else -0.5), r["se"]))
        adj = _adjust([s[2] for s in sec], how)
        for (col, treatment, p, d, se), a in zip(sec, adj):
            kind = "binary" if col == "converted" else "continuous"
            vals = {"difference": d, "difference_std_error": se, "difference_ci_low": d - an["q"] * se, "difference_ci_high": d + an["q"] * se,
                    "lift": d / 10, "lift_std_error": se / 10, "lift_ci_low": (d - an["q"] * se) / 10, "lift_ci_high": (d + an["q"] * se) / 10,
                    "statistic": d / se, "p_value": p, "p_adjusted": a, "mde": 0.031, "power_at_min_lift": 0.62,
                    "mean_treatment": 10 + d, "mean_control": 10.0}
            if kind == "continuous":
                vals["df"] = 24000.0
            for stat in decl["secondary"]["numbers"]:
                if stat in vals:
                    t.add("secondary", stat, vals[stat], level=col, level_2=treatment)
            t.add("secondary", "significance", text=_stars(p), level=col, level_2=treatment)
            t.add("secondary", "p_adjusted_significance", text=_stars(a), level=col, level_2=treatment)
            t.add("secondary", "decision", text="worse" if a < alpha and d < 0 else "better" if a < alpha else "not_significant",
                  level=col, level_2=treatment)
            t.add("secondary", "min_lift_position", text="straddles", level=col, level_2=treatment)
            t.add("secondary", "metric_type", text=kind, level=col, level_2=treatment)
    # the checks
    for check in decl["assumption"]["levels"]:
        if check == "covariate_balance" and not cuped or check == "covariate_missing_balance" and not cuped:
            continue
        if check == "crossover" and not checked.get("unit"):
            continue
        stat_p = {"srm": srm_p, "covariate_balance": 0.62, "covariate_missing_balance": 0.41}.get(check)
        verdict = "ok"
        if check == "srm":
            verdict = "failed" if srm_p < srm_alpha else "ok"
            level_alpha = srm_alpha
        else:
            level_alpha = alpha
        if check == "crossover":
            verdict = "failed"
        t.add("assumption", "h0", text=f"placeholder H0 of {check}", level=check)
        if check == "srm":
            t.add("assumption", "statistic", chi2, level=check)
            t.add("assumption", "df", 2, level=check)
        elif stat_p is not None:
            t.add("assumption", "statistic", 0.48 if check == "covariate_balance" else 1.8, level=check)
            t.add("assumption", "df", 2, level=check)
        if stat_p is not None:
            t.add("assumption", "p_value", stat_p, level=check)
            t.add("assumption", "significance", text=_stars(stat_p) if stat_p < level_alpha else "", level=check)
            t.add("assumption", "threshold", level_alpha, level=check)
        if check == "crossover":
            t.add("assumption", "count", 12, level=check)
        if check == "variance_zero":
            t.add("assumption", "value", 0, level=check)
        t.add("assumption", "verdict", text=verdict, level=check)
        t.add("assumption", "advice", text=f"placeholder advice of {check}", level=check)
        t.add("assumption", "advice_id", text=f"{check}_{'failed' if verdict == 'failed' else 'ok'}", level=check)
    # curves
    for j, name in enumerate(VARIANTS):
        for d in range(DAYS):
            t.add("curve", "y", (ex["n_assigned"][j] * (d + 1) / DAYS) / (total * (d + 1) / DAYS), level="srm_share", level_2=name,
                  x0=DAY0 + d, index=d)
    for j, name in enumerate(VARIANTS):
        for d in range(DAYS):
            t.add("curve", "y", ex["n_assigned"][j] / DAYS * (1 + 0.2 * math.sin(d * 0.9 + j)), level="daily_units", level_2=name,
                  x0=DAY0 + d, index=d)
    if checked["sequential"]:
        for r in an["rows"]:
            name, s = VARIANTS[r["t"]], seq[r["t"]]
            for stat in ("y", "low", "high"):
                for d in range(DAYS):
                    t.add("curve", stat, s[stat][d], level="sequential_lift", level_2=name, x0=DAY0 + d, index=d)
            for d in range(DAYS):
                t.add("curve", "y", s["p"][d], level="sequential_p", level_2=name, x0=DAY0 + d, index=d)
    if checked["bayes"]:
        lo = min(m - 5 * math.sqrt(v) for m, v in zip(ex["m"], ex["v"]))
        hi = max(m + 5 * math.sqrt(v) for m, v in zip(ex["m"], ex["v"]))
        for j, name in enumerate(VARIANTS):
            dist = NormalDist(ex["m"][j], math.sqrt(ex["v"][j]))
            for i in range(40):
                x = lo + (hi - lo) * i / 39
                t.add("curve", "y", dist.pdf(x), level="posterior", level_2=name, x0=x, index=i)
    for r in an["rows"]:
        name = VARIANTS[r["t"]]
        for i in range(25):
            lift = 0.001 + 0.06 * i / 24
            t.add("curve", "y", N.cdf(lift / (r["mde"] / (an["q"] + N.inv_cdf(0.8))) - an["q"]), level="power", level_2=name, x0=lift, index=i)
    if "hist" in decl:
        edges = [None] + [i * 12.0 for i in range(31)] + [None]
        for name in VARIANTS:
            for i in range(32):
                lo_e, hi_e = (None if i == 0 else edges[i], None if i == 31 else edges[i + 1])
                t.add("hist", "count", round(2600 * math.exp(-i / 5)) + (i == 31) * 40, level="metric", level_2=name,
                      x0=lo_e if lo_e is not None else 0.0, x1=hi_e if hi_e is not None else 372.0, index=i)
    # the reading: the templates a run says, @id name=value (docs/specs/ab-testing.md, «Explaining the method in the report»)
    blocks = _reading(metric_type, ex, an, checked, srm_p, chi2, expected, headline, winners, cuped, cap, metric, label, pct, seq)
    allowed = decl["reading"]["levels"]
    for block, level_2, lines in blocks:
        assert block in allowed, block
        for k, line in enumerate(lines, 1):
            t.add("reading", f"line_{k}", text=line, level=block, level_2=level_2)
    return {"model": "ab_test", "variant": variant, "params": params, "rows": t.rows}


def _reading(metric_type, ex, an, checked, srm_p, chi2, expected, headline, winners, cuped, cap, metric, label, pct, seq):  # noqa: ANN001
    alpha, how = checked["alpha"], checked["correction"]
    best = max(an["rows"], key=lambda r: r["lift"])
    out: list[tuple[str, str | None, list[str]]] = []
    b = VARIANTS[best["t"]]
    if metric_type == "binary":
        counts = note("decision_counts", variant=b, units=ex["n"][best["t"]], converted=ex["extra"]["conversions"][best["t"]],
                      rate=ex["m"][best["t"]], control="A", units_control=ex["n"][0], rate_control=ex["m"][0])
    else:
        counts = note("decision_means", variant=b, units=ex["n"][best["t"]], value=ex["m"][best["t"]], control="A",
                      units_control=ex["n"][0], value_control=ex["m"][0], metric=metric)
    out.append(("decision", None, [note(f"decision_{'no_difference' if headline == 'no_difference' else 'better' if headline != 'worse' else 'worse'}",
                                        variant=b, control="A", metric=metric, lift=best["lift"], lift_low=best["lo"], lift_high=best["hi"],
                                        p_adjusted=best["adj"], correction=how, alpha=alpha), counts,
                                   note("decision_interval", lift_low=best["lo"], lift_high=best["hi"], conf=1 - alpha)]))
    out.append(("srm", None, [note("srm_failed" if srm_p < checked["srm_alpha"] else "srm_ok", shares=",".join(f"{e:.4f}" for e in expected),
                                   observed=",".join(f"{n / sum(ex['n_assigned']):.4f}" for n in ex["n_assigned"]), p=srm_p,
                                   srm_alpha=checked["srm_alpha"], chi2=chi2)]))
    for r in an["rows"]:
        name = VARIANTS[r["t"]]
        sig = r["p"] < alpha
        lines = [note("comparison_significant" if sig else "comparison_not_significant", variant=name, control="A", p=r["p"], alpha=alpha)]
        if len(an["rows"]) > 1:
            lines.append(note(f"correction_{how}", p=r["p"], p_adjusted=r["adj"], treatments=2))
        out.append(("comparison", name, lines))
    if cuped:
        lines = [note("cuped_reduced", covariate=checked["covariate"], reduction=0.23, theta=0.41, correlation=0.48)]
        lines.append(note("cuped_filled", count=4210) if checked["covariate_missing"] == "fill" else note("cuped_excluded", count=605))
        out.append(("cuped", None, lines))
    if cap:
        out.append(("cap", None, [note("cap_applied", percentile=0.999, value=1840.5, count=96, variants="A,B")]))
    if checked["bayes"]:
        for r in an["rows"]:
            name = VARIANTS[r["t"]]
            out.append(("bayes", name, [note("bayes_beat", variant=name, control="A", probability=N.cdf(r["d"] / r["se"])),
                                        note("bayes_loss", variant=name, loss=r["se"] * 0.1, threshold=checked.get("loss_threshold", 0.001))]))
    if checked["sequential"]:
        for r in an["rows"]:
            name = VARIANTS[r["t"]]
            out.append(("sequential", name, [note("sequential_read", variant=name, p=seq[r["t"]]["p"][-1], alpha=alpha, day=DAY0 + 9),
                                             note("sequential_snapshot")]))
    out.append(("power", None, [note("power_detectable", mde=max(r["mde"] for r in an["rows"]), power=checked["power"], min_lift=checked["min_lift"])]))
    if checked["secondary_metrics"]:
        out.append(("secondary", None, [note("secondary_worse", metric=checked["secondary_metrics"][-1], variant="B", p_adjusted=0.004)]))
    method = ["decision", "comparison", "srm", "power", *(["cuped"] if cuped else []), *(["cap"] if cap else []),
              *(["bayes"] if checked["bayes"] else []), *(["sequential"] if checked["sequential"] else []),
              *(["secondary"] if checked["secondary_metrics"] else [])]
    out.append(("method", None, [note(f"method_{m}") for m in method]))
    formulas = [("formula_z" if metric_type == "binary" else "formula_welch" if metric_type == "continuous" else "formula_delta"),
                "formula_fieller", f"formula_{'holm' if how == 'holm' else 'bh' if how == 'benjamini_hochberg' else 'bonferroni'}",
                "formula_srm", *(["formula_cuped"] if cuped else []), *(["formula_beta" if metric_type == "binary" else "formula_normal"]
                                                                      if checked["bayes"] else []),
                *(["formula_mixture"] if checked["sequential"] else [])]
    out.append(("formulas", None, [note(f) for f in formulas]))
    return out


# ── ab_plan ────────────────────────────────────────────────────────────────────────────────────────────────────
def build_ab_plan(variant: str, params: dict[str, Any], spec: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
    metric_type = variant
    arms, alpha, power = checked["arms"], checked["alpha"], checked["power"]
    how = checked["correction"]
    a1 = alpha / (arms - 1) if how != "none" else alpha
    za, zb = N.inv_cdf(1 - a1 / 2), N.inv_cdf(power)
    p0 = {"binary": 0.036, "continuous": 41.2, "ratio": 0.0412}[metric_type]
    sd0 = math.sqrt(p0 * (1 - p0)) if metric_type == "binary" else {"continuous": 88.0, "ratio": 0.35}[metric_type]
    cuped = checked.get("covariate") is not None
    rho = 0.48 if cuped else 0.0
    wc = checked.get("control_share") or 1 / arms
    wt = (1 - wc) / (arms - 1)

    def n_total(lift: float, var_scale: float = 1.0) -> float:
        delta = p0 * lift
        s_t = math.sqrt(((p0 * (1 + lift)) * (1 - p0 * (1 + lift))) if metric_type == "binary" else sd0 * sd0)
        s_c = sd0
        return (za + zb) ** 2 * (s_c * s_c * var_scale / wc + s_t * s_t * var_scale / wt) / (delta * delta)

    daily = 5300.0 * checked.get("traffic_share", 1.0)
    min_lift = checked["min_lift"] if "min_lift" in checked else 0.02
    nt = n_total(min_lift)
    nt_c = n_total(min_lift, 1 - rho * rho)
    lifts = sorted({min_lift * 4 ** (-1 + 2 * i / 29) for i in range(30)} | {min_lift})
    t = Table()
    decl = spec["sections"]
    texts = {"model": "ab_plan", "variant": variant, "rows_from": "run", "group_version": "1",
             "formula": f"plan for {metric_type} metric · {arms} variants · {how}", "software": "R 4.6.0; stats 4.6.0; arrow 26.0.1",
             "metric_type": metric_type, "input_shape": "units", "solve_for": checked["solve_for"], "alternative": "two_sided",
             "correction": how, "history_short": note("history_short", history_days=56, planned_days=math.ceil(nt / daily))}
    if cuped:
        texts["covariate_missing"] = checked["covariate_missing"]
    numbers = {"alpha": alpha, "n": 40000, "n_missing": 112, "min_lift": min_lift, "arms": arms, "control_share": wc,
               "traffic_share": checked.get("traffic_share", 1.0), "baseline_mean": p0, "baseline_sd": sd0, "n_history": 40000,
               "history_days": 56, "daily_units": daily, "alpha_per_comparison": a1, "n_per_variant": math.ceil(nt * wt),
               "n_control": math.ceil(nt * wc), "n_total": math.ceil(nt), "days": math.ceil(nt / daily),
               "weeks": math.ceil(math.ceil(nt / daily) / 7), "mde": min_lift, "mde_absolute": p0 * min_lift, "power": power}
    if cuped:
        numbers.update(covariate_correlation=rho, variance_reduction=rho * rho, n_covariate_filled=4210, n_covariate_excluded=0,
                       n_per_variant_cuped=math.ceil(nt_c * wt), n_total_cuped=math.ceil(nt_c), days_cuped=math.ceil(nt_c / daily),
                       mde_cuped=min_lift * math.sqrt(1 - rho * rho))
    if metric_type == "continuous" and checked.get("cap_percentile") is not None:
        numbers.update(cap_value=1840.5, n_capped=96)
    for stat in decl["model"]["texts"]:
        if stat in texts:
            t.add("model", stat, text=texts[stat])
    for stat in decl["model"]["numbers"]:
        if stat in numbers:
            t.add("model", stat, numbers[stat])
    for lift in lifts:
        label = repr(round(lift, 6))
        total = n_total(lift)
        vals = {"lift": lift, "mde_absolute": p0 * lift, "n_per_variant": math.ceil(total * wt), "n_control": math.ceil(total * wc),
                "n_total": math.ceil(total), "days": math.ceil(total / daily), "weeks": math.ceil(math.ceil(total / daily) / 7),
                "asked": 1.0 if lift == min_lift else 0.0}
        if cuped:
            vals.update(n_per_variant_cuped=math.ceil(n_total(lift, 1 - rho * rho) * wt), days_cuped=math.ceil(n_total(lift, 1 - rho * rho) / daily))
        for stat in decl["plan"]["numbers"]:
            if stat in vals:
                t.add("plan", stat, vals[stat], level=label)
    for i in range(30):
        lift = lifts[i]
        t.add("curve", "y", N.cdf((p0 * lift) / (sd0 * math.sqrt(1 / (nt * wc) + 1 / (nt * wt))) - za), level="power_lift", x0=lift, index=i)
        t.add("curve", "y", lift * math.sqrt(nt / max(1.0, 7 * (i + 1) * daily)), level="mde_days", x0=7.0 * (i + 1), index=i)
        t.add("curve", "y", n_total(lift), level="n_lift", x0=lift, index=i)
    for i in range(32):
        t.add("hist", "count", round(2600 * math.exp(-i / 5)), level="metric", x0=i * 12.0, x1=(i + 1) * 12.0, index=i)
    lines = {
        "plan": [note("plan_size", units=math.ceil(nt * wt), total=math.ceil(nt), days=math.ceil(nt / daily), daily=daily, lift=min_lift,
                      power=power, alpha=alpha, baseline=p0)],
        "baseline": [note("baseline_read", mean=p0, sd=sd0, history_days=56, units=40000)],
        "correction": [note(f"correction_{how}", arms=arms, alpha=alpha, alpha_per_comparison=a1)],
        "method": [note("method_plan"), note("method_power"), note("method_weeks")],
        "formulas": [note("formula_sample_size")],
    }
    if cuped:
        lines["cuped"] = [note("cuped_plan", reduction=rho * rho, units=math.ceil(nt_c), days=math.ceil(nt_c / daily))]
    for block, ls in lines.items():
        assert block in decl["reading"]["levels"], block
        for k, line in enumerate(ls, 1):
            t.add("reading", f"line_{k}", text=line, level=block)
    return {"model": "ab_plan", "variant": variant, "params": params, "rows": t.rows}


def build(model: str, variant: str, params: dict[str, Any], spec: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
    return (build_ab_test if model == "ab_test" else build_ab_plan)(variant, params, spec, checked)
