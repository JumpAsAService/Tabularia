"""The report of a logistic regression (docs/specs/statistical-models.md, `logistic`): maximum
likelihood or Firth, logit or probit; with a penalty, `penalized.py`'s blocks.

Cards: rows and events, the AUC with its interval, McFadden's R², the Brier score.
Coefficients with their tests, intervals and odds ratios. The confusion matrix at the
threshold. Diagnostics: ROC, calibration (Hosmer–Lemeshow), the predicted probabilities by
outcome, the confusion matrix, deviance residuals against the linear predictor, the odds
ratios on a logarithmic axis.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import penalized, register
from app.engine.models.layouts import shared_readings as sr
from app.engine.stats import layouts as stats
from app.engine.stats.catalog import label_of


def name(params: dict[str, Any], locale: str) -> str:
    checked = catalog.check_params("logistic", params)
    w = c.words("logistic", catalog.variant("logistic", checked), locale)
    if checked.get("penalty", "none") != "none":
        return penalized.name(w, checked)
    parts = [w["name"]]
    if checked.get("link") == "probit":
        parts.append(w["variants"]["probit"])
    if checked.get("estimation") == "firth":
        parts.append(w["variants"]["firth"])
    return " — ".join(parts)


def about(params: dict[str, Any], locale: str) -> str:
    outcome, event = params.get("outcome"), params.get("event_value")
    left = f"{outcome} = {label_of(event)}" if outcome and event is not None else outcome
    return c.subject({**params, "outcome": left})


def rows_and_events(w: dict) -> c.Card:
    k = w["kpi"]
    return c.Card("k_n", k["n_events"], f'{c.shown(c.num(c.model_key("n")), "#,##0")} & " / " & '
                                        f'{c.shown(c.num(c.model_key("events")), "#,##0")}')


def auc_card(w: dict, label: str) -> c.Card:
    k = w["kpi"]
    low, high = c.num(c.model_key("auc_ci_low")), c.num(c.model_key("auc_ci_high"))
    return c.Card("k_auc", label, c.shown(c.num(c.model_key("auc")), "0.000"),
                  under=f'IF(ISNULL({low}), "", {c.join(k["auc_ci"], low=c.shown(low, "0.000"), high=c.shown(high, "0.000"))})')


def classification_charts(w: dict, out_of_fold: bool = False) -> tuple[c.Chart, ...]:
    """ROC, calibration, the predicted probabilities by outcome, the confusion matrix."""
    ch, s = w["charts"], w["series"]
    suffix = "_oof" if out_of_fold else ""
    roc = c.xy("roc", ch["roc" + suffix], '[section] = "curve" AND [level] = "roc"', [c.mark("y", "line", s["roc"])],
               {"diagonal": True, "legend": False})
    # out of fold (a penalized fit) the chart is the diagnostic itself: Hosmer–Lemeshow is not computed there
    calibration = c.xy("calibration", ch["calibration" + suffix], '[section] = "point" AND [level] = "calibration"',
                       [c.mark("y", "point", s["deciles"]), c.mark("y", "line")], {"diagonal": True, "legend": False},
                       under=None if out_of_fold else "hosmer_lemeshow")
    prob = c.histogram("c_prob", ch["prob" + suffix], "prob", s["count"], split=True)
    f = '[section] = "cell"'
    confusion = c.Chart("confusion", "heatmap", ch["confusion" + suffix], {
        "filter": f, "category": "[level_2]", "split_by": "[level]",
        "series": [{"value": "SUM([value])", "name": s["observed"]}],
        "options": {"labels": True, "format": "#,##0"}, "visible": c.exists(f)})
    return roc, calibration, prob, confusion


def confusion_block(p: stats.Page, w: dict) -> None:
    """The confusion matrix at the threshold: the observed classes by row, the predicted by
    column, with the numbers of the classification."""
    t = w["tables"]
    f = '[section] = "cell"'
    c.heading(p, "s_confusion", w["sections"]["confusion"])
    p.text("confusion_line", "=" + c.join(t["confusion_line"], threshold=c.shown(c.num(c.model_key("threshold")), "0.###"),
                                          accuracy=c.shown(c.num(c.model_key("accuracy")), "0.0%"),
                                          sensitivity=c.shown(c.num(c.model_key("sensitivity")), "0.0%"),
                                          specificity=c.shown(c.num(c.model_key("specificity")), "0.0%")),
           h=5, can_grow=True)
    p.keep()
    p.y += 1.0
    p.add({"id": "confusion_table", "type": "table", "dataset": c.DATASET, "x": 0, "y": p.y, "w": 120, "h": 8,
           "title": t["confusion"], "filter": f, "groups": [{"by": "[level]"}],
           "columns": [{"header": t["observed_predicted"], "width": 50}],
           "matrix": {"columns_by": "[level_2]", "value": "SUM([value])", "total": True, "format": "#,##0", "width": 22},
           "style": {"size": 8}, "visible": c.exists(f)}, 10)
    c.reading_under(p, w, "confusion")


def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    checked = catalog.check_params(model, params)
    if checked.get("penalty", "none") != "none":
        return penalized.page(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale))
    w = c.words(model, "unpenalized", locale)
    k, ch, s, ct = w["kpi"], w["charts"], w["series"], w["tables"]
    cards = (rows_and_events(w), auc_card(w, k["auc"]),
             c.Card("k_r2", k["mcfadden_r2"], c.shown(c.num(c.model_key("mcfadden_r2")), "0.0000"),
                    under=c.join(k["lr_test"], p=c.p_with_stars("lr_p_value"))),
             c.Card("k_brier", k["brier"], c.shown(c.num(c.model_key("brier")), "0.0000")))
    tables = (c.CoefTable("coef", ct["estimates"], ("estimate", "std_error", "statistic", "p_value")),
              c.CoefTable("coef_ci", ct["intervals"], ("ci_low", "ci_high", "vif"), width=30),
              c.CoefTable("coef_or", ct["odds_ratios"], ("odds_ratio", "or_ci_low", "or_ci_high"), width=30))
    resid = c.xy("resid_linear", ch["resid_linear"], '[section] = "point" AND [level] = "resid_linear"',
                 [{"value": "[value]", "as": "point", "name": s["points"], "label": "[text]"}], {"target": 0, "legend": False},
                 under="deviance_residuals")
    # the intercept is left out: its odds are a baseline's, on another scale than the ratios
    odds = c.coef_interval("odds_ratio_interval", ch["odds_ratio_interval"], ("odds_ratio", "or_ci_low", "or_ci_high"),
                           (s["odds_ratio"], s["ci_low"], s["ci_high"]), 1.0, log=True,
                           filter_='[section] = "coef" AND [level] <> "(Intercept)"')._replace(wide=True)
    # a probit's coefficients have no odds ratios: no chart, rather than an empty place for one
    charts = (*classification_charts(w), resid, *((odds,) if checked.get("link", "logit") == "logit" else ()))
    return c.page(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale), cards=cards,
                  coef_tables=tables, blocks=(confusion_block,), charts=charts, explained=("summary", "assumptions", "coefficients"))


def event(checked: Mapping[str, Any]) -> str:
    """The event as the header says it: «churned = yes»."""
    outcome, value = checked.get("outcome"), checked.get("event_value")
    return f"{outcome} = {label_of(value)}" if outcome and value is not None else str(outcome or "")


def confusion_lines(rows: list[dict], checked: Mapping[str, Any]) -> list[c.ReadingLine]:
    """The classification at the threshold, in words (`confusion_at`): four shares, measures."""
    k = sr.keyed(rows)
    shares = {s: sr.model(k, s) for s in ("threshold", "accuracy", "sensitivity", "specificity", "precision")}
    if any(v is None for v in shares.values()):
        return []
    return [("confusion", None, "confusion_at", {**shares, "event": event(checked)})]


# the standard errors that are not the classical ones: their method's paragraph (HC0 to HC3 share one)
SE_METHOD = {"hc0": "hc", "hc1": "hc", "hc2": "hc", "hc3": "hc", "cluster": "cluster"}


def readings(rows: list[dict], checked: Mapping[str, Any]) -> list[c.ReadingLine]:
    """The explanation (docs/specs/report-explanations.md, «B2»): the AUC, McFadden's R², the Brier score and the
    likelihood-ratio test; the checks; the terms' tests and the first significant term's odds ratio; the classification
    at the threshold; the method's paragraphs and formulas. With a penalty, `penalized.readings`."""
    if checked.get("penalty", "none") != "none":
        return penalized.readings(rows, checked, "logistic")
    k, a = sr.keyed(rows), sr.alpha(checked)
    low, high = sr.model(k, "auc_ci_low"), sr.model(k, "auc_ci_high")
    lines: list[c.ReadingLine] = [("summary", None, "summary_logistic" if low is not None and high is not None else "summary_logistic_no_ci", {
        "n": sr.model(k, "n"), "events": sr.model(k, "events"), "event": event(checked), "auc": sr.model(k, "auc"),
        "low": low, "high": high, "conf": sr.conf(checked), "r2": sr.model(k, "mcfadden_r2"), "brier": sr.model(k, "brier")})]
    p = sr.model(k, "lr_p_value")
    if p is not None:
        lines.append(("summary", None, "fit_lr_reject" if p < a else "fit_lr_keep", {
            "lr": sr.model(k, "lr_statistic"), "df": sr.model(k, "lr_df"), "p": p, "alpha": a, "event": event(checked)}))
    lines += sr.assumption_lines(rows, checked)
    coef, significant = sr.coefficient_lines(rows, checked)
    se = str(checked.get("se") or "classical")
    if coef and se in SE_METHOD:
        coef.append(("coefficients", None, "coef_robust", {"kind": se.upper()}))
    if checked.get("link", "logit") == "logit":
        coef += sr.ratio_line(rows, checked, significant, ("odds_ratio", "or_ci_low", "or_ci_high"), "coef_odds")
    lines += coef
    lines += confusion_lines(rows, checked)
    probit = checked.get("link") == "probit"
    lines += sr.method_lines("method_probit" if probit else "method_logit",
                             "method_firth" if checked.get("estimation") == "firth" else "",
                             "method_se_" + SE_METHOD[se] if se in SE_METHOD else "", "method_auc", "method_threshold",
                             "method_checks", "method_p_value")
    lines += sr.formula_lines("formula_probit" if probit else "formula_logit", "formula_lr", "formula_wald", "formula_auc",
                              "formula_brier")
    return lines


register("logistic", layout, name, about)
c.READINGS["logistic"] = readings
