"""The report of a Cox proportional hazards model (docs/specs/statistical-models.md, `cox`).

Cards: rows and events, the concordance, the likelihood-ratio test's p-value, the AIC.
Coefficients: log hazard ratios with their tests (the robust standard errors when chosen), the
hazard ratios with their intervals, the VIF. Diagnostics: the hazard ratios on a logarithmic
axis; the scaled Schoenfeld residuals of each predictor against time with their smooth and its
band (proportional hazards; a category's columns are one series, as cox.zph tests them); the
martingale residuals of the null model against each numeric predictor (its form); deviance
residuals against the linear predictor (influence); the survival at the fit's means, a curve a
stratum.

Without strata the runner names the one curve «all»: the page says it in its language.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.engine.models.layouts import common as c
from app.engine.models.layouts import register
from app.engine.models.layouts import shared_readings as sr
from app.engine.stats.catalog import label_of

MAX_TERMS_CHARTED = 12  # the Schoenfeld and martingale charts drawn, one a predictor
ALL = "all"  # the runner's name of the one stratum without strata


def name(params: dict[str, Any], locale: str) -> str:
    return c.words("cox", "default", locale)["name"]


def about(params: dict[str, Any], locale: str) -> str:
    time, event, value = params.get("time"), params.get("event"), params.get("event_value")
    if params.get("start") and time:  # start–stop rows: the interval
        time = f"{params['start']}–{time}"
    left = f"{time}, {event} = {label_of(value)}" if time and event and value is not None else str(time or "")
    return c.subject({**params, "outcome": left})


def _schoenfeld(w: dict, params: dict[str, Any]) -> tuple[c.Chart, ...]:
    """A Schoenfeld chart for each predictor."""
    ch, s = w["charts"], w["series"]
    out = []
    for i, term in enumerate(list(params.get("predictors") or [])[:MAX_TERMS_CHARTED]):
        t = c.q(str(term))
        f = f'([section] = "point" OR [section] = "curve") AND [level] = "schoenfeld" AND [level_2] = {t}'
        out.append(c.xy(f"schoenfeld_{i}", ch["schoenfeld"].replace("{term}", str(term)), f, [
            {"value": 'IF([section] = "point", [value], NULL)', "as": "point", "name": s["residuals"]},
            {"value": 'IF([section] = "curve" AND [stat] = "smooth", [value], NULL)', "as": "line", "name": s["smooth"]},
            {"value": 'IF([section] = "curve" AND [stat] = "lower", [value], NULL)', "as": "band_low"},
            {"value": 'IF([section] = "curve" AND [stat] = "upper", [value], NULL)', "as": "band_high"},
        ], {"target": 0, "legend": False}, under=f"schoenfeld|{term}"))
    return tuple(out)


def _martingale(w: dict, params: dict[str, Any]) -> tuple[c.Chart, ...]:
    """A martingale chart for each numeric predictor (a categorical one has none: its place,
    beside its Schoenfeld chart, stays empty)."""
    ch, s = w["charts"], w["series"]
    out = []
    for i, term in enumerate(list(params.get("predictors") or [])[:MAX_TERMS_CHARTED]):
        t = c.q(str(term))
        f = f'([section] = "point" OR [section] = "curve") AND [level] = "martingale" AND [level_2] = {t}'
        out.append(c.xy(f"martingale_{i}", ch["martingale"].replace("{term}", str(term)), f, [
            {"value": 'IF([section] = "point", [value], NULL)', "as": "point", "name": s["residuals"]},
            {"value": 'IF([section] = "curve", [value], NULL)', "as": "line", "name": s["lowess"]},
        ], {"target": 0, "legend": False}, under=f"martingale_form|{term}"))
    return tuple(out)


def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    w = c.words(model, "default", locale)
    k, ch, s, ct = w["kpi"], w["charts"], w["series"], w["tables"]
    cards = (c.Card("k_n", k["n_events"], f'{c.shown(c.num(c.model_key("n")), "#,##0")} & " / " & '
                                          f'{c.shown(c.num(c.model_key("events")), "#,##0")}'),
             c.Card("k_concordance", k["concordance"], c.shown(c.num(c.model_key("concordance")), "0.000"),
                    under=c.join(k["se"], se=c.shown(c.num(c.model_key("concordance_se")), "0.000"))),
             c.Card("k_lr", k["lr_p"], c.p_with_stars("lr_p_value")),
             c.Card("k_aic", k["aic"], c.shown(c.num(c.model_key("aic")))))
    robust = params.get("se") == "robust"
    estimates = ("estimate", "std_error", *(("robust_se",) if robust else ()), "statistic", "p_value")
    tables = (c.CoefTable("coef", ct["estimates"], estimates, width=22 if robust else 26),
              c.CoefTable("coef_hr", ct["hazard_ratios"], ("hazard_ratio", "hr_ci_low", "hr_ci_high", "vif"), width=28))
    hr = c.coef_interval("hazard_ratio_interval", ch["hazard_ratio_interval"], ("hazard_ratio", "hr_ci_low", "hr_ci_high"),
                         (s["hazard_ratio"], s["ci_low"], s["ci_high"]), 1.0, log=True)._replace(wide=True)
    deviance = c.xy("deviance_linear", ch["deviance_linear"], '[section] = "point" AND [level] = "deviance_linear"',
                    [{"value": "[value]", "as": "point", "name": s["residuals"], "label": "[text]"}], {"target": 0, "legend": False},
                    under="dfbeta")
    adjusted = c.xy("adjusted_survival", ch["adjusted_survival"], '[section] = "curve" AND [level] = "adjusted_survival"',
                    [c.mark("surv", "step")], {"min": 0, "max": 1},
                    split_by=f'IF([level_2] = "{ALL}", {c.q(w["all"])}, [level_2])', wide=True)
    return c.page(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale), cards=cards,
                  coef_tables=tables, charts=(hr, adjusted, *_by_predictor(w, params), deviance),
                  explained=("summary", "assumptions", "coefficients"))


def _by_predictor(w: dict, params: dict[str, Any]) -> tuple[c.Chart, ...]:
    """Each predictor's row: its Schoenfeld chart, and beside it its martingale chart (none for
    a category), so that a category leaves half a row empty, not a whole one."""
    return tuple(chart for pair in zip(_schoenfeld(w, params), _martingale(w, params)) for chart in pair)


def readings(rows: list[dict], checked: Mapping[str, Any]) -> list[c.ReadingLine]:
    """The explanation (docs/specs/report-explanations.md, «B2»): the concordance and the likelihood-ratio test; the
    checks (Schoenfeld's among them); the terms' tests and the first significant term's hazard ratio; the method's
    paragraphs and formulas."""
    k, a = sr.keyed(rows), sr.alpha(checked)
    lines: list[c.ReadingLine] = [("summary", None, "summary_cox", {
        "n": sr.model(k, "n"), "events": sr.model(k, "events"), "concordance": sr.model(k, "concordance"),
        "se": sr.model(k, "concordance_se")})]
    p = sr.model(k, "lr_p_value")
    if p is not None:
        lines.append(("summary", None, "fit_lr_reject" if p < a else "fit_lr_keep", {
            "lr": sr.model(k, "lr_statistic"), "df": sr.model(k, "lr_df"), "p": p, "alpha": a}))
    lines += sr.assumption_lines(rows, checked)
    coef, significant = sr.coefficient_lines(rows, checked)
    robust = checked.get("se") == "robust"
    if coef and robust:
        coef.append(("coefficients", None, "coef_robust", {}))
    lines += coef + sr.ratio_line(rows, checked, significant, ("hazard_ratio", "hr_ci_low", "hr_ci_high"), "coef_hazard")
    lines += sr.method_lines("method_cox", "method_ties_" + str(checked.get("ties") or "efron"),
                             "method_se_robust" if robust else "", "method_schoenfeld", "method_checks", "method_p_value")
    lines += sr.formula_lines("formula_cox", "formula_partial_likelihood", "formula_hr", "formula_schoenfeld")
    return lines


register("cox", layout, name, about)
c.READINGS["cox"] = readings
