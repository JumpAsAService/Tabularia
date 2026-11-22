"""The report of a penalized regression, linear or logistic (docs/specs/statistical-models.md,
`penalty`): ridge, lasso, elastic net, λ chosen by cross-validation. No standard error,
p-value or interval (inference after a penalized selection is out of scope, and the page
says so); the checks are diagnostics, not tests, and the page says that too: the choice of λ,
and for the logistic separation and the events per variable; the tests of an unpenalized fit
are not computed.

Cards: linear — the rows, λ, the terms selected, R² out of fold; logistic — rows and events,
the AUC out of fold (the mean of the folds' AUCs with its standard error between folds, as
cv.glmnet's cvm: the folds' fits have different intercepts, so the AUC of all the out-of-fold
predictions together would mix their scales and is not shown), λ, the Brier score out of fold
with its standard error between folds. Charts: the cross-validation curve with
its band and both λ marked (the least error's and the 1 SE rule's, and a fixed λ), the paths
of the coefficients with the chosen λ marked, the terms kept at the chosen λ (the intercept
left out: on the outcome's scale it would flatten the others); then the residuals out of fold,
against the fitted values, their Q–Q and histogram (linear), or the ROC, calibration,
probabilities and confusion matrix out of fold (logistic), the ROC the mean of the folds' curves.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import shared_readings as sr

# the series of the paths that sums up the terms not drawn one by one (the runner's name for it)
OTHERS = "(others)"


def name(w: dict, checked: dict[str, Any]) -> str:
    """«Linear regression — lasso»."""
    return w["name"] + " — " + w["penalties"][checked["penalty"]]


def cv_charts(w: dict, checked: dict[str, Any]) -> tuple[c.Chart, ...]:
    ch, s = w["charts"], w["series"]
    fixed = not isinstance(checked.get("lambda", "cv_1se"), str)
    marked = [c.num(c.model_key("log_lambda_min")), c.num(c.model_key("log_lambda_1se"))]
    if fixed:
        marked.append(c.num(c.model_key("log_lambda")))
    cv = c.xy("cv", ch["cv_fixed" if fixed else "cv"], '[section] = "cv"', [
        c.mark("cv_mean", "line", w["measures"][checked.get("measure") or "mse"]), c.mark("cv_mean", "point"),
        c.mark("cv_low", "band_low", s["cv_band"]), c.mark("cv_high", "band_high"),
    ], {"target_x": marked, "legend": True}, under="lambda_choice", wide=True)
    paths = c.xy("paths", ch["paths"], '[section] = "curve" AND [level] = "paths"', [c.mark("y", "line")],
                 {"target_x": [c.num(c.model_key("log_lambda"))], "target": 0},
                 split_by=f'IF([level_2] = {c.q(OTHERS)}, {c.q(s["others"])}, [level_2])', wide=True)
    # the terms kept; the intercept left out: on the outcome's scale (or the log odds' baseline) it would
    # flatten the others. No labels on the bars: the table above has the numbers, and a negative bar's
    # label would cross its term's name
    f = '[section] = "coef" AND [stat] = "estimate" AND [level] <> "(Intercept)" AND [value] <> 0'
    selected = c.Chart("coef_selected", "bar", ch["coef_selected"], {
        "filter": f, "category": c.term(), "series": [{"value": "SUM([value])", "name": s["estimate"]}],
        "options": {"legend": False, "format": "0.###"}, "visible": c.exists(f)})
    return cv, paths, selected


def folds_card(w: dict, id_: str, label: str, stat: str, under: str, k: str | None = None) -> c.Card:
    """A number out of fold with its standard error between folds under it («mean of 10 folds · SE 0.031»)."""
    se = c.num(c.model_key(stat + "_se"))
    parts = {"se": c.shown(se, "0.000")}
    if k:
        parts["k"] = c.shown(c.num(c.model_key(k)), "#,##0")
    return c.Card(id_, label, c.shown(c.num(c.model_key(stat)), "0.0000" if stat == "brier" else "0.000"),
                  under=f'IF(ISNULL({se}), "", {c.join(under, **parts)})')


def coef_note(w: dict, penalty: str) -> str:
    """No inference, and, when a categorical predictor made dummies, that each is penalized alone
    (the lasso and the elastic net may keep one level and drop another; the ridge shrinks each)."""
    dummies = w["coef"]["dummies_ridge" if penalty == "ridge" else "dummies"]
    alone, with_dummies = w["coef"]["no_inference"], w["coef"]["no_inference"] + " " + dummies
    dummies = '[section] = "coef" AND NOT ISNULL([level_2]) AND [level_2] <> ""'
    return f"=IF(COUNTIF({dummies}) > 0, {c.q(with_dummies)}, {c.q(alone)})"


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
        charts = (*cv_charts(w, checked), *linear.residual_charts(w, out_of_fold=True),
                  c.histogram("c_residual", w["charts"]["residual_oof"], "residual", w["series"]["count"]))
    else:
        cards = (logistic.rows_and_events(w), folds_card(w, "k_auc", k["auc_oof"], "auc", k["auc_folds"], k="auc_folds"), lam,
                 folds_card(w, "k_brier", k["brier_oof"], "brier", k["brier_folds"]))
        stats_ = ("estimate", "odds_ratio", "selected")
        charts = (*cv_charts(w, checked), *logistic.classification_charts(w, out_of_fold=True))
    return c.page(model, params, locale, sample, step, name=name, about=about, cards=cards,
                  coef_tables=(c.CoefTable("coef", ct["at_lambda"], stats_, width=30),), coef_note=coef_note(w, checked["penalty"]),
                  charts=charts, blocks=(logistic.confusion_block,) if model == "logistic" else (),
                  assumptions_note=w["diagnostics_note"][model], p_values=False, explained=("summary", "assumptions"))


def readings(rows: list[dict], checked: Mapping[str, Any], model: str) -> list[c.ReadingLine]:
    """The explanation of a penalized fit (docs/specs/report-explanations.md, «B2»): measures only, no verdict (no
    p-value exists after a penalized selection). What the penalty kept, the fit out of fold, the choice of λ; the
    diagnostics past their thresholds; for the logistic the classification out of fold. The method says why there is no
    p-value."""
    k = sr.keyed(rows)
    terms = [r for r in rows if r["section"] == "coef" and r["stat"] == "selected" and r["level"] != sr.INTERCEPT]
    lines: list[c.ReadingLine] = [("summary", None, "summary_penalized", {
        "n": sr.model(k, "n"), "selected": sr.model(k, "n_selected"), "total": len(terms), "mixing": sr.model(k, "mixing")})]
    if model == "linear":
        lines.append(("summary", None, "summary_penalized_r2", {"r2": sr.model(k, "r_squared"),
                                                                "outcome": str(checked.get("outcome") or "")}))
    elif sr.model(k, "auc") is not None:
        lines.append(("summary", None, "summary_penalized_auc", {"auc": sr.model(k, "auc"), "se": sr.model(k, "auc_se"),
                                                                 "folds": sr.model(k, "auc_folds")}))
    lines += [("summary", None, "penalized_measure", {}),  # the measure by the node's `measure` (its words choose)
              ("summary", None, "penalized_lambda", {"lambda": sr.model(k, "lambda"), "cv": sr.model(k, "cv_value"),
                                                     "cv_se": sr.model(k, "cv_se")})]
    lines += sr.assumption_lines(rows, checked, tests=False)
    if model == "logistic":
        from app.engine.models.layouts import logistic

        lines += logistic.confusion_lines(rows, checked)
    lines += sr.method_lines("method_" + str(checked["penalty"]), "method_cv", "method_no_inference",
                             *(("method_auc_folds", "method_threshold") if model == "logistic" else ()))
    lines += sr.formula_lines("formula_penalized", "formula_cv")
    return lines
