"""The report of the node «Risk simulation» (docs/specs/garch-risk.md, part 2, «The report (pixel perfect)»): N simulated
futures of a fitted GARCH or ARIMA, the risk measures read from them with their Monte Carlo intervals, and the backtest of
the model's 1-step value at risk on the holdout.

1. Summary: four cards (the simulation, the model, the VaR and the ES at their levels), the formula line, the checks of the
   model fitted (and the backtest's three tests with a holdout), the measures under «Measures, not tests» with their Monte
   Carlo intervals, the readings of the summary, of the VaR and of the ES.
2. The fan: the last observations, the bands of the paths nested from the widest (lighter outward), the median, the
   sample paths as thin grey lines, the threshold, the last observation marked; the readings of the fan and the threshold.
3. The outcome at the horizon: the histogram of the risk variable with the VaR and the ES (and the threshold on that
   scale), the histogram of the drawdown with its VaR; the readings of the shape and the drawdown.
4. Through the horizon: the VaR and the ES at each step with their intervals; with a threshold, its two probabilities.
5. Backtest (with a holdout): the page of the GARCH report (`garch.backtest_page`).
6. The model simulated: its coefficients, and the line that sends to the full report of the GARCH or ARIMA node.
7. How this report was computed (`common.how_computed`), the numbers of the simulation, the notes.

The model simulated comes before the backtest, whose chart is 60 mm (lot G9): the model's short tables fill the page the
charts above leave, where the backtest's chart, a block of 90 mm, left half a page empty. With a position the measures' table says the
VaR and the ES in money too (the runner's `risk|var||money`, `risk|es||money`), and their readings say it.

A measure of a relative change (a GARCH's cumulative return, an ARIMA's change relative to the last value) is written as a
percentage; the value or the running total of an ARIMA in the series' units. The explanation is the runner's (`reading`
rows; the contract is docs/specs/garch-risk.md, «As built / G4»). The words are in `report_texts/risk_simulation.json`.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import garch as g
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats

WORDS = "risk_simulation"
MEASURES = ("var", "var_money", "es", "es_money", "prob_beyond", "prob_crossed", "drawdown", "mean", "sd", "skewness", "kurtosis")
NBSP = "\u00a0"
FAN = '[section] = "curve" AND [level] = "fan" AND [level_2] <> "band_error"'
BAND = "#3b5bdb"
HISTORY = "#495057"
PATH = "#adb5bd"
FIT_SKIP = ("first", "last")


def _checked(params: dict[str, Any]) -> dict[str, Any]:
    return catalog.check_params(WORDS, dict(params))


def model_name(params: dict[str, Any], w: dict) -> str:
    """«GJR-GARCH(1,1)», «SARIMA(1,0,1)(0,1,1)[7]», «ARIMA (automatic orders)» in the page's words."""
    checked = _checked(params)
    if checked.get("model") == "arima":
        return w["auto_name"] if checked.get("orders") == "auto" else catalog.arima_name(params)
    return g.core({k: params[k] for k in catalog.MODELS["garch"]["takes"] if k in params})


def name(params: dict[str, Any], locale: str) -> str:
    """«Risk simulation · GJR-GARCH(1,1)»."""
    w = c.words(WORDS, catalog.variant(WORDS, _checked(params)), locale)
    return f"{w['name']} · {model_name(params, w)}"


def about(params: dict[str, Any], locale: str) -> str:
    w = c.words(WORDS, catalog.variant(WORDS, _checked(params)), locale)
    value, time = params.get("value"), params.get("time")
    if not (value and time):
        return str(value or "")
    return w["series_of"].replace("{value}", str(value)).replace("{time}", str(time))


def relative(checked: dict[str, Any]) -> bool:
    """The risk variable is a relative change (a GARCH's cumulative return, an ARIMA's `relative_change`): its measures are
    percentages; else the series' own units."""
    return checked.get("model") == "garch" or checked.get("aggregate") == "relative_change"


def pct(x: str) -> str:
    """A share as a percent, its «%» held to the number by a no-break space: a narrow column never ends a line with it alone."""
    return c.pct(x, "0.##").replace('" %"', f'"{NBSP}%"')


def said(x: str, rel: bool) -> str:
    """A value of the risk variable as the page writes it, «–» for none."""
    return f'IF(ISNULL({x}), "–", {pct(x) if rel else c.shown(x, "#,##0.##")})'


