"""The report of a parametric survival model (docs/specs/statistical-models.md, «The node «Parametric
survival»», «The report»): the distribution fitted (a Weibull, a Gompertz, …, or the one `auto`
chose among six), its coefficients as time ratios and as hazard ratios, its own parameters, the fit
against the Kaplan–Meier curve, the profile's median, quantiles and survival, the hazard over time.

Cards: the rows and events, the distribution (with `auto` and its Akaike weight), the profile's
median with its interval (with strata, the concordance), the AIC. Blocks: the distribution's
parameters and the ratios' charts under the coefficients; the choice of the distribution
(`auto` only: the candidates sorted by AIC and their curves); the fit to the data (Kaplan–Meier
with its band against the fitted curve, Cox–Snell, log(−log S)); the profile; the influence.

The fitted curve against Kaplan–Meier's is one chart without groups and a second with them
(`split_by`: a step and a line a group, in the group's colour); the groups are the strata, or the
levels of a predictor, which the runner names in `level_2` («all» when there are none).

Without strata the runner names the one stratum «all»: the page says it in its language.
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats
from app.engine.stats.catalog import label_of

SMALL = "0.#####"  # the tables' numbers: five decimals at most, as many as they have below
ALL = "all"  # the runner's name of the one stratum without strata
MAX_TIMES_DRAWN = 4  # the vertical lines of the profile's survival: the node's first four times (MAX_XY_TARGETS_X)
DISTRIBUTIONS = ("exponential", "weibull", "gompertz", "loglogistic", "lognormal", "gengamma")
MEASURES = ("km_agreement", "dfbeta", "epv", "extrapolation")  # numbers with no p-value: no verdict, no colour
CANDIDATE_STATUS = ("ok", "not_converged", "hessian", "time")


def name(params: dict[str, Any], locale: str) -> str:
    return c.words("parametric_survival", "auto", locale)["name"]


def about(params: dict[str, Any], locale: str) -> str:
    time, event, value = params.get("time"), params.get("event"), params.get("event_value")
    left = f"{time}, {event} = {label_of(value)}" if time and event and value is not None else str(time or "")
    said = c.subject({**params, "outcome": left})
    if params.get("strata"):
        said = c.words("parametric_survival", "auto", locale)["by"].replace("{what}", said).replace("{strata}", str(params["strata"]))
    return said


# ── small expressions ───────────────────────────────────────────────────────
def _distribution(w: dict, field: str) -> str:
    """A distribution's raw name as the page says it («weibull» → «Weibull»)."""
    names = w["distributions"]
    return "IFS(" + ", ".join(f'{field} = "{d}", {c.q(names[d])}' for d in DISTRIBUTIONS) + f", {field})"


def _group(w: dict, field: str = "[level_2]") -> str:
    """A group of the charts as the page names it: the words for all the rows, else its own name."""
    return f'IF({field} = "{ALL}", {c.q(w["all"])}, {field})'


def _grouped(filter_: str = '[section] = "curve" AND [level] = "fit_km"') -> str:
    """True when the Kaplan–Meier chart has more than one group (strata, or the levels of a predictor)."""
    return f'COUNTIF({filter_} AND [level_2] <> "{ALL}") > 0'


def _interval(low: str, high: str) -> str:
    """«21.6 – 26.3» from two numbers; an end that is missing (never reached) is a dash."""
    def end(x: str) -> str:
        return f'IF(ISNULL({x}), "—", FORMAT({x}, "0.###"))'

    return f'IF(ISNULL({low}) AND ISNULL({high}), "", {end(low)} & " – " & {end(high)})'


def _times(params: dict[str, Any]) -> list[float]:
    return [float(t) for t in (params.get("times") or [])][:MAX_TIMES_DRAWN]


# ── under the coefficients: the distribution's parameters and the ratios' charts ──
def _parameters(p: stats.Page, w: dict) -> None:
    t = w["tables"]
    one = '[section] = "parameter" AND [stat] = "estimate"'
    names = "IFS(" + ", ".join(f'[level] = "{k}", {c.q(v)}' for k, v in w["parameters"].items()) + ", [level])"
    cols = [{"header": t["parameter"], "value": f'{names} & IF(ISNULL([level_2]) OR [level_2] = "", "", " — " & [level_2])'},
            *({"header": c.stat_label(w, "parameter", s), "value": c.number_of_row("parameter", s), "width": 26,
               "align": "right", "format": SMALL} for s in ("estimate", "std_error", "ci_low", "ci_high"))]
    p.add(stats.table("parameters", t["parameters"], one, cols, p.y, visible=c.exists(one)), 10)
    p.text("parameters_note", w["parameters_note"], h=4.5, size=7, color=c.MUTED, can_grow=True, visible=c.exists(one))
    p.y += 1.5


