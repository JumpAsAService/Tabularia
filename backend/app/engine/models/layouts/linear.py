"""The report of a linear regression (docs/specs/statistical-models.md, `linear`): ordinary or
weighted least squares with their standard errors; with a penalty, `penalized.py`'s blocks.

Cards: the rows, R², adjusted R², σ. Coefficients with their tests and intervals, the VIF.
Diagnostics: residuals against fitted values (Breusch–Pagan), the normal Q–Q (Shapiro–Wilk),
scale–location (White), residuals against leverage with the largest Cook's distances named,
the histogram of the residuals (Jarque–Bera), observed against fitted, the coefficients with
their intervals.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import penalized, register
from app.engine.models.layouts import shared_readings as sr


def name(params: dict[str, Any], locale: str) -> str:
    checked = catalog.check_params("linear", params)
    w = c.words("linear", catalog.variant("linear", checked), locale)
    if checked.get("penalty", "none") != "none":
        return penalized.name(w, checked)
    return w["variants"]["wls"] if checked.get("estimation") == "wls" else w["name"]


def about(params: dict[str, Any], locale: str) -> str:
    return c.subject(params)


def points_and_line(id_: str, title: str, line: str, w: dict, under: str | None = None, target: float | None = 0.0,
                    diagonal: bool = False, label: bool = True) -> c.Chart:
    """The points of the chart `id_` (the named ones with their row's number) and its line."""
    f = f'([section] = "point" OR [section] = "curve") AND [level] = "{id_}"'
    s = w["series"]
    series = [{"value": 'IF([section] = "point", [value], NULL)', "as": "point", "name": s["points"],
               **({"label": "[text]"} if label else {})},
              {"value": 'IF([section] = "curve", [value], NULL)', "as": "line", "name": line}]
    options: dict[str, Any] = {"legend": False}
    if target is not None:
        options["target"] = target
    if diagonal:
        options["diagonal"] = True
    return c.xy(id_, title, f, series, options, under)


def residual_charts(w: dict, out_of_fold: bool = False) -> tuple[c.Chart, ...]:
    """Residuals against fitted values and their normal Q–Q (out of fold with a penalty)."""
    ch = w["charts"]
    s = w["series"]
    return (points_and_line("resid_fitted", ch["resid_fitted_oof" if out_of_fold else "resid_fitted"], s["lowess"], w,
                            under=None if out_of_fold else "breusch_pagan"),
            points_and_line("qq", ch["qq_oof" if out_of_fold else "qq"], s["qq_line"], w,
                            under=None if out_of_fold else "shapiro_wilk", target=None, label=False))


def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    checked = catalog.check_params(model, params)
    if checked.get("penalty", "none") != "none":
        return penalized.page(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale))
    w = c.words(model, "unpenalized", locale)
    k, ch, s, ct = w["kpi"], w["charts"], w["series"], w["tables"]
    cards = (c.Card("k_n", k["n"], c.num(c.model_key("n")), "#,##0"),
             c.Card("k_r2", k["r_squared"], c.shown(c.num(c.model_key("r_squared")), "0.0000")),
             c.Card("k_adj_r2", k["adj_r_squared"], c.shown(c.num(c.model_key("adj_r_squared")), "0.0000")),
             c.Card("k_sigma", k["sigma"], c.shown(c.num(c.model_key("sigma"))),
                    under=c.join(k["f_test"], p=c.p_with_stars("f_p_value"))))
    tables = (c.CoefTable("coef", ct["estimates"], ("estimate", "std_error", "statistic", "df", "p_value")),
              c.CoefTable("coef_ci", ct["intervals"], ("ci_low", "ci_high", "vif"), width=30))
    charts = (*residual_charts(w),
              points_and_line("scale_location", ch["scale_location"], s["lowess"], w, under="white", target=None),
              points_and_line("resid_leverage", ch["resid_leverage"], s["lowess"], w, under="cooks_distance"),
              c.histogram("c_residual", ch["residual"], "residual", s["count"], under="jarque_bera"),
              points_and_line("observed_fitted", ch["observed_fitted"], s["lowess"], w, target=None, diagonal=True, label=False),
              # the intercept is left out: on the outcome's scale, it would flatten the slopes to a line
              c.coef_interval("coef_interval", ch["coef_interval"], ("estimate", "ci_low", "ci_high"),
                              (s["estimate"], s["ci_low"], s["ci_high"]), 0.0,
                              filter_='[section] = "coef" AND [level] <> "(Intercept)"'))
    charts = tuple(ch_._replace(wide=True) if ch_.id == "coef_interval" else ch_ for ch_ in charts)
    return c.page(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale), cards=cards,
                  coef_tables=tables, charts=charts, explained=("summary", "assumptions", "coefficients", "fit"))


# the standard errors that are not the classical ones: their method's paragraph (HC0 to HC3 share one)
SE_METHOD = {"hc0": "hc", "hc1": "hc", "hc2": "hc", "hc3": "hc", "hac": "hac", "cluster": "cluster"}


def readings(rows: list[dict], checked: Mapping[str, Any]) -> list[c.ReadingLine]:
    """The explanation (docs/specs/report-explanations.md, «B2»): what R² and the F test say, the checks, the terms'
    tests, the errors of the fit; the method's paragraphs and formulas. With a penalty, `penalized.readings`."""
    if checked.get("penalty", "none") != "none":
        return penalized.readings(rows, checked, "linear")
    k, a = sr.keyed(rows), sr.alpha(checked)
    outcome = str(checked.get("outcome") or "")
    lines: list[c.ReadingLine] = [("summary", None, "summary_linear", {
        "n": sr.model(k, "n"), "outcome": outcome, "r2": sr.model(k, "r_squared"), "adj": sr.model(k, "adj_r_squared")})]
    p = sr.model(k, "f_p_value")
    if p is not None:
        lines.append(("summary", None, "fit_f_reject" if p < a else "fit_f_keep", {
            "f": sr.model(k, "f_statistic"), "df1": sr.model(k, "f_df_1"), "df2": sr.model(k, "f_df_2"), "p": p, "alpha": a,
            "outcome": outcome}))
    lines += sr.assumption_lines(rows, checked)
    coef, _ = sr.coefficient_lines(rows, checked)
    se = str(checked.get("se") or "classical")
    if coef and se in SE_METHOD:
        coef.append(("coefficients", None, "coef_robust", {"kind": se.upper()}))
    lines += coef
    lines.append(("fit", None, "fit_errors", {"rmse": sr.model(k, "rmse"), "mae": sr.model(k, "mae"),
                                              "sigma": sr.model(k, "sigma"), "outcome": outcome}))
    lines += sr.method_lines("method_wls" if checked.get("estimation") == "wls" else "method_ols",
                             "method_se_" + SE_METHOD.get(se, "classical"), "method_checks", "method_vif", "method_p_value")
    lines += sr.formula_lines("formula_ols", "formula_r2", "formula_f", "formula_t",
                              "formula_sandwich" if se in SE_METHOD else "", "formula_vif")
    return lines


register("linear", layout, name, about)
c.READINGS["linear"] = readings