def unit_of(w: dict, checked: dict[str, Any]) -> str:
    return w["values"]["time_unit"].get(str(checked.get("frequency")), w["values"]["time_unit"]["index"])


def level_said(x: float, lang: str) -> str:
    return g.numbers("{x}", lang, x=x * 100)


# ── page 1 ──────────────────────────────────────────────────────────────────
def _cards(p: stats.Page, w: dict, lang: str, params: dict[str, Any], checked: dict[str, Any]) -> None:
    from app.reports import formats

    k = w["kpi"]
    n = c.model_number
    unit = unit_of(w, checked)
    value = c.join(k["paths"], paths=c.shown(n("paths"), "#,##0"), steps=c.shown(n("horizon"), "#,##0"))
    loc = formats.locale(lang)
    widest = k["paths"].replace("{paths}", formats.format_number(float(checked.get("paths") or 0), "#,##0", loc)).replace(
        "{steps}", formats.format_number(float(checked.get("horizon") or 0), "#,##0", loc))
    g.card(p, "k_simulation", 0, k["simulation"].replace("{unit}", unit), value,
           [(c.join(k["values"], n=c.shown(n("simulated_values"), "#,##0"), seed=c.shown(n("seed"), "0")), None)], widest=widest)
    model = model_name(params, w)
    under = c.q(g.law(w, checked)) if checked.get("model") == "garch" else c.q(w["values"]["aggregate"].get(
        str(checked.get("aggregate") or "last"), ""))
    g.card(p, "k_model", 47, k["model"], c.q(model), [(under, None)], widest=model)
    rel = relative(checked)
    for id_, x, key, level in (("k_var", 94, "var", checked.get("var_level")), ("k_es", 141, "es", checked.get("es_level"))):
        value = c.num(f"risk|{key}||value")
        low, high = c.num(f"risk|{key}||low"), c.num(f"risk|{key}||high")
        interval = f'IF(ISNULL({low}), "", {said(low, rel)} & " – " & {said(high, rel)})'
        g.card(p, id_, x, k[key].replace("{level}", level_said(float(level or 0), lang)), said(value, rel), [(interval, None)])
    p.y += 21.5


def _measures(p: stats.Page, w: dict, checked: dict[str, Any]) -> None:
    """The risk measures at the horizon, each with its Monte Carlo interval and what it is: no verdict, no colour."""
    m = w["measures"]
    rel = relative(checked)
    # with a position, the VaR's and the ES's money (position × the value) on rows of their own, in the series' money: a
    # number with no % and no interval (the runner writes the money of the value only)
    one = ('[section] = "risk" AND (ISNULL([level_2]) OR [level_2] = "") AND '
           '(([stat] = "value" AND [level] <> "moments") OR [stat] = "money" OR ([level] = "moments" AND ([stat] = "mean" OR '
           '[stat] = "sd" OR [stat] = "skewness" OR [stat] = "kurtosis")))')
    key = 'IFS([level] = "moments", [stat], [stat] = "money", [level] & "_money", TRUE, [level])'
    label = "IFS(" + ", ".join(f'{key} = "{s}", {c.q(w["risk_names"][s])}' for s in MEASURES) + ", TRUE, [level])"
    share = 'OR '.join(f'{key} = "{s}" ' for s in ("prob_beyond", "prob_crossed", "drawdown"))

    def shown(x: str) -> str:
        return (f'IF(ISNULL({x}), "", IFS({share}, {pct(x)}, {key} = "skewness" OR {key} = "kurtosis", '
                f'FORMAT({x}, "0.###"), [stat] = "money", {c.shown(x, "#,##0.##")}, TRUE, {said(x, rel)}))')

    low = 'LOOKUP("risk|" & [level] & "||low", [key], [value])'
    high = 'LOOKUP("risk|" & [level] & "||high", [key], [value])'
    interval = f'IF([level] = "moments" OR [stat] = "money" OR ISNULL({low}), "", {shown(low)} & " – " & {shown(high)})'
    what = "IFS(" + ", ".join(f'{key} = "{s}", {c.q(w["risk_what"][s])}' for s in MEASURES) + ', TRUE, "")'
    t = stats.table("measures", m["title"], one, [
        {"header": m["measure"], "value": label, "width": 42},
        {"header": m["value"], "value": shown("[value]"), "width": 21, "align": "right"},
        {"header": m["interval"], "value": interval, "width": 40, "align": "right"},
        {"header": m["what"], "value": what, "style": {"size": 7.5}},
    ], p.y, visible=c.exists(one))
    t["sort"] = [{"by": "IFS(" + ", ".join(f'{key} = "{s}", {i}' for i, s in enumerate(MEASURES)) + ", TRUE, 99)"}]
    p.add(t, 10)