def _ratio_charts(variant: str):
    def block(p: stats.Page, w: dict) -> None:
        ch, s = w["charts"], w["series"]
        not_intercept = '[section] = "coef" AND [level] <> "(Intercept)"'
        charts = []
        if variant != "ph":
            charts.append(c.coef_interval("time_ratio_interval", ch["time_ratio_interval"], ("time_ratio", "tr_ci_low", "tr_ci_high"),
                                          (s["time_ratio"], s["ci_low"], s["ci_high"]), 1.0, log=True, filter_=not_intercept))
        if variant != "aft":
            charts.append(c.coef_interval("hazard_ratio_interval", ch["hazard_ratio_interval"],
                                          ("hazard_ratio", "hr_ci_low", "hr_ci_high"),
                                          (s["hazard_ratio"], s["ci_low"], s["ci_high"]), 1.0, log=True, filter_=not_intercept))
        p.y += 1.0
        c.grid(p, w, tuple(charts), None)
    return block


# ── the choice of the distribution (`auto`) ─────────────────────────────────
def _candidates(params: dict[str, Any]):
    def block(p: stats.Page, w: dict) -> None:
        t, ch, s = w["tables"], w["charts"], w["series"]
        c.heading(p, "s_candidates", w["sections"]["candidates"])
        one = '[section] = "candidate" AND [stat] = "n_parameters"'
        chosen_row = c.of_row("candidate", "chosen")
        moved = f'COUNTIF([section] = "candidate" AND [stat] = "parsimony" AND [value] = 1) > 0'
        n = c.shown(f'COUNTIF([section] = "candidate" AND [stat] = "converged")', "#,##0")
        chosen = c.txt(c.model_key("distribution"))
        p.text("candidates_said", "=" + c.join(w["candidates"]["lowest"], distribution=chosen, n=n), h=5, size=8.5, can_grow=True,
               advance=False, visible=f"=NOT ({moved})")
        default = f'{float(params.get("aic_margin") if params.get("aic_margin") is not None else 2):g}'  # the node's option
        margin = c.shown(f'COALESCE({c.num(c.model_key("aic_margin"))}, {default})', "0.##")
        p.text("candidates_parsimony", "=" + c.join(w["candidates"]["parsimony"], distribution=chosen, margin=margin), h=5, size=8.5,
               can_grow=True, visible=f"={moved}")
        p.keep()  # the heading with its sentence: the table, which takes its row alone, follows
        status = ("IFS(" + ", ".join(f'{c.of_row("candidate", "status", "text")} = "{k}", {c.q(v)}'
                                      for k, v in w["candidates"]["status"].items()) + ', "")')
        mark = f'IF({c.of_row("candidate", "parsimony")} = 1, "✓ ·", IF({chosen_row} = 1, "✓", ""))'

        def number(stat: str, fmt: str = "0.##", width: float = 22.0) -> dict:
            return {"header": c.stat_label(w, "candidate", stat), "value": c.number_of_row("candidate", stat), "width": width,
                    "align": "right", "format": fmt}

        table = stats.table("candidates", t["candidates"], one, [
            {"header": t["distribution"], "value": _distribution(w, "[level]"),
             "rules": [{"when": f"{chosen_row} = 1", "style": {"weight": "bold", "color": c.ACCENT}}]},
            number("n_parameters", "#,##0", 18), number("log_lik", "0.##", 30), number("aic", "0.##", 15), number("bic", "0.##", 15),
            number("delta_aic", "0.##", 14), number("akaike_weight", "0.###", 20),
            {"header": t["status"], "value": status, "width": 22},
            {"header": c.stat_label(w, "candidate", "chosen"), "value": mark, "width": 12, "align": "center"},
        ], p.y, visible=c.exists(one))
        aic = c.of_row("candidate", "aic")  # a candidate with no AIC (it did not converge) goes last
        table["sort"] = [{"by": f"IF(ISNULL({aic}), 1E+300, {aic})"}, {"by": "[ord]"}]
        p.add(table, 10)
        p.text("candidates_legend", w["candidates"]["legend"], h=4.5, size=7, color=c.MUTED, can_grow=True)
        f = '[section] = "curve" AND [level] = "candidates"'
        km_step = {"value": 'IF([level_2] = "km", [value], NULL)', "as": "step", "name": s["km"]}
        fitted = {"value": 'IF([level_2] <> "km", [value], NULL)', "as": "line", "name": ""}
        item = {"id": "candidates_chart", "type": "chart", "dataset": c.DATASET, "kind": "xy", "x": 0, "y": p.y + 2, "w": c.WIDTH,
                "h": 64, "title": stats.fit(ch["candidates"], 7.5, c.WIDTH - 4, 700), "filter": f, "category": "[x0]",
                "split_by": f'IF([level_2] = "km", {c.q(s["km"])}, {_distribution(w, "[level_2]")})',
                "series": [km_step, fitted], "options": {"format": "0.##", "min": 0, "max": 1}, "visible": c.exists(f)}
        p.items.append({"id": "candidates_chart_keep", "type": "rect", "x": 0, "y": p.y + 2, "w": 0.1, "h": 71.5, "style": {},
                        "visible": c.exists(f)})
        p.add(item, 66.5)
        p.text("candidates_chart_note", w["candidates"]["chart_note"], h=4.5, size=7, color=c.MUTED, can_grow=True,
               visible=c.exists(f))
    return block


