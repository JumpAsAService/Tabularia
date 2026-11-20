"""The report of the boosting group's models, XGBoost and CatBoost (docs/specs/statistical-models.md,
«Group `boosting` — Python», «The report»): the blocks both share, and `report`, which draws a
model's page; `xgboost.py` and `catboost.py` say what is theirs (the name, the hyperparameters
they show) and register.

First page: the four cards of the task, the diagnostics (a verdict and an advice each, no p-value:
the page says they are not tests). Then the fit (the metrics on training, validation and the
baseline, how the validation part was made, the hyperparameters), the importance of each
predictor, the charts: importance by the model's own measure and by mean |SHAP|, the SHAP summary
(a beeswarm, regression and binary), the learning curves, and by task residuals and observed
against predicted, or ROC, calibration, the predicted probabilities and the confusion matrix, or
for several classes the confusion matrix and the ROC curves one class against the rest.

Nothing is computed here: every number is a row the runner wrote, read by its key or section.
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.stats import layouts as stats
from app.engine.stats.catalog import label_of

# the ramp of the beeswarm: the five bins of the predictor's value, then a category and a missing value
BEESWARM = (("low", "#2c5aa0"), ("mid_low", "#7fa7d6"), ("mid", "#8c8c8c"), ("mid_high", "#e3866b"), ("high", "#b2182b"),
            ("category", "#6b7280"), ("missing", "#c4c8cf"))
# the validation numbers the metrics table has a row for, by task; the fit's table leaves them out
METRICS = {"regression": ("rmse", "mae", "r_squared"),
           "binary": ("auc", "logloss", "brier", "accuracy", "sensitivity", "specificity", "precision", "f1"),
           "multiclass": ("mlogloss", "accuracy", "balanced_accuracy", "macro_auc", "macro_f1")}
PARTS = ("train", "validation", "baseline")


def name(model: str, params: dict[str, Any], locale: str) -> str:
    checked = catalog.check_params(model, params)
    w = c.words(model, catalog.variant(model, checked), locale)
    return f'{w["name"]} — {w["tasks"][checked["task"]]}'


def about(params: dict[str, Any], locale: str) -> str:
    outcome, event = params.get("outcome"), params.get("event_value")
    left = f"{outcome} = {label_of(event)}" if params.get("task") == "binary" and outcome and event is not None else outcome
    return c.subject({**params, "outcome": left})


def stat_names(task: str) -> tuple[str, ...]:
    """The numbers of the `model` section the metrics table says, which the fit's table leaves out."""
    return tuple(f"{p}_{m}" for m in METRICS[task] for p in PARTS)


def hyperparameters(model: str, checked: dict[str, Any], keys: tuple[str, ...]) -> str:
    """«learning_rate = 0.05 · max_depth = 6 …»: the node's settings, the defaults filled in."""
    return "  ·  ".join(f"{k} = {_said(checked[k])}" for k in keys if k in checked and checked[k] is not None)