def _summary(p: stats.Page, w: dict, lang: str, params: dict[str, Any], checked: dict[str, Any]) -> None:
    p.heading("s_summary", w["sections"]["summary"])
    _cards(p, w, lang, params, checked)
    p.keep()
    if checked.get("model") == "garch":
        p.text("formula", stats.static(g.formula(w, checked)), h=6.5, size=9.5, weight="semibold", background=c.PANEL,
               padding=1.5, can_grow=True)
    else:
        p.text("formula", "=" + c.model_text("formula"), h=6.5, size=9.5, weight="semibold", background=c.PANEL, padding=1.5,
               can_grow=True, visible=c.exists('[key] = "model|||formula"'))
    c.reading_under(p, w, "summary")
    _measures(p, w, checked)
    c.reading_under(p, w, "var", "es")
    c._assumptions(p, w, lang, g.numbers(w["alpha_line"], lang, alpha=float(checked.get("alpha") or 0.05)), True)
    g.first_level_only(p)
    if (checked.get("holdout") or 0) > 0:
        g.backtest_table(p, w, lang, checked)


# ── page 2: the fan ─────────────────────────────────────────────────────────
def bands(checked: dict[str, Any]) -> tuple[list[tuple[str, str, float]], str | None]:
    """The fan's bands, nested from the widest: (low quantile's name, high quantile's name, the share of the paths between
    them), each quantile below 0.5 paired with its mirror above; and the median's name when 0.5 is asked."""
    levels = sorted({float(x) for x in checked.get("quantiles") or []})
    named = {x: catalog._number_label(x) for x in levels}
    low, high = [x for x in levels if x < 0.5], [x for x in levels if x > 0.5][::-1]
    pairs = [(f"quantile_{named[a]}", f"quantile_{named[b]}", b - a) for a, b in zip(low, high)]
    return pairs, (f"quantile_{named[0.5]}" if 0.5 in named else None)


def _fan(p: stats.Page, w: dict, lang: str, checked: dict[str, Any]) -> None:
    s = w["series"]
    g.heading(p, "s_fan", w["sections"]["fan"])
    pairs, median = bands(checked)
    series: list[dict] = []
    for lo, hi, share in pairs:
        series += [g.line(lo, s["band"].replace("{share}", level_said(share, lang)), "band_low", BAND), g.line(hi, "", "band_high", BAND)]
    if median:
        series.append(g.line(median, s["median"], colour=c.ACCENT))
    series.append(g.line("history", s["history"], colour=HISTORY))
    series.append({"value": 'IF(STARTSWITH([level_2], "path_"), [value], NULL)', "as": "line", "name": s["path"], "color": PATH})
    last = 'MAX(IF([section] = "curve" AND [level] = "fan" AND [level_2] = "history", [x0], NULL))'
    item = g.chart("fan", w["charts"]["fan"], FAN, series, p.y, 110,
                   {"legend": False, "target": c.model_number("threshold"), "target_x": last})
    item["split_by"] = 'IF(STARTSWITH([level_2], "path_"), [level_2], "fan")'
    g.twice(p, item, w)
    p.y += 111.5
    g.caption(p, "fan_how", w["captions"]["fan"], visible=c.exists(FAN))
    c.reading_under(p, w, "fan", "threshold")


# ── page 3: the outcome at the horizon ──────────────────────────────────────
def histogram(id_: str, title: str, level: str, name_: str, targets: list[str], tall: float, y: float) -> dict:
    f = f'[section] = "hist" AND [level] = "{level}"'
    item = g.chart(id_, title, f, [{"value": "[value]", "as": "stem", "name": name_}], y, tall,
                   {"legend": False, "format": "#,##0.####", "target_x": targets}, category="([x0] + [x1]) / 2")
    return item