# ── the fit to the data ─────────────────────────────────────────────────────
def _verdict(w: dict, key: str) -> str:
    said = "IFS(" + ", ".join(f'{c.txt(key)} = "{v}", {c.q(w["verdicts"][v])}' for v in catalog.VERDICTS) + ', "")'
    return "=" + c.join(w["illustrates"], check=f'LOOKUP({c.q(key)}, [key], [label])', verdict=said)


def _fit_to_the_data(has_loglog: bool):
    def block(p: stats.Page, w: dict) -> None:
        ch, s = w["charts"], w["series"]
        c.heading(p, "s_fit_data", w["sections"]["fit_data"])
        f = '[section] = "curve" AND [level] = "fit_km"'
        band = [{"value": 'IF([stat] = "lower", [value], NULL)', "as": "band_low"},
                {"value": 'IF([stat] = "upper", [value], NULL)', "as": "band_high"}]
        top, tall = p.y, 74.0
        common = {"type": "chart", "dataset": c.DATASET, "kind": "xy", "x": 0, "y": top, "w": c.WIDTH, "h": tall,
                  "filter": f, "category": "[x0]", "options": {"format": "0.##", "min": 0, "max": 1}}
        one = {**common, "id": "fit_km", "title": stats.fit(ch["fit_km"], 7.5, c.WIDTH - 4, 700),
               "series": [{"value": 'IF([stat] = "surv", [value], NULL)', "as": "step", "name": s["km"]}, *band,
                          {"value": 'IF([stat] = "fitted", [value], NULL)', "as": "line", "name": s["fitted"]}],
               "visible": f"={c.exists(f).lstrip('=')} AND NOT ({_grouped()})"}
        many = {**common, "id": "fit_km_groups", "title": stats.fit(ch["fit_km_groups"], 7.5, c.WIDTH - 4, 700),
                "split_by": _group(w),
                "series": [{"value": 'IF([stat] = "surv", [value], NULL)', "as": "step", "name": s["km"]}, *band,
                           {"value": 'IF([stat] = "fitted", [value], NULL)', "as": "line", "name": s["fitted"]}],
                "visible": f"={_grouped()}"}
        p.items += [one, many]
        key = f"assumption|km_agreement|{ALL}|verdict"
        p.items.append({"id": "fit_km_caption", "type": "text", "x": 0, "y": top + tall + 0.3, "w": c.WIDTH, "h": 4.5,
                        "value": _verdict(w, key), "style": {"size": 7, "color": c.MUTED},
                        "visible": f"=COUNTIF([key] = {c.q(key)}) > 0"})
        p.items.append({"id": "fit_km_how", "type": "text", "x": 0, "y": top + tall + 4.8, "w": c.WIDTH, "h": 8.0,
                        "value": stats.static(w["fit_km_how"]), "style": {"size": 7, "color": c.MUTED}, "visible": c.exists(f)})
        p.y = top + tall + 13.5
        p.keep()
        p.y += 2.0
        snell = c.xy("cox_snell", ch["cox_snell"], '[section] = "point" AND [level] = "cox_snell"',
                     [c.mark("y", "point", s["residuals"]), c.mark("y", "line")], {"diagonal": True, "legend": False},
                     under=None)._replace(caption=w["cox_snell_how"])
        charts = [snell]
        if has_loglog:
            f2 = '([section] = "point" OR [section] = "curve") AND [level] = "loglog"'
            charts.append(c.xy("loglog", ch["loglog"], f2, [
                {"value": 'IF([section] = "point", [value], NULL)', "as": "point", "name": s["km_points"]},
                {"value": 'IF([section] = "curve", [value], NULL)', "as": "line", "name": s["fitted"]}], {"format": "0.##"},
                split_by=_group(w))._replace(caption=w["loglog_how"]))
        c.grid(p, w, tuple(charts), None)
    return block