def _said(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# ── the blocks ───────────────────────────────────────────────────────────────
def cards(task: str, w: dict) -> tuple[c.Card, ...]:
    k, num, shown, key = w["kpi"], c.num, c.shown, c.model_key
    trees = c.Card("k_trees", k["trees"], shown(num(key("trees")), "#,##0"),
                   under=c.join(k["best_round"], x=shown(num(key("best_iteration")) + " + 1", "#,##0")))
    split = c.Card("k_n", k["n_split"], f'{shown(num(key("n_train")), "#,##0")} & " · " & {shown(num(key("n_validation")), "#,##0")}')
    if task == "regression":
        return (split, c.Card("k_rmse", k["rmse"], shown(num(key("validation_rmse")))),
                c.Card("k_r2", k["r_squared"], shown(num(key("validation_r_squared")), "0.0000")), trees)
    if task == "binary":
        n = c.Card("k_n", k["n_events"], f'{shown(num(key("n")), "#,##0")} & " / " & {shown(num(key("events")), "#,##0")}')
        return (n, c.Card("k_auc", k["auc"], shown(num(key("validation_auc")), "0.000")),
                c.Card("k_logloss", k["logloss"], shown(num(key("validation_logloss")), "0.000")), trees)
    n = c.Card("k_n", k["n_classes"], f'{shown(num(key("n")), "#,##0")} & " / " & {shown(num(key("n_classes")), "#,##0")}')
    return (n, c.Card("k_accuracy", k["accuracy"], shown(num(key("validation_accuracy")), "0.0%")),
            c.Card("k_logloss", k["logloss"], shown(num(key("validation_mlogloss")), "0.000")), trees)


def fit_block(model: str, checked: dict[str, Any], keys: tuple[str, ...]):
    """After the numbers of the fit: how the task and the validation part were set, the metrics
    on training, validation and the baseline, the hyperparameters."""
    task = checked["task"]

    def block(p: stats.Page, w: dict) -> None:
        f, m = w["fit"], w["metrics"]
        left = about(checked, "en").split(" ~ ")[0]
        how = c.join(f["validation"], how=c.txt(c.model_key("validation")),
                     share=c.shown(f'{c.num(c.model_key("n_validation"))} / ({c.num(c.model_key("n_train"))} + '
                                   f'{c.num(c.model_key("n_validation"))})', "0.#%"),
                     seed=c.shown(c.num(c.model_key("seed")), "0"))
        p.y += 1.0
        p.text("setup", "=" + c.q(f'{w["tasks"][task]}: {left} · ') + " & " + how, h=5, size=9, weight="semibold", can_grow=True)
        wanted = " OR ".join(f'[stat] = "validation_{x}"' for x in METRICS[task])
        f_metrics = f'[section] = "model" AND ({wanted}) AND NOT ISNULL([value])'
        suffix = 'RIGHT([stat], LEN([stat]) - 11)'

        def of(part: str) -> str:
            x = f'LOOKUP("model|||{part}_" & {suffix}, [key], [value])'
            return f'IF(ISNULL({x}), "", {c.shown(x)})'

        names = "IFS(" + ", ".join(f'[stat] = "validation_{x}", {c.q(m["names"][x])}' for x in METRICS[task]) + ", [label])"
        p.add(stats.table("metrics", m["title"], f_metrics, [
            {"header": m["metric"], "value": names},
            {"header": m["train"], "value": of("train"), "width": 32, "align": "right"},
            {"header": m["validation"], "value": c.shown("[value]"), "width": 32, "align": "right"},
            {"header": m["baseline"], "value": of("baseline"), "width": 32, "align": "right"},
        ], p.y, visible=c.exists(f_metrics)), 10)
        p.text("metrics_note", m["note"] + (m["note_binary"] if task == "binary" else ""), h=5, size=7.5, color=c.MUTED, can_grow=True)
        p.y += 1.0
        shown = hyperparameters(model, checked, keys)
        p.text("hyperparameters", stats.static(f'{f["hyperparameters"]}: {shown}'), h=5, size=8, color=c.MUTED, can_grow=True)

    return block


def importance_block(task: str):
    def block(p: stats.Page, w: dict) -> None:
        i = w["importance"]
        c.heading(p, "s_importance", w["sections"]["importance"])
        p.text("importance_note", i["gain_note"] + (" " + i["multiclass_note"] if task == "multiclass" else ""),
               h=5, size=8, color=c.MUTED, can_grow=True)
        p.y += 1.0
        st = w["stats"]["importance"]
        f = '[section] = "importance" AND [stat] = "rank"'
        pct = lambda s: f'IF(ISNULL({c.of_row("importance", s)}), "", {c.shown(c.of_row("importance", s), "0.0%")})'  # noqa: E731
        shap = c.of_row("importance", "mean_abs_shap")
        table = stats.table("importance", i["title"], f, [
            {"header": st["rank"], "value": "[value]", "width": 20, "align": "right", "format": "0"},
            {"header": i["predictor"], "value": "[level]"},
            {"header": i["gain_share"], "value": pct("gain_share"), "width": 30, "align": "right"},
            {"header": st["mean_abs_shap"], "value": f'IF(ISNULL({shap}), "", {c.shown(shap, "0.####")})', "width": 28, "align": "right"},
            {"header": st["mean_abs_shap_share"], "value": pct("mean_abs_shap_share"), "width": 32, "align": "right"},
            {"header": st["missing_share"], "value": pct("missing_share"), "width": 22, "align": "right"},
        ], p.y, visible=c.exists(f))
        table["sort"] = [{"by": "[value]"}]
        p.add(table, 10)
        if task == "multiclass":
            c.heading(p, "s_classes", w["sections"]["classes"])
            cs = w["stats"]["class"]
            only = '[section] = "class" AND [stat] = "support"'
            cols = [{"header": w["classes"]["class"], "value": "[level]"}]
            for s in ("support", "share_train", "share_validation", "precision", "recall", "f1", "auc_ovr"):
                x = c.of_row("class", s)
                pattern = "#,##0" if s == "support" else "0.0%" if s.startswith("share") else "0.000"
                cols.append({"header": cs[s], "value": f'IF(ISNULL({x}), "", {c.shown(x, pattern)})', "width": 22, "align": "right"})
            p.add(stats.table("classes", w["classes"]["title"], only, cols, p.y, visible=c.exists(only)), 10)

    return block


def confusion_block(task: str):
    """The confusion matrix on the validation part, as a table (the chart draws it too): the
    numbers of the classification in a line, the observed classes by row."""
    def block(p: stats.Page, w: dict) -> None:
        t = w["confusion"]
        f = '[section] = "cell"'
        c.heading(p, "s_confusion", w["sections"]["confusion"])
        n = lambda s, pattern: c.shown(c.num(c.model_key(s)), pattern)  # noqa: E731
        if task == "binary":
            line = c.join(t["line_binary"], threshold=n("threshold", "0.###"), accuracy=n("validation_accuracy", "0.0%"),
                          sensitivity=n("validation_sensitivity", "0.0%"), specificity=n("validation_specificity", "0.0%"))
        else:
            line = c.join(t["line_multiclass"], accuracy=n("validation_accuracy", "0.0%"),
                          balanced=n("validation_balanced_accuracy", "0.0%"), f1=n("validation_macro_f1", "0.0%"))
        p.text("confusion_line", "=" + line, h=5, can_grow=True)
        p.keep()
        p.y += 1.0
        p.add({"id": "confusion_table", "type": "table", "dataset": c.DATASET, "x": 0, "y": p.y, "w": c.WIDTH, "h": 8,
               "title": t["title"], "filter": f, "groups": [{"by": "[level]"}],
               "columns": [{"header": t["observed_predicted"], "width": 36}],
               "matrix": {"columns_by": "[level_2]", "value": "SUM([value])", "total": True, "format": "#,##0", "width": 16},
               "style": {"size": 8}, "visible": c.exists(f)}, 10)

    return block


# ── the charts ───────────────────────────────────────────────────────────────
def importance_bars(task: str, w: dict) -> tuple[c.Chart, c.Chart]:
    ch, s = w["charts"], w["series"]
    most = catalog.LIMITS["max_importance_bars"]
    gain = '[section] = "importance" AND [stat] = "gain_share"'
    gain_chart = c.Chart("importance_gain", "bar", ch["importance_gain"], {
        "filter": gain, "category": "[level]", "series": [{"value": "SUM([value])", "name": s["importance"]}],
        "options": {"sort": "desc", "top": most, "labels": True, "legend": False, "format": "0.0%"}, "visible": c.exists(gain)})
    if task == "multiclass":  # one bar a predictor, stacked by class: the summary of each class is not drawn
        f = '[section] = "importance" AND [stat] = "mean_abs_shap" AND NOT ISNULL([level_2])'
        shap = c.Chart("importance_shap", "bar", ch["importance_shap_class"], {
            "filter": f, "category": "[level]", "split_by": "[level_2]", "series": [{"value": "SUM([value])", "name": s["importance"]}],
            "options": {"sort": "desc", "top": most, "stacked": True, "legend": True, "format": "0.###"}, "visible": c.exists(f)},
            under="importance_stability")
    else:
        f = '[section] = "importance" AND [stat] = "mean_abs_shap"'
        shap = c.Chart("importance_shap", "bar", ch["importance_shap"], {
            "filter": f, "category": "[level]", "series": [{"value": "SUM([value])", "name": s["importance"]}],
            "options": {"sort": "desc", "top": most, "labels": True, "legend": False, "format": "0.###"}, "visible": c.exists(f)},
            under="importance_stability")
    return gain_chart, shap


def beeswarm(task: str, w: dict) -> c.Chart:
    """The SHAP summary: the runner's points (x the SHAP value, y the place of the predictor with
    its jitter, `stat` the colour bin), a series a bin in the ramp, the names on the y axis; its
    caption says the scale of the task (the outcome's units, or log-odds of the event)."""
    f = '[section] = "point" AND [level] = "shap_beeswarm"'
    series = [{"value": f'IF([stat] = "{bin_}", [value], NULL)', "as": "point", "name": bin_, "color": colour} for bin_, colour in BEESWARM]
    return c.xy("shap_beeswarm", w["charts"]["shap_beeswarm"], f, series,
                {"legend": False, "target_x": [0], "y_categories": "[level_2]"}, wide=True)._replace(
                    caption=w["charts"]["beeswarm_note_" + task])


def learning_charts(task: str, w: dict, checked: dict[str, Any]) -> tuple[c.Chart, ...]:
    ch, s = w["charts"], w["series"]
    stop_metric = checked["stop_metric"]
    part = f'IF([level_2] = "train", {c.q(s["train"])}, {c.q(s["validation"])})'
    best = c.num(c.model_key("best_iteration")) + " + 1"

    def curve(level: str, title: str, under: str, caption: str | None = None) -> c.Chart:
        chart = c.xy(level, title, f'[section] = "curve" AND [level] = "{level}"', [c.mark("y", "line")],
                     {"target_x": [best], "legend": True}, under=under, split_by=part)
        return chart._replace(caption=caption) if caption else chart

    # with balanced weights the curves are the losses the fit weighs (as early stopping reads them), not the table's
    balanced = task != "regression" and checked.get("class_weight") == "balanced"
    out = [curve("learning_loss", ch["learning_loss_" + task], "overfitting", ch["learning_balanced_note"] if balanced else None)]
    if stop_metric != "loss":  # the stop metric is another number than the loss: its curve too
        # a binary error rate cuts at 0.5 (XGBoost's error, CatBoost's ZeroOneLoss), whatever the node's threshold
        said = ch["error_note"] if stop_metric == "error" and task == "binary" else None
        out.append(curve("learning_stop", ch["learning_stop"], "trees_limit", said))
    return tuple(out)


def points(id_: str, title: str, w: dict, target: float | None, diagonal: bool = False) -> c.Chart:
    """Validation points (x the predicted value, y the observed one or the residual), the extremes named."""
    f = f'[section] = "point" AND [level] = "{id_}"'
    options: dict[str, Any] = {"legend": False}
    if target is not None:
        options["target"] = target
    if diagonal:
        options["diagonal"] = True
    return c.xy(id_, title, f, [{"value": 'IF([stat] = "y", [value], NULL)', "as": "point", "name": w["series"]["points"],
                                 "label": "[text]"}], options)


def task_charts(task: str, w: dict) -> tuple[c.Chart, ...]:
    ch, s = w["charts"], w["series"]
    if task == "regression":
        return (points("observed_predicted", ch["observed_predicted"], w, None, diagonal=True),
                points("resid_predicted", ch["resid_predicted"], w, 0.0),
                c.histogram("residual", ch["residual"], "residual", s["count"]))
    f = '[section] = "cell"'
    confusion = c.Chart("confusion", "heatmap", ch["confusion"], {
        "filter": f, "category": "[level_2]", "split_by": "[level]", "series": [{"value": "SUM([value])", "name": s["observed"]}],
        "options": {"labels": True, "format": "#,##0"}, "visible": c.exists(f)})
    if task == "multiclass":
        roc = c.xy("roc_ovr", ch["roc_ovr"], '[section] = "curve" AND [level] = "roc_ovr"', [c.mark("y", "line")],
                   {"diagonal": True, "legend": True}, split_by="[level_2]")
        return confusion, roc
    roc = c.xy("roc", ch["roc"], '[section] = "curve" AND [level] = "roc"', [c.mark("y", "line", s["roc"])],
               {"diagonal": True, "legend": False})
    calibration = c.xy("calibration", ch["calibration"], '[section] = "point" AND [level] = "calibration"',
                       [c.mark("y", "point", s["deciles"]), c.mark("y", "line")],
                       {"diagonal": True, "unit_square": True, "legend": False}, under="calibration")
    return roc, calibration, c.histogram("probability", ch["probability"], "probability", s["count"], split=True), confusion


def report(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None, keys: tuple[str, ...]) -> dict:
    """A boosting model's report: the page of `model` (`keys`: the hyperparameters it shows)."""
    checked = catalog.check_params(model, params)
    task = checked["task"]
    w = c.words(model, catalog.variant(model, checked), locale)
    gain, shap = importance_bars(task, w)
    charts = [gain, shap]
    if task != "multiclass":
        charts.append(beeswarm(task, w))
    charts += [*learning_charts(task, w, checked), *task_charts(task, w)]
    blocks = (fit_block(model, checked, keys), importance_block(task), *((confusion_block(task),) if task != "regression" else ()))
    return c.page(model, params, locale, sample, step, name=name(model, params, locale), about=about(params, locale),
                  cards=cards(task, w), blocks=blocks, charts=tuple(charts), assumptions_note=w["diagnostics_note"],
                  p_values=False, fit_skip=stat_names(task))