def _outcome(p: stats.Page, w: dict, checked: dict[str, Any]) -> None:
    ch, s = w["charts"], w["series"]
    g.heading(p, "s_outcome", w["sections"]["outcome"])
    targets = [c.num("risk|var||value"), c.num("risk|es||value")]
    # the threshold is on the histogram's scale when the risk variable is what the fan shows (a GARCH's cumulative return of
    # returns given as returns, an ARIMA's value or running total)
    if (checked.get("model") == "garch" and checked.get("input_kind") != "prices") or (
            checked.get("model") == "arima" and checked.get("aggregate") in ("last", "sum")):
        targets.append(c.model_number("threshold"))
    p.add(histogram("horizon", ch["horizon"], "horizon", s["count"], targets, 80, p.y))
    p.y += 81.5
    g.caption(p, "outcome_how", w["captions"]["outcome"], visible=c.exists('[section] = "hist" AND [level] = "horizon"'))
    p.y += 1.5
    p.add(histogram("drawdown", ch["drawdown"], "drawdown", s["count"], [c.num("risk|drawdown||value")], 60, p.y))
    p.items[-1]["collapse_hidden"] = True
    p.y += 61.5
    c.reading_under(p, w, "shape", "drawdown")


# ── page 4: through the horizon ─────────────────────────────────────────────
def _term(p: stats.Page, w: dict) -> None:
    ch, s = w["charts"], w["series"]
    g.heading(p, "s_term", w["sections"]["term"])
    f = '[section] = "curve" AND [level] = "term" AND ([level_2] = "var" OR [level_2] = "es" OR [level_2] = "var_low" OR ' \
        '[level_2] = "var_high" OR [level_2] = "es_low" OR [level_2] = "es_high")'
    g.twice(p, g.chart("term", ch["term"], f, [
        g.line("var_low", s["var_band"], "band_low", c.ACCENT), g.line("var_high", "", "band_high", c.ACCENT),
        g.line("es_low", s["es_band"], "band_low", c.MID), g.line("es_high", "", "band_high", c.MID),
        g.line("var", s["var"], colour=c.ACCENT), g.line("es", s["es"], colour=c.MID)], p.y, 60), w)
    p.y += 61.5
    probabilities = '[section] = "curve" AND [level] = "term" AND ([level_2] = "prob_crossed" OR [level_2] = "prob_beyond")'
    g.twice(p, g.chart("probabilities", ch["probabilities"], probabilities, [
        g.line("prob_crossed", s["prob_crossed"], colour=c.ACCENT), g.line("prob_beyond", s["prob_beyond"], colour=c.MID)],
        p.y, 50, {"format": "0.###"}), w)
    p.y += 51.5


# ── page 6: the model ───────────────────────────────────────────────────────
def _model(p: stats.Page, w: dict, lang: str, checked: dict[str, Any]) -> None:
    level = g.numbers("{x}", lang, x=(1 - float(checked.get("alpha") or 0.05)) * 100)
    tables = (c.CoefTable("coef", w["coef_title"], ("estimate", "std_error", "statistic", "p_value")),
              c.CoefTable("coef_ci", w["coef_intervals"].replace("{level}", level), ("ci_low", "ci_high"), width=34))
    c._coefficients(p, {**w, "sections": {**w["sections"], "coefficients": w["sections"]["model"]}}, lang, tables, None)
    for it in p.items:
        if it["id"] in ("coef", "coef_ci"):
            it["columns"][0]["value"] = g.symbol_of("[level]", w)
    p.text("full_report", stats.static(w["full_report"][str(checked.get("model") or "garch")]), h=5, size=8.5, color=c.MUTED,
           can_grow=True)
    c.reading_under(p, w, "model")