# ── the profile: its median, quantiles and survival; the curves ─────────────
def _profile(params: dict[str, Any]):
    times = _times(params)

    def block(p: stats.Page, w: dict) -> None:
        t, ch, s = w["tables"], w["charts"], w["series"]
        c.heading(p, "s_profile", w["sections"]["profile"])
        p.text("profile_line", "=" + c.join(w["profile_is"], profile=c.txt(c.model_key("profile"))), h=5, size=8.5, can_grow=True,
               visible=c.exists('[key] = "model|||profile"'))
        one = '[section] = "stratum" AND [stat] = "n"'
        stratum = {"header": t["stratum"], "value": _group(w, "[level]")}
        p.add(stats.table("strata", t["strata"], one, [
            stratum,
            {"header": c.stat_label(w, "stratum", "n"), "value": c.of_row("stratum", "n"), "width": 22, "align": "right",
             "format": "#,##0"},
            {"header": c.stat_label(w, "stratum", "events"), "value": c.of_row("stratum", "events"), "width": 22, "align": "right",
             "format": "#,##0"},
            {"header": c.stat_label(w, "stratum", "last_time"), "value": c.of_row("stratum", "last_time"), "width": 36,
             "align": "right", "format": "0.###"}], p.y, visible=c.exists(one)), 10)
        quant = ('[section] = "stratum" AND ([stat] = "median" OR (STARTSWITH([stat], "quantile_") AND RIGHT([stat], 7) <> "_ci_low" '
                 'AND RIGHT([stat], 8) <> "_ci_high"))')
        key = '"stratum|" & [level] & "||median"'
        p.add(stats.table("quantiles", t["quantiles"], quant, [
            stratum, {"header": t["quantity"], "value": "[label]", "width": 40},
            {"header": t["time"], "value": "[value]", "width": 26, "align": "right", "format": "0.###"},
            {"header": t["low"], "value": 'LOOKUP([key] & "_ci_low", [key], [value])', "width": 26, "align": "right",
             "format": "0.###"},
            {"header": t["high"], "value": 'LOOKUP([key] & "_ci_high", [key], [value])', "width": 26, "align": "right",
             "format": "0.###"},
            {"header": c.stat_label(w, "stratum", "km_median"),
             "value": f'IF([stat] = "median", LOOKUP({key.replace("||median", "||km_median")}, [key], [value]), NULL)',
             "width": 34, "align": "right", "format": "0.###"},
        ], p.y, visible=c.exists(quant)), 10)
        p.text("quantiles_note", w["quantiles_note"], h=4.5, size=7, color=c.MUTED, can_grow=True, visible=c.exists(quant))
        at = ('[section] = "stratum" AND STARTSWITH([stat], "surv_at_") AND RIGHT([stat], 4) <> "_low" '
              'AND RIGHT([stat], 5) <> "_high"')
        p.add(stats.table("surv_at", t["surv_at"], at, [
            stratum, {"header": t["time"], "value": "[x0]", "width": 30, "align": "right", "format": "0.######"},
            {"header": t["survival"], "value": "[value]", "width": 26, "align": "right", "format": "0.0000"},
            {"header": t["low"], "value": 'LOOKUP([key] & "_low", [key], [value])', "width": 26, "align": "right", "format": "0.0000"},
            {"header": t["high"], "value": 'LOOKUP([key] & "_high", [key], [value])', "width": 26, "align": "right",
             "format": "0.0000"},
        ], p.y, visible=c.exists(at)), 10)
        p.y += 1.0
        survival = c.xy("profile_survival", ch["profile_survival"], '[section] = "curve" AND [level] = "profile_survival"',
                        [c.mark("surv", "line", s["survival"]), c.mark("lower", "band_low"), c.mark("upper", "band_high")],
                        {"format": "0.##", "min": 0, "max": 1, "legend": True, **({"target_x": times} if times else {})},
                        split_by=_group(w))
        hazard = c.xy("hazard", ch["hazard"], '[section] = "curve" AND [level] = "hazard"',
                      [c.mark("hazard", "line", s["hazard"]), c.mark("lower", "band_low"), c.mark("upper", "band_high")],
                      {"format": "0.####", "legend": True}, split_by=_group(w))
        c.grid(p, w, (survival, hazard), None)
    return block


