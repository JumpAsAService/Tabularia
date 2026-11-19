"""The report of a linear regression (docs/specs/statistical-models.md, `linear`): ordinary or
weighted least squares with their standard errors; with a penalty, `penalized.py`'s blocks.

Cards: the rows, R², adjusted R², σ. Coefficients with their tests and intervals, the VIF.
Diagnostics: residuals against fitted values (Breusch–Pagan), the normal Q–Q (Shapiro–Wilk),
scale–location (White), residuals against leverage with the largest Cook's distances named,
the histogram of the residuals (Jarque–Bera), observed against fitted, the coefficients with
their intervals.
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import penalized, register


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
              c.coef_interval("coef_interval", ch["coef_interval"], ("estimate", "ci_low", "ci_high"),
                              (s["estimate"], s["ci_low"], s["ci_high"]), 0.0))
    charts = tuple(ch_._replace(wide=True) if ch_.id == "coef_interval" else ch_ for ch_ in charts)
    return c.page(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale), cards=cards,
                  coef_tables=tables, charts=charts)


register("linear", layout, name, about)