# ── the page ────────────────────────────────────────────────────────────────
def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    from app.reports import formats

    lang = c.code(locale)
    checked = _checked(params)
    w = c.words(model, catalog.variant(model, checked), lang)
    header, footer = stats.bands(name(params, lang), " · ".join(x for x in (about(params, lang), step) if x), w,
                                 formats.locale(lang).texts, stats.sample_line(sample, lang))
    p = stats.Page()
    _summary(p, w, lang, params, checked)
    _fan(p, w, lang, checked)
    _outcome(p, w, checked)
    _term(p, w)
    _model(p, w, lang, checked)
    if (checked.get("holdout") or 0) > 0:
        # 60 mm, as the charts through the horizon: the GARCH report's 90 mm left the page above it half empty (lot G9)
        g.backtest_page(p, w, w["series"]["observed"], 60.0)
    c.how_computed(p, w)
    c._fit(p, w, False, FIT_SKIP)
    c._notes(p, w)
    g.whole_tables(p)
    return c.document(header, p.items, footer, lang)


# ── the explanation (docs/specs/garch-risk.md, «As built / G4») ─────────────
# Chosen from the rows the runner (lot G3) writes, as the GARCH node's: the risk measures at the horizon are measures, said
# as the rows hold them, in % for a relative change and in the series' units otherwise (the id's `_level`); the fitted
# model's checks and the backtest are verdicts against the node's alpha (`garch.check_lines`, `garch.backtest_lines`).
ARIMA_TESTS = ("ljung_box_10", "ljung_box_2s", "arch_lm", "variance_ratio", "jarque_bera")


def variable_of(checked: Mapping[str, Any]) -> str:
    """The risk variable: a GARCH's cumulative return, an ARIMA's value, running total or relative change."""
    if checked.get("model") == "garch":
        return "cumulative_return"
    return {"last": "value", "sum": "running_total"}.get(str(checked.get("aggregate") or "last"), "relative_change")