def _influence(p: stats.Page, w: dict) -> None:
    ch, s = w["charts"], w["series"]
    c.heading(p, "s_influence", w["sections"]["influence"])
    deviance = c.xy("deviance_linear", ch["deviance_linear"], '[section] = "point" AND [level] = "deviance_linear"',
                    [{"value": "[value]", "as": "point", "name": s["residuals"], "label": "[text]"}], {"target": 0, "legend": False},
                    under="dfbeta")
    c.grid(p, w, (deviance,), None)


# ── the page ────────────────────────────────────────────────────────────────
def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    checked = catalog.check_params(model, params)
    variant = catalog.variant(model, checked)
    w = c.words(model, variant, locale)
    k, ct = w["kpi"], w["tables"]
    strata = c.num(c.model_key("n_strata"))
    several, alone = f"={strata} > 1", f"=NOT ({strata} > 1)"
    median = c.num(f"stratum|{ALL}||median")
    median_ci = _interval(c.num(f"stratum|{ALL}||median_ci_low"), c.num(f"stratum|{ALL}||median_ci_high"))
    auto = f'{c.txt(c.model_key("selection"))} = {c.q(w["values"]["selection"]["auto"])}'
    weight = f'SUMIF([section] = "candidate" AND [stat] = "akaike_weight" AND {c.of_row("candidate", "chosen")} = 1, [value])'
    cards = (c.Card("k_n", k["n_events"], f'{c.shown(c.num(c.model_key("n")), "#,##0")} & " / " & '
                                          f'{c.shown(c.num(c.model_key("events")), "#,##0")}'),
             c.Card("k_distribution", k["distribution"], c.txt(c.model_key("distribution")),
                    under=f'IF({auto}, ' + c.join(k["auto_weight"], weight=c.shown(weight, "0.00")) + ', "")'),
             (c.Card("k_median", k["median"], f'IF(ISNULL({median}), {c.q(k["never"])}, {c.shown(median, "0.###")})',
                     visible=alone, under=c.join(k["ci"], ci=median_ci)),
              c.Card("k_concordance", k["concordance"], c.shown(c.num(c.model_key("concordance")), "0.000"), visible=several,
                     under=c.join(k["se"], se=c.shown(c.num(c.model_key("concordance_se")), "0.000")))),
             c.Card("k_aic", k["aic"], c.shown(c.num(c.model_key("aic"))),
                    under=c.join(k["bic"], bic=c.shown(c.num(c.model_key("bic"))))))
    weibull, gompertz = c.q(w["values"]["distribution"]["weibull"]), c.q(w["values"]["distribution"]["gompertz"])
    dist = c.txt(c.model_key("distribution"))
    note = ("=" + c.join(w["coef_scale"], scale=c.txt(c.model_key("coefficient_scale")))
            + f' & IF({dist} = {weibull} AND {strata} > 1, " " & {c.q(w["coef_no_hr"])}, IF({dist} = {gompertz}, " " & {c.q(w["coef_no_tr"])}, ""))')
    estimates = c.CoefTable("coef", ct["estimates"], ("estimate", "std_error", "statistic", "p_value"), fmt=SMALL)
    ratios = c.CoefTable("coef_tr", ct["time_ratios"], ("time_ratio", "tr_ci_low", "tr_ci_high", "vif"), width=26, fmt=SMALL)
    hazards = c.CoefTable("coef_hr", ct["hazard_ratios"], ("hazard_ratio", "hr_ci_low", "hr_ci_high", *(("vif",) if variant == "ph" else ())),
                          width=26, fmt=SMALL)
    cox = c.CoefTable("coef_cox", ct["cox"], ("cox_hazard_ratio", "cox_hr_ci_low", "cox_hr_ci_high"), width=30, fmt=SMALL)
    tables = (estimates, *(() if variant == "ph" else (ratios,)), *(() if variant == "aft" else (hazards, cox)))
    loglog = variant in ("ph_aft", "auto")
    alpha_note = c.alpha_said(w["alpha_note"], checked.get("alpha", 0.05), locale)
    blocks = (*((_candidates(checked),) if variant == "auto" else ()), _fit_to_the_data(loglog), _profile(checked), _influence)
    return c.page(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale), cards=cards,
                  coef_tables=tables, coef_note=note, assumptions_note=alpha_note, fit_format=SMALL, room=12.0,
                  after_assumptions=(c.measures(MEASURES),), coef_first=(_ratio_charts(variant),), after_coefficients=(_parameters,),
                  blocks=blocks)


register("parametric_survival", layout, name, about)
