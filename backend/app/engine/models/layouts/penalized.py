"""The report of a penalized regression, linear or logistic (docs/specs/statistical-models.md,
`penalty`): ridge, lasso, elastic net, λ chosen by cross-validation. No standard error,
p-value or interval (inference after a penalized selection is out of scope, and the page
says so); the choice of λ is the check, its curve and the coefficients' paths its charts.

Cards: linear — the rows, λ, the terms selected, R² out of fold; logistic — rows and events,
the AUC out of fold, λ, the Brier score. Charts: the cross-validation curve with its band
and the chosen λ, the paths of the coefficients, the coefficients at the chosen λ; then the
residuals out of fold (linear) or the ROC, calibration, probabilities and confusion matrix
out of fold (logistic).
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c


def name(w: dict, checked: dict[str, Any]) -> str:
    """«Linear regression — lasso»."""
    return w["name"] + " — " + w["penalties"][checked["penalty"]]


def cv_charts(w: dict) -> tuple[c.Chart, ...]:
    ch, s = w["charts"], w["series"]
    cv = c.xy("cv", ch["cv"], '[section] = "cv"', [
        c.mark("cv_mean", "line", s["cv_mean"]), c.mark("cv_mean", "point"),
        c.mark("cv_low", "band_low", s["cv_band"]), c.mark("cv_high", "band_high"),
    ], {"target_x": [c.num(c.model_key("log_lambda"))], "legend": False}, under="lambda_choice")
    paths = c.xy("paths", ch["paths"], '[section] = "curve" AND [level] = "paths"', [c.mark("y", "line")],
                 {"target_x": [c.num(c.model_key("log_lambda"))], "target": 0}, split_by="[level_2]", wide=True)
    selected = c.Chart("coef_selected", "bar", ch["coef_selected"], {
        "filter": '[section] = "coef" AND [stat] = "estimate"', "category": c.term(),
        "series": [{"value": "SUM([value])", "name": s["estimate"]}],
        "options": {"labels": True, "format": "0.###", "legend": False},
        "visible": c.exists('[section] = "coef" AND [stat] = "estimate"')})
    return cv, paths, selected


def page(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None, *,
         name: str, about: str) -> dict:
    from app.engine.models.layouts import linear, logistic

    checked = catalog.check_params(model, params)
    w = c.words(model, "penalized", locale)
    k, ct = w["kpi"], w["tables"]
    lam = c.Card("k_lambda", k["lambda"], c.shown(c.num(c.model_key("lambda"))),
                 under=c.join(k["log_lambda"], x=c.shown(c.num(c.model_key("log_lambda")), "0.###")))
    if model == "linear":
        cards = (c.Card("k_n", k["n"], c.num(c.model_key("n")), "#,##0"), lam,
                 c.Card("k_selected", k["n_selected"], c.num(c.model_key("n_selected")), "#,##0"),
                 c.Card("k_r2", k["r_squared_oof"], c.shown(c.num(c.model_key("r_squared")), "0.0000")))
        stats_ = ("estimate", "selected")
        charts = (*cv_charts(w), *linear.residual_charts(w, out_of_fold=True))
    else:
        cards = (logistic.rows_and_events(w), logistic.auc_card(w, k["auc_oof"]), lam,
                 c.Card("k_brier", k["brier"], c.shown(c.num(c.model_key("brier")), "0.0000")))
        stats_ = ("estimate", "odds_ratio", "selected")
        charts = (*cv_charts(w), *logistic.classification_charts(w, out_of_fold=True))
    note = w["coef"]["no_inference"] + (" " + w["coef"]["dummies"] if checked.get("predictors") else "")
    return c.page(model, params, locale, sample, step, name=name, about=about, cards=cards,
                  coef_tables=(c.CoefTable("coef", ct["at_lambda"], stats_, width=30),), coef_note=note, charts=charts,
                  blocks=(logistic.confusion_block,) if model == "logistic" else ())