def readings(rows: list[dict], checked: Mapping[str, Any]) -> list[c.ReadingLine]:
    alpha = float(checked.get("alpha") or 0.05)
    r = g.Rows(rows)
    num = lambda stat: r.num(c.model_key(stat))  # noqa: E731
    garch = checked.get("model") == "garch"
    unit = checked.get("frequency") or "index"
    steps = num("horizon") or checked.get("horizon")
    variable = "variable:" + variable_of(checked)
    out: list[c.ReadingLine] = []
    # summary
    # the model by its orders: a GARCH's from the options, an ARIMA's as the runner chose them (automatic orders too)
    model = (g.core({k: v for k, v in checked.items() if k in catalog.MODELS["garch"]["takes"]}) if garch
             else r.text(c.model_key("model_name")) or catalog.arima_name(dict(checked)))
    head = {"paths": num("paths") or checked.get("paths"), "steps": steps, "unit": unit, "model": model}
    if garch:
        head["distribution"] = checked.get("distribution") or "std"
    out.append(("summary", None, "summary_garch" if garch else "summary_arima",
                {**head, "series": checked.get("value"), "seed": num("seed") if num("seed") is not None else checked.get("seed")}))
    if checked.get("innovations") == "bootstrap":
        out.append(("summary", None, "summary_bootstrap", {}))
    out.append(("summary", None, "summary_variable", {"variable": variable, "steps": steps, "unit": unit}))
    holdout = int(num("holdout") or 0)
    if holdout > 0:
        out.append(("summary", None, "summary_holdout", {"holdout": holdout, "unit": unit}))
    # the risk measures at the horizon
    side = "upper" if checked.get("tail") == "upper" else "lower"
    scale = "" if relative(dict(checked)) else "_level"
    risk = lambda level, stat: r.num(f"risk|{level}||{stat}")  # noqa: E731
    position = num("position")
    if risk("var", "value") is not None:
        out.append(("var", None, f"var_{side}{scale}", {"p": 1 - float(checked.get("var_level") or 0.99), "variable": variable,
                                                        "steps": steps, "unit": unit, "value": risk("var", "value"),
                                                        "low": risk("var", "low"), "high": risk("var", "high"),
                                                        "paths": head["paths"], "confidence": 1 - alpha}))
        if position is not None and risk("var", "money") is not None:
            # «amount», not «position»: a position is a name of the notes (a season's), never read as a number
            out.append(("var", None, "var_money", {"amount": position, "money": risk("var", "money")}))
    if risk("es", "value") is not None:
        es = {"p": 1 - float(checked.get("es_level") or 0.975), "variable": variable, "value": risk("es", "value"),
              "low": risk("es", "low"), "high": risk("es", "high")}
        # the quantile the ES averages beyond (the VaR at the ES's level): the last step of its curve (runners since lot G9)
        quantile = [(x.get("x0"), v) for x in rows if x["section"] == "curve" and x["level"] == "term"
                    and x.get("level_2") == "es_quantile" and (v := g.number(x.get("value"))) is not None]
        if quantile:
            es["quantile"] = max(quantile, key=lambda a: a[0] if isinstance(a[0], (int, float)) else float("-inf"))[1]
        out.append(("es", None, f"es_{side}{scale}" + ("_quantile" if quantile else ""), es))
        if position is not None and risk("es", "money") is not None:
            out.append(("es", None, "es_money", {"amount": position, "money": risk("es", "money")}))
    threshold = num("threshold")
    if threshold is not None and risk("prob_crossed", "value") is not None:
        on = ("variable:price" if checked.get("input_kind") == "prices" else variable) if garch else \
            ("variable:running_total" if checked.get("aggregate") == "sum" else "variable:value")
        level = "" if garch and checked.get("input_kind") != "prices" else "_level"
        out.append(("threshold", None, f"threshold_{side}{level}", {
            "variable": on, "threshold": threshold, "steps": steps, "unit": unit, "prob_crossed": risk("prob_crossed", "value"),
            "low": risk("prob_crossed", "low"), "high": risk("prob_crossed", "high"), "prob_beyond": risk("prob_beyond", "value")}))
    if risk("drawdown", "value") is not None and risk("drawdown", "median") is not None:
        out.append(("drawdown", None, "drawdown", {"median": risk("drawdown", "median"), "tail": risk("drawdown", "value"),
                                                   "p": 1 - float(checked.get("var_level") or 0.99), "steps": steps, "unit": unit}))
    if risk("moments", "skewness") is not None:
        out.append(("shape", None, "shape", {"variable": variable, "steps": steps, "unit": unit,
                                             "skewness": risk("moments", "skewness"), "kurtosis": risk("moments", "kurtosis")}))
    # the fan: its bands' shares (the node's quantiles), the largest error of their edges, the sample paths drawn
    pairs, _ = bands(dict(checked))
    errors = [v for x in rows if x["section"] == "curve" and x["level"] == "fan" and x.get("level_2") == "band_error"
              and (v := g.number(x.get("value"))) is not None]
    sample = {x["level_2"] for x in rows if x["section"] == "curve" and x["level"] == "fan" and str(x.get("level_2") or "").startswith("path_")}
    if pairs and errors:
        shares = {"shares": sorted(round(share, 10) for _, _, share in pairs), "error": max(errors)}
        out.append(("fan", None, "fan", {**shares, "sample": len(sample)}) if sample else ("fan", None, "fan_bands", shares))
    # the model's checks
    if garch:
        out += g.check_lines(r, alpha, "model", "model", "model")
    else:
        tested = [(t, p) for t in ARIMA_TESTS if (p := r.p(t)) is not None]
        rejected = [(t, p) for t, p in tested if p < alpha]
        out += [("model", None, "arima_check_failed", {"check": f"check:{t}", "p_value": p, "alpha": alpha}) for t, p in rejected]
        if tested and not rejected:
            out.append(("model", None, "arima_checks_ok", {"total": len(tested), "alpha": alpha}))
    out += g.backtest_lines(r, checked, alpha)
    # «How this report was computed»
    method = ["method_simulation", "method_seed", "method_innovations", "method_variable", "method_var", "method_es"]
    if threshold is not None:
        method.append("method_threshold")
    if risk("drawdown", "value") is not None:
        method.append("method_drawdown")
    method.append("method_passes")
    if holdout > 0:
        method.append("method_backtest")
    method.append("method_model_garch" if garch else "method_model_arima")
    formulas = ["formula_garch_path" if garch else "formula_arima_path", "formula_variable", "formula_var", "formula_es"]
    if threshold is not None:
        formulas.append("formula_clopper_pearson")
    if risk("drawdown", "value") is not None:
        formulas.append("formula_drawdown")
    formulas += ["formula_moments", "formula_band_error"]
    if holdout > 0:
        formulas += ["formula_var_1step", "formula_kupiec", "formula_christoffersen"]
    out += [("method", None, m, {}) for m in method]
    out += [("formulas", None, x, {}) for x in formulas]
    return out


c.READINGS["risk_simulation"] = readings

register("risk_simulation", layout, name, about)
